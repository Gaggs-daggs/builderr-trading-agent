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

## forensics-v1 — post-mortem: bug fixes only, no performance changes kept

**Bug fixes** (kept on correctness grounds, whatever the backtest says)
- B1 `agent.py`: the trend lookbacks are now SMA50/100/150/200, and the vote
  denominator is fixed so a missing vote counts as bearish. Before, the SMA250
  vote only existed when the engine passed 252 or more bars. The live board
  passes ~309 and admission ~220, so the two ran different strategies: the tier
  differed on 5.6% of days, and the live variant had never been backtested.
- B2 `agent.py`: GLD is removed from the HALF and OFF books. The live board feeds
  only 42 tickers (`live_runner.ROUND2_FETCH_UNIVERSE`) and GLD isn't one of them,
  so those slices were silently held as cash. Live behaviour is unchanged.
- B3 `agent.py`: the forced trim fires above 27.5% (it was 29%), and a
  single-name trim no longer forces 1-share trims of every other name. QQQ had
  closed at 28.2–28.3% for 5 sessions in Sep 2024, over the 28% target.
- Effect of B1–B3 (2011–2026, engine-faithful): CAGR 18.2% vs 17.4%, max
  drawdown 28.3% vs 29.0%, peak position 27.7% vs 28.3%, better in every named
  stress window. Before 2023 it's a wash (-0.05 points per 19-session window);
  from 2023 the mean is +0.24.

**Simulator fidelity** (`backtest.py`, `fetch_history.py`), matched to `live_runner.py`
- dividend-adjusted OHLC, as yfinance `auto_adjust=True` gives;
- a 309-bar history window;
- only the 42 fed tickers visible and fillable;
- sells before buys, at most 100 orders per decision;
- each buy capped by cash, 30% room and 1.5x beta room at the open;
- data from 2010, partial intraday bars dropped.
- The 309-bar window alone moved Trendline's 2017–2026 CAGR from 23.3% to 20.7%.
  Results from the old 220-bar simulator (including the 3-day smoothing
  validation) didn't describe the live configuration.

**Performance fixes tried** (selected on data through 2022; 54 candidate runs, 6 fixes)
- A1, hysteresis band 0.125 with the tier read from holdings: passed selection
  (+0.05 points per window, bootstrap 82%, helped in 19 of 21 ±20% settings).
  Rejected at the one-time 2023+ confirmation: the 2025 crash-and-snapback window
  was 2.05 points worse, bootstrap 73%, and it is 1.3 points worse in the 2020
  crash trough. The gain is far below noise for that tail cost.
- A2, fast exit with a 5-day entry confirmation: median -0.40. Rejected.
- C, drawdown cap (QQQ 6% below its 20-day high caps the tier at MID): about 0.
  Rejected.
- M, no new ON entry when QQQ is more than 8% above its SMA50: about 0. Rejected.
- P1, TQQQ ratchet (a 30% run-up then a 15% pullback caps the tier at MID):
  +0.01 points per window. Immaterial, rejected.
- P2, profit trim at 1.38x gross: about 0, bootstrap 12%. Rejected.

**Tests**: `strategy_selftest.py` (22 checks) tightened to ≤1.45x and ≤28%, and adds:
- a ±15% TQQQ shock day;
- the Ridgeline-to-Trendline revision transition under the engine's fill rules;
- history-length invariance (220 vs 309 bars);
- every target ticker is in the live feed;
- a trim before 28%.
