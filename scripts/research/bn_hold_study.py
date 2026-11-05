"""Iteration 3 (2026-11-06): longer holds at Bank Nifty level touches (2-year 5-min spot, touches from bn_level_study.py).
Fixed before running: fade move (pts, fade direction) after 30 / 60 / 120 min and at 15:15, overall and split by day direction
(pullback = fading WITH the day's move so far >= +0.3 %; counter = fading AGAINST it <= -0.3 %). Net = minus 12.5 pts. Y1 vs Y2."""
import glob, os
import numpy as np, pandas as pd
DOC = os.path.expanduser("~/mnt/Documents")
T = pd.read_csv("data_store/research/bn_level_study/touches.csv")
fs = sorted(glob.glob(f"{DOC}/nifty-live-tracker/datasets/2024-07-17_to_2026-07-19/*/BANKNIFTY_spot.csv"))
B = pd.concat([pd.read_csv(f) for f in fs]); B["dt"] = pd.to_datetime(B.datetime); B = B.drop_duplicates("dt").sort_values("dt")
B["day"] = B.dt.dt.strftime("%Y-%m-%d"); B["hm"] = B.dt.dt.strftime("%H:%M"); G = {d: g.set_index("hm").close for d, g in B.groupby("day")}
def at(d, hm, mins):
    s = G[d]; m = min(int(hm[:2]) * 60 + int(hm[3:]) + mins, 15 * 60 + 15); t = f"{m // 60:02d}:{m % 60:02d}"
    s2 = s[s.index <= t]; return s2.iloc[-1]
for m in (30, 60, 120, 999):
    T[f"m{m}"] = [r.side * (at(r.day, r.hm, m if m < 999 else 600) - r.e) for r in T.itertuples()]
T["ctx"] = np.where(T.daymove >= 0.3, "pullback (with day)", np.where(T.daymove <= -0.3, "counter (against day)", "flat day"))
out = T.groupby(["ctx", "year"])[["m30", "m60", "m120", "m999"]].mean().round(1)
out["n"] = T.groupby(["ctx", "year"]).size(); print(out.to_string())
print("\nall:", T.groupby("year")[["m30", "m60", "m120", "m999"]].mean().round(1).to_string())
print("\nnet after 12.5 pts, pullback 120 min:", T[T.ctx.str.startswith("pull")].groupby("year").m120.apply(lambda s: round(s.mean() - 12.5, 1)).to_dict())
