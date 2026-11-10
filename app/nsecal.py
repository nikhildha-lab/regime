"""NSE SESSION GUARD (2026-11-10, Nikhil: "market is off today, not sure what trades you are taking for the Indian market").

On 10 Nov 2026 (an NSE holiday) the Indian paper rules kept working off a frozen option chain - NSE's site served the previous session's
chain all morning, identical prices in 31 snapshots - so overnight trades were "closed", a vol-cycle night straddle "exited" and four day
straddles "opened" on prices that never traded. Two checks now gate every Indian paper rule:
  1. is_trading_day(d) - Monday-Friday and not in NSE's trading-holiday list (nseindia.com/api/holiday-master?type=trading, cached per year
     in data_store/flow/nse_holidays.json; when the list cannot be fetched and nothing is cached the day is assumed open).
  2. chain_live() - the recorded option-chain snapshots of the day: if the last two snapshots (>= 3 min apart) show IDENTICAL prices on
     every strike, nothing is trading -> not live. None when there are fewer than two snapshots (unknown, treated as live).
session_ok() = both. Used by volcycle, overnight, idxstrangle, touchtrades, domcards, stockorb, stockvb and the option-chain recorder.
"""
from __future__ import annotations

import json
import os
import time
from datetime import date, datetime

import pandas as pd

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
HOL = os.path.join(ROOT, "data_store", "flow", "nse_holidays.json")
OCD = os.path.join(ROOT, "data_store", "flow", "option_chain")
_C: dict = {"hol": None, "hol_t": 0, "live": None, "live_t": 0, "live_mt": None}


def _fetch_holidays() -> dict:
    """{year: [YYYY-MM-DD]} from NSE (CM segment); raises on failure."""
    import updater as U
    s = U._CSESSION
    if s is None:
        raise RuntimeError("no browser session")
    s.get("https://www.nseindia.com/", timeout=20)
    r = s.get("https://www.nseindia.com/api/holiday-master?type=trading", headers={"Referer": "https://www.nseindia.com/", "Accept": "application/json"}, timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"NSE HTTP {r.status_code}")
    j = r.json(); out = {}
    for seg in ("CM", "FO"):
        for row in j.get(seg) or []:
            d = pd.to_datetime(row.get("tradingDate"), dayfirst=True, errors="coerce")
            if pd.notna(d):
                out.setdefault(str(d.year), set()).add(d.strftime("%Y-%m-%d"))
    return {y: sorted(v) for y, v in out.items()}


def holidays() -> dict:
    if _C["hol"] is not None and time.time() - _C["hol_t"] < 6 * 3600:
        return _C["hol"]
    cache = {}
    try:
        cache = json.load(open(HOL))
    except Exception:
        pass
    yr = str(date.today().year)
    if yr not in cache or time.time() - cache.get("_fetched", 0) > 7 * 86400:
        try:
            got = _fetch_holidays()
            if got:
                cache.update(got); cache["_fetched"] = time.time()
                os.makedirs(os.path.dirname(HOL), exist_ok=True); json.dump(cache, open(HOL, "w"), indent=1)
        except Exception as e:
            cache["_error"] = f"{type(e).__name__}: {str(e)[:80]}"
    _C["hol"] = cache; _C["hol_t"] = time.time()
    return cache


def is_trading_day(d=None) -> bool:
    d = pd.Timestamp(d or date.today())
    if d.weekday() >= 5:
        return False
    return d.strftime("%Y-%m-%d") not in set(holidays().get(str(d.year)) or [])


def chain_live(day=None):
    """True / False / None(unknown) from the day's option-chain snapshots: identical prices across the last two snapshots = not live."""
    day = day or date.today().strftime("%Y-%m-%d"); p = os.path.join(OCD, f"{day}.csv.gz")
    try:
        mt = os.path.getmtime(p)
    except OSError:
        return None
    if _C["live_mt"] == mt and time.time() - _C["live_t"] < 120:
        return _C["live"]
    try:
        o = pd.read_csv(p, usecols=["ts", "symbol", "expiry", "strike", "side", "ltp"])
        o = o[o.ts != "ts"]
        ts = sorted(o.ts.unique())
        if len(ts) < 2:
            res = None
        else:
            a = o[o.ts == ts[-1]].set_index(["symbol", "expiry", "strike", "side"]).ltp.astype(float)
            b = o[o.ts == ts[-2]].set_index(["symbol", "expiry", "strike", "side"]).ltp.astype(float)
            j = pd.concat([a, b], axis=1, join="inner").dropna()
            res = None if len(j) < 20 else bool((j.iloc[:, 0] != j.iloc[:, 1]).mean() > 0.02)      # > 2 % of strikes moved -> live
    except Exception:
        res = None
    _C["live"], _C["live_t"], _C["live_mt"] = res, time.time(), mt
    return res


def session_ok(d=None) -> bool:
    if not is_trading_day(d):
        return False
    return chain_live() is not False


def status() -> dict:
    h = holidays(); y = str(date.today().year)
    return {"today": date.today().strftime("%Y-%m-%d"), "trading_day": is_trading_day(), "chain_live": chain_live(), "session_ok": session_ok(),
            "holidays_this_year": h.get(y), "holiday_error": h.get("_error")}
