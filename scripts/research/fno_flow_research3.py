"""F&O FLOW - round 3: the one candidate that survived rounds 1-2, stress-tested (2026-11-08).

Candidate: FRESH SHORT BUILD-UP CONTINUATION. A stock whose futures fall hard on the day WITH a big rise in open interest (new shorts,
not longs leaving) keeps underperforming the next day. Round 2 (|OI| >= 15 %, move <= -3 %): +33.6 bps next day vs the market, t 2.7,
both halves (+29 / +39), n 400. The long-side mirror (big long build-up) showed nothing, and so did the build-up classes on normal days.
Round 3 checks, before anything is traded:
  1. threshold grid (OI rise x price fall) - is it a smooth region or one lucky cell?
  2. RAW short P&L (what a trader actually earns), not just vs the market; entry at today's close vs next open; hold 1 / 2 / 3 / 5 days
  3. stability: by half, by quarter; trades per month; worst trades; corporate-action filter (|move| > 20 % removed)
  4. add-ons fixed in advance: heavy call writing (lean < 0), sector also down, market (Nifty) down that day
Costs: 0.06 % round trip on futures (STT 0.02 % sell side + charges + slippage). Short futures, so no borrow needed.
Run: python3 scripts/research/fno_flow_research3.py
"""
from __future__ import annotations

import os

import numpy as np
import pandas as pd

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
P = pd.read_csv(os.path.join(ROOT, "data_store", "research", "fno_flow_panel.csv.gz"))
COST = 6.0          # bps round trip
days = sorted(P.day.unique()); half = days[len(days) // 2]
P = P[P.ret.abs() <= 20]                                                    # corporate actions / bad prints out
P["mkt1"] = P.groupby("day").r1.transform("mean"); P["mkt_ret"] = P.groupby("day").ret.transform("mean")
P["sec_ret"] = P.groupby(["day", "sector"]).ret.transform("median")
P["rid_short"] = -P.rid; P["r1_short"] = -P.r1


def t(x):
    x = pd.Series(x).dropna(); return x.mean() / x.std() * np.sqrt(len(x)) if len(x) > 2 and x.std() else np.nan


def stats(e, col="r1_short", lab=""):
    x = e[col] * 100; h1, h2 = x[e.day < half], x[e.day >= half]
    by_day = x.groupby(e.day).mean()                                          # equal weight per day (what a portfolio earns)
    return (f"{lab:44s} n {len(e):4d} ({e.day.nunique():3d} days) | gross {x.mean():+6.1f} bps (t {t(x):+.1f}) net {x.mean()-COST:+6.1f} | win {100*(x>0).mean():3.0f}% "
            f"| halves {h1.mean():+6.1f} / {h2.mean():+6.1f} | per-day Sharpe {by_day.mean()/by_day.std()*np.sqrt(252) if by_day.std() else np.nan:+.2f}")


if __name__ == "__main__":
    print(f"panel {len(P):,} stock-days · {len(days)} days {days[0]} .. {days[-1]}\n")
    print("1. GRID: short next day (close -> next close), RAW gross bps / n  (rows: OI rise >=, cols: price fall <=)")
    moves = [-1.5, -2, -3, -4, -5]; ois = [5, 10, 15, 20, 30]
    print("   OI\\move " + " ".join(f"{m:>12}" for m in moves))
    for o in ois:
        cells = []
        for m in moves:
            e = P[(P.oi_chg >= o) & (P.ret <= m)].dropna(subset=["r1"])
            cells.append(f"{(e.r1_short*100).mean():+6.1f}/{len(e):<5d}")
        print(f"   >= {o:3d}% " + " ".join(f"{c:>12}" for c in cells))
    print("   same grid, vs the market (r1 minus the average stock):")
    for o in ois:
        cells = []
        for m in moves:
            e = P[(P.oi_chg >= o) & (P.ret <= m)].dropna(subset=["r1"])
            cells.append(f"{(-(e.r1 - e.mkt1)*100).mean():+6.1f} t{t(-(e.r1-e.mkt1)):+.1f}")
        print(f"   >= {o:3d}% " + " ".join(f"{c:>12}" for c in cells))
    print("   mirror (long side): price up >= m, OI up >= o, LONG next day, raw gross bps:")
    for o in ois:
        cells = []
        for m in [1.5, 2, 3, 4, 5]:
            e = P[(P.oi_chg >= o) & (P.ret >= m)].dropna(subset=["r1"])
            cells.append(f"{(e.r1*100).mean():+6.1f}/{len(e):<5d}")
        print(f"   >= {o:3d}% " + " ".join(f"{c:>12}" for c in cells))

    base = P[(P.oi_chg >= 10) & (P.ret <= -2)].dropna(subset=["r1"]).copy()
    print("\n2. ENTRY / HOLD for the base rule (OI >= +10 %, move <= -2 %), short futures:")
    print("  ", stats(base, "r1_short", "enter at today's close, out next close"))
    base["on_s"] = -base.ron; base["r2_s"] = -(base.c3 / base.close - 1) * 100 if "c3" in base else np.nan
    print("  ", stats(base, "on_s", "enter at close, out next open (overnight)"))
    print("  ", stats(base, "rid_short", "enter next open, out next close (intraday)"))
    base["r3_s"] = -base.r3; base["r5_s"] = -base.r5
    print("  ", stats(base.dropna(subset=["r3"]), "r3_s", "enter at close, hold 3 days"))
    print("  ", stats(base.dropna(subset=["r5"]), "r5_s", "enter at close, hold 5 days"))
    for o, m in ((15, -3), (20, -3), (10, -3), (15, -2)):
        e = P[(P.oi_chg >= o) & (P.ret <= m)].dropna(subset=["r1"])
        print("  ", stats(e, "r1_short", f"OI >= +{o} %, move <= {m} %, 1 day"))

    print("\n3. STABILITY of the base rule (1 day, close -> close), by quarter:")
    base["qtr"] = pd.PeriodIndex(base.day, freq="Q").astype(str)
    for q, e in base.groupby("qtr"):
        x = e.r1_short * 100
        print(f"   {q}: n {len(e):4d} gross {x.mean():+6.1f} bps net {x.mean()-COST:+6.1f} win {100*(x>0).mean():3.0f}%")
    x = base.r1_short * 100
    print(f"   worst 5 trades (bps): {sorted(x)[:5]} | best 5: {sorted(x)[-5:]} | trades / month {len(base) / (len(days)/21):.1f}")

    print("\n4. ADD-ONS (fixed in advance), base rule 1 day:")
    for nm, m in (("call writers heavier (lean < 0)", base.lean < 0), ("put writers heavier (lean > 0)", base.lean > 0),
                  ("sector also down (median < 0)", base.sec_ret < 0), ("sector up / flat", base.sec_ret >= 0),
                  ("market down that day (avg stock < 0)", base.mkt_ret < 0), ("market up that day", base.mkt_ret >= 0),
                  ("closed near the low (clv < -0.5)", base.clv < -0.5), ("closed off the low (clv >= -0.5)", base.clv >= -0.5),
                  ("volume >= 2x normal", base.vol_rel >= 2), ("volume < 2x normal", base.vol_rel < 2)):
        print("  ", stats(base[m], "r1_short", nm))
    print("\n5. CONTROL: same days, same sizes of fall but OI DOWN (longs leaving, not shorts arriving):")
    ctl = P[(P.oi_chg <= -5) & (P.ret <= -2)].dropna(subset=["r1"])
    print("  ", stats(ctl, "r1_short", "move <= -2 %, OI <= -5 % (long unwinding), short 1 day"))
    ctl2 = P[(P.oi_chg.abs() < 2) & (P.ret <= -2)].dropna(subset=["r1"])
    print("  ", stats(ctl2, "r1_short", "move <= -2 %, OI flat (|OI| < 2 %), short 1 day"))
    base.to_csv(os.path.join(ROOT, "data_store", "research", "fno_short_buildup_trades.csv"), index=False)
