"""Stock Volume Break v1 - exit check with a 2-ATR trail (2026-11-08, Nikhil: "we can loosen the trail ATR to 2 ATR .. if the default SL is
tight then loosen it up"). Same 149 entries as scripts/research/stock_vadx_deepcheck.py; only the exit changes. 1-min path, the stop is
checked BEFORE the trail moves within a minute (conservative). Costs 0.12 %. ATR = 5-min ATR(14) at the signal bar.
Run: python3 scripts/research/stock_vadx_trail.py
"""
from __future__ import annotations

import os

import numpy as np
import pandas as pd

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
HIST = os.path.join(ROOT, "data_store", "research", "stocks_intraday")
E = pd.read_csv(os.path.join(ROOT, "data_store", "research", "stock_vadx_events.csv.gz"))
C = E[(E.setup == "break") & (E.spike >= 6) & (E.adx > 25) & (E.di_with == 1) & (E.rvol >= 2)].copy()
COST = 0.0012
#            stop ATR, target R, trail ATR, trail starts at +R (0 = from entry), breakeven at +R
V = {"A  stop 2 ATR, target 2R (backtest)":            (2, 2.0, None, 0, None),
     "B  stop 2 ATR, trail 2 ATR, no target":           (2, None, 2, 0, None),
     "C  stop 2 ATR, trail 2 ATR after +1R":            (2, None, 2, 1, None),
     "D  stop 2 ATR, target 3R, trail 2 ATR after +1R": (2, 3.0, 2, 1, None),
     "E  stop 2 ATR, target 2R, breakeven at +1R":      (2, 2.0, None, 0, 1),
     "F  stop 2.5 ATR, trail 2 ATR after +1R":          (2.5, None, 2, 1, None),
     "G  stop 3 ATR, trail 2 ATR after +1R":            (3, None, 2, 1, None),
     "H  stop 2 ATR, half at 2R, rest trail 2 ATR":     (2, "half2", 2, 1, None)}

cache = {}
rows = []
days = sorted(E.day.unique()); cut = days[int(len(days) * 2 / 3)]
for _, ev in C.iterrows():
    if ev.sym not in cache:
        d = pd.read_csv(os.path.join(HIST, f"{ev.sym}_1m.csv.gz"), parse_dates=["ts"]).drop_duplicates("ts").sort_values("ts")
        d["day"] = d.ts.dt.strftime("%Y-%m-%d"); d["mn"] = d.ts.dt.hour * 60 + d.ts.dt.minute - 555
        cache[ev.sym] = {k: g for k, g in d.groupby("day")}
    g = cache[ev.sym][ev.day]; mn = g.mn.values
    i0 = np.searchsorted(mn, (ev.slot + 1) * 5); i1 = np.searchsorted(mn, 360)
    H, L, Cl = g.high.values[i0:i1], g.low.values[i0:i1], g.close.values[i0:i1]
    e, sd = ev.entry, ev.side; atr = ev.atr_pct * e / 100
    out = {"day": ev.day, "sym": ev.sym}
    for nm, (sk, tgt, tk, tstart, be) in V.items():
        dist = max(sk * atr, e * 0.0015); stop = e - sd * dist; best = e; px = None; booked = 0.0; left = 1.0
        for h, l, c in zip(H, L, Cl):
            if (sd > 0 and l <= stop) or (sd < 0 and h >= stop):
                px = stop; break
            fav = h if sd > 0 else l
            if tgt == "half2" and left == 1.0 and sd * (fav - e) >= 2 * dist:
                booked += 0.5 * 2 * dist; left = 0.5
            elif isinstance(tgt, float) and sd * (fav - e) >= tgt * dist:
                px = e + sd * tgt * dist; break
            best = max(best, h) if sd > 0 else min(best, l)
            if be and sd * (best - e) >= be * dist:
                stop = max(stop, e) if sd > 0 else min(stop, e)
            if tk and sd * (best - e) >= tstart * dist:
                ts_ = best - sd * tk * atr
                stop = max(stop, ts_) if sd > 0 else min(stop, ts_)
        if px is None:
            px = Cl[-1]
        pnl = booked + left * sd * (px - e)
        out[nm] = pnl / e * 100 - COST * 100                       # % of price after costs
        out[nm + "_R"] = pnl / dist - COST * e / dist
    rows.append(out)
X = pd.DataFrame(rows)
print(f"entries {len(X)} | costs 0.12 % | per trade = % of price after costs (Rs on a Rs 1 lakh position = x 1,000)\n")
for nm in V:
    a = X[nm]; tr = X[X.day < cut][nm]; te = X[X.day >= cut][nm]
    mo = X.assign(m=X.day.str[:7]).groupby("m")[nm].mean()
    print(f"{nm:48s} per trade {a.mean():+.3f}% (Rs {a.mean()*1000:+4.0f}) | win {100*(a>0).mean():3.0f}% | R {X[nm+'_R'].mean():+.2f} | "
          f"train {tr.mean():+.3f}% test {te.mean():+.3f}% | months + {(mo>0).sum()}/{len(mo)} | worst {a.min():+.2f}% best {a.max():+.2f}%")
