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
    "SPY", "QQQ", "XLK", "SMH", "XLP", "XLU", "XLV",
    "QLD", "SSO", "TQQQ",
)
DEFENSIVE = {"XLP", "XLU", "XLV"}
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
    assert agent.CAP <= 0.28 and agent.FORCE_TRIM_WEIGHT < 0.28
    assert agent.MAX_BETA_GROSS <= 1.45 and agent.FORCE_TRIM_GROSS <= 1.45
    assert agent.MAX_ORDERS == 45


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
        assert gross <= 1.45, (name, gross)
        assert weight <= 0.28 and longest == 0, (name, weight, longest)


def test_gap_down_stays_inside_limits() -> None:
    for name, path in {
        "-6% after rally": [melt(0.012)] * 6 + [melt(-0.06)] + [{}] * 4,
        "-10% after rally": [melt(0.012)] * 8 + [melt(-0.10)] + [{}] * 3,
        "-10% then +5% bounce": [melt(-0.10), melt(0.05)] + [{}] * 3,
    }.items():
        gross, weight, longest = simulate_book(path)
        assert gross <= 1.45, (name, gross)
        assert weight <= 0.28 and longest == 0, (name, weight, longest)


def test_tqqq_single_day_shocks_stay_inside_limits() -> None:
    """A single +/-15% TQQQ day (QQQ +/-5%), alone and after a rally."""
    for name, path in {
        "TQQQ +15% day": [melt(0.05)] + [{}] * 4,
        "TQQQ -15% day": [melt(-0.05)] + [{}] * 4,
        "rally then TQQQ +15%": [melt(0.012)] * 6 + [melt(0.05)] + [{}] * 3,
        "rally then TQQQ -15%": [melt(0.012)] * 6 + [melt(-0.05)] + [{}] * 3,
    }.items():
        gross, weight, longest = simulate_book(path)
        assert gross <= 1.45, (name, gross)
        assert weight <= 0.28 and longest == 0, (name, weight, longest)


def engine_fill(cash: float, held: dict[str, float], orders: list[dict], px: dict[str, float]):
    """live_runner.run_bot's opening-fill rules: sells first; each buy capped by cash,
    30% single-name room and 1.5x beta-gross room (slippage ignored)."""
    held = dict(held)
    for o in sorted(orders[:100], key=lambda o: 0 if o["side"] == "sell" else 1):
        t, q, p = o["ticker"], float(o["quantity"]), px[o["ticker"]]
        if o["side"] == "sell":
            q = min(q, held.get(t, 0.0))
            held[t] = held.get(t, 0.0) - q
            cash += q * p
            continue
        equity = cash + sum(v * px[k] for k, v in held.items())
        name_room = 0.30 * equity - held.get(t, 0.0) * p
        beta_room = 1.5 * equity - sum(v * px[k] * agent._beta(k) for k, v in held.items())
        q = max(0.0, min(q, min(cash, name_room, beta_room / agent._beta(t)) / p))
        held[t] = held.get(t, 0.0) + q
        cash -= q * p
    return cash, {t: v for t, v in held.items() if v > 1e-9}


def test_revision_transition_from_ridgeline_book() -> None:
    """If a revision inherits the old Ridgeline book (stocks + QLD/SSO sleeve at 1.45x),
    the first decision migrates it in one session without breaching any limit."""
    data = market(CALM_UP)
    for t in ("NVDA", "AMD", "MU"):
        data[t] = bars(100.0, [0.002] * len(CALM_UP))
    px = {t: v[-1]["close"] for t, v in data.items()}
    book = {"NVDA": 0.25, "AMD": 0.20, "MU": 0.10, "QLD": 0.23, "SSO": 0.22}
    held = {t: w * 100_000.0 / px[t] for t, w in book.items()}
    pf = {"cash": 0.0, "last_prices": px,
          "positions": [{"ticker": t, "quantity": q, "avg_cost": px[t]} for t, q in held.items()]}
    orders = agent.decide(data, pf, 0.0)
    assert 0 < len(orders) <= 45, len(orders)
    cash, after = engine_fill(0.0, held, orders, px)
    equity = cash + sum(q * px[t] for t, q in after.items())
    gross = sum(q * px[t] * agent._beta(t) for t, q in after.items()) / equity
    assert gross <= 1.45, gross
    assert all(q * px[t] / equity <= 0.28 for t, q in after.items()), after
    assert not set(after) & {"NVDA", "AMD", "MU", "SSO"}, after


def test_tier_is_independent_of_history_length() -> None:
    """Admission passes ~220 bars, the live board ~309: the tier must not depend on which."""
    path = [0.0015] * 300 + [-0.004] * 60 + [0.003] * 60
    for end in range(330, len(path) + 1, 6):
        full = market(path[:end])
        short = {t: v[-220:] for t, v in full.items()}
        assert agent.regime(full) == agent.regime(short), end
    thin = {t: v[-60:] for t, v in market(CALM_UP).items()}
    assert agent.trend_score(thin) <= 0.25 and agent.regime(thin) != "ON", "thin history must not look like a full trend"


def test_every_target_is_in_the_live_feed() -> None:
    from fetch_history import LIVE_FEED
    for book in (agent.BOOK_ON, agent.BOOK_MID, agent.BOOK_HALF, agent.BOOK_OFF):
        assert set(book) <= set(LIVE_FEED), set(book) - set(LIVE_FEED)


def test_drifted_position_is_trimmed_before_28pct() -> None:
    """A MID book whose QQQ drifted to 27.8% trims QQQ only (no tiny trims elsewhere)."""
    data = market([0.0015] * 200 + [0.0005] * 60, other=0.0005)
    px = {t: v[-1]["close"] for t, v in data.items()}
    weights = dict(agent.target_weights(data))
    assert "QQQ" in weights, weights
    weights["QQQ"] = 0.278
    held = {t: w * 100_000.0 / px[t] for t, w in weights.items()}
    cash = 100_000.0 - sum(q * px[t] for t, q in held.items())
    pf = {"cash": cash, "last_prices": px,
          "positions": [{"ticker": t, "quantity": q, "avg_cost": px[t]} for t, q in held.items()]}
    orders = agent.decide(data, pf, cash)
    sells = {o["ticker"]: o["quantity"] for o in orders if o["side"] == "sell"}
    assert set(sells) == {"QQQ"}, orders
    assert (held["QQQ"] - sells["QQQ"]) * px["QQQ"] / 100_000.0 <= agent.CAP + 1e-9


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
        test_tqqq_single_day_shocks_stay_inside_limits,
        test_revision_transition_from_ridgeline_book,
        test_tier_is_independent_of_history_length,
        test_every_target_is_in_the_live_feed,
        test_drifted_position_is_trimmed_before_28pct,
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
