"""One-time MCX futures intraday history download (2026-11-05, Nikhil: "Yes" to the MCX history download). Research data only - never orders.
1-minute OHLCV + OI for Gold Mini, Silver Mini, Crude Mini, NatGas Mini, Copper, Aluminium and Zinc futures - the nearest 3 live
contracts of each (Dhan has no data for expired contracts) - about the last 100 days, through the Mac's existing Dhan login
(app/dhan_live.intraday, 85-day chunks). Contracts from the Dhan scrip master (~/Documents/dhan/orderflow/data/scrip_master.csv).
Saved to data_store/research/mcx_intraday/<UNDERLYING>_<YYYY-MM-DD expiry>_1m.csv.gz; progress in _status.json.
Started from /api/livedesk/run_tool?name=fetch_mcx_hist (background thread, ~0.6 s between requests)."""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import date, datetime, timedelta

import pandas as pd

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
OUT = os.path.join(ROOT, "data_store", "research", "mcx_intraday")
SCRIP = os.path.join(os.environ.get("MP_DHAN_DIR") or os.path.join(os.path.expanduser("~"), "Documents", "dhan"), "orderflow", "data", "scrip_master.csv")
UNDER = ["GOLDM", "SILVERM", "CRUDEOILM", "NATGASMINI", "COPPER", "ALUMINIUM", "ZINC"]
NEAR = 3
S = {"running": False}


def contracts(near: int = NEAR) -> list[dict]:
    """Nearest `near` live FUTCOM contracts per underlying (expiry today or later)."""
    m = pd.read_csv(SCRIP, usecols=["SEM_EXM_EXCH_ID", "SEM_SMST_SECURITY_ID", "SEM_INSTRUMENT_NAME", "SEM_TRADING_SYMBOL",
                                    "SEM_EXPIRY_DATE", "SM_SYMBOL_NAME"], dtype=str)
    m = m[(m.SEM_EXM_EXCH_ID == "MCX") & (m.SEM_INSTRUMENT_NAME == "FUTCOM") & (m.SM_SYMBOL_NAME.isin(UNDER))].copy()
    m["exp"] = m.SEM_EXPIRY_DATE.str[:10]
    m = m[m.exp >= date.today().isoformat()].sort_values(["SM_SYMBOL_NAME", "exp"])
    out = []
    for u, g in m.groupby("SM_SYMBOL_NAME"):
        for _, r in g.head(near).iterrows():
            out.append({"under": u, "sid": r.SEM_SMST_SECURITY_ID, "symbol": r.SEM_TRADING_SYMBOL, "expiry": r.exp})
    return out


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
    try:
        C = contracts()
    except Exception as e:
        S["running"] = False
        return _status(state="failed", error=f"scrip master: {str(e)[:120]}")
    done, failed, rows = [], {}, {}
    _status(state="running", total=len(C), done=0, failed={}, contracts=[c["symbol"] for c in C])
    for c in C:
        try:
            b = DL.intraday("MCX_COMM", c["sid"], "FUTCOM", date.today() - timedelta(days=days), date.today(), 1, oi=True)
            if not b:
                raise ValueError("no bars")
            d = pd.DataFrame(b)
            d["ts"] = pd.to_datetime(d.ts.astype(str).str[:19])
            first = d.groupby(d.ts.dt.date).ts.transform("min")            # dhan_live guesses UTC vs IST by NSE hours; MCX runs to
            bad = first.dt.hour < 9                                          # 23:30, so a day that starts before 09:00 IST is UTC -> +5:30
            d.loc[bad, "ts"] = d.loc[bad, "ts"] + pd.Timedelta(hours=5, minutes=30)
            cols = [k for k in ("ts", "open", "high", "low", "close", "volume", "oi") if k in d.columns]
            d[cols].to_csv(os.path.join(OUT, f"{c['under']}_{c['expiry']}_1m.csv.gz"), index=False)
            done.append(c["symbol"]); rows[c["symbol"]] = len(d)
        except Exception as e:
            failed[c["symbol"]] = str(e)[:120]
        _status(state="running", done=len(done), failed=failed, rows=rows, last=c["symbol"])
        time.sleep(0.6)
    S["running"] = False
    return _status(state="finished", done=len(done), failed=failed, rows=rows)


def start(days: int = 100) -> dict:
    if S["running"]:
        return {"ok": False, "detail": "already running", **_status()}
    S["running"] = True
    threading.Thread(target=_run, args=(days,), name="mcxhist", daemon=True).start()
    return {"ok": True, "detail": f"downloading ~{days} days of MCX 1-min bars in the background"}
