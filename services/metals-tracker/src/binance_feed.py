"""Binance USD-M futures public websocket feed.

Streams aggTrade (real buyer/seller aggressor flag -> true CVD/footprint) and
markPrice (mark + funding) for every Binance-sourced coin into MarketState,
mirroring feed.LiveFeed. Open interest + 24h volume are polled over REST.
"""
from __future__ import annotations

import datetime as dt
import json
import threading
import time

from .config import Config
from .state import MarketState

_WS = "wss://fstream.binance.com/stream?streams="


class BinanceFeed:
    def __init__(self, cfg: Config, resolved: dict, state: MarketState, client=None):
        self.cfg = cfg
        self.resolved = resolved          # only the Binance-sourced instruments
        self.state = state
        self.client = client              # BinanceData (for OI/volume polling)
        self.ws = None
        self.recorder = None
        self._stop = False
        self._coins = [ri.coin for ri in resolved.values()]

    def start(self):
        import websocket

        streams = []
        for c in self._coins:
            sym = f"{c.lower()}usdt"
            streams.append(f"{sym}@aggTrade")
            streams.append(f"{sym}@markPrice@1s")
        url = _WS + "/".join(streams)

        def on_open(ws):
            print(f"[bn-feed] subscribed {self._coins} (aggTrade/markPrice)")

        def on_message(ws, message):
            try:
                self._handle(json.loads(message))
            except Exception as e:
                print(f"[bn-feed] parse error: {e}")

        def on_error(ws, err):
            print(f"[bn-feed] error: {err}")

        def on_close(ws, code, msg):
            print(f"[bn-feed] closed ({code}) — reconnecting…")

        def run():
            while not self._stop:
                try:
                    self.ws = websocket.WebSocketApp(
                        url, on_open=on_open, on_message=on_message,
                        on_error=on_error, on_close=on_close)
                    self.ws.run_forever(ping_interval=60, ping_timeout=15)
                except Exception as e:
                    print(f"[bn-feed] run_forever crashed: {e}")
                if not self._stop:
                    time.sleep(3)

        threading.Thread(target=run, daemon=True, name="bn-feed").start()
        if self.client is not None:
            threading.Thread(target=self._poll_ctx, daemon=True,
                             name="bn-ctx-poll").start()

    def _handle(self, msg: dict):
        data = msg.get("data") or {}
        ev = data.get("e")
        if ev == "aggTrade":
            sym = data.get("s", "")
            coin = sym[:-4] if sym.endswith("USDT") else sym
            ts = dt.datetime.utcfromtimestamp(int(data.get("T", 0)) / 1000.0)
            price = float(data.get("p", 0) or 0)
            size = float(data.get("q", 0) or 0)
            is_buy = not bool(data.get("m"))      # m=True -> buyer is maker -> SELL aggressor
            if price and size:
                self.state.apply_trade(coin, ts, price, size, is_buy)
                if self.recorder is not None:
                    try:
                        self.recorder.raw_trade(coin, ts, price, size, is_buy)
                    except Exception:
                        pass
        elif ev == "markPriceUpdate":
            sym = data.get("s", "")
            coin = sym[:-4] if sym.endswith("USDT") else sym
            fr = data.get("r")
            self.state.apply_ctx(
                coin,
                mark=float(data.get("p", 0) or 0) or None,
                funding=(float(fr) / 8.0) if fr not in (None, "") else None)

    def _poll_ctx(self):
        """Open interest + 24h quote volume via REST (not in the ws streams)."""
        while not self._stop:
            for key, ri in list(self.resolved.items()):
                try:
                    oi = self.client.open_interest(ri.coin)
                    if oi is not None:
                        self.state.apply_ctx(ri.coin, oi=oi)
                except Exception:
                    pass
                time.sleep(1.5)                   # spread calls across the minute
            time.sleep(30)

    def stop(self):
        self._stop = True
        try:
            if self.ws:
                self.ws.close()
        except Exception:
            pass
