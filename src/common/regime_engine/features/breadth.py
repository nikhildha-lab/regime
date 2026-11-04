"""
C.5 Breadth scorers from a panel of daily stock closes (EOD, as-of lag = 1 day for intraday use).

Input: long DataFrame with columns [date, symbol, close].
Sign convention: +1 = broad participation (bullish), -1 = narrow / falling breadth.
Scores are 60-day percentile ranks mapped to [-1, 1] (BRD §6.1: percentile, never raw level).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .price_features import _rolling_pct_rank

FEATURES = {
    "C.5a_pct_above_50dma": ("BREADTH", "+1 = share of stocks above their 50-DMA is high vs the last 60 days"),
    "C.5b_adv_dec_10d": ("BREADTH", "+1 = 10-day net advances high vs the last 60 days"),
}

INDEX_SYMBOLS = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50", "NIFTY 200", "NIFTY 500"}
MIN_STOCKS = 50   # below this many reporting stocks the day is not scored


def compute_breadth_features(panel: pd.DataFrame) -> pd.DataFrame:
    p = panel[~panel["symbol"].isin(INDEX_SYMBOLS) & ~panel["symbol"].str.startswith("NIFTY")]
    wide = p.pivot_table(index="date", columns="symbol", values="close").sort_index()
    sma50 = wide.rolling(50, min_periods=50).mean()
    above = (wide > sma50).astype(float).where(sma50.notna())
    n_valid = above.notna().sum(axis=1)
    pct_above = (above.sum(axis=1) / n_valid.replace(0, np.nan)).where(n_valid >= MIN_STOCKS)

    chg = wide.diff()
    n_chg = chg.notna().sum(axis=1)
    net_ad = ((chg > 0).sum(axis=1) - (chg < 0).sum(axis=1)) / n_chg.replace(0, np.nan)
    net_ad = net_ad.where(n_chg >= MIN_STOCKS)
    ad10 = net_ad.rolling(10, min_periods=10).sum()

    out = pd.DataFrame({
        "C.5a_pct_above_50dma": 2 * _rolling_pct_rank(pct_above, 60, 40) - 1,
        "C.5b_adv_dec_10d": 2 * _rolling_pct_rank(ad10, 60, 40) - 1,
    })
    out["raw_pct_above_50dma"] = pct_above
    return out
