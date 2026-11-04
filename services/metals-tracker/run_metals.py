#!/usr/bin/env python3
"""Entry point for the crypto (BTC/ETH/SOL) live 5-min tracker.

    python run.py                     # RUN_MODE from .env (sim by default)
    RUN_MODE=live python run.py       # stream Hyperliquid perps (24/7, no keys)
    python run.py --mode sim --once   # compute one snapshot and print it (smoke test)
    python run.py --mode replay --dataset datasets/<name>
"""
from __future__ import annotations

import argparse
import json
import threading
import time
import webbrowser

from src.config import load_config
from src.state import MarketState
from src.engine import Engine
from src.alerts import Alerter


def build_client_and_resolved(cfg, dataset=None):
    """Return (client, resolved, mode). client is HLData (live), SimBroker (sim),
    or None (replay)."""
    if cfg.run_mode == "replay":
        from src.replay import build_replay_resolved
        if not dataset:
            raise SystemExit("replay mode needs --dataset <dir>")
        resolved = build_replay_resolved(cfg, dataset)
        return None, resolved, "replay"
    if cfg.run_mode == "live":
        from src.hl_data import HLData
        from src.binance_data import BinanceData
        from src.instruments import resolve_all
        hl = HLData(cfg.info_url)
        bn = BinanceData()
        resolved = resolve_all(hl, cfg, bn_client=bn)
        return {"hl": hl, "bn": bn}, resolved, "live"
    from src.simbroker import SimBroker
    from src.instruments import resolve_all
    broker = SimBroker(cfg)
    resolved = resolve_all(broker, cfg)
    return broker, resolved, "sim"


class Latest:
    def __init__(self):
        self._snap = {"generated_at": None, "instruments": {}, "top_signals": [],
                      "mode": "starting"}
        self._lock = threading.Lock()

    def set(self, s):
        with self._lock:
            self._snap = s

    def get(self):
        with self._lock:
            return self._snap


def _bar_marker(snap):
    """Per-instrument last-candle map. A bar close = ANY instrument rolled, so
    one stalled feed (or the midnight HH:MM wrap) can never freeze the paper
    engine / alerts / recorder for the whole book."""
    return {k: (v.get("candles") or [{}])[-1].get("t")
            for k, v in snap.get("instruments", {}).items()}


# BTC / ETH / SOL are always fully recomputed (footprint, CVD, levels) every cycle;
# other coins refresh fully only when their bar rolls (2026-10-20).
ALWAYS_LIVE = {"BTC", "ETH", "SOL"}


def compute_loop(engine, alerter, latest, period_s, stop, state, recorder=None,
                 paper=None, focus_ref=None):
    last_bar = None
    last_paper = {"enabled": False}
    stable_top, stable_sig, stable_reg = [], {}, {}
    while not stop.is_set():
        try:
            snap = engine.compute(focus=set((focus_ref or {}).get("keys") or ()) | ALWAYS_LIVE)
            bar = _bar_marker(snap)
            first = last_bar is None
            bar_closed = (not first) and bool(bar) and any(
                bar.get(k) != (last_bar or {}).get(k) for k in bar)
            if first or bar_closed:
                last_bar = bar
                stable_top = snap.get("top_signals", [])
                stable_sig = {k: v.get("signals", [])
                              for k, v in snap.get("instruments", {}).items()}
                stable_reg = {k: v.get("regime", {})
                              for k, v in snap.get("instruments", {}).items()}
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
                    print(f"[paper] tick error: {e}")
            snap["top_signals"] = stable_top
            for k, v in snap.get("instruments", {}).items():
                if k in stable_sig:
                    v["signals"] = stable_sig[k]
                if k in stable_reg:
                    v["regime"] = stable_reg[k]
            if paper is not None:
                snap["paper"] = last_paper
            latest.set(snap)
        except Exception as e:
            import traceback
            print(f"[engine] compute error: {e}")
            traceback.print_exc()
        stop.wait(period_s)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["sim", "live", "replay"], default=None)
    ap.add_argument("--dataset", default=None)
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()

    cfg = load_config()
    if args.mode:
        cfg.run_mode = args.mode
    print(f"[boot] mode = {cfg.run_mode}")

    client, resolved, mode = build_client_and_resolved(cfg, dataset=args.dataset)
    for k, ri in resolved.items():
        print(f"[boot] {k}: coin={ri.coin} szDec={ri.sz_decimals} maxLev={ri.max_leverage}")

    state = MarketState(resolved, cfg.candle_minutes)
    engine = Engine(cfg, state)
    alerter = Alerter(cfg)

    if mode == "live":
        # Seed in the BACKGROUND so the dashboard is reachable within seconds —
        # 50+ coins take a couple of minutes to backfill; charts fill as they land.
        import threading as _th

        def _boot_seed():
            try:
                from src.seed import seed_today, fetch_daily, seed_footprint
                print("[boot] seeding history in background (50+ coins, ~2 min)…")
                seed_today(client, resolved, state, cfg)
                print("[boot] rebuilding footprint…")
                seed_footprint(state, cfg)
                print("[boot] fetching daily history…")
                fetch_daily(client, resolved, state, cfg)
                print("[boot] background seeding DONE")
            except Exception as e:
                print(f"[boot] seed skipped: {e}")
            while True:
                time.sleep(3600)
                try:
                    fetch_daily(client, resolved, state, cfg)
                    print("[seed] daily history refreshed")
                except Exception as e:
                    print(f"[seed] daily refresh failed: {e}")

        _th.Thread(target=_boot_seed, daemon=True, name="boot-seed").start()
        from src.feed import LiveFeed
        from src.binance_feed import BinanceFeed
        hl_res = {k: ri for k, ri in resolved.items() if ri.source == "hl"}
        bn_res = {k: ri for k, ri in resolved.items() if ri.source == "bn"}
        feeds = []
        if hl_res:
            feeds.append(LiveFeed(cfg, hl_res, state))
        if bn_res:
            feeds.append(BinanceFeed(cfg, bn_res, state,
                                     client=(client.get("bn") if isinstance(client, dict) else None)))

        class _MultiFeed:
            def __init__(self, fs): self.fs = fs
            def start(self): [f.start() for f in self.fs]
            def stop(self): [f.stop() for f in self.fs]
            @property
            def recorder(self): return getattr(self.fs[0], "recorder", None)
            @recorder.setter
            def recorder(self, r):
                for f in self.fs:
                    f.recorder = r
        feed = _MultiFeed(feeds)
    elif mode == "replay":
        from src.replay import ReplayFeed
        feed = ReplayFeed(cfg, args.dataset, resolved, state)
        print(f"[boot] replay dataset: {args.dataset} ({len(feed.events)} events)")
    else:
        from src.feed import SimFeed
        feed = SimFeed(cfg, client, resolved, state)
    feed.start()

    if args.once:
        warm = 6.0 if mode != "live" else 12.0
        print(f"[once] warming up {warm}s…")
        time.sleep(warm)
        snap = engine.compute()
        print(json.dumps(snap, indent=2)[:6000])
        feed.stop()
        return

    recorder = None
    if mode == "live":
        from src.recorder import Recorder
        recorder = Recorder(cfg)
        if recorder.enabled:
            print(f"[boot] recording live stream -> {recorder.dir}")
            if hasattr(feed, "recorder"):
                feed.recorder = recorder

    from src.paper_trade import PaperBook
    paper = PaperBook(cfg)
    if paper.enabled:
        print(f"[boot] paper trading ON (min_score={paper.min_score}) -> {paper.path}")

    latest = Latest()
    stop = threading.Event()
    period = float(cfg.a("dashboard", "refresh_ms", default=1500)) / 1000.0
    focus_ref = {"keys": {next(iter(resolved), None)}}
    t = threading.Thread(target=compute_loop,
                         args=(engine, alerter, latest, period, stop, state, recorder, paper,
                               focus_ref),
                         daemon=True, name="engine")
    t.start()

    from dashboard.server import build_app
    import uvicorn
    def _refresh_history():
        if mode != "live":
            return "only available in LIVE mode"
        from src.seed import reseed_charts
        return reseed_charts(client, resolved, state, cfg)
    def _set_focus(k):
        if k:
            focus_ref["keys"] = {k}
    app = build_app(latest.get, refresh_ms=int(cfg.a("dashboard", "refresh_ms", default=1500)),
                    refresh_history=_refresh_history, set_focus=_set_focus)
    host = cfg.a("dashboard", "host", default="127.0.0.1")
    port = int(cfg.a("dashboard", "port", default=8899))
    url = f"http://{host}:{port}"
    print(f"[boot] dashboard -> {url}")
    if not args.no_browser:
        try:
            threading.Timer(1.2, lambda: webbrowser.open(url)).start()
        except Exception:
            pass
    try:
        uvicorn.run(app, host=host, port=port, log_level="warning")
    finally:
        stop.set()
        feed.stop()
        if recorder is not None:
            recorder.close()


if __name__ == "__main__":
    main()
