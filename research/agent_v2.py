"""Apex Momentum v2 — aggressive, stateless, stdlib-only."""
from __future__ import annotations
from math import sqrt

P = dict(
    K=3,              # names held
    W_MAX=0.26,       # per-name cap (engine cap is 0.30)
    LEV_SHARE=0.25,   # share of equity in 3x ETFs when regime is hot
    GROSS_MAX=1.44,
    MIN_DOLLAR_VOL=40e6,
    W63=0.45, W21=0.35, W126=0.20,
    VOL_PEN=0.5,      # score / vol**VOL_PEN
    NEAR_HIGH=0.10,   # must be within 10% of 20d high
    BAND=0.03,        # rebalance band (fraction of equity)
    HOLD_RANK=8,      # keep incumbents if still within top-N
    QQQ_VOL_OFF=0.40,
    STOP=0.10,
    TRIM_AT=0.285,    # hard trim: never sit near the 30% concentration line
    GROSS_TRIM=1.47,
    CRASH_3D=-0.04,
)
BETA = {"TQQQ": 3., "SOXL": 3., "UPRO": 3., "SPXL": 3., "TNA": 3., "FAS": 3., "TECL": 3., "LABU": 3.,
        "CURE": 3., "DRN": 3., "UDOW": 3., "NAIL": 3., "QLD": 2., "SSO": 2., "DDM": 2., "ROM": 2., "UWM": 2., "AGQ": 2.}
EXCLUDE = set(BETA) | {"SPY", "QQQ", "DIA", "IWM", "XLK", "XLC", "XLY", "XLF", "XLI", "XLE", "XLV", "XLP", "XLU",
                       "XLRE", "GLD", "TLT", "USO", "XOP", "XBI", "ARKK", "SOXX"}


def _c(bars):
    try:
        return [float(b["close"]) for b in bars]
    except Exception:
        return []


def _sma(x, n):
    return sum(x[-n:]) / n if len(x) >= n else None


def _vol(x, n):
    if len(x) <= n:
        return None
    r = [x[i] / x[i - 1] - 1 for i in range(len(x) - n, len(x))]
    m = sum(r) / n
    return sqrt(sum((a - m) ** 2 for a in r) / n) * sqrt(252)


def _ret(x, n):
    return x[-1] / x[-1 - n] - 1 if len(x) > n else None


def regime(ms):
    q, s = _c(ms.get("QQQ", [])), _c(ms.get("SPY", []))
    if len(q) < 60 or len(s) < 60:
        return 0.0, False
    q50, s50, q20 = _sma(q, 50), _sma(s, 50), _sma(q, 20)
    qv = _vol(q, 20)
    hi20 = max(q[-20:])
    crash = q[-1] / q[-4] - 1 < P["CRASH_3D"]
    on = (not crash) and q[-1] > q50 and s[-1] > s50 and qv < P["QQQ_VOL_OFF"] and q[-1] > hi20 * 0.93
    hot = on and q[-1] > q20 and q20 > q50 and qv < 0.30 and q[-1] > hi20 * 0.97
    return (1.0 if on else 0.0), hot


def rank(ms):
    out = []
    for t, bars in ms.items():
        if t in EXCLUDE:
            continue
        x = _c(bars)
        if len(x) < 130 or x[-1] < 5:
            continue
        try:
            dv = sum(float(b["close"]) * float(b["volume"]) for b in bars[-20:]) / 20
        except Exception:
            continue
        if dv < P["MIN_DOLLAR_VOL"]:
            continue
        r21, r63, r126 = _ret(x, 21), _ret(x, 63), _ret(x, 126)
        s20, s50, v = _sma(x, 20), _sma(x, 50), _vol(x, 20)
        if None in (r21, r63, r126, s20, s50, v) or v <= 0:
            continue
        if not (x[-1] > s20 and x[-1] > s50 and r21 > 0 and x[-1] >= max(x[-20:]) * (1 - P["NEAR_HIGH"])):
            continue
        mom = P["W63"] * r63 + P["W21"] * r21 + P["W126"] * r126
        if mom <= 0:
            continue
        out.append((mom / (v ** P["VOL_PEN"]), t, v))
    out.sort(reverse=True)
    return out


def targets(ms, held):
    on, hot = regime(ms)
    if not on:
        return {}
    ranked = rank(ms)
    names = [t for _, t, _ in ranked]
    keep = [t for t in held if t in names[:P["HOLD_RANK"]]]
    pick = list(keep)
    for t in names:
        if len(pick) >= P["K"]:
            break
        if t not in pick:
            pick.append(t)
    pick = pick[:P["K"]]
    if not pick:
        return {}
    lev = P["LEV_SHARE"] if hot else 0.0
    levt = {}
    if lev > 0:
        sm, qq = _c(ms.get("SMH", [])), _c(ms.get("QQQ", []))
        sx, tq = _c(ms.get("SOXL", [])), _c(ms.get("TQQQ", []))
        use_soxl = len(sx) > 50 and len(sm) > 50 and sm[-1] > _sma(sm, 20) and (_ret(sm, 21) or 0) > (_ret(qq, 21) or 0) and sx[-1] > _sma(sx, 20)
        use_tqqq = len(tq) > 50 and tq[-1] > _sma(tq, 20)
        levt = {"SOXL": lev} if use_soxl else ({"TQQQ": lev} if use_tqqq else {})
        if not levt:
            lev = 0.0
    budget = 1.0 - lev - 0.02
    inv = {t: 1.0 / max(v, 0.15) for _, t, v in ranked if t in pick}
    tot = sum(inv.values())
    w = {t: min(P["W_MAX"], budget * inv[t] / tot) for t in pick}
    w.update(levt)
    g = sum(wt * BETA.get(t, 1.0) for t, wt in w.items())
    if g > P["GROSS_MAX"]:
        w = {t: wt * P["GROSS_MAX"] / g for t, wt in w.items()}
    return w


def decide(market_state, portfolio_state, cash):
    ms = market_state or {}
    if not ms:
        return []
    lp = portfolio_state.get("last_prices", {}) or {}
    pos = {}
    for p in portfolio_state.get("positions", []) or []:
        if float(p.get("quantity", 0)) > 0:
            pos[p["ticker"]] = (float(p["quantity"]), float(p.get("avg_cost", 0) or 0))
    px = {t: _c(b)[-1] for t, b in ms.items() if _c(b)}
    for t, v in lp.items():
        px.setdefault(t, float(v))
    eq = float(portfolio_state.get("cash", cash)) + sum(q * px.get(t, a) for t, (q, a) in pos.items())
    if eq <= 0:
        return []
    tg = targets(ms, [t for t in pos if t not in EXCLUDE])
    # stop-loss on incumbents
    for t, (q, a) in pos.items():
        if a > 0 and px.get(t, a) < a * (1 - P["STOP"]):
            tg.pop(t, None)
    orders, proceeds = [], 0.0
    gross = sum(q * px.get(t, a) * BETA.get(t, 1.0) for t, (q, a) in pos.items()) / eq
    for t, (q, a) in pos.items():
        p = px.get(t)
        if not p:
            continue
        cur, tv = q * p, eq * tg.get(t, 0.0)
        hard = cur > eq * P["TRIM_AT"] or (gross > P["GROSS_TRIM"] and BETA.get(t, 1.0) > 1)
        if t not in tg:
            orders.append({"ticker": t, "side": "sell", "quantity": int(q)}); proceeds += q * p
        elif hard or cur - tv > eq * P["BAND"]:
            n = int((cur - tv) // p)
            if n > 0:
                orders.append({"ticker": t, "side": "sell", "quantity": n}); proceeds += n * p
    spend = float(cash) + proceeds * 0.995
    for t, w in sorted(tg.items(), key=lambda kv: -kv[1]):
        p = px.get(t)
        if not p:
            continue
        cur = pos.get(t, (0, 0))[0] * p
        d = eq * w - cur
        if d > eq * P["BAND"] or (t not in pos and d > eq * 0.01):
            n = int(min(d, spend) * 0.995 // p)
            if n > 0:
                orders.append({"ticker": t, "side": "buy", "quantity": n}); spend -= n * p
    return orders
