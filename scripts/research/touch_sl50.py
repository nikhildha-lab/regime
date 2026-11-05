"""Touch-minute: wider stop retest (2026-11-06, Nikhil: "SL around 23 points .. can we have 50 points and retest all the trades").
Paper research only. Every live touch-minute signal on record (data_store/livedesk/touches, via touchtrades.trades_for: live levels,
live delta filter, one trade per index), managed on the touch record's own SPOT path (touchlog: one spot price per minute for 60 min after the touch -
the same feed the live rule used; a first try with futures-minus-basis 1-min bars matched the live cards poorly, corr 0.43):
  A  current : stop 0.10 % past the level (at least 0.05 % from entry)                 ~23 pts Nifty
  B  50 pts  : stop 50 Nifty points from entry; Bank Nifty the same % (50 / 24,000 = 0.21 % -> ~115 pts)
  B50 BN 50  : Bank Nifty with a flat 50 pts too (for comparison only)
  C  0.20 %  : stop 0.20 % past the level (the record-only 'wide' version on the cards)
Same for all: +1R -> stop to entry; from +2R trail 1R behind the best close; out after 30 min or 15:20.
Money: synthetic future 1 lot (Nifty 65 / Bank Nifty 30) after 4.5 / 12.5 pts. Check: version A vs the live cards (3-5 Nov)."""
import os, sys, json
import numpy as np, pandas as pd
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")); sys.path.insert(0, os.path.join(ROOT, "app"))
import touchtrades as TT
OF = os.environ.get("MP_OF_DIR") or os.path.expanduser("~/Documents/dhan/orderflow/data/of")
SYN = {"NIFTY": 4.5, "BANKNIFTY": 12.5}; LOT = {"NIFTY": 65, "BANKNIFTY": 30}

def bars(day, sym):
    s = pd.read_csv(os.path.join(OF, day, f"sec_{sym}.csv"), usecols=["t", "bid", "ask", "mid"])
    s = s[(s.bid > 0) & (s.ask > 0)]
    s = s.assign(hm=pd.to_datetime(s.t, unit="s", utc=True).dt.tz_convert("Asia/Kolkata").dt.strftime("%H:%M"))
    return s.groupby("hm").mid.last()

def manage(c, e, sd, stop, t0):
    R = abs(e - stop); sl, best, trail = stop, e, 0
    end = (pd.Timestamp("2000-01-01 " + t0) + pd.Timedelta(minutes=31)).strftime("%H:%M")
    for hm, px in c.items():
        if sd * (px - sl) <= 0:
            return sd * (px - e), ["SL", "breakeven", "trail"][trail], hm, R
        best = max(best, px) if sd > 0 else min(best, px); mv = sd * (best - e)
        if mv >= 2 * R:
            nt = best - sd * R; sl = max(sl, nt) if sd > 0 else min(sl, nt); trail = 2
        elif mv >= R and trail == 0:
            sl = e; trail = 1
        if hm >= end or hm >= "15:20":
            return sd * (px - e), "30 min", hm, R
    return sd * (c.iloc[-1] - e), "end", c.index[-1], R

rows = []
for day in sorted(f[:10] for f in os.listdir(TT.TDIR) if f.endswith(".json")):
    raw = {r["id"]: r for r in TT._rows(day)}
    cache = {}
    for t in TT.trades_for(day):
        if t["status"] != "closed": continue
        k = t["key"]; r = raw[t["id"]]
        P = r.get("path") or []
        if len(P) < 3: continue
        t0m = pd.Timestamp("2000-01-01 " + t["time"])
        c = pd.Series(P[2:], index=[(t0m + pd.Timedelta(minutes=i + 1)).strftime("%H:%M") for i in range(len(P) - 2)])   # closes after the entry minute
        sd = 1 if t["side"] == "long" else -1; e, L = t["entry"], t["level"]; t0 = t["time"]
        stops = {"A": min(L * 0.999, e * 0.9995) if sd > 0 else max(L * 1.001, e * 1.0005),
                 "B": e - sd * (50 if k == "NIFTY" else 50 / 24000 * e),
                 "B50": e - sd * 50,
                 "C": min(L * 0.998, e * 0.9995) if sd > 0 else max(L * 1.002, e * 1.0005)}
        row = dict(day=day, key=k, time=t0, side=t["side"], level=t["level_name"][:30])
        for v, st in stops.items():
            if v == "B50" and k == "NIFTY": continue
            pts, why, hm, R = manage(c, e, sd, st, t0)
            row.update({f"{v}_R": round(R, 1), f"{v}_pts": round(pts, 1), f"{v}_why": why, f"{v}_rs": round((pts - SYN[k]) * LOT[k])})
        rows.append(row)
T = pd.DataFrame(rows)
os.makedirs(os.path.join(ROOT, "data_store/research/touch_sl50"), exist_ok=True); T.to_csv(os.path.join(ROOT, "data_store/research/touch_sl50/trades.csv"), index=False)
# check A against the live cards
chk = []
for f in sorted(os.listdir(TT.CDIR)):
    for c in json.load(open(os.path.join(TT.CDIR, f))):
        if c["status"] == "closed" and c.get("spot_pts") is not None:
            m = T[(T.day == c["day"]) & (T.key == c["key"]) & (T.time == c["time"])]
            if len(m): chk.append((c["day"], c["key"], c["time"], c["spot_pts"], m.A_pts.iloc[0], c["exit_reason"], m.A_why.iloc[0]))
C = pd.DataFrame(chk, columns=["day", "key", "time", "live_pts", "sim_pts", "live_why", "sim_why"])
print("check vs live cards: n", len(C), "same exit type", (C.live_why.str.replace(" SL", "").str.replace("breakeven", "breakeven") == C.sim_why).sum(),
      "pts corr", round(C.live_pts.corr(C.sim_pts), 2), "live sum", round(C.live_pts.sum(), 1), "sim sum", round(C.sim_pts.sum(), 1))
days = sorted(T.day.unique()); half = days[len(days) // 2]
def summ(v, Z):
    z = Z[f"{v}_rs"].dropna(); p = Z[f"{v}_pts"].dropna()
    return dict(version=v, n=len(z), win=round(100 * (z > 0).mean()), avg_pts=round(p.mean(), 1), rs=int(z.sum()),
                h1=int(Z[Z.day < half][f"{v}_rs"].sum()), h2=int(Z[Z.day >= half][f"{v}_rs"].sum()),
                SL=int((Z[f"{v}_why"] == "SL").sum()), BE=int((Z[f"{v}_why"] == "breakeven").sum()), TR=int((Z[f"{v}_why"] == "trail").sum()), T30=int((Z[f"{v}_why"] == "30 min").sum()))
for nm, Z in (("ALL", T), ("NIFTY", T[T.key == "NIFTY"]), ("BANKNIFTY", T[T.key == "BANKNIFTY"])):
    print("\n" + nm, "(split", half, ")"); print(pd.DataFrame([summ(v, Z) for v in ("A", "B", "C") + (("B50",) if nm == "BANKNIFTY" else ())]).to_string(index=False))
print("\nper day rs:"); print(T.groupby("day")[["A_rs", "B_rs", "C_rs"]].sum().to_string())
print("\nNifty rows:"); print(T[T.key == "NIFTY"][["day", "time", "side", "A_R", "A_pts", "A_why", "B_pts", "B_why"]].to_string(index=False))
