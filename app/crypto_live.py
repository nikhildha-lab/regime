"""Crypto live engine inside Market Pulse (2026-11-01, Nikhil: "merge them, no dependency at the backend").
Same engine and rules as the old Documents/crypto-tracker app (code now in app/cryptoengine), run as threads in this
process: Hyperliquid (+ Binance) websocket feeds -> 5-min candles, footprint, CVD, levels, setups -> paper book.
livedesk.py reads snapshot() directly instead of http://127.0.0.1:8898. Data: data_store/crypto_engine/.
Normal import only (never hot-reloaded) so the running feeds survive livedesk reloads. Research / paper only - no orders."""
from __future__ import annotations

import threading
import time
import traceback

_E = globals().get("_E") or {"running": False, "started": None, "error": None, "snap": None, "stop": None, "feed": None,
                              "recorder": None, "focus": {"keys": set()}, "lock": threading.Lock()}
ALWAYS_LIVE = {"BTC", "ETH", "SOL"}


def snapshot() -> dict:
    """Latest full snapshot (same shape as the old /api/snapshot?full=1)."""
    with _E["lock"]:
        s = _E["snap"]
    if s is None:
        raise RuntimeError(_E["error"] or ("crypto engine starting" if _E["running"] else "crypto engine not running"))
    return s


def status() -> dict:
    s = _E["snap"] or {}
    return {"running": _E["running"], "started": _E["started"], "error": _E["error"], "generated": s.get("generated_at"),
            "instruments": len(s.get("instruments") or {})}


def _bar_marker(snap):
    return {k: (v.get("candles") or [{}])[-1].get("t") for k, v in snap.get("instruments", {}).items()}


def _compute_loop(engine, alerter, period_s, stop, state, recorder, paper):
    last_bar, last_paper = None, {"enabled": False}
    stable_top, stable_sig, stable_reg = [], {}, {}
    while not stop.is_set():
        try:
            snap = engine.compute(focus=set(_E["focus"]["keys"]) | ALWAYS_LIVE)
            bar = _bar_marker(snap)
            first = last_bar is None
            bar_closed = (not first) and bool(bar) and any(bar.get(k) != (last_bar or {}).get(k) for k in bar)
            if first or bar_closed:
                last_bar = bar
                stable_top = snap.get("top_signals", [])
                stable_sig = {k: v.get("signals", []) for k, v in snap.get("instruments", {}).items()}
                stable_reg = {k: v.get("regime", {}) for k, v in snap.get("instruments", {}).items()}
                if bar_closed:
                    if paper is not None:
                        last_paper = paper.update(snap)
                    alerter.dispatch(snap)
                    if recorder is not None:
                        recorder.capture(state)
            if paper is not None and not bar_closed:
                try:
                    last_paper = paper.tick(snap) or last_paper
                except Exception as e:
                    print(f"[crypto engine] paper tick error: {e}")
            snap["top_signals"] = stable_top
            for k, v in snap.get("instruments", {}).items():
                if k in stable_sig:
                    v["signals"] = stable_sig[k]
                if k in stable_reg:
                    v["regime"] = stable_reg[k]
            if paper is not None:
                snap["paper"] = last_paper
            with _E["lock"]:
                _E["snap"] = snap
            _E["error"] = None
        except Exception as e:
            _E["error"] = f"compute error: {e}"[:200]
            traceback.print_exc()
        stop.wait(period_s)


_START = globals().get("_START") or threading.Lock()
_ALL_FEEDS = globals().get("_ALL_FEEDS") or []      # every feed ever started, so stop() can never leave one running


def start() -> dict:
    """2026-11-03: one engine only. Before, the Live Desk poll could start a second engine while a restart was still
    seeding (start() takes several seconds); the first engine's feeds were never stopped, so app/stream.py received
    every Binance trade two or three times (1-min / 5-min buy-sell numbers too big; session CVD wrong)."""
    if _E["running"] or _E.get("starting"):
        return {"ok": True, "detail": "crypto engine already running" if _E["running"] else "crypto engine already starting"}
    with _START:
        if _E["running"]:
            return {"ok": True, "detail": "crypto engine already running"}
        _E["starting"] = True
        try:
            return _start()
        finally:
            _E["starting"] = False


def _start() -> dict:
    try:
        from cryptoengine.config import load_config
        from cryptoengine.state import MarketState
        from cryptoengine.engine import Engine
        from cryptoengine.alerts import Alerter
        from cryptoengine.hl_data import HLData
        from cryptoengine.binance_data import BinanceData
        from cryptoengine.instruments import resolve_all
        from cryptoengine.feed import LiveFeed
        from cryptoengine.binance_feed import BinanceFeed
        from cryptoengine.recorder import Recorder
        from cryptoengine.paper_trade import PaperBook
        from cryptoengine.seed import seed_today, fetch_daily, seed_footprint
    except Exception as e:
        _E["error"] = f"engine import failed: {e}"[:200]
        return {"ok": False, "detail": _E["error"]}
    cfg = load_config()
    cfg.run_mode = "live"
    hl, bn = HLData(cfg.info_url), BinanceData()
    resolved = resolve_all(hl, cfg, bn_client=bn)
    client = {"hl": hl, "bn": bn}
    state = MarketState(resolved, cfg.candle_minutes)
    engine, alerter = Engine(cfg, state), Alerter(cfg)
    stop = threading.Event()

    def boot_seed():
        try:
            seed_today(client, resolved, state, cfg)
            seed_footprint(state, cfg)
            fetch_daily(client, resolved, state, cfg)
        except Exception as e:
            print(f"[crypto engine] seed skipped: {e}")
        while not stop.wait(3600):
            try:
                fetch_daily(client, resolved, state, cfg)
            except Exception as e:
                print(f"[crypto engine] daily refresh failed: {e}")

    threading.Thread(target=boot_seed, daemon=True, name="crypto-seed").start()
    hl_res = {k: ri for k, ri in resolved.items() if ri.source == "hl"}
    bn_res = {k: ri for k, ri in resolved.items() if ri.source == "bn"}
    feeds = ([LiveFeed(cfg, hl_res, state)] if hl_res else []) + ([BinanceFeed(cfg, bn_res, state, client=client["bn"])] if bn_res else [])
    recorder = Recorder(cfg)
    if recorder.enabled:
        for f in feeds:
            f.recorder = recorder
    else:
        recorder = None
    def link_stream():                     # 2026-11-01: the live price / flow panels (app/stream.py) read the same Binance trades.
        import sys as _sys                 # Wait until Market Pulse has loaded stream.py itself - importing it from here at start-up
        for _ in range(300):               # raced the updater thread and broke stream_hotfix (3 Nov).
            S = _sys.modules.get("stream")
            if S is not None and hasattr(S, "on_binance") and hasattr(S, "poll_yahoo_once") and hasattr(S, "HUB"):
                for f in feeds:
                    if isinstance(f, BinanceFeed):
                        f.forward = S.on_binance
                        S.ENGINE_FEED = True
                return
            if stop.wait(2):
                return
        print("[crypto engine] stream link skipped: stream.py not loaded")
    threading.Thread(target=link_stream, daemon=True, name="crypto-stream-link").start()
    for f in feeds:
        f.start()
        _ALL_FEEDS.append(f)
    paper = PaperBook(cfg)
    period = float(cfg.a("dashboard", "refresh_ms", default=1500)) / 1000.0
    _E.update(running=True, started=time.strftime("%Y-%m-%d %H:%M:%S"), error=None, stop=stop, feed=feeds, recorder=recorder,
              focus={"keys": {next(iter(resolved), None)}})
    threading.Thread(target=_compute_loop, args=(engine, alerter, period, stop, state, recorder, paper),
                     daemon=True, name="crypto-engine").start()
    return {"ok": True, "detail": f"crypto engine started ({len(resolved)} markets)"}


def stop() -> None:
    if _E.get("stop"):
        _E["stop"].set()
    for f in list(_ALL_FEEDS) + list(_E.get("feed") or []):
        try:
            f.forward = None
            f.stop()
        except Exception:
            pass
    _ALL_FEEDS.clear()
    if _E.get("recorder") is not None:
        try:
            _E["recorder"].close()
        except Exception:
            pass
    with _E["lock"]:
        _E["snap"] = None
    _E.update(running=False, stop=None, feed=None, recorder=None)


def restart() -> dict:
    with _START:
        stop()
        time.sleep(1)
    return start()
