"""Download ~10 years of daily bars from Yahoo's public chart API for backtest.py.

    python fetch_history.py        # writes data_cache/bars.json (gitignored)

Standard library only. Covers every ETF agent.py can hold, the benchmarks,
and a point-in-time stock list (the ~40 largest US stocks at the end of 2016)
for checking stock-picking ideas without hindsight in the ticker list.
"""
from __future__ import annotations

import json
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

OUT = Path(__file__).parent / "data_cache" / "bars.json"

ETFS = [
    "SPY", "QQQ", "DIA", "IWM", "QLD", "SSO", "TQQQ", "UPRO", "SOXL",
    "SMH", "SOXX", "XLK", "XLC", "XLY", "XLF", "XLI", "XLE", "XLV", "XLP", "XLU", "XLRE", "XLB",
    "GLD", "SLV", "TLT", "IEF", "SHY", "BIL", "HYG", "LQD", "EFA", "EEM", "XBI", "KRE", "VNQ", "USO",
]
LARGEST_2016 = [
    "AAPL", "GOOGL", "MSFT", "BRK-B", "XOM", "AMZN", "META", "JNJ", "JPM", "GE", "WFC", "T", "BAC",
    "PG", "CVX", "VZ", "PFE", "KO", "HD", "CMCSA", "INTC", "MRK", "PEP", "ORCL", "DIS", "CSCO", "V",
    "UNH", "PM", "IBM", "C", "AMGN", "MO", "MMM", "MDT", "MA", "ABBV", "BA", "KHC", "HON",
]


def fetch(ticker: str) -> tuple[str, list | None]:
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?range=10y&interval=1d"
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            res = json.load(urllib.request.urlopen(req, timeout=30))["chart"]["result"][0]
            q = res["indicators"]["quote"][0]
            rows = []
            for i, ts in enumerate(res["timestamp"]):
                o, h, l, c, v = q["open"][i], q["high"][i], q["low"][i], q["close"][i], q["volume"][i]
                if None in (o, h, l, c) or c <= 0:
                    continue
                day = datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")
                rows.append([day, o, h, l, c, v or 0])
            return ticker, rows
        except Exception:  # noqa: BLE001
            time.sleep(1 + attempt)
    return ticker, None


def main() -> None:
    tickers = list(dict.fromkeys(ETFS + LARGEST_2016))
    with ThreadPoolExecutor(6) as ex:
        got = dict(ex.map(fetch, tickers))
    ok = {t: rows for t, rows in got.items() if rows}
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps(ok))
    print(f"fetched {len(ok)}/{len(tickers)} tickers -> {OUT}; missing: {[t for t in tickers if t not in ok]}")


if __name__ == "__main__":
    main()
