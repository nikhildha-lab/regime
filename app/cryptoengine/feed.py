"""Feed drivers: push trades + context into MarketState.

LiveFeed   -> Hyperliquid public websocket (trades, l2Book, activeAssetCtx). 24/7.
SimFeed    -> SimBroker synthetic events (offline testing).

Both normalize into MarketState.apply_trade / apply_book / apply_ctx. Crypto gives
REAL trade prints with a known aggressor side, so the order-flow footprint is exact
rather than the quote/tick-rule proxy an index snapshot feed forces.
"""
from __future__ import annotations

import datetime as dt
import json
import threading
import time

from .config import Config
from .simbroker import SimBroker
from .state import MarketState


class SimFeed:
    def __init__(self, cfg: Config, broker: SimBroker, resolved: dict, state: MarketState):
        self.cfg = cfg
        self.broker = broker
        self.resolved = resolved
        self.state = state
        self._stop = threading.Event()
        self.thread: threading.Thread | None = None
        self.recorder = None

    def start(self):
        self.thread = threading.Thread(target=self._run, daemon=True, name="sim-feed")
        self.thread.start()

    def _run(self):
        tps = float(self.cfg.a("sim", "ticks_per_sec", default=8))
        delay = 1.0 / max(1.0, tps)
        gen = self.broker.generate(self.resolved, dt.datetime.utcnow())
        for batch in gen:
            if self._stop.is_set():
                return
            for ev in batch:
                self._dispatch(ev)
            time.sleep(delay)

    def _dispatch(self, ev):
        kind = ev[0]
        if kind == "trade":
            _, coin, ts, price, size, is_buy = ev
            self.state.apply_trade(coin, ts, price, size, is_buy)
            if self.recorder is not None:
                self.recorder.raw_trade(coin, ts, price, size, is_buy)
        elif kind == "ctx":
            _, coin, ts, mark, funding, oi, day_vol_usd, prev_day_px = ev
            self.state.apply_ctx(coin, mark=mark, funding=funding, oi=oi, ts=ts,
                                 day_vol_usd=day_vol_usd, prev_day_px=prev_day_px)

    def stop(self):
        self._stop.set()


class LiveFeed:
    """Hyperliquid public websocket. Subscribes trades + l2Book + activeAssetCtx
    for every tracked coin and streams them into MarketState."""

    def __init__(self, cfg: Config, resolved: dict, state: MarketState):
        self.cfg = cfg
        self.resolved = resolved
        self.state = state
        self.ws = None
        self.recorder = None
        self._stop = False
        self._coins = [ri.coin for ri in resolved.values()]
        # watchdog (2026-10-20): Hyperliquid sometimes keeps the socket open but stops
        # sending trades for some coins (prices still move, footprint freezes).
        self._watch = [c for c in ("BTC", "ETH", "SOL") if c in self._coins]
        self._last_trade = {}
        self._opened_at = time.time()

    def _watchdog(self):
        while not self._stop:
            time.sleep(20)
            now = time.time()
            if now - self._opened_at < 90:
                continue
            dead = [c for c in self._watch if now - self._last_trade.get(c, self._opened_at) > 90]
            if dead and self.ws is not None:
                print(f"[live-feed] no trades for {dead} in 90s - reconnecting", flush=True)
                self._opened_at = now
                try:
                    self.ws.close()
                except Exception:
                    pass

    def start(self):
        import websocket  # websocket-client

        def on_open(ws):
            for coin in self._coins:
                for typ in ("trades", "l2Book", "activeAssetCtx"):
                    ws.send(json.dumps({
                        "method": "subscribe",
                        "subscription": {"type": typ, "coin": coin},
                    }))
            self._opened_at = time.time()
            print(f"[live-feed] subscribed {self._coins} (trades/l2Book/ctx)", flush=True)

        def on_message(ws, message):
            try:
                self._handle(json.loads(message))
            except Exception as e:
                print(f"[live-feed] parse error: {e}")

        def on_error(ws, err):
            print(f"[live-feed] error: {err}")

        def on_close(ws, code, msg):
            print(f"[live-feed] closed ({code}) — reconnecting…")

        def run():
            while not self._stop:
                try:
                    self.ws = websocket.WebSocketApp(
                        self.cfg.ws_url, on_open=on_open, on_message=on_message,
                        on_error=on_error, on_close=on_close)
                    self.ws.run_forever(ping_interval=30, ping_timeout=10)
                except Exception as e:
                    print(f"[live-feed] run_forever crashed: {e}")
                if not self._stop:
                    time.sleep(3)

        threading.Thread(target=run, daemon=True, name="live-feed").start()
        threading.Thread(target=self._watchdog, daemon=True, name="live-feed-watchdog").start()

    def _handle(self, msg: dict):
        ch = msg.get("channel")
        data = msg.get("data")
        if ch == "trades":
            for t in data or []:
                coin = t.get("coin")
                px = _f(t.get("px"))
                sz = _f(t.get("sz"))
                is_buy = t.get("side") == "B"      # B = taker bought (lifted ask)
                ts = _ms_to_dt(t.get("time"))
                if coin and px is not None and sz is not None:
                    self._last_trade[coin] = time.time()
                    self.state.apply_trade(coin, ts, px, sz, is_buy)
                    if self.recorder is not None:
                        self.recorder.raw_trade(coin, ts, px, sz, is_buy)
        elif ch == "l2Book":
            coin = (data or {}).get("coin")
            levels = (data or {}).get("levels") or []
            bid = ask = None
            if len(levels) >= 2:
                if levels[0]:
                    bid = _f(levels[0][0].get("px"))
                if levels[1]:
                    ask = _f(levels[1][0].get("px"))
            if coin:
                self.state.apply_book(coin, bid, ask)
        elif ch == "activeAssetCtx":
            coin = (data or {}).get("coin")
            ctx = (data or {}).get("ctx") or {}
            if coin:
                self.state.apply_ctx(
                    coin,
                    mark=_f(ctx.get("markPx")),
                    funding=_f(ctx.get("funding")),
                    oi=_f(ctx.get("openInterest")),
                    day_vol_usd=_f(ctx.get("dayNtlVlm")),
                    prev_day_px=_f(ctx.get("prevDayPx")),
                )
                if self.recorder is not None:
                    self.recorder.ctx(coin, ctx)

    def stop(self):
        self._stop = True
        if self.ws:
            try:
                self.ws.close()
            except Exception:
                pass


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _ms_to_dt(ms):
    try:
        return dt.datetime.utcfromtimestamp(int(ms) / 1000.0)
    except Exception:
        return dt.datetime.utcnow()
