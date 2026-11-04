"""
Scorer contract (BRD §6.1) and provenance wrapper (BRD §9).

Hard rule (AGENTIC_DESIGN §1/§7, AI_LEARNING_GOVERNANCE §6):
    every `score` is the return value of a deterministic function.
    Nothing outside this package may write a score.

A FeatureScore is frozen, so downstream code (including any LLM agent layer)
cannot overwrite the number before it is serialized.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field, asdict
from typing import Optional

FETCH_OK, FETCH_ERROR, FETCH_STALE = "ok", "error", "stale"


@dataclass(frozen=True)
class Provenance:
    source: str                      # e.g. "csv:backtest/regime/nifty_regime_daily_2023_2026.csv"
    instrument_or_series_id: str     # e.g. "NIFTY_SPOT_1D"
    as_of: str                       # timestamp of the last bar the score is allowed to see
    raw_value: Optional[float]
    fetch_status: str = FETCH_OK     # ok | error | stale
    fetched_at: Optional[str] = None


@dataclass(frozen=True)
class FeatureScore:
    feature_id: str                  # e.g. "C.6a_ema21_distance"
    group: str                       # TREND | VOLATILITY | BREADTH
    score: Optional[float]           # in [-1, +1]; None = could not compute (never a fallback number)
    regime_tag: Optional[str] = None
    provenance: Optional[Provenance] = None
    sign_convention: str = ""        # what +1 means, documented in code

    def __post_init__(self):
        if self.score is not None:
            if not isinstance(self.score, (int, float)) or math.isnan(self.score):
                raise ValueError(f"{self.feature_id}: score must be a real number or None")
            if not -1.0 <= self.score <= 1.0:
                raise ValueError(f"{self.feature_id}: score {self.score} outside [-1, 1]")
        if self.score is not None and self.provenance is not None and self.provenance.fetch_status != FETCH_OK:
            raise ValueError(f"{self.feature_id}: a score is only allowed when fetch_status == ok")

    def to_dict(self) -> dict:
        return asdict(self)


def clip_score(x) -> Optional[float]:
    """NaN/inf -> None, otherwise clipped to [-1, 1]."""
    if x is None:
        return None
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    if math.isnan(x) or math.isinf(x):
        return None
    return max(-1.0, min(1.0, x))
