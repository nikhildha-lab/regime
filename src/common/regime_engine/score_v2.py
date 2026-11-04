"""
Regime score v2 (2026-10-17): DIRECTION and CHARACTER scored separately, per timeframe, against each instrument's
OWN history (rolling ~1 year), so the same code works for Nifty, Bank Nifty or a stock.

  direction  -100 (strong bear) .. +100 (strong bull) = equal-weight mean of signed feature scores x 100
  trend      0 .. 100  how trending (efficiency ratio + ADX, each vs own history)
  vol        0 .. 100  how volatile (realised vol, + India VIX on daily, vs own history)
  label      Trending up / Trending down / Range / Volatile / Mixed, switched only after it holds K bars (hysteresis)

Feature score = 2*Phi(z) - 1, z = (x - rolling mean) / rolling std over ~1 year of that timeframe's bars (fast stand-in
for a percentile; clipped to [-1, 1]). Equal weights inside a timeframe: our earlier tests showed fitted weights did
not beat equal weights out of sample. Which timeframes may be TRUSTED is decided by scripts/research/test_score_v2.py.

Timeframe inputs (shorter = price / volume / flow; longer = more macro):
  5m   momentum (6 bars), distance from session VWAP (TWAP when volume is missing)            [+ order flow: live context only]
  15m  EMA20 vs EMA50, RSI14 - 50, position in the opening range
  75m  ADX x DI sign, swing structure (higher highs / lows), EMA20 slope        (75m = 5 equal bars per NSE session)
  1D   close vs EMA50 / EMA200, 20-day return, breadth (% F&O / NSE stocks above 50-day), India VIX (inverse)
  1W   26-week return, close vs 40-week average, US 10Y 4-week change (inverse), USD/INR 4-week change (inverse),
       crude oil 4-week change (inverse), S&P 500 13-week return
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

LOOKBACK = {"5m": 18900, "15m": 6300, "75m": 1260, "1D": 252, "1W": 52}
HYST = {"5m": 3, "15m": 3, "75m": 3, "1D": 2, "1W": 2}
TF_WEIGHT = {"5m": 0.10, "15m": 0.15, "75m": 0.20, "1D": 0.35, "1W": 0.20}
LABEL_RULES = dict(vol_hi=80, trend_hi=55, trend_lo=45, dir_trend=25, dir_range=20)


def zscore(x: pd.Series, win: int) -> pd.Series:
    m = x.rolling(win, min_periods=max(20, win // 4)).mean()
    s = x.rolling(win, min_periods=max(20, win // 4)).std()
    return (x - m) / s.replace(0, np.nan)


def sc(x: pd.Series, win: int, sign: float = 1.0) -> pd.Series:
    z = zscore(x, win).clip(-4, 4)
    phi = 0.5 * (1 + z.apply(lambda v: math.erf(v / math.sqrt(2)) if pd.notna(v) else np.nan))
    return sign * (2 * phi - 1)


def pct100(x: pd.Series, win: int) -> pd.Series:
    return (sc(x, win) + 1) * 50


def _adx(h, l, c, n=14):
    up, dn = h.diff(), -l.diff()
    pdm = up.where((up > dn) & (up > 0), 0.0)
    ndm = dn.where((dn > up) & (dn > 0), 0.0)
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / n, adjust=False).mean()
    pdi = 100 * pdm.ewm(alpha=1 / n, adjust=False).mean() / atr
    ndi = 100 * ndm.ewm(alpha=1 / n, adjust=False).mean() / atr
    dx = 100 * (pdi - ndi).abs() / (pdi + ndi).replace(0, np.nan)
    return dx.ewm(alpha=1 / n, adjust=False).mean(), pdi, ndi


def _er(c, n=20):
    return (c - c.shift(n)).abs() / c.diff().abs().rolling(n).sum().replace(0, np.nan)


def _rsi(c, n=14):
    d = c.diff()
    u, v = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean(), (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + u / v.replace(0, np.nan))


def _session(idx):
    return pd.Series(idx.normalize() if idx.tz is None else idx.tz_localize(None).normalize(), index=idx)


def features(tf: str, b: pd.DataFrame, ext: dict | None = None) -> tuple[pd.DataFrame, pd.Series, pd.Series]:
    """Returns (signed feature scores, trend 0-100, vol 0-100). b: open/high/low/close[/volume] for that timeframe."""
    ext = ext or {}
    w = LOOKBACK[tf]
    o, h, l, c = b["open"], b["high"], b["low"], b["close"]
    F = pd.DataFrame(index=b.index)
    lr = np.log(c / c.shift())
    if tf == "5m":
        F["momentum 30 min"] = sc(np.log(c / c.shift(6)), w)
        day = _session(b.index)
        vol = b["volume"] if "volume" in b and b["volume"].fillna(0).sum() > 0 else pd.Series(1.0, index=b.index)
        tp = (h + l + c) / 3
        vwap = (tp * vol).groupby(day).cumsum() / vol.groupby(day).cumsum()
        F["vs session VWAP"] = sc(c / vwap - 1, w)
    elif tf == "15m":
        e20, e50 = c.ewm(span=20, adjust=False).mean(), c.ewm(span=50, adjust=False).mean()
        F["EMA20 vs EMA50"] = sc(e20 / e50 - 1, w)
        F["RSI14"] = sc(_rsi(c) - 50, w)
        day = _session(b.index)
        orh = h.groupby(day).transform(lambda s: s.iloc[:2].max())     # first 30 minutes
        orl = l.groupby(day).transform(lambda s: s.iloc[:2].min())
        pos = (c - (orh + orl) / 2) / (orh - orl).replace(0, np.nan)
        F["vs opening range"] = sc(pos.clip(-5, 5), w)
    elif tf == "75m":
        adx, pdi, ndi = _adx(h, l, c)
        F["ADX x direction"] = sc(adx * np.sign(pdi - ndi), w)
        hh = (h > h.rolling(10).max().shift(1)).astype(int).rolling(10).sum()
        ll = (l < l.rolling(10).min().shift(1)).astype(int).rolling(10).sum()
        F["swing structure"] = sc((hh - ll).astype(float), w)
        e20 = c.ewm(span=20, adjust=False).mean()
        F["EMA20 slope"] = sc(e20 / e20.shift(5) - 1, w)
    elif tf == "1D":
        F["vs EMA50"] = sc(c / c.ewm(span=50, adjust=False).mean() - 1, w)
        F["vs EMA200"] = sc(c / c.ewm(span=200, adjust=False).mean() - 1, w)
        F["20-day return"] = sc(c / c.shift(20) - 1, w)
        if ext.get("breadth") is not None:
            F["breadth (% above 50-day)"] = sc(ext["breadth"].reindex(b.index).ffill(limit=3), w)
        if ext.get("vix") is not None:
            F["India VIX (inverse)"] = sc(ext["vix"].reindex(b.index).ffill(limit=3), w, -1)
    elif tf == "1W":
        F["26-week return"] = sc(c / c.shift(26) - 1, w)
        F["vs 40-week average"] = sc(c / c.rolling(40, min_periods=30).mean() - 1, w)
        for key, name, sign, n in (("us10y", "US 10Y 4-week change (inverse)", -1, 4), ("usdinr", "USD/INR 4-week change (inverse)", -1, 4),
                                   ("crude", "crude oil 4-week change (inverse)", -1, 4), ("spx", "S&P 500 13-week return", 1, 13)):
            s = ext.get(key)
            if s is not None:
                s = s.reindex(b.index, method="ffill")
                chg = (s - s.shift(n)) if key == "us10y" else (s / s.shift(n) - 1)
                F[name] = sc(chg, w, sign)
    adx = _adx(h, l, c)[0]
    trend = (pct100(_er(c), w) + pct100(adx, w)) / 2
    rv = lr.rolling(20, min_periods=10).std()
    vol = pct100(rv, w)
    if tf == "1D" and ext.get("vix") is not None:
        vol = (vol + pct100(ext["vix"].reindex(b.index).ffill(limit=3), w)) / 2
    return F, trend, vol


def raw_label(d, tr, v):
    r = LABEL_RULES
    if pd.isna(d) or pd.isna(tr) or pd.isna(v):
        return None
    if v >= r["vol_hi"]:
        return "Volatile"
    if tr >= r["trend_hi"] and d >= r["dir_trend"]:
        return "Trending up"
    if tr >= r["trend_hi"] and d <= -r["dir_trend"]:
        return "Trending down"
    if tr <= r["trend_lo"] and abs(d) < r["dir_range"]:
        return "Range"
    return "Mixed"


def hysteresis(raw: pd.Series, k: int) -> pd.Series:
    out, cur, cand, n = [], None, None, 0
    for x in raw:
        if x is None:
            out.append(cur)
            continue
        if cur is None:
            cur = x
        elif x != cur:
            if x == cand:
                n += 1
            else:
                cand, n = x, 1
            if n >= k:
                cur, cand, n = x, None, 0
        else:
            cand, n = None, 0
        out.append(cur)
    return pd.Series(out, index=raw.index)


def score(tf: str, b: pd.DataFrame, ext: dict | None = None) -> pd.DataFrame:
    F, tr, v = features(tf, b, ext)
    d = 100 * F.mean(axis=1, skipna=True)
    raw = pd.Series([raw_label(a, x, y) for a, x, y in zip(d, tr, v)], index=b.index)
    out = pd.DataFrame({"direction": d, "trend": tr, "vol": v, "raw": raw, "label": hysteresis(raw, HYST[tf])}, index=b.index)
    for col in F.columns:
        out["f:" + col] = F[col]
    return out


def reasons(row: pd.Series, n=3) -> list[str]:
    fs = {k[2:]: v for k, v in row.items() if str(k).startswith("f:") and pd.notna(v)}
    top = sorted(fs.items(), key=lambda kv: -abs(kv[1]))[:n]
    return [f"{k} {'supports up' if v > 0 else 'pushes down'} ({v:+.2f})" for k, v in top]


def alignment(rows: dict) -> dict:
    """rows: tf -> {"direction", "label"}. Weighted direction, share of timeframes agreeing, confidence 0-100."""
    ws = {tf: TF_WEIGHT[tf] for tf, r in rows.items() if r and r.get("direction") is not None}
    if not ws:
        return {"direction": None, "confidence": None, "agree": None}
    tot = sum(ws.values())
    d = sum(rows[tf]["direction"] * w for tf, w in ws.items()) / tot
    sgn = np.sign(d) if abs(d) >= 5 else 0
    agree = sum(w for tf, w in ws.items() if sgn and np.sign(rows[tf]["direction"]) == sgn) / tot if sgn else 0.0
    conf = round(min(100.0, abs(d) * 1.5) * agree)
    word = "aligned" if agree >= 0.75 else "mostly aligned" if agree >= 0.55 else "conflicting"
    return {"direction": round(d, 1), "agree_pct": round(100 * agree), "confidence": conf, "word": word}
