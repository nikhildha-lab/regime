"""
Regime score v2 for the US and crypto desks (2026-10-18). Same method as score_v2.py (India): direction (-100..+100) and
character (trend 0-100, volatility 0-100) scored separately, each input against the instrument's OWN last year, equal
weights, labels held K bars (hysteresis). Only the inputs and timeframes change per market ("market pack").

US     (ES / NQ futures, SPY / QQQ):   5m, 15m (regular session only), 1h, 1D, 1W
CRYPTO (BTC / ETH / SOL perpetuals):   5m, 15m, 4h, 1D, 1W   (24/7, day = 00:00 UTC = 05:30 IST)

ext keys (all optional; a missing input is simply left out of the average):
  US      vix, vix3m (daily), us10y, dxy (weekly), peer (5m close of the other index, ES<->NQ)
  CRYPTO  funding (8h rate), dvol (daily), nq, dxy (weekly), taker_buy (5m taker-buy share of volume)
Which inputs may be trusted is decided by scripts/research/test_score_v2_markets.py (results in score_v2_validation.json).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from . import score_v2 as S

TFS = {"US": ["5m", "15m", "1h", "1D", "1W"], "CRYPTO": ["5m", "15m", "4h", "1D", "1W"]}
LOOKBACK = {"US": {"5m": 19656, "15m": 6552, "1h": 5800, "1D": 252, "1W": 52},
            "CRYPTO": {"5m": 105120, "15m": 35040, "4h": 2190, "1D": 365, "1W": 52}}
HYST = {"5m": 3, "15m": 3, "1h": 3, "4h": 3, "1D": 2, "1W": 2}
TF_WEIGHT = {"5m": 0.10, "15m": 0.15, "1h": 0.20, "4h": 0.20, "1D": 0.35, "1W": 0.20}
TF_NAME = {"5m": "5 min", "15m": "15 min", "1h": "1 hour", "4h": "4 hour", "1D": "Daily", "1W": "Weekly"}
CRYPTO_SESSIONS_UTC = (0, 7, 13.5)           # Asia, Europe, US open (hours UTC)


def _al(s, idx, limit=3):
    """Align an external series to the bar index (last known value, never a future one)."""
    if s is None:
        return None
    s = s.sort_index()
    s = s[~s.index.duplicated(keep="last")]
    return s.reindex(s.index.union(idx)).ffill(limit=None).reindex(idx)


def _crypto_session(idx):
    h = idx.hour + idx.minute / 60
    k = np.select([h >= CRYPTO_SESSIONS_UTC[2], h >= CRYPTO_SESSIONS_UTC[1]], [2, 1], 0)
    return pd.Series(idx.normalize().astype("int64") // 10**9 * 10 + k, index=idx)


def features(market: str, tf: str, b: pd.DataFrame, ext: dict | None = None):
    ext = ext or {}
    w = LOOKBACK[market][tf]
    sc, pct = (lambda x, s=1.0: S.sc(x, w, s)), (lambda x: S.pct100(x, w))
    o, h, l, c = b["open"], b["high"], b["low"], b["close"]
    F = pd.DataFrame(index=b.index)
    lr = np.log(c / c.shift())
    day = S._session(b.index)
    if tf == "5m":
        F["momentum 30 min"] = sc(np.log(c / c.shift(6)))
        vol = b["volume"] if "volume" in b and b["volume"].fillna(0).sum() > 0 else pd.Series(1.0, index=b.index)
        tp = (h + l + c) / 3
        vwap = (tp * vol).groupby(day).cumsum() / vol.groupby(day).cumsum()
        F["vs session VWAP"] = sc(c / vwap - 1)
        if market == "US" and ext.get("peer") is not None:
            p = _al(ext["peer"], b.index)
            F["other index momentum (ES/NQ)"] = sc(np.log(p / p.shift(6)))
        if market == "CRYPTO" and ext.get("taker_buy") is not None:
            F["taker buy share"] = sc(_al(ext["taker_buy"], b.index) - 0.5)
    elif tf == "15m":
        e20, e50 = c.ewm(span=20, adjust=False).mean(), c.ewm(span=50, adjust=False).mean()
        F["EMA20 vs EMA50"] = sc(e20 / e50 - 1)
        F["RSI14"] = sc(S._rsi(c) - 50)
        grp = day if market == "US" else _crypto_session(b.index)
        orh = h.groupby(grp).transform(lambda s: s.iloc[:2].max())
        orl = l.groupby(grp).transform(lambda s: s.iloc[:2].min())
        pos = (c - (orh + orl) / 2) / (orh - orl).replace(0, np.nan)
        F["vs opening range" if market == "US" else "vs session opening range"] = sc(pos.clip(-5, 5))
    elif tf in ("1h", "4h"):
        adx, pdi, ndi = S._adx(h, l, c)
        F["ADX x direction"] = sc(adx * np.sign(pdi - ndi))
        hh = (h > h.rolling(10).max().shift(1)).astype(int).rolling(10).sum()
        ll = (l < l.rolling(10).min().shift(1)).astype(int).rolling(10).sum()
        F["swing structure"] = sc((hh - ll).astype(float))
        e20 = c.ewm(span=20, adjust=False).mean()
        F["EMA20 slope"] = sc(e20 / e20.shift(5) - 1)
        if market == "CRYPTO" and ext.get("funding") is not None:
            F["funding rate (inverse, crowding)"] = sc(_al(ext["funding"], b.index).rolling(6, min_periods=1).mean(), -1)
    elif tf == "1D":
        F["vs EMA50"] = sc(c / c.ewm(span=50, adjust=False).mean() - 1)
        F["vs EMA200"] = sc(c / c.ewm(span=200, adjust=False).mean() - 1)
        F["20-day return"] = sc(c / c.shift(20) - 1)
        if market == "US":
            if ext.get("vix") is not None:
                F["VIX (inverse)"] = sc(_al(ext["vix"], b.index), -1)
            if ext.get("vix") is not None and ext.get("vix3m") is not None:
                F["VIX / VIX3M (inverse)"] = sc(_al(ext["vix"], b.index) / _al(ext["vix3m"], b.index), -1)
        else:
            if ext.get("funding") is not None:
                f = ext["funding"].resample("1D").mean()
                F["funding 7-day (inverse, crowding)"] = sc(_al(f.rolling(7, min_periods=3).mean(), b.index), -1)
            if ext.get("dvol") is not None:
                F["DVOL (inverse)"] = sc(_al(ext["dvol"], b.index), -1)
    elif tf == "1W":
        F["26-week return"] = sc(c / c.shift(26) - 1)
        F["vs 40-week average"] = sc(c / c.rolling(40, min_periods=30).mean() - 1)
        if market == "US" and ext.get("us10y") is not None:
            y = _al(ext["us10y"], b.index)
            F["US 10Y 4-week change (inverse)"] = sc(y - y.shift(4), -1)
        if ext.get("dxy") is not None:
            d = _al(ext["dxy"], b.index)
            F["dollar 4-week change (inverse)"] = sc(d / d.shift(4) - 1, -1)
        if market == "CRYPTO" and ext.get("nq") is not None:
            q = _al(ext["nq"], b.index)
            F["Nasdaq 13-week return"] = sc(q / q.shift(13) - 1)
    adx = S._adx(h, l, c)[0]
    trend = (pct(S._er(c)) + pct(adx)) / 2
    vol = pct(lr.rolling(20, min_periods=10).std())
    if tf == "1D":
        g = ext.get("vix") if market == "US" else ext.get("dvol")
        if g is not None:
            vol = (vol + pct(_al(g, b.index))) / 2
    return F, trend, vol


def score(market: str, tf: str, b: pd.DataFrame, ext: dict | None = None) -> pd.DataFrame:
    F, tr, v = features(market, tf, b, ext)
    d = 100 * F.mean(axis=1, skipna=True)
    raw = pd.Series([S.raw_label(a, x, y) for a, x, y in zip(d, tr, v)], index=b.index)
    out = pd.DataFrame({"direction": d, "trend": tr, "vol": v, "raw": raw, "label": S.hysteresis(raw, HYST[tf])}, index=b.index)
    for col in F.columns:
        out["f:" + col] = F[col]
    return out


def alignment(rows: dict) -> dict:
    ws = {tf: TF_WEIGHT[tf] for tf, r in rows.items() if r and r.get("direction") is not None}
    if not ws:
        return {"direction": None, "confidence": None, "agree_pct": None, "word": None}
    tot = sum(ws.values())
    d = sum(rows[tf]["direction"] * w for tf, w in ws.items()) / tot
    sgn = np.sign(d) if abs(d) >= 5 else 0
    agree = sum(w for tf, w in ws.items() if sgn and np.sign(rows[tf]["direction"]) == sgn) / tot if sgn else 0.0
    return {"direction": round(float(d), 1), "agree_pct": round(100 * agree), "confidence": round(min(100.0, abs(d) * 1.5) * agree),
            "word": "aligned" if agree >= 0.75 else "mostly aligned" if agree >= 0.55 else "conflicting"}
