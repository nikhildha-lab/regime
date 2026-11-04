"""Fetch REAL 5-min historical bars from Kite into a replay dataset.

    python -m src.historical_fetch --date 2025-11-12
    python -m src.historical_fetch --date 2025-11-12 --interval 5minute

Pulls, for each configured instrument, the spot index and the near-month futures
(with volume + OI) for the given date, and writes them to
`datasets/<date>/<KEY>_{spot,fut}.csv` — ready for:

    python run.py --mode replay --dataset datasets/2025-11-12

Requires a valid Kite token (run `python -m src.kite_auth` first) and the
historical-data API entitlement on your Kite Connect app.

Notes
-----
* Option-chain OI history for a *past* day needs that day's specific option
  instrument tokens (which differ per expiry). This fetcher covers spot + futures
  (which fully power price action, volume profile, market profile, and
  imbalance). Live OI is the intended source for the OI panel; historical OI
  replay per strike is a documented future enhancement.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
from pathlib import Path

from .config import ROOT, load_config
from .kite_auth import build_kite
from .instruments import resolve_all, _as_date


def _write(path: Path, rows: list[dict], with_oi: bool):
    path.parent.mkdir(parents=True, exist_ok=True)
    header = ["datetime", "open", "high", "low", "close"] + \
             (["volume", "oi"] if with_oi else [])
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        for r in rows:
            ts = r["date"]
            ts = ts.strftime("%Y-%m-%d %H:%M") if hasattr(ts, "strftime") else str(ts)
            line = [ts, r["open"], r["high"], r["low"], r["close"]]
            if with_oi:
                line += [r.get("volume", 0), r.get("oi", 0)]
            w.writerow(line)


def fetch(date_str: str, interval: str = "5minute"):
    cfg = load_config()
    if not cfg.access_token:
        raise SystemExit("No Kite token. Run: python -m src.kite_auth")
    kite = build_kite(cfg)
    resolved = resolve_all(kite, cfg)

    day = dt.datetime.strptime(date_str, "%Y-%m-%d").date()
    frm = dt.datetime.combine(day, dt.time(9, 15))
    to = dt.datetime.combine(day, dt.time(15, 30))
    out_dir = ROOT / "datasets" / date_str

    for key, ri in resolved.items():
        # spot (index: no volume/oi)
        try:
            spot = kite.historical_data(ri.spot_token, frm, to, interval)
            _write(out_dir / f"{key}_spot.csv", spot, with_oi=False)
        except Exception as e:
            print(f"[hist] {key} spot failed: {e}")
        # futures (volume + oi)
        try:
            fut = kite.historical_data(ri.fut_token, frm, to, interval, oi=True)
            _write(out_dir / f"{key}_fut.csv", fut, with_oi=True)
        except Exception as e:
            print(f"[hist] {key} fut failed: {e}")
        print(f"[hist] {key}: wrote {out_dir}/{key}_spot.csv, {key}_fut.csv")

    print(f"\nReplay it:\n  python run.py --mode replay --dataset datasets/{date_str}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", required=True, help="YYYY-MM-DD (a past trading day)")
    ap.add_argument("--interval", default="5minute",
                    help="Kite interval: minute, 3minute, 5minute, 15minute…")
    args = ap.parse_args()
    fetch(args.date, args.interval)
