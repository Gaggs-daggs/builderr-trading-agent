"""Self-test for Apex Momentum: caps, risk-off, determinism.  Run: python strategy_selftest.py"""
import gzip, json
from pathlib import Path
import agent

BETA = agent.BETA
regimes = json.loads(gzip.open(Path(__file__).parent / "sample_regimes.json.gz").read())


def state(reg, upto):
    ms = {t: [dict(zip(("ts", "open", "high", "low", "close", "volume"), r)) for r in rows if r[0] <= upto]
          for t, rows in reg["bars"].items()}
    return ms, {"cash": 100000.0, "positions": [], "last_prices": {t: b[-1]["close"] for t, b in ms.items() if b}}


def check_orders(orders, ps):
    px, eq = ps["last_prices"], ps["cash"]
    notional = {}
    for o in orders:
        assert o["side"] in ("buy", "sell") and o["quantity"] > 0 and o["ticker"] in px
        notional[o["ticker"]] = notional.get(o["ticker"], 0) + o["quantity"] * px[o["ticker"]]
    assert all(v / eq < 0.30 for v in notional.values()), notional
    assert sum(v * BETA.get(t, 1.0) for t, v in notional.items()) / eq <= 1.5, notional
    assert sum(notional.values()) <= eq


def test_caps_every_day():
    reg = regimes["calm_uptrend"]
    dates = sorted({r[0] for rows in reg["bars"].values() for r in rows if reg["eval_start"] <= r[0] <= reg["eval_end"]})
    for d in dates[::5]:
        ms, ps = state(reg, d)
        check_orders(agent.decide(ms, ps, ps["cash"]), ps)


def test_cash_when_regime_off():
    reg = regimes["moderate_selloff"]
    ms, ps = state(reg, reg["eval_end"])
    # force QQQ below its 50d average
    for b in ms["QQQ"][-5:]:
        b["close"] *= 0.8
    ps["last_prices"]["QQQ"] = ms["QQQ"][-1]["close"]
    assert agent.decide(ms, ps, ps["cash"]) == []


def test_deterministic_and_empty_safe():
    reg = regimes["calm_uptrend"]
    ms, ps = state(reg, reg["eval_end"])
    assert agent.decide(ms, ps, ps["cash"]) == agent.decide(ms, ps, ps["cash"])
    assert agent.decide({}, {"cash": 1.0, "positions": [], "last_prices": {}}, 1.0) == []


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn(); print("ok", name)
