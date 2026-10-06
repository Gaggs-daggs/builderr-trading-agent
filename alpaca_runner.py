#!/usr/bin/env python3
"""Alpaca PAPER-trading runner for agent.py.

    python alpaca_runner.py                  # dry run (default): prints the orders, sends nothing
    python alpaca_runner.py --live-paper     # submits market orders to the Alpaca PAPER account

Once per trading day, shortly before the close (default 15:45 ET) it
  1. fetches daily bars, positions and cash from Alpaca,
  2. builds market_state / portfolio_state / cash exactly as live_runner.py does
     (the same 42-ticker feed, up to 309 daily bars per ticker as dicts with
     ts/open/high/low/close/volume, dividend/split-adjusted, last_prices = latest close),
  3. calls agent.decide() unchanged,
  4. converts its orders to whole-share market orders (sells first, at most 45),
  5. recomputes the post-trade book and ABORTS the whole run if beta-adjusted gross
     would exceed 1.45x or any position 28% (beta table = agent.py's),
  6. sends the orders (only with --live-paper) and appends a row to runs.csv.

Safety rules baked in:
  * paper only: TradingClient(paper=True) is hard-coded and the client's endpoint must be
    paper-api.alpaca.markets or the runner refuses to start. There is no live switch.
  * keys come from ALPACA_API_KEY / ALPACA_SECRET_KEY in the environment only, and are
    redacted from every log line and message.
  * skipped (nothing sent) when the market is closed, outside the run window, data is stale
    or missing, decide() fails, orders are already open, or the account is unusable;
  * the paper account must be dedicated to this bot: foreign or short positions abort the run.

Differences from the scoring engine (see README): it decides on bars that include today's
partial bar at ~15:45 and trades at ~15:45, not on the prior close with next-open fills, and
the free Alpaca feed (IEX) is not the consolidated tape yfinance uses.
"""
from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import logging
import math
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
NY = ZoneInfo("America/New_York")

PAPER_HOST = "paper-api.alpaca.markets"
HISTORY = 309                  # bars per ticker the live board hands to decide()
MAX_ORDERS = 45                # contest limit is 50 trades/day
GROSS_LIMIT = 1.45             # abort above this beta-adjusted gross (contest limit 1.5x)
POSITION_LIMIT = 0.28          # abort above this single-name weight (contest limit 30%)
CASH_TOLERANCE = 0.001         # of equity: how far below zero estimated post-trade cash may go
MAX_DAILY_MOVE = 0.35          # a bigger last-day move in any fed ticker looks like bad data
RUN_AT = "15:45"
WINDOW_MIN = 10
CLOSE_GUARD_MIN = 2            # never submit within this many minutes of the close
FILL_TIMEOUT_S = 90
HISTORY_CALENDAR_DAYS = 540
RUNS_FIELDS = ["date", "time_et", "mode", "status", "reason", "tier", "positions_before", "positions_after",
               "orders", "gross_before", "gross_after", "max_position", "equity", "cash"]

log = logging.getLogger("alpaca_runner")


# ------------------------------------------------------------------ broker interface
@dataclass
class Clock:
    now: datetime
    is_open: bool


@dataclass
class Session:
    day: date
    open: datetime
    close: datetime


@dataclass
class Account:
    cash: float
    equity: float
    status: str = "ACTIVE"
    blocked: bool = False


@dataclass
class Position:
    symbol: str
    qty: float
    avg_entry_price: float = 0.0


@dataclass
class RunResult:
    status: str                      # dry-run | submitted | partial | skipped | aborted | error
    reason: str = ""
    mode: str = "dry-run"
    session: Optional[date] = None
    now_et: Optional[datetime] = None
    tier: str = ""
    orders: list = field(default_factory=list)
    positions_before: dict = field(default_factory=dict)
    positions_after: dict = field(default_factory=dict)
    gross_before: float = 0.0
    gross_after: float = 0.0
    max_position: float = 0.0
    equity: float = 0.0
    cash: float = 0.0
    book: dict = field(default_factory=dict)


class SkipRun(Exception):
    """Nothing is sent; the run is recorded as skipped."""


class AbortRun(Exception):
    """A safety limit was hit; the whole run is aborted (nothing is sent)."""


class AlpacaBroker:
    """Thin adapter over alpaca-py. Everything the runner needs from Alpaca goes through here."""

    def __init__(self, trading: Any, data: Any):
        self.trading, self.data = trading, data
        self.assert_paper()

    # -- paper-only guard (checked at construction and again before every order)
    def base_url(self) -> str:
        v = getattr(self.trading, "_base_url", None)
        return str(getattr(v, "value", v))

    def assert_paper(self) -> None:
        host = urlparse(self.base_url()).hostname
        if host != PAPER_HOST:
            raise SystemExit(f"refusing to run: trading endpoint is {host!r}, not the paper endpoint {PAPER_HOST!r}")

    @staticmethod
    def _ny(dt: datetime) -> datetime:
        return dt.replace(tzinfo=NY) if dt.tzinfo is None else dt.astimezone(NY)

    def get_clock(self) -> Clock:
        c = self.trading.get_clock()
        return Clock(now=c.timestamp, is_open=bool(c.is_open))

    def get_sessions(self, start: date, end: date) -> list[Session]:
        from alpaca.trading.requests import GetCalendarRequest
        cal = self.trading.get_calendar(GetCalendarRequest(start=start, end=end))
        return [Session(day=c.date, open=self._ny(c.open), close=self._ny(c.close)) for c in cal]

    def get_account(self) -> Account:
        a = self.trading.get_account()
        status = str(getattr(a.status, "value", a.status)).upper()
        return Account(cash=float(a.cash), equity=float(a.equity), status=status,
                       blocked=bool(a.trading_blocked or a.account_blocked))

    def get_positions(self) -> list[Position]:
        out = []
        for p in self.trading.get_all_positions():
            qty = float(p.qty)
            if str(getattr(p.side, "value", p.side)).lower() == "short" and qty > 0:
                qty = -qty
            out.append(Position(symbol=str(p.symbol).upper(), qty=qty, avg_entry_price=float(p.avg_entry_price or 0.0)))
        return out

    def get_open_orders(self) -> list[dict]:
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest
        orders = self.trading.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN))
        return [{"id": str(o.id), "symbol": o.symbol} for o in orders]

    def get_bars(self, tickers: list[str], start: datetime, feed: str) -> dict[str, list[dict]]:
        from alpaca.data.enums import Adjustment, DataFeed
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame
        req = StockBarsRequest(symbol_or_symbols=list(tickers), timeframe=TimeFrame.Day, start=start,
                               adjustment=Adjustment.ALL, feed=DataFeed(feed))
        res = self.data.get_stock_bars(req)
        out: dict[str, list[dict]] = {}
        for sym, bars in res.data.items():
            by_day: dict[str, dict] = {}
            for b in bars:
                day = b.timestamp.astimezone(NY).strftime("%Y-%m-%d")
                by_day[day] = {"ts": day, "open": float(b.open), "high": float(b.high), "low": float(b.low),
                               "close": float(b.close), "volume": int(b.volume or 0)}
            out[str(sym).upper()] = [by_day[d] for d in sorted(by_day)]
        return out

    def submit_market_order(self, symbol: str, side: str, qty: int, client_order_id: str) -> str:
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import MarketOrderRequest
        self.assert_paper()
        req = MarketOrderRequest(symbol=symbol, qty=int(qty),
                                 side=OrderSide.SELL if side == "sell" else OrderSide.BUY,
                                 time_in_force=TimeInForce.DAY, client_order_id=client_order_id)
        return str(self.trading.submit_order(req).id)

    def wait_for_fill(self, order_id: str, timeout_s: float = FILL_TIMEOUT_S) -> dict:
        deadline = time.monotonic() + timeout_s
        while True:
            o = self.trading.get_order_by_id(order_id)
            status = str(getattr(o.status, "value", o.status)).lower()
            if status in ("filled", "canceled", "expired", "rejected", "done_for_day") or time.monotonic() >= deadline:
                return {"status": status, "filled": status == "filled", "qty": o.filled_qty,
                        "price": o.filled_avg_price}
            time.sleep(1.0)


def make_alpaca_broker() -> AlpacaBroker:
    key, secret = os.environ.get("ALPACA_API_KEY"), os.environ.get("ALPACA_SECRET_KEY")
    if not key or not secret:
        raise SystemExit("set ALPACA_API_KEY and ALPACA_SECRET_KEY in the environment (paper-trading keys)")
    try:
        from alpaca.data.historical import StockHistoricalDataClient
        from alpaca.trading.client import TradingClient
    except ImportError:
        raise SystemExit("alpaca-py is not installed: python3 -m venv .venv && .venv/bin/pip install alpaca-py")
    trading = TradingClient(key, secret, paper=True)       # paper is hard-coded; url_override is never used
    return AlpacaBroker(trading, StockHistoricalDataClient(key, secret))


# ------------------------------------------------------------------ logging / secrets
def redact(text: Any) -> str:
    s = str(text)
    for name in ("ALPACA_API_KEY", "ALPACA_SECRET_KEY"):
        v = os.environ.get(name)
        if v:
            s = s.replace(v, "***")
    return s


class _RedactFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(record.getMessage())
        record.args = ()
        return True


def setup_logging(log_dir: Path, verbose: bool = False) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    log.setLevel(logging.DEBUG if verbose else logging.INFO)
    for h in list(log.handlers):
        log.removeHandler(h)
        h.close()
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    for h in (logging.FileHandler(log_dir / "alpaca_runner.log"), logging.StreamHandler(sys.stdout)):
        h.setFormatter(fmt)
        h.addFilter(_RedactFilter())
        log.addHandler(h)
    log.propagate = False


# ------------------------------------------------------------------ agent + inputs
def load_agent(path: Path = HERE / "agent.py") -> Any:
    spec = importlib.util.spec_from_file_location("agent", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def feed_tickers(universe_path: Path = HERE / "universe.json") -> list[str]:
    """The tickers the live board feeds to decide(): live_runner.ROUND2_FETCH_UNIVERSE within universe.json."""
    sys.path.insert(0, str(HERE))
    from fetch_history import LIVE_FEED
    frozen = set(json.loads(universe_path.read_text())["tickers"])
    return [t for t in LIVE_FEED if t in frozen]


def agent_tickers(agent: Any) -> set[str]:
    books = (agent.BOOK_ON, agent.BOOK_MID, agent.BOOK_HALF, agent.BOOK_OFF)
    return {t for b in books for t in b} | {"QQQ", "SPY"}


def min_bars(agent: Any) -> int:
    return max(agent.TREND_LOOKBACKS) + agent.SMOOTH_DAYS - 1


def build_market_state(raw: dict[str, list[dict]], tickers: list[str], history: int = HISTORY) -> dict[str, list[dict]]:
    """{ticker: last `history` bars}, each bar exactly {ts, open, high, low, close, volume}, oldest first."""
    state = {}
    for t in tickers:
        rows = raw.get(t)
        if rows:
            state[t] = [{"ts": r["ts"], "open": r["open"], "high": r["high"], "low": r["low"],
                         "close": r["close"], "volume": int(r["volume"])} for r in rows[-history:]]
    return state


def build_portfolio_state(cash: float, held: dict[str, float], avg_cost: dict[str, float],
                          market_state: dict[str, list[dict]]) -> dict:
    return {"cash": cash,
            "positions": [{"ticker": t, "quantity": q, "avg_cost": avg_cost.get(t, 0.0)}
                          for t, q in sorted(held.items()) if q > 0],
            "last_prices": {t: rows[-1]["close"] for t, rows in market_state.items() if rows}}


def validate_market_state(state: dict, required: set[str], expected_day: date, need_bars: int) -> Optional[str]:
    """Reason the data can't be used, or None if it is fresh and sane."""
    iso = expected_day.isoformat()
    for t in sorted(required):
        rows = state.get(t)
        if not rows:
            return f"missing data: no bars for {t}"
        if rows[-1]["ts"] != iso:
            return f"stale data: {t} last bar {rows[-1]['ts']}, expected {iso}"
    for t in ("QQQ", "SPY"):
        if len(state[t]) < need_bars:
            return f"short history: {t} has {len(state[t])} bars, need {need_bars}"
    for t, rows in state.items():
        last = rows[-1]
        for k in ("open", "high", "low", "close"):
            if not (isinstance(last[k], (int, float)) and math.isfinite(last[k]) and last[k] > 0):
                return f"bad data: {t} {k}={last[k]!r}"
        if len(rows) > 1 and abs(last["close"] / rows[-2]["close"] - 1.0) > MAX_DAILY_MOVE:
            return f"suspicious data: {t} moved {last['close'] / rows[-2]['close'] - 1.0:+.0%} on the last bar"
    return None


# ------------------------------------------------------------------ orders + safety layer
def convert_orders(raw_orders: Any, held: dict[str, float], allowed: set[str],
                   max_orders: int = MAX_ORDERS) -> tuple[list[dict], list[str]]:
    """decide() orders -> whole-share Alpaca market orders, sells first, at most `max_orders`."""
    sells: list[dict] = []
    buys: list[dict] = []
    dropped: list[str] = []
    for o in raw_orders if isinstance(raw_orders, list) else []:
        try:
            sym, side, qty = str(o["ticker"]).strip().upper(), o["side"], float(o["quantity"])
        except (KeyError, TypeError, ValueError, AttributeError):
            dropped.append(f"malformed order {o!r}")
            continue
        if side not in ("buy", "sell") or not math.isfinite(qty) or qty <= 0:
            dropped.append(f"invalid order {sym} {side} {qty}")
            continue
        if sym not in allowed:
            dropped.append(f"{sym} is outside the feed/universe")
            continue
        whole = math.floor(qty)
        if side == "sell":
            whole = min(whole, math.floor(held.get(sym, 0.0)))
        if whole < 1:
            dropped.append(f"{side} {sym} {qty:g} is under one whole share")
            continue
        (sells if side == "sell" else buys).append({"symbol": sym, "side": side, "qty": whole})
    ordered = sells + buys
    if len(ordered) > max_orders:
        dropped.append(f"{len(ordered) - max_orders} orders over the {max_orders}-order cap dropped (buys first)")
    return ordered[:max_orders], dropped


def post_trade_book(cash: float, held: dict[str, float], orders: list[dict], prices: dict[str, float],
                    beta: Callable[[str], float]) -> dict:
    """Apply the orders at last prices (sells, then buys) and measure the resulting book."""
    qty = dict(held)
    cash_after = cash
    for o in sorted(orders, key=lambda o: 0 if o["side"] == "sell" else 1):
        px = prices[o["symbol"]]
        if o["side"] == "sell":
            qty[o["symbol"]] = qty.get(o["symbol"], 0.0) - o["qty"]
            cash_after += o["qty"] * px
        else:
            qty[o["symbol"]] = qty.get(o["symbol"], 0.0) + o["qty"]
            cash_after -= o["qty"] * px
    qty = {t: q for t, q in qty.items() if abs(q) > 1e-9}
    equity = cash_after + sum(q * prices[t] for t, q in qty.items())
    weights = {t: q * prices[t] / equity for t, q in qty.items()} if equity > 0 else {}
    gross = sum(w * beta(t) for t, w in weights.items())
    top = max(weights, key=weights.get) if weights else ""
    return {"qty": qty, "cash": cash_after, "equity": equity, "weights": weights, "gross": gross,
            "max_weight": weights.get(top, 0.0), "max_ticker": top}


def safety_violation(book: dict) -> Optional[str]:
    if book["equity"] <= 0:
        return "post-trade equity is not positive"
    if any(q < 0 for q in book["qty"].values()):
        return "post-trade book would hold a short position"
    if book["gross"] > GROSS_LIMIT:
        return f"beta-adjusted gross {book['gross']:.3f}x would exceed {GROSS_LIMIT}x"
    if book["max_weight"] > POSITION_LIMIT:
        return f"{book['max_ticker']} would be {book['max_weight']:.1%}, over the {POSITION_LIMIT:.0%} limit"
    if book["cash"] < -CASH_TOLERANCE * book["equity"]:
        return f"orders would spend ${-book['cash']:,.0f} more than the cash on hand (margin)"
    return None


# ------------------------------------------------------------------ runs.csv
def fmt_positions(d: dict[str, float]) -> str:
    return " ".join(f"{t}:{q:g}" for t, q in sorted(d.items()))


def fmt_orders(orders: list[dict]) -> str:
    return "; ".join(f"{o['side']} {o['symbol']} {o['qty']}" for o in orders)


def log_run(path: Path, r: RunResult) -> None:
    new = not path.exists()
    with open(path, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(RUNS_FIELDS)
        w.writerow([r.session.isoformat() if r.session else "", r.now_et.isoformat(timespec="seconds") if r.now_et else "",
                    r.mode, r.status, redact(r.reason), r.tier, fmt_positions(r.positions_before),
                    fmt_positions(r.positions_after), fmt_orders(r.orders), f"{r.gross_before:.3f}",
                    f"{r.gross_after:.3f}", f"{r.max_position:.4f}", f"{r.equity:.2f}", f"{r.cash:.2f}"])


def already_ran(path: Path, session: date) -> bool:
    if not path.exists():
        return False
    with open(path, newline="") as f:
        return any(row["date"] == session.isoformat() and row["mode"] == "live-paper"
                   and row["status"] in ("submitted", "partial") for row in csv.DictReader(f))


# ------------------------------------------------------------------ the run
def _run_window(now_et: datetime, run_at: str, window_min: int) -> tuple[datetime, datetime]:
    h, m = (int(x) for x in run_at.split(":"))
    start = datetime.combine(now_et.date(), dtime(h, m), tzinfo=NY)
    return start, start + timedelta(minutes=window_min)


def run_once(broker: Any, agent: Any, *, live: bool = False, run_at: str = RUN_AT, window_min: int = WINDOW_MIN,
             ignore_window: bool = False, allow_closed: bool = False, feed: str = "iex", force: bool = False,
             runs_csv: Path = HERE / "runs.csv", tickers: Optional[list[str]] = None,
             liquidate_foreign: bool = False, out: Callable[[str], None] = print) -> RunResult:
    """One decision cycle. Never raises for expected conditions; returns what happened."""
    if live and allow_closed:
        raise ValueError("--allow-closed is for dry runs only")
    mode = "live-paper" if live else "dry-run"
    res = RunResult(status="error", mode=mode)
    try:
        _run(broker, agent, res, live=live, run_at=run_at, window_min=window_min, ignore_window=ignore_window,
             allow_closed=allow_closed, feed=feed, force=force, runs_csv=runs_csv,
             tickers=tickers or feed_tickers(), liquidate_foreign=liquidate_foreign, out=out)
    except SkipRun as e:
        res.status, res.reason = "skipped", str(e)
    except AbortRun as e:
        res.status, res.reason = "aborted", str(e)
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001 — report, never trade on an unexpected failure
        res.status, res.reason = "error", f"{type(e).__name__}: {redact(e)}"
    log.info("run %s: %s%s", mode, res.status, f" - {res.reason}" if res.reason else "")
    if res.session is None and res.now_et is not None:
        res.session = res.now_et.date()
    try:
        log_run(runs_csv, res)
    except OSError as e:
        log.error("could not write %s: %s", runs_csv, e)
    return res


def _run(broker, agent, res: RunResult, *, live, run_at, window_min, ignore_window, allow_closed, feed, force,
         runs_csv, tickers, liquidate_foreign, out) -> None:
    clock = broker.get_clock()
    now_et = clock.now.astimezone(NY)
    res.now_et = now_et
    today = now_et.date()
    sessions = broker.get_sessions(today - timedelta(days=12), today)
    started = [s for s in sessions if s.open <= clock.now]
    if not started:
        raise SkipRun("no recent trading session")
    session = started[-1]
    res.session = session.day

    if not clock.is_open and not allow_closed:
        raise SkipRun("market is closed")
    if live and clock.now >= session.close - timedelta(minutes=CLOSE_GUARD_MIN):
        raise SkipRun("too close to the close to submit orders")
    if not (ignore_window or allow_closed):
        lo, hi = _run_window(now_et, run_at, window_min)
        if not (lo <= now_et <= hi):
            raise SkipRun(f"outside the run window {lo:%H:%M}-{hi:%H:%M} ET (use --now to run anyway)")
    if live and not force and already_ran(runs_csv, session.day):
        raise SkipRun(f"already submitted orders for {session.day} (use --force to override)")

    acct = broker.get_account()
    if acct.status != "ACTIVE" or acct.blocked:
        raise SkipRun(f"account unusable (status {acct.status}, blocked={acct.blocked})")
    if acct.cash < 0:
        raise SkipRun(f"cash is negative (${acct.cash:,.0f}): margin is in use")
    if broker.get_open_orders():
        raise SkipRun("open orders already exist; not stacking more")

    positions = broker.get_positions()
    required = agent_tickers(agent)
    shorts = [p.symbol for p in positions if p.qty < 0]
    if shorts:
        raise AbortRun(f"short positions present: {shorts}")
    foreign = sorted(p.symbol for p in positions if p.qty > 0 and p.symbol not in required)
    if foreign and not liquidate_foreign:
        raise AbortRun(f"positions outside this bot's tickers: {foreign} "
                       f"(use a dedicated paper account, or pass --liquidate-foreign to let the bot sell them)")
    if foreign:
        log.warning("--liquidate-foreign: the bot will treat %s as stray positions and sell them", foreign)
    held = {p.symbol: p.qty for p in positions if p.qty > 0}
    avg_cost = {p.symbol: p.avg_entry_price for p in positions}

    start = datetime.now(timezone.utc) - timedelta(days=HISTORY_CALENDAR_DAYS)
    raw = broker.get_bars(tickers, start, feed)
    state = build_market_state(raw, tickers)
    problem = validate_market_state(state, required | set(held), session.day, min_bars(agent))
    if problem:
        raise SkipRun(problem)

    prices = {t: rows[-1]["close"] for t, rows in state.items()}
    cash = acct.cash
    pf = build_portfolio_state(cash, held, avg_cost, state)
    equity = cash + sum(q * prices[t] for t, q in held.items())
    res.cash, res.equity, res.positions_before = cash, equity, dict(held)
    res.gross_before = sum(q * prices[t] * agent._beta(t) for t, q in held.items()) / equity if equity > 0 else 0.0
    try:
        res.tier = agent.regime(state)
        raw_orders = agent.decide(state, pf, cash)
    except Exception as e:  # noqa: BLE001
        raise SkipRun(f"decide() failed: {type(e).__name__}: {redact(e)}")

    orders, dropped = convert_orders(raw_orders, held, set(state))
    for d in dropped:
        log.warning("order dropped: %s", d)
    book = post_trade_book(cash, held, orders, prices, agent._beta)
    res.orders, res.book = orders, book
    res.positions_after = {t: q for t, q in book["qty"].items()}
    res.gross_after, res.max_position = book["gross"], book["max_weight"]
    violation = safety_violation(book)
    if violation:
        raise AbortRun(violation)

    out(describe(res, prices, live))
    if not live:
        res.status = "dry-run"
        res.reason = "no orders" if not orders else "orders not sent (dry run)"
        return
    if not orders:
        res.status, res.reason = "submitted", "no orders needed"
        return
    _submit(broker, res, prices, session.day, out)


def _submit(broker, res: RunResult, prices: dict[str, float], session_day: date, out) -> None:
    sells = [o for o in res.orders if o["side"] == "sell"]
    buys = [o for o in res.orders if o["side"] == "buy"]
    problems: list[str] = []
    placed: list[tuple[dict, str]] = []

    def send(o: dict, n: int) -> Optional[str]:
        cid = f"tl-{session_day:%Y%m%d}-{n}-{o['symbol']}-{o['side']}"
        try:
            oid = broker.submit_market_order(o["symbol"], o["side"], o["qty"], cid)
        except Exception as e:  # noqa: BLE001
            problems.append(f"{o['side']} {o['symbol']} rejected: {redact(e)}")
            return None
        placed.append((o, oid))
        out(f"  sent {o['side']:4s} {o['symbol']:5s} x{o['qty']}  (order {oid[:8]})")
        return oid

    sell_ids = [send(o, i) for i, o in enumerate(sells)]
    sells_ok = all(oid is not None for oid in sell_ids)
    for oid in sell_ids:
        if oid is not None and not broker.wait_for_fill(oid)["filled"]:
            sells_ok = False
            problems.append(f"sell order {oid[:8]} not filled in time")
    if not sells_ok:
        problems.append("buys skipped because the sells did not all fill")
    else:
        budget = broker.get_account().cash        # cash after the sells; never spend more than this (no margin)
        buy_ids = []
        for i, o in enumerate(buys):
            px = prices[o["symbol"]]
            qty = min(o["qty"], math.floor(max(budget, 0.0) * (1.0 - CASH_TOLERANCE) / px))
            if qty < 1:
                problems.append(f"buy {o['symbol']} skipped: cash left ${budget:,.0f} covers no whole share")
                continue
            sent = {"symbol": o["symbol"], "side": "buy", "qty": qty}
            oid = send(sent, len(sells) + i)
            if oid is not None:
                budget -= qty * px
                buy_ids.append(oid)
        for oid in buy_ids:
            if not broker.wait_for_fill(oid)["filled"]:
                problems.append(f"buy order {oid[:8]} not filled in time")
    res.orders = [o for o, _ in placed]
    try:
        after = {p.symbol: p.qty for p in broker.get_positions() if p.qty > 0}
        res.positions_after = after
        acct = broker.get_account()
        res.cash, res.equity = acct.cash, acct.equity
    except Exception as e:  # noqa: BLE001
        problems.append(f"could not re-read the account: {redact(e)}")
    res.status = "partial" if problems else "submitted"
    res.reason = "; ".join(problems)


def describe(res: RunResult, prices: dict[str, float], live: bool) -> str:
    b = res.book
    lines = [f"{'LIVE-PAPER' if live else 'DRY RUN (no orders will be sent)'}  session {res.session}  tier {res.tier}  "
             f"equity ${res.equity:,.0f}  cash ${res.cash:,.0f}"]
    lines.append("  holding now : " + (fmt_positions(res.positions_before) or "nothing"))
    if res.orders:
        for o in res.orders:
            lines.append(f"  {o['side']:4s} {o['symbol']:5s} x{o['qty']:<6d} @ ~${prices[o['symbol']]:,.2f}  "
                         f"~${o['qty'] * prices[o['symbol']]:,.0f}")
    else:
        lines.append("  no orders")
    if b:
        lines.append("  after trades: " + ", ".join(f"{t} {w:.1%}" for t, w in sorted(b["weights"].items(), key=lambda kv: -kv[1]))
                     + f", cash {b['cash'] / b['equity']:.1%}")
        lines.append(f"  beta gross {res.gross_before:.2f}x -> {b['gross']:.2f}x (abort above {GROSS_LIMIT}x), "
                     f"largest position {b['max_weight']:.1%} (abort above {POSITION_LIMIT:.0%})")
    return "\n".join(lines)


# ------------------------------------------------------------------ CLI
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Alpaca PAPER runner for agent.py (dry run by default)")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="print the orders, send nothing (default)")
    mode.add_argument("--live-paper", action="store_true", help="submit orders to the Alpaca PAPER account")
    p.add_argument("--run-at", default=RUN_AT, help="ET time of the daily run, HH:MM (default %(default)s)")
    p.add_argument("--window-min", type=int, default=WINDOW_MIN,
                   help="minutes after --run-at in which a run is allowed (default %(default)s)")
    p.add_argument("--now", action="store_true", help="ignore the run-window guard (the market must still be open)")
    p.add_argument("--allow-closed", action="store_true", help="dry run only: run while the market is closed")
    p.add_argument("--feed", choices=("iex", "sip"), default="iex", help="Alpaca data feed (default %(default)s)")
    p.add_argument("--force", action="store_true", help="live-paper: ignore the once-per-day guard")
    p.add_argument("--liquidate-foreign", action="store_true",
                   help="let the bot sell positions it doesn't hold by design (e.g. a previous strategy's stocks); "
                        "they must be in the live 42-ticker feed")
    p.add_argument("--runs-csv", type=Path, default=HERE / "runs.csv")
    p.add_argument("--log-dir", type=Path, default=HERE / "logs")
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def main(argv: Optional[list[str]] = None, broker: Any = None, agent: Any = None) -> int:
    args = build_parser().parse_args(argv)
    if args.live_paper and args.allow_closed:
        build_parser().error("--allow-closed is only valid for dry runs")
    setup_logging(args.log_dir, args.verbose)
    try:
        broker = broker or make_alpaca_broker()
    except SystemExit as e:
        log.error("%s", e)
        return 2
    res = run_once(broker, agent or load_agent(), live=args.live_paper, run_at=args.run_at,
                   window_min=args.window_min, ignore_window=args.now, allow_closed=args.allow_closed,
                   feed=args.feed, force=args.force, runs_csv=args.runs_csv,
                   liquidate_foreign=args.liquidate_foreign)
    return {"aborted": 2, "error": 1}.get(res.status, 0)


if __name__ == "__main__":
    sys.exit(main())
