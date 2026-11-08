"""RABBIT LIQUIDITY SPIKE v1.1.1 ON US FUTURES (2026-11-08, Nikhil: "can we test rabbit_liquidity_spike_v1_1_1 on US futures?").

The original (src/strategies/rabbit_liquidity_spike_v1_1_1 + scripts/run_rabbit_liquidity_spike_v1_1_1_screener.py) is an NSE midcap
cash-equity screener on 1-minute snapshots. Mapped to ES / NQ / GC / CL futures as literally as the data allows:
  spike      bar volume > K x the PREVIOUS bar's volume (original: 1-min, K = 10 or 20)  AND  bar volume > the average bar volume of
             the last 5 sessions (Condition 4). Rupee-value / spread / market-cap filters dropped (futures are always liquid).
  direction  day change vs the previous regular-session close: +1 % .. +4 % -> LONG, -4 % .. -1 % -> SHORT (Condition 3)
  trend gate previous close < 5-day SMA AND > previous day's VWAP AND < 20-day SMA (Condition 5, same for both sides)
  entries    09:30-15:00 New York, none 13:00-14:00 (the original's lunch window), one trade per market per day, entry = next bar open
  exits      (strategy code) stop 1 %, take profit 10 %, trailing stop 2 % behind the peak once +2 %, flat 15:45 New York
             (original 15:15 IST = 15 min before the close); stop checked first in a bar
Data: Yahoo 5-min (26 Jul - 6 Nov 2026) and 15-min (MASTER_BACKTESTER May-Jul + Yahoo Jul-Nov). No 1-minute US futures history exists
here, so the "10x the previous minute" spike is tested on 5- / 15-min bars, with K = 10 (literal) and looser K = 5, 3, 2.
Costs round trip: ES 0.5 pt, NQ 2 pt, GC $0.3, CL $0.03.
Run: python3 scripts/research/rabbit_ls_us_futures.py
"""
from __future__ import annotations

import os

import numpy as np
import pandas as pd

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
YAH = os.path.join(ROOT, "data_store", "market_data", "synaptic_intraday_all.csv")
MB = os.path.join(os.path.expanduser("~"), "Documents", "MASTER_BACKTESTER", "data", "us")
if not os.path.isdir(MB):
    MB = os.path.join(os.environ.get("MP_DOCS", ""), "MASTER_BACKTESTER", "data", "us")
MK = {"ES": ("ES=F", "ES", 0.5, 50), "NQ": ("NQ=F", "NQ", 2.0, 20), "GC": ("GC=F", "GOLD_COMEX", 0.3, 100), "CL": ("CL=F", "CRUDE_WTI", 0.03, 1000)}
SL, TP, TR_ON, TR = 0.01, 0.10, 0.02, 0.02


def load(mk, iv):
    ys, mbn, _, _ = MK[mk]
    d = pd.read_csv(YAH, usecols=["datetime_utc", "symbol", "interval", "open", "high", "low", "close", "volume"])
    d = d[(d.symbol == ys) & (d.interval == iv)].copy(); d["ts"] = pd.to_datetime(d.datetime_utc.str[:19]).dt.tz_localize("UTC")
    parts = [d[["ts", "open", "high", "low", "close", "volume"]]]
    if iv == "15m":
        p = os.path.join(MB, f"{mbn}_15min.csv")
        if os.path.exists(p):
            m = pd.read_csv(p); m["ts"] = pd.to_datetime(m.date).dt.tz_localize("UTC")      # file times are UTC (Globex opens 22:00)
            parts.insert(0, m[["ts", "open", "high", "low", "close", "volume"]])
    x = pd.concat(parts).drop_duplicates("ts").sort_values("ts")
    x["et"] = x.ts.dt.tz_convert("America/New_York"); x["day"] = x.et.dt.strftime("%Y-%m-%d"); x["hm"] = x.et.dt.strftime("%H:%M")
    return x[(x.et.dt.weekday < 5)].reset_index(drop=True)


def run(mk, iv, K, use_c5=True, use_lunch=True, use_c3=True):
    x = load(mk, iv); cost = MK[mk][2]
    rth = x[(x.hm >= "09:30") & (x.hm < "16:00")]
    days = sorted(rth.day.unique())
    daily = rth.groupby("day").agg(c=("close", "last"), n=("close", "size"))
    vw = rth.assign(pv=(rth.high + rth.low + rth.close) / 3 * rth.volume).groupby("day").agg(pv=("pv", "sum"), v=("volume", "sum"))
    daily["vwap"] = vw.pv / vw.v
    daily["sma5"], daily["sma20"] = daily.c.rolling(5).mean(), daily.c.rolling(20).mean()
    barvol = rth.groupby("day").volume.mean()
    x["prevvol"] = x.volume.shift(1)
    out = []
    for i, dd in enumerate(days):
        if i < 20:
            continue
        pdd = days[i - 1]; pc = daily.at[pdd, "c"]
        c5 = pc < daily.at[pdd, "sma5"] and pc > daily.at[pdd, "vwap"] and pc < daily.at[pdd, "sma20"]
        if use_c5 and not c5:
            continue
        avg5 = barvol.loc[days[i - 5:i]].mean()
        g = x[(x.day == dd) & (x.hm >= "09:30") & (x.hm < "16:00")].reset_index(drop=True)
        for j in range(len(g) - 1):
            r = g.iloc[j]
            if r.hm > "15:00" or (use_lunch and "13:00" <= r.hm < "14:00"):
                continue
            chg = (r.close / pc - 1) * 100
            spike = r.prevvol > 0 and r.volume > K * r.prevvol and r.volume > avg5
            if not spike:
                continue
            if use_c3:
                if 1 <= chg <= 4: sd = 1
                elif -4 <= chg <= -1: sd = -1
                else: continue
            else:
                sd = 1 if chg > 0 else -1
            e = g.iloc[j + 1].open; stop = e * (1 - sd * SL); peak = e; px = None; why = "EOD"
            for k in range(j + 1, len(g)):
                b = g.iloc[k]
                if b.hm >= "15:45":
                    px = g.iloc[k - 1].close; break
                if (sd > 0 and b.low <= stop) or (sd < 0 and b.high >= stop):
                    px = stop; why = "trail" if abs(stop / e - 1) < SL - 1e-9 or sd * (stop - e) > 0 else "stop"; break
                if sd * (b.high if sd > 0 else b.low) >= sd * e * (1 + sd * TP):
                    px = e * (1 + sd * TP); why = "target"; break
                peak = max(peak, b.high) if sd > 0 else min(peak, b.low)
                if sd * (peak / e - 1) >= TR_ON:
                    ts_ = peak * (1 - sd * TR)
                    stop = max(stop, ts_) if sd > 0 else min(stop, ts_)
            if px is None:
                px = g.iloc[-1].close
            pts = sd * (px - e) - cost
            out.append({"mk": mk, "day": dd, "t": r.hm, "side": sd, "chg": round(chg, 2), "spike": round(r.volume / r.prevvol, 1), "entry": e,
                        "pts": pts, "pct": pts / e * 100, "usd": pts * MK[mk][3], "why": why})
            break                                                     # one trade per market per day
    return pd.DataFrame(out), len(days) - 20


def show(T, lab, ndays):
    if not len(T):
        print(f"  {lab:58s} trades 0 (over ~{ndays} days)"); return
    print(f"  {lab:58s} trades {len(T):3d} | win {100*(T.pts>0).mean():3.0f}% | avg {T.pct.mean():+.3f}% after costs | "
          f"total ${T.usd.sum():+9,.0f} (1 full contract each) | exits {T.why.value_counts().to_dict()}")


if __name__ == "__main__":
    for iv in ("5m", "15m"):
        print("=" * 120, f"\n{iv} bars")
        for K in (10, 5, 3, 2):
            for nm, kw in (("literal (C3 1-4 % + C5 trend gate)", {}), ("without the C5 trend gate", {"use_c5": False}),
                           ("without C5 and without the 1-4 % gate", {"use_c5": False, "use_c3": False})):
                allT, nd = [], 0
                for mk in MK:
                    T, n = run(mk, iv, K, **kw); allT.append(T); nd = max(nd, n)
                T = pd.concat(allT) if allT else pd.DataFrame()
                show(T, f"K={K:<2d} {nm}", nd)
                if len(T) and kw == {"use_c5": False, "use_c3": False}:
                    print("      by market:", {m: f"{len(t)} tr, {t.pct.mean():+.3f}%" for m, t in T.groupby("mk")})
