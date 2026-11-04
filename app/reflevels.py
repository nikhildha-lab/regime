"""REFERENCE LEVELS for 24/7 markets (2026-11-04, Nikhil: crypto, metals and commodities need VAH / VAL, IB, PDH / PDL,
PWH / PWL ... on the block line chart, like Nifty).

Days are UTC days (crypto and the Hyperliquid metal / energy perps trade round the clock):
  VAH / VAL  - 5-day value area (70 % of volume) from 1-hour bars (typical price x volume)
  IBH / IBL  - initial balance = first hour of the current UTC day (00:00-01:00); the previous day's once today's first hour
               is not complete yet
  PDH / PDL  - previous UTC day high / low;  PWH / PWL - previous ISO week;  PMH / PML - previous calendar month
Sources: Binance USD-M futures 1-hour klines for the coins, Hyperliquid xyz perps 1-hour candles for metals / energy.
Same output shape as levelmap.build()['levels']. Context only.
"""
from __future__ import annotations

import json
import time
import urllib.request
from datetime import datetime, timedelta, timezone

import pandas as pd

BN = {"BTC", "ETH", "SOL", "BNB", "XRP", "UNI", "ZEC", "LINK", "DASH"}
HL = {"GOLD": "xyz:GOLD", "SILVER": "xyz:SILVER", "COPPER": "xyz:COPPER", "CRUDE": "xyz:CL", "NATGAS": "xyz:NATGAS",
      "ALUMINUM": "xyz:ALUMINIUM", "BRENT": "xyz:BRENTOIL"}
_C: dict = {}


def _get(url, body=None, timeout=10):
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"})
    return json.loads(urllib.request.urlopen(req, timeout=timeout).read())


def _bars(key: str) -> pd.DataFrame:
    start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=62)
    ms = int(start.timestamp() * 1000)
    if key in BN:
        rows = []
        st = ms
        for _ in range(3):                                    # 1000 bars a call; 62 days = ~1500 bars
            k = _get(f"https://fapi.binance.com/fapi/v1/klines?symbol={key}USDT&interval=1h&startTime={st}&limit=1000")
            if not k:
                break
            rows += k
            st = int(k[-1][0]) + 3600_000
            if len(k) < 1000:
                break
        d = pd.DataFrame([[int(r[0]), float(r[2]), float(r[3]), float(r[4]), float(r[5])] for r in rows], columns=["t", "high", "low", "close", "volume"])
    elif key in HL:
        k = _get("https://api.hyperliquid.xyz/info", {"type": "candleSnapshot", "req": {"coin": HL[key], "interval": "1h", "startTime": ms,
                                                                                        "endTime": int(time.time() * 1000)}})
        d = pd.DataFrame([[int(r["t"]), float(r["h"]), float(r["l"]), float(r["c"]), float(r["v"])] for r in k], columns=["t", "high", "low", "close", "volume"])
    else:
        return pd.DataFrame()
    d["ts"] = pd.to_datetime(d.t, unit="ms", utc=True)
    return d.drop_duplicates("t").set_index("ts").sort_index()


def build(key: str) -> dict:
    d = _bars(key)
    if d.empty or len(d) < 48:
        return {"key": key, "levels": [], "error": "no bars"}
    spot = float(d.close.iloc[-1])
    now = d.index[-1]
    today = now.normalize()
    out = []
    day = d.resample("1D").agg({"high": "max", "low": "min"}).dropna()
    prev = day[day.index < today]
    if len(prev):
        out += [("PDH", f"previous day high ({prev.index[-1]:%d %b}, UTC)", prev.high.iloc[-1]), ("PDL", f"previous day low ({prev.index[-1]:%d %b}, UTC)", prev.low.iloc[-1])]
    wk_start = today - timedelta(days=today.weekday())
    pw = d[(d.index >= wk_start - timedelta(days=7)) & (d.index < wk_start)]
    if len(pw):
        out += [("PWH", "previous week high", pw.high.max()), ("PWL", "previous week low", pw.low.min())]
    m_start = today.replace(day=1)
    pm_start = (m_start - timedelta(days=1)).replace(day=1)
    pm = d[(d.index >= pm_start) & (d.index < m_start)]
    if len(pm) > 24 * 20:
        out += [("PMH", "previous month high", pm.high.max()), ("PML", "previous month low", pm.low.min())]
    z = d[d.index >= today - timedelta(days=4)]                      # today + the 4 days before = rolling 5 days
    if z.volume.sum() > 0:
        step = spot * 0.001
        tp = ((z.high + z.low + z.close) / 3 / step).round() * step
        v = z.volume.groupby(tp.values).sum().sort_values(ascending=False)
        va = v[(v.cumsum() / v.sum()) <= 0.7].index
        if len(va):
            out += [("VAH", "5-day value area high (rolling volume profile, 1-hour bars)", float(max(va))),
                    ("VAL", "5-day value area low (rolling volume profile, 1-hour bars)", float(min(va)))]
        out.append(("POC", "5-day point of control (price with the most volume, 1-hour bars)", float(v.index[0])))
    zd = d[d.index >= today]                                       # 2026-11-04: VWAP of the current UTC day (yesterday's until today has 1 bar)
    vd = today if len(zd) and zd.volume.sum() > 0 else today - timedelta(days=1)
    zd = d[(d.index >= vd) & (d.index < vd + timedelta(days=1))]
    if len(zd) and zd.volume.sum() > 0:
        out.append(("VWAP", f"VWAP of {vd:%d %b} (UTC day)", float(((zd.high + zd.low + zd.close) / 3 * zd.volume).sum() / zd.volume.sum())))
    ib_day = today if len(d[d.index >= today + timedelta(hours=1)]) else today - timedelta(days=1)
    ib = d[(d.index >= ib_day) & (d.index < ib_day + timedelta(hours=1))]
    if len(ib):
        out += [("IBH", f"initial balance high {ib_day:%d %b} (00:00-01:00 UTC)", ib.high.max()), ("IBL", f"initial balance low {ib_day:%d %b} (00:00-01:00 UTC)", ib.low.min())]
    lv = [{"short": a, "name": n, "px": round(float(x), 6 if x < 10 else 4 if x < 1000 else 2), "dist_pct": round(100 * (float(x) / spot - 1), 2)} for a, n, x in out if x == x and x]
    return {"key": key, "spot": spot, "levels": lv, "source": "Binance futures" if key in BN else "Hyperliquid " + HL[key], "at": datetime.now().strftime("%Y-%m-%d %H:%M")}


def get(key: str) -> dict:
    x = _C.get(key)
    if x and time.time() - x[0] < 300:
        return x[1]
    try:
        v = build(key)
    except Exception as e:
        v = (x[1] if x else {"key": key, "levels": []}) | {"error": str(e)[:120]}
    _C[key] = (time.time(), v)
    return v
