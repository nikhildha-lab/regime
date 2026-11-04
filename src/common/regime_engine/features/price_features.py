"""
Deterministic feature scorers built from a single close series (Group C subset).

Every function takes a close series indexed by bar time and returns a Series of
scores in [-1, +1] (NaN = cannot compute -> becomes None in the contract).
The value at bar t uses ONLY closes up to and including bar t (rolling windows,
no centering, no future rows). tests/regime_engine/test_no_lookahead.py checks this.

Sign convention for every score: +1 = bullish for Nifty, -1 = bearish.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

# Feature registry: id -> (group, sign convention text)
FEATURES = {
    "C.6a_ema21_distance": ("TREND", "+1 = close far above its 21-EMA, scaled by recent volatility"),
    "C.6b_sma5_8_stack": ("TREND", "+1 = SMA5 > SMA8 with both rising (legacy regime_detail), -1 = mirror, 0 = choppy"),
    "C.6c_return_z": ("TREND", "+1 = 20-bar return unusually high vs the trailing 120-bar distribution"),
    "C.6d_ema50_slope": ("TREND", "+1 = 50-EMA rising steeply relative to volatility"),
    "C.12a_realized_vol_pct": ("VOLATILITY", "+1 = realized vol in the bottom of its range (calm, risk-on); -1 = top (stress)"),
    "C.12b_vol_change": ("VOLATILITY", "+1 = realized vol falling over 5 bars; -1 = rising fast"),
}


def _tanh_half(z: pd.Series) -> pd.Series:
    return np.tanh(z / 2.0)


def _rolling_pct_rank(s: pd.Series, window: int, min_periods: int) -> pd.Series:
    """Percentile rank of the LAST value inside its trailing window, in [0, 1]."""
    def rank_last(a):
        last = a[-1]
        return (np.sum(a < last) + 0.5 * np.sum(a == last) - 0.5) / max(len(a) - 1, 1)
    return s.rolling(window, min_periods=min_periods).apply(rank_last, raw=True).clip(0, 1)


def realized_vol(close: pd.Series, n: int = 20) -> pd.Series:
    return np.log(close).diff().rolling(n, min_periods=n).std()


def ema21_distance(close: pd.Series) -> pd.Series:
    ema = close.ewm(span=21, adjust=False, min_periods=21).mean()
    vol = realized_vol(close, 20)
    z = (np.log(close) - np.log(ema)) / (vol * np.sqrt(5))   # distance in "~1-week sigma" units
    return _tanh_half(z)


def sma5_8_stack(close: pd.Series, slope_lookback: int = 5) -> pd.Series:
    f, s = close.rolling(5).mean(), close.rolling(8).mean()
    sf, ss = f - f.shift(slope_lookback), s - s.shift(slope_lookback)
    out = pd.Series(0.0, index=close.index)
    out[(f > s) & (sf > 0) & (ss > 0)] = 1.0
    out[(f < s) & (sf < 0) & (ss < 0)] = -1.0
    out[ss.isna()] = np.nan
    return out


def return_z(close: pd.Series, n: int = 20, window: int = 120) -> pd.Series:
    r = np.log(close).diff(n)
    mu = r.rolling(window, min_periods=60).mean()
    sd = r.rolling(window, min_periods=60).std()
    return _tanh_half((r - mu) / sd)


def ema50_slope(close: pd.Series, k: int = 10) -> pd.Series:
    ema = close.ewm(span=50, adjust=False, min_periods=50).mean()
    slope = np.log(ema).diff(k)
    vol = realized_vol(close, 20) * np.sqrt(k)
    return _tanh_half(slope / vol * 2.0)


def realized_vol_pct(close: pd.Series, window: int = 120) -> pd.Series:
    pct = _rolling_pct_rank(realized_vol(close, 20), window, 60)
    return 1.0 - 2.0 * pct


def vol_change(close: pd.Series, k: int = 5) -> pd.Series:
    rv = realized_vol(close, 20)
    ch = np.log(rv).diff(k)
    sd = ch.rolling(120, min_periods=60).std()
    return -_tanh_half(ch / sd)


def vol_state(close: pd.Series, window: int = 120) -> pd.Series:
    """Categorical tag: Low / Normal / High realized volatility vs its own trailing range."""
    pct = _rolling_pct_rank(realized_vol(close, 20), window, 60)
    out = pd.Series(np.where(pct < 1 / 3, "Low", np.where(pct > 2 / 3, "High", "Normal")), index=close.index)
    out[pct.isna()] = None
    return out


def compute_price_features(close: pd.Series) -> pd.DataFrame:
    close = close.astype(float)
    return pd.DataFrame({
        "C.6a_ema21_distance": ema21_distance(close),
        "C.6b_sma5_8_stack": sma5_8_stack(close),
        "C.6c_return_z": return_z(close),
        "C.6d_ema50_slope": ema50_slope(close),
        "C.12a_realized_vol_pct": realized_vol_pct(close),
        "C.12b_vol_change": vol_change(close),
    }, index=close.index)
