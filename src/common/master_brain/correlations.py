"""
Cross-asset correlation structure (daily, point-in-time).

  avg_corr          average pairwise 60-day correlation of daily returns across the 12 markets
                    (spikes = everything moving together = systemic stress; low = dispersion)
  stock_bond        60-day correlation of S&P 500 vs Treasuries (IEF) daily returns
                    (> +0.3: stocks and bonds fall together = inflation / rates regime; < -0.3: bonds hedge = growth scare)
  oil_vs_*          60-day correlation of crude (CL) with S&P, the dollar index and VIX changes
                    -> "oil trading as": Growth (with S&P), Dollar (against DXY), Shock (with VIX), Own supply story
  curve_10y3m       US 10Y minus 3-month T-bill yield (Yahoo ^TNX - ^IRX); below 0 = inverted

Each daily value is stamped with the latest publication time of the inputs used (US close for US data,
00:00 UTC for crypto, 15:31 IST for India).
"""
from __future__ import annotations

from functools import lru_cache

import numpy as np
import pandas as pd

from src.common.master_brain import markets as M
from src.common.master_brain import vol_positioning as VP

WIN = 60


def _close_frame(series: dict) -> tuple[pd.DataFrame, pd.DataFrame]:
    """dict name -> DataFrame(value, available_at) -> aligned daily closes + availability (by calendar date)."""
    vals, avs = {}, {}
    for k, df in series.items():
        if df is None or df.empty:
            continue
        idx = pd.DatetimeIndex(df.index)
        idx = idx.tz_localize(None) if idx.tz is not None else idx
        d = pd.DataFrame({"v": df["value"].values, "a": pd.DatetimeIndex(df["available_at"]).tz_convert("UTC")}, index=idx.normalize())
        d = d[~d.index.duplicated(keep="last")]
        vals[k], avs[k] = d["v"], d["a"]
    return pd.DataFrame(vals).sort_index(), pd.DataFrame(avs).sort_index()


def _mkt(m):
    b = M.market_bars(m)
    return pd.DataFrame({"value": b["close"].values, "available_at": pd.DatetimeIndex(b["available_at"])}, index=b.index)


@lru_cache(maxsize=None)
def frame() -> pd.DataFrame:
    yd = VP.yahoo_daily
    px, av = _close_frame({**{m: _mkt(m) for m in M.MARKETS}, "IEF": yd("IEF"), "DXY": yd("DX-Y.NYB"),
                           "VIX": yd("^VIX"), "TNX": yd("^TNX"), "IRX": yd("^IRX")})
    # weekday calendar: every market's last known close carried to each weekday (crypto weekends roll into Monday;
    # a market holiday counts as 0 change). Correlations then use the same 60 weekdays for every pair.
    px = px.ffill()
    px = px[px.index.dayofweek < 5]
    av = av.ffill().reindex(px.index)
    rets = np.log(px / px.shift())
    out = pd.DataFrame(index=px.index)
    mk = [m for m in M.MARKETS if m in rets]
    r = rets[mk]
    cm = r.rolling(WIN, min_periods=40).corr()
    iu = np.triu_indices(len(mk), 1)
    vals = cm.values.reshape(len(r), len(mk), len(mk))
    out["avg_corr"] = [np.nanmean(v[iu]) if np.isfinite(v[iu]).sum() > 10 else np.nan for v in vals]
    out["stock_bond"] = rets["ES"].rolling(WIN, min_periods=40).corr(rets["IEF"])
    dv = px["VIX"].diff()
    out["oil_vs_spx"] = rets["CRUDE"].rolling(WIN, min_periods=40).corr(rets["ES"])
    out["oil_vs_dxy"] = rets["CRUDE"].rolling(WIN, min_periods=40).corr(rets["DXY"])
    out["oil_vs_vix"] = rets["CRUDE"].rolling(WIN, min_periods=40).corr(dv)
    out["curve_10y3m"] = px["TNX"] - px["IRX"]
    out["btc_vs_nq"] = rets["BTC"].rolling(WIN, min_periods=40).corr(rets["NQ"])
    # availability: worst case of the inputs a column uses, carried forward
    out["available_at"] = av.max(axis=1)
    return out


def oil_story(r) -> str:
    c = {"Growth (moves with stocks)": r.get("oil_vs_spx"), "Dollar (moves against DXY)": -(r.get("oil_vs_dxy") or 0),
         "Shock (moves with fear)": r.get("oil_vs_vix")}
    c = {k: v for k, v in c.items() if v is not None and not np.isnan(v)}
    if not c:
        return None
    k, v = max(c.items(), key=lambda kv: kv[1])
    return k if v >= 0.3 else "Own supply story (weak links)"


def now(as_of) -> dict:
    t = pd.Timestamp(as_of)
    t = t if t.tzinfo else t.tz_localize("Asia/Kolkata")
    f = frame()
    f = f[f["available_at"] <= t.tz_convert("UTC")].dropna(subset=["avg_corr"])
    if f.empty:
        return {}
    r = f.iloc[-1]
    pct = lambda col: float((f[col].iloc[-756:] <= r[col]).mean() * 100)   # noqa: E731  3-year percentile
    sb = r["stock_bond"]
    return {"date": str(f.index[-1].date()), "avg_corr": round(float(r["avg_corr"]), 2), "avg_corr_pct_3y": round(pct("avg_corr")),
            "stock_bond": round(float(sb), 2),
            "stock_bond_reading": "Stocks and bonds falling together (inflation / rates regime)" if sb > 0.3
            else "Bonds hedging stocks (growth-scare regime)" if sb < -0.3 else "No clear link",
            "oil_vs_spx": round(float(r["oil_vs_spx"]), 2), "oil_vs_dxy": round(float(r["oil_vs_dxy"]), 2),
            "oil_vs_vix": round(float(r["oil_vs_vix"]), 2), "oil_story": oil_story(r),
            "curve_10y3m": round(float(r["curve_10y3m"]), 2), "btc_vs_nq": round(float(r["btc_vs_nq"]), 2)}


def series_for_tiles():
    """(value, available_at) frames for the macro tiles."""
    f = frame()
    return {c: pd.DataFrame({"value": f[c], "available_at": f["available_at"]}).dropna() for c in
            ("avg_corr", "stock_bond", "curve_10y3m", "btc_vs_nq")}
