"""Price / volume imbalance ("order-flow-lite").

A tick feed without full market depth can't give true bid/ask delta, so we
approximate buying vs selling pressure from each futures candle using the
close's position within its range weighted by volume (a standard proxy):

    pressure = volume * (2*(close-low)/(high-low) - 1)   in [-vol, +vol]

Cumulative pressure ~ a synthetic cumulative delta. We also flag:
  * volume spikes           (5-min vol >> rolling median)
  * delta divergence        (price up but pressure down, or vice-versa)
  * effort-vs-result        (big volume, tiny range = absorption)
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass, field

from ..candles import Candle


@dataclass
class ImbalanceRead:
    cum_delta: float = 0.0
    last_delta: float = 0.0
    delta_state: str = "flat"          # buyers / sellers / flat
    volume_spike: bool = False
    last_volume: float = 0.0
    median_volume: float = 0.0
    divergence: str = "none"           # bearish_div / bullish_div / none
    absorption: bool = False

    def to_dict(self):
        return {
            "cum_delta": int(self.cum_delta), "last_delta": int(self.last_delta),
            "delta_state": self.delta_state, "volume_spike": self.volume_spike,
            "last_volume": int(self.last_volume), "median_volume": int(self.median_volume),
            "divergence": self.divergence, "absorption": self.absorption,
        }


def _pressure(c: Candle) -> float:
    rng = c.high - c.low
    if rng <= 0 or c.volume <= 0:
        return 0.0
    return c.volume * (2 * (c.close - c.low) / rng - 1)


def analyze(candles: list[Candle], spike_mult: float = 2.0) -> ImbalanceRead:
    out = ImbalanceRead()
    cands = [c for c in candles if c.volume > 0]
    if not cands:
        return out

    deltas = [_pressure(c) for c in cands]
    out.cum_delta = sum(deltas)
    out.last_delta = deltas[-1]
    if out.last_delta > 0:
        out.delta_state = "buyers"
    elif out.last_delta < 0:
        out.delta_state = "sellers"

    vols = [c.volume for c in cands]
    out.last_volume = vols[-1]
    if len(vols) >= 3:
        med = statistics.median(vols[:-1]) or 0.0
        out.median_volume = med
        if med > 0 and vols[-1] >= spike_mult * med:
            out.volume_spike = True

    # divergence over last 3 candles: price direction vs cumulative pressure
    if len(cands) >= 4:
        price_up = cands[-1].close > cands[-4].close
        press_recent = sum(deltas[-3:])
        if price_up and press_recent < 0:
            out.divergence = "bearish_div"
        elif not price_up and press_recent > 0:
            out.divergence = "bullish_div"

    # absorption: last candle high volume but small body relative to range
    last = cands[-1]
    rng = last.high - last.low
    body = abs(last.close - last.open)
    if out.volume_spike and rng > 0 and body / rng < 0.35:
        out.absorption = True
    return out
