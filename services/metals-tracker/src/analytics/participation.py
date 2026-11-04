"""Futures participation (price + OI quadrant) on the current-month contract.

Classifies the near-month futures into the classic price-vs-OI states, graded by
volume conviction:

    price up   + OI up    -> long buildup      (bullish, new longs)
    price down + OI up    -> short buildup     (bearish, new shorts)
    price up   + OI down   -> short covering    (bullish-lite, shorts exiting)
    price down + OI down   -> long unwinding    (bearish-lite, longs exiting)

Uses the futures close (price) + futures OI over a short lookback; volume grades
conviction (a buildup on a volume surge is 'strong', on thin volume 'weak').
OI history is live-only (not retrievable for old dates), so this is a LIVE read,
not part of the 2-year-validated core.

Expiry caveat: near expiry, current-month OI falls as positions ROLL to next
month — that can mimic 'unwinding'. When dte is small we mark low confidence.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass

from ..candles import Candle


@dataclass
class ParticipationRead:
    state: str = "n/a"          # long_buildup / short_buildup / short_covering / long_unwinding / neutral
    bias: str = "neutral"       # long / short / long_weak / short_weak / neutral
    strong: bool = False        # volume-confirmed
    oi_chg_pct: float | None = None
    price_chg: float | None = None
    oi: float | None = None
    roll_warning: bool = False  # near expiry -> OI drop may be a roll, not unwind

    def to_dict(self):
        return {
            "state": self.state, "bias": self.bias, "strong": self.strong,
            "oi_chg_pct": None if self.oi_chg_pct is None else round(self.oi_chg_pct, 1),
            "price_chg": None if self.price_chg is None else round(self.price_chg, 1),
            "oi": None if self.oi is None else int(self.oi),
            "roll_warning": self.roll_warning,
        }


_LABEL = {
    "long_buildup": "Long buildup", "short_buildup": "Short buildup",
    "short_covering": "Short covering", "long_unwinding": "Long unwinding",
    "neutral": "Neutral",
}


def label(state: str) -> str:
    return _LABEL.get(state, state)


def analyze(fut_candles: list[Candle], lookback: int = 3,
            vol_spike_mult: float = 1.5, min_oi_pct: float = 0.15,
            dte: int | None = None) -> ParticipationRead:
    out = ParticipationRead()
    cands = [c for c in fut_candles if c.oi and c.oi > 0]
    if len(cands) < lookback + 1:
        return out
    now = cands[-1]
    prev = cands[-1 - lookback]
    dprice = now.close - prev.close
    doi = now.oi - prev.oi
    out.price_chg = dprice
    out.oi = now.oi
    out.oi_chg_pct = (doi / prev.oi * 100) if prev.oi else 0.0

    # ignore trivial OI moves (noise) -> neutral
    if abs(out.oi_chg_pct) < min_oi_pct:
        out.state, out.bias = "neutral", "neutral"
        return out

    if dprice > 0 and doi > 0:
        out.state, out.bias = "long_buildup", "long"
    elif dprice < 0 and doi > 0:
        out.state, out.bias = "short_buildup", "short"
    elif dprice > 0 and doi < 0:
        out.state, out.bias = "short_covering", "long_weak"
    elif dprice < 0 and doi < 0:
        out.state, out.bias = "long_unwinding", "short_weak"
    else:
        out.state, out.bias = "neutral", "neutral"

    # volume conviction over the same window
    vols = [c.volume for c in cands if c.volume > 0]
    if len(vols) >= 3:
        med = statistics.median(vols[:-1]) or 0.0
        out.strong = med > 0 and now.volume >= vol_spike_mult * med

    # rollover distortion near expiry (positions migrate to next month)
    if dte is not None and dte <= 2 and doi < 0:
        out.roll_warning = True
    return out
