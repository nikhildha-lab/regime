"""
Market CHARACTER features: is the market trending, mean-reverting, choppy, compressed or volatile?
(direction is handled elsewhere; this is about the *quality* of the move)

All features use only bars up to and including t (no look-ahead). Percentiles and robust z-scores are
computed against each series' own past (rolling 1 year by default).

  trend persistence   ADX(14), Kaufman efficiency ratio (20), Hurst exponent (rolling 128, variance-ratio method),
                      lag-1 autocorrelation of daily returns (60), drawdown from 252-bar high
  volatility          close-to-close, Parkinson, Garman-Klass, Yang-Zhang (20 bars, annualised),
                      vol-of-vol (60-bar std of 20-bar vol / its mean), ATR/price, Bollinger width (20, 2),
                      Choppiness index (14), 60-bar return skew
  participation       relative volume (vs 20-bar mean)  -- where volume is real

CHARACTER LABEL (rules fixed before testing; first match wins)
  Volatile     realised-vol (Yang-Zhang) 1-year percentile > 85
  Trending     ADX >= 25 and efficiency ratio >= its 1-year 60th percentile
  Compression  Bollinger width 1-year percentile < 15 and ADX < 20
  Choppy       ADX < 20 or Choppiness index > 61.8
  Mixed        everything else
"""
from __future__ import annotations

import numpy as np
import pandas as pd

ANN = 252


def _pct_rank(s: pd.Series, win: int = 252, minp: int = 120) -> pd.Series:
    return s.rolling(win, min_periods=minp).apply(lambda a: (a <= a[-1]).mean() * 100, raw=True)


def robust_z(s: pd.Series, win: int = 252, minp: int = 120) -> pd.Series:
    med = s.rolling(win, min_periods=minp).median()
    mad = (s - med).abs().rolling(win, min_periods=minp).median() * 1.4826
    return ((s - med) / mad.replace(0, np.nan)).clip(-4, 4)


def adx(h, l, c, n: int = 14):
    up, dn = h.diff(), -l.diff()
    plus = np.where((up > dn) & (up > 0), up, 0.0)
    minus = np.where((dn > up) & (dn > 0), dn, 0.0)
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / n, adjust=False).mean()
    pdi = 100 * pd.Series(plus, index=c.index).ewm(alpha=1 / n, adjust=False).mean() / atr
    mdi = 100 * pd.Series(minus, index=c.index).ewm(alpha=1 / n, adjust=False).mean() / atr
    dx = 100 * (pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan)
    return dx.ewm(alpha=1 / n, adjust=False).mean(), pdi, mdi, atr


def efficiency_ratio(c, n: int = 20):
    return (c - c.shift(n)).abs() / c.diff().abs().rolling(n).sum()


def hurst(c, win: int = 128):
    """Variance-ratio Hurst estimate on log prices: H from how the spread of k-bar changes grows with k."""
    lp = np.log(c)
    lags = np.array([2, 4, 8, 16])
    out = pd.Series(np.nan, index=c.index)
    vals = lp.values
    for i in range(win, len(vals)):
        x = vals[i - win:i + 1]
        sd = [np.std(x[k:] - x[:-k]) for k in lags]
        if min(sd) > 0:
            out.iloc[i] = np.polyfit(np.log(lags), np.log(sd), 1)[0]
    return out


def vol_estimators(o, h, l, c, n: int = 20):
    lr = np.log(c / c.shift())
    cc = lr.rolling(n).std() * np.sqrt(ANN)
    hl = np.log(h / l)
    park = np.sqrt((hl ** 2).rolling(n).mean() / (4 * np.log(2)) * ANN)
    co = np.log(c / o)
    gk = np.sqrt((0.5 * hl ** 2 - (2 * np.log(2) - 1) * co ** 2).rolling(n).mean().clip(lower=0) * ANN)
    oc = np.log(o / c.shift())
    k = 0.34 / (1.34 + (n + 1) / (n - 1))
    rs = (np.log(h / c) * np.log(h / o) + np.log(l / c) * np.log(l / o)).rolling(n).mean()
    yz = np.sqrt((oc.rolling(n).var() + k * co.rolling(n).var() + (1 - k) * rs).clip(lower=0) * ANN)
    return cc, park, gk, yz


def choppiness(h, l, c, n: int = 14):
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    rng = h.rolling(n).max() - l.rolling(n).min()
    return 100 * np.log10(tr.rolling(n).sum() / rng.replace(0, np.nan)) / np.log10(n)


def compute(bars: pd.DataFrame, has_volume: bool = True, hurst_win: int = 128) -> pd.DataFrame:
    """bars: open/high/low/close(/volume), one row per bar. Returns one row of features per bar + 'character'."""
    b = bars.copy()
    o, h, l, c = (b[k].astype(float) for k in ("open", "high", "low", "close"))
    # bars with missing or bad high/low (some index data): fall back to close for the range-based measures
    bad = (h < l) | h.isna() | l.isna() | (h <= 0) | (l <= 0)
    h, l = h.where(~bad, c), l.where(~bad, c)
    o = o.where((o > 0) & o.notna(), c.shift())
    f = pd.DataFrame(index=b.index)
    f["adx"], f["plus_di"], f["minus_di"], atr = adx(h, l, c)
    f["atr_pct"] = atr / c * 100
    f["eff_ratio"] = efficiency_ratio(c)
    f["hurst"] = hurst(c, hurst_win)
    lr = np.log(c / c.shift())
    f["autocorr"] = lr.rolling(60).apply(lambda a: pd.Series(a).autocorr(1), raw=True)
    f["drawdown_pct"] = (c / c.rolling(252, min_periods=20).max() - 1) * 100
    for k, n in (("ret_5", 5), ("ret_20", 20), ("ret_60", 60)):
        f[k] = lr.rolling(n).sum() * 100
    f["vol_cc"], f["vol_park"], f["vol_gk"], f["vol_yz"] = vol_estimators(o, h, l, c)
    v20 = f["vol_cc"]
    f["vol_of_vol"] = v20.rolling(60).std() / v20.rolling(60).mean()
    ma, sd = c.rolling(20).mean(), c.rolling(20).std()
    f["bb_width"] = 4 * sd / ma * 100
    f["chop"] = choppiness(h, l, c)
    f["skew_60"] = lr.rolling(60).skew()
    if has_volume and "volume" in b and b["volume"].fillna(0).gt(0).mean() > 0.8:
        f["rel_volume"] = b["volume"] / b["volume"].rolling(20).mean()
    else:
        f["rel_volume"] = np.nan
    f["vol_pct"] = _pct_rank(f["vol_yz"].fillna(f["vol_cc"]))
    f["bbw_pct"] = _pct_rank(f["bb_width"])
    f["er_pct"] = _pct_rank(f["eff_ratio"])
    f["character"] = label(f)
    return f


def label(f: pd.DataFrame) -> pd.Series:
    out = pd.Series("Mixed", index=f.index, dtype=object)
    chop = (f["adx"] < 20) | (f["chop"] > 61.8)
    comp = (f["bbw_pct"] < 15) & (f["adx"] < 20)
    trend = (f["adx"] >= 25) & (f["er_pct"] >= 60)
    vol = f["vol_pct"] > 85
    out[chop] = "Choppy"
    out[comp] = "Compression"
    out[trend] = "Trending"
    out[vol] = "Volatile"
    out[f["adx"].isna() | f["vol_pct"].isna()] = None
    return out
