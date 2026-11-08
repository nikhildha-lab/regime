"""F&O FLOW - round 4: turn "fresh short build-up" into a tradable rule and test it as a portfolio (2026-11-08).

Signal at day t's close (the live board has it by 15:20): stock futures move <= -2 % on the day AND total futures OI up >= +10 %.
Variants fixed in advance (from round 3, both made economic sense there): + closed in the lower half of the day's range (clv < -0.5),
+ call writers heavier than put writers (lean < 0).
Trade: SHORT the most-held futures contract at the NEXT day's open (the edge sat in the next session, not overnight), stop-loss on a
day's high, exit at the close after 1, 3 or 5 sessions. Max 5 new trades a day (the 5 biggest OI rises), equal rupees per trade.
Costs 0.06 % round trip. Stop fills at the stop price (gaps above the stop fill at the open - worse).
Reported per trade and as a daily portfolio (sum of open trades' daily P&L / max positions), by half and by year.
Run: python3 scripts/research/fno_flow_research4.py
"""
from __future__ import annotations

import glob
import os

import numpy as np
import pandas as pd

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
H = os.path.join(ROOT, "data_store", "research", "fno_hist")
P = pd.read_csv(os.path.join(ROOT, "data_store", "research", "fno_flow_panel.csv.gz"))
F = pd.concat([pd.read_csv(f, usecols=["day", "sym", "expiry", "open", "high", "low", "close"]) for f in sorted(glob.glob(os.path.join(H, "fut_*.csv.gz")))])
F = F.drop_duplicates(["day", "sym", "expiry"]).set_index(["sym", "expiry", "day"]).sort_index()
days = sorted(P.day.unique()); di = {d: i for i, d in enumerate(days)}; half = days[len(days) // 2]
COST = 0.06


def simulate(sig, hold, stop):
    rows = []
    for _, s in sig.iterrows():
        i = di[s.day]
        if i + 1 >= len(days):
            continue
        path = []
        for k in range(1, hold + 1):
            if i + k >= len(days):
                break
            key = (s.sym, s.expiry, days[i + k])
            if key not in F.index:
                break
            path.append(F.loc[key])
        if not path:
            continue
        e = path[0]["open"]; sp = e * (1 + stop / 100) if stop else None; out, why, held = None, "time", 0
        for k, b in enumerate(path):
            held = k + 1
            if sp and b["high"] >= sp:
                out = max(sp, b["open"]) if k > 0 else (sp if b["open"] < sp else b["open"]); why = "stop"; break
        if out is None:
            out = path[-1]["close"]
        pnl = (e - out) / e * 100 - COST
        rows.append({"day": s.day, "sym": s.sym, "entry_day": days[i + 1], "pnl": pnl, "why": why, "held": held})
    return pd.DataFrame(rows)


def show(T, lab):
    if not len(T):
        print(f"  {lab}: no trades"); return
    x = T.pnl; yr = T.groupby(T.day.str[:4]).pnl.mean()
    eq = T.groupby("entry_day").pnl.sum().sort_index() / 5                       # 5 slots of equal size -> daily % of capital
    cum = eq.cumsum(); dd = (cum - cum.cummax()).min()
    sh = eq.mean() / eq.std() * np.sqrt(252) if eq.std() else np.nan
    print(f"  {lab:50s} n {len(T):4d} | avg {x.mean():+.2f}% net (t {x.mean()/x.std()*np.sqrt(len(x)):+.1f}) win {100*(x>0).mean():.0f}% | stops {100*(T.why=='stop').mean():.0f}% "
          f"| halves {x[T.day<half].mean():+.2f} / {x[T.day>=half].mean():+.2f} | by year " + " ".join(f"{k} {v:+.2f}" for k, v in yr.items())
          + f" | portfolio: {eq.sum():+.1f}% total, Sharpe {sh:.2f}, max DD {dd:.1f}%")


if __name__ == "__main__":
    P = P[P.ret.abs() <= 20]
    base = P[(P.oi_chg >= 10) & (P.ret <= -2)].copy()
    base = base.sort_values(["day", "oi_chg"], ascending=[True, False]).groupby("day").head(5)
    V = {"core (OI >= +10 %, move <= -2 %)": base, "core + weak close (clv < -0.5)": base[base.clv < -0.5],
         "core + weak close + call writers heavier": base[(base.clv < -0.5) & (base.lean < 0)]}
    for nm, sig in V.items():
        print(nm)
        for hold in (1, 3, 5):
            for stop in (None, 3, 5):
                show(simulate(sig, hold, stop), f"hold {hold} d, stop {stop or 'none'}{'' if not stop else ' %'}")
        print()
