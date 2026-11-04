"""
Download history for backtesting.

  1. NIFTYBEES / BANKBEES 1-minute candles  -> volume profile + order flow signals
  2. NIFTY / BANKNIFTY expired options (rolling ATM-6 ... ATM+6, calls & puts)
     -> real option prices for spreads and option selling

Run:   python download_history.py            (both indices)
       python download_history.py NIFTY      (one index)
Safe to re-run: it skips months already downloaded.
"""

import sys
import time
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

import config as C
from common import HERE, connect, equity_id

DATA = HERE / C.DATA_DIR
PAUSE = 0.35  # seconds between API calls (stay under rate limits)


def month_chunks(start, end, days):
    d = start
    while d < end:
        e = min(d + timedelta(days=days), end)
        yield d, e
        d = e


def to_df(block, extra=None):
    if not block or not block.get("timestamp"):
        return pd.DataFrame()
    df = pd.DataFrame({k: v for k, v in block.items() if isinstance(v, list) and len(v) == len(block["timestamp"])})
    df["ts"] = pd.to_datetime(df.pop("timestamp"), unit="s", utc=True).dt.tz_convert("Asia/Kolkata").dt.tz_localize(None)
    for k, v in (extra or {}).items():
        df[k] = v
    return df


def download_signal_bars(dhan, symbol, start, end):
    out = DATA / f"{symbol}_1m.csv"
    old = pd.read_csv(out, parse_dates=["ts"]) if out.exists() else pd.DataFrame()
    if not old.empty:
        start = max(start, old["ts"].max().date())
    sid = equity_id(symbol)
    frames = [old]
    print(f"\n{symbol} (id {sid}) 1-min candles {start} -> {end}")
    for a, b in month_chunks(start, end, 85):
        r = dhan.intraday_minute_data(sid, "NSE_EQ", "EQUITY", f"{a} 09:15:00", f"{b} 15:30:00", interval=1)
        if r.get("status") != "success":
            print(f"  {a}..{b}: FAILED {r.get('remarks')}")
            continue
        df = to_df(r["data"])
        print(f"  {a}..{b}: {len(df)} bars")
        frames.append(df)
        time.sleep(PAUSE)
    df = pd.concat(frames).drop_duplicates("ts").sort_values("ts")
    df = df[["ts", "open", "high", "low", "close", "volume"]]
    df.to_csv(out, index=False)
    print(f"  saved {len(df)} bars -> {out.name}")


def download_options(dhan, name, m, start, end):
    folder = DATA / f"{name}_options"
    folder.mkdir(parents=True, exist_ok=True)
    fields = ["open", "high", "low", "close", "volume", "oi", "iv", "strike", "spot"]
    print(f"\n{name} expired options ({m['expiry_flag']}, ATM{min(C.OPTION_OFFSETS)}..ATM+{max(C.OPTION_OFFSETS)})")
    for a, b in month_chunks(start, end, 30):
        out = folder / f"{a}.csv"
        if out.exists() and b < date.today() - timedelta(days=1):
            continue  # already have this month
        frames = []
        for opt in ("CALL", "PUT"):
            for k in C.OPTION_OFFSETS:
                strike = "ATM" if k == 0 else f"ATM{k:+d}"
                r = dhan.expired_options_data(
                    m["index_id"], "NSE_FNO", "OPTIDX", m["expiry_flag"], C.EXPIRY_CODE,
                    strike, opt, fields, str(a), str(b), interval=1)
                time.sleep(PAUSE)
                if r.get("status") != "success":
                    print(f"  {a} {opt} {strike}: FAILED {r.get('remarks')}")
                    continue
                d = r["data"].get("data", r["data"])
                block = d.get("ce" if opt == "CALL" else "pe")
                df = to_df(block, {"type": "CE" if opt == "CALL" else "PE", "offset": k})
                if not df.empty:
                    frames.append(df)
        if frames:
            df = pd.concat(frames)
            df.to_csv(out, index=False)
            print(f"  {a}: {len(df)} rows")
        else:
            print(f"  {a}: no data (if every month is empty, set EXPIRY_CODE = 0 in config.py)")


def main():
    names = [a.upper() for a in sys.argv[1:]] or list(C.MARKETS)
    _, dhan = connect()
    end = date.today()
    start = end - timedelta(days=365 * C.HISTORY_YEARS)
    DATA.mkdir(parents=True, exist_ok=True)
    for name in names:
        m = C.MARKETS[name]
        download_signal_bars(dhan, m["signal_symbol"], start, end)
        download_options(dhan, name, m, start, end)
    print("\nDone. Next: python backtest.py")


if __name__ == "__main__":
    main()
