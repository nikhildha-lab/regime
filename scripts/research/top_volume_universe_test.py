"""TOP-VOLUME UNIVERSE for intraday stock rules (2026-11-09, Nikhil: "intraday strategies will target only top F&O stocks by volume for the day
along with their own entry conditions, max 10 stocks a day").

Question: do the intraday stock rules do better when restricted to the day's highest-volume names? Two definitions, both known before the
trade (no look-ahead), ranked within the 48 F&O stocks that have 1-min history (data_store/research/stocks_intraday):
   PREV   top N by the PREVIOUS session's traded value (price x volume)
   OPEN   top N by traded value in the first 15 minutes today (09:15-09:30)
N = 10 (the ask) and 20 (neighbour). Rules tested on their own signals:
   ORB         the S1 signals of strategy_changes_test.py (49 stocks, 20 Aug - 6 Nov, 64 trades), live exit, 0.12 % costs
   DOM Stocks  the 312 recorded trades (3-9 Nov), recorded R after costs
   Vol Break   stock_volume_adx_backtest signals if its trade file exists (data_store/research/stock_vadx/*.csv), else skipped
Output: data_store/research/strategy_changes/top_volume_report.txt.   Run: python3 scripts/research/top_volume_universe_test.py
"""
from __future__ import annotations

import glob
import json
import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
import strategy_changes_test as SC   # noqa: E402

ROOT = SC.ROOT; SD = SC.SD; OUT = SC.OUT


def t(x):
    x = pd.Series(x).dropna(); return x.mean() / x.std() * np.sqrt(len(x)) if len(x) > 2 and x.std() else np.nan


def ranks():
    """per day: traded value yesterday and in the first 15 min today, for every stock with 1-min history"""
    rows = []
    for f in sorted(glob.glob(os.path.join(SD, "*_1m.csv.gz"))):
        sym = os.path.basename(f)[:-10]
        d = pd.read_csv(f, parse_dates=["ts"]); d["day"] = d.ts.dt.strftime("%Y-%m-%d"); d["hm"] = d.ts.dt.strftime("%H:%M")
        d["val"] = d.close * d.volume
        day_val = d.groupby("day").val.sum(); open_val = d[d.hm < "09:30"].groupby("day").val.sum()
        for day in day_val.index:
            rows.append({"sym": sym, "day": day, "val_day": day_val[day], "val_open": open_val.get(day, 0.0)})
    R = pd.DataFrame(rows).sort_values(["sym", "day"])
    R["val_prev"] = R.groupby("sym").val_day.shift(1)
    R["rank_prev"] = R.groupby("day").val_prev.rank(ascending=False, method="first")
    R["rank_open"] = R.groupby("day").val_open.rank(ascending=False, method="first")
    return R.set_index(["day", "sym"])


def main():
    os.makedirs(OUT, exist_ok=True)
    R = ranks()
    L = [f"TOP-VOLUME UNIVERSE TEST · ranks within {R.index.get_level_values(1).nunique()} F&O stocks with 1-min history\n"]
    # ORB
    A = SC.orb()
    A["key"] = list(zip(A.day, A.sym))
    A["rank_prev"] = [R.rank_prev.get(k, np.nan) for k in A.key]; A["rank_open"] = [R.rank_open.get(k, np.nan) for k in A.key]
    L.append(f"ORB · {len(A)} trades · live exit, % per trade after costs")
    L.append(f"   {'universe':26s} {'n':>4s} {'avg %':>7s} {'win':>5s} {'t':>5s} {'total %':>8s}")
    for lab, m in (("all 48", A.day.notna()), ("top 10 by yesterday's value", A.rank_prev <= 10), ("top 20 by yesterday's value", A.rank_prev <= 20),
                   ("top 10 by 09:15-09:30 value", A.rank_open <= 10), ("top 20 by 09:15-09:30 value", A.rank_open <= 20),
                   ("NOT in top 10 (yesterday)", A.rank_prev > 10), ("NOT in top 10 (open)", A.rank_open > 10)):
        x = A[m]
        if len(x):
            L.append(f"   {lab:26s} {len(x):4d} {x.base.mean():+7.3f} {100*(x.base>0).mean():4.0f}% {t(x.base):+5.1f} {x.base.sum():+8.2f}")
    # DOM stocks
    T = [x for x in json.load(open(SC.TR)) if x["key"] not in ("NIFTY", "BANKNIFTY") and x["status"] == "closed"]
    B = pd.DataFrame([{"day": x["day"], "sym": x["key"], "r": x.get("r"), "pnl": x.get("pnl")} for x in T]).dropna(subset=["r"])
    B["key"] = list(zip(B.day, B.sym)); B["rank_prev"] = [R.rank_prev.get(k, np.nan) for k in B.key]; B["rank_open"] = [R.rank_open.get(k, np.nan) for k in B.key]
    L.append(f"\nDOM STOCKS · {len(B)} recorded trades · R after costs")
    L.append(f"   {'universe':26s} {'n':>4s} {'avg R':>6s} {'win':>5s} {'t':>5s} {'total Rs':>9s}")
    for lab, m in (("all", B.day.notna()), ("top 10 by yesterday's value", B.rank_prev <= 10), ("top 20 by yesterday's value", B.rank_prev <= 20),
                   ("top 10 by 09:15-09:30 value", B.rank_open <= 10), ("top 20 by 09:15-09:30 value", B.rank_open <= 20),
                   ("NOT in top 10 (yesterday)", B.rank_prev > 10), ("NOT in top 10 (open)", B.rank_open > 10)):
        x = B[m]
        if len(x):
            L.append(f"   {lab:26s} {len(x):4d} {x.r.mean():+6.2f} {100*(x.r>0).mean():4.0f}% {t(x.r):+5.1f} {x.pnl.sum():+9.0f}")
    # Vol break, if a trade file exists
    vf = sorted(glob.glob(os.path.join(ROOT, "data_store", "research", "stock_vadx*", "*trades*.csv")))
    if vf:
        V = pd.read_csv(vf[-1])
        dcol = next((c for c in V.columns if c in ("day", "date")), None); scol = next((c for c in V.columns if c in ("sym", "symbol", "stock")), None)
        rcol = next((c for c in V.columns if c in ("net_pct", "net", "pct", "ret")), None)
        if dcol and scol and rcol:
            V["day"] = V[dcol].astype(str).str[:10]; V["key"] = list(zip(V.day, V[scol]))
            V["rank_prev"] = [R.rank_prev.get(k, np.nan) for k in V.key]; V["rank_open"] = [R.rank_open.get(k, np.nan) for k in V.key]
            L.append(f"\nVOL BREAK · {len(V)} backtest trades ({os.path.basename(vf[-1])}) · {rcol} per trade")
            for lab, m in (("all", V.day.notna()), ("top 10 by yesterday's value", V.rank_prev <= 10), ("top 10 by 09:15-09:30 value", V.rank_open <= 10), ("NOT in top 10 (open)", V.rank_open > 10)):
                x = V[m]
                if len(x):
                    L.append(f"   {lab:26s} {len(x):4d} {x[rcol].mean():+7.3f} {100*(x[rcol]>0).mean():4.0f}% {t(x[rcol]):+5.1f}")
    else:
        L.append("\nVOL BREAK · no saved backtest trade file found - not tested here")
    txt = "\n".join(L); print(txt); open(os.path.join(OUT, "top_volume_report.txt"), "w").write(txt)


if __name__ == "__main__":
    main()
