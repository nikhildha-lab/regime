"""Price-action structure from 5-min candles.

Produces: swing pivots, market structure state (BOS / CHoCH), trend label,
opening range (ORB) with breakout state, intraday support/resistance from swing
clusters, and VWAP. Works on the index candles (structure doesn't need volume).
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from ..candles import Candle


@dataclass
class Swing:
    idx: int
    time: str
    price: float
    kind: str  # "H" or "L"


@dataclass
class PriceActionRead:
    trend: str = "n/a"
    structure_event: str = "none"     # BOS_UP / BOS_DOWN / CHoCH_UP / CHoCH_DOWN / none
    last_swing_high: float | None = None
    last_swing_low: float | None = None
    swings: list[Swing] = field(default_factory=list)
    orb_high: float | None = None
    orb_low: float | None = None
    orb_state: str = "forming"        # forming / inside / broke_up / broke_down
    vwap: float | None = None
    support: list[float] = field(default_factory=list)
    resistance: list[float] = field(default_factory=list)
    last_price: float | None = None

    def to_dict(self):
        return {
            "trend": self.trend,
            "structure_event": self.structure_event,
            "last_swing_high": _r(self.last_swing_high),
            "last_swing_low": _r(self.last_swing_low),
            "orb_high": _r(self.orb_high),
            "orb_low": _r(self.orb_low),
            "orb_state": self.orb_state,
            "vwap": _r(self.vwap),
            "support": [_r(x) for x in self.support],
            "resistance": [_r(x) for x in self.resistance],
            "swings": [{"t": s.time, "p": _r(s.price), "k": s.kind} for s in self.swings[-8:]],
            "last_price": _r(self.last_price),
        }


def _r(x, n=2):
    return None if x is None else round(x, n)


def _fractal_swings(candles: list[Candle], k: int) -> list[Swing]:
    swings: list[Swing] = []
    n = len(candles)
    for i in range(k, n - k):
        window = candles[i - k: i + k + 1]
        c = candles[i]
        if c.high == max(w.high for w in window) and \
                all(candles[i].high >= candles[j].high for j in range(i - k, i + k + 1)):
            swings.append(Swing(i, c.start.strftime("%H:%M"), c.high, "H"))
        if c.low == min(w.low for w in window) and \
                all(candles[i].low <= candles[j].low for j in range(i - k, i + k + 1)):
            swings.append(Swing(i, c.start.strftime("%H:%M"), c.low, "L"))
    return swings


def _cluster(levels: list[float], tol: float) -> list[float]:
    if not levels:
        return []
    levels = sorted(levels)
    clusters = [[levels[0]]]
    for x in levels[1:]:
        if abs(x - clusters[-1][-1]) <= tol:
            clusters[-1].append(x)
        else:
            clusters.append([x])
    # weight by cluster size, return cluster means sorted by prominence
    ranked = sorted(clusters, key=len, reverse=True)
    return [round(sum(c) / len(c), 2) for c in ranked]


def analyze(candles: list[Candle], k: int = 2, orb_minutes: int = 15,
            candle_minutes: int = 5, tick_size: float = 0.05) -> PriceActionRead:
    out = PriceActionRead()
    if not candles:
        return out
    out.last_price = candles[-1].close

    # VWAP (typical price weighted by volume; falls back to simple mean w/o vol)
    num = den = 0.0
    for c in candles:
        tp = (c.high + c.low + c.close) / 3
        w = c.volume if c.volume > 0 else 1.0
        num += tp * w
        den += w
    out.vwap = num / den if den else None

    swings = _fractal_swings(candles, k)
    out.swings = swings
    highs = [s for s in swings if s.kind == "H"]
    lows = [s for s in swings if s.kind == "L"]
    if highs:
        out.last_swing_high = highs[-1].price
    if lows:
        out.last_swing_low = lows[-1].price

    # trend from last two highs and lows
    trend = "range"
    if len(highs) >= 2 and len(lows) >= 2:
        hh = highs[-1].price > highs[-2].price
        hl = lows[-1].price > lows[-2].price
        lh = highs[-1].price < highs[-2].price
        ll = lows[-1].price < lows[-2].price
        if hh and hl:
            trend = "up"
        elif lh and ll:
            trend = "down"
    out.trend = trend

    # structure event: did the latest close break the prior swing?
    price = candles[-1].close
    event = "none"
    if len(highs) >= 1 and len(lows) >= 1:
        prev_high = highs[-1].price
        prev_low = lows[-1].price
        # was previous leg up or down?
        prior_dir = "up" if (highs[-1].idx > lows[-1].idx) else "down"
        if price > prev_high:
            event = "BOS_UP" if prior_dir == "up" else "CHoCH_UP"
        elif price < prev_low:
            event = "BOS_DOWN" if prior_dir == "down" else "CHoCH_DOWN"
    out.structure_event = event

    # opening range
    n_orb = max(1, orb_minutes // candle_minutes)
    orb = candles[:n_orb]
    if orb:
        out.orb_high = max(c.high for c in orb)
        out.orb_low = min(c.low for c in orb)
        if len(candles) <= n_orb:
            out.orb_state = "forming"
        elif price > out.orb_high:
            out.orb_state = "broke_up"
        elif price < out.orb_low:
            out.orb_state = "broke_down"
        else:
            out.orb_state = "inside"

    # support/resistance from swing clusters relative to current price
    tol = max(tick_size * 20, (max(c.high for c in candles) -
                               min(c.low for c in candles)) * 0.0015)
    # 2026-11-09 (Nikhil: "Resistance keeps changing within a few seconds"): the lists used to be swing HIGHS above price and swing LOWS
    # below price, so the moment price ticked above a swing high that level vanished (it was neither) and the label jumped to the next one;
    # a tick back and it reappeared. Now: a level keeps its side until price is clearly through it (more than `tol` beyond), and a level
    # price has gone through flips to the other side (broken resistance = support), so the nearest levels change only on a real break.
    band = max(tol, price * 0.0015)
    res = _cluster([s.price for s in highs if s.price > price - band] + [s.price for s in lows if s.price > price + band], tol)
    sup = _cluster([s.price for s in lows if s.price < price + band] + [s.price for s in highs if s.price < price - band], tol)
    out.support = sup[:3]
    out.resistance = res[:3]
    return out
