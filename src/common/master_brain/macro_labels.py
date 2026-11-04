"""
Regime labels 3 and 4: RISK APPETITE and MACRO BACKDROP (labels 1-2, trend and volatility, live in markets.py).

Rules were written BEFORE any backtest and are not tuned on results.

Every component is a series with its publication time (`available_at`). Each is turned into a score with a
rolling 1-year z-score of its own past only (no look-ahead), then sampled "as of" a decision time.

RISK APPETITE (score > +0.25 risk-on, < -0.25 risk-off, else mixed)
  Global: credit appetite HYG/IEF (20-day change, +), VIX 1-year percentile (-), VIX above VIX3M (-),
          cyclicals vs defensives XLY/XLP (20d, +), small caps IWM/RSP (20d, +), copper/gold (20d, +),
          dollar index (20d, -)
  India : Midcap 50 / Nifty (20d, +), USD/INR (20d, -), India VIX 1-year percentile (-), Brent (20d, -)
          + global risk score at half weight

MACRO BACKDROP
  Money  (Tight / Easy / Neutral): US 10Y real yield (60-day change, + = tighter), US 2Y (60d, +),
          financial conditions NFCI (level, + = tighter), Fed net liquidity (60d change, - = tighter)
  Growth (Growth / Slowdown / Neutral): copper/gold (60d, +), 10Y-2Y curve (60d change, + = steepening),
          small caps vs equal weight (60d, +)
  Inflation pressure flag: 10Y breakeven (DGS10 - DFII10, 60d change) and Brent (60d) both rising
"""
from __future__ import annotations

from functools import lru_cache

import numpy as np
import pandas as pd

from src.common.regime_engine import data as D
from src.common.master_brain import markets as M
from src.common.master_brain import vol_positioning as VP

TH = 0.25
Z_WIN = 252


def _z(s: pd.Series, win: int = Z_WIN) -> pd.Series:
    m = s.rolling(win, min_periods=120).mean()
    sd = s.rolling(win, min_periods=120).std()
    return ((s - m) / sd).clip(-3, 3)


def _pct(s: pd.Series, win: int = Z_WIN) -> pd.Series:
    """Rolling percentile (0..1) of the latest value within its own past year."""
    return s.rolling(win, min_periods=120).apply(lambda a: (a <= a[-1]).mean(), raw=True)


def _frame(df: pd.DataFrame | None) -> pd.DataFrame | None:
    if df is None or df.empty:
        return None
    out = df[["value", "available_at"]].copy()
    out["available_at"] = pd.DatetimeIndex(out["available_at"]).tz_convert("UTC")
    out = out[~out.index.duplicated(keep="last")].sort_index()
    return out


def _ratio(a, b):
    a, b = _frame(a), _frame(b)
    if a is None or b is None:
        return None
    j = a.join(b, rsuffix="_b", how="inner")
    return pd.DataFrame({"value": j["value"] / j["value_b"], "available_at": j[["available_at", "available_at_b"]].max(axis=1)},
                        index=j.index)


def _fred(sid):
    f = VP._csv("macro_fred.csv")
    if f is None:
        return None
    f = f[f["series"] == sid].copy()
    f["date"] = pd.to_datetime(f["date"])
    f = f.drop_duplicates("date", keep="last").set_index("date").sort_index()
    return pd.DataFrame({"value": f["value"], "available_at": pd.to_datetime(f["available_at"]).dt.tz_localize("UTC")})


def _bars(market):
    b = M.market_bars(market)
    idx = b.index.tz_localize(None) if b.index.tz is not None else b.index
    return pd.DataFrame({"value": b["close"].values, "available_at": pd.DatetimeIndex(b["available_at"]).tz_convert("UTC")}, index=idx)


def _component(df, how: str, sign: int, days: int = 20) -> pd.DataFrame | None:
    """Score series (z-score of a change, a level, or a percentile) with its availability time."""
    df = _frame(df)
    if df is None:
        return None
    v = df["value"].astype(float)
    if how == "chg":
        x = _z(np.log(v / v.shift(days)) if (v > 0).all() else v - v.shift(days))
    elif how == "diff":
        x = _z(v - v.shift(days))
    elif how == "level":
        x = _z(v)
    elif how == "pct":
        x = (_pct(v) - 0.5) * 4                   # 0..1 -> -2..+2
    else:
        raise ValueError(how)
    return pd.DataFrame({"score": sign * x, "available_at": df["available_at"]}).dropna()


def _corr_component(col: str, sign: int):
    from src.common.master_brain import correlations as X
    f = X.frame()
    v = f[col].astype(float)
    x = v.clip(-1, 1) * 2 if col == "stock_bond" else _z(v)      # a correlation is already scale-free
    return pd.DataFrame({"score": sign * x, "available_at": f["available_at"]}).dropna()


@lru_cache(maxsize=None)
def components() -> dict:
    yd = VP.yahoo_daily
    vix, vix3m = yd("^VIX"), yd("^VIX3M")
    term = _ratio(vix, vix3m)
    if term is not None:
        term = pd.DataFrame({"value": (term["value"] > 1).astype(float), "available_at": term["available_at"]})
    be = None
    d10, r10 = _fred("DGS10"), _fred("DFII10")
    if d10 is not None and r10 is not None:
        j = d10.join(r10, rsuffix="_r", how="inner")
        be = pd.DataFrame({"value": j["value"] - j["value_r"], "available_at": j[["available_at", "available_at_r"]].max(axis=1)})
    liq = None
    w, t, r = _fred("WALCL"), _fred("WTREGEN"), _fred("RRPONTSYD")
    if w is not None and t is not None and r is not None:
        tt = t["value"].reindex(w.index, method="ffill")
        rr = r["value"].reindex(w.index, method="ffill")
        av = pd.concat([w["available_at"], t["available_at"].reindex(w.index, method="ffill"),
                        r["available_at"].reindex(w.index, method="ffill")], axis=1).max(axis=1)
        liq = pd.DataFrame({"value": w["value"] - tt - 1000 * rr, "available_at": av}).dropna()
    cu_au = _ratio(_bars("COPPER"), _bars("GOLD"))
    c = {
        "global": {
            "Credit appetite (HYG/IEF)": _component(_ratio(yd("HYG"), yd("IEF")), "chg", +1),
            "VIX level": _component(vix, "pct", -1),
            "VIX above VIX3M": (lambda f: None if f is None else pd.DataFrame(
                {"score": -2.0 * f["value"], "available_at": f["available_at"]}))(term),
            "Cyclicals vs defensives (XLY/XLP)": _component(_ratio(yd("XLY"), yd("XLP")), "chg", +1),
            "Small caps (IWM/RSP)": _component(_ratio(yd("IWM"), yd("RSP")), "chg", +1),
            "Copper / gold": _component(cu_au, "chg", +1),
            "Dollar index": _component(yd("DX-Y.NYB"), "chg", -1),
        },
        "india": {
            "Midcap / Nifty": _component(_ratio(yd("^NSEMDCP50"), yd("^NSEI")), "chg", +1),
            "USD / INR": _component(yd("INR=X"), "chg", -1),
            "India VIX level": _component(yd("^INDIAVIX"), "pct", -1),
            "Brent crude": _component(yd("BZ=F"), "chg", -1),
        },
        "money": {
            "Real yield (60d)": _component(_fred("DFII10"), "diff", +1, 60),
            "US 2Y (60d)": _component(_fred("DGS2"), "diff", +1, 60),
            "Financial conditions (NFCI)": _component(_fred("NFCI"), "level", +1),
            "Fed net liquidity (60d)": _component(liq, "chg", -1, 12),          # weekly series: 12 weeks
        },
        # v2 candidate (being tested, not used for the live label): v1 + stock-bond correlation + 10Y-3M curve
        "money_v2": {
            "Real yield (60d)": _component(_fred("DFII10"), "diff", +1, 60),
            "US 2Y (60d)": _component(_fred("DGS2"), "diff", +1, 60),
            "Financial conditions (NFCI)": _component(_fred("NFCI"), "level", +1),
            "Fed net liquidity (60d)": _component(liq, "chg", -1, 12),
            "Stock-bond correlation": _corr_component("stock_bond", +1),
            "10Y minus 3M curve": _corr_component("curve_10y3m", -1),
        },
        "growth": {
            "Copper / gold (60d)": _component(cu_au, "chg", +1, 60),
            "Yield curve steepening (60d)": _component(_fred("T10Y2Y"), "diff", +1, 60),
            "Small caps (60d)": _component(_ratio(yd("IWM"), yd("RSP")), "chg", +1, 60),
        },
        "inflation": {
            "Breakeven inflation (60d)": _component(be, "diff", +1, 60),
            "Brent (60d)": _component(yd("BZ=F"), "chg", +1, 60),
        },
    }
    return {g: {k: v for k, v in comps.items() if v is not None and len(v)} for g, comps in c.items()}


def _asof(comp: pd.DataFrame, times: pd.DatetimeIndex) -> pd.Series:
    s = comp.sort_values("available_at")
    q = pd.DataFrame({"t": times.tz_convert("UTC")}).reset_index().sort_values("t")
    m = pd.merge_asof(q, s.reset_index(drop=True), left_on="t", right_on="available_at", direction="backward",
                      tolerance=pd.Timedelta(days=21))                       # stale > 3 weeks = missing
    return pd.Series(m.sort_values("index")["score"].values, index=times)


def group_scores(times) -> pd.DataFrame:
    """Score per group at each decision time (UTC-aware DatetimeIndex)."""
    times = pd.DatetimeIndex(times)
    times = times if times.tz is not None else times.tz_localize(D.IST)
    out = {}
    for g, comps in components().items():
        cols = pd.DataFrame({k: _asof(v, times) for k, v in comps.items()})
        out[g] = cols.mean(axis=1, skipna=True).where(cols.notna().sum(axis=1) >= max(1, len(comps) // 2))
    df = pd.DataFrame(out, index=times)
    df["india"] = (df["india"] * 2 + df["global"]) / 3                    # India: own inputs + global at half weight
    return df


def label(score, pos="Risk-on", neg="Risk-off", mid="Mixed"):
    if score is None or (isinstance(score, float) and np.isnan(score)):
        return None
    return pos if score > TH else neg if score < -TH else mid


def labels(times) -> pd.DataFrame:
    g = group_scores(times)
    out = pd.DataFrame(index=g.index)
    out["global_risk"] = [label(x) for x in g["global"]]
    out["india_risk"] = [label(x) for x in g["india"]]
    out["money"] = [label(x, "Tight", "Easy", "Neutral") for x in g["money"]]
    if "money_v2" in g:
        out["money_v2"] = [label(x, "Tight", "Easy", "Neutral") for x in g["money_v2"]]
    out["growth"] = [label(x, "Growth", "Slowdown", "Neutral") for x in g["growth"]]
    out["inflation"] = [None if pd.isna(x) else ("Rising" if x > TH else "Falling" if x < -TH else "Stable") for x in g["inflation"]]
    return pd.concat([g.add_suffix("_score"), out], axis=1)


def now(as_of) -> dict:
    """Labels + the components behind them at one moment (for the dashboard)."""
    t = pd.Timestamp(as_of)
    t = t if t.tzinfo else t.tz_localize(D.IST)
    lab = labels(pd.DatetimeIndex([t])).iloc[0]
    detail = {}
    for g, comps in components().items():
        if g == "money_v2":
            continue
        detail[g] = [{"name": k, "score": None if pd.isna(v := _asof(c, pd.DatetimeIndex([t])).iloc[0]) else round(float(v), 2)}
                     for k, c in comps.items()]
    clean = lambda x: None if x is None or (isinstance(x, float) and np.isnan(x)) else (round(float(x), 2) if isinstance(x, (float, np.floating)) else x)  # noqa: E731
    return {k: clean(v) for k, v in lab.items()} | {"components": detail}
