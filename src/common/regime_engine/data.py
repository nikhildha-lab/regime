"""
Offline, point-in-time data loaders for the regime engine.

v1 runs on files already in the repo (no network):
  - NIFTY daily closes   backtest/regime/nifty_regime_daily_2023_2026.csv
  - NIFTY 4H closes      backtest/regime/nifty_regime_4h_2023_2026.csv  (bar OPEN times 09:15 / 13:15 IST)
  - Stock panel (breadth) stocks_data.csv  (daily OHLCV, ~229 symbols, 2024-03 onward)

Every bar carries `available_at`: the earliest moment its close is known.
Readers must only use rows with available_at <= as_of.
TODO (Phase 0): replace with live Zerodha/Upstox candles through src/brokers/adapter.
"""
from __future__ import annotations

import os
import numpy as np
import pandas as pd

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
IST = "Asia/Kolkata"

DAILY_CSV = "backtest/regime/nifty_regime_daily_2023_2026.csv"
H4_CSV = "backtest/regime/nifty_regime_4h_2023_2026.csv"
STOCKS_CSV = "stocks_data.csv"
STOCKS_EOD = "data_store/market_data/nse_stocks_eod.csv"


def _p(rel: str) -> str:
    return os.path.join(REPO_ROOT, rel)


def load_nifty_daily() -> pd.DataFrame:
    d = pd.read_csv(_p(DAILY_CSV), usecols=["date", "close"])
    d["date"] = pd.to_datetime(d["date"])
    d = d.drop_duplicates("date").sort_values("date").set_index("date")
    d["available_at"] = (d.index + pd.Timedelta(hours=15, minutes=30)).tz_localize(IST)
    d.attrs["source"] = f"csv:{DAILY_CSV}"
    return d


def load_nifty_4h() -> pd.DataFrame:
    d = pd.read_csv(_p(H4_CSV), usecols=["date", "close"])
    d["date"] = pd.to_datetime(d["date"], utc=True).dt.tz_convert(IST)
    d = d.drop_duplicates("date").sort_values("date").set_index("date")
    # 09:15 bar closes at 13:15; 13:15 bar closes at 15:30 (+1 min settle, same rule as
    # nd_tt_v4_options_v1/backtest/bt_engine/regime.py)
    close_at = [ts.normalize() + (pd.Timedelta(hours=13, minutes=16) if ts.hour < 13 else pd.Timedelta(hours=15, minutes=31))
                for ts in d.index]
    d["available_at"] = pd.DatetimeIndex(close_at)
    d.attrs["source"] = f"csv:{H4_CSV}"
    return d


def load_stock_panel() -> pd.DataFrame:
    path = _p(STOCKS_CSV)
    if not os.path.exists(path):
        return pd.DataFrame(columns=["date", "symbol", "close"])
    d = pd.read_csv(path, usecols=["Date", "Symbol", "Close"])
    d = d.rename(columns={"Date": "date", "Symbol": "symbol", "Close": "close"})
    d["date"] = pd.to_datetime(d["date"], format="%d-%m-%Y", errors="coerce")
    live = _p(STOCKS_EOD)                    # NSE cash bhavcopy closes added by app/updater.py (after 2026-03)
    if os.path.exists(live):
        e = pd.read_csv(live, usecols=["date", "symbol", "close"])
        e["date"] = pd.to_datetime(e["date"])
        d = pd.concat([d, e], ignore_index=True)
    d = d.dropna().drop_duplicates(["date", "symbol"], keep="last").sort_values(["date", "symbol"])
    return d


# ---------------------------------------------------------------------------
# Phase 0 market data (downloaded 2026-10-03 via the desktop browser; see
# data_store/market_data/README.md). Replace with scheduled live fetchers.
# ---------------------------------------------------------------------------
MARKET_DIR = "data_store/market_data"
YAHOO_DAILY = f"{MARKET_DIR}/synaptic_daily.csv"
YAHOO_INTRADAY = f"{MARKET_DIR}/synaptic_intraday.csv"
PARTICIPANT_OI = f"{MARKET_DIR}/nse_participant_oi_2023_2026.csv"
INDEX_FUTURES = f"{MARKET_DIR}/nse_index_futures_2023_2026.csv"


def has_market_data() -> bool:
    return os.path.exists(_p(YAHOO_DAILY))


def load_yahoo_daily(symbol: str) -> pd.DataFrame:
    """Daily OHLC for one Yahoo symbol, indexed by local calendar date (naive)."""
    d = pd.read_csv(_p(YAHOO_DAILY))
    d = d[d["symbol"] == symbol].copy()
    d["date"] = pd.to_datetime(d["datetime_local"].str[:10])
    d = d.drop_duplicates("date", keep="last").set_index("date").sort_index()
    return d[["open", "high", "low", "close", "volume"]].astype(float)


def load_yahoo_intraday(symbol_interval: str) -> pd.DataFrame:
    """Intraday bars (bar OPEN time, IST) for e.g. '^NSEI_1h'."""
    d = pd.read_csv(_p(YAHOO_INTRADAY))
    d = d[d["symbol_interval"] == symbol_interval].copy()
    d["ts"] = pd.to_datetime(d["datetime_local"]).dt.tz_localize(IST)
    d = d.drop_duplicates("ts", keep="last").set_index("ts").sort_index()
    return d[["open", "high", "low", "close", "volume"]].astype(float)


def load_nifty_daily_long() -> pd.DataFrame:
    """NIFTY daily closes 2016-> from Yahoo (falls back to the repo CSV)."""
    if not has_market_data():
        return load_nifty_daily()
    y = load_yahoo_daily("^NSEI")
    d = pd.DataFrame({"close": y["close"]})
    d["available_at"] = (d.index + pd.Timedelta(hours=15, minutes=30)).tz_localize(IST)
    d.attrs["source"] = f"csv:{YAHOO_DAILY}#^NSEI"
    return d


def load_nifty_1h() -> pd.DataFrame:
    y = load_yahoo_intraday("^NSEI_1h")
    d = pd.DataFrame({"close": y["close"]})
    # Yahoo 1h bars open at :15; the last bar of the day (15:15) closes at 15:30
    close_at = [min(ts + pd.Timedelta(hours=1), ts.normalize() + pd.Timedelta(hours=15, minutes=30)) + pd.Timedelta(minutes=1)
                for ts in d.index]
    d["available_at"] = pd.DatetimeIndex(close_at)
    d.attrs["source"] = f"csv:{YAHOO_INTRADAY}#^NSEI_1h"
    return d


def load_participant_oi() -> pd.DataFrame:
    d = pd.read_csv(_p(PARTICIPANT_OI))
    d["date"] = pd.to_datetime(d["date"])
    return d.sort_values(["date", "client_type"])


def load_index_futures() -> pd.DataFrame:
    d = pd.read_csv(_p(INDEX_FUTURES))
    d["date"] = pd.to_datetime(d["date"])
    d["expiry"] = pd.to_datetime(d["expiry"])
    return d.sort_values(["symbol", "date", "expiry"])


# ---------------------------------------------------------------------------
# OHLC bars for every timeframe (indicator panel needs high/low/volume)
# ---------------------------------------------------------------------------
YAHOO_INTRADAY_5M_30M = f"{MARKET_DIR}/synaptic_intraday_5m_30m.csv"
BAR_MINUTES = {"5m": 5, "15m": 15, "30m": 30, "1h": 60}
SESSION_END = pd.Timedelta(hours=15, minutes=30)


def _intraday_file(symbol_interval: str) -> str:
    return YAHOO_INTRADAY_5M_30M if symbol_interval.endswith(("_5m", "_30m")) else YAHOO_INTRADAY


def load_bars(profile: str, symbol: str = "^NSEI") -> pd.DataFrame:
    """OHLCV + available_at for daily / 4h / 1h / 30m / 15m / 5m."""
    if profile == "daily":
        y = load_yahoo_daily(symbol)
        y["available_at"] = (y.index + SESSION_END).tz_localize(IST)
        y.attrs["source"] = f"csv:{YAHOO_DAILY}#{symbol}"
        return y
    if profile == "4h":
        h = load_bars("1h", symbol)
        lab = [ts.normalize() + (pd.Timedelta(hours=9, minutes=15) if ts.hour < 13 else pd.Timedelta(hours=13, minutes=15))
               for ts in h.index]
        g = h.groupby(pd.DatetimeIndex(lab))
        b = pd.DataFrame({"open": g["open"].first(), "high": g["high"].max(), "low": g["low"].min(),
                          "close": g["close"].last(), "volume": g["volume"].sum()})
        b["available_at"] = [ts.normalize() + (pd.Timedelta(hours=13, minutes=16) if ts.hour < 13 else pd.Timedelta(hours=15, minutes=31))
                             for ts in b.index]
        b.attrs["source"] = f"csv:{YAHOO_INTRADAY}#{symbol}_1h->4h"
        return b
    si = f"{symbol}_{profile}"
    path = _intraday_file(si)
    d = pd.read_csv(_p(path))
    d = d[d["symbol_interval"] == si].copy()
    d["ts"] = pd.to_datetime(d["datetime_local"]).dt.tz_localize(IST)
    d = d.drop_duplicates("ts", keep="last").set_index("ts").sort_index()
    b = d[["open", "high", "low", "close", "volume"]].astype(float)
    m = BAR_MINUTES[profile]
    b["available_at"] = [min(ts + pd.Timedelta(minutes=m), ts.normalize() + SESSION_END) + pd.Timedelta(minutes=1) for ts in b.index]
    b.attrs["source"] = f"csv:{path}#{si}"
    return b


# ---------------------------------------------------------------------------
# NIFTY FUTURES as the price source (default). Index (spot) stays available as source="spot".
#   daily   : NSE contract-wise futures (2023-01 ->), continuous NEAR-MONTH, unadjusted (like TradingView NIFTY1!)
#   intraday: Upstox 5-minute futures candles (public API), continuous near-month among the contracts we hold;
#             public API only serves live contracts, so intraday history starts when the oldest live contract
#             listed (2026-07-29 in the 2026-10-03 snapshot). Bars starting at/after 15:30 are post-close and dropped.
# ---------------------------------------------------------------------------
FUTURES_5M = f"{MARKET_DIR}/nifty_futures_5m_upstox.csv"
DEFAULT_SOURCE = "futures"
_FUT_EXPIRY = {"OCT26": "2026-10-27", "NOV26": "2026-11-23", "DEC26": "2026-12-29"}   # seed only; new contracts come from futures_contracts.json (Dhan)
CONTRACTS_JSON = f"{MARKET_DIR}/futures_contracts.json"     # written by app/updater.py (adds new contracts as they list)


def fut_expiry_map() -> dict:
    m = dict(_FUT_EXPIRY)
    p = _p(CONTRACTS_JSON)
    if os.path.exists(p):
        import json
        for c in json.load(open(p)).get("contracts", []):
            m[c["contract"][-5:]] = c["expiry"]
    return m


def load_futures_daily(symbol: str = "NIFTY") -> pd.DataFrame:
    f = load_index_futures()
    f = f[(f["symbol"] == symbol) & (f["expiry"] >= f["date"]) & (f["close"] > 0)]
    near = f.sort_values(["date", "expiry"]).groupby("date").first()
    oi = load_index_futures()
    oi = oi[oi["symbol"] == symbol].groupby("date")["open_interest"].sum()
    b = pd.DataFrame({"open": near["open"], "high": near["high"], "low": near["low"], "close": near["close"],
                      "volume": near["volume_contracts"], "oi_total": oi.reindex(near.index), "expiry": near["expiry"]})
    b = b[(b["open"] > 0) & (b["high"] > 0) & (b["low"] > 0)].astype({"open": float, "high": float, "low": float, "close": float})
    b["available_at"] = (b.index + SESSION_END).tz_localize(IST)
    b.attrs["source"] = f"csv:{INDEX_FUTURES}#{symbol} near-month continuous"
    return b


def load_futures_5m(symbol: str = "NIFTY") -> pd.DataFrame:
    d = pd.read_csv(_p(FUTURES_5M))
    d = d[d["contract"].str.startswith(f"{symbol}_FUT_")].copy()
    d["ts"] = pd.to_datetime(d["ts"]).dt.tz_convert(IST)
    d = d[(d["ts"].dt.hour * 60 + d["ts"].dt.minute) < 15 * 60 + 30]                  # drop post-close bars
    d["expiry"] = pd.to_datetime(d["contract"].str[-5:].map(fut_expiry_map()))
    d["day"] = d["ts"].dt.tz_localize(None).dt.normalize()
    d = d[d["expiry"] >= d["day"]]
    first_exp = d.groupby("day")["expiry"].transform("min")                            # near month among held contracts
    d = d[d["expiry"] == first_exp].drop_duplicates("ts").set_index("ts").sort_index()
    b = d[["open", "high", "low", "close", "volume", "oi"]].astype(float)
    b.attrs["contracts"] = sorted(d["contract"].unique())
    return b


def _resample_session(b5: pd.DataFrame, minutes: int) -> pd.DataFrame:
    """Session-anchored bars (09:15 origin), like NSE charts. 240 -> 09:15-13:15 and 13:15-15:30."""
    t = b5.index
    mins = (t.hour * 60 + t.minute) - (9 * 60 + 15)
    if minutes == 240:
        start = np.where(mins < 240, 0, 240)
    else:
        start = (mins // minutes) * minutes
    lab = t.normalize() + pd.to_timedelta(start + 9 * 60 + 15, unit="m")
    g = b5.groupby(lab)
    out = pd.DataFrame({"open": g["open"].first(), "high": g["high"].max(), "low": g["low"].min(),
                        "close": g["close"].last(), "volume": g["volume"].sum(), "oi": g["oi"].last()})
    end = [min(ts + pd.Timedelta(minutes=minutes), ts.normalize() + SESSION_END) + pd.Timedelta(minutes=1) for ts in out.index]
    out["available_at"] = pd.DatetimeIndex(end)
    return out


def load_futures_bars(profile: str, symbol: str = "NIFTY") -> pd.DataFrame:
    if profile == "daily":
        return load_futures_daily(symbol)
    b5 = load_futures_5m(symbol)
    mins = {"5m": 5, "15m": 15, "30m": 30, "1h": 60, "4h": 240}[profile]
    b = _resample_session(b5, mins)
    b.attrs["source"] = f"csv:{FUTURES_5M}#{symbol} near-month 5m->{profile} ({', '.join(b5.attrs['contracts'])})"
    return b
