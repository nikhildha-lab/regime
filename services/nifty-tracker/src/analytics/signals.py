"""Opportunity engine: fuse the analytics reads into actionable signals.

Each signal has a direction (long/short), a confluence list (the agreeing
factors), a score (how many factors), and a short rationale. Only signals with
>= min_confluence agreeing factors are surfaced, so a lone indicator never
raises an alert on its own. This is the layer the trading-skill / F&O planner
consumes to reason about a trade.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .price_action import PriceActionRead
from .volume_profile import VolumeProfileRead
from .market_profile import MarketProfileRead
from .oi_analysis import OIRead
from .imbalance import ImbalanceRead


@dataclass
class Signal:
    instrument: str
    direction: str          # long / short
    score: int
    title: str
    confluence: list[str]
    rationale: str
    price: float | None

    def key(self) -> str:
        return f"{self.instrument}:{self.direction}:{self.title}"

    def to_dict(self):
        return {
            "instrument": self.instrument, "direction": self.direction,
            "score": self.score, "title": self.title,
            "confluence": self.confluence, "rationale": self.rationale,
            "price": None if self.price is None else round(self.price, 2),
        }


def build_signals(instrument: str, price: float | None,
                  pa: PriceActionRead, vp: VolumeProfileRead,
                  mp: MarketProfileRead, oi: OIRead, imb: ImbalanceRead,
                  min_confluence: int = 2, part=None) -> list[Signal]:
    long_factors: list[str] = []
    short_factors: list[str] = []

    # --- price action ---
    if pa.structure_event in ("BOS_UP", "CHoCH_UP"):
        long_factors.append(f"structure {pa.structure_event}")
    if pa.structure_event in ("BOS_DOWN", "CHoCH_DOWN"):
        short_factors.append(f"structure {pa.structure_event}")
    if pa.trend == "up":
        long_factors.append("trend up (HH/HL)")
    if pa.trend == "down":
        short_factors.append("trend down (LH/LL)")
    if pa.orb_state == "broke_up":
        long_factors.append("ORB breakout up")
    if pa.orb_state == "broke_down":
        short_factors.append("ORB breakout down")
    if price is not None and pa.vwap is not None:
        if price > pa.vwap:
            long_factors.append("above VWAP")
        else:
            short_factors.append("below VWAP")

    # --- volume profile ---
    if vp.price_vs_va == "above_value":
        long_factors.append("accepted above value area")
    if vp.price_vs_va == "below_value":
        short_factors.append("accepted below value area")
    if price is not None and vp.poc is not None:
        # rejection from POC / value edges
        if vp.val is not None and abs(price - vp.val) / max(price, 1) < 0.0008 and imb.delta_state == "buyers":
            long_factors.append("bounce at value-area low")
        if vp.vah is not None and abs(price - vp.vah) / max(price, 1) < 0.0008 and imb.delta_state == "sellers":
            short_factors.append("rejection at value-area high")

    # --- market profile ---
    if mp.ib_extension == "up" and mp.day_type in ("Trend", "Normal-Variation"):
        long_factors.append(f"IB range-extension up ({mp.day_type})")
    if mp.ib_extension == "down" and mp.day_type in ("Trend", "Normal-Variation"):
        short_factors.append(f"IB range-extension down ({mp.day_type})")
    if mp.poor_high:
        short_factors.append("poor high (unfinished, may revisit down)")
    if mp.poor_low:
        long_factors.append("poor low (unfinished, may revisit up)")

    # --- OI ---
    if oi.pcr_signal == "bullish":
        long_factors.append(f"PCR bullish ({oi.pcr})")
    if oi.pcr_signal == "bearish":
        short_factors.append(f"PCR bearish ({oi.pcr})")
    if price is not None and oi.put_wall is not None and abs(price - oi.put_wall) / max(price, 1) < 0.001:
        long_factors.append(f"at put-OI support {oi.put_wall:g}")
    if price is not None and oi.call_wall is not None and abs(price - oi.call_wall) / max(price, 1) < 0.001:
        short_factors.append(f"at call-OI resistance {oi.call_wall:g}")
    # premium+OI per-strike flow (writing vs buying, from the top few moves)
    for b in oi.buildup[:5]:
        k, s = b["kind"], b["strike"]
        if k == "put_writing":       # writers selling puts -> support (bullish)
            long_factors.append(f"put writing {s:g}")
        elif k == "call_long":       # calls being bought (bullish)
            long_factors.append(f"call buying {s:g}")
        elif k == "call_writing":    # writers selling calls -> resistance (bearish)
            short_factors.append(f"call writing {s:g}")
        elif k == "put_long":        # puts being bought (bearish)
            short_factors.append(f"put buying {s:g}")
    # aggregate options premium+OI bias near ATM
    if getattr(oi, "options_bias", "neutral") == "bullish":
        long_factors.append("options flow bullish")
    elif getattr(oi, "options_bias", "neutral") == "bearish":
        short_factors.append("options flow bearish")

    # --- imbalance ---
    if imb.delta_state == "buyers" and imb.volume_spike:
        long_factors.append("buy imbalance on volume spike")
    if imb.delta_state == "sellers" and imb.volume_spike:
        short_factors.append("sell imbalance on volume spike")
    if imb.divergence == "bullish_div":
        long_factors.append("bullish delta divergence")
    if imb.divergence == "bearish_div":
        short_factors.append("bearish delta divergence")
    if imb.absorption:
        # absorption flags a possible reversal against the last push
        (short_factors if imb.delta_state == "buyers" else long_factors).append(
            "absorption (effort vs result)")

    # --- futures participation (price + OI) ---
    if part is not None and getattr(part, "state", "n/a") not in ("n/a", "neutral"):
        tag = f"fut {part.state}" + (" (vol-confirmed)" if part.strong else "")
        # strong states (buildup) add a full factor; weak states (covering/unwinding)
        # only add when volume-confirmed, so a lone OI drop doesn't over-score
        if part.state == "long_buildup":
            long_factors.append(tag)
            if part.strong:
                long_factors.append("fut OI long buildup surge")
        elif part.state == "short_buildup":
            short_factors.append(tag)
            if part.strong:
                short_factors.append("fut OI short buildup surge")
        elif part.state == "short_covering" and not part.roll_warning:
            if part.strong:
                long_factors.append(tag)
        elif part.state == "long_unwinding" and not part.roll_warning:
            if part.strong:
                short_factors.append(tag)

    signals: list[Signal] = []
    if len(long_factors) >= min_confluence:
        signals.append(Signal(
            instrument=instrument, direction="long", score=len(long_factors),
            title="Long confluence", confluence=long_factors, price=price,
            rationale="; ".join(long_factors),
        ))
    if len(short_factors) >= min_confluence:
        signals.append(Signal(
            instrument=instrument, direction="short", score=len(short_factors),
            title="Short confluence", confluence=short_factors, price=price,
            rationale="; ".join(short_factors),
        ))
    signals.sort(key=lambda s: -s.score)
    return signals
