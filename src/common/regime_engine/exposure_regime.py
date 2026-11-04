"""
Five-state EXPOSURE regime ("character first"), as proposed in the user's spec of 2026-10-04.

  High_Volatility  realised-vol 1y percentile > HV_PCT  or  implied-vol 1y percentile > 90
                   or (realised-vol pct > 60 and 60-day drawdown < -10%)
  Bull_Trend       efficiency ratio(20) > ER  and ADX(14) > ADX  and 20d return > 0 and close > EMA200
  Bear_Trend       efficiency ratio(20) > ER  and ADX(14) > ADX  and 20d return < 0 and close < EMA200
  Compression      realised-vol pct < 30 and efficiency ratio(20) < 0.15
  Range            everything else
(first match wins, in that order). Default thresholds = the middle of the ranges given in the spec:
ER 0.30 (0.25-0.35), ADX 22.5 (20-25), HV_PCT 80 (70-85). The validation script also runs every corner of
those ranges, so no single "best" setting is picked.

Exposure (long only, 0..1; the spec's 1.0-1.2x for Bull is tested as a separate variant):
  Bull 1.0 · Compression 0.9 · Range 0.6 · High_Vol 0.2 · Bear 0.15

All inputs use data up to the close of day t only. A label decided at the close of t is traded from t+1.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from src.common.regime_engine.features import character as CH

EXPOSURE = {"Bull_Trend": 1.0, "Compression": 0.9, "Range": 0.6, "High_Volatility": 0.2, "Bear_Trend": 0.15}
ORDER = ["Bull_Trend", "Compression", "Range", "High_Volatility", "Bear_Trend"]


def _pct(s: pd.Series, win: int = 252, minp: int = 120) -> pd.Series:
    return s.rolling(win, min_periods=minp).apply(lambda a: (a <= a[-1]).mean() * 100, raw=True)


def features(bars: pd.DataFrame, iv: pd.Series | None = None, ann: int = 252) -> pd.DataFrame:
    o, h, l, c = (bars[k].astype(float) for k in ("open", "high", "low", "close"))
    f = pd.DataFrame(index=bars.index)
    lr = np.log(c / c.shift())
    f["vol_20d"] = lr.rolling(20).std() * np.sqrt(ann)
    f["vol_pct"] = _pct(f["vol_20d"])
    f["er_20"] = CH.efficiency_ratio(c, 20)
    f["adx"], _, _, _ = CH.adx(h.where(h >= l, c), l.where(h >= l, c), c)
    f["ret_20d"] = c / c.shift(20) - 1
    f["ema200"] = c.ewm(span=200, adjust=False).mean()
    f["px_vs_ema200"] = c / f["ema200"] - 1
    f["dd_60d"] = c / c.rolling(60, min_periods=20).max() - 1
    f["rsi"] = 100 - 100 / (1 + lr.clip(lower=0).ewm(alpha=1 / 14).mean() / (-lr.clip(upper=0)).ewm(alpha=1 / 14).mean())
    if iv is not None:
        ivs = iv.reindex(bars.index, method="ffill", limit=5)
        f["iv"] = ivs
        f["iv_pct"] = _pct(ivs)
    else:
        f["iv_pct"] = np.nan
    return f


def label(f: pd.DataFrame, er: float = 0.30, adx: float = 22.5, hv_pct: float = 80, iv_pct: float = 90) -> pd.Series:
    hv = (f["vol_pct"] > hv_pct) | (f["iv_pct"] > iv_pct) | ((f["vol_pct"] > 60) & (f["dd_60d"] < -0.10))
    trend = (f["er_20"] > er) & (f["adx"] > adx)
    bull = trend & (f["ret_20d"] > 0) & (f["px_vs_ema200"] > 0)
    bear = trend & (f["ret_20d"] < 0) & (f["px_vs_ema200"] < 0)
    comp = (f["vol_pct"] < 30) & (f["er_20"] < 0.15)
    out = pd.Series("Range", index=f.index, dtype=object)
    out[comp] = "Compression"
    out[bear] = "Bear_Trend"
    out[bull] = "Bull_Trend"
    out[hv] = "High_Volatility"
    out[f["vol_pct"].isna() | f["adx"].isna() | f["ema200"].isna()] = None
    return out


def exposure(lab: pd.Series, table: dict | None = None) -> pd.Series:
    t = table or EXPOSURE
    return lab.map(t).astype(float)
