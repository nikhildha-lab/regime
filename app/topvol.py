"""TOP-VOLUME UNIVERSE for the intraday stock rules (2026-11-09, Nikhil: "intraday strategies will target only top F&O stocks by volume for
the day along with their own entry conditions, max 10 stocks a day").

Each morning the tracked F&O stocks (app/stockhist.STOCKS, the names the rules can trade) are ranked by traded VALUE in the first
15 minutes (09:15-09:30, cash 1-min bars). The top N (N = 10) is the day's universe for Stocks in Play ORB, Stock Volume Break and
DOM Stocks; every rule keeps its own entry conditions. Fixed for the day once written (no re-ranking later, so a 10:30 trade and a
backtest see the same list). Saved in data_store/livedesk/topvol/<day>.json; also what the dashboard shows.
History check (scripts/research/top_volume_universe_test.py): ORB trades inside this top 10 made +0.19 % a trade vs +0.10 % for all and
−0.08 % for the rest (42 of 64 trades kept); DOM Stocks: no help either way. Vol Break: untested (no saved trade file).
If the list is missing (stockorb has not scanned yet, data error) the rules ALLOW the trade and record "universe: not ranked", so a data
gap never blocks trades silently.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
OUT = os.path.join(ROOT, "data_store", "livedesk", "topvol")
N = 10
_C: dict = {}


def _p(day):
    return os.path.join(OUT, f"{day}.json")


def write(day: str, values: dict, at: str | None = None) -> dict:
    """values = {sym: traded value 09:15-09:30}. Writes the ranked list once per day (first writer wins)."""
    if os.path.exists(_p(day)):
        return load(day)
    ranked = sorted(((v, s) for s, v in values.items() if v and v == v), reverse=True)
    doc = {"day": day, "at": at or datetime.now().strftime("%H:%M"), "n": N, "window": "09:15-09:30 traded value (cash)",
           "ranked": [{"sym": s, "value": round(v), "rank": i + 1} for i, (v, s) in enumerate(ranked)],
           "top": [s for _, s in ranked[:N]]}
    os.makedirs(OUT, exist_ok=True)
    tmp = _p(day) + ".tmp"; json.dump(doc, open(tmp, "w"), indent=1); os.replace(tmp, _p(day))
    _C.pop(day, None)
    return doc


def load(day: str | None = None) -> dict | None:
    day = day or datetime.now().strftime("%Y-%m-%d")
    c = _C.get(day)
    if c and time.time() - c[0] < 30:
        return c[1]
    try:
        d = json.load(open(_p(day)))
    except Exception:
        d = None
    _C[day] = (time.time(), d)
    return d


def allowed(sym: str, day: str | None = None) -> tuple[bool, str]:
    """-> (ok, note). ok when the list is missing (never block on a data gap)."""
    d = load(day)
    if not d or not d.get("top"):
        return True, "universe: not ranked yet - allowed"
    if sym in d["top"]:
        return True, f"universe: top {N} by opening value (rank {next((r['rank'] for r in d['ranked'] if r['sym'] == sym), '?')})"
    rk = next((r["rank"] for r in d["ranked"] if r["sym"] == sym), None)
    return False, f"universe: outside the top {N} by opening value (rank {rk if rk else 'n/a'})"
