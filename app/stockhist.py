"""One-time stock intraday history download (2026-11-05, Nikhil: "Yes download"). Research data only - never orders.
1-minute OHLCV for the 48 F&O stocks on the order-flow engine's book feed, ~120 days, through the Mac's existing Dhan login
(app/dhan_live.intraday, 85-day chunks). Saved to data_store/research/stocks_intraday/<SYM>_1m.csv.gz; progress in _status.json.
Started from /api/livedesk/run_tool?name=fetch_stock_hist (runs in a background thread, ~0.5 s between requests)."""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import date, datetime, timedelta

import pandas as pd

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
OUT = os.path.join(ROOT, "data_store", "research", "stocks_intraday")
STOCKS = ["RELIANCE", "HDFCBANK", "ICICIBANK", "INFY", "TCS", "SBIN", "AXISBANK", "KOTAKBANK", "LT", "BHARTIARTL", "ITC",
          "BAJFINANCE", "M&M", "MARUTI", "TMPV", "HINDUNILVR", "SUNPHARMA", "TITAN", "ULTRACEMCO", "ADANIENT", "ADANIPORTS",
          "NTPC", "POWERGRID", "ONGC", "COALINDIA", "TATASTEEL", "JSWSTEEL", "HINDALCO", "BAJAJFINSV", "BAJAJ-AUTO",
          "EICHERMOT", "HCLTECH", "WIPRO", "TECHM", "ASIANPAINT", "DRREDDY", "CIPLA", "APOLLOHOSP", "INDUSINDBK", "BEL",
          "HAL", "TRENT", "SHRIRAMFIN", "VEDL", "DLF", "BANKBARODA", "TATAPOWER", "ETERNAL"]
S = {"running": False}


def _status(**kw):
    os.makedirs(OUT, exist_ok=True)
    st = {}
    try:
        st = json.load(open(os.path.join(OUT, "_status.json")))
    except Exception:
        pass
    st.update(kw, at=datetime.now().isoformat(timespec="seconds"))
    json.dump(st, open(os.path.join(OUT, "_status.json"), "w"), indent=1, default=str)
    return st


def _run(days):
    import dhan_live as DL
    done, failed = [], {}
    _status(state="running", total=len(STOCKS), done=0, failed={})
    for sym in STOCKS:
        try:
            sid = DL.ids_equity(sym)
            if not sid:
                raise ValueError("no security id")
            b = DL.intraday("NSE_EQ", sid, "EQUITY", date.today() - timedelta(days=days), date.today(), 1, oi=False)
            if not b:
                raise ValueError("no bars")
            d = pd.DataFrame(b)
            d["ts"] = pd.to_datetime(d.ts.astype(str).str[:19])
            d[["ts", "open", "high", "low", "close", "volume"]].to_csv(os.path.join(OUT, f"{sym}_1m.csv.gz"), index=False)
            done.append(sym)
        except Exception as e:
            failed[sym] = str(e)[:120]
        _status(state="running", done=len(done), failed=failed, last=sym)
        time.sleep(0.6)
    S["running"] = False
    return _status(state="finished", done=len(done), failed=failed)


def start(days: int = 120) -> dict:
    if S["running"]:
        return {"ok": False, "detail": "already running", **_status()}
    S["running"] = True
    threading.Thread(target=_run, args=(days,), name="stockhist", daemon=True).start()
    return {"ok": True, "detail": f"downloading ~{days} days of 1-min bars for {len(STOCKS)} stocks in the background"}
