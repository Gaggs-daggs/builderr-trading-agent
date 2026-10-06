"""Strategy-level checks for agent.py.

No network, no private engine, no third-party packages. These are not the
official builderr evals; they catch contract, cap, and regime bugs before
submission.

Run:
    python strategy_selftest.py
"""
from __future__ import annotations

import time
from datetime import date, timedelta

import agent


UNIVERSE = (
    "SPY", "QQQ", "XLK", "SMH", "XLP", "XLU", "XLV", "GLD",
    "QLD", "SSO", "TQQQ",
)
DEFENSIVE = {"XLP", "XLU", "XLV", "GLD"}
LEVERED = {"QLD", "SSO", "TQQQ"}


def bars(start: float, returns: list[float]) -> list[dict]:
    out = []
    px = start
    d = date(2024, 1, 1)
    for r in returns:
        px *= 1.0 + r
        out.append({
            "ts": d.isoformat(),
            "open": px,
            "high": px * 1.01,
            "low": px * 0.99,
            "close": px,
            "volume": 1_000_000,
        })
        d += timedelta(days=1)
    return out


def market(index_returns: list[float], other: float = 0.001) -> dict[str, list[dict]]:
    """SPY and QQQ follow `index_returns`; every other ticker drifts at `other` per day."""
    n = len(index_returns)
    data = {t: bars(100.0, [other] * n) for t in UNIVERSE}
    data["SPY"] = bars(100.0, index_returns)
    data["QQQ"] = bars(100.0, index_returns)
    return data


CALM_UP = [0.0015] * 260
CHOPPY_UP = [0.0015 + (0.025 if i % 2 else -0.025) for i in range(260)]
BEAR = [0.0015] * 120 + [-0.003] * 140
CRASH = [0.0015] * 255 + [-0.03] * 5


def portfolio(cash: float = 100_000.0, positions: list[dict] | None = None) -> dict:
    return {"cash": cash, "positions": positions or [], "last_prices": {}}


def buys(orders: list[dict]) -> set[str]:
    return {o["ticker"] for o in orders if o["side"] == "buy"}


def test_empty_data_returns_no_orders() -> None:
    assert agent.decide({}, portfolio(), 100_000.0) == []


def test_calm_uptrend_uses_levered_book() -> None:
    data = market(CALM_UP)
    assert agent.regime(data) == "ON"
    picked = buys(agent.decide(data, portfolio(), 100_000.0))
    assert picked & LEVERED, picked


def test_high_vol_uptrend_drops_leverage() -> None:
    data = market(CHOPPY_UP)
    assert agent.regime(data) != "ON", agent.regime(data)
    assert not (buys(agent.decide(data, portfolio(), 100_000.0)) & LEVERED)


def test_bear_market_holds_only_defensives() -> None:
    data = market(BEAR)
    assert agent.regime(data) == "OFF", agent.regime(data)
    assert buys(agent.decide(data, portfolio(), 100_000.0)) <= DEFENSIVE


def test_crash_brake_caps_exposure() -> None:
    data = market(CRASH)
    assert agent.crash_brake(data)
    assert agent.regime(data) in ("HALF", "OFF"), agent.regime(data)


def test_caps_hold_in_every_regime() -> None:
    for path in (CALM_UP, CHOPPY_UP, BEAR, CRASH):
        weights = agent.target_weights(market(path))
        assert all(w <= agent.CAP + 1e-9 for w in weights.values()), weights
        gross = sum(w * agent._beta(t) for t, w in weights.items())
        assert gross <= agent.MAX_BETA_GROSS + 1e-9, gross
        assert sum(weights.values()) <= 1.0 + 1e-9, weights


def test_overweight_position_is_trimmed() -> None:
    data = market(CALM_UP)
    px = data["QQQ"][-1]["close"]
    qty = 0.40 * 100_000.0 / px
    pf = portfolio(cash=60_000.0, positions=[{"ticker": "QQQ", "quantity": qty, "avg_cost": px}])
    sells = [o for o in agent.decide(data, pf, 60_000.0) if o["side"] == "sell" and o["ticker"] == "QQQ"]
    assert sells, "a 40% QQQ position must be cut below the 30% cap"
    assert (qty - sells[0]["quantity"]) * px / 100_000.0 < 0.30


def test_on_target_book_does_not_churn() -> None:
    data = market(CALM_UP)
    weights = agent.target_weights(data)
    positions, spent = [], 0.0
    for t, w in weights.items():
        px = data[t][-1]["close"]
        positions.append({"ticker": t, "quantity": w * 100_000.0 / px, "avg_cost": px})
        spent += w * 100_000.0
    cash = 100_000.0 - spent
    assert agent.decide(data, portfolio(cash, positions), cash) == []


def test_orders_are_bounded_and_fast() -> None:
    for path in (CALM_UP, CHOPPY_UP, BEAR, CRASH):
        start = time.perf_counter()
        orders = agent.decide(market(path), portfolio(), 100_000.0)
        assert time.perf_counter() - start < 5.0
        assert len(orders) <= 50
        for o in orders:
            assert o["side"] in ("buy", "sell") and o["quantity"] > 0 and o["ticker"] in UNIVERSE


def run() -> None:
    tests = [
        test_empty_data_returns_no_orders,
        test_calm_uptrend_uses_levered_book,
        test_high_vol_uptrend_drops_leverage,
        test_bear_market_holds_only_defensives,
        test_crash_brake_caps_exposure,
        test_caps_hold_in_every_regime,
        test_overweight_position_is_trimmed,
        test_on_target_book_does_not_churn,
        test_orders_are_bounded_and_fast,
    ]
    for test in tests:
        test()
    print(f"✓ {len(tests)} strategy checks passed.")


if __name__ == "__main__":
    run()
