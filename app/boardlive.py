"""
Live prices for the Regime Board (2026-11-11, Nikhil: "LTP should refresh every second, live feed wherever possible").

Started on demand: the board page asks every second (through regimeboard.detail("_live")), and the feeds stop on
their own about 90 seconds after the page stops asking, so nothing runs while nobody is looking.

Feeds
  * Crypto: Binance spot websocket. miniTicker gives the last price once a second; kline_1d gives today's open
    (00:00 UTC), so the change matches the board's own daily change.
  * Metals, commodities, US: a Yahoo batch poll every 5 seconds (two calls for all of them). Yahoo's streaming
    websocket is kept too, but it only pushes US indices during US hours and never pushes futures. Yahoo's futures
    prices (gold, silver, crude ...) usually run about 10 minutes behind the exchange.
  * India (indices and the stocks under them): one Dhan quote call for all of them, every 2 seconds in NSE hours and
    every 60 seconds outside. Dhan allows about one quote call a second for the whole app, and the live stream
    (app/stream.py) already uses the other half, so 2 seconds is the fastest India can go.

Read-only. Nothing here places orders.
"""
from __future__ import annotations

import base64
import json
import os
import threading
import time
from datetime import datetime

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
LATEST = os.path.join(ROOT, "data_store", "livedesk", "regimeboard", "latest.json")
IDLE = 90                     # seconds without a page asking before the feeds stop
INDIA_LIVE_EVERY = 2
INDIA_SHUT_EVERY = 60

# Kept across reloads: a reload bumps "gen" and the old threads see the new number and stop.
_B = globals().get("_B") or {"gen": 0, "px": {}, "asked": 0.0, "lock": threading.Lock(), "feeds": {},
                              "syms": None, "syms_t": 0.0, "threads": {}}
_B["gen"] += 1
GEN = _B["gen"]


def _alive(gen: int) -> bool:
    return _B["gen"] == gen and time.time() - _B["asked"] < IDLE


def _feed(name: str, state: str, detail: str = "", gen: int | None = None):
    if gen is not None and gen != _B["gen"]:          # a thread replaced by a reload keeps quiet
        return
    _B["feeds"][name] = {"state": state, "detail": detail[:140], "at": datetime.now().isoformat(timespec="seconds")}


def _put(key: str, px, chg, t: float):
    try:
        px = float(px)
    except (TypeError, ValueError):
        return
    with _B["lock"]:
        _B["px"][key] = {"px": px, "chg": None if chg is None else round(float(chg), 2), "t": round(t, 1)}


def _nse_open() -> bool:
    n = datetime.now()
    return n.weekday() < 5 and "09:15" <= n.strftime("%H:%M") <= "15:30"


def _symbols() -> dict:
    """What the board shows, from its own saved file (re-read every 5 minutes)."""
    if _B["syms"] and time.time() - _B["syms_t"] < 300:
        return _B["syms"]
    out = {"idx": set(), "eq": set(), "crypto": set(), "yahoo": set()}
    try:
        st = json.load(open(LATEST))
        for r in (st.get("rows") or {}).values():
            g, s = r.get("group"), r.get("sym")
            if s:
                if g == "india":
                    out["idx"].add(s)
                elif g == "crypto":
                    out["crypto"].add(s)
                elif g in ("metals", "global", "us"):
                    out["yahoo"].add(s)
            for k in r.get("children") or []:
                if k.get("sym"):
                    out["eq"].add(k["sym"])
    except Exception as e:
        _feed("symbols", "error", f"{type(e).__name__}: {e}")
    _B["syms"], _B["syms_t"] = out, time.time()
    return out


# ------------------------------------------------------------------ India: Dhan quotes
def _dhan_loop(gen: int):
    import dhan_live as DL
    ids, ids_t = {}, 0.0
    while _alive(gen):
        try:
            if not DL.ok():
                _feed("dhan", "off", "no valid Dhan login")
                time.sleep(30)
                continue
            if time.time() - ids_t > 300:
                sy, ids = _symbols(), {}
                for s in sy["idx"]:
                    i = DL.ids_index(s)
                    if i:
                        ids[("IDX_I", str(i))] = "idx|" + s
                for s in sy["eq"]:
                    i = DL.ids_equity(s)
                    if i:
                        ids[("NSE_EQ", str(i))] = "eq|" + s
                ids_t = time.time()
            req: dict = {}
            for seg, i in ids:
                req.setdefault(seg, []).append(int(i))
            if req:
                data = DL.quote(req)
                now = time.time()
                for seg, rows in (data or {}).items():
                    for sid, row in (rows or {}).items():
                        k = ids.get((seg, str(sid)))
                        if not k:
                            continue
                        f = DL.row_fields(row)
                        if f.get("ltp") is None:
                            continue
                        pc = f.get("prev_close")
                        _put(k, f["ltp"], None if not pc else (float(f["ltp"]) / float(pc) - 1) * 100, now)
                _feed("dhan", "live", f"{len(ids)} instruments, every {INDIA_LIVE_EVERY if _nse_open() else INDIA_SHUT_EVERY} s")
        except Exception as e:
            _feed("dhan", "error", f"{type(e).__name__}: {e}")
            time.sleep(10)
        time.sleep(INDIA_LIVE_EVERY if _nse_open() else INDIA_SHUT_EVERY)
    _feed("dhan", "stopped", "no page asking", gen)


# ------------------------------------------------------------------ Crypto: Binance spot websocket
def _binance_loop(gen: int):
    import websocket
    opens: dict = {}
    while _alive(gen):
        syms = sorted(_symbols()["crypto"])
        if not syms:
            time.sleep(30)
            continue
        streams = "/".join(f"{s.lower()}@miniTicker/{s.lower()}@kline_1d" for s in syms)
        url = "wss://stream.binance.com:9443/stream?streams=" + streams

        def on_message(ws, m):
            if not _alive(gen):
                ws.close()
                return
            try:
                d = (json.loads(m) or {}).get("data") or {}
                s = d.get("s")
                if not s:
                    return
                if d.get("e") == "kline":
                    opens[s] = float(d["k"]["o"])
                    c = float(d["k"]["c"])
                elif d.get("e") == "24hrMiniTicker":
                    c = float(d["c"])
                else:
                    return
                o = opens.get(s)
                _put("c|" + s, c, (c / o - 1) * 100 if o else None, time.time())
            except Exception:
                pass

        ws = websocket.WebSocketApp(url, on_message=on_message,
                                    on_open=lambda w: _feed("binance", "live", f"{len(syms)} coins"),
                                    on_error=lambda w, e: _feed("binance", "error", str(e)))
        t = threading.Thread(target=ws.run_forever, kwargs={"ping_interval": 60, "ping_timeout": 20}, daemon=True)
        t.start()
        while t.is_alive():
            time.sleep(5)
            if not _alive(gen) or sorted(_symbols()["crypto"]) != syms:
                ws.close()
                break
        time.sleep(3)
    _feed("binance", "stopped", "no page asking", gen)


# ------------------------------------------------------------------ Metals, commodities, US: Yahoo streamer
def _yahoo_loop(gen: int):
    import websocket
    try:
        import stream as STR                    # already loaded by the live stream; only its decoder is used
        decode = STR._pb_pricing
    except Exception as e:
        _feed("yahoo", "error", f"no decoder: {e}")
        return
    while _alive(gen):
        syms = sorted(_symbols()["yahoo"])
        if not syms:
            time.sleep(30)
            continue

        def on_open(ws, _s=syms):
            ws.send(json.dumps({"subscribe": _s}))
            _feed("yahoo", "live", f"{len(_s)} symbols (pushes while each market trades)")

        def on_message(ws, msg):
            if not _alive(gen):
                ws.close()
                return
            try:
                if isinstance(msg, bytes):
                    msg = msg.decode()
                if msg.startswith("{"):
                    msg = (json.loads(msg) or {}).get("message") or ""
                    if not msg:
                        return
                d = decode(base64.b64decode(msg))
                sym, px = d.get("id"), d.get("price")
                if sym and px:
                    _put("y|" + sym, px, d.get("chg_pct"), (d.get("time") or 0) / 1000 or time.time())
            except Exception:
                pass

        ws = websocket.WebSocketApp("wss://streamer.finance.yahoo.com/?version=2", on_open=on_open, on_message=on_message,
                                    on_error=lambda w, e: _feed("yahoo", "error", str(e)),
                                    header={"User-Agent": "Mozilla/5.0", "Origin": "https://finance.yahoo.com"})
        t = threading.Thread(target=ws.run_forever, kwargs={"ping_interval": 30, "ping_timeout": 10}, daemon=True)
        t.start()
        while t.is_alive():
            time.sleep(5)
            if not _alive(gen) or sorted(_symbols()["yahoo"]) != syms:
                ws.close()
                break
        time.sleep(3)
    _feed("yahoo", "stopped", "no page asking", gen)


YAHOO_POLL_EVERY = 5


def _yahoo_poll_loop(gen: int):
    """Batch quotes for the board's Yahoo symbols (the same spark call the live stream uses, 10 symbols a call)."""
    import urllib.parse
    try:
        import stream as STR
        import updater as U
    except Exception as e:
        _feed("yahoo_poll", "error", f"no Yahoo helpers: {e}")
        return
    wait = YAHOO_POLL_EVERY
    while _alive(gen):
        syms = sorted(_symbols()["yahoo"])
        try:
            got: dict = {}
            for i in range(0, len(syms), 10):
                q = f"/v8/finance/spark?symbols={urllib.parse.quote(','.join(syms[i:i + 10]))}&range=1d&interval=1m"
                err = None
                for host in U.YAHOO_HOSTS:
                    try:
                        got.update(STR.parse_spark(json.loads(U.yahoo_get(host + q))))
                        err = None
                        break
                    except Exception as e:
                        err = e
                if err is not None:
                    raise RuntimeError(str(err)[:120])
            for sym, v in got.items():
                pc = v.get("prev_close")
                k = "y|" + sym
                cur = _B["px"].get(k)
                t = float(v.get("t") or time.time())
                if cur and cur.get("t", 0) > t:          # a newer push from the websocket wins
                    continue
                _put(k, v["last"], None if not pc else (v["last"] / pc - 1) * 100, t)
            _feed("yahoo_poll", "live", f"{len(got)} of {len(syms)} symbols every {YAHOO_POLL_EVERY} s")
            wait = YAHOO_POLL_EVERY
        except Exception as e:
            _feed("yahoo_poll", "error", f"{type(e).__name__}: {e}")
            wait = min(wait * 2, 60)                       # back off if Yahoo pushes back
        time.sleep(wait)
    _feed("yahoo_poll", "stopped", "no page asking", gen)


LOOPS = {"dhan": _dhan_loop, "binance": _binance_loop, "yahoo": _yahoo_loop, "yahoo_poll": _yahoo_poll_loop}


def _ensure():
    for name, fn in LOOPS.items():
        g, th = _B["threads"].get(name, (None, None))
        if g == GEN and th is not None and th.is_alive():
            continue
        th = threading.Thread(target=fn, args=(GEN,), daemon=True, name="boardlive_" + name)
        _B["threads"][name] = (GEN, th)
        th.start()


def snap() -> dict:
    """Latest prices: {key: {px, chg, t}} with keys idx|<Dhan name>, eq|<NSE symbol>, c|<Binance pair>, y|<Yahoo symbol>."""
    _B["asked"] = time.time()
    _ensure()
    with _B["lock"]:
        px = dict(_B["px"])
    return {"t": round(time.time(), 1), "px": px, "feeds": dict(_B["feeds"])}
