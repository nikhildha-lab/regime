"""
Technical indicator panel (the TradingView "MTF trend / wait zone" table), computed per timeframe.

Every indicator gives a state in {+1 bullish, 0 neutral/mixed, -1 bearish} on CLOSED bars only
(rolling/recursive, never centered or shifted backward). Defaults match common TradingView settings:

  T.1 EMA 9/21/50   +1 "Stacked Up" (9>21>50), -1 "Stacked Down" (9<21<50), 0 "Mixed"
  T.2 SMA 50/200    +1 close>SMA50>SMA200, -1 close<SMA50<SMA200, 0 "Mixed"
  T.3 Supertrend    ATR(10) x 3: +1 uptrend, -1 downtrend
  T.4 Ichimoku      9/26/52, cloud displaced 26 bars: +1 above cloud, -1 below, 0 "In Cloud"
  T.5 Bollinger     20, 2sd: +1 close in upper half (>= basis), -1 lower half
  T.6 VWAP          session VWAP (resets each day). Index bars carry no volume, so intraday uses the
                    session TWAP of typical price (TradingView's behaviour without volume).
                    Daily timeframe: rolling 20-day volume-weighted price. +1 above, -1 below.
  T.7 Parabolic SAR 0.02 / 0.02 / 0.2, port of TradingView ta.sar: +1 SAR below price, -1 SAR above
  Confluence        count of +1 and -1 states; trend = Up if up > down, Down if down > up, else Flat

Wait zone (ASSUMPTION - the screenshot's script source was not provided; rule is configurable):
  a timeframe is in its wait zone when its signal is not clean, i.e. ANY of
    - |up - down| <= WAIT_MARGIN (weak confluence),
    - price is inside the Ichimoku cloud,
    - its trend disagrees with the next higher timeframe.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

FEATURES = {
    "T.1_ema_9_21_50": ("TECHNICALS", "+1 = EMA 9 > 21 > 50 (stacked up), -1 = stacked down, 0 = mixed"),
    "T.2_sma_50_200": ("TECHNICALS", "+1 = close > SMA50 > SMA200, -1 = close < SMA50 < SMA200, 0 = mixed"),
    "T.3_supertrend": ("TECHNICALS", "+1 = Supertrend(10,3) up, -1 = down"),
    "T.4_ichimoku": ("TECHNICALS", "+1 = above cloud, -1 = below cloud, 0 = in cloud"),
    "T.5_bollinger": ("TECHNICALS", "+1 = close in upper half of BB(20,2), -1 = lower half"),
    "T.6_vwap": ("TECHNICALS", "+1 = close above session VWAP (TWAP when no volume), -1 = below"),
    "T.7_parabolic_sar": ("TECHNICALS", "+1 = SAR below price (bull), -1 = SAR above (bear)"),
}
LABELS = {
    "T.1_ema_9_21_50": {1: "Stacked Up", -1: "Stacked Down", 0: "Mixed"},
    "T.2_sma_50_200": {1: "Bullish", -1: "Bearish", 0: "Mixed"},
    "T.3_supertrend": {1: "Uptrend", -1: "Downtrend"},
    "T.4_ichimoku": {1: "Above Cloud", -1: "Below Cloud", 0: "In Cloud"},
    "T.5_bollinger": {1: "Upper Half", -1: "Lower Half"},
    "T.6_vwap": {1: "Above VWAP", -1: "Below VWAP"},
    "T.7_parabolic_sar": {1: "SAR Below (Bull)", -1: "SAR Above (Bear)"},
}
WAIT_MARGIN = 1
MIN_INDICATORS = 4          # trend is shown once at least 4 of the 7 indicators have enough history


def _ema(s, n):
    return s.ewm(span=n, adjust=False, min_periods=n).mean()


def _atr(h, l, c, n):
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()     # Wilder (RMA), as TradingView


def ema_stack(c):
    e9, e21, e50 = _ema(c, 9), _ema(c, 21), _ema(c, 50)
    out = pd.Series(0.0, index=c.index)
    out[(e9 > e21) & (e21 > e50)] = 1.0
    out[(e9 < e21) & (e21 < e50)] = -1.0
    out[e50.isna()] = np.nan
    return out


def sma_50_200(c):
    s50, s200 = c.rolling(50).mean(), c.rolling(200).mean()
    out = pd.Series(0.0, index=c.index)
    out[(c > s50) & (s50 > s200)] = 1.0
    out[(c < s50) & (s50 < s200)] = -1.0
    out[s200.isna()] = np.nan
    return out


def supertrend(h, l, c, n=10, mult=3.0):
    atr = _atr(h, l, c, n).values
    hl2 = ((h + l) / 2).values
    cv = c.values
    up, dn = hl2 - mult * atr, hl2 + mult * atr
    fu, fd = up.copy(), dn.copy()
    trend = np.full(len(c), np.nan)
    for i in range(len(c)):
        if np.isnan(atr[i]):
            continue
        if i == 0 or np.isnan(trend[i - 1]):
            trend[i] = 1.0
            continue
        fu[i] = max(up[i], fu[i - 1]) if cv[i - 1] > fu[i - 1] else up[i]
        fd[i] = min(dn[i], fd[i - 1]) if cv[i - 1] < fd[i - 1] else dn[i]
        if trend[i - 1] == -1 and cv[i] > fd[i - 1]:
            trend[i] = 1.0
        elif trend[i - 1] == 1 and cv[i] < fu[i - 1]:
            trend[i] = -1.0
        else:
            trend[i] = trend[i - 1]
    return pd.Series(trend, index=c.index)


def ichimoku(h, l, c, t=9, k=26, s=52, disp=26):
    tenkan = (h.rolling(t).max() + l.rolling(t).min()) / 2
    kijun = (h.rolling(k).max() + l.rolling(k).min()) / 2
    span_a = ((tenkan + kijun) / 2).shift(disp - 1)        # value plotted at the current bar
    span_b = ((h.rolling(s).max() + l.rolling(s).min()) / 2).shift(disp - 1)
    top, bot = np.maximum(span_a, span_b), np.minimum(span_a, span_b)
    out = pd.Series(0.0, index=c.index)
    out[c > top] = 1.0
    out[c < bot] = -1.0
    out[span_b.isna()] = np.nan
    return out


def bollinger(c, n=20, k=2.0):
    basis = c.rolling(n).mean()
    out = pd.Series(np.where(c >= basis, 1.0, -1.0), index=c.index)
    out[basis.isna()] = np.nan
    return out


def vwap_state(bars: pd.DataFrame, daily: bool):
    h, l, c, v = bars["high"], bars["low"], bars["close"], bars["volume"].fillna(0)
    tp = (h + l + c) / 3
    if daily:
        vw = (tp * v).rolling(20, min_periods=20).sum() / v.rolling(20, min_periods=20).sum()
        vw = vw.where(v.rolling(20).sum() > 0, tp.rolling(20).mean())
    else:
        day = pd.Series(bars.index.tz_localize(None).normalize() if bars.index.tz is not None else bars.index.normalize(), index=bars.index)
        has_vol = v.groupby(day).transform("sum") > 0
        cum_pv = (tp * v).groupby(day).cumsum()
        cum_v = v.groupby(day).cumsum()
        twap = tp.groupby(day).cumsum() / tp.groupby(day).cumcount().add(1)
        vw = (cum_pv / cum_v.replace(0, np.nan)).where(has_vol, twap)
    out = pd.Series(np.where(c >= vw, 1.0, -1.0), index=c.index)
    out[vw.isna()] = np.nan
    return out


def parabolic_sar(h, l, c=None, start=0.02, inc=0.02, mx=0.2):
    """Port of TradingView's built-in ta.sar (Pine reference implementation): the reversal test uses
    the SAR before the 2-bar high/low clamp, and the clamp is applied afterwards."""
    hv, lv = h.values, l.values
    cv = c.values if c is not None else None
    n = len(hv)
    state = np.full(n, np.nan)
    if n < 3:
        return pd.Series(state, index=h.index)
    result = max_min = acc = np.nan
    is_below = None
    for i in range(1, n):
        first = False
        if i == 1:
            up = (cv[1] > cv[0]) if cv is not None else (hv[1] >= hv[0])
            if up:
                is_below, max_min, result = True, hv[1], lv[0]
            else:
                is_below, max_min, result = False, lv[1], hv[0]
            first, acc = True, start
        result = result + acc * (max_min - result)
        if is_below:
            if result > lv[i]:
                first, is_below, result, max_min, acc = True, False, max(hv[i], max_min), lv[i], start
        else:
            if result < hv[i]:
                first, is_below, result, max_min, acc = True, True, min(lv[i], max_min), hv[i], start
        if not first:
            if is_below and hv[i] > max_min:
                max_min, acc = hv[i], min(acc + inc, mx)
            elif (not is_below) and lv[i] < max_min:
                max_min, acc = lv[i], min(acc + inc, mx)
        if is_below:
            result = min(result, lv[i - 1], lv[i - 2] if i > 1 else lv[i - 1])
        else:
            result = max(result, hv[i - 1], hv[i - 2] if i > 1 else hv[i - 1])
        state[i] = 1.0 if is_below else -1.0
    state[:5] = np.nan                                       # warm-up
    return pd.Series(state, index=h.index)


def compute_panel(bars: pd.DataFrame, daily: bool = False) -> pd.DataFrame:
    h, l, c = bars["high"], bars["low"], bars["close"]
    p = pd.DataFrame({
        "T.1_ema_9_21_50": ema_stack(c),
        "T.2_sma_50_200": sma_50_200(c),
        "T.3_supertrend": supertrend(h, l, c),
        "T.4_ichimoku": ichimoku(h, l, c),
        "T.5_bollinger": bollinger(c),
        "T.6_vwap": vwap_state(bars, daily),
        "T.7_parabolic_sar": parabolic_sar(h, l, c),
    }, index=bars.index)
    cols = list(FEATURES)
    p["confluence_up"] = (p[cols] == 1).sum(axis=1)
    p["confluence_down"] = (p[cols] == -1).sum(axis=1)
    p["panel_trend"] = np.where(p["confluence_up"] > p["confluence_down"], "Up",
                                np.where(p["confluence_down"] > p["confluence_up"], "Down", "Flat"))
    p["indicators_available"] = p[cols].notna().sum(axis=1)
    p.loc[p["indicators_available"] < MIN_INDICATORS, "panel_trend"] = None   # e.g. SMA200 needs 200 bars
    return p


def panel_row(p: pd.Series) -> dict:
    """Human-readable panel for one bar."""
    states = {}
    for f in FEATURES:
        v = p.get(f)
        states[f] = None if v is None or pd.isna(v) else LABELS[f].get(int(v), "Mixed")
    trend = p["panel_trend"] if isinstance(p["panel_trend"], str) else None
    return {"indicators": {k: (v if v is not None else "n/a (not enough bars)") for k, v in states.items()},
            "confluence_up": int(p["confluence_up"]), "confluence_down": int(p["confluence_down"]),
            "indicators_available": int(p.get("indicators_available", 7)), "trend": trend}


def wait_zone(trend: str, up: int, down: int, ichimoku_state, higher_trend) -> bool:
    if trend is None:
        return True
    weak = abs(up - down) <= WAIT_MARGIN
    in_cloud = ichimoku_state == 0
    disagrees = higher_trend is not None and higher_trend != trend
    return bool(weak or in_cloud or disagrees)
