"""
12-MONTH REAL FOOTPRINT FOR BTC / ETH / SOL (2026-11-11, Nikhil: "yes for 12 month rebuild ... it needs to keep updating").

Binance USD-M futures publish every aggregated trade in a daily archive (data.binance.vision). This job downloads one day at a
time, turns it into 5-minute footprint bars (price row x aggressive buy / aggressive sell volume) and keeps only the bars; the
raw trades are never written to disk. The cloud session cannot reach the archive, so the job runs inside the dashboard
process on the Mac, in a background thread, one day after another, resuming where it stopped after a restart. Once the year
is in, it only adds each new day (the archive publishes the previous UTC day the next morning).

Rows: the same row size the live charts use (BTC 25 USD, ETH 1.5 USD, SOL 0.1 USD at today's prices), scaled to each day's
opening price so a bar has about the same number of rows all year. Aggressor: Binance's is_buyer_maker flag (buyer is maker
= the seller hit the bid = aggressive sell). Times are UTC.

Output: data_store/research/ofhist/<COIN>/<YYYY-MM-DD>.json = {"step": row size, "src": "binance-um-aggTrades",
"bars": {"HH:MM": {t, d, o, h, l, c, delta, total, bins: [{p, b, s}]}}}. About 150 KB a coin-day.

Research only. Nothing here trades.
"""
from __future__ import annotations

import io
import json
import os
import threading
import time
import urllib.request
import zipfile
from datetime import date, datetime, timedelta, timezone

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
OUT = os.path.join(ROOT, "data_store", "research", "ofhist")
COINS = {"BTC": (25.0, 73900.0), "ETH": (1.5, 2060.0), "SOL": (0.1, 83.2)}    # live row size and the price it was set at
DAYS = 365
URL = "https://data.binance.vision/data/futures/um/daily/aggTrades/{s}USDT/{s}USDT-aggTrades-{d}.zip"
PAUSE = 1.0                         # seconds between days, so the dashboard stays responsive

_CODE = os.path.getmtime(__file__) if os.path.exists(__file__) else 0
_J = globals().get("_J") or {"gen": 0, "thread": None, "status": {}, "stop": False}


def _step(coin: str, open_px: float) -> float:
    """Live row size scaled to the day's price, kept to two significant figures (25 -> 24, 1.5 -> 1.4 ...)."""
    from math import floor, log10
    base, at = COINS[coin]
    raw = base * open_px / at
    e = floor(log10(raw))
    return round(round(raw / 10 ** e, 1) * 10 ** e, 10)


def _path(coin: str, day: str) -> str:
    return os.path.join(OUT, coin, f"{day}.json")


def _download(coin: str, day: str) -> bytes | None:
    req = urllib.request.Request(URL.format(s=coin, d=day), headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None                 # not published yet (or no trading)
        raise


def build_day(coin: str, day: str, raw_zip: bytes) -> dict:
    import numpy as np
    import pandas as pd
    z = zipfile.ZipFile(io.BytesIO(raw_zip))
    name = z.namelist()[0]
    with z.open(name) as f:
        head = f.readline().decode().strip()
    has_header = not head.split(",")[0].isdigit()
    cols = ["agg_trade_id", "price", "quantity", "first_trade_id", "last_trade_id", "transact_time", "is_buyer_maker"]
    with z.open(name) as f:
        df = pd.read_csv(f, header=0 if has_header else None, names=None if has_header else cols,
                         usecols=["price", "quantity", "transact_time", "is_buyer_maker"])
    if df.empty:
        return {}
    ts = df["transact_time"].astype("int64")
    if ts.iloc[0] > 1e14:               # microseconds
        ts = ts // 1000
    bucket = (ts // 300000).astype("int64")
    px = df["price"].astype(float).values
    qty = df["quantity"].astype(float).values
    sell = df["is_buyer_maker"].astype(str).str.lower().isin(["true", "1"]).values
    step = _step(coin, float(px[0]))
    row = np.floor(px / step) * step
    g = pd.DataFrame({"b": bucket.values, "row": np.round(row, 10), "buy": np.where(sell, 0.0, qty), "sell": np.where(sell, qty, 0.0), "px": px})
    ohlc = g.groupby("b")["px"].agg(["first", "max", "min", "last"])
    cells = g.groupby(["b", "row"])[["buy", "sell"]].sum()
    bars = {}
    for b, r in ohlc.iterrows():
        t = datetime.fromtimestamp(int(b) * 300, tz=timezone.utc)
        hm = t.strftime("%H:%M")
        cs = cells.loc[b]
        bins = [{"p": round(float(p), 10), "b": round(float(v.buy), 6), "s": round(float(v.sell), 6)} for p, v in cs.iterrows()]
        tb, tsell = sum(x["b"] for x in bins), sum(x["s"] for x in bins)
        bars[hm] = {"t": hm, "d": day, "o": float(r["first"]), "h": float(r["max"]), "l": float(r["min"]), "c": float(r["last"]),
                    "delta": round(tb - tsell, 6), "total": round(tb + tsell, 6), "bins": bins}
    return {"step": step, "src": "binance-um-aggTrades", "built": datetime.now(timezone.utc).isoformat(timespec="seconds"), "bars": bars}


def _todo() -> list:
    last = datetime.now(timezone.utc).date() - timedelta(days=1)
    days = [(last - timedelta(days=i)).isoformat() for i in range(DAYS)]
    out = []
    for d in days:                      # newest first, so the most useful months land first
        for c in COINS:
            p = _path(c, d)
            if os.path.exists(p):
                continue
            m = p + ".missing"
            if os.path.exists(m) and time.time() - os.path.getmtime(m) < 86400:
                continue                # not published when last asked - ask again tomorrow
            out.append((c, d))
    return out


def _run(gen: int):
    st = _J["status"]
    st.update(state="running", started=datetime.now().isoformat(timespec="seconds"), done=0, failed=0, missing=0, last=None, err=None)
    while _J["gen"] == gen and not _J["stop"]:
        todo = _todo()
        st["left"] = len(todo)
        if not todo:
            st.update(state="up to date", left=0)
            # check again in an hour for the next published day
            for _ in range(360):
                if _J["gen"] != gen or _J["stop"]:
                    break
                time.sleep(10)
            continue
        coin, day = todo[0]
        t0 = time.time()
        try:
            raw = _download(coin, day)
            if raw is None:
                st["missing"] += 1
                # not published - mark so the loop moves on; the next pass tries again tomorrow
                os.makedirs(os.path.join(OUT, coin), exist_ok=True)
                open(_path(coin, day) + ".missing", "w").write(datetime.now().isoformat())
            else:
                res = build_day(coin, day, raw)
                os.makedirs(os.path.join(OUT, coin), exist_ok=True)
                tmp = _path(coin, day) + ".tmp"
                json.dump(res, open(tmp, "w"), separators=(",", ":"))
                os.replace(tmp, _path(coin, day))
                st["done"] += 1
                st["last"] = {"coin": coin, "day": day, "secs": round(time.time() - t0, 1), "mb": round(len(raw) / 1e6, 1), "bars": len(res.get("bars") or {})}
        except Exception as e:
            st["failed"] += 1
            st["err"] = f"{coin} {day}: {type(e).__name__}: {e}"[:200]
            time.sleep(30)
        time.sleep(PAUSE)
    st["state"] = "stopped"


def start() -> dict:
    # a reloaded file starts a fresh thread on the new code (the old loop sees the new gen and ends)
    if _J.get("code") != _CODE and _J.get("thread") is not None and _J["thread"].is_alive():
        _J["gen"] += 1
        _J["thread"] = None
    th = _J.get("thread")
    if th is not None and th.is_alive():
        return status()
    _J["gen"] += 1
    _J["stop"] = False
    th = threading.Thread(target=_run, args=(_J["gen"],), daemon=True, name="ofhistory")
    _J["thread"] = th
    _J["code"] = _CODE
    th.start()
    return status()


def stop() -> dict:
    _J["stop"] = True
    return status()


def status() -> dict:
    have = {}
    for c in COINS:
        try:
            fs = [f for f in os.listdir(os.path.join(OUT, c)) if f.endswith(".json")]
            have[c] = {"days": len(fs), "first": min(fs)[:10] if fs else None, "last": max(fs)[:10] if fs else None}
        except Exception:
            have[c] = {"days": 0}
    th = _J.get("thread")
    return {"running": bool(th and th.is_alive()), "status": _J.get("status"), "have": have, "target_days": DAYS}


def bars(coin: str, days: int = 400) -> list:
    """Every stored 5-minute bar for one coin, oldest first, in the engine's bar shape (rows ascending)."""
    folder = os.path.join(OUT, coin)
    try:
        files = sorted(f for f in os.listdir(folder) if f.endswith(".json"))[-days:]
    except Exception:
        return []
    out = []
    for f in files:
        try:
            day = json.load(open(os.path.join(folder, f)))
        except Exception:
            continue
        for t in sorted(day.get("bars") or {}):
            b = day["bars"][t]
            rows = sorted((float(x["p"]), float(x["b"]), float(x["s"])) for x in b["bins"])
            if not rows:
                continue
            buy = sum(r[1] for r in rows); sell = sum(r[2] for r in rows)
            out.append({"d": b["d"], "t": t, "o": b["o"], "h": b["h"], "l": b["l"], "c": b["c"], "buy": buy, "sell": sell, "delta": buy - sell,
                        "vol": buy + sell, "rows": rows, "step": day.get("step") or 1.0})
    return out
