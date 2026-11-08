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


NM = os.path.join(ROOT, "data_store", "research", "nifty_monthly")


def _nifty_monthly_job():
    """2026-11-08 (Nikhil: monthly Supertrend short-strangle test on NIFTY, last 5 years): NIFTY daily from Dhan (2014 ->) plus the NSE F&O
    bhavcopy (NIFTY options + futures only) for the first and last trading day of every month from Nov 2021. Old bhavcopy format before
    8 Jul 2024, UDiFF after. Saved under data_store/research/nifty_monthly/."""
    import dhan_live as DL
    import updater as U
    os.makedirs(NM, exist_ok=True)
    S.update(running=True, kind="nifty_monthly", done=0, saved=0, missing=0, err=None, started=time.strftime("%H:%M:%S"))
    try:
        sid = DL.ids_index("nifty")
        bars = DL.daily("IDX_I", sid, "INDEX", date(2014, 1, 1), date.today())
        d = pd.DataFrame(bars); d["day"] = d.ts.map(lambda t: t.strftime("%Y-%m-%d"))
        d[["day", "open", "high", "low", "close"]].to_csv(os.path.join(NM, "nifty_daily.csv"), index=False)
        d = d[d.day >= "2021-11-01"]; d["m"] = d.day.str[:7]
        need = sorted(set(d.groupby("m").day.min()) | set(d.groupby("m").day.max()))
        S["total"] = len(need)
        for day in need:
            S["done"] += 1; fp = os.path.join(NM, f"bhav_{day}.csv.gz")
            if os.path.exists(fp):
                continue
            dt = date.fromisoformat(day)
            try:
                if dt >= date(2024, 7, 8):
                    df = U._unzip_csv(U.http_get(f"{U.NSE}/fo/BhavCopy_NSE_FO_0_0_0_{dt:%Y%m%d}_F_0000.csv.zip", U.NSE_HEADERS))
                    df = df[(df.TckrSymb == "NIFTY") & df.FinInstrmTp.isin(["IDO", "IDF"])]
                    out = pd.DataFrame({"day": day, "kind": df.FinInstrmTp.map({"IDO": "OPT", "IDF": "FUT"}).values, "expiry": df.XpryDt.astype(str).str[:10].values,
                                        "strike": df.StrkPric.values, "cp": df.OptnTp.values, "open": df.OpnPric.values, "high": df.HghPric.values, "low": df.LwPric.values,
                                        "close": df.ClsPric.values, "settle": df.SttlmPric.values if "SttlmPric" in df else np.nan, "vol": df.TtlTradgVol.values,
                                        "oi": df.OpnIntrst.values, "und": df.UndrlygPric.values})
                else:
                    mon = dt.strftime("%b").upper()
                    df = U._unzip_csv(U.http_get(f"{U.NSE}/historical/DERIVATIVES/{dt:%Y}/{mon}/fo{dt:%d}{mon}{dt:%Y}bhav.csv.zip", U.NSE_HEADERS))
                    df.columns = [c.strip() for c in df.columns]
                    df = df[(df.SYMBOL.str.strip() == "NIFTY") & df.INSTRUMENT.str.strip().isin(["OPTIDX", "FUTIDX"])]
                    out = pd.DataFrame({"day": day, "kind": df.INSTRUMENT.str.strip().map({"OPTIDX": "OPT", "FUTIDX": "FUT"}).values,
                                        "expiry": pd.to_datetime(df.EXPIRY_DT.str.strip(), format="%d-%b-%Y").dt.strftime("%Y-%m-%d").values,
                                        "strike": df.STRIKE_PR.values, "cp": df.OPTION_TYP.str.strip().values, "open": df.OPEN.values, "high": df.HIGH.values,
                                        "low": df.LOW.values, "close": df.CLOSE.values, "settle": df.SETTLE_PR.values, "vol": df.CONTRACTS.values,
                                        "oi": df.OPEN_INT.values, "und": np.nan})
                out.to_csv(fp, index=False, compression="gzip"); S["saved"] += 1
            except U.NotFound:
                S["missing"] += 1
            except Exception as ex:
                S["err"] = f"{day}: {type(ex).__name__} {str(ex)[:90]}"; time.sleep(2)
            time.sleep(0.5)
    except Exception as ex:
        S["err"] = f"{type(ex).__name__}: {str(ex)[:120]}"
    finally:
        S["running"] = False; S["ended"] = time.strftime("%H:%M:%S")


IVH = os.path.join(ROOT, "data_store", "research", "iv_hist")


def iv_day(df, day):
    """One UDiFF bhavcopy -> per underlying (stocks + indices): ATM IV of the nearest expiry with >= 5 days left and of the one after,
    the ATM straddle, and a simple skew (IV of the put ~5 % below spot minus the call ~5 % above). Black-Scholes on spot, r = 6.5 %."""
    import fnoflow as FF
    o = df[df.FinInstrmTp.isin(["STO", "IDO"])].copy(); o["XpryDt"] = o.XpryDt.astype(str).str[:10]
    d0 = date.fromisoformat(day); rows = []
    for sym, g in o.groupby("TckrSymb"):
        und = g.UndrlygPric.dropna()
        if not len(und):
            continue
        S0 = float(und.iloc[0]); exps = [e for e in sorted(g.XpryDt.unique()) if (date.fromisoformat(e) - d0).days >= 5][:2]
        r = {"day": day, "sym": sym, "und": S0}
        for tag, e in zip(("near", "next"), exps):
            x = g[g.XpryDt == e]; ce = x[x.OptnTp == "CE"].set_index("StrkPric").ClsPric; pe = x[x.OptnTp == "PE"].set_index("StrkPric").ClsPric
            ks = sorted(set(ce.index) & set(pe.index))
            if not ks:
                continue
            k = min(ks, key=lambda z: abs(z - S0)); T = ((date.fromisoformat(e) - d0).days + 0.25) / 365
            v1, v2 = FF._iv(float(ce[k]), S0, k, T, "CE"), FF._iv(float(pe[k]), S0, k, T, "PE")
            vs = [v for v in (v1, v2) if v]
            r[f"{tag}_exp"] = e; r[f"{tag}_iv"] = round(sum(vs) / len(vs) * 100, 2) if vs else None; r[f"{tag}_straddle"] = round(float(ce[k] + pe[k]), 2)
            if tag == "near":
                kp = min(ks, key=lambda z: abs(z - S0 * 0.95)); kc = min(ks, key=lambda z: abs(z - S0 * 1.05))
                ip, ic = FF._iv(float(pe[kp]), S0, kp, T, "PE"), FF._iv(float(ce[kc]), S0, kc, T, "CE")
                r["skew"] = round((ip - ic) * 100, 2) if ip and ic else None
        rows.append(r)
    return pd.DataFrame(rows)


def _iv_job():
    """2026-11-08 (Options Lab): IV history for every F&O name, one bhavcopy per session since 8 Jul 2024 -> iv_hist/iv_<day>.csv.gz."""
    import updater as U
    os.makedirs(IVH, exist_ok=True)
    days = sorted(f[4:14] for f in os.listdir(HIST) if f.startswith("fut_") and f.endswith(".csv.gz"))
    S.update(running=True, kind="iv_hist", total=len(days), done=0, saved=0, missing=0, err=None, started=time.strftime("%H:%M:%S"))
    try:
        for day in days:
            S["done"] += 1; fp = os.path.join(IVH, f"iv_{day}.csv.gz")
            if os.path.exists(fp):
                continue
            try:
                df = U._unzip_csv(U.http_get(f"{U.NSE}/fo/BhavCopy_NSE_FO_0_0_0_{day.replace('-', '')}_F_0000.csv.zip", U.NSE_HEADERS))
                iv_day(df, day).to_csv(fp, index=False, compression="gzip"); S["saved"] += 1
            except U.NotFound:
                S["missing"] += 1
            except Exception as ex:
                S["err"] = f"{day}: {type(ex).__name__} {str(ex)[:90]}"; time.sleep(2)
            time.sleep(0.3)
    except Exception as ex:
        S["err"] = f"{type(ex).__name__}: {str(ex)[:120]}"
    finally:
        S["running"] = False; S["ended"] = time.strftime("%H:%M:%S")


CH = os.path.join(ROOT, "data_store", "research", "chains")


def chains_day(df, day):
    """One UDiFF bhavcopy -> compact per-strike option rows for every F&O name: strikes within 12 % of the underlying, the nearest 2
    expiries (indices: nearest 4, so the monthly is in), plus the near futures close. OI and volume are in units (UDiFF)."""
    o = df[df.FinInstrmTp.isin(["STO", "IDO"])].copy(); o["XpryDt"] = o.XpryDt.astype(str).str[:10]
    o = o[(o.StrkPric / o.UndrlygPric - 1).abs() <= 0.12]
    n = o.groupby("TckrSymb").XpryDt.transform(lambda x: x.isin(sorted(x.unique())[:4] if x.name in ("NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY") else sorted(x.unique())[:2]))
    o = o[n]
    col = lambda c: o[c].values if c in o.columns else np.nan
    return pd.DataFrame({"day": day, "sym": o.TckrSymb.values, "expiry": o.XpryDt.values, "strike": o.StrkPric.values, "cp": o.OptnTp.values,
                         "open": col("OpnPric"), "high": col("HghPric"), "low": col("LwPric"), "close": col("ClsPric"), "settle": col("SttlmPric"),
                         "vol": col("TtlTradgVol"), "oi": col("OpnIntrst"), "und": col("UndrlygPric")})


def _chains_job():
    """2026-11-08 (Nikhil: "deep research and experimentation .. option, futures, MIS, spreads"): per-strike option history for every
    F&O name, one NSE bhavcopy per session since 8 Jul 2024 -> chains/ch_<day>.csv.gz. Also refreshes iv_hist when missing."""
    import updater as U
    os.makedirs(CH, exist_ok=True); os.makedirs(IVH, exist_ok=True)
    days = sorted(f[4:14] for f in os.listdir(HIST) if f.startswith("fut_") and f.endswith(".csv.gz"))
    S.update(running=True, kind="chains", total=len(days), done=0, saved=0, missing=0, err=None, started=time.strftime("%H:%M:%S"))
    try:
        for day in days:
            S["done"] += 1; fp = os.path.join(CH, f"ch_{day}.csv.gz")
            if os.path.exists(fp):
                continue
            try:
                df = U._unzip_csv(U.http_get(f"{U.NSE}/fo/BhavCopy_NSE_FO_0_0_0_{day.replace('-', '')}_F_0000.csv.zip", U.NSE_HEADERS))
                chains_day(df, day).to_csv(fp, index=False, compression="gzip"); S["saved"] += 1
                fi = os.path.join(IVH, f"iv_{day}.csv.gz")
                if not os.path.exists(fi):
                    iv_day(df, day).to_csv(fi, index=False, compression="gzip")
            except U.NotFound:
                S["missing"] += 1
            except Exception as ex:
                S["err"] = f"{day}: {type(ex).__name__} {str(ex)[:90]}"; time.sleep(2)
            time.sleep(0.3)
    except Exception as ex:
        S["err"] = f"{type(ex).__name__}: {str(ex)[:120]}"
    finally:
        S["running"] = False; S["ended"] = time.strftime("%H:%M:%S")


def api(q):
    if q.get("bures") == "chains" and not S.get("running"):
        threading.Thread(target=_chains_job, daemon=True, name="bures_ch").start()
        time.sleep(0.2)
    if q.get("bures") == "iv_hist" and not S.get("running"):
        threading.Thread(target=_iv_job, daemon=True, name="bures_iv").start()
        time.sleep(0.2)
    if q.get("bures") == "nifty_monthly" and not S.get("running"):
        threading.Thread(target=_nifty_monthly_job, daemon=True, name="bures_nm").start()
        time.sleep(0.2)
    if q.get("bures") == "opts" and not S.get("running"):
        threading.Thread(target=_opt_job, daemon=True, name="bures_opts").start()
        time.sleep(0.2)
    if q.get("bures") == "start" and not S.get("running"):
        threading.Thread(target=_job, daemon=True, name="bures").start()
        time.sleep(0.2)
    return S
