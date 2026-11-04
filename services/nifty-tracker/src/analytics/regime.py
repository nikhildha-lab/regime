"""Market regime / stance — the 'what to do' layer.

Synthesizes the structure reads (trend, day-type, VWAP, value acceptance, IB
extension, ORB) into one actionable stance:

    trend_up   -> "Buy dips"       (only longs; buy pullbacks to support)
    trend_down -> "Sell rallies"   (only shorts; sell bounces to resistance)
    range      -> "Fade edges"     (buy below POC / sell above POC)
    no_trade   -> "Stand aside"    (mixed / choppy)

It is built from STRUCTURE (which is in the validated backtest), so the regime
gate can itself be backtested — unlike the live-only OI reads, which enter here
only as optional confirmation.

`aligns(rr, direction, price)` says whether a signal is WITH the regime (take)
or AGAINST it (skip) — the basis for regime-gated trading.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class RegimeRead:
    regime: str = "no_trade"          # trend_up / trend_down / range / no_trade
    stance: str = "Stand aside"
    bull: int = 0
    bear: int = 0
    confidence: str = "weak"          # strong / moderate / weak
    buy_level: float | None = None    # where to buy (dip support / range low)
    sell_level: float | None = None   # where to sell (rally resistance / range high)
    poc: float | None = None
    note: str = ""
    tf: int = 0                       # timeframe (min) the regime is read on

    def to_dict(self):
        return {
            "regime": self.regime, "stance": self.stance,
            "bull": self.bull, "bear": self.bear, "confidence": self.confidence,
            "buy_level": _r(self.buy_level), "sell_level": _r(self.sell_level),
            "note": self.note, "tf": self.tf,
        }


def _r(x, n=1):
    return None if x is None else round(x, n)


def classify(pa, vp, mp, price, oi=None, part=None) -> RegimeRead:
    out = RegimeRead()
    if price is None:
        return out
    bull = bear = 0

    if pa.trend == "up":
        bull += 1
    elif pa.trend == "down":
        bear += 1
    if pa.vwap is not None:
        bull += 1 if price > pa.vwap else 0
        bear += 1 if price < pa.vwap else 0
    if vp.price_vs_va == "above_value":
        bull += 1
    elif vp.price_vs_va == "below_value":
        bear += 1
    if pa.structure_event in ("BOS_UP", "CHoCH_UP"):
        bull += 1
    elif pa.structure_event in ("BOS_DOWN", "CHoCH_DOWN"):
        bear += 1
    if mp.ib_extension == "up":
        bull += 1
    elif mp.ib_extension == "down":
        bear += 1
    if pa.orb_state == "broke_up":
        bull += 1
    elif pa.orb_state == "broke_down":
        bear += 1

    out.bull, out.bear = bull, bear
    out.poc = vp.poc
    gap = bull - bear
    trending = mp.day_type in ("Trend", "Normal-Variation")

    if gap >= 2 and (trending or bull >= 3):
        out.regime, out.stance = "trend_up", "Buy dips"
    elif gap <= -2 and (trending or bear >= 3):
        out.regime, out.stance = "trend_down", "Sell rallies"
    elif mp.day_type in ("Balanced", "Normal", "Neutral") and abs(gap) <= 1:
        out.regime, out.stance = "range", "Fade edges"
    else:
        out.regime, out.stance = "no_trade", "Stand aside"

    ag = abs(gap)
    out.confidence = "strong" if ag >= 4 else "moderate" if ag >= 2 else "weak"

    # action levels: support below (to buy), resistance above (to sell)
    call_wall = getattr(oi, "call_wall", None) if oi else None
    put_wall = getattr(oi, "put_wall", None) if oi else None
    below = [v for v in (pa.vwap, vp.val, put_wall, pa.last_swing_low, mp.ib_low)
             if v is not None and v < price]
    above = [v for v in (pa.vwap, vp.vah, call_wall, pa.last_swing_high, mp.ib_high)
             if v is not None and v > price]
    out.buy_level = max(below) if below else None
    out.sell_level = min(above) if above else None

    if out.regime == "trend_up":
        out.note = f"longs favored; buy pullbacks toward {_r(out.buy_level)}"
    elif out.regime == "trend_down":
        out.note = f"shorts favored; sell bounces toward {_r(out.sell_level)}"
    elif out.regime == "range":
        out.note = f"fade: buy near {_r(out.buy_level)}, sell near {_r(out.sell_level)}"
    else:
        out.note = "mixed / choppy — no clean edge"
    return out


def aligns(rr: RegimeRead, direction: str, price: float | None) -> bool:
    """Is a signal WITH the regime (take it) or AGAINST it (skip)?"""
    if rr.regime == "trend_up":
        return direction == "long"
    if rr.regime == "trend_down":
        return direction == "short"
    if rr.regime == "range":
        if rr.poc is None or price is None:
            return True
        return (direction == "long" and price <= rr.poc) or \
               (direction == "short" and price >= rr.poc)
    return False   # no_trade
