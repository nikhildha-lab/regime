"""DOM STOCKS x TOP-10 UNIVERSE (2026-11-09, Nikhil: "flush out all trades for DOM v2 .. backtest again with 10 top stocks per day").

The DOM Stocks record (396 closed trades, 3-9 Nov 2026, archived in _archive/2026-11-03/backups/2026-11-09_dom_stocks_flush/) replayed under
the universe rule now live in app/topvol.py: a stock may be traded only if it is in the day's top 10 by traded value 09:15-09:30 (cash
1-min bars, ranked within the tracked F&O stocks). The rule only REMOVES trades - entries, stops, targets and exits are the recorded ones
(R after the engine's costs), so this is exactly what the engine would have recorded with the filter on. No look-ahead: the rank is
known at 09:30, every DOM stock entry is after 09:30.
Also shown: top 5 / 20, top 10 by the PREVIOUS day's value, by setup (hold / break), by side, by day, 2x / 3x costs, best 3 removed.
Output: data_store/research/dom_stocks_top10/report.txt.   Run: python3 scripts/research/dom_stocks_top10_backtest.py
"""
from __future__ import annotations

import json
import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
import top_volume_universe_test as TV   # noqa: E402  (ranks())

ROOT = TV.ROOT
TR = os.path.join(ROOT, "_archive", "2026-11-03", "backups", "2026-11-09_dom_stocks_flush", "dom_stocks_trades_396.json")
OUT = os.path.join(ROOT, "data_store", "research", "dom_stocks_top10")


def t(x):
    x = pd.Series(x).dropna(); return x.mean() / x.std() * np.sqrt(len(x)) if len(x) > 2 and x.std() else np.nan


def line(lab, x):
    if not len(x):
        return f"   {lab:34s}    0"
    gp = x.pnl[x.pnl > 0].sum(); gl = -x.pnl[x.pnl < 0].sum()
    return (f"   {lab:34s} {len(x):4d}  avg {x.r.mean():+5.2f} R  win {100*(x.r>0).mean():3.0f}%  PF {gp/gl if gl else float('inf'):4.2f}  t {t(x.r):+4.1f}  "
            f"total Rs {x.pnl.sum():+8.0f}  stocks/day {x.groupby('day').sym.nunique().mean():.1f}  trades/day {len(x)/max(x.day.nunique(),1):.1f}")


def main():
    os.makedirs(OUT, exist_ok=True)
    R = TV.ranks()
    T = json.load(open(TR))
    B = pd.DataFrame([{"day": x["day"], "sym": x["key"], "setup": x["setup"], "side": x["side"], "t0": x.get("opened", "")[11:16] if isinstance(x.get("opened"), str) else "",
                       "r": x.get("r"), "r_gross": x.get("r_gross"), "cost_r": x.get("cost_r"), "pnl": x.get("pnl"), "why_exit": x.get("exit_reason")} for x in T]).dropna(subset=["r"])
    B["key"] = list(zip(B.day, B.sym))
    B["rank_open"] = [R.rank_open.get(k, np.nan) for k in B.key]; B["rank_prev"] = [R.rank_prev.get(k, np.nan) for k in B.key]
    miss = B.rank_open.isna().sum()
    L = [f"DOM STOCKS x TOP-10 UNIVERSE · {len(B)} recorded trades · {B.day.min()} .. {B.day.max()} ({B.day.nunique()} sessions) · R and Rs after the engine's costs",
         f"   ranks within {R.index.get_level_values(1).nunique()} tracked F&O stocks with 1-min history · {miss} trades without a rank (treated as outside)\n",
         "1. THE RULE (top 10 by 09:15-09:30 traded value) against the full record and its neighbours"]
    top10 = B[B.rank_open <= 10]
    for lab, m in (("all recorded (no universe)", B.day.notna()), ("TOP 10 by opening value  <- live rule", B.rank_open <= 10), ("top 5 by opening value", B.rank_open <= 5),
                   ("top 20 by opening value", B.rank_open <= 20), ("outside the top 10 (open)", ~(B.rank_open <= 10)),
                   ("top 10 by yesterday's value", B.rank_prev <= 10), ("top 10 by both", (B.rank_open <= 10) & (B.rank_prev <= 10))):
        L.append(line(lab, B[m]))
    L.append("\n2. TOP 10 by day")
    for d, g in top10.groupby("day"):
        L.append(line(f"{d}  ({', '.join(sorted(g.sym.unique()))})"[:34], g))
    L.append("   all days, full record:")
    for d, g in B.groupby("day"):
        L.append(line(f"{d}  all", g))
    L.append("\n3. TOP 10 by setup and side")
    for (s_, sd), g in top10.groupby(["setup", "side"]):
        L.append(line(f"{s_} {sd}", g))
    for s_, g in B.groupby("setup"):
        L.append(line(f"{s_} - full record", g))
    L.append("\n4. TOP 10 by exit reason")
    for w, g in top10.groupby("why_exit"):
        L.append(line(str(w)[:34], g))
    L.append("\n5. TOP 10 by entry hour")
    top10 = top10.assign(hr=top10.t0.str[:2])
    for h, g in top10.groupby("hr"):
        L.append(line(f"{h}:xx", g))
    L.append("\n6. STRESS (top 10)")
    x = top10
    if len(x):
        c = x.cost_r.fillna(0)
        L.append(f"   2x costs: avg {(x.r - c).mean():+.2f} R · 3x costs: avg {(x.r - 2*c).mean():+.2f} R · best 3 removed: avg {x.r.sort_values().iloc[:-3].mean():+.2f} R · gross (no costs) avg {x.r_gross.mean():+.2f} R")
        eq = x.sort_values(["day", "t0"]).pnl.cumsum(); L.append(f"   equity: end Rs {eq.iloc[-1]:+.0f} · max drawdown Rs {(eq - eq.cummax()).min():+.0f} · best day Rs {x.groupby('day').pnl.sum().max():+.0f} · worst day Rs {x.groupby('day').pnl.sum().min():+.0f}")
    L.append("\nVERDICT: see FINDINGS_dom_stocks_top10.md. 5 sessions is far too little to judge a rule; the filter cuts the trade count by ~5/6 but does not turn the sign.")
    txt = "\n".join(L); print(txt); open(os.path.join(OUT, "report.txt"), "w").write(txt)
    B.to_csv(os.path.join(OUT, "trades_ranked.csv"), index=False)


if __name__ == "__main__":
    main()
