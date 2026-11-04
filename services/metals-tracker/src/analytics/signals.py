"""Opportunity engine: fuse the analytics reads into actionable signals.

Each signal has a direction (long/short), a confluence list (agreeing factors), a
score (how many factors), and a rationale. Only signals with >= min_confluence
agreeing factors are surfaced, so a lone indicator never raises an alert on its
own. This is the layer the dashboard + paper engine + LLM analyst consume.

Crypto note: the option-chain OI factors (PCR / call & put walls / writing) are
replaced by perp-native ones — funding as a contrarian crowd read, estimated
liquidation magnets, and the OI/funding net-flow bias. Perp price+OI participation
(long/short buildup) is folded in exactly as the NSE futures participation was.
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
            "price": None if self.price is None else round(self.price, 4),
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

    # --- perp funding / OI / liquidation magnets ---
    if oi.funding_bias == "bullish":
        long_factors.append(f"funding contrarian bullish ({oi.funding_annual_pct:.0f}%/yr, shorts crowded)")
    elif oi.funding_bias == "bearish":
        short_factors.append(f"funding contrarian bearish ({oi.funding_annual_pct:.0f}%/yr, longs crowded)")
    # liquidation magnets: stacked liquidation liquidity tends to PULL price toward it
    if price is not None and oi.liq_up is not None and (oi.liq_up - price) / max(price, 1) < 0.008:
        long_factors.append(f"short-liq magnet just above {oi.liq_up:g}")
    if price is not None and oi.liq_dn is not None and (price - oi.liq_dn) / max(price, 1) < 0.008:
        short_factors.append(f"long-liq magnet just below {oi.liq_dn:g}")
    if oi.net_bias == "bullish":
        long_factors.append("OI/funding flow bullish")
    elif oi.net_bias == "bearish":
        short_factors.append("OI/funding flow bearish")

    # --- imbalance (REAL CVD from trade-classified flow) ---
    if imb.delta_state == "buyers" and imb.volume_spike:
        long_factors.append("buy imbalance on volume spike")
    if imb.delta_state == "sellers" and imb.volume_spike:
        short_factors.append("sell imbalance on volume spike")
    if imb.divergence == "bullish_div":
        long_factors.append("bullish delta divergence")
    if imb.divergence == "bearish_div":
        short_factors.append("bearish delta divergence")
    if imb.absorption:
        (short_factors if imb.delta_state == "buyers" else long_factors).append(
            "absorption (effort vs result)")

    # --- perp participation (price + OI quadrant) ---
    if part is not None and getattr(part, "state", "n/a") not in ("n/a", "neutral"):
        tag = f"{part.state}" + (" (vol-confirmed)" if part.strong else "")
        if part.state == "long_buildup":
            long_factors.append(tag)
            if part.strong:
                long_factors.append("OI long-buildup surge")
        elif part.state == "short_buildup":
            short_factors.append(tag)
            if part.strong:
                short_factors.append("OI short-buildup surge")
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
