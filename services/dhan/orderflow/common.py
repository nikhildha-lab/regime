"""Shared helpers: Dhan login and instrument lookup."""

import os
import sys
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv

HERE = Path(__file__).resolve().parent
SCRIP_FILE = HERE / "data" / "scrip_master.csv"
SCRIP_URL = "https://images.dhan.co/api-data/api-scrip-master.csv"


def connect():
    """Return (DhanContext, dhanhq client) using .env from this folder or its parent."""
    from dhanhq import DhanContext, dhanhq

    for env in (HERE / ".env", HERE.parent / ".env"):
        if env.exists():
            load_dotenv(env)
            break
    cid, tok = os.getenv("DHAN_CLIENT_ID"), os.getenv("DHAN_ACCESS_TOKEN")
    if not cid or not tok:
        sys.exit("Missing DHAN_CLIENT_ID / DHAN_ACCESS_TOKEN in .env")
    ctx = DhanContext(cid, tok)
    return ctx, dhanhq(ctx)


def scrip_master(refresh=False):
    """Dhan instrument list, cached for the day."""
    SCRIP_FILE.parent.mkdir(parents=True, exist_ok=True)
    fresh = SCRIP_FILE.exists() and (
        pd.Timestamp.now() - pd.Timestamp(SCRIP_FILE.stat().st_mtime, unit="s")
    ) < pd.Timedelta(hours=12)
    if refresh or not fresh:
        print("Downloading Dhan instrument list...")
        df = pd.read_csv(SCRIP_URL, low_memory=False)
        df.to_csv(SCRIP_FILE, index=False)
    return pd.read_csv(SCRIP_FILE, low_memory=False)


def equity_id(symbol):
    """Security id of an NSE equity/ETF, e.g. NIFTYBEES."""
    s = scrip_master()
    row = s[(s["SEM_EXM_EXCH_ID"] == "NSE") & (s["SEM_SEGMENT"] == "E")
            & (s["SEM_TRADING_SYMBOL"] == symbol)]
    if row.empty:
        sys.exit(f"Could not find {symbol} in instrument list")
    return str(int(row.iloc[0]["SEM_SMST_SECURITY_ID"]))


def current_future(index_name):
    """(security_id, trading_symbol) of the nearest index future, e.g. NIFTY / BANKNIFTY."""
    s = scrip_master()
    f = s[(s["SEM_EXM_EXCH_ID"] == "NSE") & (s["SEM_INSTRUMENT_NAME"] == "FUTIDX")
          & (s["SEM_TRADING_SYMBOL"].str.startswith(index_name + "-"))].copy()
    f["expiry"] = pd.to_datetime(f["SEM_EXPIRY_DATE"])
    f = f[f["expiry"] >= pd.Timestamp.now().normalize()].sort_values("expiry")
    r = f.iloc[0]
    return str(int(r["SEM_SMST_SECURITY_ID"])), r["SEM_TRADING_SYMBOL"]
