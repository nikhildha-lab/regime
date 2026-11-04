"""Feed drivers: push ticks into MarketState.

LiveFeed   -> KiteTicker websocket (real market hours).
SimFeed    -> SimBroker synthetic ticks (closed-market testing).

Both normalize a tick into (token, ts, ltp, cum_volume, oi) and call
MarketState.apply_tick. A tick's timestamp is used verbatim so 5-min buckets
line up with the exchange clock (and with compressed sim time).
"""
from __future__ import annotations

import datetime as dt
import threading
import time
from typing import Callable

from .config import Config
from .simbroker import SimBroker
from .state import MarketState


def _best_bid_ask(tick: dict):
    """Best bid / ask from a MODE_FULL tick's 5-level depth (None if absent)."""
    d = tick.get("depth")
    if not d:
        return None, None
    try:
        buy = d.get("buy") or []
        sell = d.get("sell") or []
        bid = buy[0].get("price") if buy else None
        ask = sell[0].get("price") if sell else None
        return (bid or None), (ask or None)
    except Exception:
        return None, None


def _extract(tick: dict):
    token = tick.get("instrument_token")
    ltp = tick.get("last_price")
    ts = tick.get("exchange_timestamp") or tick.get("timestamp") or dt.datetime.now()
    if isinstance(ts, (int, float)):
        ts = dt.datetime.fromtimestamp(ts)
    cum_vol = tick.get("volume_traded", tick.get("volume"))
    oi = tick.get("oi")
    bid, ask = _best_bid_ask(tick)
    return token, ts, ltp, cum_vol, oi, bid, ask


class SimFeed:
    def __init__(self, cfg: Config, broker: SimBroker, resolved: dict, state: MarketState):
        self.cfg = cfg
        self.broker = broker
        self.resolved = resolved
        self.state = state
        self._stop = threading.Event()
        self.thread: threading.Thread | None = None

    def start(self):
        self.thread = threading.Thread(target=self._run, daemon=True, name="sim-feed")
        self.thread.start()

    def _run(self):
        tps = float(self.cfg.a("sim", "ticks_per_sec", default=8))
        delay = 1.0 / max(1.0, tps)
        gen = self.broker.generate_ticks(self.resolved,
                                         dt.datetime.combine(dt.date.today(),
                                                             self.cfg.market_open))
        for batch in gen:
            if self._stop.is_set():
                return
            for tk in batch:
                token, ts, ltp, cum_vol, oi, bid, ask = _extract(tk)
                if token and ltp is not None:
                    self.state.apply_tick(token, ts, ltp, cum_vol, oi, bid=bid, ask=ask)
            time.sleep(delay)

    def stop(self):
        self._stop.set()


class LiveFeed:
    def __init__(self, cfg: Config, kite, resolved: dict, state: MarketState):
        self.cfg = cfg
        self.kite = kite
        self.resolved = resolved
        self.state = state
        self.ticker = None
        self.recorder = None   # set by run.py to capture raw ticks

    def _all_tokens(self) -> list[int]:
        toks: list[int] = []
        for ri in self.resolved.values():
            toks.extend(ri.all_tokens())
        return sorted(set(toks))

    def start(self):
        from kiteconnect import KiteTicker

        tokens = self._all_tokens()
        kt = KiteTicker(self.cfg.api_key, self.cfg.access_token)

        def on_connect(ws, response):
            ws.subscribe(tokens)
            ws.set_mode(ws.MODE_FULL, tokens)  # FULL = OHLC + volume + OI

        def on_ticks(ws, ticks):
            for tk in ticks:
                token, ts, ltp, cum_vol, oi, bid, ask = _extract(tk)
                if token and ltp is not None:
                    self.state.apply_tick(token, ts, ltp, cum_vol, oi, bid=bid, ask=ask)
                    if self.recorder is not None:
                        self.recorder.raw_tick(token, ts, ltp, cum_vol, oi, bid=bid, ask=ask)

        def on_error(ws, code, reason):
            print(f"[live-feed] error {code}: {reason}")

        kt.on_connect = on_connect
        kt.on_ticks = on_ticks
        kt.on_error = on_error
        self.ticker = kt
        # threaded=True so KiteTicker runs its own reactor loop in the background
        kt.connect(threaded=True)

    def stop(self):
        if self.ticker:
            try:
                self.ticker.close()
            except Exception:
                pass
