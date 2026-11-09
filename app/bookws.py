"""LIVE ORDER BOOKS over WebSockets - several venues, maintained locally (2026-11-10, Nikhil: "OK TRY" after reviewing
github.com/zadescoxp/order-flow-screener, whose exchange adapters were the template: Binance futures diff depth, Bybit v5 orderbook
snapshot + delta with sequence check, OKX / Bitget books snapshot + update, Hyperliquid l2Book snapshots).

Why: the REST snapshots in app/bookwalls.py see only the top of each book (BTC: 1000 ticks = +-0.15 %) once a minute. A local book fed by
the diff stream keeps every price level that has changed since the snapshot, so it reaches further as the minutes pass, and it is
second-accurate (walls appearing / pulled are seen at once). bookwalls.py reads these books first and falls back to REST.

Venues : binance (fstream, <sym>@depth@500ms, seeded by /fapi/v1/depth?limit=1000, U/u/pu sequence check as Binance documents)
         bybit   (public/linear, orderbook.200.<SYM>, snapshot + delta, 'u' sequence; gap -> resubscribe)
         okx     (ws/v5/public, books = 400 levels, snapshot + update)      - may be unreachable from India (REST timed out)
         bitget  (v2/ws/public, books = full depth, snapshot + update)
         hyperliquid (l2Book snapshots for the xyz metal / energy perps)
One connection per venue, all symbols subscribed on it. Reconnects with back-off. Read-only, public streams, no keys.
books() -> {(key, venue): {"bids": {px: qty}, "asks": {px: qty}, "ts": epoch}}  (qty in base units).
"""
from __future__ import annotations

import json
import threading
import time
import urllib.request

CRYPTO = ("BTC", "ETH", "SOL", "BNB", "XRP", "UNI", "ZEC", "LINK", "DASH", "AVAX")
HL = {"GOLD": "xyz:GOLD", "SILVER": "xyz:SILVER", "COPPER": "xyz:COPPER", "CRUDE": "xyz:CL", "NATGAS": "xyz:NATGAS", "ALUMINUM": "xyz:ALUMINIUM"}
_B: dict = {}                       # (key, venue) -> {"bids": {}, "asks": {}, "ts": float, "seq": int, "ok": bool}
_LOCK = threading.Lock()
STATE: dict = {"started": False, "venues": {}}
MAX_LEVELS = 6000                   # per side; beyond that the far tail is trimmed


def _get(url, timeout=8):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    return json.loads(urllib.request.urlopen(req, timeout=timeout).read())


def _book(key, venue):
    b = _B.get((key, venue))
    if b is None:
        b = _B[(key, venue)] = {"bids": {}, "asks": {}, "ts": 0.0, "seq": 0, "ok": False}
    return b


def _apply(b, bids, asks, reset=False):
    if reset:
        b["bids"].clear(); b["asks"].clear()
    for side, rows in (("bids", bids), ("asks", asks)):
        d = b[side]
        for p, q in rows:
            p = float(p); q = float(q)
            if q <= 0:
                d.pop(p, None)
            else:
                d[p] = q
    if len(b["bids"]) > MAX_LEVELS:
        for p in sorted(b["bids"])[: len(b["bids"]) - MAX_LEVELS]: b["bids"].pop(p, None)
    if len(b["asks"]) > MAX_LEVELS:
        for p in sorted(b["asks"], reverse=True)[: len(b["asks"]) - MAX_LEVELS]: b["asks"].pop(p, None)
    b["ts"] = time.time(); b["ok"] = bool(b["bids"] and b["asks"])


def _status(venue, state, note=""):
    STATE["venues"][venue] = {"state": state, "note": note[:120], "at": time.strftime("%H:%M:%S")}


def _run(venue, url, on_open, on_message, ping=None, text_ping=None):
    """text_ping: some venues (OKX, Bitget) want the literal text 'ping' every ~25 s instead of a WebSocket ping frame."""
    import websocket
    wait = 5
    while True:
        t0 = time.time(); _status(venue, "connecting")
        try:
            def _open(w):
                on_open(w)
                if text_ping:
                    def loop():
                        while getattr(w, "keep_running", False) and w.sock and w.sock.connected:
                            try: w.send(text_ping)
                            except Exception: break
                            time.sleep(25)
                    threading.Thread(target=loop, daemon=True).start()
            def _msg(w, m):
                STATE.setdefault("last", {})[venue] = str(m)[:300]          # last raw message, for debugging from /api/livedesk/bookwalls
                on_message(w, m)
            ws = websocket.WebSocketApp(url, on_open=_open, on_message=_msg,
                                        on_error=lambda w, e: _status(venue, "error", str(e)))
            ws.run_forever(ping_interval=(ping if ping is not None else 60) or 0, ping_timeout=20 if (ping is None or ping) else None)
        except Exception as e:
            _status(venue, "error", str(e))
        with _LOCK:
            for k, b in _B.items():
                if k[1] == venue: b["ok"] = False
        wait = 5 if time.time() - t0 > 120 else min(wait * 2, 300)
        _status(venue, "off", f"reconnecting in {wait}s"); time.sleep(wait)


# ------------------------------------------------------------------ Binance USD-M futures: diff depth, seeded by a REST snapshot
def _binance():
    pend: dict = {}                    # key -> buffered diffs until the snapshot is in
    def seed(key):
        try:
            snap = _get(f"https://fapi.binance.com/fapi/v1/depth?symbol={key}USDT&limit=1000")
        except Exception as e:
            _status("binance", "live", f"snapshot {key} failed: {str(e)[:60]}"); return
        with _LOCK:
            b = _book(key, "binance"); _apply(b, snap["bids"], snap["asks"], reset=True); b["seq"] = snap["lastUpdateId"]
            for ev in pend.pop(key, []):
                if ev["u"] >= b["seq"]:
                    _apply(b, ev["b"], ev["a"]); b["seq"] = ev["u"]
    seedq: list = []
    def seeder():
        while True:
            if seedq:
                k = seedq.pop(0); seed(k); time.sleep(1.5)        # one snapshot every 1.5 s keeps well inside Binance's weight limit
            else:
                time.sleep(0.5)
    threading.Thread(target=seeder, name="bookws-binance-seed", daemon=True).start()
    def on_open(ws):
        _status("binance", "live", "depth streams")
        for k in CRYPTO:
            if k not in seedq: seedq.append(k)
    def on_message(ws, m):
        try:
            d = json.loads(m).get("data") or {}
            if d.get("e") != "depthUpdate": return
            key = d["s"][:-4]
            with _LOCK:
                b = _book(key, "binance")
                if not b["seq"]:
                    pend.setdefault(key, []).append(d); return
                if d["pu"] != b["seq"]:                      # sequence gap -> re-seed (as Binance documents)
                    b["seq"] = 0; b["ok"] = False
                    if key not in seedq: seedq.append(key)
                    return
                _apply(b, d["b"], d["a"]); b["seq"] = d["u"]
        except Exception:
            pass
    url = "wss://fstream.binance.com/stream?streams=" + "/".join(f"{k.lower()}usdt@depth@500ms" for k in CRYPTO)
    _run("binance", url, on_open, on_message)


# ------------------------------------------------------------------ Bybit v5 linear: orderbook.200 snapshot + delta
def _bybit():
    def on_open(ws):
        _status("bybit", "live", "orderbook.200")
        ws.send(json.dumps({"op": "subscribe", "args": [f"orderbook.200.{k}USDT" for k in CRYPTO]}))
    def on_message(ws, m):
        try:
            x = json.loads(m)
            if x.get("op") in ("pong", "ping", "subscribe"):
                if x.get("op") == "subscribe" and not x.get("success", True): _status("bybit", "live", "subscribe failed: " + str(x.get("ret_msg"))[:80])
                return
            if not str(x.get("topic", "")).startswith("orderbook."): return
            key = x["data"]["s"][:-4]; d = x["data"]
            with _LOCK:
                b = _book(key, "bybit")
                if x["type"] == "snapshot":
                    _apply(b, d["b"], d["a"], reset=True); b["seq"] = d["u"]; return
                if b["seq"] and d["u"] != b["seq"] + 1 and d["u"] != b["seq"]:   # gap -> ask for a fresh snapshot
                    b["ok"] = False; b["seq"] = 0
                    ws.send(json.dumps({"op": "unsubscribe", "args": [f"orderbook.200.{key}USDT"]})); ws.send(json.dumps({"op": "subscribe", "args": [f"orderbook.200.{key}USDT"]})); return
                _apply(b, d["b"], d["a"]); b["seq"] = d["u"]
        except Exception:
            pass
    def ping_loop(ws):
        pass
    _run("bybit", "wss://stream.bybit.com/v5/public/linear", on_open, on_message, ping=0, text_ping='{"op":"ping"}')


# ------------------------------------------------------------------ OKX swap: books (400 levels) snapshot + update
def _okx():
    def on_open(ws):
        _status("okx", "live", "books")
        ws.send(json.dumps({"op": "subscribe", "args": [{"channel": "books", "instId": f"{k}-USDT-SWAP"} for k in CRYPTO]}))
    ct: dict = {}
    def on_message(ws, m):
        try:
            if m == "pong": return
            x = json.loads(m)
            if (x.get("arg") or {}).get("channel") != "books": return
            key = x["arg"]["instId"].split("-")[0]
            if key not in ct:
                try:
                    inst = _get(f"https://www.okx.com/api/v5/public/instruments?instType=SWAP&instId={key}-USDT-SWAP")["data"][0]; ct[key] = float(inst["ctVal"])
                except Exception:
                    ct[key] = 1.0
            for d in x.get("data") or []:
                with _LOCK:
                    b = _book(key, "okx")
                    bids = [(r[0], float(r[1]) * ct[key]) for r in d["bids"]]; asks = [(r[0], float(r[1]) * ct[key]) for r in d["asks"]]
                    _apply(b, bids, asks, reset=(x.get("action") == "snapshot"))
        except Exception:
            pass
    _run("okx", "wss://ws.okx.com:8443/ws/v5/public", on_open, on_message, ping=0, text_ping="ping")


# ------------------------------------------------------------------ Bitget USDT futures: books (full depth) snapshot + update
def _bitget():
    def on_open(ws):
        _status("bitget", "live", "books")
        ws.send(json.dumps({"op": "subscribe", "args": [{"instType": "USDT-FUTURES", "channel": "books", "instId": f"{k}USDT"} for k in CRYPTO]}))
    def on_message(ws, m):
        try:
            if m == "pong": return
            x = json.loads(m)
            if (x.get("arg") or {}).get("channel") != "books": return
            key = x["arg"]["instId"][:-4]
            for d in x.get("data") or []:
                with _LOCK:
                    _apply(_book(key, "bitget"), d["bids"], d["asks"], reset=(x.get("action") == "snapshot"))
        except Exception:
            pass
    _run("bitget", "wss://ws.bitget.com/v2/ws/public", on_open, on_message, ping=0, text_ping="ping")


# ------------------------------------------------------------------ Hyperliquid xyz perps: l2Book snapshots
def _hyperliquid():
    def on_open(ws):
        _status("hyperliquid", "live", "l2Book")
        for k, c in HL.items():
            ws.send(json.dumps({"method": "subscribe", "subscription": {"type": "l2Book", "coin": c}}))
    inv = {v: k for k, v in HL.items()}
    def on_message(ws, m):
        try:
            x = json.loads(m)
            if x.get("channel") != "l2Book": return
            d = x["data"]; key = inv.get(d["coin"])
            if not key: return
            lv = d["levels"]
            with _LOCK:
                _apply(_book(key, "hyperliquid"), [(r["px"], r["sz"]) for r in lv[0]], [(r["px"], r["sz"]) for r in lv[1]], reset=True)
        except Exception:
            pass
    _run("hyperliquid", "wss://api.hyperliquid.xyz/ws", on_open, on_message, ping=50)


def start():
    if STATE["started"]:
        return False
    STATE["started"] = True
    for name, fn in (("binance", _binance), ("bybit", _bybit), ("okx", _okx), ("bitget", _bitget), ("hyperliquid", _hyperliquid)):
        threading.Thread(target=fn, name=f"bookws-{name}", daemon=True).start()
    return True


def books(max_age=30):
    """fresh local books: {(key, venue): {"bids": [(px, qty)...], "asks": [...], "ts": epoch}} - lists sorted best first."""
    out = {}
    now = time.time()
    with _LOCK:
        for (key, venue), b in _B.items():
            if b["ok"] and now - b["ts"] <= max_age:
                out[(key, venue)] = {"bids": sorted(b["bids"].items(), reverse=True), "asks": sorted(b["asks"].items()), "ts": b["ts"]}
    return out


def status():
    with _LOCK:
        n = {f"{k}/{v}": (len(b["bids"]), len(b["asks"]), round(time.time() - b["ts"]) if b["ts"] else None) for (k, v), b in _B.items() if b["ok"]}
    return {"started": STATE["started"], "venues": STATE["venues"], "books": n, "last": STATE.get("last", {})}
