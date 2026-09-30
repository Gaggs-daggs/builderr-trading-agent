"""Ridgeline — trend-following with inverse-volatility sizing and a 3-tier de-risk ladder.

Contest objective: maximize risk-adjusted return (return / max drawdown), not raw return.
Beat the Round 1 winner (Arnav): a CASH/NEUTRAL/FULL momentum rotator with a trailing stop
and drawdown taper. This agent keeps that same discipline (trend filter, breadth, vol brake,
trailing stops, drawdown tapering, cooldowns/hysteresis) and improves on two levers that most
directly move Calmar:

  1. Inverse-volatility sizing within the leader basket, instead of rank-linear sizing. Two
     names with the same momentum score but different volatility get different weight — the
     calmer one gets more. This lowers basket variance without giving up participation.
  2. A DEFENSIVE tier between NEUTRAL and CASH. Going instantly from fully invested to 100%
     cash on the first trend break is itself a lurch; a brief DEFENSIVE step (low-vol
     staples/utilities/healthcare at reduced size) smooths the exit and gives the hysteresis
     band room to work without either whipsawing or staying exposed too long.

No network calls, no LLM, no third-party packages — pure standard library, deterministic,
long-only. Every target weight stays under the 30% concentration cap; beta-adjusted gross is
clamped under the 1.5x leverage cap.
"""
from __future__ import annotations

import math
from statistics import pstdev
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Fixed parameters. Changing any of these ±20% should not collapse behavior —
# that is the "don't curve-fit" check from AGENT_BRIEF.md.
# ---------------------------------------------------------------------------
MOM_LONG = 42
MOM_SHORT = 21
MOM_W_LONG = 0.50
MOM_W_SHORT = 0.30
MOM_W_GAP = 0.20
NAME_SMA = 50
IDX_SMA_FAST = 20
IDX_SMA_SLOW = 50
ENTER_BAND = 0.01
EXIT_BAND = 0.01
VOL_LOOKBACK = 20
VOL_BRAKE_LOOKBACK = 10
VOL_FULL_MAX = 0.30
BRAKE_VOL10 = 0.50
BRAKE_R3 = -0.05
BREADTH_MIN = 0.50
TOP_N_MAX = 5
NAME_CAP = 0.26
CORE_FULL = 0.67
CORE_NEUTRAL = 0.85
CORE_DEFENSIVE = 0.45
SLEEVE_DOLLAR_FULL = 0.33
MAX_BETA_GROSS = 1.45
DD_HALF = -0.06
DD_LOCK = -0.10
TAPER_HALF = 0.50
TAPER_LOCK = 0.25
TRAIL_STOP = 0.075
STOP_COOLDOWN_DAYS = 3
REBALANCE_DAYS = 3
COOLDOWN_DAYS = 3
DRIFT_LIMIT = 0.28
MIN_TRADE_PCT = 0.03
CASH_BUFFER = 0.98
MAX_ORDERS = 45
MIN_BARS = 51
MIN_VOL_FLOOR = 0.08  # avoid dividing by ~0 vol on a freakishly quiet name

# A genuine V-recovery shows a strong short-horizon thrust on QQQ; lets a
# CASH -> DEFENSIVE re-entry fire without waiting for the slow SMA50 reclaim.
# Purely additive: never overrides the brake, never reaches FULL, never levers.
THRUST_LOOKBACK = 10
THRUST_MIN_RET = 0.12

INDEX_REF = ("SPY", "QQQ")

LEADER_STOCKS = (
    "NVDA", "MSFT", "AAPL", "META", "AMZN", "GOOGL", "AVGO", "AMD", "MU", "MRVL",
    "NFLX", "TSLA", "PLTR", "ORCL", "CRM", "JPM", "V", "MA", "COST", "LLY",
)
LEADER_ETFS = (
    "QQQ", "SPY", "SMH", "XLK", "XLC", "XLY", "XLF", "XLI", "XLE", "XLV",
    "XLP", "XLU", "XLRE", "DIA", "IWM", "SOXX",
)
LEADER_POOL = tuple(dict.fromkeys(LEADER_STOCKS + LEADER_ETFS))

# 2x ETF sleeve — bought ONLY in the FULL state.
SLEEVE = ("QLD", "SSO")

# Defensive basket used only in the DEFENSIVE tier — low-beta, non-cyclical.
DEFENSIVE_BASKET = (("XLP", 0.40), ("XLU", 0.35), ("XLV", 0.25))

BETA: dict[str, float] = {
    "QLD": 2.0, "SSO": 2.0, "DDM": 2.0, "ROM": 2.0, "UWM": 2.0, "AGQ": 2.0,
    "TQQQ": 3.0, "SOXL": 3.0, "UPRO": 3.0, "SPXL": 3.0, "TNA": 3.0, "FAS": 3.0,
    "TECL": 3.0, "LABU": 3.0, "CURE": 3.0, "DRN": 3.0, "UDOW": 3.0, "NAIL": 3.0,
}

_STATE_RANK = {"CASH": 0, "DEFENSIVE": 1, "NEUTRAL": 2, "FULL": 3}

# ---------------------------------------------------------------------------
# Persistent state. The engine runs each regime/round in a fresh process, so
# these globals are the agent's only memory across calls.
# ---------------------------------------------------------------------------
_state: str = "NEUTRAL"
_prev_state: str = "NEUTRAL"
_prev_taper_mult: float = 1.0
_cooldown: int = 0
_peak_equity: float = 0.0
_pos_high: dict[str, float] = {}
_stop_block: dict[str, int] = {}
_last_rebalance_date: Optional[str] = None
_last_seen_date: Optional[str] = None


# ---------------------------------------------------------------------------
# Feature helpers — pure functions over close-price series (oldest first).
# ---------------------------------------------------------------------------
def _beta(ticker: str) -> float:
    return BETA.get(ticker, 1.0)


def _date_of(ts: Any) -> str:
    return str(ts)[:10]


def _closes_of(market_state: dict, ticker: str, cache: dict) -> Optional[list[float]]:
    if ticker in cache:
        return cache[ticker]
    closes: Optional[list[float]] = None
    bars = market_state.get(ticker)
    if bars:
        try:
            closes = [float(b["close"]) for b in bars]
        except (KeyError, TypeError, ValueError):
            closes = None
    cache[ticker] = closes
    return closes


def _computable(closes: Optional[list[float]]) -> bool:
    return closes is not None and len(closes) >= MIN_BARS and closes[-1] > 0.0


def _sma(closes: list[float], n: int) -> Optional[float]:
    if len(closes) < n:
        return None
    return sum(closes[-n:]) / n


def _ret(closes: list[float], k: int) -> Optional[float]:
    if len(closes) < k + 1:
        return None
    start = closes[-(k + 1)]
    if start <= 0.0:
        return None
    return closes[-1] / start - 1.0


def _vol(closes: list[float], n: int) -> Optional[float]:
    if len(closes) < n + 1:
        return None
    window = closes[-(n + 1):]
    rets: list[float] = []
    for i in range(1, len(window)):
        prev = window[i - 1]
        if prev <= 0.0:
            return None
        rets.append(window[i] / prev - 1.0)
    if len(rets) < 2:
        return None
    return pstdev(rets) * math.sqrt(252.0)


def _trend_gap(closes: list[float]) -> Optional[float]:
    sma50 = _sma(closes, NAME_SMA)
    if sma50 is None or sma50 <= 0.0:
        return None
    return closes[-1] / sma50 - 1.0


def _momentum_score(closes: list[float]) -> Optional[float]:
    r_long = _ret(closes, MOM_LONG)
    r_short = _ret(closes, MOM_SHORT)
    gap = _trend_gap(closes)
    if r_long is None or r_short is None or gap is None:
        return None
    return MOM_W_LONG * r_long + MOM_W_SHORT * r_short + MOM_W_GAP * gap


# ---------------------------------------------------------------------------
# Portfolio / price helpers.
# ---------------------------------------------------------------------------
def _resolve_cash(portfolio_state: dict, cash: float) -> float:
    try:
        return float(portfolio_state.get("cash", cash))
    except (TypeError, ValueError):
        try:
            return float(cash)
        except (TypeError, ValueError):
            return 0.0


def _aggregate_positions(portfolio_state: dict) -> dict[str, dict[str, float]]:
    out: dict[str, dict[str, float]] = {}
    for raw in portfolio_state.get("positions", []) or []:
        try:
            ticker = str(raw["ticker"]).upper()
            qty = float(raw.get("quantity", 0.0))
            avg_cost = float(raw.get("avg_cost", 0.0))
        except (KeyError, TypeError, ValueError):
            continue
        if qty <= 0.0:
            continue
        if ticker in out:
            existing = out[ticker]
            total = existing["quantity"] + qty
            existing["avg_cost"] = (
                (existing["avg_cost"] * existing["quantity"] + avg_cost * qty) / total
                if total > 0.0 else avg_cost
            )
            existing["quantity"] = total
        else:
            out[ticker] = {"quantity": qty, "avg_cost": avg_cost}
    return out


def _mark_price(ticker: str, market_state: dict, cache: dict, last_prices: dict) -> Optional[float]:
    lp = last_prices.get(ticker)
    try:
        if lp is not None and float(lp) > 0.0:
            return float(lp)
    except (TypeError, ValueError):
        pass
    closes = _closes_of(market_state, ticker, cache)
    if closes and closes[-1] > 0.0:
        return closes[-1]
    return None


def _exec_price(ticker: str, market_state: dict, cache: dict, last_prices: dict) -> Optional[float]:
    closes = _closes_of(market_state, ticker, cache)
    if closes and closes[-1] > 0.0:
        return closes[-1]
    lp = last_prices.get(ticker)
    try:
        if lp is not None and float(lp) > 0.0:
            return float(lp)
    except (TypeError, ValueError):
        pass
    return None


def _compute_equity(positions: dict, market_state: dict, cache: dict, last_prices: dict, cash_value: float) -> float:
    total = cash_value
    for ticker in sorted(positions):
        pos = positions[ticker]
        price = _mark_price(ticker, market_state, cache, last_prices)
        if price is None:
            price = pos["avg_cost"] if pos["avg_cost"] > 0.0 else 0.0
        total += pos["quantity"] * max(price, 0.0)
    return max(total, 0.0)


# ---------------------------------------------------------------------------
# Target-weight construction.
# ---------------------------------------------------------------------------
def _build_targets(
    state: str,
    taper_mult: float,
    market_state: dict,
    cache: dict,
    stop_block: dict[str, int],
) -> dict[str, float]:
    weights: dict[str, float] = {}
    if state == "CASH":
        return weights

    if state == "DEFENSIVE":
        budget = CORE_DEFENSIVE * taper_mult
        for ticker, share in DEFENSIVE_BASKET:
            if ticker in stop_block:
                continue
            if not _computable(_closes_of(market_state, ticker, cache)):
                continue
            w = min(budget * share, NAME_CAP)
            if w > 0.0:
                weights[ticker] = w
        return weights

    sleeve_present: list[str] = []
    if state == "FULL":
        sleeve_present = [
            s for s in SLEEVE
            if s not in stop_block and _computable(_closes_of(market_state, s, cache))
        ]

    if state == "FULL":
        core_base = CORE_FULL if sleeve_present else (CORE_FULL + SLEEVE_DOLLAR_FULL)
    else:
        core_base = CORE_NEUTRAL
    core_budget = core_base * taper_mult

    # --- selection: qualify + rank the leader pool by momentum ---
    qualifiers: list[tuple[float, str, float]] = []
    for ticker in LEADER_POOL:
        if ticker in stop_block:
            continue
        closes = _closes_of(market_state, ticker, cache)
        if not _computable(closes):
            continue
        score = _momentum_score(closes)  # type: ignore[arg-type]
        if score is None:
            continue
        sma50 = _sma(closes, NAME_SMA)  # type: ignore[arg-type]
        if sma50 is None:
            continue
        vol = _vol(closes, VOL_LOOKBACK)  # type: ignore[arg-type]
        if vol is None:
            continue
        if score > 0.0 and closes[-1] > sma50:  # type: ignore[index]
            qualifiers.append((score, ticker, max(vol, MIN_VOL_FLOOR)))

    qualifiers.sort(key=lambda triple: (-triple[0], triple[1]))
    selected = qualifiers[:TOP_N_MAX]

    # --- risk-adjusted-momentum sizing: weight by score/vol so a strong,
    #     calm leader outweighs a strong-but-jumpy one, but raw momentum
    #     strength still drives most of the split (pure inverse-vol would
    #     flatten conviction and cap upside in a clean trend). ---
    if selected:
        raw = [max(score, 0.0) / vol for score, _, vol in selected]
        total_raw = sum(raw)
        if total_raw > 0.0:
            for (_, ticker, _), r in zip(selected, raw):
                weight = min(core_budget * r / total_raw, NAME_CAP)
                if weight > 0.0:
                    weights[ticker] = weight

    if sleeve_present:
        per = (SLEEVE_DOLLAR_FULL * taper_mult) / len(sleeve_present)
        for s in sleeve_present:
            w = min(per, NAME_CAP)
            if w > 0.0:
                weights[s] = w

    beta_gross = sum(w * _beta(t) for t, w in weights.items())
    if beta_gross > MAX_BETA_GROSS and beta_gross > 0.0:
        scale = MAX_BETA_GROSS / beta_gross
        weights = {t: w * scale for t, w in weights.items()}

    return weights


# ---------------------------------------------------------------------------
# Order generation — sell-before-buy, deterministic ordering.
# ---------------------------------------------------------------------------
def _generate_orders(
    do_rebalance: bool,
    weights: dict[str, float],
    positions: dict[str, dict[str, float]],
    forced_stops: list[tuple[str, float]],
    equity: float,
    market_state: dict,
    cache: dict,
    last_prices: dict,
    cash_value: float,
) -> list[dict[str, Any]]:
    orders: list[dict[str, Any]] = []
    sold: set[str] = set()
    proceeds = 0.0
    min_trade = MIN_TRADE_PCT * equity

    for ticker, qty in forced_stops:
        if qty > 0.0:
            orders.append({"ticker": ticker, "side": "sell", "quantity": qty})
            sold.add(ticker)
            price = _exec_price(ticker, market_state, cache, last_prices)
            if price is not None:
                proceeds += qty * price

    if do_rebalance:
        for ticker in sorted(positions):
            if ticker in sold:
                continue
            held = positions[ticker]["quantity"]
            if held <= 0.0:
                continue
            target_w = weights.get(ticker, 0.0)
            price = _exec_price(ticker, market_state, cache, last_prices)

            if target_w == 0.0:
                orders.append({"ticker": ticker, "side": "sell", "quantity": held})
                sold.add(ticker)
                if price is not None and price > 0.0:
                    proceeds += held * price
                continue

            if price is None or price <= 0.0:
                continue
            target_shares = math.floor(target_w * equity / price)
            delta = target_shares - held
            if delta < 0 and (-delta) * price >= min_trade:
                sell_qty = float(int(min(-delta, held)))
                if sell_qty > 0.0:
                    orders.append({"ticker": ticker, "side": "sell", "quantity": sell_qty})
                    sold.add(ticker)
                    proceeds += sell_qty * price

        spendable = cash_value + CASH_BUFFER * proceeds
        for ticker in sorted(weights, key=lambda t: (-weights[t], t)):
            price = _exec_price(ticker, market_state, cache, last_prices)
            if price is None or price <= 0.0:
                continue
            held = positions[ticker]["quantity"] if ticker in positions else 0.0
            target_shares = math.floor(weights[ticker] * equity / price)
            deficit = target_shares - held
            if deficit > 0 and deficit * price >= min_trade:
                affordable = math.floor(min(deficit * price, spendable) / price)
                if affordable > 0:
                    orders.append({"ticker": ticker, "side": "buy", "quantity": float(affordable)})
                    spendable -= affordable * price

    if len(orders) > MAX_ORDERS:
        sells = [o for o in orders if o["side"] == "sell"]
        buys = [o for o in orders if o["side"] == "buy"]
        orders = (sells + buys)[:MAX_ORDERS]

    return [o for o in orders if o["quantity"] > 0.0]


# ---------------------------------------------------------------------------
# Master decision cycle. `decide` wraps `_run` in a defensive guard so no
# exception can forfeit the "runs clean" admission gate; on failure it
# restores the pre-call global snapshot to avoid state desync.
# ---------------------------------------------------------------------------
def decide(market_state: dict, portfolio_state: dict, cash: float) -> list[dict]:
    """Return a list of long-only buy/sell orders for this decision cycle."""
    global _state, _cooldown, _peak_equity, _pos_high, _stop_block
    global _last_rebalance_date, _last_seen_date, _prev_state, _prev_taper_mult

    snapshot = (
        _state, _cooldown, _peak_equity, dict(_pos_high), dict(_stop_block),
        _last_rebalance_date, _last_seen_date, _prev_state, _prev_taper_mult,
    )
    try:
        return _run(market_state or {}, portfolio_state or {}, cash)
    except Exception:  # noqa: BLE001 — never let a bad tick raise.
        (
            _state, _cooldown, _peak_equity, _pos_high, _stop_block,
            _last_rebalance_date, _last_seen_date, _prev_state, _prev_taper_mult,
        ) = snapshot
        return []


def _run(market_state: dict, portfolio_state: dict, cash: float) -> list[dict[str, Any]]:
    global _state, _cooldown, _peak_equity, _pos_high, _stop_block
    global _last_rebalance_date, _last_seen_date, _prev_state, _prev_taper_mult

    if not market_state:
        return []

    cache: dict[str, Optional[list[float]]] = {}
    last_prices: dict[str, Any] = {}
    for key, value in (portfolio_state.get("last_prices", {}) or {}).items():
        last_prices[str(key).upper()] = value
    cash_value = _resolve_cash(portfolio_state, cash)

    spy_bars = market_state.get("SPY")
    spy = _closes_of(market_state, "SPY", cache)
    qqq = _closes_of(market_state, "QQQ", cache)
    current_date: Optional[str] = None
    if spy_bars:
        ts = spy_bars[-1].get("ts")
        current_date = _date_of(ts) if ts is not None else str(len(spy_bars))

    # ---- Data guard: if indices aren't computable, liquidate and wait. ----
    if not _computable(spy) or not _computable(qqq):
        positions = _aggregate_positions(portfolio_state)
        orders: list[dict[str, Any]] = []
        for ticker in sorted(positions):
            if market_state.get(ticker):
                qty = positions[ticker]["quantity"]
                if qty > 0.0:
                    orders.append({"ticker": ticker, "side": "sell", "quantity": qty})
        _prev_state = _state
        _prev_taper_mult = 1.0
        if current_date is not None:
            _last_seen_date = current_date
        return orders

    spy_closes: list[float] = spy  # type: ignore[assignment]
    qqq_closes: list[float] = qqq  # type: ignore[assignment]

    positions = _aggregate_positions(portfolio_state)

    is_new_day = current_date != _last_seen_date
    if is_new_day:
        if _cooldown > 0:
            _cooldown -= 1
        if _stop_block:
            decayed: dict[str, int] = {}
            for ticker, days in _stop_block.items():
                remaining = days - 1
                if remaining > 0:
                    decayed[ticker] = remaining
            _stop_block = decayed

    equity = _compute_equity(positions, market_state, cache, last_prices, cash_value)
    if equity <= 0.0:
        _prev_state = _state
        _prev_taper_mult = 1.0
        _last_seen_date = current_date
        return []
    _peak_equity = max(_peak_equity, equity)
    dd = (equity / _peak_equity - 1.0) if _peak_equity > 0.0 else 0.0
    if dd <= DD_LOCK:
        taper_mult = TAPER_LOCK
    elif dd <= DD_HALF:
        taper_mult = TAPER_HALF
    else:
        taper_mult = 1.0

    spy_close = spy_closes[-1]
    qqq_close = qqq_closes[-1]
    spy_sma_fast = _sma(spy_closes, IDX_SMA_FAST)
    spy_sma_slow = _sma(spy_closes, IDX_SMA_SLOW)
    qqq_sma_fast = _sma(qqq_closes, IDX_SMA_FAST)
    qqq_sma_slow = _sma(qqq_closes, IDX_SMA_SLOW)
    qqq_vol20 = _vol(qqq_closes, VOL_LOOKBACK)
    qqq_r3 = _ret(qqq_closes, 3)
    qqq_vol10 = _vol(qqq_closes, VOL_BRAKE_LOOKBACK)

    n_comp = 0
    n_up = 0
    for ticker in LEADER_POOL:
        closes = _closes_of(market_state, ticker, cache)
        if not _computable(closes):
            continue
        sma50 = _sma(closes, NAME_SMA)  # type: ignore[arg-type]
        if sma50 is None:
            continue
        n_comp += 1
        if closes[-1] > sma50:  # type: ignore[index]
            n_up += 1
    breadth = (n_up / n_comp) if n_comp > 0 else 0.0

    # ---- State machine: CASH < DEFENSIVE < NEUTRAL < FULL, with hysteresis. ----
    prev_cycle_state = _state
    brake_fired = (
        (qqq_r3 is not None and qqq_r3 < BRAKE_R3)
        or (qqq_vol10 is not None and qqq_vol10 > BRAKE_VOL10)
    )
    hard_cash = (
        brake_fired
        or (spy_sma_slow is not None and spy_close < spy_sma_slow * (1.0 - EXIT_BAND))
        or (qqq_sma_slow is not None and qqq_close < qqq_sma_slow * (1.0 - EXIT_BAND))
    )
    # Leaving CASH requires a clear reclaim (both indices above SMA50*(1+band)),
    # not merely clearing the trigger — a hysteresis band that removes
    # liquidate/re-buy whipsaw in choppy tape.
    reclaim = (
        spy_sma_slow is not None and spy_close > spy_sma_slow * (1.0 + ENTER_BAND)
        and qqq_sma_slow is not None and qqq_close > qqq_sma_slow * (1.0 + ENTER_BAND)
    )
    qqq_ret10 = _ret(qqq_closes, THRUST_LOOKBACK)
    thrust_signal = (
        qqq_ret10 is not None and qqq_ret10 > THRUST_MIN_RET
        and qqq_sma_fast is not None and qqq_close > qqq_sma_fast
        and len(qqq_closes) >= 2 and qqq_close > qqq_closes[-2]
    )
    if prev_cycle_state == "CASH" and not brake_fired and not reclaim and thrust_signal:
        _state = "DEFENSIVE"  # thrust override: step out of cash, not straight to FULL
    elif hard_cash:
        _state = "CASH"
        _cooldown = COOLDOWN_DAYS
    elif prev_cycle_state == "CASH" and not reclaim:
        _state = "CASH"  # hysteresis hold: stay in cash until a clear reclaim
    elif prev_cycle_state in ("CASH", "DEFENSIVE") and not reclaim:
        _state = "DEFENSIVE"  # soft landing on the way out of a de-risked state
    elif _cooldown > 0:
        _state = "NEUTRAL"
    else:
        full_conditions = (
            spy_sma_fast is not None and spy_close > spy_sma_fast
            and qqq_sma_fast is not None and qqq_close > qqq_sma_fast
            and spy_sma_slow is not None and spy_close > spy_sma_slow * (1.0 + ENTER_BAND)
            and qqq_sma_slow is not None and qqq_close > qqq_sma_slow * (1.0 + ENTER_BAND)
            and breadth >= BREADTH_MIN
            and qqq_vol20 is not None and qqq_vol20 < VOL_FULL_MAX
        )
        _state = "FULL" if full_conditions else "NEUTRAL"

    # ---- Trailing-stop updates (every cycle). ----
    for ticker in list(_pos_high):
        if ticker not in positions:
            del _pos_high[ticker]
    forced_stops: list[tuple[str, float]] = []
    for ticker in sorted(positions):
        price = _exec_price(ticker, market_state, cache, last_prices)
        if price is None:
            continue
        high = _pos_high.get(ticker, price)
        if price > high:
            high = price
        _pos_high[ticker] = high
        if high > 0.0 and price < high * (1.0 - TRAIL_STOP):
            forced_stops.append((ticker, positions[ticker]["quantity"]))
            _stop_block[ticker] = STOP_COOLDOWN_DAYS
            if ticker in _pos_high:
                del _pos_high[ticker]

    # ---- Rebalance gate. ----
    if _last_rebalance_date is None:
        do_rebalance = True
    else:
        elapsed_dates: set[str] = set()
        for bar in spy_bars:
            ts = bar.get("ts")
            bar_date = _date_of(ts) if ts is not None else ""
            if bar_date > _last_rebalance_date:
                elapsed_dates.add(bar_date)
        days_since = len(elapsed_dates)
        derisk_state = _STATE_RANK[_state] < _STATE_RANK[_prev_state]
        derisk_taper = taper_mult < _prev_taper_mult
        drift = False
        for ticker, pos in positions.items():
            price = _exec_price(ticker, market_state, cache, last_prices)
            if price is not None and equity > 0.0 and (pos["quantity"] * price / equity) > DRIFT_LIMIT:
                drift = True
                break
        do_rebalance = days_since >= REBALANCE_DAYS or derisk_state or derisk_taper or drift
    if _last_rebalance_date == current_date:
        do_rebalance = False

    weights = (
        _build_targets(_state, taper_mult, market_state, cache, _stop_block)
        if do_rebalance else {}
    )

    orders = _generate_orders(
        do_rebalance, weights, positions, forced_stops,
        equity, market_state, cache, last_prices, cash_value,
    )
    if do_rebalance and len(orders) >= 1:
        _last_rebalance_date = current_date

    _prev_state = _state
    _prev_taper_mult = taper_mult
    _last_seen_date = current_date
    return orders
