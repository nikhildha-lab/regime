"""Seed today's session history on a LIVE start.

If you launch the tracker mid-session, the candle history would otherwise begin
at launch time — so the Initial Balance, VWAP, opening range, volume/market
profile and structure would all be wrong. This backfills today's 5-min candles
from 09:15 up to now (spot + near futures, incl. futures OI) so the full day is
present the moment you start, then the live websocket continues from here.

Uses the same Kite client as the live feed. If seeding fails (no historical
entitlement, etc.), the app still starts — just without the backfill.
"""
from __future__ import annotations

import datetime as dt

from .candles import Candle, floor_to_bucket


def _as_dt(v):
    if isinstance(v, dt.datetime):
        return v.replace(tzinfo=None)
    try:
        return dt.datetime.fromisoformat(str(v)[:19]).replace(tzinfo=None)
    except Exception:
        return None


_KITE_INTERVALS = {1: "minute", 3: "3minute", 5: "5minute", 10: "10minute",
                   15: "15minute", 30: "30minute", 60: "60minute"}


def kite_interval(cm: int) -> str:
    """Map candle_minutes to a valid Kite historical interval (nearest supported)."""
    if cm in _KITE_INTERVALS:
        return _KITE_INTERVALS[cm]
    supported = sorted(_KITE_INTERVALS)
    nearest = min(supported, key=lambda x: abs(x - cm))
    print(f"[seed] candle_minutes={cm} not a native Kite interval; seeding at "
          f"{nearest}min (Kite supports {supported})")
    return _KITE_INTERVALS[nearest]


def seed_today(kite, resolved, state, cfg, now=None):
    cm = cfg.candle_minutes
    interval = kite_interval(cm)
    now = now or dt.datetime.now()
    today = now.date()
    frm = dt.datetime.combine(today, cfg.market_open)
    if now <= frm:
        return
    cur_bucket = floor_to_bucket(now, cm)   # skip the in-progress bar; live builds it
    chart_bars = int(cfg.a("dashboard", "chart_bars", default=96))

    for key, ri in resolved.items():
        st = state.instruments.get(key)
        if st is None:
            continue
        # spot (index: no volume/oi)
        try:
            bars = kite.historical_data(ri.spot_token, frm, now, interval)
            n = 0
            for c in bars:
                start = _as_dt(c["date"])
                if start is None or start >= cur_bucket:
                    continue
                st.spot.completed.append(
                    Candle(start, c["open"], c["high"], c["low"], c["close"], 0.0, 0.0))
                n += 1
            if st.spot.completed:
                st.last_spot = st.spot.completed[-1].close
            print(f"[seed] {key} spot: {n} morning candles (09:15→now)")
        except Exception as e:
            print(f"[seed] {key} spot failed ({e}) — starting from now")
        # near futures (volume + OI)
        try:
            bars = kite.historical_data(ri.fut_token, frm, now, interval, oi=True)
            n = 0
            for c in bars:
                start = _as_dt(c["date"])
                if start is None or start >= cur_bucket:
                    continue
                st.fut.completed.append(
                    Candle(start, c["open"], c["high"], c["low"], c["close"],
                           float(c.get("volume", 0) or 0), float(c.get("oi", 0) or 0)))
                n += 1
            if st.fut.completed:
                st.last_fut = st.fut.completed[-1].close
            print(f"[seed] {key} fut: {n} morning candles (with volume + OI)")
        except Exception as e:
            print(f"[seed] {key} fut failed ({e}) — volume profile builds from now")
        # display-only chart series: fixed window, reaches back across sessions
        try:
            if getattr(st, "chart_base", None) is not None:
                nb = 0
                for c in kite.historical_data(ri.spot_token,
                                              now - dt.timedelta(days=8), now, interval):
                    start = _as_dt(c["date"])
                    if start is None or start >= cur_bucket:
                        continue
                    st.chart_base.completed.append(
                        Candle(start, c["open"], c["high"], c["low"], c["close"], 0.0, 0.0))
                    nb += 1
                del st.chart_base.completed[:-(chart_bars * 2)]
                print(f"[seed] {key} chart {cm}m: {nb} candles (multi-day)")
            if getattr(st, "chart5", None) is not None:
                cur5 = floor_to_bucket(now, 5)
                n5 = 0
                for c in kite.historical_data(ri.spot_token,
                                              now - dt.timedelta(days=4), now, "5minute"):
                    start = _as_dt(c["date"])
                    if start is None or start >= cur5:
                        continue
                    st.chart5.completed.append(
                        Candle(start, c["open"], c["high"], c["low"], c["close"], 0.0, 0.0))
                    n5 += 1
                del st.chart5.completed[:-(chart_bars * 2)]
                print(f"[seed] {key} chart 5m: {n5} candles (multi-day)")
        except Exception as e:
            print(f"[seed] {key} chart series failed ({e}) — builds from now")


def _approx_bins(low, high, bw, buy, sell):
    """Spread a bar's buy/sell volume across price bins — buys stacked toward the
    high, sells toward the low — so a rebuilt historical bar renders plausibly."""
    import math
    lo_b = math.floor(low / bw) * bw
    hi_b = math.floor(high / bw) * bw
    bins = {}
    prices = []
    p = lo_b
    while p <= hi_b + 1e-9:
        prices.append(round(p, 4))
        p += bw
    if not prices:
        prices = [round(lo_b, 4)]
    rng = (high - low) or 1.0
    w_up = {p: max(0.0, (p - low) / rng) for p in prices}    # near high
    w_dn = {p: max(0.0, (high - p) / rng) for p in prices}   # near low
    su = sum(w_up.values()) or 1.0
    sd = sum(w_dn.values()) or 1.0
    for p in prices:
        bins[p] = (buy * w_up[p] / su, sell * w_dn[p] / sd)
    return bins


def seed_footprint(state, cfg):
    """Rebuild an APPROXIMATE order-flow footprint for 09:15→now from the seeded
    futures candles, so the morning history is present the moment you (re)open the
    app. Delta sign per bar comes from the close's position in its range (the same
    proxy the imbalance module uses); real tick-classified flow continues live.
    Historical bars are flagged 'approx' so the UI can mark them.
    """
    for key, st in state.instruments.items():
        book = st.footprint
        if book is None:
            continue
        n = 0
        for c in st.fut.completed:
            if c.volume <= 0:
                continue
            rng = c.high - c.low
            frac_up = ((c.close - c.low) / rng) if rng > 0 else 0.5
            buy = c.volume * frac_up
            sell = c.volume * (1 - frac_up)
            bins = _approx_bins(c.low, c.high, book.bw, buy, sell)
            book.seed_bar(c.start, c.open, c.high, c.low, c.close, buy, sell,
                          bins, approx=True)
            n += 1
        print(f"[seed] {key} footprint: {n} morning bars rebuilt (approx flow)")


def fetch_daily(kite, resolved, state, cfg, days=150, now=None):
    """Fetch recent DAILY candles per instrument for the 1D / 1W chart views."""
    now = now or dt.datetime.now()
    frm = now - dt.timedelta(days=days)
    for key, ri in resolved.items():
        st = state.instruments.get(key)
        if st is None:
            continue
        try:
            bars = kite.historical_data(ri.spot_token, frm, now, "day")
            out = []
            for c in bars:
                d = _as_dt(c["date"])
                if d is None:
                    continue
                out.append({"d": d.strftime("%d-%b"), "iso": d.strftime("%Y-%m-%d"),
                            "o": round(c["open"], 2),
                            "h": round(c["high"], 2), "l": round(c["low"], 2),
                            "c": round(c["close"], 2)})
            st.daily = out
            print(f"[seed] {key} daily: {len(out)} days (for 1D/1W chart)")
        except Exception as e:
            print(f"[seed] {key} daily failed ({e}) — 1D/1W views unavailable")


def reseed_charts(kite, resolved, state, cfg, now=None):
    """On-demand (dashboard button): re-fetch the display chart series so every
    chart shows the full defined history (dashboard.chart_bars candles across
    sessions). Spot/fut analytics series are untouched; daily is refreshed."""
    cm = cfg.candle_minutes
    interval = kite_interval(cm)
    now = now or dt.datetime.now()
    chart_bars = int(cfg.a("dashboard", "chart_bars", default=96))
    cur = floor_to_bucket(now, cm)
    cur5 = floor_to_bucket(now, 5)
    msgs = []
    for key, ri in resolved.items():
        st = state.instruments.get(key)
        if st is None:
            continue
        try:
            if getattr(st, "chart_base", None) is not None:
                fresh = []
                for c in kite.historical_data(ri.spot_token,
                                              now - dt.timedelta(days=8), now, interval):
                    b = _as_dt(c["date"])
                    if b is None or b >= cur:
                        continue
                    fresh.append(Candle(b, c["open"], c["high"], c["low"], c["close"], 0.0, 0.0))
                fresh = fresh[-(chart_bars * 2):]
                with state.lock:
                    st.chart_base.completed = fresh
                msgs.append(f"{key} {len(fresh)}x{cm}m")
        except Exception as e:
            msgs.append(f"{key} {cm}m failed: {e}")
        try:
            if getattr(st, "chart5", None) is not None:
                fresh5 = []
                for c in kite.historical_data(ri.spot_token,
                                              now - dt.timedelta(days=4), now, "5minute"):
                    b = _as_dt(c["date"])
                    if b is None or b >= cur5:
                        continue
                    fresh5.append(Candle(b, c["open"], c["high"], c["low"], c["close"], 0.0, 0.0))
                fresh5 = fresh5[-(chart_bars * 2):]
                with state.lock:
                    st.chart5.completed = fresh5
                msgs.append(f"{len(fresh5)}x5m")
        except Exception as e:
            msgs.append(f"{key} 5m failed: {e}")
    try:
        fetch_daily(kite, resolved, state, cfg)
        msgs.append("daily ok")
    except Exception as e:
        msgs.append(f"daily failed: {e}")
    out = " · ".join(msgs)
    print(f"[reseed] {out}")
    return out
