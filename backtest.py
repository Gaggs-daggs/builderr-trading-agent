"""Walk-forward backtester over ~10 years of real daily bars.

    python fetch_history.py               # once: pulls bars into data_cache/bars.json
    python backtest.py                    # evaluates agent.py
    python backtest.py my_variant.py      # evaluates another file

Fill model mirrors preview.py: orders from day t fill at day t+1's open with
5 bps slippage (10 bps on leveraged ETFs), buys are capped by cash, marks are
on the close. Each window gets a freshly loaded agent (fresh globals), like
the real engine.

Windows: 60 trading days, a new one every 20 days, each with ~220 bars of
history. Split into TRAIN (window start before 2022-07-01) and TEST (after),
so a change only counts if it helps on years it was not tuned on.
"""
from __future__ import annotations

import importlib.util
import json
import math
import sys
from collections.abc import Mapping
from pathlib import Path

HERE = Path(__file__).parent
DATA = HERE / "data_cache" / "bars.json"
BETA_3X = {"TQQQ", "SOXL", "UPRO", "SPXL", "TNA", "FAS", "TECL", "LABU", "CURE", "DRN", "UDOW", "NAIL"}
BETA_2X = {"QLD", "SSO", "DDM", "ROM", "UWM", "AGQ"}
START_CASH = 100_000.0
HISTORY = 220
WINDOW = 60
STEP = 20
SPLIT = "2022-07-01"
ROUND2_START = "2026-07-07"


def beta(t: str) -> float:
    return 3.0 if t in BETA_3X else 2.0 if t in BETA_2X else 1.0


class Market:
    def __init__(self, raw: dict[str, list[list]]):
        self.cal = [r[0] for r in raw["SPY"]]
        self.bars: dict[str, list[dict]] = {}
        self.idx: dict[str, dict[str, int]] = {}
        for t, rows in raw.items():
            self.bars[t] = [
                {"ts": r[0], "open": r[1], "high": r[2], "low": r[3], "close": r[4], "volume": r[5]}
                for r in rows
            ]
            self.idx[t] = {r[0]: i for i, r in enumerate(rows)}

    def px(self, t: str, date: str, field: str):
        i = self.idx.get(t, {}).get(date)
        return None if i is None else self.bars[t][i][field]


class LazyState(Mapping):
    """market_state view: each ticker's last HISTORY bars up to `date`, sliced on demand."""

    def __init__(self, mkt: Market, date: str):
        self.m, self.d, self.cache = mkt, date, {}

    def _slice(self, t):
        if t not in self.cache:
            i = self.m.idx.get(t, {}).get(self.d)
            self.cache[t] = None if i is None else self.m.bars[t][max(0, i - HISTORY + 1): i + 1]
        return self.cache[t]

    def __getitem__(self, t):
        v = self._slice(t)
        if v is None:
            raise KeyError(t)
        return v

    def __iter__(self):
        return (t for t in self.m.bars if self._slice(t) is not None)

    def __len__(self):
        return sum(1 for _ in self)


def load(path: Path):
    spec = importlib.util.spec_from_file_location("agent_bt", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.decide


def simulate(agent_path: Path, mkt: Market, dates: list[str], cost_mult: float = 1.0, probe=None) -> dict:
    """Run one fresh agent over `dates`. Orders decided on day t's close fill at day t+1's open.

    cost_mult scales slippage (1.0 = 5 bps plain / 10 bps leveraged). `probe(state)`, if given,
    is called on each day's market view and its result recorded (e.g. the agent's regime).
    """
    decide = load(agent_path)
    cash, pos, pending = START_CASH, {}, []
    curve, gross_path, probes = [], [], []
    trades, traded, costs, errors = 0, 0.0, 0.0, 0
    peak_gross, peak_conc, streak, max_streak = 0.0, 0.0, {}, 0
    for date in dates:
        for o in pending:
            px = mkt.px(o["ticker"], date, "open")
            if px is None:
                continue
            slip = (0.001 if beta(o["ticker"]) > 1 else 0.0005) * cost_mult
            if o["side"] == "buy":
                fill = px * (1 + slip)
                qty = min(o["quantity"], cash / fill)
                if qty > 0:
                    pos[o["ticker"]] = pos.get(o["ticker"], 0.0) + qty
                    cash -= qty * fill
            else:
                qty = min(o["quantity"], pos.get(o["ticker"], 0.0))
                if qty > 0:
                    pos[o["ticker"]] -= qty
                    cash += qty * px * (1 - slip)
            if qty > 0:
                trades += 1
                traded += qty * px
                costs += qty * px * slip
        pending = []
        prices = {t: mkt.px(t, date, "close") for t in pos if pos[t] > 0}
        prices = {t: p for t, p in prices.items() if p is not None}
        value = {t: pos[t] * prices.get(t, 0.0) for t in pos}
        equity = max(cash + sum(value.values()), 1e-9)
        curve.append(equity)
        gross = sum(v * beta(t) for t, v in value.items()) / equity
        gross_path.append(gross)
        peak_gross = max(peak_gross, gross)
        for t, v in value.items():
            w = v / equity
            peak_conc = max(peak_conc, w)
            streak[t] = streak.get(t, 0) + 1 if w >= 0.30 else 0
            max_streak = max(max_streak, streak[t])
        state = LazyState(mkt, date)
        if probe is not None:
            probes.append(probe(state))
        last = {t: state[t][-1]["close"] for t in state if t in pos or t in ("SPY", "QQQ")}
        pf = {
            "cash": cash,
            "positions": [{"ticker": t, "quantity": q, "avg_cost": 0.0} for t, q in pos.items() if q > 0],
            "last_prices": last,
        }
        try:
            orders = decide(state, pf, cash) or []
        except Exception:  # noqa: BLE001
            errors += 1
            orders = []
        pending = [o for o in orders if o.get("side") in ("buy", "sell") and float(o.get("quantity", 0)) > 0]
    peak, mdd = curve[0], 0.0
    for v in curve:
        peak = max(peak, v)
        mdd = max(mdd, 1 - v / peak)
    ret = curve[-1] / START_CASH - 1
    avg_equity = sum(curve) / len(curve)
    return {"ret": ret, "mdd": mdd, "trades": trades, "curve": curve, "gross": peak_gross,
            "conc": peak_conc, "conc_streak": max_streak, "errors": errors,
            "avg_gross": sum(gross_path) / len(gross_path), "gross_path": gross_path, "probes": probes,
            "turnover": traded / avg_equity, "costs": costs, "sharpe": sharpe(curve),
            "calmar": (annualize(ret, len(curve)) / mdd) if mdd > 1e-9 else 0.0}


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
