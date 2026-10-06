"""Walk-forward backtester that mirrors the live leaderboard engine (live_runner.py).

    python fetch_history.py               # once: pulls bars into data_cache/bars.json
    python backtest.py                    # evaluates agent.py
    python backtest.py my_variant.py      # evaluates another file

Engine rules reproduced here (see live_runner.run_bot):
  * decide() sees only bars up to the prior close; orders fill at the next
    session's OPEN with 5 bps slippage (10 bps on 2x/3x ETFs).
  * At most 100 orders per decision are read; sells execute before buys.
  * Each buy is capped by cash, by 30% single-name room and by 1.5x beta-gross
    room, all measured at the opening fill (equity at the open).
  * Only the 42 tickers the live board fetches (fetch_history.LIVE_FEED) are
    visible and fillable; anything else is silently dropped, as live.
  * market_state carries up to 309 bars per ticker, as the live board's
    310-bar fetch does (admission's hidden regimes use ~220; pass history=220).
  * Prices are split- and dividend-adjusted, like yfinance auto_adjust=True.

Windows: 60 trading days, a new one every 20 days. Split into TRAIN (window
start before 2023-01-01) and TEST (after), so a change only counts if it helps
on years it was not tuned on.
"""
from __future__ import annotations

import importlib.util
import json
import math
import sys
from collections.abc import Mapping
from pathlib import Path

from fetch_history import LIVE_FEED

HERE = Path(__file__).parent
DATA = HERE / "data_cache" / "bars.json"
BETA_3X = {"TQQQ", "SOXL", "UPRO", "SPXL", "TNA", "FAS", "TECL", "LABU", "CURE", "DRN", "UDOW", "NAIL"}
BETA_2X = {"QLD", "SSO", "DDM", "ROM", "UWM", "AGQ"}
START_CASH = 100_000.0
HISTORY = 309                 # bars visible to decide() on the live board
ADMISSION_HISTORY = 220       # README: admission regimes carry ~220 bars
MAX_ORDERS_PER_DECISION = 100
MAX_NAME_WEIGHT = 0.30
MAX_BETA_GROSS = 1.50
WINDOW = 60
STEP = 20
SPLIT = "2023-01-01"
ROUND2_START = "2026-07-07"


def beta(t: str) -> float:
    return 3.0 if t in BETA_3X else 2.0 if t in BETA_2X else 1.0


class Market:
    """Daily bars indexed by date. `feed` limits what the agent can see and trade."""

    def __init__(self, raw: dict[str, list[list]], feed=LIVE_FEED):
        self.cal = [r[0] for r in raw["SPY"]]
        self.bars: dict[str, list[dict]] = {}
        self.idx: dict[str, dict[str, int]] = {}
        for t, rows in raw.items():
            self.bars[t] = [
                {"ts": r[0], "open": r[1], "high": r[2], "low": r[3], "close": r[4], "volume": r[5]}
                for r in rows
            ]
            self.idx[t] = {r[0]: i for i, r in enumerate(rows)}
        self.feed = [t for t in (feed if feed is not None else raw) if t in raw]

    def px(self, t: str, date: str, field: str):
        i = self.idx.get(t, {}).get(date)
        return None if i is None else self.bars[t][i][field]


class LazyState(Mapping):
    """market_state view: each feed ticker's last `history` bars up to `date`, sliced on demand."""

    def __init__(self, mkt: Market, date: str, history: int = HISTORY):
        self.m, self.d, self.h, self.cache = mkt, date, history, {}
        self.allowed = set(mkt.feed)

    def _slice(self, t):
        if t not in self.cache:
            i = self.m.idx.get(t, {}).get(self.d) if t in self.allowed else None
            self.cache[t] = None if i is None else self.m.bars[t][max(0, i - self.h + 1): i + 1]
        return self.cache[t]

    def __getitem__(self, t):
        v = self._slice(t)
        if v is None:
            raise KeyError(t)
        return v

    def __iter__(self):
        return (t for t in self.m.feed if self._slice(t) is not None)

    def __len__(self):
        return sum(1 for _ in self)


def load(path: Path):
    spec = importlib.util.spec_from_file_location("agent_bt", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.decide


def simulate(agent_path: Path, mkt: Market, dates: list[str], cost_mult: float = 1.0, probe=None,
             history: int = HISTORY, start_book: dict | None = None, record: bool = False) -> dict:
    """Run one fresh agent over `dates` with the live engine's fill rules.

    Orders decided on day t's close fill at day t+1's open. cost_mult scales slippage.
    `probe(state)` is recorded each day (e.g. the agent's regime). `start_book`
    ({"cash": x, "positions": {ticker: qty}}) starts from an inherited book instead of
    cash. `record=True` also returns every fill and the end-of-day holdings.
    """
    decide = load(agent_path)
    cash = float(start_book["cash"]) if start_book else START_CASH
    pos = {t: float(q) for t, q in (start_book or {}).get("positions", {}).items() if q > 0}
    avg_cost: dict[str, float] = {}
    start_equity = cash + sum(q * (mkt.px(t, dates[0], "open") or 0.0) for t, q in pos.items())
    pending: list[dict] = []
    curve, gross_path, gross_open_path, probes, fills, books = [], [], [], [], [], []
    trades, traded, costs, errors, capped = 0, 0.0, 0.0, 0, 0
    peak_gross, peak_gross_open, peak_conc, streak, max_streak = 0.0, 0.0, 0.0, {}, 0
    feed = set(mkt.feed)
    for di, date in enumerate(dates):
        open_px = {t: p for t in feed if (p := mkt.px(t, date, "open")) is not None}
        prev_close = {t: mkt.px(t, dates[di - 1], "close") for t in feed} if di else {}
        normalized = []
        for o in pending[:MAX_ORDERS_PER_DECISION]:
            try:
                tk, side, qty = str(o["ticker"]).strip().upper(), o["side"], float(o["quantity"])
            except (KeyError, TypeError, ValueError):
                continue
            if side in ("buy", "sell") and math.isfinite(qty) and qty > 0 and tk in open_px:
                normalized.append((tk, side, qty))
        for tk, side, qty in sorted(normalized, key=lambda x: 0 if x[1] == "sell" else 1):
            px = open_px[tk]
            slip = (0.001 if beta(tk) > 1 else 0.0005) * cost_mult
            want = qty
            if side == "buy":
                fill = px * (1 + slip)
                eq_open = max(cash + sum(q * open_px.get(t, 0.0) for t, q in pos.items()), 1e-9)
                held = pos.get(tk, 0.0)
                conc_room = max(0.0, MAX_NAME_WEIGHT * eq_open - held * fill)
                beta_used = sum(q * open_px.get(t, 0.0) * beta(t) for t, q in pos.items())
                beta_room = max(0.0, MAX_BETA_GROSS * eq_open - beta_used)
                qty = min(qty, min(cash, conc_room, beta_room / beta(tk)) / fill if fill > 0 else 0.0)
                if qty < want - 1e-9 and min(conc_room, beta_room / beta(tk)) < cash:
                    capped += 1
                if qty <= 0:
                    continue
                avg_cost[tk] = (avg_cost.get(tk, 0.0) * held + fill * qty) / (held + qty)
                pos[tk] = held + qty
                cash -= fill * qty
            else:
                qty = min(qty, pos.get(tk, 0.0))
                if qty <= 0:
                    continue
                pos[tk] -= qty
                cash += qty * px * (1 - slip)
            trades += 1
            traded += qty * px
            costs += qty * px * slip
            if record:
                fills.append({"date": date, "ticker": tk, "side": side, "qty": qty, "open": px,
                              "prev_close": prev_close.get(tk), "slip": qty * px * slip})
        pending = []
        if normalized:
            eq_o = max(cash + sum(q * open_px.get(t, 0.0) for t, q in pos.items()), 1e-9)
            g_o = sum(q * open_px.get(t, 0.0) * beta(t) for t, q in pos.items()) / eq_o
            peak_gross_open = max(peak_gross_open, g_o)
            gross_open_path.append(g_o)
        prices = {t: p for t in pos if pos[t] > 0 and (p := mkt.px(t, date, "close")) is not None}
        value = {t: pos[t] * prices.get(t, 0.0) for t in pos if pos[t] > 0}
        equity = max(cash + sum(value.values()), 1e-9)
        curve.append(equity)
        gross = sum(v * beta(t) for t, v in value.items()) / equity
        gross_path.append(gross)
        peak_gross = max(peak_gross, gross)
        for t in set(streak) | set(value):
            w = value.get(t, 0.0) / equity
            peak_conc = max(peak_conc, w)
            streak[t] = streak.get(t, 0) + 1 if w >= MAX_NAME_WEIGHT else 0
            max_streak = max(max_streak, streak[t])
        if record:
            books.append({"date": date, "cash": cash, "positions": {t: q for t, q in pos.items() if q > 0},
                          "equity": equity})
        state = LazyState(mkt, date, history)
        if probe is not None:
            probes.append(probe(state))
        last = {t: state[t][-1]["close"] for t in state}
        pf = {
            "cash": cash,
            "positions": [{"ticker": t, "quantity": q, "avg_cost": avg_cost.get(t, 0.0)}
                          for t, q in pos.items() if q > 0],
            "last_prices": last,
        }
        try:
            orders = decide(state, pf, cash) or []
        except Exception:  # noqa: BLE001
            errors += 1
            orders = []
        pending = orders if isinstance(orders, list) else []
    peak, mdd = start_equity, 0.0
    for v in curve:
        peak = max(peak, v)
        mdd = max(mdd, 1 - v / peak)
    ret = curve[-1] / start_equity - 1
    avg_equity = sum(curve) / len(curve)
    out = {"ret": ret, "mdd": mdd, "trades": trades, "curve": curve, "gross": peak_gross,
           "gross_open": peak_gross_open, "conc": peak_conc, "conc_streak": max_streak, "errors": errors,
           "avg_gross": sum(gross_path) / len(gross_path), "gross_path": gross_path, "probes": probes,
           "turnover": traded / avg_equity, "costs": costs, "capped_buys": capped,
           "sharpe": sharpe(curve), "calmar": (annualize(ret, len(curve)) / mdd) if mdd > 1e-9 else 0.0,
           "start_equity": start_equity}
    if record:
        out.update(fills=fills, books=books)
    return out


def sharpe(curve: list[float]) -> float:
    rets = [curve[i] / curve[i - 1] - 1 for i in range(1, len(curve))]
    if len(rets) < 2:
        return 0.0
    mean = sum(rets) / len(rets)
    sd = math.sqrt(sum((r - mean) ** 2 for r in rets) / (len(rets) - 1))
    return mean / sd * math.sqrt(252) if sd > 1e-12 else 0.0


def annualize(ret: float, days: int) -> float:
    return (1 + ret) ** (252 / days) - 1 if days > 0 else 0.0


def bh(mkt: Market, t: str, dates: list[str]) -> float:
    a, b = mkt.px(t, dates[0], "open"), mkt.px(t, dates[-1], "close")
    return b / a - 1 if a and b else 0.0


def summarize(rows: list[dict]) -> dict:
    rets = sorted(r["ret"] for r in rows)
    n = len(rets)
    return {
        "n": n,
        "mean": sum(rets) / n,
        "median": rets[n // 2],
        "p10": rets[max(0, n // 10)],
        "mdd": sum(r["mdd"] for r in rows) / n,
        "worst_dd": max(r["mdd"] for r in rows),
        "beat_qqq": sum(r["ret"] > r["qqq"] for r in rows) / n,
        "trades": sum(r["trades"] for r in rows) / n,
        "score": sum(rets) / n - 0.5 * sum(r["mdd"] for r in rows) / n,
    }


def evaluate(agent_path: Path, mkt: Market | None = None, quiet: bool = False) -> dict:
    mkt = mkt or Market(json.load(open(DATA)))
    cal = mkt.cal
    rows = []
    for s in range(HISTORY, len(cal) - WINDOW, STEP):
        dates = cal[s: s + WINDOW]
        r = simulate(agent_path, mkt, dates)
        r.update(start=dates[0], qqq=bh(mkt, "QQQ", dates))
        rows.append(r)
    train = summarize([r for r in rows if r["start"] < SPLIT])
    test = summarize([r for r in rows if r["start"] >= SPLIT])
    r2_dates = [d for d in cal if d >= ROUND2_START]
    r2 = simulate(agent_path, mkt, r2_dates)
    full = simulate(agent_path, mkt, cal[HISTORY:])
    years = len(cal[HISTORY:]) / 252
    out = {"train": train, "test": test,
           "round2": {"ret": r2["ret"], "mdd": r2["mdd"], "trades": r2["trades"], "qqq": bh(mkt, "QQQ", r2_dates),
                      "from": r2_dates[0], "to": r2_dates[-1]},
           "full": {"cagr": (1 + full["ret"]) ** (1 / years) - 1, "mdd": full["mdd"], "trades": full["trades"],
                    "gross": full["gross"], "conc": full["conc"], "errors": full["errors"]},
           "windows": [{k: r[k] for k in ("start", "ret", "mdd", "qqq", "trades")} for r in rows]}
    if not quiet:
        report(agent_path.name, out)
    return out


# Fixed stress/character windows (evaluation only; the agent never sees these dates).
SCENARIOS = [
    ("choppy sideways 2018", "2018-01-29", "2018-09-28"),
    ("crash Q4 2018", "2018-10-01", "2018-12-31"),
    ("vol spike + snapback 2020", "2020-02-19", "2020-06-30"),
    ("bear market 2022", "2022-01-03", "2022-12-30"),
    ("strong bull 2023", "2023-01-03", "2023-07-31"),
    ("crash + snapback 2025", "2025-02-19", "2025-06-30"),
]


def scenarios(agent_path: Path, mkt: Market, cost_mult: float = 1.0) -> list[dict]:
    out = []
    windows = [(n, [d for d in mkt.cal if a <= d <= b]) for n, a, b in SCENARIOS]
    windows.append(("most recent 60 days", mkt.cal[-WINDOW:]))
    for name, dates in windows:
        r = simulate(agent_path, mkt, dates, cost_mult)
        r.update(name=name, qqq=bh(mkt, "QQQ", dates), days=len(dates))
        out.append(r)
    return out


def scenario_table(label: str, rows: list[dict]) -> None:
    print(f"=== {label} ===")
    print(f"  {'window':27s} {'ret':>7s} {'maxDD':>6s} {'Sharpe':>6s} {'Calmar':>7s} {'turn':>5s} {'trades':>6s} "
          f"{'avgGross':>8s} {'pkGross':>7s} {'pkConc':>6s} {'QQQ':>7s}")
    for r in rows:
        print(f"  {r['name']:27s} {r['ret']*100:6.2f}% {r['mdd']*100:5.1f}% {r['sharpe']:6.2f} {r['calmar']:7.2f} "
              f"{r['turnover']:5.1f} {r['trades']:6d} {r['avg_gross']:7.2f}x {r['gross']:6.2f}x {r['conc']*100:5.0f}% "
              f"{r['qqq']*100:6.2f}%")
    rets = sorted(r["ret"] for r in rows)
    worst = min(rows, key=lambda r: r["ret"])
    print(f"  {'median window':27s} {rets[len(rets) // 2]*100:6.2f}%   |  worst: {worst['name']} "
          f"{worst['ret']*100:.2f}% (DD {max(r['mdd'] for r in rows)*100:.1f}% worst DD)")


def report(name: str, o: dict) -> None:
    print(f"=== {name} ===")
    for k in ("train", "test"):
        s = o[k]
        print(f"  {k:5s} {s['n']:3d} windows | mean {s['mean']*100:6.2f}%  median {s['median']*100:6.2f}%  "
              f"p10 {s['p10']*100:6.2f}% | avg DD {s['mdd']*100:5.2f}%  worst DD {s['worst_dd']*100:5.2f}% | "
              f"beat QQQ {s['beat_qqq']*100:4.0f}% | trades/window {s['trades']:5.1f} | score {s['score']*100:6.2f}")
    f, r2 = o["full"], o["round2"]
    print(f"  full  CAGR {f['cagr']*100:6.2f}%  maxDD {f['mdd']*100:5.2f}%  trades {f['trades']}  "
          f"peak gross {f['gross']:.2f}x  peak conc {f['conc']*100:.0f}%  errors {f['errors']}")
    print(f"  round2 {r2['from']}..{r2['to']}: ret {r2['ret']*100:6.2f}%  DD {r2['mdd']*100:5.2f}%  "
          f"trades {r2['trades']}  (QQQ {r2['qqq']*100:6.2f}%)")


if __name__ == "__main__":
    m = Market(json.load(open(DATA)))
    for p in sys.argv[1:] or ["agent.py"]:
        path = HERE / p if not Path(p).is_absolute() else Path(p)
        evaluate(path, m)
        scenario_table(f"{path.name} — stress windows (5/10 bps slippage)", scenarios(path, m))
