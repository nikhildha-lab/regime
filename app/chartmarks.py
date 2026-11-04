"""
Chart annotations for the order-flow (footprint) chart (2026-10-25, Nikhil: "things that were observed should be pointed on charts").
Descriptive only - not tested as signals. Every event is logged so it can be graded later.

  OI tag per bar  : price direction x OI change over the bar ->
                    SC short covering (price up, OI down) · NL new longs (up, up) · NS new shorts (down, up) · LX longs exiting (down, down)
                    sources: crypto = Binance 5-min open interest; Nifty / Bank Nifty = near-month futures 1-min OI (Dhan file)
  burst           : bar whose |buy - sell| is >= 3x the median of the bars shown
  setup events    : the tracker's setup turning "ready" / flipping side (absorption reversal, fade, continuation) - logged
                    from now on to data_store/livedesk/marks/<desk>-<date>.jsonl
"""
from __future__ import annotations

import json
import os
import time
import urllib.request
from datetime import datetime, timedelta

import pandas as pd

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
MDIR = os.path.join(ROOT, "data_store", "livedesk", "marks")
FUT1M = os.path.join(ROOT, "data_store", "market_data", "nifty_futures_1m_upstox.csv")
BINANCE = {"BTC": "BTCUSDT", "ETH": "ETHUSDT", "SOL": "SOLUSDT"}
THR = {"crypto": 0.05, "nifty": 0.03}          # % OI change below this = no tag
C: dict = {"last": {}}


def _binance_oi(sym):
    k = ("b", sym)
    x = C.get(k)
    if x and time.time() - x[0] < 60:
        return x[1]
    url = f"https://fapi.binance.com/futures/data/openInterestHist?symbol={sym}&period=5m&limit=500"
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "synaptic"}), timeout=10) as r:
        j = json.loads(r.read().decode())
    s = pd.Series([float(r["sumOpenInterest"]) for r in j],
                  index=pd.to_datetime([int(r["timestamp"]) for r in j], unit="ms"))   # Binance OI history = snapshot at that timestamp
    C[k] = (time.time(), s)
    return s


def _nifty_oi(key):
    k = ("n", key)
    try:
        mt = os.path.getmtime(FUT1M)
    except OSError:
        return None
    x = C.get(k)
    if x and x[0] == mt:
        return x[1]
    d = pd.read_csv(FUT1M, usecols=["contract", "ts", "volume", "oi"])
    d = d[d.contract.str.startswith(key + "_FUT")]
    d["ts"] = pd.to_datetime(d.ts).dt.tz_convert("Asia/Kolkata").dt.tz_localize(None)
    d["day"] = d.ts.dt.normalize()
    near = d.groupby(["day", "contract"]).volume.sum().reset_index().sort_values("volume").groupby("day").tail(1)   # most traded contract each day
    d = d.merge(near[["day", "contract"]], on=["day", "contract"])
    s = d.drop_duplicates("ts", keep="last").set_index("ts").sort_index()["oi"].astype(float) 
    s.index = s.index + pd.Timedelta(minutes=1)
    C[k] = (mt, s)
    return s


def oi_series(desk, key):
    try:
        if desk == "crypto" and key in BINANCE:
            return _binance_oi(BINANCE[key])
        if desk == "nifty" and key in ("NIFTY", "BANKNIFTY"):
            return _nifty_oi(key)
    except Exception as e:
        C["err"] = str(e)[:120]
    return None


def annotate(desk, key, bars, tf):
    """Adds oi / oi_pct / oi_tag / burst to each bar (in place) and returns the OI line [(i, oi)]."""
    if not bars:
        return []
    s = oi_series(desk, key)
    line = []
    for i, b in enumerate(bars):
        b["oi_tag"] = None
        if s is None or not len(s) or not b.get("d") or not b.get("t"):
            continue
        try:
            st = pd.Timestamp(f"{b['d']} {str(b['t'])[:5]}")
        except Exception:
            continue
        en = st + pd.Timedelta(minutes=tf)
        a = s[s.index <= st]; z = s[s.index <= en]
        if not len(a) or not len(z) or (st - a.index[-1]) > pd.Timedelta(minutes=30) or z.index[-1] <= st:
            continue
        o0, o1 = float(a.iloc[-1]), float(z.iloc[-1])
        pct = (o1 / o0 - 1) * 100 if o0 else 0.0
        b["oi"] = o1; b["oi_pct"] = round(pct, 3)
        line.append((i, o1))
        up = (b.get("c") or 0) >= (b.get("o") or 0)
        if abs(pct) >= THR.get(desk, 0.05):
            b["oi_tag"] = ("NL" if pct > 0 else "SC") if up else ("NS" if pct > 0 else "LX")
    ds = sorted(abs(b.get("delta") or 0) for b in bars if b.get("total"))
    med = ds[len(ds) // 2] if ds else 0
    for b in bars:
        b["burst"] = bool(med and abs(b.get("delta") or 0) >= 3 * med and abs(b.get("delta") or 0) > 0)
    return line


def record(desk, watch, t):
    """Log setup changes (ready / side flips) so they can be drawn on the chart and graded later."""
    last = C["last"].setdefault(desk, {})
    out = []
    for key, w in (watch or {}).items():
        # 2026-10-25: log only a real change - the setup becoming ready, or flipping side - and at most once per 10 min per market
        ready = w.get("state") in ("ready", "triggered", "active") and bool(w.get("side"))
        sig = (ready, w.get("side") if ready else None)
        prev = last.get(key); last[key] = sig
        if prev is None or sig == prev or not ready:
            continue
        lt = C.setdefault("last_t", {}).get((desk, key), 0)
        if time.time() - lt < 600:
            continue
        C["last_t"][(desk, key)] = time.time()
        out.append({"at": t.isoformat(timespec="seconds"), "key": key, "state": w.get("state"), "side": w.get("side"),
                    "setup": w.get("setup"), "level": w.get("level"), "grade": w.get("grade"), "spot": w.get("spot")})
    if out:
        os.makedirs(MDIR, exist_ok=True)
        with open(os.path.join(MDIR, f"{desk}-{t:%Y-%m-%d}.jsonl"), "a") as f:
            for e in out:
                f.write(json.dumps(e) + "\n")
    return out


def events(desk, key, days=2):
    out = []
    for k in range(days):
        d = (datetime.now() - timedelta(days=k)).strftime("%Y-%m-%d")
        try:
            for ln in open(os.path.join(MDIR, f"{desk}-{d}.jsonl")):
                e = json.loads(ln)
                if e.get("key") == key:
                    out.append(e)
        except Exception:
            pass
    return out
