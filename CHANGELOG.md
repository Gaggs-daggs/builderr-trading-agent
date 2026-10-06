# Changelog

## Unreleased — Trendline + 3-day trend smoothing

**Kept**
- `agent.py`: the regime now uses the trend score averaged over the last 3 sessions
  (`SMOOTH_DAYS = 3`), recomputed from bars each call, so the agent stays stateless.
  The crash brake still uses raw data and reacts on the same day.
  - 10-year walk-forward: median 60-day window went from 3.81% to 4.64%. The new
    version beats the old in 58 of 112 windows, ties 21, loses 33, and averages
    +0.53 points per window. Turnover is about 42% lower and full-period CAGR went
    from 20.6% to 23.3%. Max drawdown went from 30.1% to 28.4%.
  - Holds up under ±20% perturbation of every other parameter: the median improved
    in 9 of 10 perturbed settings and turnover fell in all 10.

**Tried and rejected** (one change at a time against the Trendline baseline)
- Beta target 1.40x or 1.43x: the median gain was noise (+0.08 points), and 1.43x
  pushed peak gross to 1.47x.
- ON book with more capital and less 3x (adding SPY or dropping TQQQ): lower median.
- MID book at full capital: small gain, but the worst window got about 1 point worse.
- MID book with a QLD slice: small gain on its own (and -22% turnover), but nothing
  on top of smoothing.
- OFF book as cash, with TLT, or more defensive; HALF book cash-heavy: lower median,
  or the worst drawdown got worse.
- Thrust re-entry (QQQ 10-day return above 8% or 10% sets a MID or HALF floor):
  lower median.
- Lower MID threshold (0.3): higher median, but the worst window was 2.6 points
  worse and worst drawdown 1.9 points worse.
- Wider drift band and minimum trade size: no effect, because churn comes from
  regime flips.
- Volatility targeting at 25% or 30%: no effect on the median.
- Crash brake removed: worse drawdowns, so the brake stays.
- Relative-strength choice for the ON book's 4th slot (tech/semis or sectors):
  lower median, higher turnover.
- Smoothing over 2 or 4 days: better median, but the worst window was 1.4–2.1
  points worse. 4 days was consistently worse on worst windows.

**Tooling**
- `backtest.py` additions:
  - per-window Sharpe, Calmar, turnover, slippage cost, and average and peak gross;
  - the longest run at 30% or more in one position;
  - fixed stress windows (choppy 2018, Q4 2018 crash, 2020 spike and snapback,
    2022 bear, 2023 bull, 2025 crash and snapback, most recent 60 days);
  - a `cost_mult` option for doubled slippage.
- `strategy_selftest.py` adds tests for limit margins, trimming an over-levered book,
  regime step-down and step-up, smoothing against flip-flops, the error fallback, a
  multi-day rally and gap-down days (beta gross stays at or below 1.5x and no position
  reaches 30% for more than one day), and the order-count cap.
- `agent.py`: `MAX_ORDERS = 45` caps orders per call (sells first). It never binds in
  normal operation (the agent holds at most 5 names); it only matters if the account
  somehow holds a large stray book. The backtest results are identical with and
  without it.

## 2cd930b — Trendline
- Replaced Ridgeline with a trend-tiered ETF beta core. See the README.
