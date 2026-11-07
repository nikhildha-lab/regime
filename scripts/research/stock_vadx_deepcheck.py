"""Deep check of the best stock break condition from scripts/research/stock_volume_adx_backtest.py (2026-11-08).
Candidate (chosen as the centre of the cluster that was positive in BOTH train and test, not the single best line):
    break of yesterday's high / low or the opening range | 5-min volume >= 6x normal | ADX(5m) > 25 with +DI / -DI on the trade side |
    day volume so far >= 2x normal | stop 2 x ATR(5m), target 2R, flat 15:15
Checks: by month, by side, by level, by stock (concentration), one trade per stock-day, cost 0.15 %, neighbours of every threshold,
and a random-pick test (how often a random set of the same number of breaks does this well).
Run: python3 scripts/research/stock_vadx_deepcheck.py
"""
from __future__ import annotations

import os

import numpy as np
import pandas as pd

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
E = pd.read_csv(os.path.join(ROOT, "data_store", "research", "stock_vadx_events.csv.gz"))
COST = 0.0012
E["r"] = E.E5_r - COST * E.E5_c                     # R after costs, exit E5
E["pct"] = E.r / E.E5_c * 100                         # % of price after costs
E["rs"] = E.pct / 100 * 100_000                       # Rs on a Rs 1 lakh position (Rs 20k margin x 5, as Stocks in Play ORB)
days = sorted(E.day.unique()); cut = days[int(len(days) * 2 / 3)]


def cond(spk=6, adx=25, di=True, rv=2.0, fam=None):
    m = (E.setup == "break") & (E.spike >= spk) & (E.adx > adx) & (E.rvol >= rv)
    if di:
        m &= E.di_with == 1
    if fam:
        m &= E.fam == fam
    return m


def s(x, lab):
    if not len(x):
        return f"{lab:38s} n 0"
    dd = x.groupby("day").rs.sum()
    return (f"{lab:38s} n {len(x):4d} | win {100*(x.r>0).mean():3.0f}% | R net {x.r.mean():+.3f} | per trade {x.pct.mean():+.3f}% "
            f"(Rs {x.rs.mean():+5.0f} on 1 lakh) | total Rs {x.rs.sum():+8,.0f} | days {x.day.nunique()} (+{(dd>0).sum()})")


C = E[cond()].copy()
print(s(C, "CANDIDATE all days"))
print(s(C[C.day < cut], "  train (first 2/3)")); print(s(C[C.day >= cut], "  test (last 1/3)"))
print("\nby month:"); C["mon"] = C.day.str[:7]
for m_, x in C.groupby("mon"):
    print(" ", s(x, m_))
print("\nby side / level:")
for k, x in C.groupby("side"):
    print(" ", s(x, "long" if k > 0 else "short"))
for k, x in C.groupby("level"):
    print(" ", s(x, k))
print("\nby stock (top 8 by trades):")
for k, x in sorted(C.groupby("sym"), key=lambda kv: -len(kv[1]))[:8]:
    print(" ", s(x, k))
top = C.groupby("sym").rs.sum().sort_values(ascending=False)
print("  share of profit from the best 3 stocks:", round(100 * top.head(3).sum() / max(1, top[top > 0].sum())), "% | stocks positive", (top > 0).sum(), "of", len(top))
print(s(C[~C.sym.isin(top.head(3).index)], "  without the best 3 stocks"))
f1 = C.sort_values(["day", "sym", "slot"]).groupby(["day", "sym"]).head(1)
print(s(f1, "\nfirst signal per stock-day only"))
print(s(C.assign(r=C.E5_r - 0.0015 * C.E5_c, pct=(C.E5_r - 0.0015 * C.E5_c) / C.E5_c * 100, rs=(C.E5_r - 0.0015 * C.E5_c) / C.E5_c * 1000), "cost 0.15 % instead of 0.12 %"))
print("\nneighbours (each threshold moved, rest kept):")
for kw in ({"spk": 4}, {"spk": 5}, {"spk": 8}, {"spk": 10}, {"adx": 20}, {"adx": 30}, {"di": False}, {"rv": 1.5}, {"rv": 3.0}, {"fam": "pd"}, {"fam": "or"}):
    x = E[cond(**kw)]
    print(" ", s(x, str(kw)), "| train R", round(x[x.day < cut].r.mean(), 3), "test R", round(x[x.day >= cut].r.mean(), 3))
print("\nexits for the same entries:")
for ex in ("E1", "E2", "E3", "E4", "E5", "E6", "E7", "E8", "E9"):
    r = C[f"{ex}_r"] - COST * C[f"{ex}_c"]
    print(f"  {ex}: R net {r.mean():+.3f} | per trade {(r / C[f'{ex}_c']).mean()*100:+.3f}% | win {100*(r>0).mean():.0f}%")
B = E[E.setup == "break"]
rng = np.random.default_rng(7); n = len(C); obs = C.pct.mean()
sims = np.array([B.pct.sample(n, random_state=int(rng.integers(1e9))).mean() for _ in range(2000)])
print(f"\nrandom-pick test: {n} random breaks, 2000 tries -> average {sims.mean():+.3f}% a trade; better than the candidate's {obs:+.3f}% in "
      f"{100*(sims >= obs).mean():.1f}% of tries")
