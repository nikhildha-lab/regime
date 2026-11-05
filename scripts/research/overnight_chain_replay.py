"""Overnight hold on REAL recorded option prices (2026-11-06): app/overnight.py run on every recorded option-chain day (20 Oct - 5 Nov),
signal at 15:13 from the snapshots' spot path (Dhan 1-min bars are not stored for past days), prev close = Yahoo daily,
entry priced at the latest snapshot <= 15:13, exit at the next day's first snapshot after 09:15. Paper research only."""
import os, sys, json
import pandas as pd
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")); sys.path.insert(0, os.path.join(ROOT, "app"))
import overnight as O
TT = O._TT(); days = sorted(f[:10] for f in os.listdir(TT.OCD) if f.endswith(".csv.gz"))
O.DL_OFF = True
rows = []
for day in days[:-1]:
    for key in O.KEYS:
        o = TT._chain(day)
        u = o[(o.symbol == key) & (o.ts.dt.strftime("%H:%M") < O.AT)].groupby("ts").underlying.first()
        if len(u) < 10: continue
        pc, _ = O._prev_close(key, day); a = O._snap(day, key, O.AT)
        if pc is None or a is None: continue
        r = O.evaluate(key, day, float(u.iloc[-1]), float(u.max()), float(u.min()), pc, a)
        if r["status"] == "open": O.close_row(r)
        rows.append(r)
json.dump(rows, open(os.path.join(ROOT, "data_store/research/overnight_chain_replay.json"), "w"), indent=1, default=str)
for r in rows:
    line = f"{r['day']} {r['key']:9s} loc {r['close_loc']:.2f} day {r['day_ret_pct']:+.2f}% -> {r['status']}"
    if r["status"] == "closed":
        line += f" {r['side']:5s} spot {r['spot_pts']:+.1f} pts | " + " | ".join(f"{v}: Rs {V['pnl']:+,} (gross {V['gross']:+,}, delta {V['net_delta']})" for v, V in r["versions"].items())
    print(line)
cl = [r for r in rows if r["status"] == "closed"]
for v in O.VERS:
    z = [r["versions"][v]["pnl"] for r in cl if v in r["versions"]]
    print(v, "n", len(z), "total Rs", sum(z), "win", sum(x > 0 for x in z))
