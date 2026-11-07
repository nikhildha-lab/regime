"""CRYPTO DERIVATIVES HISTORY (2026-11-07, Nikhil: "yes fetch all possible 30 day data and find whether this theory is true ... identify related
patterns before big moves"; earlier "yes" to saving OI and long / short ratios daily).

Binance USDT-margined perpetuals, public data only (no account, never orders). Written to data_store/research/deriv/:
    kl15_<C>.csv   15-min candles, last ~30 days: ts, o, h, l, c, vol, taker_buy  (CVD = running sum of 2 x taker_buy - vol)
    kl1h_<C>.csv   1-hour candles, last 2 years (same columns) - for the long CVD test
    der15_<C>.csv  15-min, last 30 days (Binance keeps only 30): oi, oi_usd, top_pos (top traders long/short by position),
                   top_acc (top traders by accounts), glob_acc (all accounts), taker_bs (taker buy / sell volume ratio)
    fund_<C>.csv   funding rate every 8 h, last 2 years
start() runs the full download in a background thread (progress in _status.json). tick() (live-desk loop, at most hourly) adds the
newest 15-min rows to der15_ / kl15_ so the history keeps growing past Binance's 30-day window.
"""
from __future__ import annotations

import csv
import json
import os
import threading
import time
import urllib.request

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
OUT = os.path.join(ROOT, "data_store", "research", "deriv")
B = "https://fapi.binance.com"
COINS = ["BTC", "ETH", "SOL", "BNB", "XRP", "DOGE", "ADA", "AVAX", "LINK", "LTC", "DOT", "UNI", "ZEC", "DASH", "SUI"]
S: dict = {"running": False, "t": time.time()}          # first hourly top-up one hour after start
DER = {"top_pos": "/futures/data/topLongShortPositionRatio", "top_acc": "/futures/data/topLongShortAccountRatio",
       "glob_acc": "/futures/data/globalLongShortAccountRatio", "taker_bs": "/futures/data/takerlongshortRatio"}


def _get(path, **q):
    """GET via curl with a hard 20 s limit (urllib reads hung for good inside the server on 7 Nov, even with a timeout)."""
    import subprocess
    u = B + path + "?" + "&".join(f"{k}={v}" for k, v in q.items())
    err = None
    for i in range(3):
        try:
            r = subprocess.run(["curl", "-sS", "--max-time", "20", "-H", "User-Agent: market-pulse-research", u], capture_output=True, timeout=25)
            if r.returncode == 0 and r.stdout:
                return json.loads(r.stdout.decode())
            err = RuntimeError((r.stderr or b"").decode()[:160] or f"curl exit {r.returncode}")
        except Exception as e:
            err = e
        time.sleep(1 + i)
    raise err


def _status(**kw):
    os.makedirs(OUT, exist_ok=True)
    st = {}
    try:
        st = json.load(open(os.path.join(OUT, "_status.json")))
    except Exception:
        pass
    st.update(kw, at=time.strftime("%Y-%m-%d %H:%M:%S"))
    json.dump(st, open(os.path.join(OUT, "_status.json"), "w"), indent=1)


def _klines(sym, iv, start_ms, end_ms):
    step = {"15m": 900e3, "1h": 3600e3}[iv]
    rows, t = [], int(start_ms)
    while t < end_ms:
        k = _get("/fapi/v1/klines", symbol=sym, interval=iv, startTime=t, limit=1500)
        if not k:
            break
        rows += [[int(x[0]), x[1], x[2], x[3], x[4], x[5], x[9]] for x in k]
        t = int(k[-1][0] + step)
        time.sleep(0.15)
        if len(k) < 1500:
            break
    return rows


def _series(sym, path, field, start_ms, end_ms, period="15m"):
    out, t = {}, int(start_ms)
    while t < end_ms:
        x = _get(path, symbol=sym, period=period, startTime=t, endTime=min(end_ms, t + 499 * 900_000), limit=500)
        if not isinstance(x, list) or not x:
            t += 499 * 900_000
            continue
        for r in x:
            out[int(r["timestamp"])] = r
        nt = int(x[-1]["timestamp"]) + 900_000
        t = nt if nt > t else t + 499 * 900_000          # always move forward (guards against a page that does not advance)
        time.sleep(0.15)
    return out


def _write(path, header, rows, key=0):
    """Merge rows into a csv by the key column (newest wins)."""
    have = {}
    if os.path.exists(path):
        with open(path) as f:
            r = csv.reader(f); next(r, None)
            for x in r:
                have[x[key]] = x
    for x in rows:
        have[str(x[key])] = [str(v) for v in x]
    tmp = path + ".tmp"
    with open(tmp, "w", newline="") as f:
        w = csv.writer(f); w.writerow(header)
        for k in sorted(have, key=lambda s: int(float(s))):
            w.writerow(have[k])
    os.replace(tmp, path)


def fetch_coin(c, days15=30, years1h=2, quick=False):
    sym = c + "USDT"; now = int(time.time() * 1000)
    s15 = now - days15 * 86400_000 + 3600_000                 # Binance's derivative-data window is "last 30 days"
    _write(os.path.join(OUT, f"kl15_{c}.csv"), ["ts", "o", "h", "l", "c", "vol", "taker_buy"], _klines(sym, "15m", s15, now))
    _status(step=f"{c} open interest")
    oi = _series(sym, "/futures/data/openInterestHist", "sumOpenInterest", s15, now)
    D = {}
    for k, p in DER.items():
        _status(step=f"{c} {k}")
        D[k] = _series(sym, p, "", s15, now)
    rows = []
    for ts in sorted(set(oi) | set().union(*[set(v) for v in D.values()])):
        o = oi.get(ts, {})
        rows.append([ts, o.get("sumOpenInterest", ""), o.get("sumOpenInterestValue", ""),
                     D["top_pos"].get(ts, {}).get("longShortRatio", ""), D["top_acc"].get(ts, {}).get("longShortRatio", ""),
                     D["glob_acc"].get(ts, {}).get("longShortRatio", ""), D["taker_bs"].get(ts, {}).get("buySellRatio", "")])
    _write(os.path.join(OUT, f"der15_{c}.csv"), ["ts", "oi", "oi_usd", "top_pos", "top_acc", "glob_acc", "taker_bs"], rows)
    if quick:                                          # 2026-11-07: keep funding fresh too (the score uses it)
        try:
            x = _get("/fapi/v1/fundingRate", symbol=sym, startTime=now - 3 * 86400_000, limit=100)
            if isinstance(x, list) and x:
                _write(os.path.join(OUT, f"fund_{c}.csv"), ["ts", "rate"], [[int(r["fundingTime"]), r["fundingRate"]] for r in x])
        except Exception:
            pass
        return len(rows)
    _status(step=f"{c} 1-hour candles (2 years)")
    s1h = now - int(years1h * 365 * 86400_000)
    _write(os.path.join(OUT, f"kl1h_{c}.csv"), ["ts", "o", "h", "l", "c", "vol", "taker_buy"], _klines(sym, "1h", s1h, now))
    f, t = [], s1h
    while t < now:
        x = _get("/fapi/v1/fundingRate", symbol=sym, startTime=t, limit=1000)
        if not x:
            break
        f += [[int(r["fundingTime"]), r["fundingRate"]] for r in x]
        t = int(x[-1]["fundingTime"]) + 1
        time.sleep(0.15)
        if len(x) < 1000:
            break
    _write(os.path.join(OUT, f"fund_{c}.csv"), ["ts", "rate"], f)
    return len(rows)


def _run(coins):
    S["running"] = True
    done, errs = [], {}
    _status(state="running", coins=coins, done=[], errors={})
    for c in coins:
        try:
            n = fetch_coin(c)
            done.append(f"{c}:{n}")
        except Exception as e:
            errs[c] = str(e)[:160]
        _status(state="running", done=done, errors=errs)
    _status(state="done", done=done, errors=errs)
    S["running"] = False


def start(coins=None):
    if S["running"]:
        return {"ok": False, "detail": "already running"}
    threading.Thread(target=_run, args=(coins or COINS,), daemon=True, name="derivhist").start()
    return {"ok": True, "detail": f"downloading {len(coins or COINS)} coins in the background", "out": OUT}


def probe():
    now = int(time.time() * 1000); t0 = time.time(); s15 = now - 30 * 86400_000 + 3600_000
    calls = []
    t = s15
    for _ in range(3):
        x = _get("/futures/data/takerlongshortRatio", symbol="BTCUSDT", period="15m", startTime=t, endTime=min(now, t + 499 * 900_000), limit=500)
        calls.append([t, len(x) if isinstance(x, list) else str(x)[:80], x[0]["timestamp"] if isinstance(x, list) and x else None, x[-1]["timestamp"] if isinstance(x, list) and x else None])
        t = int(x[-1]["timestamp"]) + 900_000 if isinstance(x, list) and x else t + 499 * 900_000
    return {"secs": round(time.time() - t0, 2), "calls": calls}


def status():
    try:
        st = json.load(open(os.path.join(OUT, "_status.json")))
    except Exception:
        st = {"state": "never run"}
    import sys, traceback
    th = {t.ident: t.name for t in threading.enumerate() if t.name.startswith("derivhist")}
    st["threads"] = {f"{th[i]}#{i}": traceback.format_stack(f)[-3:] for i, f in sys._current_frames().items() if i in th}
    st["running_flag"] = S["running"]
    if os.path.exists(os.path.join(OUT, "_probe")):
        try:
            st["probe"] = probe()
        except Exception as e:
            st["probe"] = f"ERR {e}"
    return st


def tick():
    """At most hourly: add the newest 2 days of 15-min candles + derivatives data (keeps history beyond Binance's 30 days)."""
    if S["running"] or time.time() - S["t"] < 3600:
        return None
    S["t"] = time.time()

    def run():
        S["running"] = True
        try:
            for c in COINS:
                try:
                    fetch_coin(c, days15=2, quick=True)
                except Exception:
                    pass
            _status(last_tick=time.strftime("%Y-%m-%d %H:%M:%S"))
        finally:
            S["running"] = False
    threading.Thread(target=run, daemon=True, name="derivhist-tick").start()
    return True
