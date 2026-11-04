"""
Self-scoring loop (AI_LEARNING_GOVERNANCE §1). Bookkeeping + arithmetic, no AI.

history.jsonl: one record per regime call
  {date, horizon, regime_label, confidence_tier, weight_set_version, top_factors, outcome, ...}
Outcomes are filled in once the forward return is known.
Scoreboard: hit rate by label, by confidence tier, by weight-set version, vs naive persistence
(BRD §10: "barely beating persistence is not value").
"""
from __future__ import annotations

import json
import os
from typing import Iterable

import pandas as pd

from .calibration import outcome, actual_class, persistence_label

HISTORY_DIR = os.path.join("data_store", "regime_engine")


def append_history(records: Iterable[dict], path: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a") as f:
        for r in records:
            f.write(json.dumps(r, default=str) + "\n")


def records_from_oos(oos: pd.DataFrame, horizon: str, version_prefix: str) -> list:
    recs, fold_id, last_w = [], -1, None
    for ts, r in oos.iterrows():
        if r["fold_weights"] != last_w:
            fold_id, last_w = fold_id + 1, r["fold_weights"]
        recs.append({
            "date": str(ts), "horizon": horizon, "regime_label": r["regime_label"],
            "confidence_tier": r["confidence_tier"], "vol_state": r["vol_state"],
            "weight_set_version": f"{version_prefix}-fold{fold_id:02d}",
            "top_factors": r["top_factors"], "meta_score": r["meta_score"],
            "data_completeness": r["data_completeness"],
            "outcome": outcome(r["regime_label"], r["fwd_ret"], r["band"]),
            "actual": actual_class(r["fwd_ret"], r["band"]),
            "persistence_call": persistence_label(r["past_ret"], r["band"]),
            "source": "walk-forward out-of-sample backfill",
        })
    return recs


def scoreboard(recs: list) -> dict:
    df = pd.DataFrame([r for r in recs if r["outcome"] != "pending"])
    if df.empty:
        return {}
    df["hit"] = df["outcome"].isin(["correct", "neutral-was-right"])
    df["persist_hit"] = df["persistence_call"] == df["actual"]
    df["always_bull_hit"] = df["actual"] == "Bullish"

    def rate(s):
        return round(100 * s.mean(), 1)

    out = {
        "n_scored_calls": int(len(df)),
        "hit_rate_pct": rate(df["hit"]),
        "baseline_persistence_pct": rate(df["persist_hit"]),
        "baseline_always_bullish_pct": rate(df["always_bull_hit"]),
        "by_label": {k: {"n": int(len(g)), "hit_rate_pct": rate(g["hit"])} for k, g in df.groupby("regime_label")},
        "by_confidence": {k: {"n": int(len(g)), "hit_rate_pct": rate(g["hit"])} for k, g in df.groupby("confidence_tier")},
        "per_class": {},
        "actual_mix_pct": {k: round(100 * v, 1) for k, v in df["actual"].value_counts(normalize=True).items()},
    }
    for c in ("Bullish", "Bearish", "Neutral"):
        pred, act = df["regime_label"] == c, df["actual"] == c
        tp = (pred & act).sum()
        out["per_class"][c] = {
            "precision_pct": round(100 * tp / pred.sum(), 1) if pred.sum() else None,
            "recall_pct": round(100 * tp / act.sum(), 1) if act.sum() else None,
            "n_called": int(pred.sum()),
        }
    return out
