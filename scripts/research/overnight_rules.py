"""Overnight rules from overnight_study.py (2026-11-06). Fixed BEFORE running; only data known at the time of entry.
  O1 OVERNIGHT HOLD : at the 15:25 close, close location >= 0.80 AND day return > 0 -> long; <= 0.20 AND day return < 0 -> short;
                      exit at the next day's open (first 5-min open). (FII data is published in the evening - NOT used.)
  O2 GAP FADE       : next morning, if the gap is >= 0.25 % in the same direction as the previous day's close signal (O1 condition),
                      trade AGAINST the gap at the 09:15 bar close, exit at 10:10 close (first hour).
  O3 both, as one plan (hold overnight, then flip at the open).
Money: synthetic future 1 lot - Nifty 65 x (pts - 4.5), Bank Nifty 30 x (pts - 12.5) per trade. Overnight = gap risk; worst case shown."""
import glob, os
import numpy as np, pandas as pd
DOC = os.path.expanduser("~/mnt/Documents")
COST = {"NIFTY": 4.5, "BANKNIFTY": 12.5}; LOT = {"NIFTY": 65, "BANKNIFTY": 30}
for sym in ("NIFTY", "BANKNIFTY"):
    D = pd.read_csv(f"data_store/research/overnight_{sym}.csv").set_index("day")
    fs = sorted(glob.glob(f"{DOC}/nifty-live-tracker/datasets/2024-07-17_to_2026-07-19/*/{sym}_spot.csv"))
    B = pd.concat([pd.read_csv(f) for f in fs]); B["day"] = B.datetime.str[:10]; B["hm"] = B.datetime.str[11:16]
    c0915 = B[B.hm == "09:15"].set_index("day").close
    days = list(D.index); nxt = dict(zip(days[:-1], days[1:]))
    rows = []
    for d, r in D.iterrows():
        n = nxt.get(d)
        if n is None or n not in D.index: continue
        sig = 1 if (r.cloc >= 0.8 and r.dret > 0) else -1 if (r.cloc <= 0.2 and r.dret < 0) else 0
        if not sig: continue
        no = D.o[n]; o1 = sig * (no - r.c)                                                  # overnight pts
        g = (no / r.c - 1) * 100; o2 = None
        if sig * g >= 0.25 and n in c0915.index:
            e = c0915[n]; o2 = -sig * (D.c1010[n] - e)                                       # fade the gap, first hour
        rows.append(dict(day=d, year="Y1" if d < "2025-07-01" else "Y2", sig=sig, o1=o1, o2=o2, gap=g))
    T = pd.DataFrame(rows)
    print(f"\n===== {sym} =====  signal days {len(T)} of {len(D)}")
    for k in ("o1", "o2"):
        Z = T.dropna(subset=[k])
        s = Z.groupby("year")[k].agg(n="size", avg_pts="mean", win=lambda x: (x > COST[sym]).mean(), worst="min")
        s["rs_trade"] = (s.avg_pts - COST[sym]) * LOT[sym]; s["rs_total"] = s.rs_trade * s.n
        s["long_avg"] = Z[Z.sig > 0].groupby("year")[k].mean(); s["short_avg"] = Z[Z.sig < 0].groupby("year")[k].mean()
        print({"o1": "O1 overnight hold (close -> next open)", "o2": "O2 gap fade (09:15 close -> 10:10)"}[k]); print(s.round(1).to_string())
    T["o3"] = T.o1 + T.o2.fillna(0)
    print("O3 both:", T.groupby("year").o3.agg(["size", "mean"]).round(1).to_dict())
    T.to_csv(f"data_store/research/overnight_rules_{sym}.csv", index=False)
