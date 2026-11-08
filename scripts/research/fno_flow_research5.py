"""BUILDUP v1 - round 5: can order blocks / structure, intraday order-flow proxies or a next-morning confirmation improve it? (2026-11-08)

Base = the live rule's trades (round 4): OI >= +10 %, futures <= -2 %, clv < -0.5, |move| <= 20 %, top 5 a day by OI rise; short at the
next open, stop 5 %, out at the close of the 5th session, 0.06 % costs. Each filter splits these trades into PASS / FAIL and we compare.
All filters were written down here BEFORE looking at any result. ~16 filters are tried, so one |t| ~ 2 split is expected by luck alone:
a filter only counts if PASS beats FAIL in BOTH halves of the period and the gap makes economic sense.

A. DAILY STRUCTURE / ORDER BLOCKS (continuous futures series: most-held contract, chained by daily returns - no roll jumps)
   S1 breakdown       close below the lowest low of the previous 20 sessions
   S2 below 50-day    close below its 50-session average close
   S3 bullish OB      most recent bullish order block in the previous 60 sessions = a DOWN candle followed, within 3 sessions, by a
                      close >= its high + 1.5 ATR(14). Signal close vs that zone: BROKEN (below its low) / INSIDE / ABOVE / none found
   S4 supply overhead most recent bearish order block (UP candle, then within 3 sessions a close <= its low - 1.5 ATR) whose low is 0-3 %
                      above the signal close (sellers waiting just above)
   S5 at 60-day low   close within 1 % of the 60-session low (little room left?)
   (all of A need 60 sessions of history before the signal; earlier signals are left out of A rather than counted as FAIL - the first run
    lumped them into FAIL and that flattered "above the 50-day average": fixed 8 Nov)
   S6 ADDED AFTER SEEING S2 (so treat as post-hoc): the stock was UP over the 20 sessions before the signal day
B. SIGNAL-DAY ORDER FLOW (stock 5-min cash bars, data_store/research/buildup_intraday/ - fetched by app/buresearch.py)
   F1 close below the day's VWAP
   F2 last hour (14:30 -> close) down
   F3 volume delta (sum of volume x sign(close - open) per bar / total volume) <= -0.10   (more volume on down bars)
   F4 last hour >= 25 % of the day's volume (heavy selling into the close)
   F5 last 30 min down >= 0.3 %
C. NEXT MORNING (changes the entry; P&L re-done on cash bars with the same stop / hold so both sides are measured the same way)
   E0 cash baseline: enter at the 09:15 open
   E1 enter at 09:30 (close of the 09:25 bar) only if price is still below the signal day's close AND below the 09:15-09:30 VWAP
   E2 opening gap: open below the signal close by more than 2 % (exhaustion?) vs not
D. DOM / book imbalance: the 20-level stock book was recorded only from 3 Nov 2026 - cannot be back-tested; forward-logged instead.
Run: python3 scripts/research/fno_flow_research5.py
"""
from __future__ import annotations

import glob
import importlib.util
import os

import numpy as np
import pandas as pd

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
ID = os.path.join(ROOT, "data_store", "research", "buildup_intraday")
COST = 0.06


def t(x):
    x = pd.Series(x).dropna(); return x.mean() / x.std() * np.sqrt(len(x)) if len(x) > 2 and x.std() else np.nan


def load_r4():
    sp = importlib.util.spec_from_file_location("r4", os.path.join(ROOT, "scripts", "research", "fno_flow_research4.py"))
    m = importlib.util.module_from_spec(sp); sp.loader.exec_module(m); return m


def structure(P):
    """Continuous daily OHLC per stock (chained returns) + structure features for every stock-day."""
    out = []
    for sym, g in P.sort_values("day").groupby("sym"):
        g = g.copy(); g["C"] = (1 + g.ret.fillna(0) / 100).cumprod() * 100
        for k in ("open", "high", "low"):
            g[k[0].upper()] = g[k] / g.close * g.C
        g["tr"] = np.maximum(g.H - g.L, np.maximum((g.H - g.C.shift()).abs(), (g.L - g.C.shift()).abs()))
        g["atr"] = g.tr.rolling(14, min_periods=5).mean()
        g["S1"] = g.C < g.L.shift(1).rolling(20, min_periods=10).min()
        g["S2"] = g.C < g.C.shift(1).rolling(50, min_periods=30).mean()
        g["S5"] = g.C <= g.L.rolling(60, min_periods=20).min() * 1.01
        O, H, L, C, A = g.O.values, g.H.values, g.L.values, g.C.values, g.atr.values
        n = len(g); s3 = ["none"] * n; s4 = [False] * n
        bull, bear = [], []                                                    # (index confirmed at, low, high)
        for i in range(n):
            for j in (i - 1, i - 2, i - 3):                                    # candle j confirmed as an OB once a later close displaces
                if j < 0 or not np.isfinite(A[j]):
                    continue
                if C[j] < O[j] and C[i] >= H[j] + 1.5 * A[j] and all(b[0] != j for b in bull):
                    bull.append((j, L[j], H[j]))
                if C[j] > O[j] and C[i] <= L[j] - 1.5 * A[j] and all(b[0] != j for b in bear):
                    bear.append((j, L[j], H[j]))
            # features for day i use only OBs confirmed BEFORE day i
            bl = [b for b in bull if i - 60 <= b[0] < i - 1]
            if bl:
                _, lo, hi = bl[-1]; s3[i] = "broken" if C[i] < lo else "inside" if C[i] <= hi else "above"
            br = [b for b in bear if i - 60 <= b[0] < i - 1]
            s4[i] = bool(br) and 0 <= br[-1][1] / C[i] - 1 <= 0.03
        g["S3"] = s3; g["S4"] = s4
        g["S6"] = g.C.shift(1) > g.C.shift(21)
        young = np.arange(n) < 60                                              # not enough history: leave out of the structure tests
        for c in ("S1", "S2", "S4", "S5", "S6"):
            g[c] = g[c].astype(object); g.loc[young, c] = None
        g.loc[young, "S3"] = "short history"
        out.append(g[["day", "sym", "S1", "S2", "S3", "S4", "S5", "S6"]])
    return pd.concat(out)


def bars(sym, day):
    fp = os.path.join(ID, f"{sym}_{day}.csv.gz")
    if not os.path.exists(fp):
        return None
    b = pd.read_csv(fp); b["d"] = b.ts.str[:10]; b["m"] = b.ts.str[11:16]
    return b[(b.m >= "09:15") & (b.m <= "15:25")]


def flow(sym, day):
    b = bars(sym, day)
    if b is None:
        return None
    s = b[b.d == day]
    if len(s) < 40:
        return None
    v = s.volume.clip(lower=0); tp = (s.high + s.low + s.close) / 3
    vwap = (tp * v).sum() / v.sum() if v.sum() else np.nan
    c = s.close.iloc[-1]; lh = s[s.m >= "14:30"]; l30 = s[s.m >= "15:00"]
    pre = s[s.m < "14:30"].close.iloc[-1] if (s.m < "14:30").any() else np.nan
    p30 = s[s.m < "15:00"].close.iloc[-1] if (s.m < "15:00").any() else np.nan
    return {"F1": c < vwap, "F2": c < pre, "F3": (v * np.sign(s.close - s.open)).sum() / max(1, v.sum()) <= -0.10,
            "F4": lh.volume.sum() / max(1, v.sum()) >= 0.25, "F5": (c / p30 - 1) * 100 <= -0.3}


def cash_trades(sym, day, hold=5, stop=5.0):
    """E0 / E1 / E2 on cash bars. Returns dict or None."""
    b = bars(sym, day)
    if b is None:
        return None
    days = sorted(b.d.unique());
    if day not in days or days.index(day) + 1 >= len(days):
        return None
    sc = b[b.d == day].close.iloc[-1]; nxt = days[days.index(day) + 1:days.index(day) + 1 + hold]
    if len(nxt) < hold:
        return None
    B = b[b.d.isin(nxt)].reset_index(drop=True)
    d1 = B[B.d == nxt[0]]
    def run(i0, e):
        sp = e * (1 + stop / 100)
        for _, x in B.iloc[i0:].iterrows():
            if x.high >= sp:
                return (e - max(sp, x.open)) / e * 100 - COST
        return (e - B.close.iloc[-1]) / e * 100 - COST
    e0 = d1.open.iloc[0]; r = {"E0": run(0, e0), "gap": (e0 / sc - 1) * 100}
    first = d1[d1.m < "09:30"]
    if len(first) >= 2:
        vw = ((first.high + first.low + first.close) / 3 * first.volume).sum() / max(1, first.volume.sum())
        px = first.close.iloc[-1]; i1 = first.index[-1] + 1
        r["E1ok"] = bool(px < sc and px < vw); r["E1"] = run(i1, px) if r["E1ok"] else np.nan
    return r


def split(T, col, lab, half):
    T = T[T[col].notna()]; m = T[col].astype(bool); a, b = T[m].pnl, T[~m].pnl
    h = lambda x, s: x[s].mean()
    print(f"  {lab:48s} PASS n {len(a):4d} {a.mean():+.2f}% (t {t(a):+.1f}) | FAIL n {len(b):4d} {b.mean():+.2f}% (t {t(b):+.1f}) | "
          f"halves PASS {h(a, T[m].day < half):+.2f}/{h(a, T[m].day >= half):+.2f}  FAIL {h(b, T[~m].day < half):+.2f}/{h(b, T[~m].day >= half):+.2f}")


if __name__ == "__main__":
    r4 = load_r4(); P = r4.P[r4.P.ret.abs() <= 20]
    base = P[(P.oi_chg >= 10) & (P.ret <= -2)].sort_values(["day", "oi_chg"], ascending=[True, False]).groupby("day").head(5)
    rule = base[base.clv < -0.5]
    T = r4.simulate(rule, 5, 5.0); half = r4.half
    print(f"BuildUp v1 trades: n {len(T)}, avg {T.pnl.mean():+.2f}% (t {t(T.pnl):+.1f})\n")
    ST = structure(r4.P); T = T.merge(ST, on=["day", "sym"], how="left")
    print("A. DAILY STRUCTURE / ORDER BLOCKS")
    for c, lab in (("S1", "S1 breakdown below the 20-day low"), ("S2", "S2 below the 50-day average"), ("S4", "S4 supply (bearish OB) 0-3 % overhead"),
                   ("S5", "S5 already within 1 % of the 60-day low"), ("S6", "S6 (post-hoc) up over the previous 20 sessions")):
        split(T, c, lab, half)
    for k in ("broken", "inside", "above", "none", "short history"):
        x = T[T.S3 == k].pnl
        print(f"  S3 bullish OB {k:13s} n {len(x):4d} {x.mean():+.2f}% (t {t(x):+.1f}) halves {x[T[T.S3==k].day<half].mean():+.2f}/{x[T[T.S3==k].day>=half].mean():+.2f}")
    print("\nB. SIGNAL-DAY ORDER FLOW (5-min cash bars)")
    FL = [dict(day=r.day, sym=r.sym, **f) for r in T.itertuples() if (f := flow(r.sym, r.day))]
    TF = T.merge(pd.DataFrame(FL), on=["day", "sym"]) if FL else T.iloc[:0]
    print(f"  trades with bars: {len(TF)} of {len(T)}")
    for c, lab in (("F1", "F1 closed below VWAP"), ("F2", "F2 last hour down"), ("F3", "F3 volume delta <= -0.10"),
                   ("F4", "F4 last hour >= 25 % of volume"), ("F5", "F5 last 30 min down >= 0.3 %")):
        if len(TF):
            split(TF, c, lab, half)
    print("\nC. NEXT MORNING (cash bars, same stop / hold)")
    CT = [dict(day=r.day, sym=r.sym, **c) for r in T.itertuples() if (c := cash_trades(r.sym, r.day))]
    if CT:
        C = pd.DataFrame(CT); h1 = C.day < half
        print(f"  E0 enter 09:15 open (cash baseline)   n {len(C):4d} {C.E0.mean():+.2f}% (t {t(C.E0):+.1f}) halves {C.E0[h1].mean():+.2f}/{C.E0[~h1].mean():+.2f}")
        ok = C.E1ok.fillna(False).astype(bool)
        print(f"  E1 enter 09:30 if still weak          n {ok.sum():4d} {C.E1[ok].mean():+.2f}% (t {t(C.E1[ok]):+.1f}) halves {C.E1[ok & h1].mean():+.2f}/{C.E1[ok & ~h1].mean():+.2f}"
              f" | skipped ones at the open would have made {C.E0[~ok].mean():+.2f}% (n {(~ok).sum()})")
        g = C.gap < -2
        print(f"  E2 gap down > 2 %                     n {g.sum():4d} {C.E0[g].mean():+.2f}% | gap >= -2 % n {(~g).sum():4d} {C.E0[~g].mean():+.2f}%")
    T.to_csv(os.path.join(ROOT, "data_store", "research", "buildup_round5_trades.csv"), index=False)
    if len(TF):
        TF.to_csv(os.path.join(ROOT, "data_store", "research", "buildup_round5_flow.csv"), index=False)
