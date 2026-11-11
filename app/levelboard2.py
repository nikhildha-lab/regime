"""LEVEL BOARD for the Crypto / Metals / Commodities tabs (2026-11-09, Nikhil: "can we get level board for all other tabs with whatever
data we have or can arrange"). Same idea as app/levelboard.py for Nifty, built from what these markets give us:
  levels : app/reflevels.py (PDH / PDL, PWH / PWL, PMH / PML, 5-day VAH, day VWAP, IBH / IBL) + the tracker's swing
           support / resistance clusters (view["sr"])
  walls / resting orders : app/bookwalls.py - crypto from Binance + Bybit + OKX books (aggregated, with the count of venues that show the
           wall), metals / energy from the Hyperliquid xyz perps book (thin; COMEX / MCX depth is not available)
  no OI-at-level column (no option chain on these markets); the Next Day column (Rise / Fall 4h score) is added in the browser.
Rows within +-1.5 % of price. Prices = the exchange's last / mid. Context only, never trades.
"""
from __future__ import annotations

import time
from datetime import datetime

RANGE, BAND = 0.015, 0.001
_C: dict = {}
TYPE = {"PDH": "Prev day high", "PDL": "Prev day low", "PWH": "Prev week high", "PWL": "Prev week low", "PMH": "Prev month high", "PML": "Prev month low",
        "VAH": "Value area high", "VAL": "Value area low", "POC": "Point of control", "VWAP": "Day VWAP", "IBH": "Initial balance high", "IBL": "Initial balance low"}


def build(key: str, view: dict | None, hot) -> dict:
    RL = hot("reflevels").get(key) or {}
    BW = hot("bookwalls")
    W = BW.walls(key)
    px = W.get("price") or RL.get("spot") or ((view or {}).get("spot"))
    if not px:
        return {"key": key, "err": "no price yet", "rows": []}
    rows = []
    for l in RL.get("levels") or []:
        if abs(l["px"] / px - 1) <= RANGE:
            rows.append({"name": l["short"], "kind": TYPE.get(l["short"], l["name"]), "px": l["px"], "why": l["name"]})
    sr = (view or {}).get("sr") or {}
    for side, lab in (("support", "Swing support"), ("resistance", "Swing resistance")):
        for v in sr.get(side) or []:
            if v and abs(float(v) / px - 1) <= RANGE and not any(abs(r["px"] / float(v) - 1) < BAND for r in rows):
                rows.append({"name": lab.split()[1][0].upper() + lab.split()[1][1:], "kind": lab, "px": float(v), "why": "tracker swing cluster"})
    walls = [w for w in W.get("walls") or [] if abs(w["price"] / px - 1) <= RANGE]
    for w in walls:                                             # walls that sit on no level: their own row
        if not any(abs(r["px"] / w["price"] - 1) < BAND * 1.5 for r in rows):
            rows.append({"name": f"{'Sell' if w['side']=='sell' else 'Buy'} wall", "kind": "Wall only", "px": w["price"], "why": f"resting {'asks' if w['side']=='sell' else 'bids'} on {w['n_ex']} venue(s)"})
    out = []
    for r in rows:
        lo, hi = r["px"] * (1 - BAND), r["px"] * (1 + BAND)
        w = next((w for w in walls if lo * 0.9995 <= w["price"] <= hi * 1.0005), None)
        bk = BW.resting(key, lo, hi)
        side = "R" if r["px"] > px * (1 + BAND / 2) else "S" if r["px"] < px * (1 - BAND / 2) else "inside"
        out.append({**r, "lo": lo, "hi": hi, "side": side, "dist": r["px"] - px, "dist_pct": round(100 * (r["px"] / px - 1), 3),
                    "wall": w, "book": bk})
    out.sort(key=lambda r: -r["px"])
    return {"key": key, "price": px, "rows": out, "walls_at": W.get("at"), "exchanges": W.get("exchanges") or [], "coverage": W.get("coverage"), "levels_src": RL.get("source"), "levels_at": RL.get("at"),
            "n_walls": len(walls), "err": None, "book_err": W.get("err") or {}, "at": datetime.now().strftime("%H:%M:%S"),
            "note": "metals / energy book = Hyperliquid xyz perps only (thin); COMEX / MCX price-level depth is not available" if key not in BW.CRYPTO else None}


def api(key, view, hot):
    c = _C.get(key)
    if c and time.time() - c[0] < 20:
        return c[1]
    try:
        v = build(key, view, hot)
    except Exception as e:
        v = {"key": key, "err": f"{type(e).__name__}: {str(e)[:120]}", "rows": []}
    _C[key] = (time.time(), v)
    return v
