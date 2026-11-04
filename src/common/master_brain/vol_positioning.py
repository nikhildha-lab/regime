"""
Build-order step 1: one IMPLIED-VOLATILITY input and one POSITIONING input for every market.

    implied vol   -> state  calm / normal / stressed   (1-year percentile: <30 calm, >80 stressed;
                                                        US: VIX above VIX3M also = stressed)
    positioning   -> crowded long / neutral / crowded short   (+ the raw number)

Inputs and where they come from (all point-in-time: a value is used only after `available_at`):

    market     implied vol                       positioning
    NIFTY      India VIX                         FII index-futures long % (NSE participant OI, 1y percentile)
    BANKNIFTY  India VIX                         FII index-futures long %
    BTC / ETH  Deribit DVOL                      perpetual funding, 7-day average, annualised (Binance)
    SOL        BTC DVOL (proxy, no SOL index)    SOL perpetual funding
    GOLD       GVZ                               CFTC COT: speculators' net long as % of OI (3-year percentile)
    SILVER     GVZ (proxy)                       CFTC COT silver
    COPPER     none (realised only)              CFTC COT copper
    CRUDE      OVX                               CFTC COT WTI
    ES         VIX (+ VIX / VIX3M)               CFTC COT E-mini S&P
    NQ         VXN                               CFTC COT E-mini Nasdaq
    US_STOCKS  VIX (+ VIX / VIX3M)               CFTC COT E-mini S&P

This is DESCRIPTIVE context. None of it changes a recommendation until it passes the walk-forward test.
"""
from __future__ import annotations

import os
from functools import lru_cache

import numpy as np
import pandas as pd

from src.common.regime_engine import data as D

MD = D.MARKET_DIR
IST = D.IST
US_CLOSE_UTC = pd.Timedelta(hours=21, minutes=30)      # 16:15 ET close + margin, works in EDT and EST
IN_CLOSE_UTC = pd.Timedelta(hours=10, minutes=1)       # 15:31 IST


def _csv(name):
    p = D._p(f"{MD}/{name}")
    return pd.read_csv(p) if os.path.exists(p) else None


def _series(values: pd.Series, avail: pd.Series, name: str) -> pd.DataFrame:
    s = pd.DataFrame({"value": values.astype(float).values,
                      "available_at": pd.DatetimeIndex(avail).tz_convert("UTC") if pd.DatetimeIndex(avail).tz
                      else pd.DatetimeIndex(avail).tz_localize("UTC")}, index=values.index)
    s = s.dropna().sort_values("available_at")
    s.attrs["name"] = name
    return s


@lru_cache(maxsize=None)
def yahoo_daily(sym: str) -> pd.DataFrame | None:
    """Daily close of a Yahoo symbol from synaptic_daily.csv or synaptic_daily_extra.csv."""
    for f in ("synaptic_daily.csv", "synaptic_daily_extra.csv"):
        d = _csv(f)
        if d is None or sym not in set(d["symbol"]):
            continue
        d = d[d["symbol"] == sym].copy()
        d["date"] = pd.to_datetime(d["datetime_local"].str[:10])
        d = d.drop_duplicates("date", keep="last").set_index("date").sort_index()
        india = sym in ("^INDIAVIX", "^NSEI", "^NSEBANK", "^CRSLDX", "^NSEMDCP50", "^CNXIT")
        avail = d.index + (IN_CLOSE_UTC if india else US_CLOSE_UTC)
        return _series(d["close"], avail, sym)
    return None


@lru_cache(maxsize=None)
def dvol(ccy: str) -> pd.DataFrame | None:
    d = _csv("crypto_dvol.csv")
    if d is None:
        return None
    d = d[d["currency"] == ccy].copy()
    d["date"] = pd.to_datetime(d["date"])
    d = d.set_index("date").sort_index()
    return _series(d["close"], pd.to_datetime(d["available_at"]), f"{ccy} DVOL")


@lru_cache(maxsize=None)
def funding_annual(coin: str) -> pd.DataFrame | None:
    """7-day mean funding rate, annualised in % (3 payments a day)."""
    d = _csv("crypto_funding.csv")
    if d is None:
        return None
    d = d[d["coin"] == coin].copy()
    d["t"] = pd.to_datetime(d["funding_time"]).dt.tz_localize("UTC")
    d = d.set_index("t").sort_index()
    ann = d["rate"].rolling("7D").mean() * 3 * 365 * 100
    return _series(ann, d.index.to_series(), f"{coin} funding, 7d avg, %/yr")


@lru_cache(maxsize=None)
def cot_net(market: str) -> pd.DataFrame | None:
    d = _csv("cot_legacy.csv")
    if d is None:
        return None
    d = d[d["market"] == market].copy()
    d["date"] = pd.to_datetime(d["report_date"])
    d = d.drop_duplicates("date", keep="last").set_index("date").sort_index()
    net = (d["noncomm_long"] - d["noncomm_short"]) / d["open_interest"] * 100
    return _series(net, pd.to_datetime(d["available_at"]), f"COT {market}: speculators net long, % of OI")


@lru_cache(maxsize=None)
def fii_long_pct() -> pd.DataFrame | None:
    p = D.load_participant_oi()
    f = p[p["client_type"] == "FII"].set_index("date").sort_index()
    v = 100 * f["fut_idx_long"] / (f["fut_idx_long"] + f["fut_idx_short"])
    avail = f.index + pd.Timedelta(hours=14, minutes=30)                 # NSE posts it in the evening (20:00 IST)
    return _series(v, avail, "FII index-futures long %")


IV_SOURCE = {"NIFTY": ("^INDIAVIX", None), "BANKNIFTY": ("^INDIAVIX", None),
             "BTC": ("DVOL:BTC", None), "ETH": ("DVOL:ETH", None), "SOL": ("DVOL:BTC", "BTC DVOL as proxy (no SOL index)"),
             "GOLD": ("^GVZ", None), "SILVER": ("^GVZ", "gold vol (GVZ) as proxy"), "COPPER": (None, "no implied-vol index"),
             "CRUDE": ("^OVX", None), "ES": ("^VIX", None), "NQ": ("^VXN", None), "US_STOCKS": ("^VIX", None),
             "ALUMINUM": (None, "no implied-vol index"), "NATGAS": (None, "no implied-vol index")}
POS_SOURCE = {"NIFTY": "FII", "BANKNIFTY": "FII", "BTC": "FUND:BTC", "ETH": "FUND:ETH", "SOL": "FUND:SOL",
              "GOLD": "COT:GOLD", "SILVER": "COT:SILVER", "COPPER": "COT:COPPER", "CRUDE": "COT:CRUDE",
              "ES": "COT:ES", "NQ": "COT:NQ", "US_STOCKS": "COT:ES", "ALUMINUM": None, "NATGAS": None}
IV_LABEL = {"^INDIAVIX": "India VIX", "^GVZ": "GVZ", "^OVX": "OVX", "^VIX": "VIX", "^VXN": "VXN",
            "DVOL:BTC": "BTC DVOL", "DVOL:ETH": "ETH DVOL"}


def _load(key: str):
    if key.startswith("DVOL:"):
        return dvol(key[5:])
    if key.startswith("FUND:"):
        return funding_annual(key[5:])
    if key.startswith("COT:"):
        return cot_net(key[4:])
    if key == "FII":
        return fii_long_pct()
    return yahoo_daily(key)


def _known(s: pd.DataFrame | None, as_of: pd.Timestamp) -> pd.DataFrame | None:
    if s is None or s.empty:
        return None
    k = s[s["available_at"] <= as_of.tz_convert("UTC")]
    return k if len(k) else None


def _pct_rank(hist: pd.Series, window: int) -> float:
    h = hist.iloc[-window:]
    return float((h <= h.iloc[-1]).mean() * 100)


def implied_vol(market: str, as_of) -> dict:
    as_of = pd.Timestamp(as_of)
    as_of = as_of if as_of.tzinfo else as_of.tz_localize(IST)
    key, note = IV_SOURCE.get(market, (None, "no implied-vol index"))
    if key is None:
        return {"available": False, "note": note}
    k = _known(_load(key), as_of)
    if k is None or len(k) < 60:
        return {"available": False, "note": "no data yet"}
    pct = _pct_rank(k["value"], 252)
    state = "Stressed" if pct > 80 else "Calm" if pct < 30 else "Normal"
    out = {"available": True, "source": IV_LABEL.get(key, key), "value": round(float(k["value"].iloc[-1]), 2),
           "pct_1y": round(pct), "state": state, "as_of": str(k.index[-1].date()) if hasattr(k.index[-1], "date") else str(k.index[-1]),
           "change_5": round(float(k["value"].iloc[-1] - k["value"].iloc[-6]), 2) if len(k) > 6 else None, "note": note}
    if key == "^VIX":                                         # term structure: VIX above VIX3M = near-term stress
        v3 = _known(yahoo_daily("^VIX3M"), as_of)
        if v3 is not None:
            ratio = float(k["value"].iloc[-1] / v3["value"].iloc[-1])
            out["vix_vix3m"] = round(ratio, 3)
            if ratio > 1:
                out["state"] = "Stressed"
                out["note"] = "VIX above VIX3M (inverted curve)"
    return out


def positioning(market: str, as_of) -> dict:
    as_of = pd.Timestamp(as_of)
    as_of = as_of if as_of.tzinfo else as_of.tz_localize(IST)
    key = POS_SOURCE.get(market)
    if not key:
        return {"available": False, "note": "no positioning data for this market yet"}
    k = _known(_load(key), as_of)
    if k is None or len(k) < 20:
        return {"available": False, "note": "no data yet"}
    v = float(k["value"].iloc[-1])
    out = {"available": True, "metric": k.attrs.get("name", key) if hasattr(k, "attrs") else key,
           "value": round(v, 2), "as_of": str(pd.Timestamp(k.index[-1]).date())}
    if key.startswith("COT:"):
        pct = _pct_rank(k["value"], 156)                       # 3 years of weekly reports (the classic COT index)
        out.update(pct_3y=round(pct), reading="Crowded long" if pct >= 90 else "Crowded short" if pct <= 10 else "Neutral",
                   metric=f"Speculators net long, % of open interest (CFTC COT, week of {out['as_of']})")
    elif key.startswith("FUND:"):
        # funding >= 25%/yr = longs paying a lot to stay in; below 0 = shorts paying
        out.update(reading="Crowded long" if v >= 25 else "Crowded short" if v < 0 else "Neutral",
                   metric="Perpetual funding, 7-day average, % per year (Binance)")
    else:                                                       # FII long %
        pct = _pct_rank(k["value"], 252)
        out.update(pct_1y=round(pct),
                   reading="FIIs heavily short" if v < 30 or pct <= 10 else "FIIs heavily long" if v > 70 or pct >= 90 else "Neutral",
                   metric="FII long share of index futures, % (NSE participant OI)")
    return out


def fii_cash(as_of, days: int = 5) -> dict:
    d = _csv("nse_fiidii_cash.csv")
    if d is None:
        return {"available": False}
    as_of = pd.Timestamp(as_of)
    as_of = as_of if as_of.tzinfo else as_of.tz_localize(IST)
    d["available_at"] = pd.to_datetime(d["available_at"]).dt.tz_localize("UTC")
    d = d[d["available_at"] <= as_of.tz_convert("UTC")]
    if d.empty:
        return {"available": False}
    piv = d.pivot_table(index="date", columns="category", values="net_cr").sort_index()
    last = piv.iloc[-days:]
    return {"available": True, "days": len(last), "fii_net_cr": round(float(last.get("FII", pd.Series(dtype=float)).sum()), 0),
            "dii_net_cr": round(float(last.get("DII", pd.Series(dtype=float)).sum()), 0), "last_date": str(piv.index[-1])}


def table(markets: list, as_of) -> list:
    return [{"market": m, "implied_vol": implied_vol(m, as_of), "positioning": positioning(m, as_of)} for m in markets]
