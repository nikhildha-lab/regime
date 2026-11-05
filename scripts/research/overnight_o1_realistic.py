"""O1 overnight hold - realistic version (2026-11-06): signal from bars up to 15:15 (close location / day return measured at the 15:15
bar close = ~15:20 price), entry at that price, exit at the next day's 09:15 bar OPEN and also at its 09:15 bar CLOSE (~09:20, safer
fill after the open auction). Same thresholds as overnight_rules.py (fixed before). Synthetic future 1 lot after costs.
Also: by weekday, and the 10 worst nights."""
import glob, os
import numpy as np, pandas as pd
DOC = os.path.expanduser("~/mnt/Documents"); COST = {"NIFTY": 4.5, "BANKNIFTY": 12.5}; LOT = {"NIFTY": 65, "BANKNIFTY": 30}
for sym in ("NIFTY", "BANKNIFTY"):
    fs = sorted(glob.glob(f"{DOC}/nifty-live-tracker/datasets/2024-07-17_to_2026-07-19/*/{sym}_spot.csv"))
    B = pd.concat([pd.read_csv(f) for f in fs]).drop_duplicates("datetime"); B["day"] = B.datetime.str[:10]; B["hm"] = B.datetime.str[11:16]
    rows = []; prev_c = None; days = []
    for d, g in B.groupby("day"):
        g = g.set_index("hm")
        if len(g) < 60 or "15:15" not in g.index or "09:15" not in g.index: prev_c = None if len(g) < 60 else g.close.iloc[-1]; continue
        upto = g[g.index <= "15:15"]; e = upto.close.iloc[-1]; h, l = upto.high.max(), upto.low.min()
        rows.append(dict(day=d, e=e, cloc=(e - l) / (h - l), dret=(e / prev_c - 1) * 100 if prev_c else np.nan, o915=g.open["09:15"], c915=g.close["09:15"]))
        prev_c = g.close.iloc[-1]
    D = pd.DataFrame(rows).set_index("day"); n = D.shift(-1)
    D["sig"] = np.where((D.cloc >= 0.8) & (D.dret > 0), 1, np.where((D.cloc <= 0.2) & (D.dret < 0), -1, 0))
    D["to_open"] = D.sig * (n.o915 - D.e); D["to_0920"] = D.sig * (n.c915 - D.e)
    T = D[(D.sig != 0)].dropna(subset=["to_open"]).copy(); T["year"] = np.where(T.index < "2025-07-01", "Y1", "Y2")
    T["wd"] = pd.to_datetime(T.index).day_name().str[:3]
    print(f"\n===== {sym}: {len(T)} signal nights =====")
    for k in ("to_open", "to_0920"):
        s = T.groupby("year")[k].agg(n="size", avg_pts="mean", median="median", win=lambda x: (x > COST[sym]).mean(), worst="min", best="max")
        s["rs_trade"] = (s.avg_pts - COST[sym]) * LOT[sym]
        s["long"] = T[T.sig > 0].groupby("year")[k].mean(); s["short"] = T[T.sig < 0].groupby("year")[k].mean()
        print(k); print(s.round(1).to_string())
    print("by weekday (entry day), avg pts to 09:20:", T.groupby(["wd", "year"]).to_0920.mean().round(1).unstack().to_dict())
    print("worst 6 nights:", T.to_0920.nsmallest(6).round(0).to_dict())
    # control: EVERY night long (to compare against plain drift)
    D["all_long"] = n.c915 - D.e; print("control - every night long, avg pts to 09:20:", D.groupby(np.where(D.index < "2025-07-01", "Y1", "Y2")).all_long.mean().round(1).to_dict())
    T.to_csv(f"data_store/research/overnight_o1_{sym}.csv")
