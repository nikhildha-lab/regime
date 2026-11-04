"""
World-news features from GDELT (daily tone and share of coverage per theme, Jan 2017 ->).

A day's numbers are used only from 01:00 UTC the next day. All z-scores use each series' own past year.
"""
from __future__ import annotations

import os

import numpy as np
import pandas as pd

from src.common.regime_engine import data as D

GD = "data_store/market_data/gdelt_tone.csv"
STRESS = ["conflict", "recession", "banking_stress", "market_crash", "tariffs"]
INDIA = ["india_markets", "india_economy", "rbi", "fpi"]
NAMES = {"india_markets": "Indian markets", "india_economy": "Indian economy", "rbi": "RBI", "fpi": "Foreign investors (FPI)",
         "fed": "Federal Reserve", "us_stocks": "Wall Street", "inflation": "Inflation", "recession": "Recession",
         "oil": "Oil / OPEC", "conflict": "War and conflict", "tariffs": "Tariffs / trade war", "crypto": "Crypto",
         "gold": "Gold", "china": "China economy", "banking_stress": "Banking stress", "india_geo": "India border tensions",
         "market_crash": "Market crash talk", "ai_tech": "AI / tech stocks"}


def z(s, win=252, minp=60):
    return (s - s.rolling(win, min_periods=minp).mean()) / s.rolling(win, min_periods=minp).std()


def load():
    p = D._p(GD)
    if not os.path.exists(p):
        return None
    g = pd.read_csv(p, parse_dates=["date"])
    tone = g.pivot_table(index="date", columns="theme", values="tone").sort_index()
    vol = g.pivot_table(index="date", columns="theme", values="volume").sort_index()
    return tone, vol


def features() -> pd.DataFrame | None:
    """Indexed by the date the value becomes usable (the day after the news day)."""
    tv = load()
    if tv is None:
        return None
    tone, vol = tv
    f = {}
    tone = tone.dropna(axis=1, how="all")
    vol = vol.dropna(axis=1, how="all")
    for t in tone.columns:
        t5 = tone[t].rolling(5, min_periods=3).mean()
        f[f"{t}: tone"] = z(t5)
        f[f"{t}: tone change"] = z(t5 - tone[t].rolling(20, min_periods=10).mean().shift(5))
    for t in vol.columns:
        f[f"{t}: attention"] = z(vol[t].rolling(5, min_periods=3).mean())
    st = [c for c in STRESS if c in tone and c in vol]
    if st:
        f["GLOBAL STRESS"] = z(vol[st].rolling(5, min_periods=3).mean().apply(z).mean(axis=1)
                              - tone[st].rolling(5, min_periods=3).mean().apply(z).mean(axis=1))
    ind = [c for c in INDIA if c in tone]
    if ind:
        f["INDIA MOOD"] = z(tone[ind].rolling(5, min_periods=3).mean().apply(z).mean(axis=1))
    out = pd.DataFrame(f)
    out.index = out.index + pd.Timedelta(days=1)
    return out


def now(as_of) -> dict | None:
    f = features()
    if f is None:
        return None
    t = pd.Timestamp(as_of)
    t = (t.tz_convert("UTC") if t.tzinfo else t.tz_localize(D.IST).tz_convert("UTC")).tz_localize(None)
    f = f[f.index + pd.Timedelta(hours=1) <= t]
    if f.empty:
        return None
    r = f.iloc[-1]
    themes = []
    for k, nm in NAMES.items():
        if f"{k}: tone" in r or f"{k}: attention" in r:
            themes.append({"key": k, "name": nm, "tone_z": _c(r.get(f"{k}: tone")), "attention_z": _c(r.get(f"{k}: attention")),
                           "tone_change_z": _c(r.get(f"{k}: tone change"))})
    g = f["GLOBAL STRESS"].dropna() if "GLOBAL STRESS" in f else pd.Series(dtype=float)
    tv = load()
    st = [NAMES.get(c, c) for c in STRESS if tv is not None and c in tv[0].dropna(axis=1, how="all") and c in tv[1].dropna(axis=1, how="all")]
    return {"news_day": str((f.index[-1] - pd.Timedelta(days=1)).date()), "global_stress": _c(r.get("GLOBAL STRESS")), "stress_themes": st,
            "global_stress_pct_1y": None if g.empty else round(float((g.iloc[-252:] <= g.iloc[-1]).mean() * 100)),
            "india_mood": _c(r.get("INDIA MOOD")), "themes": themes,
            "stress_series": [{"date": str(d.date()), "v": _c(v)} for d, v in g.iloc[-90:].items()]}


def _c(v):
    return None if v is None or (isinstance(v, float) and np.isnan(v)) else round(float(v), 2)
