"""Market Profile (TPO) from 5-min candles.

Each 5-min candle is one TPO period (letter). We bucket price into rows and mark
which periods traded at each row, then derive:
  * TPO POC       — row touched by the most periods
  * Value area    — 70% of TPO count around POC
  * Initial Balance (IB) high/low — first `ib_minutes` of the session
  * IB extension  — whether later trade broke IB up/down (range extension)
  * single prints — rows touched by exactly one period (imbalance/impulse zones)
  * poor high/low — multiple periods at the extreme (unfinished auction)
  * day type      — Normal / Normal-Variation / Trend / Neutral (heuristic)
TPO uses price/time only, so it runs on the index series directly.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from string import ascii_uppercase, ascii_lowercase

from ..candles import Candle

_LETTERS = ascii_uppercase + ascii_lowercase


@dataclass
class MarketProfileRead:
    tpo_poc: float | None = None
    va_high: float | None = None
    va_low: float | None = None
    ib_high: float | None = None
    ib_low: float | None = None
    ib_extension: str = "none"       # none / up / down / both
    single_prints: list[float] = field(default_factory=list)
    poor_high: bool = False
    poor_low: bool = False
    day_type: str = "n/a"
    range_pct_of_ib: float | None = None

    def to_dict(self):
        return {
            "tpo_poc": _r(self.tpo_poc), "va_high": _r(self.va_high), "va_low": _r(self.va_low),
            "ib_high": _r(self.ib_high), "ib_low": _r(self.ib_low),
            "ib_extension": self.ib_extension,
            "single_prints": [_r(x) for x in self.single_prints[:6]],
            "poor_high": self.poor_high, "poor_low": self.poor_low,
            "day_type": self.day_type,
            "range_pct_of_ib": _r(self.range_pct_of_ib),
        }


def _r(x, n=2):
    return None if x is None else round(x, n)


def _to_tpo_periods(candles: list[Candle], candle_minutes: int,
                    tpo_period_minutes: int) -> list[Candle]:
    """Aggregate execution candles (e.g. 5-min) into TPO periods (e.g. 30-min).

    Each TPO period spans [min low, max high] of the candles inside it, so it
    contributes the right rows to the profile. Classic market profile uses 30-min
    periods; 15-min is a common Indian-index compromise.
    """
    if tpo_period_minutes <= candle_minutes:
        return candles
    buckets: dict = {}
    order: list = []
    for c in candles:
        m = (c.start.minute // tpo_period_minutes) * tpo_period_minutes
        key = c.start.replace(minute=m, second=0, microsecond=0)
        if key not in buckets:
            buckets[key] = Candle(key, c.open, c.high, c.low, c.close, c.volume, c.oi)
            order.append(key)
        else:
            b = buckets[key]
            b.high = max(b.high, c.high)
            b.low = min(b.low, c.low)
            b.close = c.close
            b.volume += c.volume
    return [buckets[k] for k in order]


def analyze(candles: list[Candle], bin_width: float, candle_minutes: int = 5,
            ib_minutes: int = 60, value_area_pct: float = 0.70,
            tpo_period_minutes: int = 30) -> MarketProfileRead:
    out = MarketProfileRead()
    if not candles or bin_width <= 0:
        return out
    exec_candles = candles                 # keep 5-min for IB timing
    candles = _to_tpo_periods(candles, candle_minutes, tpo_period_minutes)

    lo = min(c.low for c in candles)
    hi = max(c.high for c in candles)
    if hi <= lo:
        hi = lo + bin_width
    n_rows = max(1, int((hi - lo) / bin_width) + 1)

    def row_of(p):
        return min(n_rows - 1, max(0, int((p - lo) / bin_width)))

    center = [lo + (r + 0.5) * bin_width for r in range(n_rows)]
    counts = [0] * n_rows
    period_at_row: list[set] = [set() for _ in range(n_rows)]

    for period, c in enumerate(candles):
        r0, r1 = row_of(c.low), row_of(c.high)
        for r in range(r0, r1 + 1):
            counts[r] += 1
            period_at_row[r].add(period)

    total = sum(counts)
    if total == 0:
        return out

    poc_row = max(range(n_rows), key=lambda r: counts[r])
    out.tpo_poc = center[poc_row]

    # value area around POC
    target = total * value_area_pct
    lo_r = hi_r = poc_row
    acc = counts[poc_row]
    while acc < target and (lo_r > 0 or hi_r < n_rows - 1):
        down = counts[lo_r - 1] if lo_r > 0 else -1
        up = counts[hi_r + 1] if hi_r < n_rows - 1 else -1
        if up >= down:
            hi_r += 1
            acc += max(0, up)
        else:
            lo_r -= 1
            acc += max(0, down)
    out.va_low, out.va_high = center[lo_r], center[hi_r]

    # Initial Balance — always from the 5-min execution candles (first hour)
    n_ib = max(1, ib_minutes // candle_minutes)
    ib = exec_candles[:n_ib]
    out.ib_high = max(c.high for c in ib)
    out.ib_low = min(c.low for c in ib)
    after = exec_candles[n_ib:]
    ext_up = any(c.high > out.ib_high for c in after)
    ext_down = any(c.low < out.ib_low for c in after)
    out.ib_extension = ("both" if ext_up and ext_down else
                        "up" if ext_up else "down" if ext_down else "none")
    day_range = hi - lo
    ib_range = out.ib_high - out.ib_low
    out.range_pct_of_ib = (day_range / ib_range) if ib_range > 0 else None

    # single prints (rows touched by exactly one period), excluding extremes handled below
    out.single_prints = [center[r] for r in range(n_rows) if counts[r] == 1]

    # poor high / low: >=2 periods at the extreme row (unfinished auction)
    out.poor_high = counts[n_rows - 1] >= 2
    out.poor_low = counts[0] >= 2

    # day type heuristic
    rng_mult = out.range_pct_of_ib or 1.0
    if out.ib_extension == "none" and rng_mult < 1.15:
        out.day_type = "Normal"
    elif out.ib_extension in ("up", "down") and rng_mult >= 1.6:
        out.day_type = "Trend"
    elif out.ib_extension in ("up", "down"):
        out.day_type = "Normal-Variation"
    elif out.ib_extension == "both":
        out.day_type = "Neutral"
    else:
        out.day_type = "Balanced"
    return out
