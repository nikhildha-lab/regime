"""Delta divergence — price vs order-flow delta disagreement.

Two reads, both off the REAL footprint delta (buy − sell, bid/ask classified):

1. Swing-pivot divergence (the tradable one):
     bullish  — price prints a LOWER low but cumulative delta makes a HIGHER low
                (sellers exhausting into the new low)
     bearish  — price prints a HIGHER high but cumulative delta makes a LOWER high
                (buyers thinning at the new high)
   Strongest when it coincides with absorption at a level (see footprint.py).

2. Cumulative-delta trend health:
     price rising while cum-delta rolls over => 'diverging' (move coasting on
     inertia, not fresh flow); both same way => 'confirming'.

Delta is inferred from ~1/sec snapshots, so treat divergence as a lean, not proof.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class DivergenceRead:
    swing: dict | None = None      # {type, pivot_t, level, strength, note}
    health: dict | None = None     # {state, note, slope}

    def to_dict(self):
        return {"swing": self.swing, "health": self.health}

    def confirms(self, direction: str):
        if not self.swing:
            return None
        t = self.swing["type"]
        if t == "bullish_div":
            return direction == "long"
        if t == "bearish_div":
            return direction == "short"
        return None


def _t2m(t):
    try:
        h, m = t.split(":")
        return int(h) * 60 + int(m)
    except Exception:
        return 0


def analyze(swings, delta_series, price_trend, health_lookback=6) -> DivergenceRead:
    out = DivergenceRead()
    if not delta_series:
        return out

    # cumulative delta by bar time
    cum = {}
    run = 0.0
    for t, d in delta_series:
        run += d
        cum[t] = run

    lows = [s for s in swings if s.get("k") == "L"]
    highs = [s for s in swings if s.get("k") == "H"]
    swing = None

    if len(lows) >= 2:
        a, b = lows[-2], lows[-1]
        ca, cb = cum.get(a["t"]), cum.get(b["t"])
        if ca is not None and cb is not None and b["p"] < a["p"] and cb > ca:
            strong = cb > 0 >= ca
            swing = {"type": "bullish_div", "pivot_t": b["t"], "level": b["p"],
                     "strength": "strong" if strong else "moderate",
                     "note": f"bullish delta divergence — lower low @ {b['p']} "
                             f"on rising delta"}

    if len(highs) >= 2:
        a, b = highs[-2], highs[-1]
        ca, cb = cum.get(a["t"]), cum.get(b["t"])
        if ca is not None and cb is not None and b["p"] > a["p"] and cb < ca:
            strong = cb < 0 <= ca
            hd = {"type": "bearish_div", "pivot_t": b["t"], "level": b["p"],
                  "strength": "strong" if strong else "moderate",
                  "note": f"bearish delta divergence — higher high @ {b['p']} "
                          f"on falling delta"}
            # if both fired, keep whichever pivot is more recent
            if swing is None or _t2m(b["t"]) >= _t2m(swing["pivot_t"]):
                swing = hd

    out.swing = swing

    # cumulative-delta trend health
    ts = [t for t, _ in delta_series]
    if len(ts) >= health_lookback + 1:
        cnow = cum[ts[-1]]
        cprev = cum[ts[-1 - health_lookback]]
        slope = cnow - cprev
        if price_trend == "up":
            state = "diverging" if slope < 0 else "confirming"
        elif price_trend == "down":
            state = "diverging" if slope > 0 else "confirming"
        else:
            state = "flat"
        note = {"confirming": "delta confirms price",
                "diverging": "delta not confirming — move coasting",
                "flat": "no clear trend"}[state]
        out.health = {"state": state, "note": note, "slope": round(slope)}

    return out
