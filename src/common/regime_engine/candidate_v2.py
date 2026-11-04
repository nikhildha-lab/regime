"""
Candidate engine v2 - PAPER ONLY (2026-10-15). Not used for any decision or position size.
It runs next to today's engine so both can be compared day by day (log: data_store/paper/candidate_v2_log.csv).

Size (Nifty / Bank Nifty), research: scripts/research/test_candidate_engine.py
  v2 size = (rule exposure + min(1, 10% / India VIX)) / 2, halved while the bear is confirmed
            (>= 2 of: EMA 9/21/50 stacked down, SMA50 < SMA200 with price below, below the Ichimoku cloud)
  Back-test 2009-26: Nifty Sharpe 0.64 -> 0.70, median lag 19 -> 11.5 sessions, share of a fall taken 35% -> 24%,
  but more false cuts (12% -> 19% of days) and no gain since 2022. Hence paper first.

Warnings (shown only, not used for size - as size cuts they cut too often):
  risk   : India VIX +15% in 5 sessions; short build-up (5-day price down + futures OI +5%); heavy selling
           volume across all NSE stocks (20-day up/down volume < 1:1.41)  -> "high volatility more likely in 10 days"
  relief : breadth thrust (10-day advance share in its top 20%); India VIX -15% in 5 sessions
           -> "a Bear / High-vol phase is more likely to end in 10 days"
NSE inputs come from data_store/market_data/history (fetch_history.py); they are published in the evening, so they
are used from the next session, and shown as "stale" when older than 4 sessions.
"""
from __future__ import annotations

import os

import numpy as np
import pandas as pd

from src.common.regime_engine import exposure_regime as XR
from src.common.regime_engine.features import indicator_panel as IP

HIST = os.path.join("data_store", "market_data", "history")
FO_SYMBOL = {"NIFTY": "NIFTY", "BANKNIFTY": "BANKNIFTY"}


def _nse(as_of_date):
    fo = br = None
    p = os.path.join(HIST, "nse_fo_daily_summary.csv")
    if os.path.exists(p):
        fo = pd.read_csv(p, usecols=["date", "symbol", "fut_total_oi"])
        fo["date"] = pd.to_datetime(fo["date"])
        fo = fo[fo["date"] < as_of_date]
    p = os.path.join(HIST, "nse_breadth_daily.csv")
    if os.path.exists(p):
        br = pd.read_csv(p)
        br["date"] = pd.to_datetime(br["date"])
        br = br[br["date"] < as_of_date].drop_duplicates("date", keep="last").set_index("date").sort_index()
    return fo, br


def candidate(m: str, b: pd.DataFrame, iv: pd.Series | None, ann: int = 252) -> dict | None:
    if m not in FO_SYMBOL or len(b) < 260:
        return None
    f = XR.features(b, iv, ann)
    rule = XR.exposure(XR.label(f))
    c = b["close"].astype(float)
    vix = f["iv"] if "iv" in f else pd.Series(np.nan, index=b.index)
    vol = (vix / 100).where(vix > 0).fillna(f["vol_20d"])
    p = IP.compute_panel(b, daily=True)
    bear_n = (p["T.1_ema_9_21_50"] == -1).astype(int) + (p["T.2_sma_50_200"] == -1).astype(int) + (p["T.4_ichimoku"] == -1).astype(int)
    bear = bear_n >= 2
    size = (rule + (0.10 / vol).clip(upper=1.0)) / 2 * np.where(bear, 0.5, 1.0)
    today = b.index[-1]
    fo, br = _nse(today + pd.Timedelta(days=1))
    risk, relief, stale = [], [], []
    v5 = vix / vix.shift(5) - 1
    if pd.notna(v5.iloc[-5:]).any():
        if (v5.iloc[-5:] > 0.15).any():
            risk.append("India VIX up more than 15% in 5 sessions")
        if (v5.iloc[-5:] < -0.15).any():
            relief.append("India VIX down more than 15% in 5 sessions")
    if fo is not None:
        x = fo[fo["symbol"] == FO_SYMBOL[m]].drop_duplicates("date", keep="last").set_index("date")["fut_total_oi"].sort_index()
        if len(x) and (today - x.index[-1]).days <= 6:
            oi5 = np.log(x / x.shift(5)).reindex(c.index).shift(1)          # yesterday's NSE file, used today
            sb = (c < c.shift(5)) & (oi5 > 0.05)
            if sb.iloc[-5:].any():
                risk.append("Short build-up: price down over 5 sessions while futures OI rose more than 5%")
        else:
            stale.append("futures OI")
    if br is not None and len(br) and (today - br.index[-1]).days <= 6:
        ud = np.log(br["up_volume"].rolling(20).sum() / br["down_volume"].rolling(20).sum()) / np.log(2)
        tot = br["advances"] + br["declines"]
        ad = br["advances"].rolling(10).sum() / tot.rolling(10).sum()
        thrust = ad > ad.expanding(min_periods=250).quantile(0.8)
        ud, thrust = ud.reindex(c.index).shift(1), thrust.reindex(c.index).shift(1)
        if (ud.iloc[-5:] < -0.5).any():
            risk.append("Heavy selling volume across NSE stocks (20 days)")
        if thrust.iloc[-5:].fillna(False).astype(bool).any():
            relief.append("Breadth thrust: unusually many stocks rising (10 days)")
    else:
        stale.append("NSE breadth")
    return {"status": "paper only - not used for size",
            "size_v2": round(float(size.iloc[-1]), 2),
            "size_today_rule": round(float((rule.iloc[-1] + min(1.0, 0.10 / f["vol_20d"].iloc[-1])) / 2), 2),
            "vol_used": "India VIX" if pd.notna(vix.iloc[-1]) else "20-day realised",
            "bear_confirmed": bool(bear.iloc[-1]), "bear_signals": int(bear_n.iloc[-1]),
            "risk_warnings": risk, "relief_warnings": relief,
            "stale_inputs": stale, "bar": str(today.date())}


def log(m: str, cand: dict, root: str = "."):
    if not cand:
        return
    p = os.path.join(root, "data_store", "paper", "candidate_v2_log.csv")
    os.makedirs(os.path.dirname(p), exist_ok=True)
    row = pd.DataFrame([{"bar": cand["bar"], "market": m, "size_today": cand["size_today_rule"], "size_v2": cand["size_v2"],
                         "bear_confirmed": cand["bear_confirmed"], "risk": " | ".join(cand["risk_warnings"]),
                         "relief": " | ".join(cand["relief_warnings"]), "logged_at": pd.Timestamp.now().isoformat(timespec="seconds")}])
    old = pd.read_csv(p) if os.path.exists(p) else pd.DataFrame()
    if len(old):
        old = old[~((old["bar"] == cand["bar"]) & (old["market"] == m))]
    pd.concat([old, row]).to_csv(p, index=False)
