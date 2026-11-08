"""F&O FLOW - round 2: where is the edge concentrated? (2026-11-08). Reads data_store/research/fno_flow_panel.csv.gz from round 1.

Round 1 said: build-up classes carry no stock-specific edge; daily quintile long-short spreads are a few bps (far below costs); the only
statistically clear effects were (a) today's return -> overnight continuation then intraday reversal, (b) option-writer lean (put OI added
minus call OI added) -> next-day open-to-close and 5-day returns, (c) market-wide lean -> Nifty next day (weak).
Round 2 asks whether the EXTREMES are big enough to pay costs, and tests two classic F&O ideas round 1 did not:
  A. decile / tail returns for the round-1 survivors, per leg, net of 0.06 % round trip per leg
  B. composite: lean + today's move + OI change, out-of-sample (weights fixed: equal z-scores, no fitting)
  C. expiry pinning: in the last 3 sessions before monthly expiry, does a stock drift toward its max-OI strike?
  D. big fresh-position events: |OI change| >= 15 % with |move| >= 3 % -> next 1 / 5 days (continuation or reversal?)
  E. index: Nifty / Bank Nifty next day after their own short covering / market lean extremes
Run: python3 scripts/research/fno_flow_research2.py
"""
from __future__ import annotations

import glob
import os

import numpy as np
import pandas as pd

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
P = pd.read_csv(os.path.join(ROOT, "data_store", "research", "fno_flow_panel.csv.gz"))
H = os.path.join(ROOT, "data_store", "research", "fno_hist")
LEG = 6.0              # bps, round trip per leg (futures)
days = sorted(P.day.unique()); half = days[len(days) // 2]


def tstat(x):
    x = pd.Series(x).dropna(); return x.mean() / x.std() * np.sqrt(len(x)) if len(x) > 2 and x.std() else np.nan


def deciles(sig, tgt, label, n=10):
    d = P.dropna(subset=[sig, tgt]).copy()
    d["q"] = (d.groupby("day")[sig].rank(method="first", pct=True) * n).clip(upper=n - 0.001).astype(int)
    d["x"] = d[tgt] - d.groupby("day")[tgt].transform("mean")
    m = d.groupby("q").agg(raw=(tgt, "mean"), rel=("x", "mean"))
    m1 = d[d.day < half].groupby("q").x.mean(); m2 = d[d.day >= half].groupby("q").x.mean()
    print(f"  {label}: decile of {sig} -> {tgt} (bps; rel = vs that day's average stock)")
    print("    decile   " + " ".join(f"{k:>6d}" for k in range(n)))
    print("    raw      " + " ".join(f"{100*v:+6.1f}" for v in m.raw))
    print("    rel      " + " ".join(f"{100*v:+6.1f}" for v in m.rel))
    print("    rel 1st  " + " ".join(f"{100*v:+6.1f}" for v in m1))
    print("    rel 2nd  " + " ".join(f"{100*v:+6.1f}" for v in m2))
    top = d[d.q == n - 1].groupby("day").x.mean() * 100; bot = d[d.q == 0].groupby("day").x.mean() * 100
    print(f"    top decile long, vs market: {top.mean():+.1f} bps/trade (t {tstat(top):+.1f}) -> net {top.mean() - LEG:+.1f} | bottom decile short: {-bot.mean():+.1f} bps (t {tstat(-bot):+.1f}) -> net {-bot.mean() - LEG:+.1f}")
    return d


if __name__ == "__main__":
    P["r1x"] = P.r1 - P.groupby("day").r1.transform("mean")
    print(f"panel {len(P):,} stock-days, {len(days)} days\n")
    print("A. TAILS")
    deciles("ret", "ron", "today's move -> overnight")
    deciles("ret", "rid", "today's move -> next day open-close")
    deciles("lean", "rid", "writer lean -> next day open-close")
    deciles("lean", "r5", "writer lean -> 5 days")
    deciles("oi_chg", "r5", "OI change -> 5 days")
    deciles("vol_rel", "r1", "volume surge -> next day")
    print("\nB. COMPOSITE (equal z-scores, no fitting): lean + 0.5 x signed OI change, for next-day open-close and 5 days")
    z = lambda c: P.groupby("day")[c].transform(lambda x: (x - x.mean()) / x.std())
    P["comp"] = z("lean").fillna(0) + 0.5 * (np.sign(P.ret) * z("oi_chg")).fillna(0)
    deciles("comp", "rid", "composite -> next day open-close")
    deciles("comp", "r5", "composite -> 5 days")

    print("\nC. EXPIRY PINNING: last 3 sessions before monthly expiry, distance to the max-OI strike")
    O = pd.concat([pd.read_csv(f) for f in sorted(glob.glob(os.path.join(H, "opt_*.csv.gz")))], ignore_index=True)
    O["dte"] = (pd.to_datetime(O.expiry) - pd.to_datetime(O.day)).dt.days
    O["pin"] = np.where(O.sup_oi.fillna(0) >= O.res_oi.fillna(0), O.sup, O.res)          # the bigger of the two walls around spot
    Q = O[(O.dte >= 1) & (O.dte <= 4)].merge(P[["day", "sym", "r1", "ret"]], on=["day", "sym"])
    Q["gap"] = (Q.pin / Q.und - 1) * 100                                               # + = pin above spot
    Q = Q[Q.gap.abs().between(0.5, 4)]
    Q["toward"] = np.sign(Q.gap) * Q.r1
    print(f"  n {len(Q):,} stock-days | next day moved toward the pin {100*(Q.toward > 0).mean():.1f}% | avg {Q.toward.mean()*100:+.1f} bps (t {tstat(Q.toward):+.1f}) "
          f"| 1st half {Q[Q.day < half].toward.mean()*100:+.1f} / 2nd {Q[Q.day >= half].toward.mean()*100:+.1f}")
    for lo, hi in ((0.5, 1), (1, 2), (2, 4)):
        q = Q[Q.gap.abs().between(lo, hi)]
        print(f"    gap {lo}-{hi} %: n {len(q):5d}  toward {q.toward.mean()*100:+6.1f} bps (t {tstat(q.toward):+.1f})")

    print("\nD. BIG FRESH-POSITION EVENTS (|OI change| >= 15 % and |move| >= 3 %)")
    E = P[(P.oi_chg.abs() >= 15) & (P.ret.abs() >= 3)].copy()
    for nm, m in (("long build-up (up, OI up)", (E.ret > 0) & (E.oi_chg > 0)), ("short build-up (down, OI up)", (E.ret < 0) & (E.oi_chg > 0)),
                  ("short covering (up, OI down)", (E.ret > 0) & (E.oi_chg < 0)), ("long unwinding (down, OI down)", (E.ret < 0) & (E.oi_chg < 0))):
        e = E[m]; sd = np.sign(e.ret)
        cont1 = sd * e.r1x; cont5 = sd * (e.r5 - P.loc[e.index, "r5"].groupby(e.day).transform("mean") * 0)
        print(f"  {nm:32s} n {len(e):4d} | next day in the move's direction, vs market {cont1.mean()*100:+6.1f} bps (t {tstat(cont1):+.1f}) "
              f"| overnight {(sd*e.ron).mean()*100:+6.1f} | 5 days {(sd*e.r5).mean()*100:+6.1f} bps (t {tstat(sd*e.r5):+.1f}) | halves 1d {(sd*e.r1x)[e.day<half].mean()*100:+.1f}/{(sd*e.r1x)[e.day>=half].mean()*100:+.1f}")

    print("\nE. INDEX NEXT DAY")
    import importlib.util
    sp = importlib.util.spec_from_file_location("r1", os.path.join(ROOT, "scripts", "research", "fno_flow_research.py")); r1 = importlib.util.module_from_spec(sp); sp.loader.exec_module(r1)
    FF, OO = r1.load(); ALL = r1.panel(FF, OO); IX = ALL[ALL.kind == "IDF"]
    for ix in ("NIFTY", "BANKNIFTY"):
        I = IX[IX.sym == ix] if (IX.sym == ix).any() else None
        if I is None or not len(I):
            continue
        g = I.groupby("bu")
        print(f"  {ix}: own build-up -> next day " + " | ".join(f"{k} {v.r1.mean()*100:+.0f} bps (n {len(v)}, t {tstat(v.r1):+.1f})" for k, v in g))
    M = P[P.kind == "STF"].groupby("day").lean.median().rename("mlean").to_frame().join(IX[IX.sym == "NIFTY"].set_index("day")[["r1", "rid", "ron"]])
    M = M.dropna(); q = pd.qcut(M.mlean, 5, labels=False)
    print("  market lean quintile -> Nifty next day (bps): " + " ".join(f"{M.r1[q==k].mean()*100:+.0f}" for k in range(5))
          + " | open-close: " + " ".join(f"{M.rid[q==k].mean()*100:+.0f}" for k in range(5)) + f" | n/quintile {len(M)//5}")
    P.to_csv(os.path.join(ROOT, "data_store", "research", "fno_flow_panel.csv.gz"), index=False, compression="gzip")
