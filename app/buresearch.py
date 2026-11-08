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

import numpy as np
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


OPT = os.path.join(ROOT, "data_store", "research", "buildup_opts")
HIST = os.path.join(ROOT, "data_store", "research", "fno_hist")


def _opt_job():
    """2026-11-08 (Nikhil: "we try call selling just for experimentation"): per-strike STOCK option prices for every session that a BuildUp
    signal could still be open on (signal day .. +12 calendar days), only for those names, the 2 nearest expiries, strikes within 20 % of
    the underlying. NSE F&O UDiFF bhavcopy, one download per session, saved as buildup_opts/opt_<day>.csv.gz."""
    import updater as U
    os.makedirs(OPT, exist_ok=True)
    sig = pd.read_csv(SIG, usecols=["day", "sym"]); sig["d"] = pd.to_datetime(sig.day)
    days = sorted(f[4:14] for f in os.listdir(HIST) if f.startswith("fut_") and f.endswith(".csv.gz"))
    S.update(running=True, kind="options", total=len(days), done=0, saved=0, skipped=0, missing=0, err=None, started=time.strftime("%H:%M:%S"))
    try:
        for day in days:
            S["done"] += 1; fp = os.path.join(OPT, f"opt_{day}.csv.gz")
            dd = pd.Timestamp(day); syms = set(sig[(sig.d <= dd) & (sig.d >= dd - pd.Timedelta(days=12))].sym)
            if not syms or os.path.exists(fp):
                S["skipped"] += 1; continue
            try:
                raw = U.http_get(f"{U.NSE}/fo/BhavCopy_NSE_FO_0_0_0_{day.replace('-', '')}_F_0000.csv.zip", U.NSE_HEADERS)
                df = U._unzip_csv(raw)
            except U.NotFound:
                S["missing"] += 1; continue
            except Exception as ex:
                S["err"] = f"{day}: {str(ex)[:90]}"; time.sleep(3); continue
            o = df[(df.FinInstrmTp == "STO") & df.TckrSymb.isin(syms)].copy()
            o["XpryDt"] = o.XpryDt.astype(str).str[:10]
            ex2 = o.groupby("TckrSymb").XpryDt.transform(lambda x: x.isin(sorted(x.unique())[:2]))
            o = o[ex2 & ((o.StrkPric / o.UndrlygPric - 1).abs() <= 0.20)]
            col = lambda c: o[c].values if c in o.columns else np.nan
            pd.DataFrame({"day": day, "sym": o.TckrSymb.values, "expiry": o.XpryDt.values, "strike": o.StrkPric.values, "cp": o.OptnTp.values,
                          "open": col("OpnPric"), "high": col("HghPric"), "low": col("LwPric"), "close": col("ClsPric"), "settle": col("SttlmPric"),
                          "vol": col("TtlTradgVol"), "oi": col("OpnIntrst"), "und": col("UndrlygPric")}).to_csv(fp, index=False, compression="gzip")
            S["saved"] += 1; time.sleep(0.4)
    except Exception as ex:
        S["err"] = f"{type(ex).__name__}: {str(ex)[:120]}"
    finally:
        S["running"] = False; S["ended"] = time.strftime("%H:%M:%S")


def api(q):
    if q.get("bures") == "opts" and not S.get("running"):
        threading.Thread(target=_opt_job, daemon=True, name="bures_opts").start()
        time.sleep(0.2)
    if q.get("bures") == "start" and not S.get("running"):
        threading.Thread(target=_job, daemon=True, name="bures").start()
        time.sleep(0.2)
    return S
