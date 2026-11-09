"""DOM INDEX - basis fix for the paper record (2026-11-09, Nikhil: "Yes fix everywhere which was impacted").

The live DOM Index engine moved the index-price blocks / option walls to futures with the tracker's 09:15 quotes, which were stale
(Nifty -7 / -58 / -88 / -17 pts, Bank Nifty +139 / -402 / -277 / -234 on 4-9 Nov; the real premium was positive), so those zones sat
far from the real level. app/domcards.py now uses a measured basis (see _rebase). This script re-runs the SAME live code on the stored
per-second files for every past day that has a levels file (3-6 Nov; 9 Nov is rebuilt by the live engine itself), and replaces the
index trades of those days in trades.json. Stock trades are not touched (stock levels need no basis). The old file is kept in
_archive/2026-11-03/backups/2026-11-09_dom_basis_fix/. 3 Nov keeps its late_levels flag (levels were first saved at 14:47).
Run: TZ=Asia/Kolkata python3 scripts/research/dom_basis_fix.py [--write]
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import dom_index_replay as R   # noqa: E402
D = R.D

WRITE = "--write" in sys.argv
T = json.load(open(D.TRADES))
old_ix = {}
for x in T:
    if x["key"] in D.INDEX:
        old_ix.setdefault(x["day"], []).append(x)
today = datetime.now().strftime("%Y-%m-%d")
new_all, days = [], []
for f in sorted(os.listdir(D.OUT)):
    if not (f.startswith("levels_") and f.endswith(".json")):
        continue
    day = f[7:17]
    if day >= today:
        continue
    L = json.load(open(os.path.join(D.OUT, f)))
    rows = sorted(R._sec(day, "NIFTY") + R._sec(day, "BANKNIFTY"), key=lambda r: r[1])
    lv = {k: L[k] for k in D.INDEX if k in L}
    tr = R.replay(day, lv, rows)
    hist = {k: (D.S.get("lv") or {}).get(k, {}).get("basis_hist") for k in D.INDEX}
    lv_ts = int(datetime.fromisoformat(L["at"]).timestamp())
    for x in tr:
        x["late_levels"] = bool(x["t0"] < lv_ts)
        x["basis_fix"] = "re-run 9 Nov with the measured basis"
    tr = [x for x in tr if x["status"] == "closed"]
    days.append(day); new_all += tr
    o = old_ix.get(day, [])
    print(f"{day}  basis moves {hist}")
    print(f"   OLD {len(o)} trades Rs {sum(x.get('pnl', 0) for x in o):+,.0f}  ->  NEW {len(tr)} trades Rs {sum(x.get('pnl', 0) for x in tr):+,.0f}"
          f"  (late-level trades: {sum(x['late_levels'] for x in tr)})")
    for x in tr:
        print(f"      {x['key']:9s} {x['setup']:5s} {x['side']:5s} {x['opened'][11:]} {x['level'][:34]:34s} {x['exit_reason']:6s} r {x['r']:+.2f} Rs {x['pnl']:+,.0f}")
if WRITE:
    keep = [x for x in T if not (x["key"] in D.INDEX and x["day"] in days)]
    out = sorted(keep + new_all, key=lambda x: (x["day"], x["opened"]))
    tmp = D.TRADES + ".tmp"
    json.dump(out, open(tmp, "w"), indent=1, default=str)
    os.replace(tmp, D.TRADES)
    print("written", len(out), "trades;", len(new_all), "index trades re-run for", days)
