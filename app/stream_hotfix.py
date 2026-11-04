"""
Small fixes and diagnostics applied to the RUNNING live stream (app/stream.py stays loaded in memory, so a code change
there only takes effect after an app restart). updater.ensure_background reloads this file and calls apply() every
refresh. Keep it idempotent.

2026-10-05: Binance serves only order-book updates on fstream.binance.com/stream; trades and candles moved to the
/market route. A second connection is opened for them (spot as a fallback). stream.py has the same fix for restarts.
"""
from __future__ import annotations

import json
import sys
import threading
import time

MARKET_URLS = ("wss://fstream.binance.com/market/stream?streams={s}", "wss://stream.binance.com:9443/stream?streams={s}")


def _diag(S):
    return S.FEEDS.setdefault("_binance_diag", {"counts": {}, "sample": {}, "error": None})


def market_loop(S):
    """Trades + daily candle; moves to the next URL if no trade arrives within 60 s."""
    import websocket
    streams = "/".join(f"{s.lower()}@aggTrade/{s.lower()}@kline_1d" for s in S.COINS.values())
    i = 0
    while True:
        url = MARKET_URLS[i % len(MARKET_URLS)].format(s=streams)
        got = {"trade": 0}
        S.feed("binance_market", "connecting", url.split("?")[0])

        def on_message(ws, m, _got=got, _url=url):
            try:
                msg = json.loads(m)
                d = msg.get("data") or msg
                if d.get("e") == "aggTrade":
                    if not _got["trade"]:
                        S.feed("binance_market", "live", _url.split("?")[0])
                    _got["trade"] += 1
                S.on_binance(msg)
            except Exception as e:
                _diag(S)["error"] = repr(e)[:200]

        ws = websocket.WebSocketApp(url, on_message=on_message,
                                    on_error=lambda w, e: S.feed("binance_market", "error", str(e)[:120]))
        t = threading.Thread(target=ws.run_forever, kwargs={"ping_interval": 60, "ping_timeout": 20}, daemon=True)
        t.start()
        t0 = time.time()
        while t.is_alive():
            time.sleep(5)
            if not got["trade"] and time.time() - t0 > 60:
                S.feed("binance_market", "error", f"no trades from {url.split('?')[0]} - trying the next source")
                ws.close()
                break
        i += 0 if got["trade"] else 1                      # keep a source that worked; otherwise rotate
        time.sleep(5)


def apply():
    S = sys.modules.get("stream")
    if S is None:
        return
    try:
        _apply(S)
        S.FEEDS["_hotfix"] = {"at": time.strftime("%Y-%m-%d %H:%M:%S"), "ok": True,
                              "topup": getattr(S.poll_yahoo_once, "__name__", "?"), "has_native_topup": hasattr(S, "top_up_missing")}
    except Exception as e:
        import traceback
        S.FEEDS["_hotfix"] = {"at": time.strftime("%Y-%m-%d %H:%M:%S"), "ok": False, "error": traceback.format_exc()[-400:]}


def _apply(S):
    # 2026-10-05: Yahoo's batch call leaves out ^NSEI / ^NSEBANK - top them up one by one (stream.py has it for restarts)
    if not hasattr(S, "top_up_missing"):
        if not hasattr(S, "_orig_poll_yahoo_once"):
            S._orig_poll_yahoo_once = S.poll_yahoo_once
        import importlib
        import stream_topup
        importlib.reload(stream_topup)

        def poll(_o=S._orig_poll_yahoo_once, _t=stream_topup):
            n = _o()
            _t.top_up(S)
            return n
        S.poll_yahoo_once = poll
    if not hasattr(S, "_orig_on_binance"):
        S._orig_on_binance = S.on_binance
    diag = _diag(S)

    def on_binance(msg, _o=S._orig_on_binance, _d=diag):
        d = msg.get("data") if isinstance(msg, dict) and isinstance(msg.get("data"), dict) else msg
        k = (d.get("e") if isinstance(d, dict) else None) or "other"
        _d["counts"][k] = _d["counts"].get(k, 0) + 1
        if k not in _d["sample"]:
            _d["sample"][k] = json.dumps(msg)[:200]
        try:
            _o(msg)
        except Exception as e:
            _d["error"] = repr(e)[:200]
    S.on_binance = on_binance
    if not getattr(S, "HAS_MARKET_WS", False) and "stream_binance_market" not in {t.name for t in threading.enumerate()}:
        threading.Thread(target=market_loop, args=(S,), daemon=True, name="stream_binance_market").start()
