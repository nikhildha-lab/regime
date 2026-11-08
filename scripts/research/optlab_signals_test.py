"""OPTIONS LAB -> stock futures / MIS signals (2026-11-08, Nikhil: "help me interpret all of them and can we trade using this info ..
deep research and experimentation .. options, futures, stocks MIS, spreads").

Question: do the Options Lab readings for a stock (skew, IV moves, term structure, unusual option volume, put/call volume, rollover,
IV rank) predict that stock's NEXT moves? Everything is known after the close of day t; trades start at the next open (no look-ahead).
Signals fixed BEFORE looking (no tuning):
  skew        put skew level (IV 5 % OTM put minus 5 % OTM call)                 [iv_hist]
  dskew       1-day change in skew
  term        near IV / next IV - 1   (> 0 = near-term options priced higher = inverted)
  div         1-day change in near ATM IV (IV jump)
  ivhv        near IV / 20-day realised volatility
  ivrank      IV rank vs the last 252 sessions (needs 120)
  optvol      total option volume / its last 20 sessions                         [fno_hist opt aggregates]
  pcv         put volume / call volume
  dpcoi       put OI added minus call OI added, scaled by total OI
Targets (stock futures, from the BuildUp panel): intraday next open -> close ("MIS"), 5 sessions next open -> close of day 5 ("swing").
Method: each day, rank stocks into deciles of the signal; report top-decile and bottom-decile returns VS the day's average stock
(market-neutral) and raw, gross and after costs (0.05 % a side = 0.10 % per trade, the backtester rule), split 70 / 30 by date
(train = first 70 % of sessions, test = last 30 %). A signal counts only if the SAME sign holds in train and test and t >= 2 in train.
9 signals x 2 horizons x 2 tails = 36 checks: about 2 would pass |t| >= 2 by luck alone.
Run: python3 scripts/research/optlab_signals_test.py
"""
from __future__ import annotations

import glob
import os

import numpy as np
import pandas as pd

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
R = os.path.join(ROOT, "data_store", "research")
COST = 0.10                     # % round trip per leg (0.05 % a side)


def t(x):
    x = pd.Series(x).dropna(); return x.mean() / x.std() * np.sqrt(len(x)) if len(x) > 2 and x.std() else np.nan


def load():
    P = pd.read_csv(os.path.join(R, "fno_flow_panel.csv.gz"))
    P = P[P.kind == "STF"] if "kind" in P else P
    P["mis"] = (P.c1 / P.o1 - 1) * 100 if "o1" in P else P.rid * 100
    P["sw5"] = (P.c5 / P.o1 - 1) * 100 if "c5" in P and "o1" in P else P.r5 * 100
    IV = pd.concat([pd.read_csv(f) for f in sorted(glob.glob(os.path.join(R, "iv_hist", "iv_*.csv.gz")))], ignore_index=True).sort_values(["sym", "day"])
    g = IV.groupby("sym")
    IV["dskew"] = g["skew"].diff(); IV["div"] = g.near_iv.diff(); IV["term"] = IV.near_iv / IV.next_iv - 1
    IV["rv20"] = g.und.transform(lambda s: np.log(s).diff().rolling(20).std() * np.sqrt(252) * 100); IV["ivhv"] = IV.near_iv / IV.rv20
    IV["ivrank"] = g.near_iv.transform(lambda s: s.rolling(253, min_periods=121).apply(lambda w: (w[:-1] < w[-1]).mean(), raw=True))
    O = pd.concat([pd.read_csv(f, usecols=["day", "sym", "ce_oi", "pe_oi", "ce_chg", "pe_chg", "ce_vol", "pe_vol"]) for f in sorted(glob.glob(os.path.join(R, "fno_hist", "opt_*.csv.gz")))], ignore_index=True).sort_values(["sym", "day"])
    O["v"] = O.ce_vol + O.pe_vol; O["optvol"] = O.v / O.groupby("sym").v.transform(lambda s: s.shift(1).rolling(20, min_periods=10).mean())
    O["pcv"] = O.pe_vol / O.ce_vol.replace(0, np.nan); O["dpcoi"] = (O.pe_chg - O.ce_chg) / (O.ce_oi + O.pe_oi).replace(0, np.nan)
    X = P.merge(IV[["day", "sym", "skew", "dskew", "term", "div", "ivhv", "ivrank"]], on=["day", "sym"], how="left") \
         .merge(O[["day", "sym", "optvol", "pcv", "dpcoi"]], on=["day", "sym"], how="left")
    return X


if __name__ == "__main__":
    X = load(); days = sorted(X.day.unique()); cut = days[int(len(days) * 0.7)]
    for tg in ("mis", "sw5"):
        X[tg + "_x"] = X[tg] - X.groupby("day")[tg].transform("mean")
    print(f"stock-days {len(X):,} · sessions {len(days)} · train < {cut} <= test\n")
    SIG = ["skew", "dskew", "term", "div", "ivhv", "ivrank", "optvol", "pcv", "dpcoi"]
    rows = []
    for s in SIG:
        d = X.dropna(subset=[s, "mis", "sw5"]).copy()
        d["q"] = (d.groupby("day")[s].rank(pct=True) * 10).clip(upper=9.999).astype(int)
        for tg in ("mis", "sw5"):
            for tail, qq in (("top", 9), ("bottom", 0)):
                e = d[d.q == qq]; per = e.groupby("day")[tg + "_x"].mean(); raw = e.groupby("day")[tg].mean()
                tr, te = per[per.index < cut], per[per.index >= cut]
                rows.append({"signal": s, "target": tg, "tail": tail, "n_days": len(per), "vs_mkt": per.mean(), "t": t(per), "train": tr.mean(), "t_train": t(tr),
                             "test": te.mean(), "t_test": t(te), "raw": raw.mean()})
    T = pd.DataFrame(rows)
    T["passes"] = (T.t_train.abs() >= 2) & (np.sign(T.train) == np.sign(T.test))
    pd.set_option("display.width", 250)
    print("Per day, the decile's average return vs the average stock (%), gross. Cost per trade 0.10 %.")
    print(T.round(3).to_string(index=False))
    print("\nPASSING (|t| train >= 2 and same sign in test):")
    print(T[T.passes].round(3).to_string(index=False) if T.passes.any() else "  none")
    T.to_csv(os.path.join(R, "optlab_signals_test.csv"), index=False)
