"""
Group aggregation (BRD §6.2) and meta-aggregation (BRD §6.3). Pure arithmetic.

- Group score = equal-weighted mean of the group's available feature scores
  (within-group weights stay equal until a fit says otherwise).
- Meta score  = sum(w_g * group_g) / sum(|w_g|) over groups that published.
  Missing groups are DROPPED and the rest renormalized - no imputation (BRD §9).
- Label: Bullish if meta > +threshold, Bearish if < -threshold, else Neutral.
- Confidence tier is a fixed rule on |meta| (AGENTIC_DESIGN §4.5).
- top_factors = top-3 |weight x contribution| (linear stand-in for SHAP, BRD §6.2).
"""
from __future__ import annotations

import math
from typing import Dict, List, Optional


def group_scores(feature_scores: Dict[str, Optional[float]], feature_groups: Dict[str, str]) -> Dict[str, Optional[float]]:
    buckets: Dict[str, List[float]] = {}
    for fid, s in feature_scores.items():
        g = feature_groups[fid]
        buckets.setdefault(g, [])
        if s is not None and not (isinstance(s, float) and math.isnan(s)):
            buckets[g].append(float(s))
    return {g: (sum(v) / len(v) if v else None) for g, v in buckets.items()}


def meta_aggregate(groups: Dict[str, Optional[float]], weights: Dict[str, float], threshold: float,
                   conf_high: float, conf_medium: float,
                   feature_scores: Dict[str, Optional[float]], feature_groups: Dict[str, str]) -> dict:
    avail = {g: s for g, s in groups.items() if s is not None and weights.get(g, 0) != 0}
    expected = [g for g, w in weights.items() if w != 0]
    completeness = len(avail) / len(expected) if expected else 0.0
    if not avail:
        return {"meta_score": None, "regime_label": None, "confidence_tier": None,
                "top_factors": [], "data_completeness": completeness}

    denom = sum(abs(weights[g]) for g in avail)
    meta = sum(weights[g] * s for g, s in avail.items()) / denom

    label = "Bullish" if meta > threshold else "Bearish" if meta < -threshold else "Neutral"
    m = abs(meta)
    tier = "High" if m >= conf_high else "Medium" if m >= conf_medium else "Low"
    if completeness < 0.67 and tier == "High":
        tier = "Medium"   # thin data cannot produce a High call

    # contribution of each feature = group weight share x feature score / n features in group
    contribs = []
    for fid, s in feature_scores.items():
        g = feature_groups[fid]
        if g not in avail or s is None or (isinstance(s, float) and math.isnan(s)):
            continue
        n = sum(1 for f2, s2 in feature_scores.items()
                if feature_groups[f2] == g and s2 is not None and not (isinstance(s2, float) and math.isnan(s2)))
        contribs.append((g, fid, round(weights[g] / denom * float(s) / n, 4)))
    contribs.sort(key=lambda t: abs(t[2]), reverse=True)

    return {"meta_score": round(meta, 4), "regime_label": label, "confidence_tier": tier,
            "top_factors": contribs[:3], "data_completeness": round(completeness, 3)}
