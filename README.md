# builderr trading agent — starter template

Submission template for the **builderr Trading Agent Leaderboard**.

> 🟢 **New to coding or trading? Start with [`START_HERE.md`](START_HERE.md)** — a plain-English, 5-minute walkthrough. No finance background, no money, no API key needed.

Fork this repo, implement `decide()` in `agent.py`, then send us the repo — **public or private, your call** (private repos use a read-only deploy key; see [«Submission»](#submission)). Submit at https://builderr.ai/trading-v0 (or just email the link to `submit@builderr.ai`). Full rules &amp; FAQ: https://builderr.ai/guidelines

---

> **Building with an AI assistant?** Paste **[`AGENT_BRIEF.md`](AGENT_BRIEF.md)** into Claude/ChatGPT/Cursor and describe your idea — it contains the full contract, rules, scoring, and traps, so your AI can write a compliant bot with you. Fastest cold start.

> **Don't know what to build?** Read **[`ANATOMY.md`](ANATOMY.md)** — the anatomy of a strong bot in plain English, with a complete recipe you can paste into your AI. No quant background needed.

## 30-second start

1. **Fork this repo** on GitHub.
2. **Implement `decide()`** in `agent.py` — or just rename `baseline.py` to `agent.py` for a 5-minute first submission that gets admitted. The full contract is in the docstring + the [&laquo;The contract&raquo;](#the-contract) section below. `baseline.py`, `example_sector_rotation.py`, and `ai_momentum.py` are real reference bots you can read, run, and beat.
3. **See it clear admission — locally, in ~10 seconds:** run **`python preview.py`**. No engine, no network, no keys, no install. It runs your bot across three real public market windows and prints the same shape of report the real admission email gives you, plus a PASS/FAIL on the safety bar admission actually gates on (clean run, leverage cap, concentration cap, no blow-up). If it says you clear the bar, you're very likely to be admitted.
4. **Push to a GitHub repo** — public, or private with a read-only deploy key (your call; [«Submission»](#submission) explains the trade-offs).
5. **Email the repo URL** to `submit@builderr.ai` (see [&laquo;Submission&raquo;](#submission)). We run admission and email you the score the same day (usually within a few hours). You can revise up to 4 times before the Round 2 cutoff — your first try is not your last.

> **`preview.py` vs `selfcheck.py`:** `preview.py` is the one to run — it shows you clearing admission with real numbers. `selfcheck.py` is an even-quicker, data-free smoke test (synthetic bars, just checks `decide()` returns well-formed orders and doesn't crash). Neither is the official eval — we run admission centrally on hidden regimes so it's identical for everyone — but a clean `preview.py` is a strong predictor of admission.
>
> **Want proof it&apos;s fair?** Read `fairness_tests.py` — the actual tests from our engine that guarantee *same code → same score* and *same order → same fill, regardless of who sent it*. (`local_test.py` / `full_test.py` are reference only; they need the private engine.)

> **Secrets:** never commit API keys. You do not need an LLM, brokerage login, or real-money account to enter. If you use an LLM, use endpoint mode or a capped throwaway key.

## Submitted agent: Trendline (trend-tiered beta core)

`agent.py` is a no-network, no-LLM, stateless ETF strategy built for the live forward-return ranking (Round 2: July 7 – October 31, 2026):

- **Trend score:** the fraction of SMA50/100/150/200 that QQQ and SPY both close above, averaged over the last 3 sessions. It needs 202 bars, so the tier is the same whether the engine passes ~220 bars (admission) or ~309 (live board).
- **Tiers:** ON (score ≥ 0.8 and QQQ 20-day vol < 28%) holds TQQQ/QLD/QQQ/SMH at ~1.35x beta. MID holds unlevered QQQ/SPY/XLK/SMH. HALF splits equity and defensives. OFF holds XLP/XLU/XLV. Every ticker is in the live board's 42-ticker feed.
- **Crash brake:** QQQ 10-day vol > 45% or 5-day return < −7% caps exposure at HALF.
- **Caps:** per-ticker targets ≤ 27% with a forced trim above 27.5% (keeps every close ≤ 28%); beta-adjusted gross targets 1.35x with a forced trim above 1.42x.

Why this and not stock/sector momentum: on a 10-year walk-forward test (`backtest.py`, 112 rolling 60-day windows, 2017–2026), momentum rotation over a ticker list with no hindsight (the largest US stocks at end-2016, or sector ETFs) made ~4–8% a year, trailing QQQ buy-and-hold (~20%). Re-run with the live engine's rules (dividend-adjusted bars, 309-bar history, 42-ticker feed, 2011–2026), Trendline made ~18% CAGR with a 28% max drawdown, versus QQQ's ~19% and 35%: roughly market-like return with a smaller drawdown, not an edge. Past results are not a forecast.

- `python strategy_selftest.py` — strategy-specific cap/regime checks.
- `python fetch_history.py && python backtest.py` — walk-forward test that mirrors `live_runner.py`'s fill rules (downloads public daily bars; not needed for submission).

---

## The contract

You implement one function:

```python
def decide(market_state, portfolio_state, cash) -> list[dict]:
    return [{"ticker": "SPY", "side": "buy", "quantity": 10}]
```

| Argument | Shape |
|---|---|
| `market_state` | `{ticker: [bar, bar, ...]}` — recent **daily** bars per ticker, oldest first (≈220 trading days, ~10 months, including a pre-regime warmup so even 200-day signals work from tick one). Each bar: `{ts, open, high, low, close, volume}`. |
| `portfolio_state` | `{cash, positions: [{ticker, quantity, avg_cost}], last_prices: {ticker: price}}` |
| `cash` | Convenience copy of `portfolio_state["cash"]`. |
| **return** | List of orders. Each: `{ticker, side: "buy"\|"sell", quantity: float}`. Empty list = no action. |

`decide()` is called once per decision interval (daily-resolution in admission and the live board).

---

## Constraints (auto-enforced)

| Rule | Limit | Breach action |
|---|---|---|
| Side | Long-only | Order rejected |
| Gross beta-adjusted exposure | ≤ 1.5x equity | Sustained breach > 60s → auto-flatten + DQ |
| Position concentration | < 30% per ticker for any 5 trading days | Sustained breach → auto-flatten + DQ |
| Trade rate | ≤ 50 trades/day | Excess rejected |
| Min hold | ≥ 60s | Excess rejected |
| Decide() runtime | ≤ 5s per call | Tick errors out (you keep going) |
| LLM use (optional) | **Bring your own API key** | Your AI spend is yours; keeps the contest about ideas, not API budget |

## Rules of engagement — external data & network

**Your agent has open network access.** Hit any external API: news feeds, alt-data vendors, social sentiment, your own server, an LLM. Real trading bots use external signals; we don't pretend otherwise.

**One absolute rule: no lookahead bias.** Admission runs in 2026 against historical regimes (2022–2024). At submission time, "live" APIs return present-day data, which for a 2023 backtest *is the future*. If your strategy queries data sources for the regime period at submission time and benefits from knowing what happened, you have lookahead bias.

How we catch it:
1. **Top admission submissions get a 10-min human code read.** Patterns like `requests.get("yahoo/SPY/2023-*")` inside the live backtest = DQ. Public postmortem on caught cases.
2. **Admission ↔ live correlation check.** If your admission Sharpe is 6 and your live Sharpe over a comparable horizon is -1, you get flagged for review. Lookahead cheaters leave that signature every time.
3. **Surprise fresh-regime reruns.** During the live round we can re-run qualified agents against new hidden 30-day windows that post-date any internet snapshot you could have queried. Inconsistency = lookahead suspicion.

If you're not sure whether your data source is OK: ask in GitHub Discussions before submitting. If your strategy is genuinely signal-driven (technicals, fundamentals available at the regime time, your own models), you're fine.

**Beta multiples** for the leverage cap:
- 3x: TQQQ, SOXL, UPRO, SPXL, TNA, FAS, TECL, LABU, CURE, DRN, UDOW, NAIL
- 2x: QLD, SSO, DDM, ROM, UWM, AGQ
- 1x: everything else (plain equities + non-leveraged ETFs)

So 100% TQQQ = 3x exposure = instant breach. Max 50% TQQQ + 50% cash works (1.5x exactly).

---

## Universe

The **top ~1000 US names by liquidity** — basically every stock and ETF people actually trade. It's ranked by trailing dollar-volume (from the S&P 500 + Nasdaq-100 + S&P 400/600 + popular ETFs) and **frozen at round open** so the tradeable set is identical for everyone and stable for the whole round. Anything outside it is silently ignored.

The exact frozen list lives in [`universe.json`](universe.json) (the board and the admission engine both read it). It includes all the obvious names — AAPL MSFT NVDA AMZN META GOOGL TSLA AMD AVGO MU MRVL QCOM PLTR COIN JPM V MA UNH LLY XOM … — plus broad/sector/thematic ETFs (SPY QQQ IWM, XLK…XLB, SMH GLD TLT …) and the long leveraged sleeves (3x: TQQQ SOXL UPRO SPXL · 2x: QLD SSO, which count 3x/2x toward the 1.5x gross cap). Regenerate with `python build_universe.py`.

---

## Scoring

We don't gate on whether we like your strategy. Three stages:

### Stage 1 — Admission (immediate, runs on submission)

We run your agent across 3 hidden 30-day historical regimes (shapes only — dates hidden):
1. Fast sector-contagion crash with broader-market spillover
2. Slow trend-down regime change from rate-hike repricing
3. Vol spike + rapid snapback from leveraged-position unwind

**Admission is a smoke screen, NOT a skill gate. You're admitted if:**
- No execution-constraint breach (leverage / concentration)
- No catastrophic blow-up (>50% drawdown in any regime)
- Runs without fatal error

That's it. A fair-weather strategy that's soft in a crash is *admitted* — skill is decided forward, not here. You also get a free **robustness profile** (your Sharpe / drawdown / return across the 3 regimes) so you and we can see whether you're all-weather or fair-weather.

### Stage 2 — Live forward test — the ranking

**Round 2 runs July 7 – October 31, 2026. Submissions close October 15.** Admitted agents run live on the shared paper sandbox over the window. Same fills for everyone. Daily leaderboard. **Ranked by forward return**, with Arnav (the Round 1 winner) as the benchmark to beat for prize money and builder points. Submissions are open now — the earlier you're admitted, the more of the window your bot trades.

### Stage 3 — Held-out rerun — the anti-luck check

Top finishers are re-run on **fresh windows (calm + stress) they've never seen**. Luck doesn't replicate; skill does. This confirms the winner isn't just the luckiest of the field.

**Prize:** Top 3 entrants who beat Arnav split **$1,000** ($600 / $250 / $150). Qualified entries can also earn builder points from the 100-point challenge pool (30 / 20 / 15 / 10 / 8 / 6 / 4 / 3 / 2 / 2 for the top 10 qualifiers). Winner's code runs on a real **$100k Nasdaq book** post-challenge, with weekly P&L posted publicly on a live ticker from week one — *"win and your code trades my real money."*

---

## Submission

You don't have to make your code public. Pick the path you're comfortable with — same competition, same scoring, regardless. All three: email the link to **submit@builderr.ai** (subject: `builderr submission — <your name>`); we run admission and email your robustness profile the same day (usually within a few hours); if admitted you're in the live round (Round 2: July 7 – October 31). You can revise up to 4 times before the Round 2 cutoff.

**1. Public repo** *(simplest)*
Push to a public GitHub repo, email the URL. Zero access setup and you get a public proof-of-work piece — but the field can read your strategy while the contest runs, and a public repo is the easiest place to leak a key. Good if you don't mind being open (or you'll open it after the contest anyway).

**2. Private repo, read-only access** *(protects your edge)*
Keep the repo private. Email us first; we reply with a **read-only deploy key** (one line). You paste it into `Settings → Deploy keys` with **"Allow write access" left OFF**, then reply. We clone, you delete the key after.
- We get **read access to that one repo and nothing else** — we *cannot push to it*, can't see your other repos, and access dies when you remove the key.
- Why not "add us as a collaborator"? On a personal GitHub repo a collaborator gets **write** access. We don't want that and you shouldn't grant it. A deploy key is read-only and scoped to the single repo.

**3. Endpoint mode** *(private code — forward scoring only)*
Host an HTTPS endpoint that accepts `POST /decide` with `{market_state, portfolio_state, cash}` and returns `{orders: [...]}`. We send data; you return orders. Your code, prompts, and API keys never leave your server.

Endpoint decisions are captured **before the target market session**, hashed, and replayed from that immutable response at the opening fill. We never backfill a live endpoint over market sessions that have already happened. A missed, late, unavailable, redirected, non-HTTPS, private-network, oversized, or invalid response produces no new decision for that session. The scoring engine independently enforces the ticker universe, finite positive quantities, cash, 30% single-name cap, and 1.5x beta-gross cap. Include the endpoint URL in your email.

> Whichever you pick: we only ever read and run your code to score it. We don't reuse your strategy, and you keep the IP (this template is MIT; your repo stays yours).

Or email **inquiries@builderr.ai** for early access / questions.

---

## Alpaca paper trading (optional, local only)

`alpaca_runner.py` runs `agent.py` unchanged against an **Alpaca paper** account, once per trading day shortly
before the close (default 15:45 ET). It is separate from the builderr submission: nothing here is sent to builderr.

**Setup**

```bash
python3 -m venv .venv && .venv/bin/pip install alpaca-py      # the runner needs alpaca-py; agent.py does not
export ALPACA_API_KEY=...        # paper-trading key id   (environment variables only, never a file in the repo)
export ALPACA_SECRET_KEY=...     # paper-trading secret
```

Keep the keys in a `.env` you load yourself (`set -a; . ./.env; set +a`); `.env`, `logs/` and `runs.csv` are git-ignored.
The runner reads the keys from the environment only, hard-codes `paper=True`, refuses to start unless the trading
endpoint is `paper-api.alpaca.markets`, and redacts the keys from every log line.

**Run**

```bash
.venv/bin/python alpaca_runner.py --allow-closed     # dry run (the default): prints the orders, sends nothing; works any time
.venv/bin/python alpaca_runner.py --now              # dry run during market hours, outside the 15:45 window
.venv/bin/python alpaca_runner.py --live-paper       # submits market orders to the PAPER account (in the 15:45-15:55 ET window)
```

What a run does: fetch 42 tickers of adjusted daily bars (the same feed and up to 309 bars per ticker that
`live_runner.py` gives `decide()`), positions and cash, call `decide()`, convert orders to whole-share market orders
(sells first, at most 45), then check the **intended post-trade book** and abort the whole run if beta-adjusted gross
would exceed 1.45x, any position 28%, or the orders would use margin. Sells are waited on before buys are sent, and
buys are re-sized to the cash actually left. It **skips** (sends nothing) when the market is closed, outside the run
window, data is stale or missing, `decide()` fails, orders are already open, the account is blocked, or it already
submitted today. Every run, including dry runs and skips, appends a row to `runs.csv`.

The paper account should be dedicated to this bot. Positions the bot doesn't hold by design abort the run; pass
`--liquidate-foreign` to let it sell them as stray positions (they must be in the 42-ticker feed).

**Daily scheduling.** The runner only acts inside its window and only once per day, so over-scheduling is safe.
- Linux (cronie): `CRON_TZ=America/New_York`, then `45,50 15 * * 1-5 cd /path/to/repo && set -a && . ./.env && set +a && .venv/bin/python alpaca_runner.py --live-paper >> logs/cron.log 2>&1`
- macOS: a launchd job with two `StartCalendarInterval` entries at 15:45 and 15:50 ET converted to your local time (launchd has no time-zone setting), running the same command through `/bin/zsh -lc`.

**How it differs from the scoring engine**: the engine decides on the prior close and fills at the next open; the
runner decides on bars that include today's partial bar at ~15:45 and trades at ~15:45 (about the close). The free
Alpaca feed (`--feed iex`) is IEX-only, not the consolidated tape yfinance uses, so prices differ slightly; `--feed sip`
needs a paid plan. It trades once a day: this is a daily-bar strategy, not an intraday one. Run
`python test_alpaca_runner.py` for the mocked-client tests (adapter tests run if alpaca-py is installed).

## Examples

- `baseline.py` — equal-weight buy-and-hold SPY+QQQ
- More coming as community shares strategies post-launch

---

## Questions

Open a GitHub Discussion on this repo, or email **inquiries@builderr.ai**.
