#!/usr/bin/env python3
"""Fetch REAL 5-min history for a date range from Kite (for a real backtest).

Loops the historical fetcher over every weekday in the range, writing one
dataset dir per day under datasets/<start>_to_<end>/<YYYY-MM-DD>/, ready for the
batch backtester.

    python -m src.kite_auth                     # refresh token first
    python tools/fetch_range.py --months 3
    python tools/fetch_range.py --start 2026-04-19 --end 2026-07-18
    python -m src.backtest --datasets datasets/2026-04-19_to_2026-07-18 --label real-3mo

Requires a valid Kite token and the historical-data entitlement. Skips weekends;
NSE holidays simply return empty and are skipped. Kite rate-limits historical
calls, so this paces itself.
"""
from __future__ import annotations

import argparse
import datetime as dt
import time
from pathlib import Path

from src.config import ROOT, load_config
from src.kite_auth import build_kite
from src.instruments import resolve_all
from src.historical_fetch import _write


def daterange_weekdays(start: dt.date, end: dt.date):
    d = start
    while d <= end:
        if d.weekday() < 5:
            yield d
        d += dt.timedelta(days=1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--months", type=int, default=None, help="fetch last N months")
    ap.add_argument("--start", default=None)
    ap.add_argument("--end", default=None)
    ap.add_argument("--interval", default="5minute")
    args = ap.parse_args()

    end = dt.date.fromisoformat(args.end) if args.end else dt.date.today() - dt.timedelta(days=1)
    if args.start:
        start = dt.date.fromisoformat(args.start)
    elif args.months:
        start = end - dt.timedelta(days=int(args.months * 30.5))
    else:
        start = end - dt.timedelta(days=92)

    cfg = load_config()
    if not cfg.access_token:
        raise SystemExit("No Kite token. Run: python -m src.kite_auth")
    kite = build_kite(cfg)
    resolved = resolve_all(kite, cfg)

    root = ROOT / "datasets" / f"{start.isoformat()}_to_{end.isoformat()}"
    print(f"Fetching {start} .. {end} -> {root}")
    ok = 0
    for day in daterange_weekdays(start, end):
        frm = dt.datetime.combine(day, dt.time(9, 15))
        to = dt.datetime.combine(day, dt.time(15, 30))
        outdir = root / day.isoformat()
        wrote = False
        for key, ri in resolved.items():
            try:
                spot = kite.historical_data(ri.spot_token, frm, to, args.interval)
                if spot:
                    _write(outdir / f"{key}_spot.csv", spot, with_oi=False)
                    fut = kite.historical_data(ri.fut_token, frm, to, args.interval, oi=True)
                    _write(outdir / f"{key}_fut.csv", fut, with_oi=True)
                    wrote = True
            except Exception as e:
                print(f"  {day} {key}: {e}")
            time.sleep(0.35)   # be gentle with Kite rate limits
        if wrote:
            ok += 1
            print(f"  {day}: ok")
    print(f"\nDone: {ok} trading days written under {root}")
    print(f"Backtest it:\n  python -m src.backtest --datasets {root.relative_to(ROOT)} --label real-{start}_{end}")


if __name__ == "__main__":
    main()
