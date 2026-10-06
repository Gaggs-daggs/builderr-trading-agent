"""Trendline — a trend-tiered beta core built from liquid ETFs.

Why this design: on a 10-year walk-forward test with no hindsight in the
ticker list, short-horizon stock/sector momentum rotation trailed plain
QQQ/SPY exposure. What held up was simpler: own the market, size exposure by
how broadly the trend is intact, and use the allowed leverage only when the
trend is broad and volatility is calm.

Regime = fraction of trend lookbacks (SMA50..SMA250) that QQQ and SPY both
trade above, plus a QQQ volatility gate for the levered tier:

  ON    score >= 0.8 and QQQ vol20 < 28%  -> levered Nasdaq book (~1.35x beta)
  MID   score >= 0.4                      -> unlevered QQQ/SPY/tech book
  HALF  score >= 0.2                      -> half equity, half defensive
  OFF   otherwise                         -> staples/healthcare/utilities/gold

Averaging several lookbacks instead of a single SMA200 switch keeps the
regime call from hinging on one arbitrary number. A fast crash brake (QQQ
10-day vol > 45% or 5-day return < -7%) caps the tier at HALF, since the slow
averages lag a sharp break.

No network, no LLM, standard library only, stateless (no module globals), so
it behaves the same whether the engine reuses the process or not. Every
target stays under CAP; beta-adjusted gross is clamped under MAX_BETA_GROSS;
a rebalance is forced whenever live weights drift near either contest limit.
"""
from __future__ import annotations

import math
from statistics import pstdev
from typing import Any, Optional

TREND_LOOKBACKS = (50, 100, 150, 200, 250)
ON_SCORE = 0.8
MID_SCORE = 0.4
HALF_SCORE = 0.2
VOL_LOOKBACK = 20
VOL_ON = 0.28
CRASH_VOL10 = 0.45         # QQQ 10-day realized vol above this -> crash brake
CRASH_RET5 = -0.07         # QQQ 5-day return below this -> crash brake
CRASH_REGIME = "HALF"      # the most exposure allowed while the brake is on

CAP = 0.27                 # target weight per ticker (contest limit: < 30%)
FORCE_TRIM_WEIGHT = 0.29   # live weight that forces a rebalance
MAX_BETA_GROSS = 1.35      # target beta-adjusted gross (contest limit: 1.5x)
FORCE_TRIM_GROSS = 1.42    # live gross that forces a rebalance
DRIFT = 0.05               # rebalance when any weight is off target by this much
MIN_TRADE_PCT = 0.02
CASH_BUFFER = 0.995

BOOK_ON = {"TQQQ": 0.15, "QLD": 0.25, "QQQ": 0.27, "SMH": 0.20}
BOOK_MID = {"QQQ": 0.27, "SPY": 0.27, "XLK": 0.20, "SMH": 0.15}
BOOK_HALF = {"QQQ": 0.20, "SPY": 0.20, "XLP": 0.15, "XLV": 0.10, "GLD": 0.10}
BOOK_OFF = {"XLP": 0.20, "XLU": 0.15, "XLV": 0.15, "GLD": 0.20}

BETA: dict[str, float] = {
    "QLD": 2.0, "SSO": 2.0, "DDM": 2.0, "ROM": 2.0, "UWM": 2.0, "AGQ": 2.0,
    "TQQQ": 3.0, "SOXL": 3.0, "UPRO": 3.0, "SPXL": 3.0, "TNA": 3.0, "FAS": 3.0,
    "TECL": 3.0, "LABU": 3.0, "CURE": 3.0, "DRN": 3.0, "UDOW": 3.0, "NAIL": 3.0,
}


def _beta(ticker: str) -> float:
    return BETA.get(ticker, 1.0)


def _closes(market_state: Any, ticker: str) -> Optional[list[float]]:
    bars = market_state.get(ticker)
    if not bars:
        return None
    try:
        closes = [float(b["close"]) for b in bars]
    except (KeyError, TypeError, ValueError):
        return None
    return closes if closes[-1] > 0.0 else None


def _sma(closes: list[float], n: int) -> Optional[float]:
    return sum(closes[-n:]) / n if len(closes) >= n else None


def _vol(closes: Optional[list[float]], n: int) -> Optional[float]:
    if not closes or len(closes) < n + 1:
        return None
    window = closes[-(n + 1):]
    rets = [window[i] / window[i - 1] - 1.0 for i in range(1, len(window)) if window[i - 1] > 0.0]
    return pstdev(rets) * math.sqrt(252.0) if len(rets) > 1 else None


def trend_score(market_state: Any) -> float:
    """Fraction of (lookback, index) pairs where the index closes above its SMA."""
    votes: list[bool] = []
    for ticker in ("QQQ", "SPY"):
        closes = _closes(market_state, ticker)
        if not closes:
            continue
        for n in TREND_LOOKBACKS:
            sma = _sma(closes, n)
            if sma is not None:
                votes.append(closes[-1] > sma)
    return sum(votes) / len(votes) if votes else 0.0


_RANK = {"OFF": 0, "HALF": 1, "MID": 2, "ON": 3}


def crash_brake(market_state: Any) -> bool:
    """Fast-crash detector: the slow trend averages lag a sharp break, this does not."""
    qqq = _closes(market_state, "QQQ")
    if not qqq or len(qqq) < 6:
        return False
    vol10 = _vol(qqq, 10)
    ret5 = qqq[-1] / qqq[-6] - 1.0 if qqq[-6] > 0.0 else 0.0
    return (vol10 is not None and vol10 > CRASH_VOL10) or ret5 < CRASH_RET5


def regime(market_state: Any) -> str:
    score = trend_score(market_state)
    vol = _vol(_closes(market_state, "QQQ"), VOL_LOOKBACK)
    if score >= ON_SCORE and vol is not None and vol < VOL_ON:
        tier = "ON"
    elif score >= MID_SCORE:
        tier = "MID"
    elif score >= HALF_SCORE:
        tier = "HALF"
    else:
        tier = "OFF"
    if crash_brake(market_state) and _RANK[tier] > _RANK[CRASH_REGIME]:
        tier = CRASH_REGIME
    return tier


def target_weights(market_state: Any) -> dict[str, float]:
    book = {"ON": BOOK_ON, "MID": BOOK_MID, "HALF": BOOK_HALF, "OFF": BOOK_OFF}[regime(market_state)]
    weights = {t: min(w, CAP) for t, w in book.items() if w > 0.0 and _closes(market_state, t)}
    gross = sum(w * _beta(t) for t, w in weights.items())
    if gross > MAX_BETA_GROSS:
        weights = {t: w * MAX_BETA_GROSS / gross for t, w in weights.items()}
    return weights


def decide(market_state: dict, portfolio_state: dict, cash: float) -> list[dict]:
    """Return long-only orders moving the book toward the current regime's targets."""
    try:
        return _run(market_state or {}, portfolio_state or {}, cash)
    except Exception:  # noqa: BLE001 — never let a bad tick raise.
        return []


def _run(market_state: Any, portfolio_state: dict, cash: float) -> list[dict]:
    if not market_state or not _closes(market_state, "QQQ"):
        return []
    try:
        cash_value = float(portfolio_state.get("cash", cash))
    except (TypeError, ValueError):
        cash_value = float(cash or 0.0)
    last_prices = {str(k).upper(): v for k, v in (portfolio_state.get("last_prices") or {}).items()}

    held: dict[str, float] = {}
    for raw in portfolio_state.get("positions") or []:
        try:
            qty = float(raw.get("quantity", 0.0))
            ticker = str(raw["ticker"]).upper()
        except (KeyError, TypeError, ValueError):
            continue
        if qty > 0.0:
            held[ticker] = held.get(ticker, 0.0) + qty

    def price(ticker: str) -> Optional[float]:
        closes = _closes(market_state, ticker)
        if closes:
            return closes[-1]
        try:
            lp = float(last_prices.get(ticker) or 0.0)
        except (TypeError, ValueError):
            return None
        return lp if lp > 0.0 else None

    equity = cash_value + sum(q * (price(t) or 0.0) for t, q in held.items())
    if equity <= 0.0:
        return []

    targets = target_weights(market_state)
    current = {t: q * (price(t) or 0.0) / equity for t, q in held.items()}
    live_gross = sum(w * _beta(t) for t, w in current.items())
    near_limit = live_gross > FORCE_TRIM_GROSS or any(w > FORCE_TRIM_WEIGHT for w in current.values())
    off_target = any(abs(targets.get(t, 0.0) - current.get(t, 0.0)) >= DRIFT for t in set(targets) | set(current))
    stray = any(t not in targets for t in current)
    if not (near_limit or off_target or stray):
        return []

    orders: list[dict] = []
    proceeds = 0.0
    min_trade = MIN_TRADE_PCT * equity
    for ticker in sorted(held):
        px = price(ticker)
        if px is None:
            continue
        goal = targets.get(ticker, 0.0)
        if goal == 0.0:
            orders.append({"ticker": ticker, "side": "sell", "quantity": held[ticker]})
            proceeds += held[ticker] * px
            continue
        excess = held[ticker] - math.floor(goal * equity / px)
        forced = current.get(ticker, 0.0) > FORCE_TRIM_WEIGHT or near_limit
        if excess > 0 and (excess * px >= min_trade or forced):
            orders.append({"ticker": ticker, "side": "sell", "quantity": float(excess)})
            proceeds += excess * px

    spendable = cash_value + CASH_BUFFER * proceeds
    for ticker in sorted(targets, key=lambda t: (-targets[t], t)):
        px = price(ticker)
        if px is None:
            continue
        need = math.floor(targets[ticker] * equity / px) - held.get(ticker, 0.0)
        if need > 0 and need * px >= min_trade:
            qty = math.floor(min(need * px, spendable) / px)
            if qty > 0:
                orders.append({"ticker": ticker, "side": "buy", "quantity": float(qty)})
                spendable -= qty * px
    return [o for o in orders if o["quantity"] > 0.0]
