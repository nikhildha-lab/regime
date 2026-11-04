"""
Live stream for the Market Pulse page (started by updater.ensure_background, no restart needed).

Feeds
  * Binance USD-M futures websocket (true streaming): BTC, ETH, SOL - every trade, today's candle (buy / sell volume),
    top-10 order book. Gives price, change since 00:00 UTC, buyers-minus-sellers (delta) for today / 5 min / 1 min,
    the 5-minute bar in progress and order-book balance.
  * Dhan market quotes (every 2 s in NSE hours): Nifty / Bank Nifty index and near futures with OI and buy / sell queues;
    stock and sector quotes every 60 s (app/dhan_live.py). Replaced Upstox on 2026-10-24.
  * Yahoo (polled every 20 s - Yahoo has no free stream): index futures, Asia, FX, gold, crude, Nifty index (fallback).
  * GIFT Nifty from NSE (every 90 s).
  * The NSE option chain stays on its 3-minute cycle (NSE only updates OI every few minutes); the page is told the
    moment a new snapshot lands.

Browser side: Server-Sent Events on http://127.0.0.1:8052/stream (one JSON snapshot at most once a second) and
http://127.0.0.1:8052/snapshot. Only listens on this computer. Read-only: nothing here places orders.
"""
from __future__ import annotations

import json
import os
import threading
import time
import traceback
from collections import deque
from datetime import datetime, timezone

import pandas as pd

import updater as U

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
OC_LIVE = os.path.join(ROOT, "data_store", "flow", "oc_live.json")
STATUS = os.path.join(ROOT, "data_store", "flow", "stream_status.json")
PORT = 8052
COINS = {k: k + "USDT" for k in ("BTC", "ETH", "SOL", "BNB", "XRP", "UNI", "ZEC", "LINK", "DASH")}   # 2026-11-03: + six coins (Crypto tab)
YAHOO = ["YM=F", "ES=F", "NQ=F", "^N225", "^HSI", "^KS11", "^STOXX50E", "INR=X", "DX-Y.NYB", "BZ=F", "^TNX", "^VIX",
         "BTC-USD", "GC=F", "^NSEI", "^NSEBANK", "SI=F", "HG=F", "CL=F", "SPY", "ALI=F", "NG=F", "000001.SS", "^GSPC", "^DJI", "^BSESN"]
YAHOO_EVERY = 20
# dashboard market -> Yahoo symbol and what it is (for markets not streamed)
Y_MARKET = {"GOLD": ("GC=F", "gold futures"), "SILVER": ("SI=F", "silver futures"), "COPPER": ("HG=F", "copper futures"),
            "CRUDE": ("CL=F", "WTI crude futures"), "ALUMINUM": ("ALI=F", "aluminium futures"), "NATGAS": ("NG=F", "natural gas futures"), "ES": ("ES=F", "S&P 500 futures"), "NQ": ("NQ=F", "Nasdaq-100 futures"),
            "US_STOCKS": ("SPY", "SPY"), "NIFTY": ("^NSEI", "Nifty 50 index"), "BANKNIFTY": ("^NSEBANK", "Bank Nifty index"),
            "SENSEX": ("^BSESN", "Sensex index")}
INDEX_KEYS = {"NSE_INDEX|Nifty 50": "NIFTY", "NSE_INDEX|Nifty Bank": "BANKNIFTY"}


def _now() -> float:
    return time.time()


def _iso(t: float | None) -> str | None:
    return None if t is None else datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _f(x):
    try:
        v = float(x)
        return v if v == v else None
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------------ shared state
class Hub:
    def __init__(self):
        self.cond = threading.Condition()
        self.v = 0
        self.body = b"{}"

    def set(self, data: dict):
        body = json.dumps(data, default=str, separators=(",", ":")).encode()
        with self.cond:
            if body == self.body:
                return
            self.body = body
            self.v += 1
            self.cond.notify_all()

    def wait(self, v: int, timeout: float):
        with self.cond:
            self.cond.wait_for(lambda: self.v != v, timeout)
            return self.v, self.body


HUB = Hub()
FEEDS: dict = {}                    # feed name -> {"state": "live" | "polling" | "off" | "error", "detail", "at"}


def feed(name, state, detail=""):
    FEEDS[name] = {"state": state, "detail": detail, "at": _iso(_now())}


# ------------------------------------------------------------------ Binance (true stream)
class Coin:
    def __init__(self):
        self.last = self.t = None
        self.day = {}                   # today's UTC candle: o h l v buy_v
        self.sec = deque()              # (second, signed taker qty, qty)
        self.bar = None                 # 5-minute bar in progress
        self.book = None                # (bid, ask, imbalance)
        self.lock = threading.Lock()    # trades arrive on the websocket thread, snapshots are read on another

    def trade(self, price, qty, sell_aggr, t_ms):
        with self.lock:
            self._trade(price, qty, sell_aggr, t_ms)

    def _trade(self, price, qty, sell_aggr, t_ms):
        self.last, self.t = price, t_ms / 1000
        s = int(t_ms // 1000)
        sq = -qty if sell_aggr else qty
        if self.sec and self.sec[-1][0] == s:
            a, b, c = self.sec[-1]
            self.sec[-1] = (a, b + sq, c + qty)
        else:
            self.sec.append((s, sq, qty))
        while self.sec and self.sec[0][0] < s - 300:
            self.sec.popleft()
        start = s - s % 300
        if not self.bar or self.bar["start"] != start:
            self.bar = {"start": start, "o": price, "h": price, "l": price, "c": price, "v": 0.0, "d": 0.0}
        b = self.bar
        b["h"], b["l"], b["c"] = max(b["h"], price), min(b["l"], price), price
        b["v"] += qty
        b["d"] += sq

    def snap(self, now):
        with self.lock:
            return self._snap(now)

    def _snap(self, now):
        if self.last is None:
            return None
        d1 = sum(x[1] for x in self.sec if x[0] >= now - 60)
        d5 = sum(x[1] for x in self.sec if x[0] >= now - 300)
        v5 = sum(x[2] for x in self.sec if x[0] >= now - 300)
        o = self.day.get("o")
        dv, bv = self.day.get("v"), self.day.get("buy_v")
        out = {"price": self.last, "at": _iso(self.t), "age_s": round(now - self.t, 1),
               "chg_pct": None if not o else round((self.last / o - 1) * 100, 2), "ref": "since 00:00 UTC",
               "delta_1m": round(d1, 3), "delta_5m": round(d5, 3), "vol_5m": round(v5, 3),
               "delta_day": None if dv is None or bv is None else round(2 * bv - dv, 1),
               "buy_share_day": None if not dv else round(bv / dv * 100, 1),
               "day_hi": self.day.get("h"), "day_lo": self.day.get("l")}
        if self.bar:
            out["bar5"] = {k: (round(v, 4) if isinstance(v, float) else v) for k, v in self.bar.items()}
            out["bar5"]["start"] = _iso(self.bar["start"])
        if self.book:
            out["bid"], out["ask"], out["book_bid_pct"] = self.book
        return out


COIN = {k: Coin() for k in COINS}
BY_SYM = {v: k for k, v in COINS.items()}


def on_binance(msg: dict):
    d = msg.get("data") or msg
    e = d.get("e")
    c = COIN.get(BY_SYM.get(str(d.get("s", "")).upper()))
    if c is None:
        return
    if e == "aggTrade":
        c.trade(float(d["p"]), float(d["q"]), bool(d["m"]), int(d["T"]))
    elif e == "kline":
        k = d["k"]
        c.day = {"o": float(k["o"]), "h": float(k["h"]), "l": float(k["l"]), "v": float(k["v"]), "buy_v": float(k["V"])}
    elif e == "depthUpdate":
        b = [(float(p), float(q)) for p, q in d.get("b", [])]
        a = [(float(p), float(q)) for p, q in d.get("a", [])]
        if b and a:
            bq, aq = sum(q for _, q in b), sum(q for _, q in a)
            c.book = (b[0][0], a[0][0], round(bq / (bq + aq) * 100, 1) if bq + aq else None)


HAS_MARKET_WS = True       # trades + candles have their own connection (binance_market_loop); see stream_hotfix.py
MARKET_URLS = ("wss://fstream.binance.com/market/stream?streams={s}", "wss://stream.binance.com:9443/stream?streams={s}")


def binance_market_loop():
    """Trades + today's candle. Binance moved these futures streams to the /market route (the old address now sends
    only order-book updates); falls back to Binance spot if no trade arrives within 60 s."""
    import websocket
    streams = "/".join(f"{s.lower()}@aggTrade/{s.lower()}@kline_1d" for s in COINS.values())
    i = 0
    while True:
        if globals().get("ENGINE_FEED"):          # 2026-11-01: the crypto engine owns the Binance trade connection now
            feed("binance_market", "live", "via the crypto engine (one Binance connection)")
            time.sleep(30); continue
        url = MARKET_URLS[i % len(MARKET_URLS)].format(s=streams)
        got = {"trade": 0}
        feed("binance_market", "connecting", url.split("?")[0])

        def on_message(ws, m, _got=got, _url=url):
            try:
                msg = json.loads(m)
                if (msg.get("data") or msg).get("e") == "aggTrade":
                    if not _got["trade"]:
                        feed("binance_market", "live", _url.split("?")[0])
                    _got["trade"] += 1
                on_binance(msg)
            except Exception:
                pass

        ws = websocket.WebSocketApp(url, on_message=on_message, on_error=lambda w, e: feed("binance_market", "error", str(e)[:120]))
        t = threading.Thread(target=ws.run_forever, kwargs={"ping_interval": 60, "ping_timeout": 20}, daemon=True)
        t.start()
        t0 = _now()
        while t.is_alive():
            time.sleep(5)
            if globals().get("ENGINE_FEED"):
                ws.close(); break
            if not got["trade"] and _now() - t0 > 60:
                feed("binance_market", "error", f"no trades from {url.split('?')[0]} - trying the next source")
                ws.close()
                break
        i += 0 if got["trade"] else 1
        time.sleep(5)


def binance_loop():
    """Order book (top 10 levels, every 0.5 s)."""
    import websocket
    streams = "/".join(f"{s.lower()}@depth10@500ms" for s in COINS.values())
    url = f"wss://fstream.binance.com/stream?streams={streams}"
    wait = 5
    while True:
        t0 = _now()
        feed("binance", "connecting")

        def on_message(ws, m):
            try:
                on_binance(json.loads(m))
                if FEEDS.get("binance", {}).get("state") != "live":
                    feed("binance", "live", "Binance futures order book")
            except Exception:
                pass

        def on_error(ws, e):
            feed("binance", "error", str(e)[:120])
        try:
            websocket.WebSocketApp(url, on_message=on_message, on_error=on_error).run_forever(ping_interval=60, ping_timeout=20)
        except Exception as e:
            feed("binance", "error", str(e)[:120])
        if FEEDS.get("binance", {}).get("state") != "error":
            feed("binance", "off", "disconnected - reconnecting")
        wait = 5 if _now() - t0 > 120 else min(wait * 2, 300)          # Binance closes every 24 h: reconnect fast
        time.sleep(wait)


# ------------------------------------------------------------------ India live prices (filled by dhan_loop)
INDIA: dict = {}


# ------------------------------------------------------------------ Dhan (2026-10-23: replaces Upstox for Nifty / Bank Nifty)
def _nse_open() -> bool:
    from zoneinfo import ZoneInfo
    n = datetime.now(ZoneInfo("Asia/Kolkata"))
    m = n.hour * 60 + n.minute
    return n.weekday() < 5 and 555 <= m <= 930


def dhan_loop():
    """Nifty / Bank Nifty index + near futures from Dhan every 2 s in NSE hours; stock / sector quotes every 60 s."""
    import dhan_live as DL
    last_q = 0.0
    while True:
        try:
            if not DL.ok():
                feed("dhan", "off", "no valid Dhan login - run Dhan-Login on the Desktop")
                time.sleep(30); continue
            if not _nse_open():
                feed("dhan", "off", "waits for NSE hours (9:15-15:30 IST)")
                time.sleep(20); continue
            fut = DL.near_futures()
            req = {"IDX_I": [13, 25], "NSE_FNO": [int(v) for v in fut.values()]}
            data = DL.quote(req)
            now = _now()
            names = {("IDX_I", "13"): ("NIFTY", "index"), ("IDX_I", "25"): ("BANKNIFTY", "index")}
            names.update({("NSE_FNO", str(v)): (k, "fut") for k, v in fut.items()})
            for seg, rows in data.items():
                for sid, row in (rows or {}).items():
                    nm = names.get((seg, str(sid)))
                    if not nm:
                        continue
                    f = DL.row_fields(row)
                    if f["ltp"] is None:
                        continue
                    r = INDIA.setdefault(nm[0], {}).setdefault(nm[1], {})
                    cp = f["prev_close"]
                    r.update(price=f["ltp"], prev_close=cp, t=now, src="Dhan", chg_pct=None if not cp else round((f["ltp"] / cp - 1) * 100, 2))
                    if nm[1] == "fut":
                        oi = _f(f.get("oi"))
                        if oi:
                            day = str(datetime.now().date())
                            if r.get("oi_day") != day:
                                r["oi_day"], r["oi_first"] = day, oi
                            r["oi"] = oi
                            r["oi_chg_pct"] = round((oi / r["oi_first"] - 1) * 100, 2) if r.get("oi_first") else None
                        bq, sq = _f(f.get("buy_qty")), _f(f.get("sell_qty"))
                        if bq and sq:
                            r["queue_ratio"] = round(bq / sq, 2)
            feed("dhan", "live", "Dhan market quotes every 2 s")
            if now - last_q >= 60:                       # stocks + sectors for the India pages
                last_q = now
                try:
                    import india
                    india.live_quotes(india.universe(), force=True)
                except Exception as e:
                    feed("dhan_quotes", "error", str(e)[:120])
        except Exception as e:
            feed("dhan", "error", str(e)[:120])
            time.sleep(10)
        time.sleep(1)


# ------------------------------------------------------------------ Yahoo (polled) and GIFT Nifty
QUOTES: dict = {}


def parse_spark(j: dict) -> dict:
    """Yahoo /v8/finance/spark in either of its two shapes -> {symbol: {last, prev_close, t}}."""
    out = {}
    if isinstance(j, dict) and "spark" in j:
        for r in (j["spark"] or {}).get("result") or []:
            resp = (r.get("response") or [{}])[0]
            meta = resp.get("meta") or {}
            closes = (((resp.get("indicators") or {}).get("quote") or [{}])[0].get("close")) or []
            ts = resp.get("timestamp") or []
            last = meta.get("regularMarketPrice")
            if last is None:
                vals = [c for c in closes if c is not None]
                last = vals[-1] if vals else None
            out[r.get("symbol")] = {"last": _f(last), "prev_close": _f(meta.get("chartPreviousClose") or meta.get("previousClose")),
                                    "t": meta.get("regularMarketTime") or (ts[-1] if ts else None)}
    elif isinstance(j, dict):
        for sym, r in j.items():
            if not isinstance(r, dict):
                continue
            closes = [c for c in (r.get("close") or []) if c is not None]
            ts = r.get("timestamp") or []
            out[sym] = {"last": _f(closes[-1]) if closes else None,
                        "prev_close": _f(r.get("chartPreviousClose") or r.get("previousClose")), "t": ts[-1] if ts else None}
    return {k: v for k, v in out.items() if k and v["last"] is not None}


def poll_yahoo_once() -> int:
    import urllib.parse
    got = {}
    for i in range(0, len(YAHOO), 10):
        part = ",".join(YAHOO[i:i + 10])
        q = f"/v8/finance/spark?symbols={urllib.parse.quote(part)}&range=1d&interval=1m"
        err = None
        for host in U.YAHOO_HOSTS:
            try:
                got.update(parse_spark(json.loads(U.yahoo_get(host + q))))
                err = None
                break
            except Exception as e:
                err = e
        if err is not None:
            raise RuntimeError(str(err)[:120])
    now = _now()
    for k, v in got.items():
        v["chg_pct"] = None if not v["prev_close"] else round((v["last"] / v["prev_close"] - 1) * 100, 2)
        v["seen"] = now
        QUOTES[k] = v
    return len(got)


_PREV: dict = {}


def chart_quote(sym: str) -> dict | None:
    """Last 1-minute price (chart API) and the previous daily close (cached for the day)."""
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if _PREV.get(sym, (None,))[0] != day:
        d, tz = U.yahoo_chart(sym, "1d", "5d")
        dates = d["ts"].dt.tz_convert(tz).dt.date
        prev = d[dates < pd.Timestamp.now(tz=tz).date()] if len(d) > 1 else d
        _PREV[sym] = (day, float(prev["close"].iloc[-1]) if len(prev) else None)
    df, _ = U.yahoo_chart(sym, "1m", "5d")
    if not len(df):
        return None
    last, pc = float(df["close"].iloc[-1]), _PREV[sym][1]
    return {"last": last, "prev_close": pc, "t": df["ts"].iloc[-1].timestamp(),
            "chg_pct": None if not pc else round((last / pc - 1) * 100, 2), "seen": _now()}


def top_up_missing():
    """Yahoo's batch call leaves some symbols out (the NSE indices): fetch those one by one."""
    for sym in ("^NSEI", "^NSEBANK"):
        q = QUOTES.get(sym)
        if q and _now() - q.get("seen", 0) < 60:
            continue
        try:
            r = chart_quote(sym)
            if r:
                QUOTES[sym] = r
        except Exception:
            pass


def poll_yahoo_fallback(i: int):
    """One symbol per call with the chart API (used only if the batch call keeps failing)."""
    sym = YAHOO[i % len(YAHOO)]
    df, _ = U.yahoo_chart(sym, "1m", "1d")
    if len(df):
        old = QUOTES.get(sym, {})
        QUOTES[sym] = {"last": float(df["close"].iloc[-1]), "prev_close": old.get("prev_close"), "t": df["ts"].iloc[-1].timestamp(),
                       "chg_pct": old.get("chg_pct"), "seen": _now()}


GIFT: dict = {}


def yahoo_loop():
    fails, i, last_gift = 0, 0, 0.0
    while True:
        try:
            if fails < 3:
                n = poll_yahoo_once()
                top_up_missing()
                fails = 0
                feed("yahoo", "polling", f"every {YAHOO_EVERY} s, {n} symbols")
            else:
                for _ in range(4):
                    poll_yahoo_fallback(i)
                    i += 1
                    time.sleep(1)
                feed("yahoo", "polling", "slower fallback (one symbol at a time)")
                if i % (len(YAHOO) * 3) == 0:
                    fails = 0                   # try the batch call again now and then
        except Exception as e:
            fails += 1
            feed("yahoo", "error", str(e)[:120])
        if _now() - last_gift > 90:
            last_gift = _now()
            try:
                import crossmkt
                g = crossmkt.gift_nifty()
                if g:
                    GIFT.update(last=g["last"], recv=g["recv"], seen=_now())
            except Exception:
                pass
        time.sleep(YAHOO_EVERY)


# ------------------------------------------------------------------ Yahoo price stream (2026-11-04, Nikhil: "all global indices live, 1 sec")
def _pb_pricing(raw: bytes) -> dict:
    """Minimal decoder for Yahoo's PricingData protobuf: 1 id, 2 price (float), 3 time (sint64 ms), 8 changePercent (float),
    10 dayHigh, 11 dayLow, 12 change. Other fields are skipped."""
    import struct
    out, i, n = {}, 0, len(raw)
    def varint():
        nonlocal i
        r = sh = 0
        while True:
            b = raw[i]; i += 1
            r |= (b & 0x7F) << sh
            if b < 0x80:
                return r
            sh += 7
    while i < n:
        key = varint(); f, wt = key >> 3, key & 7
        if wt == 0:
            v = varint()
            if f == 3:
                out["time"] = (v >> 1) ^ -(v & 1)
        elif wt == 5:
            v = struct.unpack("<f", raw[i:i + 4])[0]; i += 4
            out[{2: "price", 8: "chg_pct", 10: "high", 11: "low", 12: "change"}.get(f, f"f{f}")] = v
        elif wt == 1:
            i += 8
        elif wt == 2:
            ln = varint(); val = raw[i:i + ln]; i += ln
            if f == 1:
                out["id"] = val.decode("utf-8", "ignore")
        else:
            break
    return out


# ------------------------------------------------------------------ Hyperliquid xyz perps (24/7 index / commodity prices)
HL_COINS = ("SP500", "XYZ100", "JP225", "GOLD", "CL", "BRENTOIL", "NIFTY", "KR200")
HL: dict = {}                       # coin -> {"px", "t", "prev"} (prev = price 24 h ago, from the asset contexts)


def _hl_prev_loop():
    """24-hour reference prices (prevDayPx) for the xyz perps, once a minute."""
    import urllib.request
    while True:
        try:
            req = urllib.request.Request("https://api.hyperliquid.xyz/info", data=json.dumps({"type": "metaAndAssetCtxs", "dex": "xyz"}).encode(),
                                         headers={"Content-Type": "application/json"})
            meta, ctx = json.loads(urllib.request.urlopen(req, timeout=10).read())
            for u, c in zip(meta["universe"], ctx):
                k = u["name"].split(":")[-1]
                if k in HL_COINS:
                    h = HL.setdefault(k, {})
                    h["prev"] = _f(c.get("prevDayPx"))
                    if not h.get("px"):
                        h.update(px=_f(c.get("midPx") or c.get("markPx")), t=_now())
        except Exception as e:
            feed("hyperliquid", "error", f"contexts: {str(e)[:100]}")
        time.sleep(60)


def hl_loop():
    """Hyperliquid websocket, allMids for the xyz dex: a fresh mid price for every perp about once a second, round the clock."""
    import websocket
    threading.Thread(target=_hl_prev_loop, daemon=True, name="stream_hl_prev").start()
    while True:
        try:
            def on_open(ws):
                ws.send(json.dumps({"method": "subscribe", "subscription": {"type": "allMids", "dex": "xyz"}}))
                feed("hyperliquid", "live", "xyz perps: " + ", ".join(HL_COINS))

            def on_message(ws, msg):
                try:
                    j = json.loads(msg)
                    if j.get("channel") != "allMids":
                        return
                    now = _now()
                    for k, v in ((j.get("data") or {}).get("mids") or {}).items():
                        c = k.split(":")[-1]
                        if c in HL_COINS:
                            HL.setdefault(c, {}).update(px=_f(v), t=now)
                except Exception:
                    pass

            websocket.WebSocketApp("wss://api.hyperliquid.xyz/ws", on_open=on_open, on_message=on_message,
                                   on_error=lambda w, e: feed("hyperliquid", "error", str(e)[:120])).run_forever(ping_interval=30, ping_timeout=10)
        except Exception as e:
            feed("hyperliquid", "error", str(e)[:120])
        time.sleep(5)


WS_SEEN: dict = {}            # symbol -> messages received (status / debugging)


def yahoo_ws_loop():
    """Yahoo's streamer pushes a message each time a quote changes (indices every few seconds while their market is open,
    futures round the clock). The 20-second batch poll keeps running underneath as the fallback and for previous closes."""
    import base64
    import websocket
    while True:
        try:
            def on_open(ws):
                ws.send(json.dumps({"subscribe": YAHOO}))
                feed("yahoo_stream", "live", f"{len(YAHOO)} symbols")

            def on_message(ws, msg):
                try:
                    if isinstance(msg, bytes):
                        msg = msg.decode()
                    if msg.startswith("{"):
                        j = json.loads(msg)
                        msg = j.get("message") or ""
                        if not msg:
                            return
                    d = _pb_pricing(base64.b64decode(msg))
                    sym, px = d.get("id"), d.get("price")
                    WS_SEEN[sym] = WS_SEEN.get(sym, 0) + 1
                    if not sym or not px:
                        return
                    old = QUOTES.get(sym, {})
                    pc = old.get("prev_close")
                    chg = round(float(d["chg_pct"]), 2) if d.get("chg_pct") is not None else (round((px / pc - 1) * 100, 2) if pc else old.get("chg_pct"))
                    QUOTES[sym] = {**old, "last": round(float(px), 4), "chg_pct": chg, "t": (d.get("time") or 0) / 1000 or _now(),
                                   "seen": _now(), "ws": _now()}
                except Exception:
                    pass

            ws = websocket.WebSocketApp("wss://streamer.finance.yahoo.com/?version=2", on_open=on_open, on_message=on_message,
                                        on_error=lambda w, e: feed("yahoo_stream", "error", str(e)[:120]),
                                        header={"User-Agent": "Mozilla/5.0", "Origin": "https://finance.yahoo.com"})
            ws.run_forever(ping_interval=30, ping_timeout=10)
        except Exception as e:
            feed("yahoo_stream", "error", str(e)[:120])
        time.sleep(5)


# ------------------------------------------------------------------ assemble one snapshot a second
def market_live(now: float) -> dict:
    out = {}
    for m in COINS:
        s = COIN[m].snap(now)
        if s:
            out[m] = {**s, "src": "Binance futures", "mode": "stream", "what": f"{COINS[m]} perpetual"}
    for m in ("NIFTY", "BANKNIFTY"):
        r = (INDIA.get(m) or {}).get("fut")
        if r and now - r["t"] < 120:
            out[m] = {"price": r["price"], "chg_pct": r.get("chg_pct"), "ref": "vs yesterday's close", "at": _iso(r["t"]),
                      "age_s": round(now - r["t"], 1), "src": r.get("src", "Dhan"), "mode": "stream", "what": "near-month futures",
                      "oi": r.get("oi"), "oi_chg_pct": r.get("oi_chg_pct"), "queue_ratio": r.get("queue_ratio")}
    for m, (sym, what) in Y_MARKET.items():
        if m in out:
            continue
        q = QUOTES.get(sym)
        if q:
            ws_live = now - (q.get("ws") or 0) < 120
            out[m] = {"price": q["last"], "chg_pct": q.get("chg_pct"), "ref": "vs previous close", "at": _iso(q.get("t")),
                      "age_s": None if not q.get("t") else round(now - float(q["t"]), 0), "src": "Yahoo stream" if ws_live else "Yahoo",
                      "mode": "stream" if ws_live else "poll", "what": what}
    return out


def publisher_loop():
    n = 0
    while True:
        now = _now()
        try:
            try:
                oc = os.path.getmtime(OC_LIVE)
            except OSError:
                oc = None
            mk = market_live(now)
            if n % 30 == 0:                              # status file for health checks (the stream itself is in memory)
                st = {"at": _iso(now), "feeds": FEEDS, "yahoo_ws_seen": dict(WS_SEEN), "markets": {k: {"src": v["src"], "mode": v["mode"], "age_s": v.get("age_s")}
                                                                   for k, v in mk.items()}, "clients_version": HUB.v}
                with open(STATUS + ".tmp", "w") as f:
                    json.dump(st, f, default=str, indent=1)
                os.replace(STATUS + ".tmp", STATUS)
            HUB.set({"t": _iso(now), "markets": mk,
                     "quotes": {k: {"last": v["last"], "chg_pct": v.get("chg_pct"), "t": v.get("t"), "live": now - (v.get("ws") or 0) < 120} for k, v in QUOTES.items()},
                     "hl": {k: {"px": v.get("px"), "chg24": (round((v["px"] / v["prev"] - 1) * 100, 2) if v.get("px") and v.get("prev") else None),
                            "live": now - (v.get("t") or 0) < 30} for k, v in HL.items()},
                     "gift": dict(GIFT) or None, "oc_v": oc, "feeds": FEEDS})
        except Exception:
            U.log("stream publisher: " + traceback.format_exc().splitlines()[-1])
        n += 1
        time.sleep(1)


# ------------------------------------------------------------------ Server-Sent Events
def sse_loop(wfile):
    """Write snapshots to an open text/event-stream response until the browser goes away (also used by server.py)."""
    v, last_sent = -1, 0.0
    try:
        wfile.write(b"retry: 3000\n\n")
        while True:
            nv, body = HUB.wait(v, 15)
            if nv == v:
                wfile.write(b": ping\n\n")
            else:
                gap = 1.0 - (_now() - last_sent)
                if gap > 0:
                    time.sleep(gap)
                    nv, body = HUB.v, HUB.body
                wfile.write(b"data: " + body + b"\n\n")
                v, last_sent = nv, _now()
            wfile.flush()
    except (BrokenPipeError, ConnectionResetError, OSError):
        return


def serve(port: int = PORT):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _head(self, ctype):
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-store")

        def do_GET(self):
            path = self.path.split("?")[0]
            if path == "/snapshot":
                body = HUB.body
                self._head("application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if path != "/stream":
                self.send_response(404)
                self.end_headers()
                return
            self._head("text/event-stream")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            sse_loop(self.wfile)

    class S(ThreadingHTTPServer):
        daemon_threads = True

        def handle_error(self, request, client_address):     # 2026-11-03 audit: a closed browser tab is not an error
            import sys as _s
            if isinstance(_s.exc_info()[1], (ConnectionResetError, BrokenPipeError)):
                return
            super().handle_error(request, client_address)
    S(("127.0.0.1", port), H).serve_forever()


def _ensure_websocket():
    try:
        import websocket  # noqa: F401
    except ImportError:
        import subprocess
        import sys
        subprocess.run([sys.executable, "-m", "pip", "install", "--quiet", "websocket-client"], timeout=300)


def start():
    """Run all feeds; returns only if the server cannot start (port taken)."""
    try:
        _ensure_websocket()
    except Exception as e:
        feed("binance", "error", f"websocket-client missing: {str(e)[:80]}")
    for name, fn in (("stream_binance", binance_loop), ("stream_binance_market", binance_market_loop), ("stream_yahoo", yahoo_loop), ("stream_yahoo_ws", yahoo_ws_loop), ("stream_hl", hl_loop), ("stream_dhan", dhan_loop),
                     ("stream_pub", publisher_loop)):
        threading.Thread(target=fn, daemon=True, name=name).start()
    U.log(f"live stream on http://127.0.0.1:{PORT}/stream")
    serve()
