"""DI FILTER - do not enter against a trending 5-minute DI (2026-11-09, Nikhil: "which strategy uses ADX filter when entering .. ok update the
logic, don't change the name").

Rule (fixed): ADX(14, Wilder) on 5-min bars > 25 AND the DI against the trade (long with -DI > +DI, short with +DI > -DI) -> skip the entry.
ADX <= 25 (no trend) or DI with the trade -> allowed. Based on the 8 Nov DOM Stocks filter test (312 trades): ADX > 25 against the DI
was the worst group (-0.33 R), with the DI -0.10 R, all trades -0.18 R. Untested on the other rules until their forward records say.

Used by: app/domcards.py (DOM Index v4 entries), app/papertrades._touchv2 (Touch v2 view of the touch cards), app/stockorb.py (ORB entries).
Index bars: near-month futures 1-min (data_store/market_data/nifty_futures_1m_upstox.csv, refreshed by the app) -> 5-min, today + the
previous session for warm-up, only bars closed before `when`. Stocks: the caller passes its own 5-min bars (previous two sessions + today).
"""
from __future__ import annotations

import os
import time

import numpy as np
import pandas as pd

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
FUT1M = os.path.join(ROOT, "data_store", "market_data", "nifty_futures_1m_upstox.csv")
ADX_MIN, N = 25.0, 14
_C: dict = {"fut": None, "mt": 0}


def adx_di(b: pd.DataFrame):
    """Wilder ADX / +DI / -DI on the LAST row of 5-min bars b (open/high/low/close). None if too short."""
    if b is None or len(b) < N * 2 + 2:
        return None
    h, l, c = b.high.values.astype(float), b.low.values.astype(float), b.close.values.astype(float)
    up, dn = h[1:] - h[:-1], l[:-1] - l[1:]
    pdm = np.where((up > dn) & (up > 0), up, 0.0); mdm = np.where((dn > up) & (dn > 0), dn, 0.0)
    tr = np.maximum(h[1:] - l[1:], np.maximum(np.abs(h[1:] - c[:-1]), np.abs(l[1:] - c[:-1])))
    def wild(x):
        s = np.empty(len(x)); s[:] = np.nan; a = x[:N].sum(); s[N - 1] = a
        for i in range(N, len(x)):
            a = a - a / N + x[i]; s[i] = a
        return s
    trn, pn, mn = wild(tr), wild(pdm), wild(mdm)
    with np.errstate(divide="ignore", invalid="ignore"):
        pdi = 100 * pn / trn; mdi = 100 * mn / trn
        dx = 100 * np.abs(pdi - mdi) / (pdi + mdi)
    dx = np.where(np.isfinite(dx), dx, np.nan)
    ok = ~np.isnan(dx)
    if ok.sum() < N:
        return None
    d = dx[ok]; adx = d[:N].mean()
    for v in d[N:]:
        adx = (adx * (N - 1) + v) / N
    return {"adx": round(float(adx), 1), "pdi": round(float(pdi[-1]), 1), "mdi": round(float(mdi[-1]), 1)}


def verdict(side: str, x: dict | None):
    """-> {"ok": bool, "adx", "pdi", "mdi", "note"}; ok when nothing is known (no bars) so a data gap never blocks silently - the note says so"""
    if not x:
        return {"ok": True, "note": "DI unknown (not enough bars) - allowed"}
    against = (x["mdi"] > x["pdi"]) if side == "long" else (x["pdi"] > x["mdi"])
    block = x["adx"] > ADX_MIN and against
    note = f"ADX {x['adx']:.0f} · +DI {x['pdi']:.0f} / -DI {x['mdi']:.0f} · " + ("BLOCKED: trending against the trade" if block else "trend with the trade" if x["adx"] > ADX_MIN and not against else "no trend (ADX <= 25) - allowed")
    return {"ok": not block, "adx": x["adx"], "pdi": x["pdi"], "mdi": x["mdi"], "note": note}


def _fut():
    try:
        mt = os.path.getmtime(FUT1M)
    except OSError:
        return None
    if _C["fut"] is None or mt != _C["mt"]:
        f = pd.read_csv(FUT1M, usecols=["contract", "ts", "open", "high", "low", "close", "volume"])
        f["d"] = f.ts.str[:10]; f["key"] = f.contract.str.split("_").str[0]
        vol = f.groupby(["d", "key", "contract"]).volume.sum().reset_index()
        near = vol.loc[vol.groupby(["d", "key"]).volume.idxmax()][["d", "key", "contract"]]     # most-traded contract per index per day
        f = f.merge(near, on=["d", "key", "contract"])
        f["ts"] = pd.to_datetime(f.ts.str[:19])
        _C["fut"] = f.sort_values("ts"); _C["mt"] = mt
    return _C["fut"]


def index_bars5(key: str, when: pd.Timestamp) -> pd.DataFrame | None:
    """5-min bars of the near-month future for `key`: previous two sessions + today, closed before `when`."""
    f = _fut()
    if f is None:
        return None
    x = f[(f.key == key) & (f.ts < when.replace(second=0, microsecond=0))]
    if x.empty:
        return None
    days = sorted(x.d.unique())[-3:]                      # today + two sessions of warm-up (a short special session alone is not enough)
    x = x[x.d.isin(days)].set_index("ts")
    b = x.resample("5min", origin="start_day", offset="9h15min", label="left", closed="left").agg(open=("open", "first"), high=("high", "max"), low=("low", "min"), close=("close", "last")).dropna()
    b = b[b.index + pd.Timedelta(minutes=5) <= when.replace(second=0, microsecond=0)]      # closed 5-min bars only
    return b


def check_index(key: str, side: str, when=None) -> dict:
    when = pd.Timestamp(when) if when is not None else pd.Timestamp.now()
    try:
        b = index_bars5(key, when)
        v = verdict(side, adx_di(b))
        if b is not None and len(b):
            age = (when - (b.index[-1] + pd.Timedelta(minutes=5))).total_seconds() / 60
            v["bar_at"] = str(b.index[-1])[11:16]
            if age > 20:
                v["note"] += f" · futures 1-min file is {age:.0f} min behind"
        return v
    except Exception as e:
        return {"ok": True, "note": f"DI check failed ({type(e).__name__}) - allowed"}


def check_bars(side: str, bars5: pd.DataFrame) -> dict:
    try:
        return verdict(side, adx_di(bars5))
    except Exception as e:
        return {"ok": True, "note": f"DI check failed ({type(e).__name__}) - allowed"}
