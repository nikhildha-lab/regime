#!/usr/bin/env python3
"""Fetch REAL 5-min intraday history from Kite, reusing your existing NSE setup.

Reuses ~/Documents/NSE/kite_config.py (API key) and ~/Documents/NSE/data/
kite_token.json (today's access token) — the SAME credentials your
gann_trade/fetch_real_data.py already uses. No new API app needed.

    # refresh token only if it's not today's:
    cd ~/Documents/NSE && python3 kite_login.py
    # then, from the tracker project:
    cd ~/Documents/nifty-live-tracker && source .venv/bin/activate
    python tools/fetch_kite_intraday.py --months 3

Writes datasets/<start>_to_<end>/<YYYY-MM-DD>/<KEY>_{spot,fut}.csv (replay
format), ready for:  python -m src.backtest --datasets datasets/<range> --label real-3mo

What it fetches per day: 5-min SPOT (NIFTY, BANKNIFTY) for price action + market
profile, and the current near-month FUTURES (volume + OI) for volume profile +
imbalance. Per-strike option OI is live-only (expired option contracts aren't
retrievable historically), so OI-chain signals are absent in this backtest.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import sys
import time
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NSE_DIR = Path.home() / "Documents" / "NSE"

SPOT_TOKENS = {"NIFTY": 256265, "BANKNIFTY": 260105}
FUT_NAMES = {"NIFTY": "NIFTY", "BANKNIFTY": "BANKNIFTY"}


def _load_kite():
    if not NSE_DIR.exists():
        sys.exit(f"NSE dir not found at {NSE_DIR}. Edit NSE_DIR at top of this script.")
    sys.path.insert(0, str(NSE_DIR))
    try:
        from kiteconnect import KiteConnect
    except ImportError:
        sys.exit("kiteconnect missing. Run: pip install kiteconnect")
    import kite_config as cfg
    tok_path = NSE_DIR / "data" / "kite_token.json"
    if not tok_path.exists():
        sys.exit(f"No token at {tok_path}. Run: cd {NSE_DIR} && python3 kite_login.py")
    tok = json.loads(tok_path.read_text())
    today = dt.date.today().isoformat()
    if tok.get("date") != today:
        sys.exit(f"Token dated {tok.get('date')}, not today ({today}). "
                 f"Run: cd {NSE_DIR} && python3 kite_login.py, then re-run.")
    kite = KiteConnect(api_key=cfg.KITE_API_KEY)
    kite.set_access_token(tok["access_token"])
    return kite


def _fut_contracts(kite):
    """All currently-listed FUT contracts per underlying, sorted by expiry.
    Expired contracts are gone from the dump, so this is ~near+next+far only."""
    nfo = kite.instruments("NFO")
    out = {}
    for key, name in FUT_NAMES.items():
        rows = [r for r in nfo if r.get("name") == name and r.get("instrument_type") == "FUT"]
        def _d(r):
            e = r["expiry"]
            return e if isinstance(e, dt.date) else dt.datetime.strptime(str(e)[:10], "%Y-%m-%d").date()
        out[key] = sorted([(r["instrument_token"], r["tradingsymbol"], _d(r)) for r in rows],
                          key=lambda t: t[2])
    return out


def _stitch_continuous_fut(kite, contracts, start, end, interval):
    """Build a continuous near-month futures series from the listed contracts:
    for each day use the contract with the nearest expiry >= that day (roll on
    expiry). Returns a flat list of candles."""
    # fetch each contract's full window once
    per_contract = []
    for tok, sym, exp in contracts:
        rows = _fetch(kite, tok, start, min(end, exp), oi=True, interval=interval)
        per_contract.append((exp, rows))
        print(f"    fut {sym} (exp {exp}): {len(rows)} candles")
    # assign each day to the nearest-expiry contract that hadn't expired that day
    by_day = {}
    for exp, rows in sorted(per_contract):
        for c in rows:
            d = c["date"]
            day = d.date() if hasattr(d, "date") else dt.datetime.fromisoformat(str(d)[:19]).date()
            # a later day already filled by an earlier (nearer) expiry stays; only
            # fill from this contract if not yet set (nearer expiries processed first)
            key = (day, c["date"])
            if key not in by_day:
                by_day[key] = c
    return [by_day[k] for k in sorted(by_day)]


def _chunks(start: dt.date, end: dt.date, days=55):
    s = start
    while s <= end:
        e = min(end, s + dt.timedelta(days=days))
        yield s, e
        s = e + dt.timedelta(days=1)


def _fetch(kite, token, start, end, oi=False, interval="5minute"):
    out = []
    for cs, ce in _chunks(start, end):
        frm = dt.datetime.combine(cs, dt.time(9, 15))
        to = dt.datetime.combine(ce, dt.time(15, 30))
        try:
            out += kite.historical_data(token, frm, to, interval, oi=oi)
        except Exception as e:
            print(f"    chunk {cs}..{ce} failed: {e}")
        time.sleep(0.4)
    return out


def _write_by_day(candles, key, role, root, with_oi):
    by_day = defaultdict(list)
    for c in candles:
        d = c["date"]
        day = d.date() if hasattr(d, "date") else dt.datetime.fromisoformat(str(d)[:19]).date()
        by_day[day].append(c)
    header = ["datetime", "open", "high", "low", "close"] + (["volume", "oi"] if with_oi else [])
    for day, rows in by_day.items():
        outdir = root / day.isoformat()
        outdir.mkdir(parents=True, exist_ok=True)
        with (outdir / f"{key}_{role}.csv").open("w", newline="") as f:
            w = csv.writer(f)
            w.writerow(header)
            for c in rows:
                d = c["date"]
                ts = d.strftime("%Y-%m-%d %H:%M") if hasattr(d, "strftime") else str(d)[:16]
                line = [ts, c["open"], c["high"], c["low"], c["close"]]
                if with_oi:
                    line += [c.get("volume", 0), c.get("oi", 0)]
                w.writerow(line)
    return len(by_day)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--months", type=float, default=3)
    ap.add_argument("--start", default=None)
    ap.add_argument("--end", default=None)
    args = ap.parse_args()

    end = dt.date.fromisoformat(args.end) if args.end else dt.date.today()
    start = dt.date.fromisoformat(args.start) if args.start else \
        end - dt.timedelta(days=int(args.months * 30.5))

    kite = _load_kite()
    contracts = _fut_contracts(kite)
    root = ROOT / "datasets" / f"{start.isoformat()}_to_{end.isoformat()}"
    print(f"Fetching 5-min {start} .. {end}  ->  {root}")
    print("NOTE: spot goes back years; continuous futures only spans the currently-"
          "listed contracts (~a few months) — expired contracts aren't on the API.")
    for key, spot_tok in SPOT_TOKENS.items():
        print(f"[{key}] spot…")
        spot = _fetch(kite, spot_tok, start, end, oi=False)
        nd = _write_by_day(spot, key, "spot", root, with_oi=False)
        print(f"[{key}] spot: {len(spot)} candles across {nd} days")
        cons = contracts.get(key, [])
        if cons:
            print(f"[{key}] stitching continuous futures from {len(cons)} listed contracts…")
            fut = _stitch_continuous_fut(kite, cons, start, end, "5minute")
            nd = _write_by_day(fut, key, "fut", root, with_oi=True)
            print(f"[{key}] fut (continuous): {len(fut)} candles across {nd} days")

    print(f"\nDone -> {root}")
    print(f"Backtest:\n  python -m src.backtest --datasets datasets/{root.name} --label real-2yr")
    print(f"  python -m src.compare_vehicles --label real-2yr")
    print(f"  python -m src.period_report --label real-2yr")


if __name__ == "__main__":
    main()
