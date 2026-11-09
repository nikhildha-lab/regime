"""DOM INDEX REPLAY (2026-11-06, Nikhil: "can backtest DOM Index?").

Replays the LIVE DOM hold / break code (app/domcards.py, unchanged, same parameters) on every day the Dhan order-flow engine
recorded (data/of/<day>/sec_NIFTY.csv, sec_BANKNIFTY.csv - 1-second futures mid, 20-level book lean, market buy / sell volume).
Index only (Nifty, Bank Nifty). Paper research - never orders.

Levels per day:
  - days with a stored live levels file (levels_<day>.json, from 3 Nov) -> exactly those levels (the replay should match live)
  - older days -> rebuilt the same way the live code builds them, from data known before the open:
        blocks  : levelmap.build(key, 10, asof = previous session) - medium / strong blocks, moved to futures by the basis
        OI walls: strike with the most call / put OI (nearest expiry) in the day's first option-chain snapshot (~09:16)
        basis   : futures mid - option-chain spot at that first snapshot
        IB high / low: added by the live code itself at 10:15
Money: synthetic future 1 lot after 4.5 (Nifty) / 12.5 (Bank Nifty) pts round trip - the live rule's cost.
Run: TZ=Asia/Kolkata python3 scripts/research/dom_index_replay.py      (the code reads clock times in local time)
"""
from __future__ import annotations

import json
import os
import sys
from collections import Counter

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.abspath(os.path.join(HERE, "..", "..", "app"))
sys.path.insert(0, APP)
import domcards as D          # noqa: E402
import levelmap as LM         # noqa: E402

ROOT = os.path.abspath(os.path.join(APP, ".."))
OCD = os.path.join(ROOT, "data_store", "flow", "option_chain")
OUTD = os.path.join(ROOT, "data_store", "research", "dom_index_replay")


def days():
    d = sorted(x for x in os.listdir(os.path.join(D.OFD, "of")) if len(x) == 10 and x[4] == "-")
    return [x for x in d if all(os.path.exists(os.path.join(D.OFD, "of", x, f"sec_{k}.csv")) for k in D.INDEX)]


def _sec(day, k):
    out = []
    for l in open(os.path.join(D.OFD, "of", day, f"sec_{k}.csv")):
        c = l.split(",")
        if c[0] == "t" or len(c) < 15:
            continue
        mid = D._f(c[4])
        if mid:
            out.append((k, int(c[0]), mid, D._f(c[11]) or 0.0, D._f(c[6]) or 0.0, D._f(c[7]) or 0.0, D._f(c[13]) or 0.0, D._f(c[14]) or 0.0))
    return out


def rebuilt_levels_fut(day):
    """2026-11-09 v3: futures-native levels (levelmap.build_fut as of the previous session) - what the live engine now uses."""
    out = {}
    prev = (pd.Timestamp(day) - pd.offsets.Day(1)).strftime("%Y-%m-%d")
    for k in D.INDEX:
        z = []
        try:
            for b in LM.build_fut(k, 10, asof=prev).get("blocks") or []:
                if b.get("strength") in ("medium", "strong"):
                    z.append((b["lo"], b["hi"], f"{b['strength']} block {b['lo']:.0f}-{b['hi']:.0f}"))
        except Exception as e:
            print("levelmap_fut", k, day, e)
        out[k] = {"zones": D._merge(z), "prices": "futures", "basis": None, "rebuilt": True}
    return out


def rebuilt_levels(day, rows):
    """Index levels from data known before the open (see the module note). v1/v2 style (spot blocks + OI walls moved by a basis)."""
    prev = None
    o = None
    p = os.path.join(OCD, f"{day}.csv.gz")
    if os.path.exists(p):
        o = pd.read_csv(p, usecols=["ts", "symbol", "expiry", "strike", "side", "oi", "underlying"])
        o = o[o.expiry.notna()]; o["ts"] = pd.to_datetime(o.ts)
    out = {}
    for k in D.INDEX:
        z, walls, basis = [], {}, 0.0
        if o is not None:
            x = o[o.symbol == k]
            if len(x):
                s0 = x.ts.min(); a = x[x.ts == s0]
                exp = sorted(a.expiry.astype(str).unique())[0]; a = a[a.expiry.astype(str) == exp]
                spot = float(a.underlying.iloc[0])
                t0 = pd.Timestamp(s0).tz_localize("Asia/Kolkata").timestamp()
                f = [r[2] for r in rows if r[0] == k and abs(r[1] - t0) <= 60]
                basis = (f[0] - spot) if f else 0.0
                for side, nm in (("CE", "call"), ("PE", "put")):
                    b = a[a.side == side]
                    if len(b) and b.oi.max() > 0:
                        walls[nm] = float(b.loc[b.oi.idxmax(), "strike"])
        try:
            prev = (pd.Timestamp(day) - pd.offsets.BDay(1)).strftime("%Y-%m-%d")
            for b in LM.build(k, 10, asof=prev).get("blocks") or []:
                if b.get("strength") in ("medium", "strong"):
                    z.append((b["lo"] + basis, b["hi"] + basis, f"{b['strength']} block {b['lo']:.0f}-{b['hi']:.0f}"))
        except Exception as e:
            print("levelmap", k, day, e)
        px = next((r[2] for r in rows if r[0] == k), 0)
        bd = px * D.BAND["index"]
        for nm in ("call", "put"):
            w = walls.get(nm)
            if w:
                z.append((w + basis - bd, w + basis + bd, f"{nm} wall {w:.0f}"))
        out[k] = {"zones": D._merge(z), "basis": round(basis, 1), "rebuilt": True}
    return out


def replay(day, lv, rows):
    lv = D._prep_lv(json.loads(json.dumps(lv)))      # 2026-11-09: index-price zones + measured basis, exactly as live
    D.S.clear()
    D.S.update(day=day, off={}, inst={}, lv=lv, changed=False, err={}, trades=[], lv_ts=0, flat_done=False)
    for r in rows:
        D._step(*r)
    return [dict(x) for x in D.S["trades"] if x["key"] in D.INDEX]


def main():
    os.makedirs(OUTD, exist_ok=True)
    tag = "_fut" if D.LEVELS == "fut" else ""
    allt, lvsrc = [], {}
    for day in days():
        rows = sorted(_sec(day, "NIFTY") + _sec(day, "BANKNIFTY"), key=lambda r: r[1])
        p = os.path.join(D.OUT, f"levels_{day}.json")
        if D.LEVELS == "fut":                               # 2026-11-09 v3: same futures-native levels for every day
            L = json.load(open(p)) if os.path.exists(p) else {}
            if all(isinstance(L.get(k), dict) and L[k].get("prices") == "futures" for k in D.INDEX):
                lv = {k: L[k] for k in D.INDEX}; src = "live file (futures)"
            else:
                lv = rebuilt_levels_fut(day); src = "rebuilt (futures)"
        elif os.path.exists(p):
            L = json.load(open(p)); lv = {k: L[k] for k in D.INDEX if k in L}; src = "live file"
        else:
            lv = rebuilt_levels(day, rows); src = "rebuilt"
        lvsrc[day] = src
        tr = replay(day, lv, rows)
        for x in tr:
            x["levels_src"] = src
        allt += tr
        print(day, src, f"{len(rows) // 2:>6} s", "trades", len(tr), "Rs", sum(x.get("pnl", 0) for x in tr if x["status"] == "closed"))
    d = pd.DataFrame([x for x in allt if x["status"] == "closed"])
    d.to_csv(os.path.join(OUTD, f"trades{tag}.csv"), index=False)
    d = D.plan_ix_fill([{**x, "plan_ix": None} for x in d.to_dict("records")]) if False else d
    def s(x):
        return pd.Series({"n": len(x), "win%": round(100 * (x.pnl > 0).mean()) if len(x) else 0, "avgR_gross": round(x.r_gross.mean(), 2),
                          "avgR": round(x.r.mean(), 2), "rs": int(x.pnl.sum())})
    print("\nALL", s(d).to_dict())
    print(d.groupby("levels_src").apply(s, include_groups=False).to_string())
    print(d.groupby("key").apply(s, include_groups=False).to_string())
    print(d.groupby("setup").apply(s, include_groups=False).to_string())
    print(d.groupby("side").apply(s, include_groups=False).to_string())
    print(d.groupby("exit_reason").apply(s, include_groups=False).to_string())
    print(d.groupby("day").apply(s, include_groups=False).to_string())
    days_ = sorted(d.day.unique()); h = len(days_) // 2
    print("first half", days_[:h], s(d[d.day.isin(days_[:h])]).to_dict())
    print("second half", days_[h:], s(d[d.day.isin(days_[h:])]).to_dict())
    # index plan account: first 3 index trades a day, -2R day stop
    pl = []
    for day, g in d.sort_values("opened").groupby("day"):
        took = []
        for _, x in g.iterrows():
            rc = sum(p.r for p in took if str(p.closed) <= x.opened)
            if len(took) < 3 and rc > -2:
                took.append(x)
        pl += took
    pl = pd.DataFrame(pl)
    print("PLAN (first 3 a day, -2R)", s(pl).to_dict())
    c = d.sort_values("opened").pnl.cumsum(); print("max drawdown Rs", int((c - c.cummax()).min()))
    json.dump(lvsrc, open(os.path.join(OUTD, f"levels_source{tag}.json"), "w"), indent=1)


if __name__ == "__main__":
    main()
