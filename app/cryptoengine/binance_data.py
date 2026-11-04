"""Binance USD-M futures — REST market data client (public, no keys).

Shape-compatible with HLData so the rest of the tracker doesn't care which
exchange a coin comes from:
    universe()       -> {base: {szDecimals, maxLeverage, symbol}}
    candles()        -> [{t(ms), o, h, l, c, v}] ascending
    daily_candles()  -> same, 1d bars
    contexts()       -> {base: {markPx, funding(hourly), openInterest,
                                dayNtlVlm, prevDayPx}}

Notes:
  * `coin` here is the Binance BASE asset as written in config (BTC, 1000PEPE…);
    the traded symbol is f"{coin}USDT".
  * Binance funding is per 8h — converted to an HOURLY rate to match Hyperliquid,
    so oi_analysis annualises identically for both sources.
"""
from __future__ import annotations

import time as _time

import requests

_BASE = "https://fapi.binance.com"

_INTERVALS = {"1m": "1m", "5m": "5m", "15m": "15m", "30m": "30m",
              "1h": "1h", "4h": "4h", "1d": "1d"}
_INTERVAL_MS = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000,
                "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}


def _f(x):
    try:
        return float(x)
    except Exception:
        return None


class BinanceData:
    def __init__(self, base_url: str = _BASE, timeout: float = 10.0):
        self.base = base_url.rstrip("/")
        self.timeout = timeout
        self.s = requests.Session()
        self._uni: dict | None = None

    def _get(self, path, **params):
        r = self.s.get(self.base + path, params=params or None, timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def symbol(self, coin: str) -> str:
        return f"{coin.upper()}USDT"

    # ---- universe --------------------------------------------------------
    def universe(self) -> dict:
        if self._uni is not None:
            return self._uni
        out = {}
        try:
            info = self._get("/fapi/v1/exchangeInfo")
            for s in info.get("symbols", []):
                if (s.get("quoteAsset") == "USDT"
                        and s.get("contractType") == "PERPETUAL"
                        and s.get("status") == "TRADING"):
                    base = s.get("baseAsset")
                    out[base] = {"szDecimals": int(s.get("quantityPrecision", 3)),
                                 "maxLeverage": 50,
                                 "symbol": s.get("symbol")}
        except Exception as e:
            print(f"[binance] exchangeInfo failed ({e})")
        self._uni = out
        return out

    # ---- candles ---------------------------------------------------------
    def candles(self, coin: str, interval: str = "5m", count: int = 300,
                start_ms: int | None = None, end_ms: int | None = None) -> list[dict]:
        iv = _INTERVALS.get(interval, "5m")
        iv_ms = _INTERVAL_MS.get(interval, 300_000)
        end = end_ms if end_ms is not None else int(_time.time() * 1000)
        start = start_ms if start_ms is not None else end - count * iv_ms
        limit = min(1500, max(10, (end - start) // iv_ms + 3))
        raw = self._get("/fapi/v1/klines", symbol=self.symbol(coin), interval=iv,
                        startTime=start, endTime=end, limit=limit)
        out = []
        for k in raw or []:
            out.append({"t": int(k[0]), "o": _f(k[1]), "h": _f(k[2]),
                        "l": _f(k[3]), "c": _f(k[4]), "v": _f(k[5])})
        out.sort(key=lambda x: x["t"])
        return out

    def daily_candles(self, coin: str, days: int = 60) -> list[dict]:
        end = int(_time.time() * 1000)
        return self.candles(coin, "1d", count=days + 2,
                            start_ms=end - days * _INTERVAL_MS["1d"], end_ms=end)

    # ---- live context (mark / funding / OI / volume) ---------------------
    def contexts(self) -> dict:
        out = {}
        try:
            for r in self._get("/fapi/v1/premiumIndex"):
                sym = r.get("symbol", "")
                if not sym.endswith("USDT"):
                    continue
                base = sym[:-4]
                fr = _f(r.get("lastFundingRate"))
                out[base] = {"markPx": _f(r.get("markPrice")),
                             "funding": (fr / 8.0) if fr is not None else None,
                             "openInterest": None, "dayNtlVlm": None,
                             "prevDayPx": None}
        except Exception as e:
            print(f"[binance] premiumIndex failed ({e})")
            return out
        try:
            for r in self._get("/fapi/v1/ticker/24hr"):
                sym = r.get("symbol", "")
                if sym.endswith("USDT") and sym[:-4] in out:
                    out[sym[:-4]]["dayNtlVlm"] = _f(r.get("quoteVolume"))
                    out[sym[:-4]]["prevDayPx"] = _f(r.get("openPrice"))
        except Exception:
            pass
        return out

    def open_interest(self, coin: str):
        try:
            r = self._get("/fapi/v1/openInterest", symbol=self.symbol(coin))
            return _f(r.get("openInterest"))
        except Exception:
            return None
