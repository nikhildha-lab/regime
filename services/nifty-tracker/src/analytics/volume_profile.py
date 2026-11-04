"""Volume profile from futures 5-min candles.

Distributes each candle's volume across the price bins it spanned, then derives:
  * POC   — price bin with the most volume
  * VAH / VAL — value-area high/low bounding `value_area_pct` of volume
  * HVN / LVN — high / low volume nodes (local peaks / troughs)
Indices carry no volume, so this is always computed on the futures series.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..candles import Candle


@dataclass
class VolumeProfileRead:
    poc: float | None = None
    vah: float | None = None
    val: float | None = None
    hvn: list[float] = field(default_factory=list)
    lvn: list[float] = field(default_factory=list)
    bins: list[tuple[float, float]] = field(default_factory=list)  # (price, volume)
    total_volume: float = 0.0
    price_vs_va: str = "n/a"   # above_value / in_value / below_value

    def to_dict(self):
        return {
            "poc": _r(self.poc), "vah": _r(self.vah), "val": _r(self.val),
            "hvn": [_r(x) for x in self.hvn], "lvn": [_r(x) for x in self.lvn],
            "price_vs_va": self.price_vs_va,
            "total_volume": int(self.total_volume),
            "bins": [[_r(p), int(v)] for p, v in self.bins],
        }


def _r(x, n=2):
    return None if x is None else round(x, n)


def analyze(candles: list[Candle], bin_width: float, value_area_pct: float = 0.70,
            last_price: float | None = None) -> VolumeProfileRead:
    out = VolumeProfileRead()
    cands = [c for c in candles if c.volume > 0]
    if not cands or bin_width <= 0:
        return out

    lo = min(c.low for c in cands)
    hi = max(c.high for c in cands)
    if hi <= lo:
        hi = lo + bin_width
    n_bins = max(1, int((hi - lo) / bin_width) + 1)
    vol = [0.0] * n_bins

    def bin_of(p):
        return min(n_bins - 1, max(0, int((p - lo) / bin_width)))

    for c in cands:
        b0, b1 = bin_of(c.low), bin_of(c.high)
        span = b1 - b0 + 1
        share = c.volume / span
        for b in range(b0, b1 + 1):
            vol[b] += share

    total = sum(vol)
    out.total_volume = total
    if total <= 0:
        return out

    centers = [lo + (b + 0.5) * bin_width for b in range(n_bins)]
    out.bins = list(zip(centers, vol))

    poc_bin = max(range(n_bins), key=lambda b: vol[b])
    out.poc = centers[poc_bin]

    # value area: expand from POC until value_area_pct of volume captured
    target = total * value_area_pct
    lo_b = hi_b = poc_bin
    acc = vol[poc_bin]
    while acc < target and (lo_b > 0 or hi_b < n_bins - 1):
        down = vol[lo_b - 1] if lo_b > 0 else -1
        up = vol[hi_b + 1] if hi_b < n_bins - 1 else -1
        if up >= down:
            hi_b += 1
            acc += max(0, up)
        else:
            lo_b -= 1
            acc += max(0, down)
    out.val = centers[lo_b]
    out.vah = centers[hi_b]

    # HVN/LVN: local maxima/minima of the volume histogram
    for b in range(1, n_bins - 1):
        if vol[b] > vol[b - 1] and vol[b] > vol[b + 1] and vol[b] > total / n_bins:
            out.hvn.append(centers[b])
        if vol[b] < vol[b - 1] and vol[b] < vol[b + 1]:
            out.lvn.append(centers[b])
    out.hvn = sorted(out.hvn, key=lambda p: -vol[bin_of(p)])[:4]
    out.lvn = out.lvn[:4]

    if last_price is not None and out.vah is not None:
        if last_price > out.vah:
            out.price_vs_va = "above_value"
        elif last_price < out.val:
            out.price_vs_va = "below_value"
        else:
            out.price_vs_va = "in_value"
    return out
