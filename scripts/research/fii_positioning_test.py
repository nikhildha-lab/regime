"""Does FII index-futures positioning predict NIFTY? (2026-11-08, Options Lab "Institutions" section; idea from MrChartist's FII/DII repo,
whose rules were never tested). Data: NSE participant-wise OI (data_store/market_data/nse_participant_oi_2023_2026.csv), NIFTY daily (Dhan).
Signals are known after ~18:00 IST, so every trade starts at the NEXT session's open. Fixed before looking:
  A  FII index-futures long share, ranked against its own previous 250 sessions (no look-ahead) -> quintiles -> NIFTY next open -> close
     after 1, 3 and 5 sessions
  B  extremes: rank <= 10 % (FII very short) vs >= 90 %
  C  daily change in FII net index futures (contracts), ranked the same way -> quintiles
  D  contrarian: Client long share in its top 10 % while FII in its bottom 10 % (and the reverse)
Mean in bps, t-stat, both halves. Gross (an index future round trip costs ~2-3 bps).
Run: python3 scripts/research/fii_positioning_test.py
"""
from __future__ import annotations

import os

import numpy as np
import pandas as pd

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
P = pd.read_csv(os.path.join(ROOT, "data_store", "market_data", "nse_participant_oi_2023_2026.csv"))
N = pd.read_csv(os.path.join(ROOT, "data_store", "research", "nifty_monthly", "nifty_daily.csv")).set_index("day").sort_index()


def t(x):
    x = pd.Series(x).dropna(); return x.mean() / x.std() * np.sqrt(len(x)) if len(x) > 2 and x.std() else np.nan


def prank(s, n=250):
    return s.rolling(n + 1, min_periods=120).apply(lambda w: (w[:-1] < w[-1]).mean(), raw=True)


if __name__ == "__main__":
    P["L"] = P.fut_idx_long / (P.fut_idx_long + P.fut_idx_short); P["net"] = P.fut_idx_long - P.fut_idx_short
    W = P.pivot(index="date", columns="client_type", values="L").join(P[P.client_type == "FII"].set_index("date").net.rename("fii_net")).sort_index()
    W["fii_rank"] = prank(W.FII); W["cli_rank"] = prank(W.Client); W["chg_rank"] = prank(W.fii_net.diff())
    days = list(N.index); pos = {d: i for i, d in enumerate(days)}
    for k in (1, 3, 5):
        W[f"r{k}"] = [((N.close.iloc[pos[d] + k] / N.open.iloc[pos[d] + 1] - 1) * 1e4) if d in pos and pos[d] + k < len(days) else np.nan for d in W.index]
    W = W.dropna(subset=["fii_rank", "r1"]); half = W.index[len(W) // 2]
    print(f"sessions with a signal: {len(W)} ({W.index[0]} .. {W.index[-1]}); NIFTY average next-day open->close {W.r1.mean():+.1f} bps\n")
    def row(lab, m):
        x = W[m]
        print(f"  {lab:46s} n {len(x):4d} | " + " | ".join(f"{k}d {x[f'r{k}'].mean():+6.1f} bps (t {t(x[f'r{k}']):+.1f})" for k in (1, 3, 5))
              + f" | halves 5d {x[x.index < half].r5.mean():+.0f} / {x[x.index >= half].r5.mean():+.0f}")
    print("A. FII index-futures long share, rank vs its last 250 sessions (quintile 1 = most short)")
    q = (W.fii_rank * 5).clip(upper=4.999).astype(int)
    for i in range(5):
        row(f"quintile {i + 1}", q == i)
    print("\nB. extremes")
    row("FII very short (rank <= 10 %)", W.fii_rank <= 0.10); row("FII very long (rank >= 90 %)", W.fii_rank >= 0.90)
    print("\nC. daily change in FII net index futures, rank (quintile 1 = biggest cut)")
    q2 = (W.chg_rank.fillna(-1) * 5).clip(upper=4.999).astype(int)
    for i in range(5):
        row(f"quintile {i + 1}", q2 == i)
    print("\nD. contrarian combos")
    row("Clients very long + FII very short", (W.cli_rank >= 0.9) & (W.fii_rank <= 0.1)); row("Clients very short + FII very long", (W.cli_rank <= 0.1) & (W.fii_rank >= 0.9))
    row("all sessions", W.r1.notna())
