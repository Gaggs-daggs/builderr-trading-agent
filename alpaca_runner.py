"""Run agent.decide() against an Alpaca PAPER account (paper-api only; refuses anything else).

    APCA_API_KEY_ID=... APCA_API_SECRET_KEY=... python alpaca_runner.py            # dry run, prints orders
    ... python alpaca_runner.py --reset                                            # flatten: cancel orders + close all positions
    ... python alpaca_runner.py --submit                                           # send the bot's orders
    ... python alpaca_runner.py --reset --submit                                   # start fresh, then trade

Bars come from yfinance (same source the builderr scorer uses), only COMPLETED sessions are passed to the bot.
Before the open orders go in as market-on-open (fills at the open, like the competition); during the session
they go in as day-market orders. Credentials are read from the environment only.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

BASE = "https://paper-api.alpaca.markets/v2"
assert "paper-api" in BASE, "paper trading only"
HERE = Path(__file__).parent
NY = ZoneInfo("America/New_York")
FETCH = ["SPY", "QQQ", "NVDA", "MSFT", "AAPL", "META", "AMZN", "GOOGL", "AVGO", "AMD", "MU", "MRVL", "NFLX", "TSLA",
         "PLTR", "ORCL", "CRM", "JPM", "V", "MA", "COST", "LLY", "SMH", "XLK", "XLC", "XLY", "XLF", "XLI", "XLE",
         "XLV", "XLP", "XLU", "XLRE", "DIA", "IWM", "SOXX", "QLD", "SSO", "TQQQ", "SOXL", "UPRO", "SPXL"]


def api(method: str, path: str, body: dict | None = None):
    req = urllib.request.Request(
        BASE + path, method=method, data=json.dumps(body).encode() if body is not None else None,
        headers={"APCA-API-KEY-ID": os.environ["APCA_API_KEY_ID"], "APCA-API-SECRET-KEY": os.environ["APCA_API_SECRET_KEY"],
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            raw = r.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        return {"_error": e.code, "_body": e.read().decode()[:300]}


def fetch_bars(clock: dict) -> dict:
    import yfinance as yf
    today = datetime.now(NY).strftime("%Y-%m-%d")
    session_done = (not clock["is_open"]) and datetime.now(NY).hour >= 16
    bars = {}
    for t in FETCH:
        try:
            df = yf.Ticker(t).history(period="2y", interval="1d", auto_adjust=True).dropna()
        except Exception as e:  # noqa: BLE001
            print("  skip", t, e)
            continue
        rows = [{"ts": i.strftime("%Y-%m-%d"), "open": float(r.Open), "high": float(r.High), "low": float(r.Low),
                 "close": float(r.Close), "volume": float(r.Volume)} for i, r in df.iterrows()]
        if rows and rows[-1]["ts"] == today and not session_done:
            rows = rows[:-1]            # never show the bot an unfinished session
        if rows:
            bars[t] = rows[-260:]
    return bars


def main() -> int:
    do_reset, do_submit = "--reset" in sys.argv, "--submit" in sys.argv
    acct = api("GET", "/account")
    if "_error" in acct:
        print("auth failed:", acct)
        return 1
    clock = api("GET", "/clock")
    print(f"account {acct['account_number']}  equity ${float(acct['equity']):,.2f}  cash ${float(acct['cash']):,.2f}  market_open={clock['is_open']}")

    if do_reset:
        if do_submit:
            print("reset: cancelling open orders and closing all positions")
            print("  ", api("DELETE", "/positions?cancel_orders=true"))
        else:
            print("reset (dry run): would cancel orders + close all positions")
        positions, cash = [], float(acct["equity"])         # bot sees a flat book with the full equity
    else:
        positions = [{"ticker": p["symbol"], "quantity": float(p["qty"]), "avg_cost": float(p["avg_entry_price"])}
                     for p in api("GET", "/positions")]
        cash = float(acct["cash"])

    bars = fetch_bars(clock)
    print(f"bars for {len(bars)} tickers, last session {bars['SPY'][-1]['ts']}")
    spec = importlib.util.spec_from_file_location("agent", HERE / "agent.py")
    agent = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(agent)
    state = {"cash": cash, "positions": positions, "last_prices": {t: b[-1]["close"] for t, b in bars.items()}}
    orders = agent.decide(bars, state, cash)[:50]
    if not orders:
        print("bot: no orders")
        return 0
    tif = "day" if clock["is_open"] else "opg"
    print(f"bot orders ({tif}):")
    for o in sorted(orders, key=lambda o: o["side"] != "sell"):
        print(f"  {o['side']:4s} {int(o['quantity']):5d} {o['ticker']}")
        if do_submit:
            r = api("POST", "/orders", {"symbol": o["ticker"], "qty": str(int(o["quantity"])), "side": o["side"],
                                        "type": "market", "time_in_force": tif})
            print("      ->", r.get("status") or r)
    if not do_submit:
        print("(dry run — add --submit to send)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
