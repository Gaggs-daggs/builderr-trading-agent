"""Tests for alpaca_runner.py with a mocked Alpaca client (no network, no keys).

    python test_alpaca_runner.py            # stdlib only; adapter tests also run if alpaca-py is installed
    .venv/bin/python test_alpaca_runner.py  # includes the alpaca-py adapter tests

The pipeline tests drive the real agent.py through a FakeBroker that simulates fills.
"""
from __future__ import annotations

import contextlib
import csv
import io
import math
import os
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import alpaca_runner as ar

AGENT = ar.load_agent()
NY = ar.NY
FEED = ar.feed_tickers()
END = date(2026, 10, 5)                       # a Monday
KEY = "PK" + "TESTKEY" + "9" * 8              # fake credentials, built so no scanner mistakes them for real ones
SECRET = "sEcReT" + "x" * 12


def bdays(n: int, end: date) -> list[date]:
    out, d = [], end
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d -= timedelta(days=1)
    return out[::-1]


def make_bars(end: date = END, n: int = 330, drift: float = 0.0015) -> dict[str, list[dict]]:
    bars = {}
    for i, t in enumerate(FEED):
        px = 50.0 + 7 * i
        rows = []
        for d in bdays(n, end):
            px *= 1 + (drift if t in ("QQQ", "SPY") else 0.001)
            rows.append({"ts": d.isoformat(), "open": px, "high": px * 1.01, "low": px * 0.99, "close": px, "volume": 1000})
        bars[t] = rows
    return bars


def at(d: date, hh: int, mm: int) -> datetime:
    return datetime(d.year, d.month, d.day, hh, mm, tzinfo=NY)


class FakeBroker:
    """Mocked Alpaca client: serves bars, tracks cash/positions, fills orders at the last close."""

    def __init__(self, bars=None, now=None, is_open=True, cash=100_000.0, positions=(), status="ACTIVE",
                 open_orders=(), unfilled=(), cash_after_sells=None):
        self.bars = bars if bars is not None else make_bars()
        self.now = now or at(END, 15, 47)
        self.is_open = is_open
        self.cash = cash
        self.pos = {p.symbol: p for p in positions}
        self.status = status
        self.open_orders = list(open_orders)
        self.unfilled = set(unfilled)
        self.cash_after_sells = cash_after_sells
        self.submitted: list[tuple[str, str, int, str]] = []
        self._sold = False

    def get_clock(self):
        return ar.Clock(now=self.now, is_open=self.is_open)

    def get_sessions(self, start, end):
        return [ar.Session(day=d, open=at(d, 9, 30), close=at(d, 16, 0)) for d in bdays(20, END) if start <= d <= end]

    def get_account(self):
        cash = self.cash_after_sells if (self.cash_after_sells is not None and self._sold) else self.cash
        return ar.Account(cash=cash, equity=cash + sum(p.qty * (self._px(s) if s in self.bars else 100.0) for s, p in self.pos.items()), status=self.status)

    def get_positions(self):
        return [p for p in self.pos.values() if p.qty != 0]

    def get_open_orders(self):
        return self.open_orders

    def get_bars(self, tickers, start, feed):
        return {t: list(rows) for t, rows in self.bars.items() if t in tickers}

    def _px(self, sym):
        return self.bars[sym][-1]["close"]

    def submit_market_order(self, symbol, side, qty, client_order_id):
        self.submitted.append((symbol, side, qty, client_order_id))
        px = self._px(symbol)
        if symbol in self.unfilled:
            return f"order-{len(self.submitted):04d}-unfilled"
        p = self.pos.setdefault(symbol, ar.Position(symbol, 0.0))
        if side == "sell":
            p.qty -= qty
            self.cash += qty * px
            self._sold = True
        else:
            p.qty += qty
            self.cash -= qty * px
        return f"order-{len(self.submitted):04d}"

    def wait_for_fill(self, order_id, timeout_s=0):
        return {"status": "new" if order_id.endswith("unfilled") else "filled", "filled": not order_id.endswith("unfilled")}


def run(broker=None, **kw):
    tmp = tempfile.mkdtemp()
    kw.setdefault("runs_csv", Path(tmp) / "runs.csv")
    kw.setdefault("tickers", FEED)
    buf = io.StringIO()
    broker = broker or FakeBroker()
    res = ar.run_once(broker, kw.pop("agent", AGENT), out=lambda s: buf.write(s + "\n"), **kw)
    return res, broker, buf.getvalue(), kw["runs_csv"]


def rows(path: Path) -> list[dict]:
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


class TestInputs(unittest.TestCase):
    def test_feed_is_the_live_boards_42_tickers(self):
        self.assertEqual(len(FEED), 42)
        self.assertTrue({"QQQ", "SPY", "TQQQ", "QLD", "SMH"} <= set(FEED))
        self.assertNotIn("GLD", FEED)

    def test_market_state_matches_live_runner_format(self):
        state = ar.build_market_state(make_bars(n=400), FEED)
        self.assertEqual(set(state), set(FEED))
        for rows_ in state.values():
            self.assertEqual(len(rows_), ar.HISTORY)
            self.assertEqual(set(rows_[-1]), {"ts", "open", "high", "low", "close", "volume"})
            self.assertIsInstance(rows_[-1]["volume"], int)
        self.assertEqual(ar.build_market_state(make_bars(n=100), FEED)["QQQ"].__len__(), 100)

    def test_portfolio_state_shape(self):
        state = ar.build_market_state(make_bars(), FEED)
        pf = ar.build_portfolio_state(5000.0, {"QQQ": 10.0}, {"QQQ": 400.0}, state)
        self.assertEqual(pf["cash"], 5000.0)
        self.assertEqual(pf["positions"], [{"ticker": "QQQ", "quantity": 10.0, "avg_cost": 400.0}])
        self.assertEqual(pf["last_prices"]["QQQ"], state["QQQ"][-1]["close"])
        self.assertEqual(set(pf["last_prices"]), set(state))


class TestOrderConversion(unittest.TestCase):
    ALLOWED = {"QQQ", "SPY", "TQQQ", "QLD", "SMH"}

    def test_whole_shares_only(self):
        orders, dropped = ar.convert_orders(
            [{"ticker": "QQQ", "side": "buy", "quantity": 12.9}, {"ticker": "SPY", "side": "buy", "quantity": 0.6}],
            {}, self.ALLOWED)
        self.assertEqual(orders, [{"symbol": "QQQ", "side": "buy", "qty": 12}])
        self.assertTrue(any("under one whole share" in d for d in dropped))

    def test_sell_is_clamped_to_whole_held_shares(self):
        orders, _ = ar.convert_orders([{"ticker": "QQQ", "side": "sell", "quantity": 50.0}], {"QQQ": 10.7}, self.ALLOWED)
        self.assertEqual(orders, [{"symbol": "QQQ", "side": "sell", "qty": 10}])
        orders, _ = ar.convert_orders([{"ticker": "QQQ", "side": "sell", "quantity": 5.0}], {}, self.ALLOWED)
        self.assertEqual(orders, [])

    def test_bad_orders_are_dropped(self):
        raw = [{"ticker": "AAPL", "side": "buy", "quantity": 5}, {"ticker": "QQQ", "side": "short", "quantity": 5},
               {"ticker": "QQQ", "side": "buy", "quantity": -3}, {"ticker": "QQQ", "side": "buy", "quantity": float("nan")},
               {"side": "buy"}, "junk", None, {"ticker": "qqq ", "side": "buy", "quantity": 4}]
        orders, dropped = ar.convert_orders(raw, {}, self.ALLOWED)
        self.assertEqual(orders, [{"symbol": "QQQ", "side": "buy", "qty": 4}])
        self.assertEqual(len(dropped), 7)
        self.assertEqual(ar.convert_orders("not a list", {}, self.ALLOWED)[0], [])

    def test_sells_come_before_buys_and_cap_drops_buys_first(self):
        allowed = {f"T{i}" for i in range(60)}
        raw = [{"ticker": f"T{i}", "side": "buy" if i % 2 else "sell", "quantity": 5} for i in range(60)]
        held = {f"T{i}": 10.0 for i in range(60)}
        orders, dropped = ar.convert_orders(raw, held, allowed, max_orders=45)
        self.assertEqual(len(orders), 45)
        sides = [o["side"] for o in orders]
        self.assertEqual(sides, sorted(sides, key=lambda s: 0 if s == "sell" else 1))
        self.assertEqual(sides.count("sell"), 30)
        self.assertTrue(any("over the 45-order cap" in d for d in dropped))
        self.assertEqual(ar.MAX_ORDERS, 45)


class TestSafetyLayer(unittest.TestCase):
    PRICES = {t: 100.0 for t in ("QQQ", "TQQQ", "UPRO", "SPY", "XLK", "XLP", "XLU")}
    FOUR = {"SPY": 250.0, "XLK": 250.0, "XLP": 250.0, "XLU": 250.0}      # $100k in four 25% positions

    def book(self, cash, held, orders):
        return ar.post_trade_book(cash, held, orders, self.PRICES, AGENT._beta)

    def test_clean_book_passes(self):
        b = self.book(100_000.0, {}, [{"symbol": "QQQ", "side": "buy", "qty": 250}, {"symbol": "TQQQ", "side": "buy", "qty": 150}])
        self.assertIsNone(ar.safety_violation(b))
        self.assertAlmostEqual(b["gross"], 0.25 + 3 * 0.15)

    def test_gross_over_1_45_aborts(self):
        b = self.book(100_000.0, {}, [{"symbol": "TQQQ", "side": "buy", "qty": 270}, {"symbol": "UPRO", "side": "buy", "qty": 270}])
        self.assertIn("gross", ar.safety_violation(b))

    def test_position_over_28pct_aborts(self):
        b = self.book(100_000.0, {}, [{"symbol": "QQQ", "side": "buy", "qty": 290}])
        self.assertIn("28%", ar.safety_violation(b))

    def test_margin_use_aborts(self):
        b = self.book(1_000.0, dict(self.FOUR), [{"symbol": "QQQ", "side": "buy", "qty": 100}])
        self.assertIn("margin", ar.safety_violation(b))
        b = self.book(1_000.0, dict(self.FOUR), [{"symbol": "QQQ", "side": "buy", "qty": 1}])
        self.assertIsNone(ar.safety_violation(b))

    def test_sells_fund_buys(self):
        b = self.book(0.0, dict(self.FOUR), [{"symbol": "SPY", "side": "sell", "qty": 250}, {"symbol": "QQQ", "side": "buy", "qty": 250}])
        self.assertIsNone(ar.safety_violation(b))
        self.assertEqual(b["qty"], {"XLK": 250.0, "XLP": 250.0, "XLU": 250.0, "QQQ": 250.0})


class TestRunPipeline(unittest.TestCase):
    def test_dry_run_prints_orders_and_sends_nothing(self):
        res, broker, out, csv_path = run(live=False)
        self.assertEqual(res.status, "dry-run")
        self.assertEqual(res.tier, "ON")
        self.assertEqual(broker.submitted, [])
        self.assertIn("DRY RUN", out)
        for t in ("TQQQ", "QLD", "QQQ", "SMH"):
            self.assertIn(t, out)
        r = rows(csv_path)
        self.assertEqual(len(r), 1)
        self.assertEqual((r[0]["mode"], r[0]["status"], r[0]["tier"], r[0]["date"]), ("dry-run", "dry-run", "ON", "2026-10-05"))
        self.assertLessEqual(float(r[0]["gross_after"]), 1.45)
        self.assertIn("buy TQQQ", r[0]["orders"])

    def test_live_paper_submits_sells_before_buys(self):
        held = [ar.Position("SPY", 100.0), ar.Position("XLK", 200.0)]       # a MID book while the regime is ON
        res, broker, _, csv_path = run(FakeBroker(cash=60_000.0, positions=held), live=True)
        self.assertEqual(res.status, "submitted", res.reason)
        sides = [s for _, s, _, _ in broker.submitted]
        self.assertEqual(sides[:2], ["sell", "sell"])
        self.assertTrue(all(s == "buy" for s in sides[2:]) and len(sides) > 2)
        self.assertEqual({s for s, sd, *_ in broker.submitted if sd == "sell"}, {"SPY", "XLK"})
        self.assertTrue(all(isinstance(q, int) and q >= 1 for _, _, q, _ in broker.submitted))
        self.assertLessEqual(len(broker.submitted), ar.MAX_ORDERS)
        self.assertEqual(len({c for *_, c in broker.submitted}), len(broker.submitted), "client order ids must be unique")
        final = {p.symbol: p.qty for p in broker.get_positions()}
        self.assertTrue({"TQQQ", "QLD", "QQQ", "SMH"} <= set(final) and "SPY" not in final and "XLK" not in final)
        self.assertEqual(rows(csv_path)[0]["status"], "submitted")

    def test_buys_are_skipped_when_a_sell_does_not_fill(self):
        held = [ar.Position("SPY", 100.0)]
        res, broker, _, _ = run(FakeBroker(cash=80_000.0, positions=held, unfilled={"SPY"}), live=True)
        self.assertEqual(res.status, "partial")
        self.assertEqual([s for _, s, _, _ in broker.submitted], ["sell"])
        self.assertIn("buys skipped", res.reason)

    def test_buys_never_exceed_cash_left_after_the_sells(self):
        held = [ar.Position("SPY", 100.0)]
        res, broker, _, _ = run(FakeBroker(cash=60_000.0, positions=held, cash_after_sells=10_000.0), live=True)
        spent = sum(q * broker._px(s) for s, sd, q, _ in broker.submitted if sd == "buy")
        self.assertLessEqual(spent, 10_000.0)
        self.assertEqual(res.status, "partial")

    def test_gross_cap_abort_sends_nothing(self):
        bad = [{"ticker": "TQQQ", "side": "buy", "quantity": 300}, {"ticker": "UPRO", "side": "buy", "quantity": 300}]
        with mock.patch.object(AGENT, "decide", return_value=bad):
            res, broker, _, csv_path = run(live=True)
        self.assertEqual(res.status, "aborted")
        self.assertIn("gross", res.reason)
        self.assertEqual(broker.submitted, [])
        self.assertEqual(rows(csv_path)[0]["status"], "aborted")

    def test_position_cap_abort_sends_nothing(self):
        px = FakeBroker()._px("QQQ")
        bad = [{"ticker": "QQQ", "side": "buy", "quantity": math.floor(0.31 * 100_000 / px)}]
        with mock.patch.object(AGENT, "decide", return_value=bad):
            res, broker, _, _ = run(live=True)
        self.assertEqual(res.status, "aborted")
        self.assertIn("28%", res.reason)
        self.assertEqual(broker.submitted, [])

    def test_stale_data_is_skipped(self):
        bars = make_bars()
        bars["QQQ"] = bars["QQQ"][:-1]                               # last QQQ bar is Friday, not Monday
        res, broker, _, _ = run(FakeBroker(bars=bars), live=True)
        self.assertEqual(res.status, "skipped")
        self.assertIn("stale data: QQQ", res.reason)
        self.assertEqual(broker.submitted, [])

    def test_missing_ticker_is_skipped(self):
        bars = make_bars()
        del bars["SMH"]
        res, broker, _, _ = run(FakeBroker(bars=bars), live=True)
        self.assertEqual((res.status, broker.submitted), ("skipped", []))
        self.assertIn("missing data: no bars for SMH", res.reason)

    def test_short_history_and_bad_prices_are_skipped(self):
        res, *_ = run(FakeBroker(bars=make_bars(n=150)), live=False)
        self.assertIn("short history", res.reason)
        bars = make_bars()
        bars["TQQQ"][-1]["close"] = bars["TQQQ"][-2]["close"] * 1.6
        res, *_ = run(FakeBroker(bars=bars), live=False)
        self.assertIn("suspicious data: TQQQ", res.reason)
        bars = make_bars()
        bars["QQQ"][-1]["close"] = float("nan")
        res, *_ = run(FakeBroker(bars=bars), live=False)
        self.assertEqual(res.status, "skipped")

    def test_market_closed_is_skipped_and_allow_closed_is_dry_run_only(self):
        sat = FakeBroker(bars=make_bars(end=date(2026, 10, 2)), now=at(date(2026, 10, 3), 10, 0), is_open=False)
        res, *_ = run(sat, live=False)
        self.assertEqual((res.status, res.reason), ("skipped", "market is closed"))
        res, broker, out, _ = run(sat, live=False, allow_closed=True)
        self.assertEqual(res.status, "dry-run", res.reason)
        self.assertEqual(res.session, date(2026, 10, 2))
        with self.assertRaises(ValueError):
            run(sat, live=True, allow_closed=True)
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            ar.main(["--live-paper", "--allow-closed"], broker=sat)

    def test_run_window_guard(self):
        early = FakeBroker(now=at(END, 14, 0))
        res, *_ = run(early, live=False)
        self.assertEqual(res.status, "skipped")
        self.assertIn("outside the run window 15:45-15:55", res.reason)
        res, *_ = run(early, live=False, ignore_window=True)
        self.assertEqual(res.status, "dry-run")
        late = FakeBroker(now=at(END, 15, 58))
        self.assertEqual(run(late, live=True, ignore_window=True)[0].status, "skipped")      # too close to the close
        self.assertEqual(run(FakeBroker(now=at(END, 15, 50)), live=False)[0].status, "dry-run")
        self.assertEqual(run(FakeBroker(now=at(END, 15, 30)), live=False, run_at="15:30")[0].status, "dry-run")

    def test_only_one_live_run_per_day(self):
        first, broker, _, csv_path = run(live=True)
        self.assertEqual(first.status, "submitted")
        again, broker2, _, _ = run(FakeBroker(), live=True, runs_csv=csv_path)
        self.assertEqual(again.status, "skipped")
        self.assertIn("already submitted", again.reason)
        self.assertEqual(broker2.submitted, [])
        forced, broker3, _, _ = run(FakeBroker(), live=True, runs_csv=csv_path, force=True)
        self.assertEqual(forced.status, "submitted")
        self.assertEqual(run(FakeBroker(), live=False, runs_csv=csv_path)[0].status, "dry-run")   # dry runs are never blocked

    def test_decide_failure_is_skipped(self):
        with mock.patch.object(AGENT, "decide", side_effect=RuntimeError("boom")):
            res, broker, _, _ = run(live=True)
        self.assertEqual((res.status, broker.submitted), ("skipped", []))
        self.assertIn("decide() failed", res.reason)

    def test_account_and_position_guards(self):
        self.assertEqual(run(FakeBroker(status="ACCOUNT_UPDATED"))[0].status, "skipped")
        self.assertEqual(run(FakeBroker(cash=-500.0))[0].status, "skipped")
        self.assertEqual(run(FakeBroker(open_orders=[{"id": "x", "symbol": "QQQ"}]))[0].status, "skipped")
        foreign = run(FakeBroker(positions=[ar.Position("AAPL", 10.0)]), live=True)
        self.assertEqual(foreign[0].status, "aborted")
        self.assertIn("AAPL", foreign[0].reason)
        self.assertEqual(foreign[1].submitted, [])
        self.assertEqual(run(FakeBroker(positions=[ar.Position("QQQ", -5.0)]), live=True)[0].status, "aborted")

    def test_liquidate_foreign_sells_a_previous_strategys_stocks(self):
        old = [ar.Position("AMD", 36.0), ar.Position("MRVL", 66.0), ar.Position("PLTR", 55.0)]    # all in the live feed
        res, broker, out, _ = run(FakeBroker(cash=23_000.0, positions=old), live=False, liquidate_foreign=True)
        self.assertEqual(res.status, "dry-run", res.reason)
        self.assertEqual({o["symbol"] for o in res.orders if o["side"] == "sell"}, {"AMD", "MRVL", "PLTR"})
        self.assertLessEqual(res.gross_after, 1.45)
        self.assertEqual(broker.submitted, [])
        res, broker, _, _ = run(FakeBroker(cash=23_000.0, positions=old), live=True, liquidate_foreign=True)
        self.assertEqual(res.status, "submitted", res.reason)
        sides = [s for _, s, _, _ in broker.submitted]
        self.assertEqual(sides, sorted(sides, key=lambda s: 0 if s == "sell" else 1))
        final = {p.symbol for p in broker.get_positions()}
        self.assertFalse(final & {"AMD", "MRVL", "PLTR"})
        self.assertTrue({"TQQQ", "QLD", "QQQ", "SMH"} <= final)

    def test_liquidate_foreign_still_refuses_tickers_it_has_no_data_for(self):
        res, broker, _, _ = run(FakeBroker(positions=[ar.Position("ZZZZ", 5.0)]), live=True, liquidate_foreign=True)
        self.assertEqual((res.status, broker.submitted), ("skipped", []))
        self.assertIn("no bars for ZZZZ", res.reason)

    def test_second_day_on_target_book_sends_no_orders(self):
        res, broker, _, _ = run(live=True)
        res2, broker2, out, _ = run(broker, live=True, force=True)
        self.assertEqual(res2.status, "submitted")
        self.assertEqual(res2.orders, [])
        self.assertIn("no orders", out)

    def test_secrets_never_reach_logs_output_or_csv(self):
        class Exploding(FakeBroker):
            def get_bars(self, tickers, start, feed):
                raise RuntimeError(f"401 unauthorized for {KEY}:{SECRET}")
        tmp = tempfile.mkdtemp()
        buf = io.StringIO()
        with mock.patch.dict(os.environ, {"ALPACA_API_KEY": KEY, "ALPACA_SECRET_KEY": SECRET}), contextlib.redirect_stdout(buf):
            code = ar.main(["--runs-csv", f"{tmp}/runs.csv", "--log-dir", f"{tmp}/logs"], broker=Exploding(), agent=AGENT)
        for h in list(ar.log.handlers):
            h.flush()
        blob = buf.getvalue() + Path(f"{tmp}/logs/alpaca_runner.log").read_text() + Path(f"{tmp}/runs.csv").read_text()
        self.assertEqual(code, 1)
        self.assertIn("RuntimeError", blob)
        self.assertNotIn(KEY, blob)
        self.assertNotIn(SECRET, blob)
        self.assertIn("***", blob)

    def test_main_defaults_to_dry_run(self):
        tmp = tempfile.mkdtemp()
        broker = FakeBroker()
        with contextlib.redirect_stdout(io.StringIO()):
            code = ar.main(["--runs-csv", f"{tmp}/runs.csv", "--log-dir", f"{tmp}/logs"], broker=broker, agent=AGENT)
        self.assertEqual((code, broker.submitted), (0, []))
        self.assertEqual(rows(Path(f"{tmp}/runs.csv"))[0]["mode"], "dry-run")
        with contextlib.redirect_stdout(io.StringIO()):
            code = ar.main(["--live-paper", "--runs-csv", f"{tmp}/runs.csv", "--log-dir", f"{tmp}/logs"], broker=broker, agent=AGENT)
        self.assertEqual(code, 0)
        self.assertTrue(broker.submitted)

    def test_exit_codes(self):
        tmp = tempfile.mkdtemp()
        args = ["--runs-csv", f"{tmp}/runs.csv", "--log-dir", f"{tmp}/logs"]
        bad = [{"ticker": "QQQ", "side": "buy", "quantity": 400}]
        with contextlib.redirect_stdout(io.StringIO()), mock.patch.object(AGENT, "decide", return_value=bad):
            self.assertEqual(ar.main(args, broker=FakeBroker(), agent=AGENT), 2)           # safety abort
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(ar.main(args, broker=FakeBroker(is_open=False), agent=AGENT), 0)   # skipped is not a failure

    def test_missing_keys_refuse_to_start(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(SystemExit):
                ar.make_alpaca_broker()
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(ar.main(["--log-dir", tempfile.mkdtemp()]), 2)


try:
    from alpaca.data.enums import Adjustment, DataFeed
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.trading.client import TradingClient
    from alpaca.trading.enums import OrderSide, TimeInForce
    HAVE_ALPACA = True
except ImportError:
    HAVE_ALPACA = False


@unittest.skipUnless(HAVE_ALPACA, "alpaca-py not installed")
class TestAlpacaAdapter(unittest.TestCase):
    def clients(self, base="https://paper-api.alpaca.markets"):
        trading = mock.MagicMock(spec=TradingClient)
        trading._base_url = base
        return trading, mock.MagicMock(spec=StockHistoricalDataClient)

    def test_refuses_a_non_paper_endpoint(self):
        trading, data = self.clients("https://api.alpaca.markets")
        with self.assertRaises(SystemExit):
            ar.AlpacaBroker(trading, data)
        trading, data = self.clients("https://paper-api.alpaca.markets.evil.example")
        with self.assertRaises(SystemExit):
            ar.AlpacaBroker(trading, data)
        ar.AlpacaBroker(*self.clients())

    def test_real_client_construction_is_paper(self):
        with mock.patch.dict(os.environ, {"ALPACA_API_KEY": KEY, "ALPACA_SECRET_KEY": SECRET}):
            broker = ar.make_alpaca_broker()
        self.assertEqual(broker.base_url(), "https://paper-api.alpaca.markets")

    def test_a_live_client_is_refused_even_if_swapped_in_later(self):
        trading, data = self.clients()
        broker = ar.AlpacaBroker(trading, data)
        trading._base_url = "https://api.alpaca.markets"
        with self.assertRaises(SystemExit):
            broker.submit_market_order("QQQ", "buy", 1, "cid")
        trading.submit_order.assert_not_called()

    def test_order_request(self):
        trading, data = self.clients()
        trading.submit_order.return_value = SimpleNamespace(id="abc-123")
        oid = ar.AlpacaBroker(trading, data).submit_market_order("QQQ", "sell", 5, "tl-1")
        req = trading.submit_order.call_args[0][0]
        self.assertEqual((oid, req.symbol, req.qty, req.side, req.time_in_force, req.client_order_id),
                         ("abc-123", "QQQ", 5, OrderSide.SELL, TimeInForce.DAY, "tl-1"))

    def test_bars_are_adjusted_et_dated_and_deduplicated(self):
        trading, data = self.clients()
        utc = lambda y, m, d, h: datetime(y, m, d, h, tzinfo=ar.timezone.utc)
        bar = lambda ts, c: SimpleNamespace(timestamp=ts, open=c - 1, high=c + 1, low=c - 2, close=c, volume=10.0)
        data.get_stock_bars.return_value = SimpleNamespace(data={"QQQ": [
            bar(utc(2026, 10, 2, 4), 740.0), bar(utc(2026, 10, 5, 4), 756.2), bar(utc(2026, 10, 5, 4), 757.0)]})
        out = ar.AlpacaBroker(trading, data).get_bars(["QQQ"], utc(2025, 1, 1, 0), "iex")
        req = data.get_stock_bars.call_args[0][0]
        self.assertEqual((req.adjustment, req.feed), (Adjustment.ALL, DataFeed.IEX))
        self.assertEqual([b["ts"] for b in out["QQQ"]], ["2026-10-02", "2026-10-05"])
        self.assertEqual(out["QQQ"][-1], {"ts": "2026-10-05", "open": 756.0, "high": 758.0, "low": 755.0, "close": 757.0, "volume": 10})

    def test_positions_clock_account_and_calendar(self):
        trading, data = self.clients()
        trading.get_all_positions.return_value = [
            SimpleNamespace(symbol="qqq", qty="10", side=SimpleNamespace(value="long"), avg_entry_price="700.5"),
            SimpleNamespace(symbol="SPY", qty="3", side=SimpleNamespace(value="short"), avg_entry_price=None)]
        trading.get_clock.return_value = SimpleNamespace(timestamp=at(END, 15, 45), is_open=True)
        trading.get_account.return_value = SimpleNamespace(cash="1000.5", equity="2000", status=SimpleNamespace(value="ACTIVE"),
                                                           trading_blocked=False, account_blocked=False)
        trading.get_calendar.return_value = [SimpleNamespace(date=END, open=datetime(2026, 10, 5, 9, 30), close=datetime(2026, 10, 5, 16, 0))]
        b = ar.AlpacaBroker(trading, data)
        self.assertEqual([(p.symbol, p.qty) for p in b.get_positions()], [("QQQ", 10.0), ("SPY", -3.0)])
        self.assertTrue(b.get_clock().is_open)
        self.assertEqual((b.get_account().cash, b.get_account().status, b.get_account().blocked), (1000.5, "ACTIVE", False))
        s = b.get_sessions(END, END)[0]
        self.assertEqual((s.day, s.open.hour, s.close.hour, str(s.open.tzinfo)), (END, 9, 16, "America/New_York"))


if __name__ == "__main__":
    unittest.main(verbosity=1)
