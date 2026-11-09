"""Classic trend indicators for the Impact-scores Trend section (added 2026-10-20, Nikhil's request).

Each reading -> one item on the -1..+1 scale (+1 = supports the market going UP).
Computed on the market's daily bars, point in time (only bars available at as_of).
VWAP uses today's intraday session when 1-minute data exists, else a 20-day
volume-weighted average. These are CONTEXT readings: not tested on this desk.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def _ema(s, n):
    return s.ewm(span=n, adjust=False).mean()


def _atr(h, l, c, n=14):
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


def supertrend(h, l, c, n=10, mult=3.0):
    atr = _atr(h, l, c, n).values
    hl2 = ((h + l) / 2).values
    ub, lb = hl2 + mult * atr, hl2 - mult * atr
    fu, fl = ub.copy(), lb.copy()
    d = np.ones(len(c), dtype=int)
    cv = c.values
    for i in range(1, len(c)):
        fu[i] = ub[i] if (ub[i] < fu[i - 1] or cv[i - 1] > fu[i - 1]) else fu[i - 1]
        fl[i] = lb[i] if (lb[i] > fl[i - 1] or cv[i - 1] < fl[i - 1]) else fl[i - 1]
        if d[i - 1] == 1:
            d[i] = -1 if cv[i] < fl[i] else 1
        else:
            d[i] = 1 if cv[i] > fu[i] else -1
    line = np.where(d == 1, fl, fu)
    return d, line


def psar(h, l, step=0.02, mx=0.2):
    hv, lv = h.values, l.values
    n = len(hv)
    sar = np.zeros(n)
    up = True
    af, ep = step, hv[0]
    sar[0] = lv[0]
    trend = np.ones(n, dtype=int)
    for i in range(1, n):
        s = sar[i - 1] + af * (ep - sar[i - 1])
        if up:
            s = min(s, lv[i - 1], lv[i - 2] if i >= 2 else lv[i - 1])
            if lv[i] < s:
                up, s, ep, af = False, ep, lv[i], step
            elif hv[i] > ep:
                ep, af = hv[i], min(mx, af + step)
        else:
            s = max(s, hv[i - 1], hv[i - 2] if i >= 2 else hv[i - 1])
            if hv[i] > s:
                up, s, ep, af = True, ep, hv[i], step
            elif lv[i] < ep:
                ep, af = lv[i], min(mx, af + step)
        sar[i] = s
        trend[i] = 1 if up else -1
    return trend, sar


def _since(arr):
    """bars since the last change of sign in arr (int array)."""
    k = 0
    for v in arr[::-1]:
        if v != arr[-1]:
            break
        k += 1
    return k


def items(bars: pd.DataFrame, intraday: pd.DataFrame | None, item, fmt=lambda v: f"{v:,.2f}"):
    """bars: daily OHLCV (index = date). item: the _item() helper from the exporter."""
    b = bars.dropna(subset=["close"])
    if len(b) < 60:
        return []
    h, l, c = b["high"].astype(float), b["low"].astype(float), b["close"].astype(float)
    px = float(c.iloc[-1])
    atr = float(_atr(h, l, c).iloc[-1]) or px * 0.01
    out = []
    # 1. EMA 9/21/50 stack
    e9, e21, e50 = (float(_ema(c, n).iloc[-1]) for n in (9, 21, 50))
    sc = (np.sign(px - e9) + np.sign(e9 - e21) + np.sign(e21 - e50)) / 3
    word = ("bull stack (price > 9 > 21 > 50)" if sc == 1 else "bear stack (price < 9 < 21 < 50)" if sc == -1 else "mixed")
    out.append(item("EMA 9/21/50 stack", sc, f"{word} · {fmt(e9)} / {fmt(e21)} / {fmt(e50)}",
                    "all in order up = +1, all in order down = -1"))
    # 2. SMA 50/200
    if len(c) >= 200:
        s50, s200 = float(c.rolling(50).mean().iloc[-1]), float(c.rolling(200).mean().iloc[-1])
        cross = np.sign(c.rolling(50).mean() - c.rolling(200).mean()).dropna().astype(int).values
        sc = 0.6 * np.sign(s50 - s200) + 0.4 * np.sign(px - s200)
        out.append(item("SMA 50/200", sc, f"{'golden' if s50 > s200 else 'death'} cross {_since(cross)} bars ago · 50 {fmt(s50)} / 200 {fmt(s200)}",
                        "50 above 200 = +0.6, price above 200 = +0.4"))
    # 3. Supertrend (10, 3)
    d, line = supertrend(h, l, c)
    out.append(item("Supertrend (10, 3)", float(d[-1]), f"{'up' if d[-1] == 1 else 'down'} for {_since(d)} bars · line {fmt(line[-1])}",
                    "up = +1, down = -1"))
    # 4. Ichimoku cloud (9, 26, 52)
    if len(c) >= 80:
        tk = (h.rolling(9).max() + l.rolling(9).min()) / 2
        kj = (h.rolling(26).max() + l.rolling(26).min()) / 2
        sa = ((tk + kj) / 2).shift(26)
        sb = ((h.rolling(52).max() + l.rolling(52).min()) / 2).shift(26)
        top, bot = max(sa.iloc[-1], sb.iloc[-1]), min(sa.iloc[-1], sb.iloc[-1])
        pos = 1 if px > top else -1 if px < bot else 0
        sc = 0.7 * pos + 0.3 * np.sign(tk.iloc[-1] - kj.iloc[-1])
        where = "above" if pos == 1 else "below" if pos == -1 else "inside"
        out.append(item("Ichimoku cloud", sc, f"price {where} cloud ({fmt(bot)}-{fmt(top)}) · tenkan {'>' if tk.iloc[-1] > kj.iloc[-1] else '<'} kijun",
                        "above cloud = +0.7, tenkan over kijun = +0.3"))
    # 5. Bollinger position (20, 2)
    m20, sd = c.rolling(20).mean().iloc[-1], c.rolling(20).std().iloc[-1]
    if sd and sd > 0:
        pb = (px - (m20 - 2 * sd)) / (4 * sd)
        out.append(item("Bollinger position (20, 2)", (pb - 0.5) * 2, f"%B {pb:.2f} · band {fmt(m20 - 2 * sd)}-{fmt(m20 + 2 * sd)}",
                        "upper band = +1, middle = 0, lower band = -1"))
    # 6. VWAP
    vw, src = None, None
    try:
        if intraday is not None and len(intraday) and "volume" in intraday:
            last = intraday[intraday["day"] == intraday["day"].iloc[-1]]
            tp = (last["high"] + last["low"] + last["close"]) / 3
            if last["volume"].sum() > 0:
                vw, src, px_i = float((tp * last["volume"]).sum() / last["volume"].sum()), "session VWAP", float(last["close"].iloc[-1])
    except Exception:
        vw = None
    if vw is None and "volume" in b and b["volume"].tail(20).fillna(0).sum() > 0:
        t = b.tail(20)
        tp = (t["high"] + t["low"] + t["close"]) / 3
        vw, src, px_i = float((tp * t["volume"]).sum() / t["volume"].sum()), "20-day VWAP", px
    if vw is not None:
        out.append(item("Above / below VWAP", (px_i - vw) / (0.5 * atr), f"{'above' if px_i >= vw else 'below'} {src} {fmt(vw)} ({(px_i / vw - 1) * 100:+.2f}%)",
                        "half an ATR above = +1"))
    # 7. Parabolic SAR (0.02, 0.2)
    tr, sar = psar(h, l)
    out.append(item("Parabolic SAR", float(tr[-1]), f"{'below price (up)' if tr[-1] == 1 else 'above price (down)'} for {_since(tr)} bars · {fmt(sar[-1])}",
                    "SAR below price = +1"))
    for i in out:
        i["classic"] = True
    return out


# ---------------------------------------------------------------------------------------------------------
# Per-timeframe section scores (2026-10-21): same sections as the Impact scores, on any bar size.
# Returns -10..+10 per section and a weighted overall (Trend 30, Flow 20, MAs 10, Trend signals 10, Price pos 5;
# Positioning and Timeframe agreement are daily-only / cross-timeframe so they are not part of a single TF).
# ---------------------------------------------------------------------------------------------------------
TF_W = {"trend": 40, "flow": 30}      # 2026-10-23: one Trend section (direction + momentum); price position and strength are context


def _adx(h, l, c, n=14):
    up, dn = h.diff(), -l.diff()
    pdm = pd.Series(np.where((up > dn) & (up > 0), up, 0.0), index=c.index)
    ndm = pd.Series(np.where((dn > up) & (dn > 0), dn, 0.0), index=c.index)
    atr = _atr(h, l, c, n)
    pdi = 100 * pdm.ewm(alpha=1 / n, adjust=False).mean() / atr
    ndi = 100 * ndm.ewm(alpha=1 / n, adjust=False).mean() / atr
    dx = 100 * (pdi - ndi).abs() / (pdi + ndi).replace(0, np.nan)
    return dx.ewm(alpha=1 / n, adjust=False).mean()


def tf_scores(bars: pd.DataFrame, intraday: bool, max_bars: int = 700) -> dict | None:
    b = bars.dropna(subset=["close"]).tail(max_bars)
    if len(b) < 60:
        return None
    h, l, c = b["high"].astype(float), b["low"].astype(float), b["close"].astype(float)
    cl = lambda v: None if v is None or not np.isfinite(v) else float(max(-1.0, min(1.0, v)))
    mean = lambda xs: None if not [x for x in xs if x is not None] else float(np.mean([x for x in xs if x is not None]))
    r = np.log(c).diff()
    px = float(c.iloc[-1])
    out = {}
    # Trend: 20-bar return vs volatility, price vs 200-bar EMA, efficiency, ADX
    ret20 = float(c.iloc[-1] / c.iloc[-21] - 1)
    vol20 = float(r.tail(20).std() * np.sqrt(20))
    sgn = 1 if ret20 > 0 else -1
    path = float(c.diff().abs().tail(20).sum())
    er = abs(float(c.iloc[-1] - c.iloc[-21])) / path if path > 0 else 0
    adx = float(_adx(h, l, c).iloc[-1])
    ema200 = float(_ema(c, 200).iloc[-1])
    sd200 = float(r.tail(200).std() * np.sqrt(200)) or 0.1
    # Trend (2026-10-23) = half long-term direction (price vs 200 EMA, SMA 50/200) + half medium-term momentum
    # (20-bar return, EMA 9/21/50 stack, one vote of Supertrend / PSAR / Ichimoku). Strength (ADX + efficiency) is a 0..10 label.
    e9, e21, e50 = (float(_ema(c, n).iloc[-1]) for n in (9, 21, 50))
    direction = [cl((px / ema200 - 1) / sd200)]
    if len(c) >= 200:
        s50, s200 = float(c.rolling(50).mean().iloc[-1]), float(c.rolling(200).mean().iloc[-1])
        direction.append(cl(0.6 * np.sign(s50 - s200) + 0.4 * np.sign(px - s200)))
    momentum = [cl(ret20 / vol20 if vol20 > 0 else 0), cl((np.sign(px - e9) + np.sign(e9 - e21) + np.sign(e21 - e50)) / 3)]
    strength = mean([max(0.0, min(1.0, (adx - 15) / 25)) if np.isfinite(adx) else None, min(1.0, er / 0.6)])
    # Trend-following signals
    d, _ = supertrend(h, l, c)
    pt, _ = psar(h, l)
    its = [float(d[-1]), float(pt[-1])]
    if len(c) >= 80:
        tk = (h.rolling(9).max() + l.rolling(9).min()) / 2
        kj = (h.rolling(26).max() + l.rolling(26).min()) / 2
        sa = ((tk + kj) / 2).shift(26).iloc[-1]
        sb = ((h.rolling(52).max() + l.rolling(52).min()) / 2).shift(26).iloc[-1]
        pos = 1 if px > max(sa, sb) else -1 if px < min(sa, sb) else 0
        its.append(0.7 * pos + 0.3 * np.sign(tk.iloc[-1] - kj.iloc[-1]))
    momentum.append(mean([cl(x) for x in its]))
    out["trend"] = mean([mean(direction), mean(momentum)])
    # Price position: Bollinger %B and VWAP (session VWAP on intraday bars, 20-bar VWAP otherwise)
    m20, sd = float(c.rolling(20).mean().iloc[-1]), float(c.rolling(20).std().iloc[-1])
    its = [cl(((px - (m20 - 2 * sd)) / (4 * sd) - 0.5) * 2) if sd > 0 else None]
    v = b["volume"].astype(float) if "volume" in b else None
    has_v = v is not None and v.tail(50).fillna(0).gt(0).mean() > 0.8
    atr = float(_atr(h, l, c).iloc[-1]) or px * 0.01
    if has_v:
        tp = (h + l + c) / 3
        if intraday:
            day = b.index.date
            last = day == day[-1]
            vw = float((tp[last] * v[last]).sum() / v[last].sum()) if v[last].sum() > 0 else None
        else:
            vw = float((tp.tail(20) * v.tail(20)).sum() / v.tail(20).sum()) if v.tail(20).sum() > 0 else None
        if vw:
            its.append(cl((px - vw) / (0.5 * atr)))
        if "delta" in b and b["delta"].tail(20).notna().all():
            # real taker buy - sell volume (Binance) over the last 20 bars; 10% net buying = +1
            out["flow"] = cl(float(b["delta"].tail(20).sum()) / float(v.tail(20).sum()) / 0.10)
            out["flow_src"] = "real buy/sell volume (Binance)"
        else:
            upv = float(v.where(r > 0, 0).tail(20).sum())
            dnv = float(v.where(r < 0, 0).tail(20).sum())
            out["flow"] = cl(np.log(max(1e-6, upv / dnv)) / np.log(2)) if dnv > 0 and upv > 0 else None
    else:
        out["flow"] = None
    pp = mean(its)
    fsrc = out.pop("flow_src", "up-bar vs down-bar volume")
    have = {k: x for k, x in out.items() if x is not None and k in TF_W}
    ws = sum(TF_W[k] for k in have)
    res = {k: (None if x is None else round(x * 10, 1)) for k, x in out.items()}
    res["overall"] = round(sum(TF_W[k] * x for k, x in have.items()) / ws * 10, 1) if ws else None
    res["weights"] = {k: round(TF_W[k] / ws * 100) for k in have} if ws else {}
    res["flow_src"] = fsrc if out.get("flow") is not None else None
    res["price_pos"] = None if pp is None else round(pp * 10, 1)            # timing context, not in the overall
    res["strength"] = None if strength is None else round(strength * 10, 1)  # 0 = choppy .. 10 = clean trend, no direction
    return res


BN_SYM = {k: k + "USDT" for k in ("BTC", "ETH", "SOL", "BNB", "XRP", "UNI", "ZEC", "LINK", "DASH", "AVAX")}   # 2026-11-03: + six coins
BN_IV = {"5m": "5m", "15m": "15m", "4h": "4h"}


def binance_bars(market: str, tf: str, limit: int = 700) -> pd.DataFrame | None:
    """Live Binance USD-M futures klines with taker-buy volume -> OHLCV + delta (buy - sell). None on failure."""
    if market not in BN_SYM or tf not in BN_IV:
        return None
    import urllib.request, json as _j
    url = f"https://fapi.binance.com/fapi/v1/klines?symbol={BN_SYM[market]}&interval={BN_IV[tf]}&limit={limit}"
    try:
        with urllib.request.urlopen(url, timeout=8) as r:
            k = _j.loads(r.read())
    except Exception:
        return None
    if not k:
        return None
    df = pd.DataFrame(k, columns=["t", "open", "high", "low", "close", "volume", "ct", "qv", "n", "buy", "qbuy", "x"])
    df = df.iloc[:-1]                                   # drop the bar still forming
    for c_ in ("open", "high", "low", "close", "volume", "buy"):
        df[c_] = df[c_].astype(float)
    df["delta"] = 2 * df["buy"] - df["volume"]
    idx = pd.to_datetime(df["t"], unit="ms", utc=True).dt.tz_convert("Asia/Kolkata")
    out = df[["open", "high", "low", "close", "volume", "delta"]].set_index(pd.DatetimeIndex(idx))
    out["available_at"] = pd.to_datetime(df["ct"].values + 1000, unit="ms", utc=True).tz_convert("Asia/Kolkata")
    return out
