"""Iteration 2 (2026-11-06): does the touch-minute delta predict Bank Nifty's next 30 minutes? 12 order-flow days (Dhan per-second files),
every level touch from touch_dom_combo.run() (futures; PDH/PDL/PDC, prior POC/VAH/VAL, IB, round 500s), with td = touch-bar delta / volume.
Fixed before running: buckets of td in the FADE direction (+ = buyers at support / sellers at resistance = touch-minute 'confirm'),
outcome p30 = plain 30-min move in the fade direction (pts), net after 12.5 pts. Also the opposite ('trapped side') reading."""
import os, sys
import numpy as np, pandas as pd
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import touch_dom_combo as TD
T, days = TD.run()
T = T[T.sym == "BANKNIFTY"].copy(); T["tdf"] = T.td * T.side
T["net30"] = T.p30 - 12.5
b = pd.cut(T.tdf, [-1, -0.5, -0.2, 0.2, 0.5, 1], labels=["< -0.5 (strong AGAINST)", "-0.5..-0.2", "neutral", "+0.2..+0.5 (confirm)", "> +0.5 (strong confirm)"])
print("Bank Nifty touches", len(T), "days", T.day.nunique(), T.day.min(), "->", T.day.max())
print(T.groupby(b, observed=True).agg(n=("p30", "size"), avg_p30=("p30", "mean"), med_p30=("p30", "median"), win=("p30", lambda s: (s > 12.5).mean()),
      net_rs=("net30", lambda s: round(30 * s.mean()))).round(2).to_string())
print("\ncorr(tdf, p30) =", round(T.tdf.corr(T.p30), 3), " Spearman", round(T.tdf.corr(T.p30, method="spearman"), 3))
F = T[T.tdf < -0.2].copy(); F["fade_trap"] = -F.p30    # trade WITH the aggressive side instead (break / continuation)
print("trade with the aggressive side when delta is against the fade: n", len(F), "avg pts", round(F.fade_trap.mean(), 1), "net Rs/trade", round(30 * (F.fade_trap - 12.5).mean()))
