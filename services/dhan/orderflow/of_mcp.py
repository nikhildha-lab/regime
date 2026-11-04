#!/usr/bin/env python3
"""
Synaptic order-flow MCP server (2026-10-17). Dependency-free (standard library only), stdio transport.
Read-only: answers questions from the order-flow engine's files; it cannot place orders.

Tools
  of_status        engine and feed health, contracts, data age
  of_snapshot      latest order-flow read for NIFTY / BANKNIFTY: imbalance, walls, delta, CVD, recent events
  of_events        events in the last N minutes (walls new / pulled / consumed, absorption, icebergs, big prints)
  of_footprint     last N one-minute footprint candles (buy / sell volume by price, delta, POC, stacked imbalance)
  of_liquidity     liquidity heatmap read: where resting bids / asks have been largest and most persistent
  of_day_summary   a day's summary plus the running signal check (which clues actually led price)
Every answer carries "label": order flow is CONTEXT until ~60 sessions pass the 4-gate test.

Claude Desktop config (Settings -> Developer -> Edit config), add under "mcpServers":
  "synaptic-orderflow": {"command": "/usr/bin/python3",
                         "args": ["/Users/<you>/Documents/dhan/orderflow/of_mcp.py"]}
"""
import csv
import gzip
import json
import os
import sys
import time
from collections import defaultdict
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, "data", "of")
LIVE = os.path.join(ROOT, "live.json")
LABEL = "context - order flow is untested until ~60 recorded sessions pass the 4-gate test; delta side is estimated"
PROTO = "2024-11-05"


def _live():
    if not os.path.exists(LIVE):
        return None, None
    age = round(time.time() - os.path.getmtime(LIVE), 1)
    try:
        return json.load(open(LIVE)), age
    except Exception:
        return None, age


def _sym(s):
    s = (s or "NIFTY").upper().replace(" ", "")
    return "BANKNIFTY" if "BANK" in s else "NIFTY"


def of_status(**_):
    j, age = _live()
    if not j:
        return {"running": False, "detail": "no live.json yet - start Start-Tracker-LIVE (it starts of_engine.py)", "label": LABEL}
    return {"running": age is not None and age < 10, "data_age_seconds": age, "started": j.get("started"),
            "contracts": j.get("contracts"), "feeds": j.get("feeds"), "date": j.get("date"), "label": LABEL}


def of_snapshot(symbol="NIFTY", **_):
    j, age = _live()
    if not j:
        return {"error": "engine not running"}
    sym = _sym(symbol)
    s = (j.get("instruments") or {}).get(sym) or {}
    now = s.get("now") or {}
    fps = s.get("footprint_last") or []
    d5 = sum((c.get("delta") or 0) for c in fps[-5:])
    d15 = sum((c.get("delta") or 0) for c in fps[-15:])
    px5 = (fps[-1]["c"] - fps[-5]["o"]) if len(fps) >= 5 and fps[-1].get("c") and fps[-5].get("o") else None
    read = []
    if now.get("imb20") is not None:
        read.append(f"20-level book {'bid-heavy' if now['imb20'] > 0.15 else 'ask-heavy' if now['imb20'] < -0.15 else 'balanced'} ({now['imb20']:+.2f})")
    if px5 is not None:
        if px5 > 0 and d5 < 0:
            read.append("price up over 5 min but delta negative: buying not confirmed (divergence)")
        elif px5 < 0 and d5 > 0:
            read.append("price down over 5 min but delta positive: selling not confirmed (divergence)")
    return {"symbol": sym, "data_age_seconds": age, "now": now, "cvd": s.get("cvd"),
            "delta_5m": d5, "delta_15m": d15, "price_change_5m": px5, "walls": s.get("walls"),
            "absorption_counts": s.get("absorption_counts"),
            "recent_events": [e for e in (j.get("events") or []) if e.get("sym") == sym][-10:],
            "quick_read": read, "label": LABEL}


def of_events(symbol=None, minutes=30, kind=None, **_):
    j, age = _live()
    day = (j or {}).get("date") or datetime.now().strftime("%Y-%m-%d")
    p = os.path.join(ROOT, day, "events.jsonl")
    if not os.path.exists(p):
        return {"events": [], "detail": "no events file for " + day}
    cut = time.time() - 60 * float(minutes)
    sym = _sym(symbol) if symbol else None
    out = []
    for l in open(p):
        try:
            e = json.loads(l)
        except Exception:
            continue
        if e.get("t", 0) < cut or (sym and e.get("sym") != sym) or (kind and e.get("kind") != kind):
            continue
        out.append(e)
    counts = defaultdict(int)
    for e in out:
        counts[e["kind"]] += 1
    return {"date": day, "minutes": minutes, "counts": counts, "events": out[-80:], "label": LABEL}


def of_footprint(symbol="NIFTY", minutes=15, **_):
    j, _age = _live()
    day = (j or {}).get("date") or datetime.now().strftime("%Y-%m-%d")
    sym = _sym(symbol)
    p = os.path.join(ROOT, day, f"footprint_{sym}.jsonl")
    if not os.path.exists(p):
        return {"candles": [], "detail": "no footprint yet"}
    rows = [json.loads(l) for l in open(p) if l.strip()][-int(minutes):]
    for c in rows:
        lv = c.get("levels") or []
        c["top_levels"] = sorted(lv, key=lambda x: -(x[1] + x[2]))[:5]   # [price, sell vol, buy vol]
        c.pop("levels", None)
    return {"symbol": sym, "candles": rows, "label": LABEL}


def of_liquidity(symbol="NIFTY", minutes=30, **_):
    j, _age = _live()
    day = (j or {}).get("date") or datetime.now().strftime("%Y-%m-%d")
    sym = _sym(symbol)
    p = os.path.join(ROOT, day, f"heatmap_{sym}.csv.gz")
    if not os.path.exists(p):
        return {"detail": "no heatmap yet"}
    cut = time.time() - 60 * float(minutes)
    agg = defaultdict(lambda: [0.0, 0.0, 0])
    slices = set()
    with gzip.open(p, "rt") as f:
        for r in csv.reader(f):
            try:
                t, _s, px, b, a = int(r[0]), r[1], float(r[2]), float(r[3]), float(r[4])
            except Exception:
                continue
            if t < cut:
                continue
            slices.add(t)
            g = agg[px]
            g[0] += b
            g[1] += a
            g[2] += 1
    n = max(1, len(slices))
    zones = [{"price": px, "avg_bid": round(b / n), "avg_ask": round(a / n), "present_pct": round(100 * c / n)} for px, (b, a, c) in agg.items()]
    top_b = sorted(zones, key=lambda z: -z["avg_bid"])[:5]
    top_a = sorted(zones, key=lambda z: -z["avg_ask"])[:5]
    return {"symbol": sym, "minutes": minutes, "slices": len(slices), "support_zones": top_b, "resistance_zones": top_a,
            "note": "resting liquidity can be pulled - see of_events wall_pulled", "label": LABEL}


def of_day_summary(date=None, **_):
    sys.path.insert(0, HERE)
    date = date or datetime.now().strftime("%Y-%m-%d")
    p = os.path.join(ROOT, date, "summary.json")
    summ = json.load(open(p)) if os.path.exists(p) else {"detail": f"no summary for {date} (written at 15:31)"}
    try:
        import of_eod
        stats = of_eod.cumulative()
    except Exception as e:
        stats = {"error": str(e)[:120]}
    return {"summary": summ, "signal_check_all_days": stats, "label": LABEL}


TOOLS = {
    "of_status": (of_status, "Order-flow engine health: running?, data age, Dhan feeds (trades, 20-level, 200-level), contracts.", {}),
    "of_snapshot": (of_snapshot, "Latest order-flow read for NIFTY or BANKNIFTY futures: book imbalance (5/20/deep), walls, delta and CVD, divergence, recent events.",
                    {"symbol": {"type": "string", "description": "NIFTY or BANKNIFTY"}}),
    "of_events": (of_events, "Order-flow events in the last N minutes: wall_new, wall_pulled, wall_consumed, absorption, iceberg, big_trade.",
                  {"symbol": {"type": "string"}, "minutes": {"type": "number", "default": 30}, "kind": {"type": "string"}}),
    "of_footprint": (of_footprint, "Last N one-minute footprint candles: buy/sell volume (estimated), delta, CVD, POC, stacked imbalances, top price levels.",
                     {"symbol": {"type": "string"}, "minutes": {"type": "number", "default": 15}}),
    "of_liquidity": (of_liquidity, "Liquidity heatmap read: price zones with the largest and most persistent resting bids (support) and asks (resistance).",
                     {"symbol": {"type": "string"}, "minutes": {"type": "number", "default": 30}}),
    "of_day_summary": (of_day_summary, "A day's order-flow summary (levels, delta path, events) plus the running signal check across all recorded days.",
                       {"date": {"type": "string", "description": "YYYY-MM-DD, default today"}}),
}


def _reply(i, result=None, error=None):
    msg = {"jsonrpc": "2.0", "id": i}
    if error:
        msg["error"] = error
    else:
        msg["result"] = result
    sys.stdout.write(json.dumps(msg, default=str) + "\n")
    sys.stdout.flush()


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except Exception:
            continue
        m, i = req.get("method"), req.get("id")
        if m == "initialize":
            _reply(i, {"protocolVersion": (req.get("params") or {}).get("protocolVersion", PROTO),
                       "capabilities": {"tools": {}}, "serverInfo": {"name": "synaptic-orderflow", "version": "1.0"}})
        elif m == "tools/list":
            _reply(i, {"tools": [{"name": k, "description": d, "inputSchema": {"type": "object", "properties": props}}
                                 for k, (_f, d, props) in TOOLS.items()]})
        elif m == "tools/call":
            p = req.get("params") or {}
            name, args = p.get("name"), p.get("arguments") or {}
            if name not in TOOLS:
                _reply(i, error={"code": -32601, "message": f"unknown tool {name}"})
                continue
            try:
                out = TOOLS[name][0](**args)
                _reply(i, {"content": [{"type": "text", "text": json.dumps(out, default=str)}]})
            except Exception as e:
                _reply(i, {"content": [{"type": "text", "text": f"error: {e}"}], "isError": True})
        elif m == "ping":
            _reply(i, {})
        elif i is not None and not str(m).startswith("notifications/"):
            _reply(i, error={"code": -32601, "message": f"method not found: {m}"})


if __name__ == "__main__":
    main()
