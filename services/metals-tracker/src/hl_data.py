"""Hyperliquid public market-data client.

Wraps the Hyperliquid `/info` POST endpoint for everything the tracker needs —
universe metadata, historical candles, live OI / funding / mark context, and
funding history. All of this is PUBLIC (no wallet, no API key), because the
tracker is an analysis tool that never places an order.

Kept dependency-light on purpose: plain `requests`, no SDK. (The live websocket
lives in feed.py.) This mirrors the read-only `Info` client in the user's
hyperliquid-trading-agent, but with zero auth surface.
"""
from __future__ import annotations

import time as _time

import requests

DEFAULT_INFO_URL = "https://api.hyperliquid.xyz/info"
EXTRA_DEXES = ["xyz"]          # HIP-3 builder dex with GOLD / SILVER / EUR / JPY / GBP perps (2026-10-23)

_INTERVAL_MS = {
    "1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000,
    "30m": 1_800_000, "1h": 3_600_000, "2h": 7_200_000, "4h": 14_400_000,
    "8h": 28_800_000, "12h": 43_200_000, "1d": 86_400_000,
}


def candle_minutes_to_interval(cm: int) -> str:
    m = {1: "1m", 3: "3m", 5: "5m", 15: "15m", 30: "30m", 60: "1h"}
    if cm in m:
        return m[cm]
    nearest = min(m, key=lambda x: abs(x - cm))
    return m[nearest]


class HLData:
    def __init__(self, info_url: str = DEFAULT_INFO_URL, timeout: float = 10.0):
        self.info_url = info_url
        self.timeout = timeout
        self._session = requests.Session()
        self._meta_cache = None

    # ---- low-level -------------------------------------------------------
    def _post(self, payload: dict, attempts: int = 3):
        last = None
        for i in range(attempts):
            try:
                r = self._session.post(self.info_url, json=payload,
                                       timeout=self.timeout)
                r.raise_for_status()
                return r.json()
            except Exception as e:  # network / rate-limit
                last = e
                _time.sleep(0.4 * (2 ** i))
        raise RuntimeError(f"Hyperliquid /info failed: {last}")

    # ---- metadata --------------------------------------------------------
    def meta(self) -> dict:
        if self._meta_cache is None:
            self._meta_cache = self._post({"type": "meta"})
        return self._meta_cache

    def universe(self) -> dict:
        """{coin: {'szDecimals': int, 'maxLeverage': int}} for the main perp dex + EXTRA_DEXES."""
        out = {}
        unis = list(self.meta().get("universe", []))
        for dx in EXTRA_DEXES:
            try:
                unis += self._post({"type": "meta", "dex": dx}).get("universe", [])
            except Exception as e:
                print(f"[hl] meta {dx} failed: {e}")
        for u in unis:
            out[u.get("name")] = {
                "szDecimals": int(u.get("szDecimals", 4)),
                "maxLeverage": int(u.get("maxLeverage", 20)),
            }
        return out

    # ---- live context (OI / funding / mark) ------------------------------
    def contexts(self) -> dict:
        """{coin: {markPx, oraclePx, funding, openInterest, prevDayPx, dayNtlVlm}}.

        funding is the current HOURLY funding rate (as a decimal, e.g. 0.0000125).
        openInterest is in coin units.
        """
        data = self._post({"type": "metaAndAssetCtxs"})
        if not (isinstance(data, list) and len(data) >= 2):
            return {}
        meta, ctxs = data[0], list(data[1])
        universe = list(meta.get("universe", []))
        for dx in EXTRA_DEXES:
            try:
                d2 = self._post({"type": "metaAndAssetCtxs", "dex": dx})
                universe += d2[0].get("universe", []); ctxs += d2[1]
            except Exception as e:
                print(f"[hl] ctx {dx} failed: {e}")
        out = {}
        for i, u in enumerate(universe):
            if i >= len(ctxs):
                break
            c = ctxs[i] or {}
            name = u.get("name")
            out[name] = {
                "markPx": _f(c.get("markPx")),
                "oraclePx": _f(c.get("oraclePx")),
                "midPx": _f(c.get("midPx")),
                "funding": _f(c.get("funding")),
                "openInterest": _f(c.get("openInterest")),
                "prevDayPx": _f(c.get("prevDayPx")),
                "dayNtlVlm": _f(c.get("dayNtlVlm")),
            }
        return out

    # ---- candles ---------------------------------------------------------
    def candles(self, coin: str, interval: str = "5m", count: int = 300,
                start_ms: int | None = None, end_ms: int | None = None) -> list[dict]:
        """Return [{t(ms), o, h, l, c, v}] ascending for a perp coin."""
        iv_ms = _INTERVAL_MS.get(interval, 300_000)
        end = end_ms if end_ms is not None else int(_time.time() * 1000)
        start = start_ms if start_ms is not None else end - count * iv_ms
        raw = self._post({
            "type": "candleSnapshot",
            "req": {"coin": coin, "interval": interval,
                    "startTime": start, "endTime": end},
        })
        out = []
        for k in raw or []:
            out.append({
                "t": int(k.get("t")),
                "o": _f(k.get("o")), "h": _f(k.get("h")),
                "l": _f(k.get("l")), "c": _f(k.get("c")),
                "v": _f(k.get("v")),        # base-asset (coin) volume
            })
        out.sort(key=lambda x: x["t"])
        return out

    def daily_candles(self, coin: str, days: int = 60) -> list[dict]:
        end = int(_time.time() * 1000)
        start = end - days * _INTERVAL_MS["1d"]
        return self.candles(coin, "1d", count=days + 2, start_ms=start, end_ms=end)

    # ---- funding history -------------------------------------------------
    def funding_history(self, coin: str, hours: int = 48) -> list[dict]:
        start = int(_time.time() * 1000) - hours * 3_600_000
        raw = self._post({"type": "fundingHistory", "coin": coin,
                          "startTime": start})
        return [{"time": int(r.get("time")), "funding": _f(r.get("fundingRate")),
                 "premium": _f(r.get("premium"))} for r in (raw or [])]


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None
