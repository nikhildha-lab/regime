"""LEVEL BOARD - one table per index that puts every view of a price level side by side (2026-11-09, Nikhil: "combine Levels & Blocks,
Big Walls · Order Book, OI at the Blocks, and open limit order in 1 section in a tabular manner ... levels can be rows others can be cols
and final action").

Rows    : the Level Map blocks (strong / medium / weak) and reference levels (previous day / week / month high-low, VWAP, POC / VAH / VAL;
          no IB - removed 9 Nov) within RANGE of the index price; reference levels that sit inside a block are merged into that row;
          big walls that are not at any level get their own row ("wall only"). Sorted high to low; the price row sits between.
Columns : Level (index price, futures price beside it) · Type · Distance · OI at the block (oiwatch.blocks_watch) · Big wall
          (bigwalls.walls, 200-level book) · Resting orders = bid / ask quantity waiting inside the zone right now (latest minute of the
          200-level book slices heat_live_<SYM>.json, about +-0.4 % of price) · Action.
Prices  : blocks / OI are index (spot) prices; walls and the book are futures - moved to index prices with the live basis
          (livedesk.fut_levels: median futures 1-min close - option-chain spot).
Action  : a plain rule, written before looking at any results, and UNTESTED: score = strength (strong 2 / medium 1 / weak 0) + OI read
          (defended +1, break-type read -1) + a big wall on the level's side (+1, shrinking -1) + more resting qty on the level's side than
          the other (+1). Near = within NEAR % of price. Indication only, never an order.
"""
from __future__ import annotations

import time

RANGE, NEAR, FAR, TOL = 1.2, 0.30, 0.70, 0.02        # % of price: rows kept, "near", "far", wall-to-level match tolerance
LOT = {"NIFTY": 65, "BANKNIFTY": 30}
STR = {"strong": 2, "medium": 1, "weak": 0}
REF_OK = {"PDH", "PDL", "PWH", "PWL", "PMH", "PML", "VWAP", "POC", "VAH", "VAL"}
S: dict = {"cache": {}}


def _book(BW, key):
    """latest minute of resting bid / ask qty per futures price bucket (200-level book slices)"""
    try:
        x = BW._load(key)
        if x is None:
            return None, None
        h, _, day, src = x
        t = h.t.max(); g = h[h.t == t]
        return [(float(p), float(b or 0), float(a or 0)) for p, b, a in zip(g.p, g.bid, g.ask)], {"src": src, "at": time.strftime("%H:%M", time.localtime(int(t))), "day": day}
    except Exception:
        return None, None


def build(key, prep, oi, flv, hot):
    key = (key or "NIFTY").upper()
    P = (prep or {}).get(key) or {}
    if not P or P.get("error"):
        return {"key": key, "err": "no Level Map for " + key}
    px = (oi or {}).get("spot") or P.get("spot")
    basis = (flv or {}).get("basis")
    BW = hot("bigwalls"); W = BW.api(key) or {}
    if basis is None and W.get("price") and px:
        basis = round(W["price"] - px, 1)
    basis = float(basis or 0.0)
    book, binfo = _book(BW, key)
    lot = LOT.get(key, 1)
    rows = []
    for st in ("strong", "medium", "weak"):
        for b in (P.get("blocks") or {}).get(st) or []:
            if b.get("lo") and b.get("hi") and abs(((b["lo"] + b["hi"]) / 2) / px - 1) * 100 <= RANGE:
                rows.append({"lo": b["lo"], "hi": b["hi"], "kind": "block", "strength": st, "star": bool(b.get("star")), "why": b.get("why") or "", "tags": []})
    for r in P.get("levels") or []:
        if r.get("short") not in REF_OK or not r.get("px") or abs(r["px"] / px - 1) * 100 > RANGE:
            continue
        hit = next((x for x in rows if x["lo"] - px * TOL / 100 <= r["px"] <= x["hi"] + px * TOL / 100), None)
        if hit:
            hit["tags"].append(r["short"])
        else:
            bd = px * 0.0004
            rows.append({"lo": r["px"] - bd, "hi": r["px"] + bd, "kind": "ref", "strength": "ref", "star": r["short"][1:2] in ("W", "M"), "why": r.get("name") or "", "tags": [r["short"]]})
    # OI at the blocks (strong / medium only, by overlap)
    for b in (oi or {}).get("blocks") or []:
        hit = next((x for x in rows if x["kind"] == "block" and abs(x["lo"] - b["lo"]) < 1 and abs(x["hi"] - b["hi"]) < 1), None)
        if hit:
            d = b.get("defenders") or {}
            hit["oi"] = {"type": d.get("type"), "oi": d.get("oi"), "pct30": d.get("pct_m30"), "pct_open": d.get("pct_open"), "read": b.get("read"), "tone": b.get("tone")}
    # big walls, futures -> index prices
    for w in (W.get("above") or []) + (W.get("below") or []):
        wp = w["price"] - basis
        hit = next((x for x in rows if x["lo"] - px * TOL / 100 <= wp <= x["hi"] + px * TOL / 100), None)
        info = {"side": w["side"], "x": w["x"], "trend": w["trend"], "age": w["age_min"], "fut": w["price"], "qty": w["qty_now"]}
        if hit:
            if not hit.get("wall") or w["x"] > hit["wall"]["x"]:
                hit["wall"] = info
        else:
            bd = px * 0.0002
            rows.append({"lo": wp - bd, "hi": wp + bd, "kind": "wall", "strength": "wall", "star": False, "why": "big resting order, not at a mapped level", "tags": [], "wall": info})
    # resting orders inside each zone (latest minute of the book)
    if book:
        bk_lo, bk_hi = min(p for p, _, _ in book) - basis, max(p for p, _, _ in book) - basis
        bids = sorted(q for _, q, _ in book if q > 0); asks = sorted(q for _, _, q in book if q > 0)
        mb = bids[len(bids) // 2] if bids else 0; ma = asks[len(asks) // 2] if asks else 0
        for x in rows:
            lo, hi = x["lo"] - 2, x["hi"] + 2
            if hi < bk_lo or lo > bk_hi:
                x["book"] = None; continue
            inz = [(p, b, a) for p, b, a in book if lo <= p - basis <= hi]
            n = max(len(inz), 1); qb = sum(b for _, b, _ in inz); qa = sum(a for _, _, a in inz)
            x["book"] = {"bid_lots": round(qb / lot), "ask_lots": round(qa / lot), "bid_x": round(qb / n / mb, 1) if mb and qb else None,
                         "ask_x": round(qa / n / ma, 1) if ma and qa else None, "buckets": len(inz)}
    out = []
    for x in rows:
        mid = (x["lo"] + x["hi"]) / 2; inside = x["lo"] <= px <= x["hi"]
        side = "inside" if inside else "R" if mid > px else "S"
        dist = 0.0 if inside else (x["lo"] - px if side == "R" else x["hi"] - px)
        dp = abs(dist) / px * 100
        sc = STR.get(x["strength"], 0); why = [f"{x['strength']} level" if x["kind"] == "block" else ("reference level" if x["kind"] == "ref" else "wall only")]
        o = x.get("oi")
        if o and o.get("tone") == "hold":
            sc += 1; why.append("option writers defending")
        elif o and o.get("tone") == "break":
            sc -= 1; why.append("OI points to a break")
        w = x.get("wall"); want = "sell" if side == "R" else "buy"
        if w and (side == "inside" or w["side"] == want):
            if w["trend"] == "shrinking":
                sc -= 1; why.append("wall shrinking")
            else:
                sc += 1; why.append(f"{w['side']} wall {w['x']}x")
        bk = x.get("book")
        if bk and side != "inside":
            mine, other = (bk["ask_lots"], bk["bid_lots"]) if side == "R" else (bk["bid_lots"], bk["ask_lots"])
            if mine > other and mine > 0:
                sc += 1; why.append("more resting orders on its side")
        if side == "inside":
            act, tone = "Price in zone - wait for a DOM HOLD or BREAK signal", "wait"
        elif dp > FAR:
            act, tone = "Far - no action yet", "neutral"
        elif sc >= 3:
            act = ("Strong ceiling - watch for a HOLD short; no fresh longs into it" if side == "R" else "Strong floor - watch for a HOLD long; no fresh shorts into it")
            tone = "bear" if side == "R" else "bull"
        elif sc <= 0:
            act = ("Thin ceiling - a BREAK long is more likely if price arrives with buyers" if side == "R" else "Thin floor - a BREAK short is more likely if price arrives with sellers")
            tone = "bull" if side == "R" else "bear"
        else:
            act, tone = ("Watch - react only with order-book confirmation", "wait") if dp <= NEAR else ("On the radar", "neutral")
        out.append({"lo": round(x["lo"], 1), "hi": round(x["hi"], 1), "fut_lo": round(x["lo"] + basis, 1), "fut_hi": round(x["hi"] + basis, 1), "kind": x["kind"],
                    "strength": x["strength"], "star": x["star"], "why": x["why"], "tags": x["tags"], "side": side, "dist": round(dist, 1), "dist_pct": round(dist / px * 100, 2),
                    "oi": x.get("oi"), "wall": x.get("wall"), "book": x.get("book"), "score": sc, "action": act, "tone": tone, "score_why": why})
    out.sort(key=lambda r: -r["hi"])
    return {"key": key, "price": round(px, 1), "fut": W.get("price"), "basis": round(basis, 1), "at": time.strftime("%H:%M"), "oi_at": (oi or {}).get("at"),
            "walls_at": W.get("as_of"), "walls_src": W.get("source"), "book": binfo, "rows": out, "pulled": W.get("pulled") or [],
            "note": "Action is a fixed rule (level strength + option writers + big wall + resting orders), untested - an indication, never an order."}


def api(key, prep, oi, flv, hot):
    c = S["cache"].get(key)
    if c and time.time() - c[0] < 30:
        return c[1]
    try:
        r = build(key, prep, oi, flv, hot)
    except Exception as e:
        r = {"key": key, "err": f"{type(e).__name__}: {str(e)[:160]}"}
    S["cache"][key] = (time.time(), r)
    return r
