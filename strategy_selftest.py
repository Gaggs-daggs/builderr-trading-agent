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


def test_limits_have_margin() -> None:
    assert agent.CAP <= 0.28 and agent.FORCE_TRIM_WEIGHT < 0.30
    assert agent.MAX_BETA_GROSS <= 1.45 and agent.FORCE_TRIM_GROSS <= 1.45


def test_overlevered_book_is_trimmed_under_cap() -> None:
    data = market(CALM_UP)
    px = {t: data[t][-1]["close"] for t in UNIVERSE}
    held = {"TQQQ": 0.30, "QLD": 0.30, "QQQ": 0.27}  # 1.77x beta gross
    positions = [{"ticker": t, "quantity": w * 100_000.0 / px[t], "avg_cost": px[t]} for t, w in held.items()]
    cash = 100_000.0 * (1 - sum(held.values()))
    orders = agent.decide(data, portfolio(cash, positions), cash)
    qty = {p["ticker"]: p["quantity"] for p in positions}
    for o in orders:
        qty[o["ticker"]] = qty.get(o["ticker"], 0.0) + (o["quantity"] if o["side"] == "buy" else -o["quantity"])
    gross = sum(q * px[t] * agent._beta(t) for t, q in qty.items()) / 100_000.0
    assert gross <= 1.45, gross
    assert all(q * px[t] / 100_000.0 < 0.30 for t, q in qty.items())


def test_regime_steps_down_and_back_up() -> None:
    path = [0.0015] * 260 + [-0.004] * 120 + [0.004] * 160
    seen = []
    for end in range(260, len(path) + 1, 5):
        tier = agent.regime(market(path[:end]))
        if not seen or seen[-1] != tier:
            seen.append(tier)
    assert seen[0] == "ON" and "OFF" in seen and seen[-1] == "ON", seen
    bottom = seen.index("OFF")
    ranks = [agent._RANK[t] for t in seen]
    assert ranks[:bottom + 1] == sorted(ranks[:bottom + 1], reverse=True), seen
    assert ranks[bottom:] == sorted(ranks[bottom:]), seen


def test_smoothing_reduces_flip_flops() -> None:
    # Index hovers around its long averages: the raw score flips often, the smoothed one less.
    path = [0.0015] * 200 + [0.012 if i % 3 == 0 else -0.006 for i in range(120)]
    raw_flips = smooth_flips = 0
    prev_raw = prev_smooth = None
    for end in range(220, len(path) + 1):
        data = market(path[:end])
        raw, smooth = agent.trend_score(data), agent.smoothed_score(data)
        raw_t = "hi" if raw >= agent.ON_SCORE else "lo"
        smooth_t = "hi" if smooth >= agent.ON_SCORE else "lo"
        raw_flips += prev_raw is not None and raw_t != prev_raw
        smooth_flips += prev_smooth is not None and smooth_t != prev_smooth
        prev_raw, prev_smooth = raw_t, smooth_t
    assert smooth_flips <= raw_flips, (smooth_flips, raw_flips)


def test_order_count_is_capped_even_from_a_messy_portfolio() -> None:
    data = market(CALM_UP)
    for i in range(120):
        data[f"X{i}"] = bars(100.0, [0.001] * 260)
    held = [{"ticker": f"X{i}", "quantity": 10.0, "avg_cost": 100.0} for i in range(120)]
    orders = agent.decide(data, portfolio(10_000.0, held), 10_000.0)
    assert 0 < len(orders) <= 50, len(orders)
    assert all(o["side"] == "sell" for o in orders), "sells must come first so cash is raised before buying"


def test_error_fallback_returns_empty() -> None:
    broken = market(CALM_UP)
    broken["QQQ"] = [{"close": "not a number"}] * 260
    assert agent.decide(broken, portfolio(), 100_000.0) == []
    original = agent._run
    try:
        agent._run = lambda *a, **k: 1 / 0
        assert agent.decide(market(CALM_UP), portfolio(), 100_000.0) == []
    finally:
        agent._run = original


def simulate_book(future: list[dict[str, float]]) -> tuple[float, float, int]:
    """Trade the agent through `future` (per-day return overrides by ticker) after a calm uptrend.

    Orders decided on day t fill at day t+1's price. Returns (peak beta gross, peak single-position
    weight, longest run of consecutive days with any position >= 30%), measured on every close.
    """
    hist = len(CALM_UP)
    rets = {t: [0.001] * (hist + len(future)) for t in UNIVERSE}
    for t in ("SPY", "QQQ"):
        rets[t] = list(CALM_UP) + [0.0] * len(future)
    for i, day in enumerate(future):
        for t, r in day.items():
            rets[t][hist + i] = r
    full = {t: bars(100.0, rets[t]) for t in UNIVERSE}
    cash, held = 100_000.0, {}
    peak_gross = peak_weight = 0.0
    streak: dict[str, int] = {}
    longest = 0
    for d in range(-3, len(future)):
        end = hist + d + 1
        data = {t: full[t][:end] for t in UNIVERSE}
        px = {t: data[t][-1]["close"] for t in UNIVERSE}
        equity = cash + sum(q * px[t] for t, q in held.items())
        if d >= 0:
            peak_gross = max(peak_gross, sum(q * px[t] * agent._beta(t) for t, q in held.items()) / equity)
            for t, q in held.items():
                w = q * px[t] / equity
                peak_weight = max(peak_weight, w)
                streak[t] = streak.get(t, 0) + 1 if w >= 0.30 else 0
                longest = max(longest, streak[t])
        pf = {"cash": cash, "last_prices": px,
              "positions": [{"ticker": t, "quantity": q, "avg_cost": px[t]} for t, q in held.items() if q > 0]}
        orders = agent.decide(data, pf, cash)
        assert len(orders) <= 50
        if end < len(full["QQQ"]):
            for o in orders:
                p = full[o["ticker"]][end]["close"]
                if o["side"] == "buy":
                    q = min(o["quantity"], cash / p)
                    held[o["ticker"]] = held.get(o["ticker"], 0.0) + q
                    cash -= q * p
                else:
                    q = min(o["quantity"], held.get(o["ticker"], 0.0))
                    held[o["ticker"]] = held.get(o["ticker"], 0.0) - q
                    cash += q * p
    return peak_gross, peak_weight, longest


def melt(q: float) -> dict[str, float]:
    """A day where QQQ moves q and every other ticker moves with its usual beta to QQQ."""
    return {"QQQ": q, "SPY": 0.8 * q, "QLD": 2 * q, "SSO": 1.6 * q, "TQQQ": 3 * q, "SMH": 1.4 * q, "XLK": 1.1 * q}


def test_rally_drift_stays_inside_limits() -> None:
    for name, path in {
        "steady rally": [melt(0.012)] * 25,
        "three +3% days": [melt(0.03)] * 3 + [{}] * 5,
        "rally then +5% day": [melt(0.012)] * 8 + [melt(0.05)] + [{}] * 3,
    }.items():
        gross, weight, longest = simulate_book(path)
        assert gross <= 1.5, (name, gross)
        assert weight < 0.30 and longest <= 1, (name, weight, longest)


def test_gap_down_stays_inside_limits() -> None:
    for name, path in {
        "-6% after rally": [melt(0.012)] * 6 + [melt(-0.06)] + [{}] * 4,
        "-10% after rally": [melt(0.012)] * 8 + [melt(-0.10)] + [{}] * 3,
        "-10% then +5% bounce": [melt(-0.10), melt(0.05)] + [{}] * 3,
    }.items():
        gross, weight, longest = simulate_book(path)
        assert gross <= 1.5, (name, gross)
        assert weight < 0.30 and longest <= 1, (name, weight, longest)


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
        test_limits_have_margin,
        test_rally_drift_stays_inside_limits,
        test_gap_down_stays_inside_limits,
        test_overlevered_book_is_trimmed_under_cap,
        test_regime_steps_down_and_back_up,
        test_smoothing_reduces_flip_flops,
        test_order_count_is_capped_even_from_a_messy_portfolio,
        test_error_fallback_returns_empty,
        test_orders_are_bounded_and_fast,
    ]
    for test in tests:
        test()
    print(f"✓ {len(tests)} strategy checks passed.")


if __name__ == "__main__":
    run()
