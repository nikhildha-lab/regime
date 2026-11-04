"""In-memory market state shared between the feed and the analytics/dashboard.

One MarketState holds everything for all tracked coins. The feed writes trades +
context in; the analytics engine and dashboard read snapshots out. A single lock
guards mutation so the websocket thread and the analytics thread don't race.

Crypto model: each coin has ONE perpetual series (`perp`) carrying price + real
traded volume + open interest, a live order-flow `footprint` (real aggressor
side), and a live context block (mark, funding + history, OI + day-open OI, and
estimated liquidation clusters). No spot/futures/option split — the perp is
everything.
"""
from __future__ import annotations

import datetime as dt
import threading
from collections import deque
from dataclasses import dataclass, field

from .candles import CandleSeries
from .instruments import ResolvedInstrument
from .analytics.footprint import FootprintBook


@dataclass
class InstrumentState:
    ri: ResolvedInstrument
    perp: CandleSeries
    chart5: CandleSeries | None = None          # display-only 5-min chart series
    footprint: FootprintBook | None = None
    last_price: float = 0.0
    mark: float = 0.0
    oi: float = 0.0
    oi_open: float | None = None            # first OI seen this UTC day (buildup base)
    funding: float | None = None            # current hourly funding rate (decimal)
    funding_hist: deque = field(default_factory=lambda: deque(maxlen=72))
    best_bid: float | None = None
    best_ask: float | None = None
    day_vol_usd: float | None = None
    prev_day_px: float | None = None
    daily: list = field(default_factory=list)   # daily candles (chart + prior levels)
    # large recent trades (proxy for liquidation/aggression bursts), (ts, px, sz, buy)
    big_trades: deque = field(default_factory=lambda: deque(maxlen=40))


class MarketState:
    def __init__(self, resolved: dict[str, ResolvedInstrument], candle_minutes: int):
        self.lock = threading.RLock()
        self.candle_minutes = candle_minutes
        self.instruments: dict[str, InstrumentState] = {}
        self.by_coin: dict[str, str] = {}       # coin symbol -> instrument key
        self.session_date = dt.datetime.utcnow().date()

        for key, ri in resolved.items():
            st = InstrumentState(
                ri=ri,
                perp=CandleSeries(candle_minutes),
                chart5=CandleSeries(5),
                footprint=FootprintBook(ri.cfg.profile_bin, candle_minutes),
            )
            self.instruments[key] = st
            self.by_coin[ri.coin] = key

    # ---- feed writes -----------------------------------------------------
    def apply_trade(self, coin: str, ts: dt.datetime, price: float, size: float,
                    is_buy: bool):
        key = self.by_coin.get(coin)
        if not key:
            return
        with self.lock:
            st = self.instruments[key]
            st.perp.on_trade(ts, price, size, oi=st.oi or None)
            if st.chart5 is not None:
                st.chart5.on_trade(ts, price, size)
            st.last_price = price
            if st.footprint is not None:
                st.footprint.on_trade(ts, price, size, is_buy)
            # track notable prints (size relative to a rolling sense of "big")
            if size and price:
                st.big_trades.append((ts, price, float(size), bool(is_buy)))

    def apply_book(self, coin: str, bid: float | None, ask: float | None):
        key = self.by_coin.get(coin)
        if not key:
            return
        with self.lock:
            st = self.instruments[key]
            if bid is not None:
                st.best_bid = bid
            if ask is not None:
                st.best_ask = ask

    def apply_ctx(self, coin: str, mark: float | None = None,
                  funding: float | None = None, oi: float | None = None,
                  ts: dt.datetime | None = None, day_vol_usd: float | None = None,
                  prev_day_px: float | None = None):
        key = self.by_coin.get(coin)
        if not key:
            return
        with self.lock:
            st = self.instruments[key]
            if mark is not None:
                st.mark = mark
                if not st.last_price:
                    st.last_price = mark
            if oi is not None:
                st.oi = oi
                if st.oi_open is None:
                    st.oi_open = oi
            if funding is not None:
                st.funding = funding
                stamp = (ts or dt.datetime.utcnow())
                if not st.funding_hist or st.funding_hist[-1][1] != funding:
                    st.funding_hist.append((stamp, funding))
            if day_vol_usd is not None:
                st.day_vol_usd = day_vol_usd
            if prev_day_px is not None:
                st.prev_day_px = prev_day_px
