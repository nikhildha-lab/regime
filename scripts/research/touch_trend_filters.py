"""TOUCH-MINUTE + TREND / LEVEL FILTERS - history check (2026-11-05, Nikhil: "Why touch min didn't work today ... update the strategy").
Paper research only. Base = the touch-minute signal as replayed in touch_dom_combo.py (futures 1-min bars from the per-second
files, same levels, same stop / breakeven / trail / 30-min exit, synthetic future 1 lot, 4.5 / 12.5 pts cost).
Filters fixed BEFORE running (from the 5 Nov observations, claude/touch-minute-observations.md):
  A1 gap    : opening gap (today's first 1-min open vs yesterday's last close) >= 1 % -> trade only in the gap direction
  A2 day    : move from today's open to the entry >= +0.5 % -> longs only; <= -0.5 % -> shorts only
  A3 15m    : last COMPLETED 15-min bar (bars across days): close > EMA21 and EMA9 > EMA21 -> longs only; mirror -> shorts only
  A         : A1 and A2 and A3 together (skip anything one of them calls counter-trend)
  C level   : previous-session levels only (PDH / PDL / PDC / pPOC / pVAH / pVAL) - no round numbers / IB
One open trade per index after filtering (a trade blocks new entries until its exit).
"""
import os, sys
import numpy as np, pandas as pd
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import touch_dom_combo as TD

def feats():
    days = sorted(d for d in os.listdir(TD.OF) if len(d) == 10 and d[4] == "-")
    F = {}
    for sym in ("NIFTY", "BANKNIFTY"):
        allb, prev_c = [], None
        for day in days:
            try:
                B = TD.bars(day, sym)
            except Exception:
                continue
            if len(B) < 60:
                continue
            B = B.assign(day=day, gap=(B.o.iloc[0] / prev_c - 1) if prev_c else np.nan, open_=B.o.iloc[0]); prev_c = B.c.iloc[-1]
            allb.append(B[["t", "day", "time", "o", "h", "l", "c", "gap", "open_"]])
        A = pd.concat(allb, ignore_index=True)
        A["ts"] = pd.to_datetime(A.t, unit="s")
        q = A.set_index("ts").c.resample("15min").last().dropna()          # 15-min closes (UTC bins; India opens 03:45 UTC -> aligned)
        e9, e21 = q.ewm(span=9, adjust=False).mean(), q.ewm(span=21, adjust=False).mean()
        tr15 = pd.Series(np.where((q > e21) & (e9 > e21), 1, np.where((q < e21) & (e9 < e21), -1, 0)), index=q.index)
        F[sym] = (A, tr15)
    return F

def main():
    T, _ = TD.run()
    T = T[T.touch].copy()
    F = feats(); g, dm, t15 = [], [], []
    for _, r in T.iterrows():
        A, tr15 = F[r.sym]; a = A[(A.day == r.day) & (A.time == r.time)].iloc[0]
        g.append(a.gap); dm.append(a.c / a.open_ - 1)
        done = tr15[tr15.index + pd.Timedelta(minutes=15) <= pd.Timestamp(a.t, unit="s")]   # bar must be complete at the touch-bar start
        t15.append(int(done.iloc[-1]) if len(done) else 0)
    T["gap"], T["daymove"], T["t15"] = g, dm, t15
    s = T.side
    T["A1"] = ~((T.gap.abs() >= 0.01) & (np.sign(T.gap) != s))
    T["A2"] = ~(((T.daymove >= 0.005) & (s < 0)) | ((T.daymove <= -0.005) & (s > 0)))
    T["A3"] = ~((T.t15 != 0) & (T.t15 != s))
    T["A"] = T.A1 & T.A2 & T.A3
    T["C"] = T.level.isin(["PDH", "PDL", "PDC", "pPOC", "pVAH", "pVAL"])
    T["AC"] = T.A & T.C
    T["base"] = True
    T = T.sort_values(["sym", "day", "time"])
    def one_at_a_time(Z):
        keep, busy = [], {}
        for i, r in Z.iterrows():
            k = (r.sym, r.day); t = int(r.time[:2]) * 60 + int(r.time[3:])
            if t < busy.get(k, -1):
                continue
            keep.append(i); busy[k] = t + 31          # max hold 30 min (exits earlier free the slot too, kept simple / conservative)
        return Z.loc[keep]
    days = sorted(T.day.unique()); half = days[len(days) // 2]
    out = []
    for f in ("base", "A1", "A2", "A3", "A", "C", "AC"):
        Z = one_at_a_time(T[T[f]])
        h1, h2 = Z[Z.day < half], Z[Z.day >= half]
        out.append({"filter": f, "n": len(Z), "win%": round(100 * (Z.net > 0).mean()) if len(Z) else None, "avg_pts_gross": round(Z.pts.mean(), 1),
                    "avg_pts_net": round(Z.net.mean(), 1), "rs": int(Z.rs.sum()), "half1_rs": int(h1.rs.sum()), "half2_rs": int(h2.rs.sum()),
                    "n1": len(h1), "n2": len(h2), "nov5_rs": int(Z[Z.day == "2026-11-05"].rs.sum())})
    R = pd.DataFrame(out)
    os.makedirs("data_store/research/touch_trend", exist_ok=True)
    T.to_csv("data_store/research/touch_trend/trades.csv", index=False); R.to_csv("data_store/research/touch_trend/summary.csv", index=False)
    print("days", days[0], "->", days[-1], len(days), "split at", half); print(R.to_string(index=False))
    print(T.groupby(["sym", "side"]).agg(n=("rs", "size"), rs=("rs", "sum")).to_string())

if __name__ == "__main__":
    main()
