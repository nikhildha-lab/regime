"""BOOK WALLS - resting limit orders from SEVERAL exchanges, for the Level Board on the Crypto / Metals / Commodities tabs
(2026-11-09, Nikhil: "level board for all other tabs with whatever data we have" + "does it make sense to get multiple data connections
from crypto exchanges to get open limit orders" -> yes: one exchange's wall can be one player's spoof; a wall on 2-3 books is real liquidity).

Books (REST snapshots, every 60 s, public, no keys; each call has a hard 8 s limit and a failed exchange is simply skipped):
  crypto  : Binance USD-M (1000 levels), Bybit linear (500), OKX swap (400)  -> aggregated in USD notional
  metals / energy : Hyperliquid xyz perps (xyz:GOLD, xyz:SILVER, xyz:COPPER, xyz:CL, xyz:NATGAS, xyz:ALUMINIUM) - thin, one venue;
                    COMEX / MCX price-level depth is not available to us (MCX gives 5 aggregated levels only)
Bins: price x 0.05 %. History: the last 20 snapshots (~20 min) per key and exchange.
Big wall (same rule as app/bigwalls.py for Nifty): a bin >= 4x the median bin on its side within +-1 %, within 0.6 % of price, present in
>= 70 % of the last 15 snapshots. Standing = minutes since it first showed in the window; Trend = size now vs 5 snapshots ago.
Resting orders at a level = bid / ask notional within +-0.15 % of the level, summed over exchanges. Context only, never trades.
"""
from __future__ import annotations

import json
import threading
import time
import urllib.request
from collections import deque
from datetime import datetime

CRYPTO = ("BTC", "ETH", "SOL", "BNB", "XRP", "UNI", "ZEC", "LINK", "DASH", "AVAX")
HL = {"GOLD": "xyz:GOLD", "SILVER": "xyz:SILVER", "COPPER": "xyz:COPPER", "CRUDE": "xyz:CL", "NATGAS": "xyz:NATGAS", "ALUMINUM": "xyz:ALUMINIUM"}
BIN, NEAR, K, PRESENT, WIN, HIST = 0.0005, 0.006, 4.0, 0.7, 15, 20
S: dict = {"t": 0, "busy": False, "err": {}, "hist": {}, "at": {}}      # hist[(key, exch)] = deque of (ts, mid, {bin: (bid_usd, ask_usd)})
_LOCK = threading.Lock()


def _get(url, body=None, timeout=8):
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"})
    return json.loads(urllib.request.urlopen(req, timeout=timeout).read())


def _book(key, exch):
    """-> (bids [(px, qty)], asks [(px, qty)]) in contract units; qty converted to base units where the venue quotes otherwise."""
    if exch == "binance":
        d = _get(f"https://fapi.binance.com/fapi/v1/depth?symbol={key}USDT&limit=1000")
        return [(float(p), float(q)) for p, q in d["bids"]], [(float(p), float(q)) for p, q in d["asks"]]
    if exch == "bybit":
        d = _get(f"https://api.bybit.com/v5/market/orderbook?category=linear&symbol={key}USDT&limit=500")["result"]
        return [(float(p), float(q)) for p, q in d["b"]], [(float(p), float(q)) for p, q in d["a"]]
    if exch == "okx":
        d = _get(f"https://www.okx.com/api/v5/market/books?instId={key}-USDT-SWAP&sz=400")["data"][0]
        ct = _CT.get(key) or _okx_ct(key)                      # OKX sizes are in contracts -> base units
        return [(float(r[0]), float(r[1]) * ct) for r in d["bids"]], [(float(r[0]), float(r[1]) * ct) for r in d["asks"]]
    if exch == "hyperliquid":
        d = _get("https://api.hyperliquid.xyz/info", {"type": "l2Book", "coin": HL[key]})
        lv = d["levels"]
        return [(float(x["px"]), float(x["sz"])) for x in lv[0]], [(float(x["px"]), float(x["sz"])) for x in lv[1]]
    return [], []


_CT: dict = {}


def _okx_ct(key):
    try:
        d = _get(f"https://www.okx.com/api/v5/public/instruments?instType=SWAP&instId={key}-USDT-SWAP")["data"][0]
        _CT[key] = float(d["ctVal"]) * (1.0 if d.get("ctValCcy", key) == key else 1.0)
    except Exception:
        _CT[key] = 1.0
    return _CT[key]


def _snapshot(key, exch):
    bids, asks = _book(key, exch)
    if not bids or not asks:
        return None
    mid = (bids[0][0] + asks[0][0]) / 2; step = mid * BIN
    bins = {}
    for px, q in bids:
        if px < mid * 0.99: continue
        b = round(px / step); bins.setdefault(b, [0.0, 0.0]); bins[b][0] += px * q
    for px, q in asks:
        if px > mid * 1.01: continue
        b = round(px / step); bins.setdefault(b, [0.0, 0.0]); bins[b][1] += px * q
    cov = (min(p for p, _ in bids), max(p for p, _ in asks))          # how far the snapshot reaches (BTC: 1000 ticks of 0.1 = only ~0.13 % each side)
    return (time.time(), mid, step, bins, cov)


def _work():
    try:
        jobs = [(k, e) for k in CRYPTO for e in ("binance", "bybit", "okx")] + [(k, "hyperliquid") for k in HL]
        for key, exch in jobs:
            try:
                snap = _snapshot(key, exch)
                if snap:
                    with _LOCK:
                        S["hist"].setdefault((key, exch), deque(maxlen=HIST)).append(snap)
                        S["at"][(key, exch)] = time.time(); S["err"].pop((key, exch), None)
            except Exception as e:
                S["err"][(key, exch)] = f"{type(e).__name__}: {str(e)[:60]}"
    finally:
        S["busy"] = False


def tick(force=False):
    if S["busy"] or (not force and time.time() - S["t"] < 60):
        return False
    S["t"] = time.time(); S["busy"] = True
    threading.Thread(target=_work, name="bookwalls", daemon=True).start()
    return True


def _walls_one(hist):
    """walls from one exchange's history -> {bin: dict}"""
    snaps = list(hist)[-WIN:]
    if not snaps:
        return {}, None, None
    ts, mid, step, bins = snaps[-1][:4]
    import statistics
    out = {}
    for side, i in (("buy", 0), ("sell", 1)):
        vals = [v[i] for b, v in bins.items() if v[i] > 0 and abs(b * step / mid - 1) <= 0.01]
        med = statistics.median(vals) if len(vals) >= 5 else None
        if not med:
            continue
        for b, v in bins.items():
            px = b * step
            if v[i] >= K * med and abs(px / mid - 1) <= NEAR and (side == "buy") == (px < mid):
                pres = sum(1 for s in snaps if (s[3].get(round(px / s[2])) or [0, 0])[i] >= K * med) / len(snaps)
                if pres < PRESENT:
                    continue
                first = next((s[0] for s in snaps if (s[3].get(round(px / s[2])) or [0, 0])[i] >= K * med), ts)
                old = (snaps[-6][3].get(round(px / snaps[-6][2])) or [0, 0])[i] if len(snaps) >= 6 else v[i]
                out[(side, round(px, 8))] = {"price": px, "side": side, "x": round(v[i] / med, 1), "usd": v[i], "age_min": int((ts - first) / 60) + 1,
                                            "trend": "growing" if v[i] > old * 1.15 else "shrinking" if v[i] < old * 0.85 else "steady", "present": round(pres, 2)}
    return out, mid, step


def walls(key):
    """aggregated walls for one market: merged across exchanges (same bin), with the list of venues where it is a wall on its own."""
    with _LOCK:
        per = {e: list(h) for (k, e), h in S["hist"].items() if k == key and h}
    if not per:
        return {"key": key, "price": None, "walls": [], "exchanges": [], "note": "no order book yet"}
    agg = {}; mids = []
    for e, h in per.items():
        w, mid, step = _walls_one(h)
        if mid: mids.append(mid)
        for (side, px), d in w.items():
            b = round(px / (mids[0] * BIN)) if mids else round(px)
            g = agg.setdefault((side, b), {"price": px, "side": side, "usd": 0.0, "x": 0.0, "on": [], "age_min": 0, "trend": []})
            g["usd"] += d["usd"]; g["x"] = max(g["x"], d["x"]); g["on"].append(e); g["age_min"] = max(g["age_min"], d["age_min"]); g["trend"].append(d["trend"])
    mid = sum(mids) / len(mids) if mids else None
    out = []
    for g in agg.values():
        tr = max(set(g["trend"]), key=g["trend"].count)
        out.append({"price": g["price"], "side": g["side"], "x": g["x"], "usd": round(g["usd"]), "exchanges": sorted(g["on"]), "n_ex": len(g["on"]), "age_min": g["age_min"], "trend": tr,
                    "dist_pct": round(100 * (g["price"] / mid - 1), 3) if mid else None})
    out.sort(key=lambda w: -w["usd"])
    return {"key": key, "price": mid, "walls": out, "exchanges": sorted(per), "coverage": coverage(key), "at": datetime.now().strftime("%H:%M:%S"),
            "err": {e: v for (k, e), v in S["err"].items() if k == key}}


def resting(key, lo, hi):
    """bid / ask notional (USD) resting between lo and hi, summed over the exchanges whose snapshot reaches that far (None = beyond every book)."""
    with _LOCK:
        per = {e: h[-1] for (k, e), h in S["hist"].items() if k == key and h}
    bid = ask = 0.0; n = 0
    for e, snap in per.items():
        ts, mid, step, bins = snap[:4]; cov = snap[4] if len(snap) > 4 else (0, 1e18)
        if hi < cov[0] or lo > cov[1]:
            continue
        n += 1
        for b, v in bins.items():
            px = b * step
            if lo <= px <= hi:
                bid += v[0]; ask += v[1]
    return {"bid_usd": round(bid), "ask_usd": round(ask), "exchanges": n} if n else None


def coverage(key):
    """how far the books reach, in % of price (min over venues of the bid side / max of the ask side)."""
    with _LOCK:
        per = [h[-1] for (k, e), h in S["hist"].items() if k == key and h]
    if not per or any(len(x) < 5 for x in per):
        return None
    lo = min(x[4][0] / x[1] - 1 for x in per) * 100; hi = max(x[4][1] / x[1] - 1 for x in per) * 100
    return {"down_pct": round(lo, 2), "up_pct": round(hi, 2)}


def api(key):
    return walls(key)
