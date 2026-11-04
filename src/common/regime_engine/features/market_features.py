"""
Phase 0 feature scorers from data_store/market_data (BRD Groups C and A).

All scores in [-1, +1], +1 = bullish for NIFTY. Each value is placed on the India trading
date at which it is KNOWN for the daily profile (bar close 15:30 IST):
  - India VIX close, NIFTY futures close/basis ........ same day (known at 15:30)
  - Participant OI, futures open interest (EOD reports) . previous trading day (published after close)
  - US / global markets, USD/INR ....................... previous calendar day or earlier (US closes after India)
Intraday profiles use everything from STRICTLY before the bar's day (see engine).

Group membership:
  VOLATILITY  C.1a India VIX z, C.1b VIX 1-day change, C.14 IV-RV spread
  POSITIONING C.2a futures basis, C.2b OI build-up, C.4a FII index-futures long ratio,
              C.4b FII ratio change, C.3p FII index-option bias (PCR stand-in, see note)
  GLOBAL      A.1 US 10Y change, A.2a US VIX, A.2b S&P 500 trend, A.2c dollar index, A.3 Brent, B.3 USD/INR
Note C.3p: true NIFTY put-call ratio history is not available (NSE's historical options
endpoint truncates); FII net index-option positioning from participant OI is used instead.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .. import data as D
from .price_features import _rolling_pct_rank

FEATURES = {
    "C.1a_india_vix_z": ("VOLATILITY", "+1 = India VIX low vs its last 20 days (calm); -1 = VIX spiking"),
    "C.1b_vix_change": ("VOLATILITY", "+1 = VIX fell sharply today; -1 = VIX jumped"),
    "C.14_iv_rv_spread": ("VOLATILITY", "+1 = implied vol (VIX) cheap vs realized vol; -1 = fear premium high"),
    "C.2a_futures_basis": ("POSITIONING", "+1 = near-month NIFTY futures premium rich vs last 20 days"),
    "C.2b_oi_buildup": ("POSITIONING", "+1 = OI rising with price up (long build-up); -1 = OI rising with price down (short build-up)"),
    "C.4a_fii_fut_long_ratio": ("POSITIONING", "+1 = FII index-futures long share high vs last 60 days"),
    "C.4b_fii_ratio_change": ("POSITIONING", "+1 = FII long share rising fast over 5 days"),
    "C.3p_fii_option_bias": ("POSITIONING", "+1 = FII net index-option position tilted to calls vs last 60 days"),
    "A.1_us10y_change": ("GLOBAL", "+1 = US 10Y yield falling over 20 days"),
    "A.2a_us_vix": ("GLOBAL", "+1 = US VIX low vs last 20 days"),
    "A.2b_spx_trend": ("GLOBAL", "+1 = S&P 500 20-day return high vs its history"),
    "A.2c_dollar_index": ("GLOBAL", "+1 = dollar index falling over 20 days"),
    "A.3_brent": ("GLOBAL", "+1 = Brent crude falling over 20 days"),
    "B.3_usdinr": ("GLOBAL", "+1 = rupee strengthening (USD/INR falling) over 20 days"),
}


def _z(s: pd.Series, w: int, minp: int = None) -> pd.Series:
    minp = minp or max(10, w // 2)
    return (s - s.rolling(w, min_periods=minp).mean()) / s.rolling(w, min_periods=minp).std()


def _t(z):
    return np.tanh(z / 2.0)


def _place(series: pd.Series, dates: pd.DatetimeIndex, lag: bool) -> pd.Series:
    """Value known on each India date: same-day (lag=False) or strictly earlier (lag=True)."""
    s = series.dropna().sort_index()
    left = pd.DataFrame({"d": dates})
    right = pd.DataFrame({"src": s.index, "v": s.values})
    m = pd.merge_asof(left, right, left_on="d", right_on="src", allow_exact_matches=not lag,
                      tolerance=pd.Timedelta(days=7))
    return pd.Series(m["v"].values, index=dates)


def compute_market_features(dates: pd.DatetimeIndex, nifty_close: pd.Series) -> pd.DataFrame:
    """dates: India trading dates (naive). Returns scores aligned to the DAILY profile."""
    out = pd.DataFrame(index=dates)
    if not D.has_market_data():
        for f in FEATURES:
            out[f] = np.nan
        return out

    # ---- VOLATILITY (same day) ----
    vix = D.load_yahoo_daily("^INDIAVIX")["close"]
    out["C.1a_india_vix_z"] = _place(-_t(_z(vix, 20)), dates, lag=False)
    vchg = np.log(vix).diff()
    out["C.1b_vix_change"] = _place(-_t(_z(vchg, 60)), dates, lag=False)
    nifty = nifty_close.copy()
    nifty.index = pd.DatetimeIndex(nifty.index).tz_localize(None) if getattr(nifty.index, "tz", None) else nifty.index
    rv = np.log(nifty).diff().rolling(20, min_periods=20).std() * np.sqrt(252) * 100
    ivrv = (vix.reindex(rv.index) - rv)
    out["C.14_iv_rv_spread"] = _place(-_t(_z(ivrv, 60)), dates, lag=False)

    # ---- POSITIONING ----
    fut = D.load_index_futures()
    nf = fut[fut["symbol"] == "NIFTY"]
    near = nf[nf["expiry"] >= nf["date"]].sort_values(["date", "expiry"]).groupby("date").first()
    basis = (near["close"] / nifty.reindex(near.index) - 1) * 100
    out["C.2a_futures_basis"] = _place(_t(_z(basis, 20)), dates, lag=False)           # futures close known at 15:30
    oi = nf.groupby("date")["open_interest"].sum()
    oi_chg = np.log(oi.replace(0, np.nan)).diff(5)
    px_chg = np.sign(np.log(nifty).diff(5)).reindex(oi_chg.index)
    out["C.2b_oi_buildup"] = _place(_t(_z(oi_chg, 60)).abs() * px_chg, dates, lag=True)  # EOD OI -> next day

    poi = D.load_participant_oi()
    fii = poi[poi["client_type"] == "FII"].set_index("date").sort_index()
    ratio = fii["fut_idx_long"] / (fii["fut_idx_long"] + fii["fut_idx_short"])
    out["C.4a_fii_fut_long_ratio"] = _place(2 * _rolling_pct_rank(ratio, 60, 30) - 1, dates, lag=True)
    out["C.4b_fii_ratio_change"] = _place(_t(_z(ratio.diff(5), 60)), dates, lag=True)
    opt_bias = ((fii["opt_idx_call_long"] - fii["opt_idx_call_short"]) - (fii["opt_idx_put_long"] - fii["opt_idx_put_short"]))
    out["C.3p_fii_option_bias"] = _place(2 * _rolling_pct_rank(opt_bias, 60, 30) - 1, dates, lag=True)

    # ---- GLOBAL (strictly earlier dates) ----
    def chg20(sym):
        c = D.load_yahoo_daily(sym)["close"]
        return c, np.log(c).diff(20)
    tnx, _ = chg20("^TNX")
    out["A.1_us10y_change"] = _place(-_t(_z(tnx.diff(20), 250, 60)), dates, lag=True)
    usvix, _ = chg20("^VIX")
    out["A.2a_us_vix"] = _place(-_t(_z(usvix, 20)), dates, lag=True)
    _, spx20 = chg20("^GSPC")
    out["A.2b_spx_trend"] = _place(_t(_z(spx20, 250, 60)), dates, lag=True)
    _, dxy20 = chg20("DX-Y.NYB")
    out["A.2c_dollar_index"] = _place(-_t(_z(dxy20, 250, 60)), dates, lag=True)
    _, brent20 = chg20("BZ=F")
    out["A.3_brent"] = _place(-_t(_z(brent20, 250, 60)), dates, lag=True)
    _, inr20 = chg20("INR=X")
    out["B.3_usdinr"] = _place(-_t(_z(inr20, 250, 60)), dates, lag=True)
    return out.clip(-1, 1)
