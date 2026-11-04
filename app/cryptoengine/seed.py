"""Seed today's session history on a LIVE start.

If you launch the tracker mid-day, the candle history would otherwise begin at
launch time — so the Initial Balance, VWAP, opening range, volume/market profile
and structure would all be wrong. This backfills the current UTC day's 5-min
candles (with real volume) from 00:00 UTC to now, pulls recent daily candles for
the chart + prior-day/week levels, and reads the current OI/funding/mark context
so those panels populate immediately. The websocket then continues from here.

All from Hyperliquid's public API — no keys.
"""
from __future__ import annotations

import datetime as dt
import time as _time

from .candles import Candle, floor_to_bucket


def _dedupe(series):
    """Seeding can race the live feed (background boot): keep one candle per
    bucket, ascending — the live-built copy wins over the API one."""
    seen = {}
    for c in series.completed:
        seen[c.start] = c
    series.completed = [seen[k] for k in sorted(seen)]


def _cl(client, ri):
    """Resolve the data client for an instrument (single client, or a
    {"hl":..., "bn":...} mapping when both sources are live)."""
    if isinstance(client, dict):
        return client.get(getattr(ri, "source", "hl"))
    return client


def _ms(naive_utc):
    """Naive-UTC datetime -> epoch ms. NEVER use .timestamp() directly on naive
    datetimes: Python interprets them in the Mac's LOCAL timezone (IST), which
    silently shifts every API window by -5:30h."""
    return int(naive_utc.replace(tzinfo=dt.timezone.utc).timestamp() * 1000)
from .hl_data import candle_minutes_to_interval


def seed_today(client, resolved, state, cfg, now=None):
    cm = cfg.candle_minutes
    interval = candle_minutes_to_interval(cm)
    now = now or dt.datetime.utcnow()
    day_start = dt.datetime.combine(now.date(), cfg.market_open)
    chart_bars = int(cfg.a("dashboard", "chart_bars", default=96))
    # fetch enough history to fill the fixed chart window even right after the
    # 00:00 UTC day roll (analytics stay day-scoped via the engine filter)
    hist_start = min(day_start, now - dt.timedelta(minutes=cm * chart_bars))
    start_ms = _ms(hist_start)
    end_ms = _ms(now)
    cur_bucket = floor_to_bucket(now, cm)

    # current OI / funding / mark for every coin
    try:
        if isinstance(client, dict):
            ctxs = {}
            for _c in client.values():
                try:
                    ctxs.update(_c.contexts())
                except Exception:
                    pass
        else:
            ctxs = client.contexts()
    except Exception as e:
        ctxs = {}
        print(f"[seed] contexts failed ({e})")

    for key, ri in resolved.items():
        st = state.instruments.get(key)
        if st is None:
            continue
        _time.sleep(0.06)   # pace API calls across many coins
        c = ctxs.get(ri.coin) or {}
        if c:
            state.apply_ctx(ri.coin, mark=c.get("markPx"), funding=c.get("funding"),
                            oi=c.get("openInterest"), day_vol_usd=c.get("dayNtlVlm"),
                            prev_day_px=c.get("prevDayPx"))
        try:
            bars = _cl(client, ri).candles(ri.coin, interval, start_ms=start_ms, end_ms=end_ms)
            n = 0
            for k in bars:
                start = dt.datetime.utcfromtimestamp(k["t"] / 1000.0)
                if start >= cur_bucket:
                    continue                 # skip in-progress bar; live builds it
                st.perp.completed.append(
                    Candle(start, k["o"], k["h"], k["l"], k["c"],
                           float(k.get("v", 0) or 0), float(st.oi or 0)))
                n += 1
            _dedupe(st.perp)
            if st.perp.completed:
                st.last_price = st.last_price or st.perp.completed[-1].close
            print(f"[seed] {key} perp: {n} candles ({cfg.raw['session']['market_open']} UTC → now, with volume)")
        except Exception as e:
            print(f"[seed] {key} perp failed ({e}) — starting from now")
        # display-only 5-min series for the chart
        try:
            if st.chart5 is not None:
                s5 = _ms(now - dt.timedelta(minutes=5 * chart_bars))
                cur5 = floor_to_bucket(now, 5)
                n5 = 0
                for k in _cl(client, ri).candles(ri.coin, "5m", start_ms=s5, end_ms=end_ms):
                    b5 = dt.datetime.utcfromtimestamp(k["t"] / 1000.0)
                    if b5 >= cur5:
                        continue
                    st.chart5.completed.append(
                        Candle(b5, k["o"], k["h"], k["l"], k["c"],
                               float(k.get("v", 0) or 0), 0.0))
                    n5 += 1
                _dedupe(st.chart5)
                print(f"[seed] {key} 5m chart: {n5} candles")
        except Exception as e:
            print(f"[seed] {key} 5m chart failed ({e}) — builds from now")


def _approx_bins(low, high, bw, buy, sell):
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
    w_up = {p: max(0.0, (p - low) / rng) for p in prices}
    w_dn = {p: max(0.0, (high - p) / rng) for p in prices}
    su = sum(w_up.values()) or 1.0
    sd = sum(w_dn.values()) or 1.0
    for p in prices:
        bins[p] = (buy * w_up[p] / su, sell * w_dn[p] / sd)
    return bins


def seed_footprint(state, cfg):
    """Rebuild an APPROXIMATE footprint for the seeded candles (close-in-range proxy).
    Real tick-classified flow continues live; historical bars are flagged approx."""
    for key, st in state.instruments.items():
        book = st.footprint
        if book is None:
            continue
        n = 0
        day0 = dt.datetime.combine(dt.datetime.utcnow().date(), cfg.market_open)
        for c in st.perp.completed:
            if c.start < day0:
                continue              # footprint stays scoped to today (UTC)
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
        print(f"[seed] {key} footprint: {n} bars rebuilt (approx flow)")


def fetch_daily(client, resolved, state, cfg, days=90, now=None):
    """Fetch recent DAILY candles per coin for the chart + prior-day/week levels."""
    for key, ri in resolved.items():
        st = state.instruments.get(key)
        if st is None:
            continue
        _time.sleep(0.06)   # pace API calls across many coins
        try:
            bars = _cl(client, ri).daily_candles(ri.coin, days=days)
            out = []
            for k in bars:
                d = dt.datetime.utcfromtimestamp(k["t"] / 1000.0)
                out.append({"d": d.strftime("%d-%b"), "iso": d.strftime("%Y-%m-%d"),
                            "o": round(k["o"], 2), "h": round(k["h"], 2),
                            "l": round(k["l"], 2), "c": round(k["c"], 2)})
            st.daily = out
            print(f"[seed] {key} daily: {len(out)} days")
        except Exception as e:
            print(f"[seed] {key} daily failed ({e})")


def reseed_charts(client, resolved, state, cfg, now=None):
    """On-demand (dashboard button): re-fetch the chart window from the API so
    every chart shows the full defined history (dashboard.chart_bars candles).
    Replaces completed candles for the base series and the 5-min display series,
    and refreshes daily history. The in-progress live bar is left untouched."""
    cm = cfg.candle_minutes
    interval = candle_minutes_to_interval(cm)
    now = now or dt.datetime.utcnow()
    chart_bars = int(cfg.a("dashboard", "chart_bars", default=96))
    day_start = dt.datetime.combine(now.date(), cfg.market_open)
    hist_start = min(day_start, now - dt.timedelta(minutes=cm * chart_bars))
    end_ms = _ms(now)
    msgs = []
    for key, ri in resolved.items():
        st = state.instruments.get(key)
        if st is None:
            continue
        _time.sleep(0.06)   # pace API calls across many coins
        try:
            cur = floor_to_bucket(now, cm)
            bars = _cl(client, ri).candles(ri.coin, interval,
                                  start_ms=_ms(hist_start), end_ms=end_ms)
            fresh = []
            for k in bars:
                b = dt.datetime.utcfromtimestamp(k["t"] / 1000.0)
                if b >= cur:
                    continue
                fresh.append(Candle(b, k["o"], k["h"], k["l"], k["c"],
                                    float(k.get("v", 0) or 0), float(st.oi or 0)))
            with state.lock:
                st.perp.completed = fresh
                if st.perp.completed:
                    st.last_price = st.last_price or st.perp.completed[-1].close
            msgs.append(f"{key} {len(fresh)}x{cm}m")
        except Exception as e:
            msgs.append(f"{key} {cm}m failed: {e}")
        try:
            if st.chart5 is not None:
                cur5 = floor_to_bucket(now, 5)
                s5 = _ms(now - dt.timedelta(minutes=5 * chart_bars))
                fresh5 = []
                for k in _cl(client, ri).candles(ri.coin, "5m", start_ms=s5, end_ms=end_ms):
                    b = dt.datetime.utcfromtimestamp(k["t"] / 1000.0)
                    if b >= cur5:
                        continue
                    fresh5.append(Candle(b, k["o"], k["h"], k["l"], k["c"],
                                         float(k.get("v", 0) or 0), 0.0))
                with state.lock:
                    st.chart5.completed = fresh5
                msgs.append(f"{len(fresh5)}x5m")
        except Exception as e:
            msgs.append(f"{key} 5m failed: {e}")
    try:
        fetch_daily(client, resolved, state, cfg)
        msgs.append("daily ok")
    except Exception as e:
        msgs.append(f"daily failed: {e}")
    out = " · ".join(msgs)
    print(f"[reseed] {out}")
    return out
