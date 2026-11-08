"""BUILDUP v1 - research data job (2026-11-08, Nikhil: "use more filters .. DOM, orderflow, orderblock detection etc and experiment").

One-off, server side (the research VM cannot reach Dhan). For every BuildUp signal in data_store/research/fno_short_buildup_trades.csv
(OI >= +10 %, futures <= -2 %, |move| <= 20 %), fetch the STOCK's 5-min cash bars from the session before the signal to ~8 sessions after,
one Dhan call per signal, ~0.5 s apart, and save data_store/research/buildup_intraday/<SYM>_<day>.csv.gz (ts, open, high, low, close, volume).
Cash bars, not futures: expired futures contracts are not in today's scrip master; the stock's NSE_EQ id never changes.
Trigger: /api/livedesk/fnoflow?bures=start   status: /api/livedesk/fnoflow?bures=status
"""
from __future__ import annotations

import os
import threading
import time
from datetime import date, timedelta

import pandas as pd

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SIG = os.path.join(ROOT, "data_store", "research", "fno_short_buildup_trades.csv")
OUT = os.path.join(ROOT, "data_store", "research", "buildup_intraday")
S: dict = {"running": False}


def _job():
    import dhan_live as DL
    os.makedirs(OUT, exist_ok=True)
    sig = pd.read_csv(SIG, usecols=["day", "sym"]).drop_duplicates()
    S.update(running=True, total=len(sig), done=0, saved=0, empty=0, failed=0, no_id=0, err=None, started=time.strftime("%H:%M:%S"))
    try:
        for _, r in sig.iterrows():
            fp = os.path.join(OUT, f"{r.sym}_{r.day}.csv.gz")
            S["done"] += 1
            if os.path.exists(fp) or os.path.exists(fp + ".none"):
                continue
            sid = DL.ids_equity(r.sym)
            if not sid:
                S["no_id"] += 1; open(fp + ".none", "w").close(); continue
            d = date.fromisoformat(r.day)
            try:
                bars = DL.intraday("NSE_EQ", sid, "EQUITY", d - timedelta(days=5), min(d + timedelta(days=12), date.today()), mins=5, oi=False)
            except Exception as ex:
                S["failed"] += 1; S["err"] = f"{r.sym} {r.day}: {str(ex)[:90]}"; time.sleep(2); continue
            time.sleep(0.5)
            if not bars:
                S["empty"] += 1; open(fp + ".none", "w").close(); continue
            df = pd.DataFrame(bars)
            df["ts"] = df.ts.map(lambda t: t.strftime("%Y-%m-%d %H:%M"))
            df[["ts", "open", "high", "low", "close", "volume"]].to_csv(fp, index=False, compression="gzip")
            S["saved"] += 1
    except Exception as ex:
        S["err"] = f"{type(ex).__name__}: {str(ex)[:120]}"
    finally:
        S["running"] = False; S["ended"] = time.strftime("%H:%M:%S")


def api(q):
    if q.get("bures") == "start" and not S.get("running"):
        threading.Thread(target=_job, daemon=True, name="bures").start()
        time.sleep(0.2)
    return S
