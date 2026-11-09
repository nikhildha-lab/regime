"""
Regime for ANY market the strategies trade (not just NIFTY).

Each market gets the same DESCRIPTIVE regime the NIFTY engine uses (trend group from the four
price features + realized-vol state -> one of 6 regimes) plus the 7-indicator panel trend, on
DAILY bars. Every bar carries `available_at` (when its close is known) so tagging is point-in-time.

Data (data_store/market_data, Yahoo snapshots 2026-10-03/04; see README there):
  NIFTY, BANKNIFTY : NSE near-month futures (daily)
  BTC, ETH, SOL    : BTC-USD / ETH-USD / SOL-USD daily (UTC day; close known at 00:00 UTC next day)
  GOLD             : GC=F gold futures        SILVER: SI=F    COPPER: HG=F    CRUDE: CL=F
  ES, NQ           : S&P 500 / Nasdaq-100 futures            US_STOCKS: SPY (proxy for US stock books)
  ALUMINUM: ALI=F aluminium futures    NATGAS: NG=F natural gas futures (added 2026-10-24, once history is in)
"""
from __future__ import annotations

import os
from functools import lru_cache

import numpy as np
import pandas as pd

from src.common.regime_engine import data as D
from src.common.regime_engine.engine import describe_state
from src.common.regime_engine.features import price_features as PF
from src.common.regime_engine.features import indicator_panel as IP

CRYPTO_FILE = f"{D.MARKET_DIR}/synaptic_crypto_gold.csv"
US_FILE = f"{D.MARKET_DIR}/synaptic_us_commod.csv"
YAHOO_SYMBOL = {"BTC": "BTC-USD", "ETH": "ETH-USD", "SOL": "SOL-USD", "GOLD": "GC=F", "PAXG": "PAXG-USD",
                "SILVER": "SI=F", "COPPER": "HG=F", "CRUDE": "CL=F", "ES": "ES=F", "NQ": "NQ=F", "US_STOCKS": "SPY",
                "ALUMINUM": "ALI=F", "NATGAS": "NG=F",
                # 2026-11-03 (Nikhil): six more coins on the Crypto tab (Yahoo daily / 1h / 5m-30m, like BTC / ETH / SOL)
                "BNB": "BNB-USD", "XRP": "XRP-USD", "UNI": "UNI7083-USD", "ZEC": "ZEC-USD", "LINK": "LINK-USD", "DASH": "DASH-USD", "AVAX": "AVAX-USD",
                "SENSEX": "^BSESN"}                                   # 2026-11-04 (Nikhil): Sensex, third Indian index (Yahoo index bars)
EXTRA_COINS = ["BNB", "XRP", "UNI", "ZEC", "LINK", "DASH", "AVAX"]     # 2026-11-09 (Nikhil): + AVAX
CRYPTO = {"BTC", "ETH", "SOL", *EXTRA_COINS}
MARKETS = ["NIFTY", "BANKNIFTY", "BTC", "ETH", "SOL", "GOLD", "SILVER", "COPPER", "CRUDE", "ES", "NQ", "US_STOCKS"]


def _has_daily(sym: str, min_rows: int = 300, fname: str = "synaptic_us_commod.csv") -> bool:
    """2026-10-24: new markets join only once their daily history is downloaded (enough bars for the regime)."""
    try:
        d = pd.read_csv(D._p(f"{D.MARKET_DIR}/{fname}"), usecols=["symbol", "interval"])
        return int(((d["symbol"] == sym) & (d["interval"] == "1d")).sum()) >= min_rows
    except Exception:
        return False


for _m, _s in (("ALUMINUM", "ALI=F"), ("NATGAS", "NG=F")):           # Metals: aluminium · Commodities: natural gas
    if _has_daily(_s):
        MARKETS.append(_m)
if _has_daily("^BSESN"):                                          # Indian market tab, after Bank Nifty, once its history is in
    MARKETS.insert(MARKETS.index("BANKNIFTY") + 1, "SENSEX")
for _i, _m in enumerate([m for m in EXTRA_COINS if _has_daily(YAHOO_SYMBOL[m], fname="synaptic_crypto_gold.csv")]):
    MARKETS.insert(MARKETS.index("SOL") + 1 + _i, _m)                # after SOL, once its daily history is in
TREND_FEATURES = ["C.6a_ema21_distance", "C.6b_sma5_8_stack", "C.6c_return_z", "C.6d_ema50_slope"]


def _yahoo_daily(sym: str) -> pd.DataFrame:
    path = CRYPTO_FILE if (sym.endswith("-USD") or sym == "GC=F") else US_FILE
    d = pd.read_csv(D._p(path))
    d = d[(d["symbol"] == sym) & (d["interval"] == "1d")].copy()
    ts = pd.to_datetime(d["datetime_utc"])
    crypto = sym.endswith("-USD")
    # crypto daily candles open 00:00 UTC; futures/ETF candles are stamped at the session open
    d["date"] = ts.dt.normalize()
    d = d.drop_duplicates("date", keep="last").set_index("date").sort_index()
    b = d[["open", "high", "low", "close", "volume"]].astype(float)
    if crypto:
        # Yahoo sometimes skips a finished UTC day in the daily series; rebuild missing days from 1h bars
        allr = pd.read_csv(D._p(path))
        h = allr[(allr["symbol"] == sym) & (allr["interval"] == "1h")].copy()
        if len(h):
            h["ts"] = pd.to_datetime(h["datetime_utc"])
            h["date"] = h["ts"].dt.normalize()
            g = h.groupby("date")
            full = g.size() == 24
            hd = pd.DataFrame({"open": g["open"].first(), "high": g["high"].max(), "low": g["low"].min(),
                               "close": g["close"].last(), "volume": g["volume"].sum()})[full]
            missing = hd.index.difference(b.index)
            missing = missing[(missing > b.index.min())]
            if len(missing):
                b = pd.concat([b, hd.loc[missing].astype(float)]).sort_index()
        avail = (b.index + pd.Timedelta(days=1)).tz_localize("UTC")
    elif sym == "^BSESN":   # BSE closes 15:30 IST = 10:00 UTC
        avail = (b.index + pd.Timedelta(hours=10, minutes=1)).tz_localize("UTC")
    else:   # US futures / ETF: settle by ~22:00 UTC same day
        avail = (b.index + pd.Timedelta(hours=22)).tz_localize("UTC")
    b["available_at"] = avail.tz_convert(D.IST)
    b.attrs["source"] = f"csv:{path}#{sym}"
    return b


@lru_cache(maxsize=None)
def market_bars(market: str) -> pd.DataFrame:
    if market in ("NIFTY", "BANKNIFTY"):
        return D.load_futures_daily(market)
    return _yahoo_daily(YAHOO_SYMBOL[market])


@lru_cache(maxsize=None)
def market_states(market: str) -> pd.DataFrame:
    """Per daily bar: trend score, vol state, 6-regime name, panel trend, available_at."""
    b = market_bars(market)
    f = PF.compute_price_features(b["close"])
    trend = f[TREND_FEATURES].mean(axis=1, skipna=True)
    vol = PF.vol_state(b["close"])
    panel = IP.compute_panel(b, daily=True)
    names = [describe_state(t, v if isinstance(v, str) else None) for t, v in zip(trend, vol)]
    out = pd.DataFrame({"trend_score": trend.values, "vol_state": vol.values,
                        "trend_state": [n[0] for n in names], "regime_name": [n[1] for n in names],
                        "panel_trend": panel["panel_trend"].values,
                        "confluence": [f"up {u} / down {d}" for u, d in zip(panel["confluence_up"], panel["confluence_down"])],
                        "close": b["close"].values, "available_at": pd.DatetimeIndex(b["available_at"]).tz_convert(D.IST)},
                       index=b.index)
    return out


def regime_now(market: str, as_of) -> dict:
    s = market_states(market)
    as_of = pd.Timestamp(as_of)
    as_of = as_of if as_of.tzinfo else as_of.tz_localize(D.IST)
    ok = s[s["available_at"] <= as_of]
    if ok.empty or ok["regime_name"].isna().all():
        return {"market": market, "regime_name": None}
    r = ok.dropna(subset=["regime_name"]).iloc[-1]
    return {"market": market, "bar": str(r.name.date()), "regime_name": r["regime_name"], "trend_state": r["trend_state"],
            "vol_state": r["vol_state"], "panel_trend": r["panel_trend"], "confluence": r["confluence"],
            "close": round(float(r["close"]), 2),
            "data_age_days": int((as_of.tz_convert(D.IST).tz_localize(None).normalize() - r.name.normalize()).days)}


def tag_entries(market: str, entry_ts: pd.Series) -> pd.DataFrame:
    """Regime known at each entry: last daily bar whose close is known BEFORE the entry time."""
    s = market_states(market).reset_index(drop=True).sort_values("available_at")
    e = pd.to_datetime(entry_ts)
    if e.dt.tz is None:
        e = e.dt.tz_localize(D.IST)
    left = pd.DataFrame({"entry_ts": e.values, "i": range(len(e))}).sort_values("entry_ts")
    left["entry_ts"] = pd.DatetimeIndex(left["entry_ts"]).tz_convert(D.IST) if pd.DatetimeIndex(left["entry_ts"]).tz is not None else pd.DatetimeIndex(left["entry_ts"]).tz_localize(D.IST)
    m = pd.merge_asof(left, s[["available_at", "regime_name", "trend_state", "panel_trend"]],
                      left_on="entry_ts", right_on="available_at", allow_exact_matches=False,
                      tolerance=pd.Timedelta(days=7))
    return m.sort_values("i").reset_index(drop=True)[["regime_name", "trend_state", "panel_trend"]]


# ---------------------------------------------------------------------------
# All timeframes for every market: 5m / 15m / 30m / 1h / 4h / daily
#   NIFTY, BANKNIFTY : near-month futures (Upstox 5m -> resampled on the 09:15 session grid; NSE daily)
#   others           : Yahoo bars (5m/15m/30m = last 60 days, 1h = 2 years); 4h = 1h grouped into
#                      UTC 00/04/08/12/16/20 blocks. Bar timestamps are bar OPEN; a bar is usable from open+length.
# ---------------------------------------------------------------------------
INTRADAY_ALL = f"{D.MARKET_DIR}/synaptic_intraday_all.csv"
TIMEFRAMES = ["5m", "15m", "4h", "daily", "weekly", "monthly"]     # 2026-10-17: 30m / 1h dropped, weekly / monthly added
DEEP_INDEX = {"NIFTY": "^NSEI", "BANKNIFTY": "^NSEBANK", "SENSEX": "^BSESN"}
TF_MIN = {"5m": 5, "15m": 15, "30m": 30, "1h": 60, "4h": 240}


@lru_cache(maxsize=None)
def _yahoo_intraday_all() -> pd.DataFrame:
    parts = []
    for path in (INTRADAY_ALL, CRYPTO_FILE):
        d = pd.read_csv(D._p(path))
        parts.append(d[d["interval"] != "1d"])
    d = pd.concat(parts, ignore_index=True)
    d["ts"] = pd.to_datetime(d["datetime_utc"]).dt.tz_localize("UTC")
    return d


def _yahoo_bars(sym: str, tf: str) -> pd.DataFrame:
    d = _yahoo_intraday_all()
    iv = "1h" if tf == "4h" else tf
    d = d[(d["symbol"] == sym) & (d["interval"] == iv)].drop_duplicates("ts", keep="last").set_index("ts").sort_index()
    b = d[["open", "high", "low", "close", "volume"]].astype(float)
    if tf == "4h":
        lab = b.index.floor("4h")
        g = b.groupby(lab)
        b = pd.DataFrame({"open": g["open"].first(), "high": g["high"].max(), "low": g["low"].min(),
                          "close": g["close"].last(), "volume": g["volume"].sum()})
        last_open = d.index.to_series().groupby(lab).max()
        b["available_at"] = (last_open + pd.Timedelta(hours=1, minutes=1)).reindex(b.index)
    else:
        b["available_at"] = b.index + pd.Timedelta(minutes=TF_MIN[tf] + 1)
    b["available_at"] = pd.DatetimeIndex(b["available_at"]).tz_convert(D.IST)
    b.index = b.index.tz_convert(D.IST)
    return b


def _long_daily(market: str) -> pd.DataFrame:
    """Longest daily history for weekly / monthly bars (Nifty / Bank Nifty index from 2007 when available)."""
    deep = D._p("data_store/market_data/yahoo_daily_deep.csv") if hasattr(D, "_p") else "data_store/market_data/yahoo_daily_deep.csv"
    if market in DEEP_INDEX and os.path.exists(deep):
        d = pd.read_csv(deep)
        d = d[d["symbol"] == DEEP_INDEX[market]].copy()
        d["date"] = pd.to_datetime(d["date"])
        b = d.drop_duplicates("date", keep="last").set_index("date").sort_index()[["open", "high", "low", "close", "volume"]]
        b = b[b["close"] > 0]
        if len(b) > 300:                         # 2026-11-04: Sensex long history arrives later - use its own daily bars until then
            b.index = b.index.tz_localize(D.IST)
            return b
    b = market_bars(market)[["open", "high", "low", "close", "volume"]].copy()
    if b.index.tz is None:
        b.index = b.index.tz_localize(D.IST)
    return b


def _higher_bars(market: str, tf: str) -> pd.DataFrame:
    """Completed weekly (Fri) / monthly bars; available only after the period has closed."""
    d = _long_daily(market)
    rule = "W-FRI" if tf == "weekly" else "ME"
    naive = d.tz_localize(None) if d.index.tz is not None else d
    b = naive.resample(rule).agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}).dropna(subset=["close"])
    close_t = pd.Timedelta(hours=15, minutes=31) if market in DEEP_INDEX else pd.Timedelta(hours=23, minutes=59)
    b["available_at"] = (b.index.normalize() + close_t).tz_localize(D.IST)
    b.index = b.index.tz_localize(D.IST)
    return b


@lru_cache(maxsize=None)
def market_bars_tf(market: str, tf: str) -> pd.DataFrame:
    if tf == "daily":
        return market_bars(market)
    if tf in ("weekly", "monthly"):
        return _higher_bars(market, tf)
    if market in ("NIFTY", "BANKNIFTY"):
        return D.load_futures_bars(tf, market)
    return _yahoo_bars(YAHOO_SYMBOL[market], tf)


@lru_cache(maxsize=None)
def market_states_tf(market: str, tf: str) -> pd.DataFrame:
    if tf == "daily":
        s = market_states(market).copy()
        p = IP.compute_panel(market_bars(market), daily=True)
    else:
        b = market_bars_tf(market, tf)
        f = PF.compute_price_features(b["close"])
        trend = f[TREND_FEATURES].mean(axis=1, skipna=True)
        vol = PF.vol_state(b["close"])
        p = IP.compute_panel(b, daily=tf in ("weekly", "monthly"))
        names = [describe_state(t, v if isinstance(v, str) else None) for t, v in zip(trend, vol)]
        s = pd.DataFrame({"trend_score": trend.values, "vol_state": vol.values,
                          "trend_state": [n[0] for n in names], "regime_name": [n[1] for n in names],
                          "panel_trend": p["panel_trend"].values, "close": b["close"].values,
                          "available_at": pd.DatetimeIndex(b["available_at"]).tz_convert(D.IST)}, index=b.index)
    s["up"] = p["confluence_up"].values
    s["down"] = p["confluence_down"].values
    s["ichimoku"] = p["T.4_ichimoku"].values
    s["n_ind"] = p["indicators_available"].values
    for col in IP.FEATURES:
        s[col] = p[col].values
    return s


def mtf_table(market: str, as_of, max_age_days: int = 5) -> dict:
    """TF | regime | trend | wait zone for one market, plus the summary row (same rules as the NIFTY engine)."""
    as_of = pd.Timestamp(as_of)
    as_of = as_of if as_of.tzinfo else as_of.tz_localize(D.IST)
    rows = []
    for tf in TIMEFRAMES:
        try:
            s = market_states_tf(market, tf)
        except Exception as e:                                   # missing file / symbol
            rows.append({"tf": tf, "available": False, "note": f"no data ({type(e).__name__})"})
            continue
        ok = s[s["available_at"] <= as_of]
        age_ok = {"weekly": 10, "monthly": 35}.get(tf, max_age_days)
        if ok.empty or (as_of - ok["available_at"].iloc[-1]) > pd.Timedelta(days=age_ok):
            rows.append({"tf": tf, "available": False, "note": "no recent bars"})
            continue
        r = ok.iloc[-1]
        rows.append({"tf": tf, "available": True, "bar": str(r.name), "regime": r["regime_name"],
                     "trend": r["panel_trend"] if isinstance(r["panel_trend"], str) else None,
                     "up": int(r["up"]), "down": int(r["down"]), "ichimoku": r["ichimoku"],
                     "indicators": {k.split("_", 1)[1]: IP.LABELS[k].get(int(r[k]), "Mixed") if pd.notna(r[k]) else "n/a"
                                    for k in IP.FEATURES},
                     "close": round(float(r["close"]), 4)})
    live = [r for r in rows if r["available"]]
    for i, r in enumerate(live):
        higher = live[i + 1]["trend"] if i + 1 < len(live) else None
        r["wait_zone"] = "Active" if IP.wait_zone(r["trend"], r["up"], r["down"], r["ichimoku"], higher) else "Inactive"
    ups = sum(r["trend"] == "Up" for r in live)
    downs = sum(r["trend"] == "Down" for r in live)
    active = sum(r.get("wait_zone") == "Active" for r in live)
    summary = {"trend": "Up" if ups > downs else "Down" if downs > ups else "Flat",
               "wait": "ON" if live and active * 2 >= len(live) else "OFF",
               "votes": f"up {ups} / down {downs} of {len(live)} timeframes"}
    return {"market": market, "as_of": str(as_of), "rows": rows, "summary": summary}
