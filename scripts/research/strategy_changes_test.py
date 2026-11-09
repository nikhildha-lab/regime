"""STRATEGY CHANGES TEST (2026-11-09, Nikhil: "prepare list of changes to these strategies and test also with new changes").

Changes come from the trade reviews in the paper trade log (what the Improve column kept saying). Each is tested as a NEW VERSION beside
the current rule, on the same signals, with the same costs. Rules fixed before running; nothing tuned on the result.

A. STOCKS IN PLAY ORB - "the gain was given back" (6 of 7 live winners). Same S1 signals as stock_intraday_tests.py (49 F&O stocks,
   1-min Dhan bars 13 Jul - 6 Nov 2026, 5-min decisions, max 3 a day, MIS 5x, costs 0.12 % round trip). Exit versions:
     base      half off at 1.5R + stop to entry, rest out on a 5-min close back through VWAP, flat 15:10   (the live rule)
     trail1    half at 1.5R, rest trails 1 x ATR(14, 5-min) behind the best close
     trail1.5  half at 1.5R, rest trails 1.5 x ATR
     all2R     everything out at 2R (or stop / 15:10)
     half+3R   half at 1.5R, rest at 3R (else stop at entry / 15:10)
B. DOM STOCKS - "target never reached" (194 of 396), "traded against the day's move" (60). Every recorded DOM stock trade
   (data_store/livedesk/domcards/trades.json) replayed from its entry minute on the stock's 1-min bars (stop / target on the bar's
   low / high, stop wins a tie, 60-min time exit, flat 15:20; costs 0.08 %). Versions:
     base      target 2R (the live rule - should match the recorded result roughly; recorded fills were 5-s mids)
     tgt1.5R / tgt1R      nearer targets
     time30    2R target but out after 30 min
     dayfilt   skip the trade when it is against the day's move at entry (price vs today's open: > +0.5 % -> no shorts, < -0.5 % -> no longs)
     tgt1.5R+dayfilt
C. TOUCH v2 - "skip trades against a strong day move": already tested on 5 Nov in scripts/research/touch_trend_filters.py (filter A2);
   the result is quoted in the report, not re-run.
Outputs: data_store/research/strategy_changes/{orb_variants.csv, dom_stock_variants.csv, report.txt}
Run: python3 scripts/research/strategy_changes_test.py
"""
from __future__ import annotations

import glob
import json
import os

import numpy as np
import pandas as pd

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
SD = os.path.join(ROOT, "data_store", "research", "stocks_intraday")
TR = os.path.join(ROOT, "data_store", "livedesk", "domcards", "trades.json")
OUT = os.path.join(ROOT, "data_store", "research", "strategy_changes")
COST_ORB, COST_DOM = 0.12, 0.08


def t(x):
    x = pd.Series(x).dropna(); return x.mean() / x.std() * np.sqrt(len(x)) if len(x) > 2 and x.std() else np.nan


def five(d1):
    d1 = d1.set_index("ts").sort_index()
    return d1.resample("5min", origin="start_day", offset="9h15min", label="left", closed="left").agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}).dropna()


def feats(b):
    b = b.copy(); b["day"] = b.index.normalize(); b["hm"] = b.index.strftime("%H:%M")
    tp = (b.high + b.low + b.close) / 3
    b["vwap"] = (tp * b.volume).groupby(b.day).cumsum() / b.volume.groupby(b.day).cumsum()
    tr = np.maximum(b.high - b.low, np.maximum((b.high - b.close.shift()).abs(), (b.low - b.close.shift()).abs()))
    b["atr"] = tr.rolling(14).mean()
    return b


# ------------------------------------------------------------------ A. ORB exits
def orb_exit(b, i, s, e, stop, mode):
    R = abs(e - stop); left, pnl, cur = 1.0, 0.0, stop
    H, L, C, V, HM, A = b.high.values, b.low.values, b.close.values, b.vwap.values, b.hm.values, b.atr.values
    day = b.day.values[i]; D = b.day.values; best = e
    for k in range(i + 1, len(b)):
        if D[k] != day:
            return pnl + left * s * (C[k - 1] - e)
        if HM[k] >= "15:10":
            return pnl + left * s * (C[k] - e)
        if (s > 0 and L[k] <= cur) or (s < 0 and H[k] >= cur):
            return pnl + left * s * (cur - e)
        if mode == "all2R":
            if (s > 0 and H[k] >= e + 2 * R) or (s < 0 and L[k] <= e - 2 * R):
                return s * 2 * R
            continue
        if left == 1.0 and ((s > 0 and H[k] >= e + 1.5 * R) or (s < 0 and L[k] <= e - 1.5 * R)):
            pnl += 0.5 * 1.5 * R; left = 0.5; cur = e
        if left == 0.5:
            if mode == "base" and s * (C[k] - V[k]) < 0:
                return pnl + left * s * (C[k] - e)
            if mode == "half+3R" and ((s > 0 and H[k] >= e + 3 * R) or (s < 0 and L[k] <= e - 3 * R)):
                return pnl + left * s * 3 * R
            if mode in ("trail1", "trail1.5"):
                best = max(best, C[k]) if s > 0 else min(best, C[k])
                m = 1.0 if mode == "trail1" else 1.5
                ns = best - s * m * A[k]
                cur = max(cur, ns) if s > 0 else min(cur, ns)
    return pnl + left * s * (C[-1] - e)


def orb():
    rows = []
    for f in sorted(glob.glob(os.path.join(SD, "*_1m.csv.gz"))):
        sym = os.path.basename(f)[:-10]
        d = pd.read_csv(f, parse_dates=["ts"]); b = feats(five(d)); days = sorted(b.day.unique())
        orv = b[b.hm < "09:30"].groupby("day").volume.sum(); orh = b[b.hm < "09:30"].groupby("day").high.max(); orl = b[b.hm < "09:30"].groupby("day").low.min()
        for j, dday in enumerate(days):
            if j < 20:
                continue
            avg = orv.reindex(days[j - 20:j]).mean(); prevc = b[b.day == days[j - 1]].close.iloc[-1]
            today = b[b.day == dday]
            if not len(today) or avg <= 0:
                continue
            rv = orv.get(dday, 0) / avg; gap = today.open.iloc[0] / prevc - 1
            if rv < 2 or abs(gap) < 0.005:
                continue
            s = 1 if gap > 0 else -1; hi, lo = orh[dday], orl[dday]
            for i in np.where((b.day.values == dday) & (b.hm.values >= "09:30") & (b.hm.values <= "11:30"))[0]:
                c = b.close.values[i]
                if (s > 0 and c > hi) or (s < 0 and c < lo):
                    a = b.atr.values[i]; st_or = lo if s > 0 else hi; st_atr = c - s * a
                    stop = st_or if abs(c - st_or) < abs(c - st_atr) else st_atr
                    if s * (c - stop) <= 0:
                        break
                    r = {"sym": sym, "day": str(dday)[:10], "t": str(b.index[i]), "rv": rv}
                    for mode in ("base", "trail1", "trail1.5", "all2R", "half+3R"):
                        r[mode] = orb_exit(b, i, s, c, stop, mode) / c * 100 - COST_ORB
                    rows.append(r)
                    break
    D = pd.DataFrame(rows)
    D = D.sort_values(["day", "rv"], ascending=[True, False]).groupby("day").head(3)
    return D


# ------------------------------------------------------------------ B. DOM stock exits
def dom_stocks():
    T = [x for x in json.load(open(TR)) if x["key"] not in ("NIFTY", "BANKNIFTY") and x["status"] == "closed"]
    bars = {}
    rows = []
    for x in T:
        sym = x["key"]
        if sym not in bars:
            p = os.path.join(SD, f"{sym}_1m.csv.gz")
            bars[sym] = pd.read_csv(p, parse_dates=["ts"]).set_index("ts").sort_index() if os.path.exists(p) else None
        b = bars[sym]
        if b is None:
            continue
        t0 = pd.Timestamp(x["opened"]); day = b[b.index.normalize() == t0.normalize()]
        if day.empty or t0 > day.index[-1]:
            continue
        after = day[day.index > t0]; sd = 1 if x["side"] == "long" else -1; e, stop, risk = x["entry"], x["stop"], x["risk"]
        op = float(day.open.iloc[0]); daymove = e / op - 1
        against = (sd < 0 and daymove > 0.005) or (sd > 0 and daymove < -0.005)
        def sim(rr, maxmin):
            tgt = e + sd * rr * risk; tend = t0 + pd.Timedelta(minutes=maxmin)
            for ts, r in after.iterrows():
                if ts.strftime("%H:%M") >= "15:20":
                    return (r.close - e) * sd, "flat"
                if (sd > 0 and r.low <= stop) or (sd < 0 and r.high >= stop):
                    return -risk, "stop"
                if (sd > 0 and r.high >= tgt) or (sd < 0 and r.low <= tgt):
                    return rr * risk, "target"
                if ts >= tend:
                    return (r.close - e) * sd, "time"
            return (after.close.iloc[-1] - e) * sd if len(after) else 0.0, "end"
        out = {"sym": sym, "day": x["day"], "opened": x["opened"], "side": x["side"], "against": against, "rec_r": x.get("r"), "rec_pnl": x.get("pnl"), "risk_pct": risk / e * 100}
        for nm, (rr, mm) in {"base": (2.0, 60), "tgt1.5R": (1.5, 60), "tgt1R": (1.0, 60), "time30": (2.0, 30)}.items():
            pts, why = sim(rr, mm)
            out[nm] = pts / e * 100 - COST_DOM; out[nm + "_why"] = why
        rows.append(out)
    return pd.DataFrame(rows)


def main():
    os.makedirs(OUT, exist_ok=True)
    L = ["STRATEGY CHANGES TEST - new versions beside the live rule, same signals, same costs\n"]
    # A
    A = orb(); A.to_csv(os.path.join(OUT, "orb_variants.csv"), index=False)
    days = sorted(A.day.unique()); cut = days[int(len(days) * 0.7)]
    L.append(f"A. STOCKS IN PLAY ORB · {len(A)} trades · {days[0]} .. {days[-1]} · % of position per trade after 0.12 % costs (x Rs 1,00,000 = Rs; MIS 5x on Rs 20,000)")
    L.append(f"   {'exit version':12s} {'avg %':>7s} {'win':>5s} {'t':>6s} {'Rs/trade':>9s} {'total Rs':>10s} | train avg / test avg (cut {cut})")
    for m in ("base", "trail1", "trail1.5", "all2R", "half+3R"):
        a, b_ = A[A.day < cut][m], A[A.day >= cut][m]
        L.append(f"   {m:12s} {A[m].mean():+7.3f} {100*(A[m]>0).mean():4.0f}% {t(A[m]):+6.1f} {1000*A[m].mean():+9.0f} {1000*A[m].sum():+10.0f} | {a.mean():+.3f} / {b_.mean():+.3f}")
    L.append("   (live record so far: 7 trades, 6 won, every winner smaller at the exit than at its best point)")
    # B
    B = dom_stocks(); B.to_csv(os.path.join(OUT, "dom_stock_variants.csv"), index=False)
    L.append(f"\nB. DOM STOCKS · {len(B)} recorded trades replayed on 1-min bars · % of position per trade after 0.08 % costs · Rs = % x risk-normalised: the rule risks Rs 1,000 a trade -> Rs = % / risk% x 1,000")
    B["rs_base"] = B.base / B.risk_pct * 1000
    L.append(f"   check: recorded avg R {B.rec_r.mean():+.2f} vs replayed base {(B.base / B.risk_pct).mean():+.2f} (1-min bars vs 5-s mids)")
    L.append(f"   {'version':18s} {'n':>4s} {'avg R':>6s} {'win':>5s} {'t':>6s} {'total Rs':>9s} {'stops':>6s} {'targets':>8s}")
    def line(nm, X, col):
        R = X[col] / X.risk_pct
        why = X[col + "_why"] if col + "_why" in X else X["base_why"]
        L.append(f"   {nm:18s} {len(X):4d} {R.mean():+6.2f} {100*(R>0).mean():4.0f}% {t(R):+6.1f} {1000*R.sum():+9.0f} {100*(why=='stop').mean():5.0f}% {100*(why=='target').mean():7.0f}%")
    for nm in ("base", "tgt1.5R", "tgt1R", "time30"):
        line(nm, B, nm)
    line("dayfilt (2R)", B[~B.against], "base")
    line("tgt1.5R+dayfilt", B[~B.against], "tgt1.5R")
    line("tgt1R+dayfilt", B[~B.against], "tgt1R")
    L.append(f"   trades against the day's move: {int(B.against.sum())} of {len(B)} · their base avg R {(B[B.against].base / B[B.against].risk_pct).mean():+.2f}")
    # C
    L.append("\nC. TOUCH v2 · 'skip trades against a strong day move' = filter A2 in touch_trend_filters.py (5 Nov) - see the report quoted in claude/touch-minute-observations.md; not re-run here.")
    txt = "\n".join(L); print(txt); open(os.path.join(OUT, "report.txt"), "w").write(txt)


if __name__ == "__main__":
    main()
