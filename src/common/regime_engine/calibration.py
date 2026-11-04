"""
Weight fit + walk-forward validation (BRD §6.5, §10). Deterministic skill the
Calibration Assistant calls; it NEVER publishes a weight set by itself.

Method:
  target  = forward h-bar log return of NIFTY, divided by its trailing volatility
  model   = ridge regression of target on the 3 group scores (no intercept)
  folds   = expanding walk-forward; each test block is preceded by an embargo of h bars
            (purging: training rows whose forward window overlaps the test block are dropped)
  weights = coefficients normalized so sum(|w|) = 1; a group with too little history gets 0
Outputs: final weights (fit on all data) + out-of-sample labels for every test bar.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .aggregator import meta_aggregate
from .engine import GROUPS, FEATURE_GROUPS

HORIZON = {"daily": 5, "4h": 4, "1h": 7}   # 5 trading days; 2 trading days; ~1 trading day
MIN_TRAIN = {"daily": 250, "4h": 400, "1h": 1000}
TEST_BLOCK = {"daily": 60, "4h": 120, "1h": 300}
MIN_GROUP_ROWS = {"daily": 120, "4h": 240, "1h": 500}
RIDGE_LAMBDA = 5.0


def forward_target(close: pd.Series, h: int) -> pd.DataFrame:
    lr = np.log(close)
    fwd = lr.shift(-h) - lr
    vol = lr.diff().rolling(20, min_periods=20).std() * np.sqrt(h)
    return pd.DataFrame({"fwd_ret": fwd, "fwd_z": fwd / vol, "band": 0.5 * vol})


def ridge_fit(X: np.ndarray, y: np.ndarray, lam: float = RIDGE_LAMBDA) -> np.ndarray:
    return np.linalg.solve(X.T @ X + lam * np.eye(X.shape[1]), X.T @ y)


def fit_weights(frame: pd.DataFrame, profile: str, train_mask: np.ndarray) -> dict:
    h = HORIZON[profile]
    tgt = forward_target(frame["close"], h)
    usable = [g for g in GROUPS
              if frame.loc[train_mask, f"group_{g}"].notna().sum() >= MIN_GROUP_ROWS[profile]]
    w = {g: 0.0 for g in GROUPS}
    if not usable:
        return w
    cols = [f"group_{g}" for g in usable]
    df = pd.concat([frame[cols], tgt["fwd_z"]], axis=1)[train_mask].dropna()
    if len(df) < 30:
        return w
    coef = ridge_fit(df[cols].values, df["fwd_z"].values)
    s = np.abs(coef).sum()
    for g, c in zip(usable, coef):
        w[g] = float(c / s) if s > 0 else 0.0
    return w


def label_from_scores(frame: pd.DataFrame, i: int, weights: dict, cfg: dict) -> dict:
    row = frame.iloc[i]
    fs = {fid: (None if pd.isna(row[fid]) else float(row[fid])) for fid in FEATURE_GROUPS}
    groups = {g: (None if pd.isna(row[f"group_{g}"]) else float(row[f"group_{g}"])) for g in GROUPS}
    return meta_aggregate(groups, weights, cfg["label_threshold"], cfg["confidence"]["high"],
                          cfg["confidence"]["medium"], fs, FEATURE_GROUPS)


def walk_forward(frame: pd.DataFrame, profile: str, cfg: dict):
    """Returns (oos DataFrame, list of fold weight dicts)."""
    h, n = HORIZON[profile], len(frame)
    tgt = forward_target(frame["close"], h)
    start = MIN_TRAIN[profile]
    # first bar where the core groups exist
    first_valid = frame[[f"group_TREND", f"group_VOLATILITY"]].dropna().index
    if len(first_valid):
        start += frame.index.get_loc(first_valid[0])
    rows, folds = [], []
    t0 = start
    while t0 < n:
        t1 = min(t0 + TEST_BLOCK[profile], n)
        train_mask = np.zeros(n, dtype=bool)
        train_mask[: max(0, t0 - h)] = True                 # purge + embargo of h bars
        w = fit_weights(frame, profile, train_mask)
        folds.append({"test_start": str(frame.index[t0]), "test_end": str(frame.index[t1 - 1]), "weights": w})
        for i in range(t0, t1):
            out = label_from_scores(frame, i, w, cfg)
            rows.append({"bar": frame.index[i], **{k: out[k] for k in ("meta_score", "regime_label", "confidence_tier", "data_completeness")},
                         "top_factors": out["top_factors"], "vol_state": frame["vol_state"].iloc[i],
                         "fwd_ret": tgt["fwd_ret"].iloc[i], "band": tgt["band"].iloc[i],
                         "past_ret": np.log(frame["close"]).diff(h).iloc[i], "fold_weights": w})
        t0 = t1
    return pd.DataFrame(rows).set_index("bar"), folds


def outcome(label: str, fwd_ret: float, band: float) -> str:
    if pd.isna(fwd_ret) or pd.isna(band) or label is None:
        return "pending"
    actual = "Bullish" if fwd_ret > band else "Bearish" if fwd_ret < -band else "Neutral"
    if label == actual:
        return "neutral-was-right" if label == "Neutral" else "correct"
    return "wrong"


def actual_class(fwd_ret: float, band: float):
    if pd.isna(fwd_ret) or pd.isna(band):
        return None
    return "Bullish" if fwd_ret > band else "Bearish" if fwd_ret < -band else "Neutral"


def persistence_label(past_ret: float, band: float):
    if pd.isna(past_ret) or pd.isna(band):
        return None
    return "Bullish" if past_ret > band else "Bearish" if past_ret < -band else "Neutral"
