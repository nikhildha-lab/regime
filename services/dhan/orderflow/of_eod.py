"""
Order-flow day summary + signal check (2026-10-17). Run automatically by of_engine.py at 15:31, or by hand:
    python of_eod.py 2026-10-19
1. Day summary  -> data/of/<date>/summary.json : range, delta / CVD path, absorption levels, walls that held or were
   pulled, iceberg and big-print levels, biggest footprint imbalances.
2. Signal check -> data/of/signal_stats.csv (one row per date x event kind x symbol): after each event, did the price
   move in the event's direction over the next 5 / 15 / 30 minutes? (direction = the side the event favours:
   bid wall / buyers absorbing / bid iceberg / big buy = up; the opposite = down; a pulled bid wall = down).
   Over ~60 sessions this tells us which order-flow clues are worth trusting (then the 4-gate test, incl. luck).
"""
from __future__ import annotations

import csv
import json
import os
import sys
from collections import defaultdict
from datetime import datetime

from common import HERE

ROOT = HERE / "data" / "of"
HORIZONS = (5, 15, 30)
DIRECTION = {  # expected price direction per (kind, side)
    ("wall_new", "bid"): 1, ("wall_new", "ask"): -1,
    ("wall_pulled", "bid"): -1, ("wall_pulled", "ask"): 1,
    ("wall_consumed", "bid"): -1, ("wall_consumed", "ask"): 1,
    ("absorption", "bid"): 1, ("absorption", "ask"): -1,      # side = who absorbed (bid = buyers absorbed selling)
    ("iceberg", "bid"): 1, ("iceberg", "ask"): -1,
    ("big_trade", "bid"): 1, ("big_trade", "ask"): -1,        # big_trade side "bid" = buy (bias +1)
}


def _load_mid(day, sym):
    p = ROOT / day / f"sec_{sym}.csv"
    if not p.exists():
        return {}
    out = {}
    with open(p) as f:
        for r in csv.DictReader(f):
            try:
                if r["mid"] not in ("", "None"):
                    out[int(float(r["t"]))] = float(r["mid"])
            except Exception:
                pass
    return out


def _at(mids, keys, t):
    import bisect
    i = bisect.bisect_left(keys, t)
    if i >= len(keys):
        return None
    return mids[keys[i]] if keys[i] - t <= 30 else None


def run(day: str) -> dict:
    d = ROOT / day
    events = []
    if (d / "events.jsonl").exists():
        events = [json.loads(l) for l in open(d / "events.jsonl") if l.strip()]
    summary = {"date": day, "generated": f"{datetime.now():%Y-%m-%d %H:%M}", "label": "context (untested)", "instruments": {}}
    stats_rows = []
    for sym in ("NIFTY", "BANKNIFTY"):
        mids = _load_mid(day, sym)
        if not mids:
            continue
        keys = sorted(mids)
        fps = [json.loads(l) for l in open(d / f"footprint_{sym}.jsonl")] if (d / f"footprint_{sym}.jsonl").exists() else []
        evs = [e for e in events if e.get("sym") == sym]
        hi, lo = max(mids.values()), min(mids.values())
        cvd_path = [(c.get("time"), c.get("cvd")) for c in fps[::15]]
        by_kind = defaultdict(list)
        for e in evs:
            by_kind[e["kind"]].append(e)
        levels = defaultdict(list)
        for e in evs:
            if e["kind"] in ("absorption", "iceberg", "wall_pulled", "wall_consumed"):
                levels[e["kind"]].append({"time": e.get("time"), "price": e["price"], "side": e["side"], "qty": e["qty"]})
        big_imb = sorted(fps, key=lambda c: -abs(c.get("delta") or 0))[:5]
        summary["instruments"][sym] = {
            "open": mids[keys[0]], "close": mids[keys[-1]], "high": hi, "low": lo,
            "final_cvd": fps[-1]["cvd"] if fps else None, "cvd_path_15m": cvd_path,
            "events_count": {k: len(v) for k, v in by_kind.items()},
            "key_levels": {k: v[:15] for k, v in levels.items()},
            "biggest_delta_minutes": [{"time": c.get("time"), "delta": c.get("delta"), "close": c.get("c")} for c in big_imb],
        }
        # ---- signal check
        agg = defaultdict(lambda: {h: [] for h in HORIZONS})
        for e in evs:
            dirn = DIRECTION.get((e["kind"], e["side"]))
            if not dirn:
                continue
            p0 = _at(mids, keys, int(e["t"]))
            if not p0:
                continue
            for h in HORIZONS:
                p1 = _at(mids, keys, int(e["t"]) + 60 * h)
                if p1:
                    agg[(e["kind"], e["side"])][h].append(dirn * (p1 / p0 - 1) * 1e4)   # basis points, signed
        for (kind, side), hs in agg.items():
            row = {"date": day, "sym": sym, "kind": kind, "side": side}
            for h in HORIZONS:
                xs = hs[h]
                row[f"n_{h}m"] = len(xs)
                row[f"hit_{h}m"] = round(sum(x > 0 for x in xs) / len(xs), 3) if xs else None
                row[f"avg_bps_{h}m"] = round(sum(xs) / len(xs), 2) if xs else None
            stats_rows.append(row)
    with open(d / "summary.json", "w") as f:
        json.dump(summary, f, indent=1, default=str)
    sp = ROOT / "signal_stats.csv"
    old = []
    if sp.exists():
        old = [r for r in csv.DictReader(open(sp)) if r["date"] != day]
    cols = ["date", "sym", "kind", "side"] + [f"{a}_{h}m" for h in HORIZONS for a in ("n", "hit", "avg_bps")]
    with open(sp, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in old + stats_rows:
            w.writerow({k: r.get(k) for k in cols})
    return {"summary": str(d / "summary.json"), "signal_rows": len(stats_rows)}


def cumulative() -> list:
    """All days pooled: per kind/side the count, hit rate and average signed move - what the MCP tool shows."""
    sp = ROOT / "signal_stats.csv"
    if not sp.exists():
        return []
    rows = list(csv.DictReader(open(sp)))
    days = sorted({r["date"] for r in rows})
    pool = defaultdict(lambda: {h: [0, 0.0, 0.0] for h in HORIZONS})
    for r in rows:
        for h in HORIZONS:
            n = int(r[f"n_{h}m"] or 0)
            if n and r[f"hit_{h}m"] not in ("", None):
                p = pool[(r["kind"], r["side"])][h]
                p[0] += n
                p[1] += float(r[f"hit_{h}m"]) * n
                p[2] += float(r[f"avg_bps_{h}m"]) * n
    out = []
    for (kind, side), hs in sorted(pool.items()):
        o = {"kind": kind, "side": side, "sessions": len(days)}
        for h in HORIZONS:
            n, hit, bps = hs[h]
            o[f"{h}m"] = {"n": n, "hit_rate": round(hit / n, 3) if n else None, "avg_bps": round(bps / n, 2) if n else None}
        o["status"] = "too early (< 60 sessions)" if len(days) < 60 else "ready for the 4-gate test"
        out.append(o)
    return out


if __name__ == "__main__":
    day = sys.argv[1] if len(sys.argv) > 1 else f"{datetime.now():%Y-%m-%d}"
    print(json.dumps(run(day), indent=1))
