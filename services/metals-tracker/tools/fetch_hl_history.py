#!/usr/bin/env python3
"""Fetch REAL 5-min Hyperliquid history into per-day replay datasets.

    python tools/fetch_hl_history.py --days 30
    python tools/fetch_hl_history.py --days 14 --interval 5m --label hl-2wk

Writes, for each configured coin and each UTC day:

    datasets/<label>/<YYYY-MM-DD>/<KEY>_perp.csv   datetime,open,high,low,close,volume,oi
    datasets/<label>/<YYYY-MM-DD>/<KEY>_ctx.csv    datetime,mark,funding,oi   (funding history)

Then backtest it:

    python -m src.backtest --datasets datasets/<label> --label real-<days>d
    open backtests/real-<days>d/report.html

All public Hyperliquid data — no keys. (Per-candle OI history isn't exposed by the
candle API, so the perp `oi` column is left 0; the ctx file carries hourly funding.)
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.config import load_config                      # noqa: E402
from src.hl_data import HLData                          # noqa: E402

_DAY_MS = 86_400_000


def _write_perp(path: Path, bars: list[dict]):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["datetime", "open", "high", "low", "close", "volume", "oi"])
        for k in bars:
            ts = dt.datetime.utcfromtimestamp(k["t"] / 1000.0).strftime("%Y-%m-%d %H:%M")
            w.writerow([ts, k["o"], k["h"], k["l"], k["c"], round(k.get("v", 0) or 0, 6), 0])


def _write_ctx(path: Path, fund: list[dict]):
    if not fund:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["datetime", "mark", "funding", "oi"])
        for r in fund:
            ts = dt.datetime.utcfromtimestamp(r["time"] / 1000.0).strftime("%Y-%m-%d %H:%M")
            w.writerow([ts, "", r["funding"], ""])


def fetch(days: int, interval: str, label: str):
    cfg = load_config()
    client = HLData(cfg.info_url)
    out_root = ROOT / "datasets" / label
    today = dt.datetime.utcnow().date()

    for coin_cfg in cfg.instruments:
        coin, key = coin_cfg.coin, coin_cfg.key
        # funding history for the whole window (hourly), bucket by day
        fund_all = client.funding_history(coin, hours=days * 24 + 4)
        fund_by_day: dict = {}
        for r in fund_all:
            d = dt.datetime.utcfromtimestamp(r["time"] / 1000.0).date()
            fund_by_day.setdefault(d, []).append(r)

        for i in range(days):
            day = today - dt.timedelta(days=days - i)
            start = int(dt.datetime.combine(day, dt.time()).replace(
                tzinfo=dt.timezone.utc).timestamp() * 1000)
            end = start + _DAY_MS
            try:
                bars = client.candles(coin, interval, start_ms=start, end_ms=end)
            except Exception as e:
                print(f"[hist] {key} {day} failed: {e}")
                continue
            if not bars:
                continue
            day_dir = out_root / day.isoformat()
            _write_perp(day_dir / f"{key}_perp.csv", bars)
            _write_ctx(day_dir / f"{key}_ctx.csv", fund_by_day.get(day, []))
        print(f"[hist] {key}: wrote {days} day-folders under {out_root}")

    print(f"\nBacktest it:\n  python -m src.backtest --datasets datasets/{label} --label real-{days}d")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--interval", default="5m")
    ap.add_argument("--label", default=None)
    args = ap.parse_args()
    fetch(args.days, args.interval, args.label or f"hl-{args.days}d")
