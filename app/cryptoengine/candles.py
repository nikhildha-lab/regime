"""Tick -> 5-minute OHLCV candle aggregation.

Handles indices (no volume) and F&O tokens (volume + OI) uniformly. Volume is
stored as the per-candle traded quantity (delta of cumulative day volume so we
don't double count), which is what volume/market profile needs.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field


def floor_to_bucket(ts: dt.datetime, minutes: int) -> dt.datetime:
    if minutes < 60:
        m = (ts.minute // minutes) * minutes
        return ts.replace(minute=m, second=0, microsecond=0)
    total = (ts.hour * 60 + ts.minute) // minutes * minutes
    return ts.replace(hour=total // 60, minute=total % 60, second=0, microsecond=0)


def aggregate_candles(candles, bucket_minutes):
    """Resample a candle list up to a higher timeframe (e.g. 5-min -> 15-min)."""
    if not candles:
        return []
    out = []
    cur = None
    for c in candles:
        key = floor_to_bucket(c.start, bucket_minutes)
        if cur is None or key > cur.start:
            cur = Candle(key, c.open, c.high, c.low, c.close, c.volume, c.oi)
            out.append(cur)
        else:
            cur.high = max(cur.high, c.high)
            cur.low = min(cur.low, c.low)
            cur.close = c.close
            cur.volume += c.volume
            cur.oi = c.oi
    return out


@dataclass
class Candle:
    start: dt.datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0        # traded qty during this candle
    oi: float = 0.0            # OI snapshot at candle close (F&O only)

    def update(self, price: float, vol_delta: float, oi: float | None):
        self.high = max(self.high, price)
        self.low = min(self.low, price)
        self.close = price
        self.volume += max(0.0, vol_delta)
        if oi is not None:
            self.oi = oi

    def as_dict(self):
        return {
            "t": self.start.strftime("%H:%M"),
            "o": round(self.open, 2), "h": round(self.high, 2),
            "l": round(self.low, 2), "c": round(self.close, 2),
            # crypto volumes are fractional (coin units) — keep precision, don't int()
            "v": round(self.volume, 4), "oi": round(self.oi, 2),
        }


class CandleSeries:
    """Rolling list of completed candles + the in-progress one for a single token."""

    def __init__(self, minutes: int = 5):
        self.minutes = minutes
        self.completed: list[Candle] = []
        self.current: Candle | None = None
        self._last_cum_vol: float | None = None  # cumulative day volume seen

    def on_tick(self, ts: dt.datetime, price: float,
                cum_volume: float | None = None, oi: float | None = None):
        # convert cumulative day-volume into a per-tick delta
        vol_delta = 0.0
        if cum_volume is not None:
            if self._last_cum_vol is None:
                self._last_cum_vol = cum_volume
            vol_delta = max(0.0, cum_volume - self._last_cum_vol)
            self._last_cum_vol = cum_volume

        bucket = floor_to_bucket(ts, self.minutes)
        if self.current is None:
            self.current = Candle(bucket, price, price, price, price, vol_delta, oi or 0.0)
            return
        if bucket > self.current.start:
            self.completed.append(self.current)
            self.current = Candle(bucket, price, price, price, price, vol_delta, oi or 0.0)
        else:
            self.current.update(price, vol_delta, oi)

    def on_trade(self, ts: dt.datetime, price: float, size: float,
                 oi: float | None = None):
        """Aggregate a single trade print (size = traded qty for THIS trade, not
        cumulative). Used by the crypto feed where every trade arrives discretely."""
        size = max(0.0, float(size or 0.0))
        bucket = floor_to_bucket(ts, self.minutes)
        if self.current is None:
            self.current = Candle(bucket, price, price, price, price, size, oi or 0.0)
            return
        if bucket > self.current.start:
            self.completed.append(self.current)
            self.current = Candle(bucket, price, price, price, price, size, oi or 0.0)
        else:
            self.current.update(price, size, oi)

    def all_candles(self, include_current: bool = True) -> list[Candle]:
        out = list(self.completed)
        if include_current and self.current is not None:
            out.append(self.current)
        return out

    def last_price(self) -> float | None:
        if self.current is not None:
            return self.current.close
        if self.completed:
            return self.completed[-1].close
        return None
