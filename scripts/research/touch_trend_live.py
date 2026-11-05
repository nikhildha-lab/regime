"""Same filters as touch_trend_filters.py (fixed before either run), checked on the LIVE touch-minute records
(data_store/livedesk/touches/<day>.json via touchtrades.trades_for: the live levels and live signal, plain 30-min exit, 1 lot synthetic,
4.5 / 12.5 pts). Features from the futures 1-min bars of the per-second files. Paper research only."""
import os, sys
import numpy as np, pandas as pd
R = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.abspath(os.path.join(R, "..", ".."))
sys.path.insert(0, R); sys.path.insert(0, os.path.join(ROOT, "app"))
import touch_trend_filters as TF, touchtrades as TT
SYN = {"NIFTY": 4.5, "BANKNIFTY": 12.5}
F = TF.feats()
rows = []
for day in sorted(f[:10] for f in os.listdir(TT.TDIR) if f.endswith(".json")):
    for t in TT.trades_for(day):
        if t["status"] != "closed": continue
        A, tr15 = F[t["key"]]; a = A[(A.day == day) & (A.time == t["time"])]
        if not len(a): continue
        a = a.iloc[0]; s = 1 if t["side"] == "long" else -1
        done = tr15[tr15.index + pd.Timedelta(minutes=15) <= pd.Timestamp(a.t, unit="s")]
        rows.append(dict(day=day, key=t["key"], time=t["time"], side=s, level=t["level_name"], pts=t["pts"],
                         rs=round((t["pts"] - SYN[t["key"]]) * TT.LOT[t["key"]]), gap=a.gap, daymove=a.c / a.open_ - 1,
                         t15=int(done.iloc[-1]) if len(done) else 0))
T = pd.DataFrame(rows); s = T.side
T["A1"] = ~((T.gap.abs() >= 0.01) & (np.sign(T.gap) != s))
T["A2"] = ~(((T.daymove >= 0.005) & (s < 0)) | ((T.daymove <= -0.005) & (s > 0)))
T["A3"] = ~((T.t15 != 0) & (T.t15 != s))
T["A"] = T.A1 & T.A2 & T.A3
T["C"] = T.level.str.contains("yday|PDH|PDL|prev|block", case=False)
T["AC"] = T.A & T.C; T["base"] = True
out = []
for f in ("base", "A1", "A2", "A3", "A", "C", "AC"):
    Z = T[T[f]]
    out.append({"filter": f, "n": len(Z), "win%": round(100 * (Z.rs > 0).mean()) if len(Z) else None, "avg_pts": round(Z.pts.mean(), 1),
                "rs": int(Z.rs.sum()), **{d[5:]: int(Z[Z.day == d].rs.sum()) for d in sorted(T.day.unique())}})
S = pd.DataFrame(out); T.to_csv(os.path.join(ROOT, "data_store/research/touch_trend/live_trades.csv"), index=False)
S.to_csv(os.path.join(ROOT, "data_store/research/touch_trend/live_summary.csv"), index=False)
print(S.to_string(index=False))
print(T[["day", "key", "time", "side", "level", "pts", "gap", "daymove", "t15", "A"]].round(3).to_string(index=False))
