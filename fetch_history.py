"""Download daily bars (2010 onward) from Yahoo's public chart API for backtest.py.

    python fetch_history.py        # writes data_cache/bars.json (gitignored)

Standard library only. Bars are adjusted the same way the live engine's
yfinance download is (auto_adjust=True): open/high/low/close are all scaled by
adjclose/close, so dividends and splits are in the price series. A bar for a
session that has not closed yet (an intraday partial bar) is dropped.

Covers the 42 tickers the live leaderboard actually fetches (LIVE_FEED, see
live_runner.py), other ETFs used for research, and a point-in-time stock list
(the ~40 largest US stocks at the end of 2016) for checking stock-picking
ideas without hindsight in the ticker list.
"""
from __future__ import annotations

import json
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

OUT = Path(__file__).parent / "data_cache" / "bars.json"
START = datetime(2010, 1, 1, tzinfo=timezone.utc)
NY = ZoneInfo("America/New_York")

# live_runner.py ROUND2_FETCH_UNIVERSE: the only tickers the live board feeds to
# decide() and can fill. Anything else is silently dropped in the live round.
LIVE_FEED = [
    "SPY", "QQQ",
    "NVDA", "MSFT", "AAPL", "META", "AMZN", "GOOGL", "AVGO", "AMD", "MU", "MRVL",
    "NFLX", "TSLA", "PLTR", "ORCL", "CRM", "JPM", "V", "MA", "COST", "LLY",
    "SMH", "XLK", "XLC", "XLY", "XLF", "XLI", "XLE", "XLV", "XLP", "XLU",
    "XLRE", "DIA", "IWM", "SOXX", "QLD", "SSO", "TQQQ", "SOXL", "UPRO", "SPXL",
]
ETFS = [
    "XLB", "GLD", "SLV", "TLT", "IEF", "SHY", "BIL", "HYG", "LQD", "EFA", "EEM", "XBI", "KRE", "VNQ", "USO",
]
LARGEST_2016 = [
    "AAPL", "GOOGL", "MSFT", "BRK-B", "XOM", "AMZN", "META", "JNJ", "JPM", "GE", "WFC", "T", "BAC",
    "PG", "CVX", "VZ", "PFE", "KO", "HD", "CMCSA", "INTC", "MRK", "PEP", "ORCL", "DIS", "CSCO", "V",
    "UNH", "PM", "IBM", "C", "AMGN", "MO", "MMM", "MDT", "MA", "ABBV", "BA", "KHC", "HON",
]


def _session_closed(day: str) -> bool:
    """True once the NY regular session for `day` has ended (with a small buffer)."""
    now = datetime.now(NY)
    today = now.strftime("%Y-%m-%d")
    return day < today or (day == today and (now.hour, now.minute) >= (16, 30))


def fetch(ticker: str) -> tuple[str, list | None]:
    period2 = int(time.time())
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?period1={int(START.timestamp())}"
           f"&period2={period2}&interval=1d&events=div,splits&includeAdjustedClose=true")
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            res = json.load(urllib.request.urlopen(req, timeout=30))["chart"]["result"][0]
            q = res["indicators"]["quote"][0]
            adj = (res["indicators"].get("adjclose") or [{}])[0].get("adjclose")
            rows = []
            for i, ts in enumerate(res["timestamp"]):
                o, h, l, c, v = q["open"][i], q["high"][i], q["low"][i], q["close"][i], q["volume"][i]
                a = adj[i] if adj else c
                if None in (o, h, l, c, a) or c <= 0 or a <= 0:
                    continue
                day = datetime.fromtimestamp(ts, NY).strftime("%Y-%m-%d")
                if not _session_closed(day):
                    continue
                k = a / c
                rows.append([day, o * k, h * k, l * k, a, v or 0])
            return ticker, rows
        except Exception:  # noqa: BLE001
            time.sleep(1 + attempt)
    return ticker, None


def main() -> None:
    tickers = list(dict.fromkeys(LIVE_FEED + ETFS + LARGEST_2016))
    with ThreadPoolExecutor(6) as ex:
        got = dict(ex.map(fetch, tickers))
    ok = {t: rows for t, rows in got.items() if rows}
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps(ok))
    last = max(r[-1][0] for r in ok.values())
    print(f"fetched {len(ok)}/{len(tickers)} tickers through {last} -> {OUT}; "
          f"missing: {[t for t in tickers if t not in ok]}")


if __name__ == "__main__":
    main()
