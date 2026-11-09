"""CRYPTO FLOW - trades, liquidations, open interest and funding from several venues, as 1-minute buckets (2026-11-10, Nikhil:
"plan what rich info we can get .. liquidation feed, open interest, positioning regime, big-print bubbles, resting-liquidity heatmap,
absorption markers, venue comparing panel" -> plan in the project doc crypto-flow-panels-plan.md). Public streams only, no keys,
never orders. Descriptive: nothing here is a tested signal until it passes the MASTER_BACKTESTER gauntlet.

Streams (one WebSocket per venue, all coins on it; same reconnect pattern as app/bookws.py):
  binance  : aggTrade (trades), !forceOrder@arr (liquidations - Binance sends at most ONE per symbol per second: a sample),
             markPrice@1s (mark + funding); open interest by REST /fapi/v1/openInterest every 60 s per coin
  bybit    : publicTrade, allLiquidation, tickers (openInterest, fundingRate, markPrice)
  okx      : trades, liquidation-orders (SWAP), open-interest, funding-rate
  bitget   : trade, ticker (holdingAmount = OI, fundingRate)
  hyperliquid (metals / energy xyz perps): trades, activeAssetCtx (openInterest, funding, markPx)
Buckets: per (coin, venue, minute): buy_usd, sell_usd, n, big = [(ts, side, usd, px)...] (trade >= BIG_X x the coin's rolling median
trade size), liq_long_usd, liq_short_usd, liq = [(ts, side, usd, px)...], last price, hi, lo. Kept 6 h in memory; the completed hour
is appended to data_store/livedesk/cryptoflow/<day>.csv.gz (one row per coin-venue-minute) for research.
OI / funding / mark: latest value per (coin, venue) + a 24-h minute history of aggregated OI (USD) per coin.
"""
from __future__ import annotations

import csv
import gzip
import json
import os
import threading
import time
import urllib.request
from collections import deque
from datetime import datetime, timezone

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
OUT = os.path.join(ROOT, "data_store", "livedesk", "cryptoflow")
CRYPTO = ("BTC", "ETH", "SOL", "BNB", "XRP", "UNI", "ZEC", "LINK", "DASH", "AVAX")
HL = {"GOLD": "xyz:GOLD", "SILVER": "xyz:SILVER", "COPPER": "xyz:COPPER", "CRUDE": "xyz:CL", "NATGAS": "xyz:NATGAS", "ALUMINUM": "xyz:ALUMINIUM"}
BIG_X, KEEP_MIN = 8.0, 360
_LOCK = threading.Lock()
STATE: dict = {"started": False, "venues": {}, "last": {}}
BK: dict = {}                       # (coin, venue) -> {minute_epoch: bucket}
SZ: dict = {}                       # (coin, venue) -> deque of recent trade sizes (USD) for the median
OI: dict = {}                       # (coin, venue) -> {"oi": coins, "oi_usd": , "mark": , "funding": (rate per 8h or period), "at": }
OIH: dict = {}                      # coin -> deque of (minute_epoch, agg_oi_usd, price)
LIQ: deque = deque(maxlen=400)      # recent liquidations across coins: (ts, venue, coin, side, usd, px)
_flushed: dict = {}                 # (coin, venue) -> last minute flushed to disk


def _status(venue, state, note=""):
    STATE["venues"][venue] = {"state": state, "note": str(note)[:120], "at": time.strftime("%H:%M:%S")}


def _bucket(coin, venue, ts):
    m = int(ts // 60 * 60)
    d = BK.setdefault((coin, venue), {})
    b = d.get(m)
    if b is None:
        b = d[m] = {"m": m, "buy": 0.0, "sell": 0.0, "n": 0, "big": [], "liq_long": 0.0, "liq_short": 0.0, "liq": [], "px": None, "hi": None, "lo": None}
        if len(d) > KEEP_MIN + 5:
            for k in sorted(d)[: len(d) - KEEP_MIN]:
                d.pop(k, None)
    return b


def _trade(coin, venue, ts, px, qty, side):
    """side 'buy' = taker bought (aggressive buyer)."""
    usd = px * qty
    with _LOCK:
        b = _bucket(coin, venue, ts)
        b["buy" if side == "buy" else "sell"] += usd; b["n"] += 1
        b["px"] = px; b["hi"] = px if b["hi"] is None else max(b["hi"], px); b["lo"] = px if b["lo"] is None else min(b["lo"], px)
        q = SZ.setdefault((coin, venue), deque(maxlen=3000)); q.append(usd)
        if len(q) >= 200:
            srt = sorted(q); med = srt[len(srt) // 2]
            if usd >= BIG_X * med and usd >= 20000:
                b["big"].append((round(ts, 1), side, round(usd), px))
                if len(b["big"]) > 40: b["big"] = b["big"][-40:]


def _liq(coin, venue, ts, px, qty, side):
    """side = the side being liquidated: 'long' (forced sell) or 'short' (forced buy)."""
    usd = px * qty
    with _LOCK:
        b = _bucket(coin, venue, ts)
        b["liq_long" if side == "long" else "liq_short"] += usd
        b["liq"].append((round(ts, 1), side, round(usd), px))
        if len(b["liq"]) > 40: b["liq"] = b["liq"][-40:]
        LIQ.append((round(ts, 1), venue, coin, side, round(usd), px))


def _oi(coin, venue, oi=None, mark=None, funding=None, oi_usd=None):
    with _LOCK:
        d = OI.setdefault((coin, venue), {})
        if oi is not None: d["oi"] = float(oi)
        if mark is not None: d["mark"] = float(mark)
        if funding is not None: d["funding"] = float(funding)
        if oi_usd is not None: d["oi_usd"] = float(oi_usd)
        else:
            px = d.get("mark")
            if not px:                                            # venues without a mark in the stream (OKX): last traded price
                bk = BK.get((coin, venue)) or {}
                px = bk[max(bk)]["px"] if bk else None
            if d.get("oi") is not None and px: d["oi_usd"] = d["oi"] * px
        d["at"] = time.time()


def _run(venue, url, on_open, on_message, text_ping=None, ping=60):
    import websocket
    wait = 5
    while True:
        t0 = time.time(); _status(venue, "connecting")
        try:
            def _open(w):
                on_open(w)
                if text_ping:
                    def loop():
                        while getattr(w, "keep_running", False) and w.sock and w.sock.connected:
                            try: w.send(text_ping)
                            except Exception: break
                            time.sleep(25)
                    threading.Thread(target=loop, daemon=True).start()
            def _msg(w, m):
                STATE["last"][venue] = str(m)[:200]
                try: on_message(w, m)
                except Exception as e: STATE["last"][venue + "_err"] = f"{type(e).__name__}: {str(e)[:80]}"
            ws = websocket.WebSocketApp(url, on_open=_open, on_message=_msg, on_error=lambda w, e: _status(venue, "error", str(e)))
            ws.run_forever(ping_interval=ping or 0, ping_timeout=20 if ping else None)
        except Exception as e:
            _status(venue, "error", str(e))
        wait = 5 if time.time() - t0 > 120 else min(wait * 2, 300)
        _status(venue, "off", f"reconnecting in {wait}s"); time.sleep(wait)


# ------------------------------------------------------------------ Binance
def _binance():
    def on_open(ws): _status("binance", "live", "aggTrade · forceOrder · markPrice")
    def on_message(ws, m):
        x = json.loads(m); d = x.get("data") or {}; e = d.get("e")
        if e == "aggTrade":
            coin = d["s"][:-4]
            if coin in CRYPTO: _trade(coin, "binance", d["T"] / 1000, float(d["p"]), float(d["q"]), "sell" if d["m"] else "buy")
        elif e == "forceOrder":
            o = d["o"]; coin = o["s"][:-4]
            if coin in CRYPTO: _liq(coin, "binance", o["T"] / 1000, float(o["ap"] or o["p"]), float(o["z"] or o["q"]), "long" if o["S"] == "SELL" else "short")
        elif e == "markPriceUpdate":
            coin = d["s"][:-4]
            if coin in CRYPTO: _oi(coin, "binance", mark=float(d["p"]), funding=float(d["r"]))
    streams = [f"{k.lower()}usdt@aggTrade" for k in CRYPTO] + [f"{k.lower()}usdt@markPrice@1s" for k in CRYPTO]
    _run("binance", "wss://fstream.binance.com/market/stream?streams=" + "/".join(streams), on_open, on_message)   # /market route: Binance serves trades / mark price there (binance_feed.py, 1 Nov); the plain /stream route only carries depth


def _binance_liq():
    """liquidations on their own connection (/market route)."""
    def on_open(ws): _status("binance-liq", "live", "!forceOrder@arr")
    def on_message(ws, m):
        x = json.loads(m); d = x.get("data") or x
        if d.get("e") != "forceOrder": return
        o = d["o"]; coin = o["s"][:-4]
        if coin in CRYPTO: _liq(coin, "binance", o["T"] / 1000, float(o["ap"] or o["p"]), float(o["z"] or o["q"]), "long" if o["S"] == "SELL" else "short")
    _run("binance-liq", "wss://fstream.binance.com/market/stream?streams=!forceOrder@arr", on_open, on_message)


def _binance_oi_loop():
    """REST open interest, one coin every 6 s (10 coins a minute)."""
    i = 0
    while True:
        coin = CRYPTO[i % len(CRYPTO)]; i += 1
        try:
            req = urllib.request.Request(f"https://fapi.binance.com/fapi/v1/openInterest?symbol={coin}USDT", headers={"User-Agent": "Mozilla/5.0"})
            d = json.loads(urllib.request.urlopen(req, timeout=8).read()); _oi(coin, "binance", oi=float(d["openInterest"]))
        except Exception as e:
            STATE["last"]["binance_oi_err"] = str(e)[:80]
        time.sleep(6)


# ------------------------------------------------------------------ Bybit
def _bybit():
    def on_open(ws):
        _status("bybit", "live", "publicTrade · allLiquidation · tickers")
        args = [f"publicTrade.{k}USDT" for k in CRYPTO] + [f"allLiquidation.{k}USDT" for k in CRYPTO] + [f"tickers.{k}USDT" for k in CRYPTO]
        for i in range(0, len(args), 10):
            ws.send(json.dumps({"op": "subscribe", "args": args[i:i + 10]}))
    def on_message(ws, m):
        x = json.loads(m); t = str(x.get("topic", ""))
        if t.startswith("publicTrade."):
            for r in x["data"]:
                coin = r["s"][:-4]; _trade(coin, "bybit", r["T"] / 1000, float(r["p"]), float(r["v"]), "buy" if r["S"] == "Buy" else "sell")
        elif t.startswith("allLiquidation."):
            for r in x["data"]:
                coin = r["s"][:-4]; _liq(coin, "bybit", r["T"] / 1000, float(r["p"]), float(r["v"]), "long" if r["S"] == "Sell" else "short")   # S = side of the liquidation order
        elif t.startswith("tickers."):
            d = x["data"]; coin = d["symbol"][:-4]
            _oi(coin, "bybit", oi=d.get("openInterest"), mark=d.get("markPrice"), funding=d.get("fundingRate"))
    _run("bybit", "wss://stream.bybit.com/v5/public/linear", on_open, on_message, text_ping='{"op":"ping"}', ping=0)


# ------------------------------------------------------------------ OKX
def _okx():
    ct: dict = {}
    def cval(coin):
        if coin not in ct:
            try:
                req = urllib.request.Request(f"https://www.okx.com/api/v5/public/instruments?instType=SWAP&instId={coin}-USDT-SWAP", headers={"User-Agent": "Mozilla/5.0"})
                ct[coin] = float(json.loads(urllib.request.urlopen(req, timeout=8).read())["data"][0]["ctVal"])
            except Exception:
                ct[coin] = 1.0
        return ct[coin]
    def on_open(ws):
        _status("okx", "live", "trades · liquidation-orders · open-interest · funding-rate")
        args = [{"channel": "trades", "instId": f"{k}-USDT-SWAP"} for k in CRYPTO] + [{"channel": "open-interest", "instId": f"{k}-USDT-SWAP"} for k in CRYPTO] \
             + [{"channel": "funding-rate", "instId": f"{k}-USDT-SWAP"} for k in CRYPTO] + [{"channel": "liquidation-orders", "instType": "SWAP"}]
        ws.send(json.dumps({"op": "subscribe", "args": args}))
    def on_message(ws, m):
        if m == "pong": return
        x = json.loads(m); ch = (x.get("arg") or {}).get("channel")
        if ch == "trades":
            for r in x.get("data") or []:
                coin = r["instId"].split("-")[0]; _trade(coin, "okx", int(r["ts"]) / 1000, float(r["px"]), float(r["sz"]) * cval(coin), r["side"])
        elif ch == "liquidation-orders":
            for r in x.get("data") or []:
                coin = str(r.get("instId", "")).split("-")[0]
                if coin not in CRYPTO or "USDT" not in str(r.get("instId", "")): continue
                for d in r.get("details") or []:
                    _liq(coin, "okx", int(d["ts"]) / 1000, float(d["bkPx"]), float(d["sz"]) * cval(coin), "long" if d["posSide"] == "long" or d["side"] == "sell" else "short")
        elif ch == "open-interest":
            for r in x.get("data") or []:
                coin = r["instId"].split("-")[0]; _oi(coin, "okx", oi=float(r["oiCcy"]) if r.get("oiCcy") else float(r["oi"]) * cval(coin))
        elif ch == "funding-rate":
            for r in x.get("data") or []:
                coin = r["instId"].split("-")[0]; _oi(coin, "okx", funding=float(r["fundingRate"]))
    _run("okx", "wss://ws.okx.com:8443/ws/v5/public", on_open, on_message, text_ping="ping", ping=0)


# ------------------------------------------------------------------ Bitget
def _bitget():
    def on_open(ws):
        _status("bitget", "live", "trade · ticker")
        args = [{"instType": "USDT-FUTURES", "channel": "trade", "instId": f"{k}USDT"} for k in CRYPTO] + [{"instType": "USDT-FUTURES", "channel": "ticker", "instId": f"{k}USDT"} for k in CRYPTO]
        ws.send(json.dumps({"op": "subscribe", "args": args}))
    def on_message(ws, m):
        if m == "pong": return
        x = json.loads(m); ch = (x.get("arg") or {}).get("channel")
        if ch == "trade":
            coin = x["arg"]["instId"][:-4]
            for r in x.get("data") or []:
                _trade(coin, "bitget", int(r["ts"]) / 1000, float(r["price"]), float(r["size"]), r["side"])
        elif ch == "ticker":
            coin = x["arg"]["instId"][:-4]
            for r in x.get("data") or []:
                _oi(coin, "bitget", oi=r.get("holdingAmount"), mark=r.get("markPrice"), funding=r.get("fundingRate"))
    _run("bitget", "wss://ws.bitget.com/v2/ws/public", on_open, on_message, text_ping="ping", ping=0)


# ------------------------------------------------------------------ Hyperliquid (metals / energy perps)
def _hyperliquid():
    inv = {v: k for k, v in HL.items()}
    def on_open(ws):
        _status("hyperliquid", "live", "trades · activeAssetCtx")
        for c in HL.values():
            ws.send(json.dumps({"method": "subscribe", "subscription": {"type": "trades", "coin": c}}))
            ws.send(json.dumps({"method": "subscribe", "subscription": {"type": "activeAssetCtx", "coin": c}}))
    def on_message(ws, m):
        x = json.loads(m); ch = x.get("channel")
        if ch == "trades":
            for r in x["data"]:
                coin = inv.get(r["coin"])
                if coin: _trade(coin, "hyperliquid", r["time"] / 1000, float(r["px"]), float(r["sz"]), "buy" if r["side"] == "B" else "sell")
        elif ch == "activeAssetCtx":
            d = x["data"]; coin = inv.get(d.get("coin")); c = d.get("ctx") or {}
            if coin: _oi(coin, "hyperliquid", oi=c.get("openInterest"), mark=c.get("markPx"), funding=c.get("funding"))
    _run("hyperliquid", "wss://api.hyperliquid.xyz/ws", on_open, on_message, ping=50)


# ------------------------------------------------------------------ housekeeping: OI history, hourly file
def _keeper():
    while True:
        try:
            now = time.time(); m = int(now // 60 * 60)
            with _LOCK:
                for coin in CRYPTO + tuple(HL):
                    tot = sum(d.get("oi_usd") or 0 for (c, v), d in OI.items() if c == coin and now - d.get("at", 0) < 600)
                    px = next((d.get("mark") for (c, v), d in OI.items() if c == coin and d.get("mark")), None)
                    if tot:
                        h = OIH.setdefault(coin, deque(maxlen=1500))
                        if not h or h[-1][0] != m: h.append((m, round(tot), px))
                        else: h[-1] = (m, round(tot), px)
                # flush completed minutes older than 2 min to the day file
                rows = []
                for (coin, venue), d in BK.items():
                    last = _flushed.get((coin, venue), 0)
                    for k in sorted(d):
                        if k <= last or k >= m - 60: continue
                        b = d[k]
                        rows.append([datetime.fromtimestamp(k, timezone.utc).strftime("%Y-%m-%d %H:%M"), coin, venue, round(b["buy"]), round(b["sell"]), b["n"], round(b["liq_long"]), round(b["liq_short"]), len(b["big"]), b["px"], b["hi"], b["lo"]])
                        _flushed[(coin, venue)] = k
            if rows:
                os.makedirs(OUT, exist_ok=True)
                p = os.path.join(OUT, datetime.now(timezone.utc).strftime("%Y-%m-%d") + ".csv.gz"); new = not os.path.exists(p)
                with gzip.open(p, "at", newline="") as f:
                    w = csv.writer(f)
                    if new: w.writerow(["utc_min", "coin", "venue", "buy_usd", "sell_usd", "n", "liq_long_usd", "liq_short_usd", "big_n", "px", "hi", "lo"])
                    w.writerows(rows)
        except Exception as e:
            STATE["last"]["keeper_err"] = str(e)[:100]
        time.sleep(20)


def start():
    if STATE["started"]:
        return False
    STATE["started"] = True
    for nm, fn in (("binance", _binance), ("binance-liq", _binance_liq), ("binance-oi", _binance_oi_loop), ("bybit", _bybit), ("okx", _okx), ("bitget", _bitget), ("hyperliquid", _hyperliquid), ("keeper", _keeper)):
        threading.Thread(target=fn, name=f"cryptoflow-{nm}", daemon=True).start()
    return True


# ------------------------------------------------------------------ reads
def _sum(coin, venue, mins, now=None):
    now = now or time.time(); m0 = int(now // 60 * 60) - 60 * (mins - 1)
    d = BK.get((coin, venue), {}); buy = sell = liql = liqs = 0.0; n = 0
    for k, b in d.items():
        if k >= m0:
            buy += b["buy"]; sell += b["sell"]; n += b["n"]; liql += b["liq_long"]; liqs += b["liq_short"]
    return {"buy": buy, "sell": sell, "cvd": buy - sell, "n": n, "liq_long": liql, "liq_short": liqs}


def venues(coin):
    """venue comparison for one coin."""
    now = time.time(); out = []
    with _LOCK:
        vs = sorted({v for (c, v) in BK if c == coin} | {v for (c, v) in OI if c == coin})
        ref = (OI.get((coin, "binance")) or {}).get("mark")
        for v in vs:
            d = BK.get((coin, v), {}); last = d[max(d)] if d else None
            o = OI.get((coin, v)) or {}
            px = (last or {}).get("px") or o.get("mark")
            if o.get("oi") is not None and not o.get("oi_usd") and px: o["oi_usd"] = o["oi"] * px      # OKX has no mark in its streams: last trade
            row = {"venue": v, "price": px, "basis_bps": round((px / ref - 1) * 1e4, 1) if px and ref else None,
                   "m1": _sum(coin, v, 1, now), "m5": _sum(coin, v, 5, now), "m15": _sum(coin, v, 15, now),
                   "oi": o.get("oi"), "oi_usd": o.get("oi_usd"), "funding": o.get("funding"), "oi_at": o.get("at"),
                   "big15": sum(len(b["big"]) for k, b in d.items() if k >= int(now // 60 * 60) - 840)}
            out.append(row)
    for r in out:
        for k in ("m1", "m5", "m15"):
            r[k] = {a: round(b_) for a, b_ in r[k].items()}
    signs = [r["m5"]["cvd"] for r in out if r["venue"] in ("binance", "bybit", "okx") and abs(r["m5"]["cvd"]) > 0]
    disagree = len(signs) >= 2 and (max(signs) > 0 > min(signs)) and min(abs(x) for x in signs) > 0.2 * max(abs(x) for x in signs)
    return {"coin": coin, "venues": out, "disagree": bool(disagree), "at": datetime.now().strftime("%H:%M:%S")}


def liquidations(coin=None, n=50):
    with _LOCK:
        rows = [r for r in reversed(LIQ) if coin is None or r[2] == coin][:n]
        now = time.time(); m = int(now // 60 * 60)
        strip = []
        for i in range(59, -1, -1):
            k = m - 60 * i; ll = ls = 0.0
            for (c, v), d in BK.items():
                if coin is not None and c != coin: continue
                b = d.get(k)
                if b: ll += b["liq_long"]; ls += b["liq_short"]
            strip.append([k, round(ll), round(ls)])
    tot = sorted(a[1] + a[2] for a in strip); med = tot[len(tot) // 2] if tot else 0
    cur = strip[-1][1] + strip[-1][2] if strip else 0
    return {"coin": coin, "recent": [{"ts": r[0], "venue": r[1], "coin": r[2], "side": r[3], "usd": r[4], "px": r[5]} for r in rows], "strip": strip,
            "cascade": bool(med and cur > 5 * med and cur > 200000), "note": "Binance sends at most one liquidation per symbol per second (a sample); Bitget has no public liquidation stream."}


def open_interest(coin):
    with _LOCK:
        per = {}
        for (c, v), d in OI.items():
            if c != coin: continue
            d = dict(d)
            if d.get("oi") is not None and not d.get("oi_usd"):
                bk = BK.get((coin, v)) or {}; px = d.get("mark") or (bk[max(bk)]["px"] if bk else None)
                if px: d["oi_usd"] = d["oi"] * px
            per[v] = d
        h = list(OIH.get(coin) or [])
    now_oi = h[-1][1] if h else None; px_now = h[-1][2] if h else None
    def back(mins):
        t = (h[-1][0] if h else 0) - 60 * mins
        for m, oi, px in h:
            if m >= t: return oi, px
        return (None, None)
    ch = {}
    for lab, mins in (("1h", 60), ("4h", 240), ("24h", 1440)):
        o0, p0 = back(mins)
        ch[lab] = {"oi_pct": round((now_oi / o0 - 1) * 100, 2) if now_oi and o0 else None, "px_pct": round((px_now / p0 - 1) * 100, 2) if px_now and p0 else None, "mins": len([1 for m, _, _ in h if m >= (h[-1][0] if h else 0) - 60 * mins])}
    c = ch["1h"]; lab = None
    if c["oi_pct"] is not None and c["px_pct"] is not None:
        oi_up = c["oi_pct"] >= 0.5; oi_dn = c["oi_pct"] <= -0.5; px_up = c["px_pct"] >= 0.15; px_dn = c["px_pct"] <= -0.15
        lab = "long build-up" if oi_up and px_up else "short build-up" if oi_up and px_dn else "short covering" if oi_dn and px_up else "long unwinding" if oi_dn and px_dn else "no clear change"
    return {"coin": coin, "oi_usd": now_oi, "price": px_now, "per_venue": per, "change": ch, "label_1h": lab, "history_min": len(h),
            "hist": [[m, oi, px] for m, oi, px in h[-240:]]}


def positioning(coin, hot=None):
    """one line from funding, OI trend, Binance top-trader / global long-short and taker ratio (derivhist's latest rows)."""
    oi = open_interest(coin)
    f = [d.get("funding") for d in oi["per_venue"].values() if d.get("funding") is not None]
    fund = sum(f) / len(f) if f else None                      # per 8 h on most venues (Hyperliquid hourly) -> shown as annualised
    ann = fund * 3 * 365 * 100 if fund is not None else None
    der = {}
    try:
        import pandas as pd
        p = os.path.join(ROOT, "data_store", "research", "deriv", f"der15_{coin}.csv")
        if os.path.exists(p):
            d = pd.read_csv(p).tail(1).iloc[0].to_dict()
            der = {k: (None if (isinstance(d.get(k), float) and d.get(k) != d.get(k)) else d.get(k)) for k in ("ts", "top_pos", "top_acc", "glob_acc", "taker_bs")}   # NaN -> null (JSON)
            try:
                tsv = float(der.get("ts") or 0); der["ts"] = datetime.fromtimestamp(tsv / 1000 if tsv > 1e11 else tsv).strftime("%d %b %H:%M") + " IST" if tsv else None
            except Exception:
                pass
    except Exception:
        pass
    score = 0; why = []
    if ann is not None:
        if ann > 20: score += 1; why.append(f"funding {ann:+.0f}%/yr (longs paying)")
        elif ann < -5: score -= 1; why.append(f"funding {ann:+.0f}%/yr (shorts paying)")
        else: why.append(f"funding {ann:+.0f}%/yr")
    tp = der.get("top_pos")
    if tp:
        if tp > 1.3: score += 1; why.append(f"top traders long {tp:.2f}x")
        elif tp < 0.8: score -= 1; why.append(f"top traders short {tp:.2f}x")
        else: why.append(f"top traders {tp:.2f}x")
    ga = der.get("glob_acc")
    if ga:
        if ga > 2.0: score += 1; why.append(f"crowd long {ga:.2f}x")
        elif ga < 1.0: score -= 1; why.append(f"crowd short {ga:.2f}x")
    c1 = oi["change"]["4h"]
    if c1["oi_pct"] is not None and c1["px_pct"] is not None:
        why.append(f"OI {c1['oi_pct']:+.1f}% / price {c1['px_pct']:+.1f}% in 4 h")
        if c1["oi_pct"] > 2 and c1["px_pct"] > 0.5: score += 1
        if c1["oi_pct"] > 2 and c1["px_pct"] < -0.5: score -= 1
    lab = "crowded long" if score >= 2 else "crowded short" if score <= -2 else "lean long" if score == 1 else "lean short" if score == -1 else "balanced"
    return {"coin": coin, "label": lab, "score": score, "why": why, "funding_ann_pct": round(ann, 1) if ann is not None else None, "deriv": der,
            "caveat": "descriptive - the 7 Nov study found no reliable fingerprint before big moves (T4 leaned down on 15 episodes only)"}


def big_prints(coin, n=40):
    now = time.time(); m0 = int(now // 60 * 60) - 3600
    with _LOCK:
        rows = [(ts, v, side, usd, px) for (c, v), d in BK.items() if c == coin for k, b in d.items() if k >= m0 for (ts, side, usd, px) in b["big"]]
    rows.sort(reverse=True)
    return {"coin": coin, "prints": [{"ts": r[0], "venue": r[1], "side": r[2], "usd": r[3], "px": r[4]} for r in rows[:n]], "rule": f"trade >= {BIG_X:g}x the coin's rolling median trade size and >= $20k"}


def api(coin="BTC"):
    start()
    return {"venues": venues(coin), "liq": liquidations(coin), "oi": open_interest(coin), "pos": positioning(coin), "big": big_prints(coin),
            "status": {"venues": STATE["venues"], "last_err": {k: v for k, v in STATE["last"].items() if k.endswith("err")}, "last": {k: v[:120] for k, v in STATE["last"].items() if not k.endswith("err")}}, "at": datetime.now().strftime("%H:%M:%S")}
