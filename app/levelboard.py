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


def _dom(hot, key, basis):
    """Live HOLD / BREAK checklist per DOM-engine zone (domcards keeps per-zone state in memory; read-only, the engine is not touched).
    Zones are futures prices. Returns (zones, ctx) - each zone with the state of every entry condition right now."""
    DC = hot("domcards"); S = DC.S; P = DC.P
    I = (S.get("inst") or {}).get(key)
    if not I or not I.get("h"):
        return [], {"err": "DOM engine has no data for " + key + " yet"}
    h = list(I["h"]); t, mid = h[-1][0], h[-1][1]
    base = I.get("base")
    w30 = [x[2] for x in h if x[0] >= t - 30]
    lean = (sum(w30) / len(w30) - base) if (w30 and base is not None) else None
    w60 = [x for x in h if x[0] >= t - 60]
    vol = sum(x[3] + x[4] for x in w60); dl = sum(x[3] - x[4] for x in w60)
    dpct = dl / vol if vol else None
    hm = DC._hm(t); b = mid * DC.BAND["index"]
    gate = None
    if hm < P["first"]:
        gate = f"engine starts {P['first']}"
    elif hm >= P["last"]:
        gate = f"no new trades after {P['last']}"
    elif I.get("n", 0) >= P["per_day"]:
        gate = f"day limit reached ({P['per_day']} trades)"
    zones = list(((S.get("lv") or {}).get(key) or {}).get("zones") or []) + list(I.get("extra") or [])
    trades = [x for x in S.get("trades") or [] if x.get("key") == key and x.get("day") == S.get("day")]
    out = []
    for z in zones:
        zid = f"{z['lo']}-{z['hi']}"; st = (I.get("lv") or {}).get(zid) or {}
        lo, hi = z["lo"], z["hi"]
        pos = "above" if mid > hi else "below" if mid < lo else "in"
        hold_dir = "long" if pos == "above" else "short" if pos == "below" else None    # price above a zone -> it is support -> HOLD long
        brk_dir = "short" if pos == "above" else "long" if pos == "below" else None     # breaking it goes the other way
        tc = st.get("touch"); o = st.get("out")
        # HOLD checklist
        H = {"state": "waiting", "dir": hold_dir}
        if tc:
            long_ = tc["from"] == "above"; sd = 1 if long_ else -1; far = lo if long_ else hi
            H.update(dir="long" if long_ else "short", state="testing", left=max(0, int(P["hold_s"] - (t - tc["t"]))),
                     fresh=True, held=(tc["ext"] - (far - sd * 2 * b)) * sd > 0,
                     lean=lean is not None and lean * sd >= P["imb_hold"],
                     absorbed=(tc["sell"] >= tc["buy"]) if long_ else (tc["buy"] >= tc["sell"]),
                     abs_txt=f"sells {tc['sell']:.0f} / buys {tc['buy']:.0f}", rejected=pos == tc["from"])
        else:
            sd = 1 if hold_dir == "long" else -1
            armed = pos != "in" and t - st.get("since", t) >= P["away_s"] if st else pos != "in"
            H.update(state="armed" if armed else ("in zone" if pos == "in" else "resetting"),
                     reset_left=max(0, int(P["away_s"] - (t - st.get("since", t)))) if st and not armed and pos != "in" else None,
                     fresh=False, held=None, absorbed=None, lean=(lean is not None and hold_dir is not None and lean * sd >= P["imb_hold"]))
        # BREAK checklist
        B = {"state": "waiting", "dir": brk_dir}
        if o and o.get("from") and o["from"] != o["side"] and t - o["t"] < P["break_s"] + 20:
            long_ = o["side"] == "above"; sd = 1 if long_ else -1; edge = hi if long_ else lo
            B.update(dir="long" if long_ else "short", state="breaking", through=True, held_s=int(t - o["t"]),
                     stay=all((x[1] - edge) * sd > 0 for x in h if x[0] >= o["t"]),
                     lean=lean is not None and lean * sd >= P["imb_break"], delta=dpct is not None and dpct * sd >= P["delta_break"])
        else:
            sd = 1 if brk_dir == "long" else -1
            B.update(through=False, stay=None, lean=(lean is not None and brk_dir is not None and lean * sd >= P["imb_break"]),
                     delta=(dpct is not None and brk_dir is not None and dpct * sd >= P["delta_break"]))
        fired = [{"setup": x["setup"], "side": x["side"], "at": x["opened"][11:16], "status": x["status"], "r": x.get("r"),
                  "why_out": x.get("exit_reason"), "entry": x["entry"]}
                 for x in trades if x.get("zone") and abs(x["zone"][0] - lo) < 0.6 and abs(x["zone"][1] - hi) < 0.6]
        out.append({"lo": lo, "hi": hi, "name": z.get("name"), "pos": pos, "hold": H, "brk": B, "fired": fired})
    bo = ((S.get("lv") or {}).get(key) or {}).get("basis")
    return out, {"basis_open": bo, "lean": None if lean is None else round(lean, 2), "delta60": None if dpct is None else round(100 * dpct), "gate": gate,
                 "at": time.strftime("%H:%M:%S", time.localtime(t)), "fut": round(mid, 1), "imb_hold": P["imb_hold"], "imb_break": P["imb_break"],
                 "delta_break": round(100 * P["delta_break"])}


def build(key, prep, oi, flv, hot):
    """2026-11-09 v2 (Nikhil: "be careful, all prices need to be futures prices"): every row is a FUTURES price. Blocks and reference
    levels come from levelmap.get_fut (futures 1-min bars only); walls, the book and the DOM engine's zones are futures already, so no
    conversion anywhere. The only spot data left is option OI at a block (strikes are index prices): it is matched to a futures row with
    the live basis for that one lookup, and shown with its strike."""
    key = (key or "NIFTY").upper()
    FM = hot("levelmap").get_fut(key, 10) or {}
    if FM.get("error"):
        return {"key": key, "err": "no futures Level Map for " + key + ": " + str(FM["error"])}
    BW = hot("bigwalls"); W = BW.api(key) or {}
    spot = (oi or {}).get("spot") or ((prep or {}).get(key) or {}).get("spot")
    basis = (flv or {}).get("basis")
    if basis is None and W.get("price") and spot:
        basis = round(W["price"] - spot, 1)
    basis = float(basis or 0.0)
    px = W.get("price") or (FM.get("fut") or 0) + 0.0            # futures price now
    if not px:
        return {"key": key, "err": "no futures price yet"}
    P = {"blocks": {st: [{"lo": b["lo"], "hi": b["hi"], "star": bool(b.get("alerts")), "why": ", ".join(b.get("parts") or [])[:90]}
                         for b in FM.get("blocks") or [] if b.get("strength") == st] for st in ("strong", "medium", "weak")},
         "levels": FM.get("levels") or []}
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
    # OI at the blocks: oiwatch works on the SPOT level map, so its blocks are index prices - moved to futures with the live basis for
    # this one match (overlap), shown with the strikes it counted
    for b in (oi or {}).get("blocks") or []:
        bl, bh = b["lo"] + basis, b["hi"] + basis
        hit = next((x for x in rows if x["kind"] == "block" and x["lo"] <= bh and bl <= x["hi"]), None)
        if hit and not hit.get("oi"):
            d = b.get("defenders") or {}
            hit["oi"] = {"type": d.get("type"), "oi": d.get("oi"), "pct30": d.get("pct_m30"), "pct_open": d.get("pct_open"), "read": b.get("read"), "tone": b.get("tone"),
                         "strikes": b.get("strikes")}
    # big walls (futures prices already)
    for w in (W.get("above") or []) + (W.get("below") or []):
        wp = w["price"]
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
        bk_lo, bk_hi = min(p for p, _, _ in book), max(p for p, _, _ in book)
        bids = sorted(q for _, q, _ in book if q > 0); asks = sorted(q for _, _, q in book if q > 0)
        mb = bids[len(bids) // 2] if bids else 0; ma = asks[len(asks) // 2] if asks else 0
        for x in rows:
            lo, hi = x["lo"] - 2, x["hi"] + 2
            if hi < bk_lo or lo > bk_hi:
                x["book"] = None; continue
            inz = [(p, b, a) for p, b, a in book if lo <= p <= hi]
            n = max(len(inz), 1); qb = sum(b for _, b, _ in inz); qa = sum(a for _, _, a in inz)
            x["book"] = {"bid_lots": round(qb / lot), "ask_lots": round(qa / lot), "bid_x": round(qb / n / mb, 1) if mb and qb else None,
                         "ask_x": round(qa / n / ma, 1) if ma and qa else None, "buckets": len(inz)}
    try:
        DZ, dctx = _dom(hot, key, basis)
    except Exception as e:
        DZ, dctx = [], {"err": f"DOM state: {type(e).__name__}: {str(e)[:80]}"}
    if dctx.get("basis_open") is not None:                     # engine still on spot-moved levels (v1 / v2 file) - say so
        dctx["basis_warn"] = f"DOM engine levels were moved from index prices with a {dctx['basis_open']:+.0f} pt basis (old-style levels file)"
    for z in DZ:                                              # engine zones are futures prices (v3); blocks match by name
        zl, zh = z["lo"], z["hi"]
        import re as _re
        m = _re.search(r"block (\d+)-(\d+)", str(z.get("name") or ""))
        hit = None
        if m:
            hit = next((x for x in rows if x["kind"] == "block" and int(x["lo"]) == int(m.group(1)) and int(x["hi"]) == int(m.group(2))), None)
        if hit is None:
            hit = next((x for x in rows if x["kind"] != "wall" and x["lo"] <= zh + px * TOL / 100 and zl - px * TOL / 100 <= x["hi"] and not x.get("dom")), None)
        if hit is not None and not m:
            hit["tags"].append(str(z.get("name")))
        if hit is None:
            hit = {"lo": zl, "hi": zh, "kind": "dom", "strength": "dom", "star": False, "why": "DOM engine level: " + str(z["name"]), "tags": [str(z["name"])]}
            rows.append(hit)
            if book:
                inz = [(p, bq, aq) for p, bq, aq in book if zl - 2 <= p <= zh + 2]
                hit["book"] = {"bid_lots": round(sum(q for _, q, _ in inz) / lot), "ask_lots": round(sum(q for _, _, q in inz) / lot), "buckets": len(inz)} if inz else None
        hit["dom"] = z
    out = []
    for x in rows:
        mid = (x["lo"] + x["hi"]) / 2; inside = x["lo"] <= px <= x["hi"]
        side = "inside" if inside else "R" if mid > px else "S"
        dist = 0.0 if inside else (x["lo"] - px if side == "R" else x["hi"] - px)
        dp = abs(dist) / px * 100
        sc = STR.get(x["strength"], 0); why = [f"{x['strength']} level" if x["kind"] == "block" else {"ref": "reference level", "dom": "DOM engine level"}.get(x["kind"], "wall only")]
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
        dz = x.get("dom") or {}
        for f in dz.get("fired") or []:
            if f["status"] == "open":
                act, tone = f"In a DOM {f['setup'].upper()} {f['side']} since {f['at']} - manage it (stop / target / 60 min)", "bull" if f["side"] == "long" else "bear"
        hs, bs = dz.get("hold") or {}, dz.get("brk") or {}
        if hs.get("state") == "testing" and not any(f["status"] == "open" for f in dz.get("fired") or []):
            act, tone = f"Testing now - HOLD {hs['dir']} needs {', '.join(k for k in ('held', 'lean', 'absorbed') if not hs.get(k)) or 'the bounce'}", "wait"
        elif bs.get("state") == "breaking" and not any(f["status"] == "open" for f in dz.get("fired") or []):
            act, tone = f"Breaking now - BREAK {bs['dir']} needs {', '.join(k for k in ('stay', 'lean', 'delta') if not bs.get(k)) or '30 s beyond'}", "wait"
        out.append({"lo": round(x["lo"], 1), "hi": round(x["hi"], 1), "spot_lo": round(x["lo"] - basis, 1), "spot_hi": round(x["hi"] - basis, 1), "kind": x["kind"],
                    "strength": x["strength"], "star": x["star"], "why": x["why"], "tags": x["tags"], "side": side, "dist": round(dist, 1), "dist_pct": round(dist / px * 100, 2),
                    "oi": x.get("oi"), "wall": x.get("wall"), "book": x.get("book"), "score": sc, "action": act, "tone": tone, "score_why": why, "dom": x.get("dom")})
    out.sort(key=lambda r: -r["hi"])
    return {"key": key, "price": round(px, 1), "prices": "futures", "spot": round(spot, 1) if spot else None, "basis": round(basis, 1), "map_as_of": FM.get("as_of"), "at": time.strftime("%H:%M"), "oi_at": (oi or {}).get("at"),
            "walls_at": W.get("as_of"), "walls_src": W.get("source"), "book": binfo, "rows": out, "pulled": W.get("pulled") or [],
            "dom": dctx, "note": "All prices are near-month futures. Action is a fixed rule (level strength + option writers + big wall + resting orders), untested - an indication, never an order."}


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
