"""Touch-minute: fixed-point stop / breakeven / target retest (2026-11-06, Nikhil: "if 30 points are met we move breakeven and then
target 50 points .. for bank nifty SL will 200 points and target 300-400 points"). Paper research only; the live rule is unchanged.
Same 37 live signals and the same spot path as touch_sl50.py (replay matched the live cards 23 / 23).
  Nifty      : stop 50 pts from entry, +30 pts -> stop to entry, target +50 pts
  Bank Nifty : stop 200 pts, target +300 and +400 (two versions); breakeven at +120 (same 0.6 x stop as Nifty's 30 / 50 - not given, assumed)
Time exit: 30 min (as now) and 59 min (the touch record has 60 prices: about an hour). Managed on 1-minute prices; the target fills at
the target price when a minute reaches it. Money: synthetic future 1 lot after 4.5 / 12.5 pts."""
import os, sys
import pandas as pd
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")); sys.path.insert(0, os.path.join(ROOT, "app"))
import touchtrades as TT
SYN = {"NIFTY": 4.5, "BANKNIFTY": 12.5}; LOT = {"NIFTY": 65, "BANKNIFTY": 30}

def run(P, e, sd, sl_pts, be_pts, tg_pts, mins):
    sl = e - sd * sl_pts; be = False
    for k, px in enumerate(P[2:2 + mins]):
        if sd * (px - sl) <= 0: return sd * (px - e), "breakeven" if be else "SL"
        if sd * (px - e) >= tg_pts: return tg_pts, "target"
        if not be and sd * (px - e) >= be_pts: sl = e; be = True
    p = P[min(len(P) - 1, 1 + mins)]
    return sd * (p - e), f"{mins} min"

V = {"NIFTY": {"N 50/30/50": (50, 30, 50), "N 75/30/90": (75, 30, 90)}, "BANKNIFTY": {"BN 200/120/300": (200, 120, 300), "BN 200/120/400": (200, 120, 400), "BN 100/75/150": (100, 75, 150), "BN 75/30/90": (75, 30, 90)}}   # 100/75/150: Nikhil 6 Nov
rows = []
for day in sorted(f[:10] for f in os.listdir(TT.TDIR) if f.endswith(".json")):
    raw = {r["id"]: r for r in TT._rows(day)}
    for t in TT.trades_for(day):
        if t["status"] != "closed": continue
        k = t["key"]; P = raw[t["id"]].get("path") or []
        if len(P) < 32: continue
        sd = 1 if t["side"] == "long" else -1; e = t["entry"]
        for name, (a, b, c) in V[k].items():
            for mins in (30, 59):
                if len(P) < mins + 2: continue
                pts, why = run(P, e, sd, a, b, c, mins)
                rows.append(dict(day=day, key=k, time=t["time"], side=t["side"], ver=name, hold=mins, pts=round(pts, 1), why=why,
                                 rs=round((pts - SYN[k]) * LOT[k]), best=round(max(sd * (p - e) for p in P[2:2 + mins]), 1),
                                 worst=round(min(sd * (p - e) for p in P[2:2 + mins]), 1)))
T = pd.DataFrame(rows); os.makedirs(os.path.join(ROOT, "data_store/research/touch_fixed_pts"), exist_ok=True)
T.to_csv(os.path.join(ROOT, "data_store/research/touch_fixed_pts/trades.csv"), index=False)
half = "2026-11-03"
def st(Z): return pd.Series(dict(n=len(Z), win=round(100 * (Z.rs > 0).mean()), avg_pts=round(Z.pts.mean(), 1), rs=int(Z.rs.sum()),
                                 h1=int(Z[Z.day < half].rs.sum()), h2=int(Z[Z.day >= half].rs.sum()), target=int((Z.why == "target").sum()),
                                 SL=int((Z.why == "SL").sum()), BE=int((Z.why == "breakeven").sum()), time=int(Z.why.str.contains("min").sum())))
print(T.groupby(["ver", "hold"]).apply(st).to_string())
print(T[(T.hold == 30)][["day", "key", "time", "side", "ver", "pts", "why", "best", "worst"]].to_string(index=False))
