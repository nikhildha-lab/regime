"""Per-timeframe regime and paper variants (2026-10-31, Nikhil). Same five names as the exposure regime.

tf_regime(bars)   one timeframe, on its OWN bars and against its OWN history (last <= 2000 bars):
    High Volatility  vol rank (20-bar vol vs this timeframe's past) > 80, or > 60 with a 60-bar drop of more than 10 %
    Bull Trend       efficiency(20) > 0.30 and ADX(14) > 22.5 and trend score >= +3
    Bear Trend       same, trend score <= -3
    Compression      vol rank < 30 and efficiency < 0.15
    Range            anything else            (checked in that order: High Volatility first)
  trend score = simplified dashboard Trend (-10..+10): half price vs 200-bar EMA, half 20-bar momentum + EMA 9/21/50 stack
  (correlation 0.96 with the dashboard score, scripts/research/regime_tf_combo.py).
  For understanding only - the live size still comes from exposure_regime.py.

paper variants of the LIVE daily regime (record only, never used for size; promotion only with Nikhil's approval):
    dir_score   live rules, but up / down from the daily trend score (>= +3 / <= -3) instead of 20-day return + 200-day EMA
                (2022-26 test: Nifty trend lag 11 -> 3 days, sizing Sharpe 0.34 -> 0.55; Bank Nifty / ETH a little worse)
    confirm_2d  live label, but a change needs 2 days in a row (High Volatility still at once): about 40 % fewer flips
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from src.common.regime_engine.features import character as CH

NAMES = ["Bull_Trend", "Bear_Trend", "Range", "Compression", "High_Volatility"]


def features(b: pd.DataFrame, rank_win: int = 2000) -> pd.DataFrame:
    c, h, l = (b[k].astype(float) for k in ("close", "high", "low"))
    lr = np.log(c).diff(); v = lr.rolling(20).std()
    win = min(rank_win, len(c))
    vr = v.rolling(win, min_periods=max(60, win // 3)).rank(pct=True) * 100
    er = CH.efficiency_ratio(c, 20); adx, _, _, _ = CH.adx(h.where(h >= l, c), l.where(h >= l, c), c)
    e200 = c.ewm(span=200, adjust=False).mean(); sd200 = lr.rolling(200, min_periods=60).std() * np.sqrt(200)
    d1 = ((c / e200 - 1) / sd200).clip(-1, 1)
    m1 = ((c / c.shift(20) - 1) / (v * np.sqrt(20))).clip(-1, 1)
    e9, e21, e50 = (c.ewm(span=n, adjust=False).mean() for n in (9, 21, 50))
    m2 = (np.sign(c - e9) + np.sign(e9 - e21) + np.sign(e21 - e50)) / 3
    ts = (d1 + (m1 + m2) / 2) / 2 * 10
    dd = c / c.rolling(60, min_periods=20).max() - 1
    return pd.DataFrame({"vol_rank": vr, "er": er, "adx": adx, "trend": ts, "dd60": dd})


def label(f: pd.DataFrame) -> pd.Series:
    out = pd.Series("Range", index=f.index, dtype=object)
    trend = (f.er > 0.30) & (f.adx > 22.5)
    out[(f.vol_rank < 30) & (f.er < 0.15)] = "Compression"
    out[trend & (f.trend <= -3)] = "Bear_Trend"
    out[trend & (f.trend >= 3)] = "Bull_Trend"
    out[(f.vol_rank > 80) | ((f.vol_rank > 60) & (f.dd60 < -0.10))] = "High_Volatility"
    out[f.vol_rank.isna() | f.adx.isna()] = None
    return out


def tf_regime(bars: pd.DataFrame) -> dict | None:
    b = bars.dropna(subset=["close"]).tail(2000)
    if len(b) < 120:
        return None
    f = features(b); lab = label(f)
    if lab.iloc[-1] is None:
        return None
    runs = (lab != lab.shift()).cumsum()
    r = f.iloc[-1]
    return {"regime": lab.iloc[-1], "bars": int((runs == runs.iloc[-1]).sum()),
            "vol_rank": round(float(r.vol_rank)), "er": round(float(r.er), 2), "adx": round(float(r.adx), 1),
            "trend": round(float(r.trend), 1), "dd60": round(float(r.dd60) * 100, 1), "history_bars": len(b)}


def stick(lab: pd.Series, n: int = 2, fast=("High_Volatility",)) -> pd.Series:
    o, cur, cand, k = [], None, None, 0
    for x in lab:
        if cur is None or x is None or (isinstance(x, float) and np.isnan(x)):
            cur = x if cur is None else cur
        elif x != cur:
            if x in fast:
                cur, k = x, 0
            else:
                k = k + 1 if x == cand else 1; cand = x
                if k >= n:
                    cur, k = x, 0
        else:
            k = 0
        o.append(cur)
    return pd.Series(o, index=lab.index)


def paper_variants(bars: pd.DataFrame, f_live: pd.DataFrame, lab_live: pd.Series) -> dict:
    """dir_score and confirm_2d for the LIVE daily regime. f_live / lab_live from exposure_regime.features / label."""
    ts = features(bars)["trend"].reindex(f_live.index)
    hv = (f_live["vol_pct"] > 80) | (f_live["iv_pct"] > 90) | ((f_live["vol_pct"] > 60) & (f_live["dd_60d"] < -0.10))
    trend = (f_live["er_20"] > 0.30) & (f_live["adx"] > 22.5)
    d = pd.Series("Range", index=f_live.index, dtype=object)
    d[(f_live["vol_pct"] < 30) & (f_live["er_20"] < 0.15)] = "Compression"
    d[trend & (ts <= -3)] = "Bear_Trend"
    d[trend & (ts >= 3)] = "Bull_Trend"
    d[hv] = "High_Volatility"
    d[lab_live.isna()] = None
    c2 = stick(lab_live.dropna())
    run = lambda s: int(((s != s.shift()).cumsum() == (s != s.shift()).cumsum().iloc[-1]).sum())
    return {"dir_score": {"regime": d.iloc[-1], "days": run(d.dropna()), "trend_score": None if pd.isna(ts.iloc[-1]) else round(float(ts.iloc[-1]), 1)},
            "confirm_2d": {"regime": c2.iloc[-1], "days": run(c2)}}
