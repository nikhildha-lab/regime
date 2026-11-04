#!/usr/bin/env python3
"""Entry point for the Nifty / Bank Nifty live 5-min tracker.

    python run.py            # uses RUN_MODE from .env (sim by default)
    RUN_MODE=live python run.py
    python run.py --mode sim --once   # compute one snapshot and print it (smoke test)

In LIVE mode you must have refreshed your Kite token today:
    python -m src.kite_auth
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


def build_broker_and_resolved(cfg, dataset=None):
    """Return (broker, resolved, mode)."""
    if cfg.run_mode == "replay":
        from src.replay import build_replay_resolved
        if not dataset:
            raise SystemExit("replay mode needs --dataset <dir>")
        resolved = build_replay_resolved(cfg, dataset)
        return None, resolved, "replay"
    if cfg.run_mode == "live" and cfg.broker == "dhan":
        from src.dhan_client import DhanClient
        from src.instruments import resolve_all
        if not cfg.dhan_access_token:
            raise SystemExit("No Dhan access token. Double-click Dhan-Login on the Desktop "
                             "(or run: python -m src.dhan_auth)")
        try:                                   # best-effort: keep the 24h token rolling
            from src.dhan_auth import renew, load
            if renew(quiet=True):
                cfg.dhan_client_id, cfg.dhan_access_token = load()
        except Exception as e:
            print(f"[boot] dhan token renew skipped: {e}")
        client = DhanClient(cfg.dhan_client_id, cfg.dhan_access_token)
        resolved = resolve_all(client, cfg)
        return client, resolved, "live"
    if cfg.run_mode == "live":
        from src.kite_auth import build_kite
        from src.instruments import resolve_all
        if not cfg.access_token:
            raise SystemExit(
                "No Kite access token. Run: python -m src.kite_auth")
        kite = build_kite(cfg)
        resolved = resolve_all(kite, cfg)
        return kite, resolved, "live"
    else:
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
    """Latest 5-min candle timestamp across instruments (advances on bar close)."""
    bar = None
    for v in snap.get("instruments", {}).values():
        cs = v.get("candles") or []
        if cs:
            bar = max(bar, cs[-1]["t"]) if bar else cs[-1]["t"]
    return bar


def compute_loop(engine: Engine, alerter: Alerter, latest: Latest,
                 period_s: float, stop: threading.Event, state, recorder=None,
                 paper=None):
    # The dashboard refreshes every cycle (live price) BUT signals, paper trades
    # and alerts are only evaluated on a completed 5-min bar — never intrabar.
    # This kills the per-second "repaint" flicker and matches the backtest exactly
    # (which decided on bar close). You act on closed candles, not the wiggle.
    last_bar = None
    last_paper = {"enabled": False}
    stable_top, stable_sig, stable_reg = [], {}, {}
    while not stop.is_set():
        try:
            snap = engine.compute()
            bar = _bar_marker(snap)
            first = last_bar is None
            bar_closed = (not first) and bar is not None and bar != last_bar
            if first or bar_closed:
                last_bar = bar
                stable_top = snap.get("top_signals", [])
                stable_sig = {k: v.get("signals", [])
                              for k, v in snap.get("instruments", {}).items()}
                # freeze the regime with the signals so banner + gate stay consistent
                stable_reg = {k: v.get("regime", {})
                              for k, v in snap.get("instruments", {}).items()}
                if bar_closed:                       # act once per closed bar
                    if paper is not None:
                        last_paper = paper.update(snap)
                    alerter.dispatch(snap)
                    if recorder is not None:
                        recorder.capture(state)
            # overlay the stable (last bar-close) signals + regime so the
            # Opportunities panel and stance banner stay steady while price ticks live
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
            print(f"[engine] compute error: {e}")
        stop.wait(period_s)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["sim", "live", "replay"], default=None)
    ap.add_argument("--dataset", default=None,
                    help="replay dataset directory (for --mode replay)")
    ap.add_argument("--once", action="store_true",
                    help="compute a single snapshot after warmup and exit")
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--exit-at", default=None,
                    help="auto-shutdown at local HH:MM (for scheduled market-hours runs)")
    args = ap.parse_args()

    if args.exit_at and not args.once:
        import os
        import datetime as _d
        hh, mm = args.exit_at.split(":")
        now = _d.datetime.now()
        target = now.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
        if target <= now:
            target += _d.timedelta(days=1)
        secs = (target - now).total_seconds()
        print(f"[boot] will auto-exit at {args.exit_at} (in {int(secs)}s)")
        _timer = threading.Timer(secs, lambda: os._exit(0))
        _timer.daemon = True
        _timer.start()

    cfg = load_config()
    if args.mode:
        cfg.run_mode = args.mode
    print(f"[boot] mode = {cfg.run_mode}")

    broker, resolved, mode = build_broker_and_resolved(cfg, dataset=args.dataset)
    is_live = mode == "live"
    for k, ri in resolved.items():
        print(f"[boot] {k}: spot={ri.spot_token} fut={ri.fut_tradingsymbol} "
              f"expiry={ri.expiry} strikes={len(ri.option_tokens)}")

    state = MarketState(resolved, cfg.candle_minutes)
    engine = Engine(cfg, state)
    alerter = Alerter(cfg)

    # start feed
    if mode == "live":
        # backfill today's candles (09:15 -> now) BEFORE the websocket, so IB,
        # VWAP, profiles and structure reflect the full day even on a late start
        try:
            from src.seed import seed_today, fetch_daily, seed_footprint
            print("[boot] seeding today's morning candles (09:15 -> now)…")
            seed_today(broker, resolved, state, cfg)
            print("[boot] rebuilding morning order-flow footprint…")
            seed_footprint(state, cfg)
            print("[boot] fetching daily history (for 1D/1W chart views)…")
            fetch_daily(broker, resolved, state, cfg)
        except Exception as e:
            print(f"[boot] seed skipped: {e}")
        if cfg.broker == "dhan":
            from src.dhan_feed import DhanFeed
            feed = DhanFeed(cfg, broker, resolved, state)
            print("[boot] data source: Dhan (DhanHQ v2 live feed)")
        else:
            from src.feed import LiveFeed
            feed = LiveFeed(cfg, broker, resolved, state)
    elif mode == "replay":
        from src.replay import ReplayFeed
        feed = ReplayFeed(cfg, args.dataset, resolved, state)
        print(f"[boot] replay dataset: {args.dataset} ({len(feed.events)} ticks)")
    else:
        from src.feed import SimFeed
        feed = SimFeed(cfg, broker, resolved, state)
    feed.start()

    if args.once:
        # let sim/live warm up a few minutes of candles, then dump one snapshot
        warm = 6.0 if not is_live else 12.0
        print(f"[once] warming up {warm}s…")
        time.sleep(warm)
        snap = engine.compute()
        print(json.dumps(snap, indent=2)[:6000])
        feed.stop()
        return

    # record the live stream to a replay dataset (live mode only)
    recorder = None
    if mode == "live":
        from src.recorder import Recorder
        recorder = Recorder(cfg)
        if recorder.enabled:
            print(f"[boot] recording live stream -> {recorder.dir}")
            if hasattr(feed, "recorder"):
                feed.recorder = recorder  # enables raw-tick capture if configured

    from src.paper_trade import PaperBook
    paper = PaperBook(cfg)
    if paper.enabled:
        print(f"[boot] paper trading ON (min_score={paper.min_score}) -> {paper.path}")

    latest = Latest()
    stop = threading.Event()
    period = float(cfg.a("dashboard", "refresh_ms", default=1500)) / 1000.0
    t = threading.Thread(target=compute_loop,
                         args=(engine, alerter, latest, period, stop, state, recorder, paper),
                         daemon=True, name="engine")
    t.start()

    from dashboard.server import build_app
    import uvicorn
    def _refresh_history():
        if mode != "live":
            return "only available in LIVE mode"
        from src.seed import reseed_charts
        return reseed_charts(broker, resolved, state, cfg)
    app = build_app(latest.get, refresh_ms=int(cfg.a("dashboard", "refresh_ms", default=1500)),
                    refresh_history=_refresh_history)
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
