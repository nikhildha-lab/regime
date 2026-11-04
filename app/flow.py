"""
Order-flow data (runs in its own background thread inside the app, so the 5-minute dashboard loop is never blocked).

Downloads approved by the user on 2026-10-04:
  * Binance USD-M futures, from data.binance.vision (free daily/monthly files):
      - 1-minute klines incl. taker-buy volume -> delta / CVD per minute       (BTC, ETH, SOL; 2022 ->)
      - metrics: open interest + long/short ratios every 5 minutes             (BTC, ETH, SOL; 2022 ->)
      - bookDepth: resting size within +-1..5% of price, ~every 30 s -> 1 min  (BTC, ETH; 2023 ->)
      - aggTrades: every trade with aggressor side -> 5-min FOOTPRINTS          (BTC, ETH, SOL; newest first,
        capped at ~3 GB of downloads per coin, then kept current day by day)
  * Dhan: Nifty / Bank Nifty futures 1-minute candles with open interest (2026-10-24: replaces Upstox; old file name kept)

Each call of step() does a bounded amount of work (about a minute) and records progress, so the thread can be
stopped and restarted at any time without losing or duplicating data. Raw zips are never kept: they are reduced
to small per-month / per-day files under data_store/flow/.
"""
from __future__ import annotations

import io
import json
import os
import time
import traceback
import zipfile
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd

import updater as U

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
FLOW = os.path.join(ROOT, "data_store", "flow")
PROG = os.path.join(FLOW, "progress.json")
BV = "https://data.binance.vision/data/futures/um"
COINS = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
BOOK_COINS = ["BTCUSDT", "ETHUSDT"]
FP_BUCKET = {"BTCUSDT": 50.0, "ETHUSDT": 2.0, "SOLUSDT": 0.1}
AGG_BYTES_CAP = 3 * 1024 ** 3
K_START, M_START, B_START = date(2022, 1, 1), date(2022, 1, 1), date(2023, 1, 1)
UP_1M = os.path.join(ROOT, "data_store", "market_data", "nifty_futures_1m_upstox.csv")   # name kept; data from Dhan


# ------------------------------------------------------------------ helpers
def _prog() -> dict:
    try:
        return json.load(open(PROG))
    except Exception:
        return {}


def _save_prog(p: dict):
    os.makedirs(FLOW, exist_ok=True)
    with open(PROG + ".tmp", "w") as f:
        json.dump(p, f, indent=1, default=str)
    os.replace(PROG + ".tmp", PROG)


def _download(url: str) -> bytes | None:
    """Bytes of the file, or None when it does not exist (404)."""
    try:
        if U._CSESSION is not None:
            r = U._CSESSION.get(url, timeout=180)
            if r.status_code == 404:
                return None
            if r.status_code != 200:
                raise RuntimeError(f"HTTP {r.status_code}")
            return r.content
        return U.http_get(url, timeout=180)
    except U.NotFound:
        return None


def _csv_from_zip(blob: bytes, names: list[str]) -> pd.DataFrame:
    z = zipfile.ZipFile(io.BytesIO(blob))
    raw = z.read(z.namelist()[0])
    first = raw[:200].split(b"\n")[0].split(b",")[0].strip()
    has_header = not first.replace(b".", b"").replace(b"-", b"").isdigit()
    return pd.read_csv(io.BytesIO(raw), header=0 if has_header else None, names=None if has_header else names)


def _ms(x: pd.Series) -> pd.Series:
    """Epoch ms or us -> UTC timestamps (Binance switched some files to microseconds in 2025)."""
    x = pd.to_numeric(x).astype("int64")
    return pd.Series(pd.to_datetime(np.where(x > 1e14, x // 1000, x), unit="ms", utc=True), index=x.index)


def _merge_month(path: str, new: pd.DataFrame, key: str = "ts"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.path.exists(path):
        old = pd.read_csv(path)
        new = pd.concat([old, new.astype({key: str})], ignore_index=True)
    new[key] = new[key].astype(str)
    new = new.drop_duplicates(key, keep="last").sort_values(key)
    tmp = path + ".tmp"
    new.to_csv(tmp, index=False, compression="gzip")
    os.replace(tmp, path)


def _months(a: date, b: date):
    y, m = a.year, a.month
    while (y, m) <= (b.year, b.month):
        yield y, m
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)


# ------------------------------------------------------------------ parsers (unit-tested offline)
K_COLS = ["open_time", "open", "high", "low", "close", "volume", "close_time", "quote_volume", "count",
          "taker_buy_volume", "taker_buy_quote_volume", "ignore"]


def parse_klines(df: pd.DataFrame) -> pd.DataFrame:
    df.columns = K_COLS[:len(df.columns)]
    out = pd.DataFrame({"ts": _ms(df["open_time"]).dt.strftime("%Y-%m-%d %H:%M"),
                        "open": df["open"].astype(float), "high": df["high"].astype(float), "low": df["low"].astype(float),
                        "close": df["close"].astype(float), "volume": df["volume"].astype(float),
                        "buy_volume": df["taker_buy_volume"].astype(float), "trades": df["count"].astype(int)})
    out["delta"] = 2 * out["buy_volume"] - out["volume"]          # aggressive buys minus aggressive sells
    return out


def parse_metrics(df: pd.DataFrame) -> pd.DataFrame:
    df = df.rename(columns={"create_time": "ts"})
    df["ts"] = pd.to_datetime(df["ts"]).dt.strftime("%Y-%m-%d %H:%M")
    keep = [c for c in ["ts", "sum_open_interest", "sum_open_interest_value", "count_toptrader_long_short_ratio",
                        "sum_toptrader_long_short_ratio", "count_long_short_ratio", "sum_taker_long_short_vol_ratio"] if c in df]
    return df[keep]


def parse_bookdepth(df: pd.DataFrame) -> pd.DataFrame:
    """Rows (timestamp, percentage, depth, notional) -> one row per minute with resting notional within
    +-1%, 2%, 5% of price (cumulative) and the bid/ask imbalance at 1% and 5%."""
    df["ts"] = pd.to_datetime(df["timestamp"]).dt.floor("min")
    p = df.pivot_table(index="ts", columns="percentage", values="notional", aggfunc="last")
    out = pd.DataFrame(index=p.index)
    for k in (1, 2, 5):
        if -k in p.columns and k in p.columns:
            out[f"bid{k}"] = p[-k]
            out[f"ask{k}"] = p[k]
    for k in (1, 5):
        if f"bid{k}" in out:
            out[f"imb{k}"] = (out[f"bid{k}"] - out[f"ask{k}"]) / (out[f"bid{k}"] + out[f"ask{k}"])
    out.index = out.index.strftime("%Y-%m-%d %H:%M")
    return out.reset_index().rename(columns={"index": "ts"})


A_COLS = ["agg_trade_id", "price", "quantity", "first_trade_id", "last_trade_id", "transact_time", "is_buyer_maker"]


def footprint(df: pd.DataFrame, bucket: float, bar_min: int = 5) -> pd.DataFrame:
    """aggTrades -> footprint: for every 5-minute bar and price bucket, volume bought and sold aggressively.
    is_buyer_maker True = the buyer was resting, so the SELLER hit the bid (an aggressive sell)."""
    df.columns = A_COLS[:len(df.columns)]
    t = _ms(df["transact_time"]).dt.floor(f"{bar_min}min")
    m = df["is_buyer_maker"].astype(str).str.lower().isin(["true", "1"])
    q = df["quantity"].astype(float)
    px = (np.floor(df["price"].astype(float) / bucket) * bucket).round(6)
    g = pd.DataFrame({"ts": t.dt.strftime("%Y-%m-%d %H:%M"), "price": px, "buy": np.where(~m, q, 0.0), "sell": np.where(m, q, 0.0)})
    return g.groupby(["ts", "price"], as_index=False)[["buy", "sell"]].sum()


# ------------------------------------------------------------------ Binance jobs (each does a bounded unit of work)
def job_klines(p: dict, budget_s: float) -> int:
    t0, n = time.time(), 0
    today = datetime.utcnow().date()
    for s in COINS:
        st = p.setdefault("klines", {}).setdefault(s, {"month_done": None, "day_done": None})
        for y, m in _months(K_START, today):
            if time.time() - t0 > budget_s:
                return n
            ym = f"{y}-{m:02d}"
            month_complete = (y, m) < (today.year, today.month)
            if month_complete and st["month_done"] and ym <= st["month_done"]:
                continue
            path = os.path.join(FLOW, "binance_1m", s, f"{ym}.csv.gz")
            if month_complete:
                blob = _download(f"{BV}/monthly/klines/{s}/1m/{s}-1m-{ym}.zip")
                if blob is None:                      # monthly file not published yet: use daily files
                    month_complete = False
                else:
                    _merge_month(path, parse_klines(_csv_from_zip(blob, K_COLS)))
                    st["month_done"] = ym
                    n += 1
                    _save_prog(p)
                    continue
            d = max(date(y, m, 1), date.fromisoformat(st["day_done"]) + timedelta(days=1) if st["day_done"] else date(y, m, 1))
            while d < today and d.month == m and time.time() - t0 < budget_s:
                blob = _download(f"{BV}/daily/klines/{s}/1m/{s}-1m-{d}.zip")
                if blob is None:
                    break                              # today's/yesterday's file not out yet
                _merge_month(path, parse_klines(_csv_from_zip(blob, K_COLS)))
                st["day_done"] = str(d)
                n += 1
                _save_prog(p)
                d += timedelta(days=1)
    return n


def _daily_job(p, name, coins, start, url_fn, parse_fn, folder, budget_s, t0) -> int:
    n = 0
    today = datetime.utcnow().date()
    for s in coins:
        st = p.setdefault(name, {}).setdefault(s, {"day_done": None, "missing": 0})
        d = date.fromisoformat(st["day_done"]) + timedelta(days=1) if st["day_done"] else start
        batch, last_d = [], None
        while d < today and time.time() - t0 < budget_s:
            blob = _download(url_fn(s, d))
            if blob is None:
                if (today - d).days <= 3:
                    break                              # not published yet
                st["missing"] += 1                     # a gap in Binance's archive: skip the day
            else:
                batch.append(parse_fn(_csv_from_zip(blob, [])))
                n += 1
            last_d = d
            d += timedelta(days=1)
            if len(batch) >= 10 or (batch and d.day == 1):
                _flush(batch, folder, s)
                batch = []
                st["day_done"] = str(last_d)
                _save_prog(p)
        if batch:
            _flush(batch, folder, s)
        if last_d:
            st["day_done"] = str(last_d)
            _save_prog(p)
    return n


def _flush(batch, folder, s):
    allr = pd.concat(batch, ignore_index=True)
    allr["_m"] = allr["ts"].astype(str).str[:7]
    for ym, g in allr.groupby("_m"):
        _merge_month(os.path.join(FLOW, folder, s, f"{ym}.csv.gz"), g.drop(columns="_m"))


def job_metrics(p, budget_s):
    t0 = time.time()
    return _daily_job(p, "metrics", COINS, M_START, lambda s, d: f"{BV}/daily/metrics/{s}/{s}-metrics-{d}.zip",
                      parse_metrics, "binance_metrics", budget_s, t0)


def job_bookdepth(p, budget_s):
    t0 = time.time()
    return _daily_job(p, "bookdepth", BOOK_COINS, B_START, lambda s, d: f"{BV}/daily/bookDepth/{s}/{s}-bookDepth-{d}.zip",
                      parse_bookdepth, "binance_book", budget_s, t0)


def job_aggtrades(p, budget_s) -> int:
    """Newest days first (yesterday backwards) until ~3 GB per coin has been downloaded; then only new days."""
    t0, n = time.time(), 0
    today = datetime.utcnow().date()
    for s in COINS:
        st = p.setdefault("aggtrades", {}).setdefault(s, {"newest": None, "oldest": None, "bytes": 0})
        todo = []
        newest = date.fromisoformat(st["newest"]) if st["newest"] else None
        d = today - timedelta(days=1 if newest else 2)
        while newest is None or d > newest:            # forward: days newer than what we have
            todo.append(d)
            d -= timedelta(days=1)
            if newest is None:
                break
        if st["oldest"] and st["bytes"] < AGG_BYTES_CAP:
            o = date.fromisoformat(st["oldest"])
            todo += [o - timedelta(days=i) for i in range(1, 6)]               # up to 5 more days back per round
        for d in todo:
            if time.time() - t0 > budget_s:
                return n
            out = os.path.join(FLOW, "footprint", s, f"{d}.csv.gz")
            if os.path.exists(out):
                continue
            blob = _download(f"{BV}/daily/aggTrades/{s}/{s}-aggTrades-{d}.zip")
            if blob is None:
                if (today - d).days > 3 and st["oldest"] and d < date.fromisoformat(st["oldest"]):
                    st["oldest"] = str(d)              # archive gap: step past it
                    _save_prog(p)
                continue
            st["bytes"] += len(blob)
            fp = footprint(_csv_from_zip(blob, A_COLS), FP_BUCKET[s])
            del blob
            os.makedirs(os.path.dirname(out), exist_ok=True)
            fp.to_csv(out + ".tmp", index=False, compression="gzip")
            os.replace(out + ".tmp", out)
            st["newest"] = str(max(d, date.fromisoformat(st["newest"])) if st["newest"] else d)
            st["oldest"] = str(min(d, date.fromisoformat(st["oldest"])) if st["oldest"] else d)
            n += 1
            _save_prog(p)
    return n


# ------------------------------------------------------------------ Dhan 1-minute futures with OI (2026-10-24: replaces Upstox)
def job_futures_1m(p, budget_s) -> int:
    st = p.setdefault("futures_1m", {"last_run": None})
    now = pd.Timestamp.now(tz=U.IST)
    if st["last_run"] and (now - pd.Timestamp(st["last_run"])) < pd.Timedelta(minutes=3):
        return 0
    st["last_run"] = str(now)
    today = now.date()
    cons = [c for c in U.futures_contracts() if c["expiry"] >= str(today)]
    old = pd.read_csv(UP_1M, usecols=["contract", "ts"]) if os.path.exists(UP_1M) else pd.DataFrame(columns=["contract", "ts"])
    frames = []
    for c in cons:
        o = old[old["contract"] == c["contract"]]
        s = pd.Timestamp(o["ts"].max()).tz_convert(U.IST).date() if len(o) else today - timedelta(days=95)
        try:
            df = U.dhan_futures_bars(c, s, today, 1)
        except Exception as e:
            st["error"] = f"{c['contract']}: {str(e)[:120]}"
            continue
        if len(df):
            df = df[(pd.to_datetime(df["ts"]) + pd.Timedelta(minutes=1)) <= now]
            df.insert(0, "instrument_key", c["instrument_key"])
            df.insert(0, "contract", c["contract"])
            frames.append(df)
    _save_prog(p)
    if not frames:
        return 0
    return U.merge_rows(UP_1M, pd.concat(frames, ignore_index=True), ["contract", "ts"], ["contract", "ts"])


# ------------------------------------------------------------------ spot DAILY candles (long history for the HMM / GMM models)
SPOT = "https://data.binance.vision/data/spot"
SPOT_DAILY = {"ETHUSDT": date(2017, 8, 1), "SOLUSDT": date(2020, 8, 1)}
SPOT_DAILY_CSV = os.path.join(ROOT, "data_store", "market_data", "binance_daily.csv")


def parse_spot_daily(df: pd.DataFrame, sym: str) -> pd.DataFrame:
    df.columns = K_COLS[:len(df.columns)]
    return pd.DataFrame({"date": _ms(df["open_time"]).dt.strftime("%Y-%m-%d"), "symbol": sym,
                         "open": df["open"].astype(float), "high": df["high"].astype(float), "low": df["low"].astype(float),
                         "close": df["close"].astype(float), "volume": df["volume"].astype(float)})


def _save_spot(rows: list):
    if not rows:
        return
    new = pd.concat(rows, ignore_index=True)
    if os.path.exists(SPOT_DAILY_CSV):
        new = pd.concat([pd.read_csv(SPOT_DAILY_CSV), new], ignore_index=True)
    new = new.drop_duplicates(["symbol", "date"], keep="last").sort_values(["symbol", "date"])
    new.to_csv(SPOT_DAILY_CSV + ".tmp", index=False)
    os.replace(SPOT_DAILY_CSV + ".tmp", SPOT_DAILY_CSV)


def job_spot_daily(p: dict, budget_s: float) -> int:
    """Whole months from the monthly files, the current month day by day. Small files (~3 KB each)."""
    t0, n, rows = time.time(), 0, []
    today = datetime.utcnow().date()
    try:
        for s, start in SPOT_DAILY.items():
            st = p.setdefault("spot_daily", {}).setdefault(s, {"month_done": None, "day_done": None})
            for y, m in _months(start, today):
                if time.time() - t0 > budget_s:
                    return n
                ym = f"{y}-{m:02d}"
                if (y, m) < (today.year, today.month):
                    if st["month_done"] and ym <= st["month_done"]:
                        continue
                    blob = _download(f"{SPOT}/monthly/klines/{s}/1d/{s}-1d-{ym}.zip")
                    if blob is not None:
                        rows.append(parse_spot_daily(_csv_from_zip(blob, K_COLS), s))
                        st["month_done"] = ym
                        n += 1
                        continue
                d = max(date(y, m, 1), date.fromisoformat(st["day_done"]) + timedelta(days=1) if st["day_done"] else date(y, m, 1))
                while d < today and d.month == m and time.time() - t0 < budget_s:
                    blob = _download(f"{SPOT}/daily/klines/{s}/1d/{s}-1d-{d}.zip")
                    if blob is None:
                        break
                    rows.append(parse_spot_daily(_csv_from_zip(blob, K_COLS), s))
                    st["day_done"] = str(d)
                    n += 1
                    d += timedelta(days=1)
    finally:
        _save_spot(rows)                               # data first, then progress
        _save_prog(p)
    return n


# ------------------------------------------------------------------ one round
# 2026-11-01 audit (Nikhil: "only store what is required"): metrics, bookDepth and aggTrades-footprint downloads stopped -
# only research scripts read them (scripts/test_order_flow.py). Kept: spot daily + 1-min klines (the export uses them).
JOBS = [("spot_daily", job_spot_daily, 40), ("klines", job_klines, 60)]


def step() -> dict:
    p = _prog()
    out = {}
    for name, fn, budget in JOBS:
        try:
            out[name] = fn(p, budget)
        except Exception as e:
            out[name] = f"error: {str(e)[:150]}"
            p.setdefault("errors", {})[name] = {"at": str(datetime.now()), "error": traceback.format_exc()[-400:]}
            _save_prog(p)
    p["last_round"] = {"at": str(datetime.now()), "result": out}
    _save_prog(p)
    return out


def status() -> dict:
    p = _prog()
    return {"last_round": p.get("last_round"), "klines": p.get("klines"), "metrics": p.get("metrics"),
            "bookdepth": p.get("bookdepth"), "aggtrades": p.get("aggtrades"),
            "recorder": p.get("recorder"), "spot_daily": p.get("spot_daily")}
