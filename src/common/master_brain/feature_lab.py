"""
Feature lab: every data point we can test, as a POINT-IN-TIME series, with a direction decided in advance.

Each feature = DataFrame(value, available_at[UTC]) on its own dates. Kinds:
  dir   directional. sign +1: a high reading is expected to be bullish for the market; -1: bearish
  gate  quality / stress. "high": trade only when the reading is high (above its 1-year median);
        "low": trade only when it is not stressed (below its 1-year 80th percentile)

Directions are textbook expectations written before testing (see THEORY column in the output). They are
not changed after seeing results.
"""
from __future__ import annotations

from functools import lru_cache

import numpy as np
import pandas as pd

from src.common.regime_engine import data as D
from src.common.regime_engine.features import character as CH
from src.common.regime_engine.features.breadth import compute_breadth_features
from src.common.master_brain import markets as M
from src.common.master_brain import vol_positioning as VP
from src.common.master_brain import macro_labels as ML
from src.common.master_brain import correlations as XC

IST = D.IST


# ------------------------------------------------------------------ helpers
def pit(values: pd.Series, avail) -> pd.DataFrame:
    idx = pd.DatetimeIndex(values.index)
    idx = idx.tz_localize(None) if idx.tz is not None else idx
    a = pd.DatetimeIndex(avail)
    a = a.tz_convert("UTC") if a.tz is not None else a.tz_localize("UTC")
    return pd.DataFrame({"value": np.asarray(values, dtype=float), "available_at": a}, index=idx).dropna()


def _vp(df):            # vol_positioning / macro frames already (value, available_at)
    return None if df is None else pit(df["value"], df["available_at"])


@lru_cache(maxsize=None)
def bars(market: str) -> pd.DataFrame:
    if market == "NIFTY":                              # index daily from 2016 (futures daily only from 2023)
        n = D.load_yahoo_daily("^NSEI")
        n = n[n["open"] > 0].copy()
        n["available_at"] = (n.index + pd.Timedelta(hours=15, minutes=31)).tz_localize(IST)
        return n
    sym = {"SPX": "ES=F", "BTC": "BTC-USD", "GOLD": "GC=F", "CRUDE": "CL=F"}[market]
    return M._yahoo_daily(sym)


def own(market, s: pd.Series) -> pd.DataFrame:
    b = bars(market)
    return pit(s.reindex(b.index), b["available_at"])


def rsi(c, n=14):
    d = c.diff()
    up, dn = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean(), (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + up / dn.replace(0, np.nan))


def atr(h, l, c, n=14):
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


@lru_cache(maxsize=None)
def char_frame(market):
    b = bars(market)
    return CH.compute(b, has_volume=market != "NIFTY" or b["volume"].fillna(0).gt(0).mean() > 0.8)


def _fred(sid):
    return _vp(ML._fred(sid))


def _ratio(a, b):
    r = ML._ratio(a, b)
    return None if r is None else _vp(r)


def chg(df, n, log=True):
    if df is None:
        return None
    v = df["value"]
    out = np.log(v / v.shift(n)) if log and (v > 0).all() else v - v.shift(n)
    return pit(out, df["available_at"])


# ------------------------------------------------------------------ the catalog
def catalog(market: str) -> list[tuple]:
    """(name, family, kind, direction, frame). direction: +1/-1 for dir; 'high'/'low' for gate."""
    b = bars(market)
    o, h, l, c = b["open"], b["high"], b["low"], b["close"]
    a14 = atr(h, l, c)
    cf = char_frame(market)
    yd = VP.yahoo_daily
    F = []

    def add(name, fam, kind, d, frame):
        if frame is not None and len(frame) > 150:
            F.append((name, fam, kind, d, frame))

    # --- 1 price / trend (own market)
    for n in (20, 50, 200):
        add(f"Price vs EMA{n} (in ATRs)", "Trend", "dir", +1, own(market, (c - c.ewm(span=n, adjust=False).mean()) / a14))
    add("EMA50 slope (20d)", "Trend", "dir", +1, own(market, c.ewm(span=50, adjust=False).mean().pct_change(20)))
    add("EMA200 slope (20d)", "Trend", "dir", +1, own(market, c.ewm(span=200, adjust=False).mean().pct_change(20)))
    add("EMA20 minus EMA50 (ATR)", "Trend", "dir", +1, own(market, (c.ewm(span=20).mean() - c.ewm(span=50).mean()) / a14))
    add("EMA50 minus EMA200 (ATR)", "Trend", "dir", +1, own(market, (c.ewm(span=50).mean() - c.ewm(span=200).mean()) / a14))
    add("+DI minus -DI", "Trend", "dir", +1, own(market, cf["plus_di"] - cf["minus_di"]))
    add("Drawdown from 1-year high", "Trend", "dir", +1, own(market, cf["drawdown_pct"]))
    wk = c.resample("W-FRI").last()
    wtrend = (wk - wk.ewm(span=20, adjust=False).mean()).reindex(c.index, method="ffill").shift(1)   # last COMPLETED week
    add("Weekly trend (price vs weekly EMA20)", "Trend", "dir", +1, own(market, wtrend / a14))
    # --- 2 momentum
    for n in (5, 20, 60, 120, 252):
        add(f"Return {n}d", "Momentum", "dir", +1, own(market, np.log(c / c.shift(n))))
    add("RSI 14", "Momentum", "dir", +1, own(market, rsi(c)))
    ema12, ema26 = c.ewm(span=12, adjust=False).mean(), c.ewm(span=26, adjust=False).mean()
    macd = ema12 - ema26
    add("MACD histogram (ATR)", "Momentum", "dir", +1, own(market, (macd - macd.ewm(span=9, adjust=False).mean()) / a14))
    ll, hh = l.rolling(14).min(), h.rolling(14).max()
    add("Stochastic %K", "Momentum", "dir", +1, own(market, 100 * (c - ll) / (hh - ll).replace(0, np.nan)))
    tp = (h + l + c) / 3
    add("CCI 20", "Momentum", "dir", +1, own(market, (tp - tp.rolling(20).mean()) / (0.015 * tp.rolling(20).apply(lambda a: np.mean(np.abs(a - a.mean())), raw=True))))
    add("Williams %R", "Momentum", "dir", +1, own(market, -100 * (hh - c) / (hh - ll).replace(0, np.nan)))
    add("Return skew 60d", "Momentum", "dir", +1, own(market, cf["skew_60"]))
    # --- 3 trend quality / character (gates)
    add("ADX", "Character", "gate", "high", own(market, cf["adx"]))
    add("ADX slope (5d)", "Character", "gate", "high", own(market, cf["adx"].diff(5)))
    add("Efficiency ratio", "Character", "gate", "high", own(market, cf["eff_ratio"]))
    add("Hurst exponent", "Character", "gate", "high", own(market, cf["hurst"]))
    add("Return autocorrelation", "Character", "gate", "high", own(market, cf["autocorr"]))
    add("Choppiness index", "Character", "gate", "low", own(market, cf["chop"]))
    # --- 4 volatility (own)
    for k, nm in (("vol_cc", "Realised vol close-close"), ("vol_park", "Parkinson vol"), ("vol_gk", "Garman-Klass vol"),
                  ("vol_yz", "Yang-Zhang vol")):
        add(nm, "Volatility", "gate", "low", own(market, cf[k]))
    lr = np.log(c / c.shift())
    rv5, rv20, rv60 = lr.rolling(5).std(), lr.rolling(20).std(), lr.rolling(60).std()
    add("Vol expansion RV5/RV20", "Volatility", "gate", "low", own(market, rv5 / rv20))
    add("Vol expansion RV10/RV60", "Volatility", "gate", "low", own(market, lr.rolling(10).std() / rv60))
    add("ATR5/ATR20", "Volatility", "gate", "low", own(market, atr(h, l, c, 5) / a14.rolling(20).mean()))
    add("ATR20/ATR100", "Volatility", "gate", "low", own(market, a14.rolling(20).mean() / a14.rolling(100).mean()))
    add("Vol-of-vol", "Volatility", "gate", "low", own(market, cf["vol_of_vol"]))
    add("Bollinger width", "Volatility", "gate", "high", own(market, cf["bb_width"]))
    add("ATR % of price", "Volatility", "gate", "low", own(market, cf["atr_pct"]))
    # --- 5 volume / liquidity
    if b["volume"].fillna(0).gt(0).mean() > 0.8:
        v = b["volume"].replace(0, np.nan)
        add("Relative volume (20d)", "Liquidity", "gate", "high", own(market, v / v.rolling(20).mean()))
        add("Relative volume (60d)", "Liquidity", "gate", "high", own(market, v / v.rolling(60).mean()))
        obv = (np.sign(c.diff()).fillna(0) * v.fillna(0)).cumsum()
        add("OBV trend (20d change, z)", "Liquidity", "dir", +1, own(market, (obv - obv.shift(20)) / v.rolling(20).sum()))
        amihud = (lr.abs() / (v * c)).rolling(20).mean()
        add("Amihud illiquidity", "Liquidity", "gate", "low", own(market, amihud))
        add("Price-volume agreement (20d)", "Liquidity", "dir", +1, own(market, np.sign(c.diff()).rolling(20).corr(v.pct_change())))
    # --- 6 implied volatility
    iv_own = {"NIFTY": yd("^INDIAVIX"), "SPX": yd("^VIX"), "BTC": VP.dvol("BTC"), "GOLD": yd("^GVZ"), "CRUDE": yd("^OVX")}[market]
    add("Own implied vol level", "Implied vol", "dir", -1, _vp(iv_own))
    add("Own implied vol 5d change", "Implied vol", "dir", -1, chg(_vp(iv_own), 5, log=False))
    if iv_own is not None:
        ivs = _vp(iv_own)
        rv = (rv20 * np.sqrt(365 if market == "BTC" else 252) * 100).reindex(ivs.index, method="ffill")
        add("Implied minus realised vol", "Implied vol", "dir", -1, pit(ivs["value"] - rv, ivs["available_at"]))
    add("US VIX", "Implied vol", "dir", -1, _vp(yd("^VIX")))
    add("VIX / VIX3M (term structure)", "Implied vol", "dir", -1, _ratio(yd("^VIX"), yd("^VIX3M")))
    add("VVIX", "Implied vol", "dir", -1, _vp(yd("^VVIX")))
    add("VVIX / VIX", "Implied vol", "dir", -1, _ratio(yd("^VVIX"), yd("^VIX")))
    add("MOVE (bond vol)", "Implied vol", "dir", -1, _vp(yd("^MOVE")))
    add("CBOE SKEW", "Implied vol", "dir", -1, _vp(yd("^SKEW")))
    add("VXN (Nasdaq vol)", "Implied vol", "dir", -1, _vp(yd("^VXN")))
    add("OVX (oil vol)", "Implied vol", "dir", -1, _vp(yd("^OVX")))
    add("GVZ (gold vol)", "Implied vol", "dir", -1, _vp(yd("^GVZ")))
    add("BTC DVOL", "Implied vol", "dir", -1, _vp(VP.dvol("BTC")))
    add("India VIX", "Implied vol", "dir", -1, _vp(yd("^INDIAVIX")))
    # --- 7 positioning
    if market == "NIFTY":
        fut = D.load_futures_daily("NIFTY")
        fa = (fut.index + pd.Timedelta(hours=15, minutes=31)).tz_localize(IST)
        spot = c.reindex(fut.index)
        add("Futures basis %", "Positioning", "dir", +1, pit((fut["close"] / spot - 1) * 100, fa))
        dp, doi = fut["close"].diff(), fut["oi_total"].diff()
        build = np.select([(dp > 0) & (doi > 0), (dp < 0) & (doi > 0), (dp > 0) & (doi < 0), (dp < 0) & (doi < 0)],
                          [1.0, -1.0, 0.5, -0.5], 0.0)
        add("Price x OI build-up (5d sum)", "Positioning", "dir", +1, pit(pd.Series(build, index=fut.index).rolling(5).sum(), fa))
        add("Futures OI 5d change", "Positioning", "dir", +1, pit(fut["oi_total"].pct_change(5), fa))
        add("FII index-futures long %", "Positioning", "dir", +1, _vp(VP.fii_long_pct()))
        add("FII long % 5d change", "Positioning", "dir", +1, chg(_vp(VP.fii_long_pct()), 5, log=False))
    cot = {"SPX": "ES", "GOLD": "GOLD", "CRUDE": "CRUDE", "BTC": "BTC"}.get(market)
    if cot:
        add("COT speculators net long %", "Positioning", "dir", +1, _vp(VP.cot_net(cot)))
        add("COT net long 4-week change", "Positioning", "dir", +1, chg(_vp(VP.cot_net(cot)), 4, log=False))
    if market == "BTC":
        add("BTC funding 7d (contrarian)", "Positioning", "dir", -1, _vp(VP.funding_annual("BTC")))
        add("ETH funding 7d (contrarian)", "Positioning", "dir", -1, _vp(VP.funding_annual("ETH")))
    # --- 8 breadth (India)
    if market == "NIFTY":
        bf = compute_breadth_features(D.load_stock_panel())
        ba = (bf.index + pd.Timedelta(hours=15, minutes=31)).tz_localize(IST)
        add("% stocks above 50-DMA", "Breadth", "dir", +1, pit(bf["raw_pct_above_50dma"], ba))
        add("Breadth: 10d advance/decline", "Breadth", "dir", +1, pit(bf["C.5b_adv_dec_10d"], ba))
        p = D.load_stock_panel()
        wide = p.pivot_table(index="date", columns="symbol", values="close").sort_index()
        a200 = (wide > wide.rolling(200, min_periods=200).mean()).astype(float).where(wide.rolling(200, min_periods=200).mean().notna())
        pa = a200.mean(axis=1).where(a200.notna().sum(axis=1) >= 50)
        add("% stocks above 200-DMA", "Breadth", "dir", +1, pit(pa, (pa.index + pd.Timedelta(hours=15, minutes=31)).tz_localize(IST)))
        hi = (wide >= wide.rolling(20).max()).sum(axis=1) - (wide <= wide.rolling(20).min()).sum(axis=1)
        add("20d new highs minus new lows", "Breadth", "dir", +1, pit(hi / wide.notna().sum(axis=1), (hi.index + pd.Timedelta(hours=15, minutes=31)).tz_localize(IST)))
        add("Midcap 50 / Nifty (20d)", "Breadth", "dir", +1, chg(_ratio(yd("^NSEMDCP50"), yd("^NSEI")), 20))
        add("Nifty 500 / Nifty (20d)", "Breadth", "dir", +1, chg(_ratio(yd("^CRSLDX"), yd("^NSEI")), 20))
        add("Bank Nifty / Nifty (20d)", "Breadth", "dir", +1, chg(_ratio(yd("^NSEBANK"), yd("^NSEI")), 20))
        add("IT / Nifty (20d)", "Breadth", "dir", -1, chg(_ratio(yd("^CNXIT"), yd("^NSEI")), 20))
        add("Nifty vs S&P relative strength (20d)", "Breadth", "dir", +1, chg(_ratio(yd("^NSEI"), yd("^GSPC")), 20))
    # --- 9 risk appetite / cross-asset prices
    add("Credit appetite HYG/IEF (20d)", "Risk appetite", "dir", +1, chg(_ratio(yd("HYG"), yd("IEF")), 20))
    add("High-yield spread", "Risk appetite", "dir", -1, _fred("BAMLH0A0HYM2"))
    add("Cyclicals/defensives XLY/XLP (20d)", "Risk appetite", "dir", +1, chg(_ratio(yd("XLY"), yd("XLP")), 20))
    add("Small/large IWM/RSP (20d)", "Risk appetite", "dir", +1, chg(_ratio(yd("IWM"), yd("RSP")), 20))
    add("Equal weight RSP vs S&P (20d)", "Risk appetite", "dir", +1, chg(_ratio(yd("RSP"), yd("^GSPC")), 20))
    add("Semis SMH vs S&P (20d)", "Risk appetite", "dir", +1, chg(_ratio(yd("SMH"), yd("^GSPC")), 20))
    add("Russell 2000 (20d)", "Risk appetite", "dir", +1, chg(_vp(yd("^RUT")), 20))
    cu, au = ML._bars("COPPER"), ML._bars("GOLD")
    add("Copper/gold (20d)", "Risk appetite", "dir", +1 if market != "GOLD" else -1, chg(_ratio(cu, au), 20))
    add("Gold/silver ratio (20d)", "Risk appetite", "dir", -1, chg(_ratio(au, ML._bars("SILVER")), 20))
    add("Dollar index (20d)", "Risk appetite", "dir", -1, chg(_vp(yd("DX-Y.NYB")), 20))
    add("USD/INR (20d)", "Risk appetite", "dir", -1 if market == "NIFTY" else +1, chg(_vp(yd("INR=X")), 20))
    add("Brent (20d)", "Risk appetite", "dir", +1 if market == "CRUDE" else -1, chg(_vp(yd("BZ=F")), 20))
    add("Global risk score (label 3)", "Risk appetite", "dir", +1, _score("global"))
    add("India risk score (label 3)", "Risk appetite", "dir", +1, _score("india"))
    # --- 10 macro / rates / liquidity
    rsign = +1 if market == "CRUDE" else -1
    add("US 10Y real yield (60d chg)", "Macro", "dir", -1, chg(_fred("DFII10"), 60, log=False))
    add("US 2Y (60d chg)", "Macro", "dir", -1, chg(_fred("DGS2"), 60, log=False))
    add("US 10Y (60d chg)", "Macro", "dir", rsign, chg(_fred("DGS10"), 60, log=False))
    add("10Y-2Y curve (60d chg)", "Macro", "dir", +1, chg(_fred("T10Y2Y"), 60, log=False))
    add("10Y-3M curve level", "Macro", "dir", +1, _corr_col("curve_10y3m"))
    add("Financial conditions NFCI", "Macro", "dir", -1, _fred("NFCI"))
    add("Fed net liquidity (12w chg)", "Macro", "dir", +1, _netliq())
    be = ML.components()["inflation"].get("Breakeven inflation (60d)")
    add("Breakeven inflation (60d chg)", "Macro", "dir", +1 if market in ("GOLD", "CRUDE", "BTC") else -1,
        None if be is None else pit(be["score"], be["available_at"]))
    add("Fed funds rate (60d chg)", "Macro", "dir", -1, chg(_fred("DFF"), 60, log=False))
    add("Money score (label 4a, + = tight)", "Macro", "dir", -1, _score("money"))
    add("Money v2 score", "Macro", "dir", -1, _score("money_v2"))
    add("Growth score (label 4b)", "Macro", "dir", +1, _score("growth"))
    # --- 11 correlation structure
    add("Average correlation, 12 markets", "Correlation", "dir", -1, _corr_col("avg_corr"))
    add("Stock-bond correlation", "Correlation", "dir", -1, _corr_col("stock_bond"))
    add("Oil vs S&P correlation", "Correlation", "gate", "high", _corr_col("oil_vs_spx"))
    add("BTC vs Nasdaq correlation", "Correlation", "gate", "low", _corr_col("btc_vs_nq"))
    add("Avg correlation 20d change", "Correlation", "dir", -1, chg(_corr_col("avg_corr"), 20, log=False))
    # --- round 2: data queued in the updater (skipped automatically until it has been downloaded)
    add("VIX9D / VIX", "Implied vol", "dir", -1, _ratio(yd("^VIX9D"), yd("^VIX")))
    add("Long bonds TLT (20d)", "Macro", "dir", +1 if market in ("GOLD", "BTC") else -1, chg(_vp(yd("TLT")), 20))
    add("Credit HYG/LQD (20d)", "Risk appetite", "dir", +1, chg(_ratio(yd("HYG"), yd("LQD")), 20))
    add("Emerging markets EEM vs S&P (20d)", "Risk appetite", "dir", +1, chg(_ratio(yd("EEM"), yd("^GSPC")), 20))
    for etf, nm in (("XLK", "Tech"), ("XLF", "Financials"), ("XLE", "Energy"), ("XLV", "Health care"), ("XLI", "Industrials"),
                    ("XLU", "Utilities"), ("XLB", "Materials"), ("XLRE", "Real estate"), ("XLC", "Communication")):
        d = -1 if etf in ("XLU", "XLV") else (+1 if market != "CRUDE" or etf == "XLE" else +1)
        add(f"{nm} sector {etf} vs S&P (20d)", "Sector rotation", "dir", d, chg(_ratio(yd(etf), yd("^GSPC")), 20))
    add("Semis index SOX vs S&P (20d)", "Sector rotation", "dir", +1, chg(_ratio(yd("^SOX"), yd("^GSPC")), 20))
    add("EUR/USD (20d)", "FX", "dir", +1, chg(_vp(yd("EURUSD=X")), 20))
    add("USD/JPY (20d)", "FX", "dir", +1, chg(_vp(yd("JPY=X")), 20))
    add("USD/CNH (20d)", "FX", "dir", -1, chg(_vp(yd("CNH=X")), 20))
    add("US 30Y (60d chg)", "Macro", "dir", -1 if market != "CRUDE" else +1, chg(_vp(yd("^TYX")), 60, log=False))
    if market == "NIFTY":
        opt = VP._csv("nse_options_eod.csv")
        if opt is not None and len(opt) > 150:
            o_ = opt[opt["symbol"] == "NIFTY"].copy()
            o_["date"] = pd.to_datetime(o_["date"])
            o_ = o_.set_index("date").sort_index()
            oa = pd.to_datetime(o_["available_at"]).dt.tz_localize("UTC")
            add("Put/call ratio, OI (contrarian +)", "Options", "dir", +1, pit(o_["pcr_oi"], oa))
            add("Put/call ratio, near expiry", "Options", "dir", +1, pit(o_["pcr_oi_near"], oa))
            add("Put/call ratio, volume", "Options", "dir", +1, pit(o_["pcr_vol"], oa))
            add("Spot vs max pain (%)", "Options", "dir", -1, pit((o_["underlying"] / o_["max_pain"] - 1) * 100, oa))
            add("Room to call wall (%)", "Options", "dir", +1, pit((o_["call_wall"] / o_["underlying"] - 1) * 100, oa))
            add("Room to put wall (%)", "Options", "dir", -1, pit((1 - o_["put_wall"] / o_["underlying"]) * 100, oa))
            add("ATM straddle % of spot (implied move)", "Options", "dir", -1, pit(o_["straddle_pct"], oa))
            add("Put minus call OI added", "Options", "dir", +1, pit((o_["pe_oi_chg"] - o_["ce_oi_chg"]) / 1e6, oa))
    # --- 12 calendar / event / regime state
    idx = b.index
    add("Day of week (Mon=0)", "Calendar", "gate", "low", own(market, pd.Series(idx.dayofweek, index=idx).astype(float)))
    add("Month-end window (last 3 / first 2 days)", "Calendar", "gate", "high",
        own(market, pd.Series(((idx + pd.offsets.BDay(3)).month != idx.month) | (idx.day <= 2), index=idx).astype(float)))
    if market == "NIFTY":
        exp_wd = np.where(idx < pd.Timestamp("2025-09-01"), 3, 1)
        days_to = (exp_wd - idx.dayofweek) % 7
        add("Days to weekly expiry", "Calendar", "gate", "high", own(market, pd.Series(days_to, index=idx).astype(float)))
    ev = _event_days(idx)
    if ev is not None:
        add("No core event within 2 days", "Event", "gate", "high", own(market, (ev > 2).astype(float).where(ev.notna())))
    sig = regime_sig(market)
    age = sig.groupby((sig != sig.shift()).cumsum()).cumcount() + 1
    add("Regime age (days)", "Regime state", "gate", "high", own(market, age.astype(float)))
    ts = CH.robust_z(c.ewm(span=21).mean().pct_change(10))
    add("Trend score change (10d)", "Regime state", "dir", +1, own(market, ts))
    return F


def _score(group):
    comps = ML.components().get(group)
    if not comps:
        return None
    times = pd.date_range("2012-01-01", pd.Timestamp.now(), freq="D").tz_localize("UTC") + pd.Timedelta(hours=22)
    g = ML.group_scores(times)
    s = g[group] if group in g else None
    if s is None:
        return None
    return pit(pd.Series(s.values, index=times.tz_localize(None).normalize()), times)


def _corr_col(col):
    f = XC.frame()
    return pit(f[col], f["available_at"])


def _netliq():
    w, t, r = ML._fred("WALCL"), ML._fred("WTREGEN"), ML._fred("RRPONTSYD")
    if w is None or t is None or r is None:
        return None
    liq = w["value"] - t["value"].reindex(w.index, method="ffill") - 1000 * r["value"].reindex(w.index, method="ffill")
    av = pd.concat([w["available_at"], t["available_at"].reindex(w.index, method="ffill"),
                    r["available_at"].reindex(w.index, method="ffill")], axis=1).max(axis=1)
    return pit(np.log(liq / liq.shift(12)), av)


def _event_days(idx):
    from src.common.regime_engine.features.calendar_tags import _events
    ev = sorted(e for e, n, core in _events() if core)
    if not ev:
        return None
    evi = pd.DatetimeIndex(ev)
    pos = evi.searchsorted(idx)
    nxt = pd.Series([(evi[p] - d).days if p < len(evi) else 999 for d, p in zip(idx, pos)], index=idx)
    nxt[idx < evi.min() - pd.Timedelta(days=30)] = np.nan          # calendar starts 2023: no data before
    return nxt


LONG = {"Strong uptrend", "Grinding uptrend"}
SHORT = {"Downtrend", "Panic"}


@lru_cache(maxsize=None)
def regime_sig(market):
    from src.common.regime_engine.engine import describe_state
    from src.common.regime_engine.features import price_features as PF
    c = bars(market)["close"]
    f = PF.compute_price_features(c)
    t = f[M.TREND_FEATURES].mean(axis=1, skipna=True)
    v = PF.vol_state(c)
    n = [describe_state(a, x if isinstance(x, str) else None)[1] for a, x in zip(t, v)]
    return pd.Series([1 if r in LONG else -1 if r in SHORT else 0 for r in n], index=c.index)


def asof(frame: pd.DataFrame, times: pd.DatetimeIndex, stale_days: int = 21) -> pd.Series:
    """Value known at each time (feature's own rolling 1-year percentile, 0-100)."""
    f = frame.copy()
    f["pct"] = f["value"].rolling(252, min_periods=60).apply(lambda a: (a <= a[-1]).mean() * 100, raw=True)
    s = f.dropna(subset=["pct"]).sort_values("available_at")
    q = pd.DataFrame({"t": times.tz_convert("UTC"), "i": range(len(times))}).sort_values("t")
    m = pd.merge_asof(q, s[["available_at", "pct"]], left_on="t", right_on="available_at", direction="backward",
                      tolerance=pd.Timedelta(days=stale_days))
    return pd.Series(m.sort_values("i")["pct"].values, index=times)
