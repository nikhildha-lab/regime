"""
Group E seasonality, v1 = context TAGS only (not scored yet).

BRD Group E scores need their own fit; until then these are carried as tags so the
recommender can apply event rules (e.g. pause short-vol on expiry days).

NIFTY weekly expiry weekday: Thursday until 2025-08-31, Tuesday from 2025-09-01
(NSE change). Exchange holidays move expiry to the previous trading day; this file
has no holiday calendar yet, so `is_weekly_expiry` is APPROXIMATE on holiday weeks.
TODO: load src/common/regime_engine/calendars/events_<year>.json (BRD §7) once created.
"""
from __future__ import annotations

import pandas as pd

EXPIRY_SWITCH = pd.Timestamp("2025-09-01")


def calendar_tags(ts: pd.Timestamp) -> dict:
    d = pd.Timestamp(ts).tz_localize(None) if pd.Timestamp(ts).tzinfo else pd.Timestamp(ts)
    expiry_wd = 3 if d.normalize() < EXPIRY_SWITCH else 1          # Thu=3, Tue=1
    last_day = d + pd.offsets.MonthEnd(0)
    # last expiry weekday of the month = monthly expiry
    last_exp = last_day - pd.Timedelta(days=(last_day.weekday() - expiry_wd) % 7)
    return {
        "day_of_week": d.day_name(),
        "month": d.month_name(),
        "is_weekly_expiry": bool(d.weekday() == expiry_wd),
        "is_monthly_expiry": bool(d.normalize() == last_exp.normalize()),
        "expiry_calendar_note": "approximate: no holiday calendar loaded",
    }


# ---- Scheduled binary events (Budget, election results, RBI, FOMC, tariff day) ----
import json as _json
import os as _os

_EVENTS_PATH = _os.path.join(_os.path.dirname(__file__), "..", "calendars", "events_2023_2027.json")


def _events():
    try:
        with open(_EVENTS_PATH) as f:
            return [(pd.Timestamp(e["date"]), e["name"], e["core"]) for e in _json.load(f)["events"]]
    except FileNotFoundError:
        return []


def event_tags(ts, horizon_days: int = 36) -> dict:
    """Events in the next `horizon_days` (36 = Straddle Recenter's tested horizon)."""
    d = pd.Timestamp(ts)
    d = (d.tz_localize(None) if d.tzinfo else d).normalize()
    ahead = [(e, n, c) for e, n, c in _events() if d <= e <= d + pd.Timedelta(days=horizon_days)]
    nxt_core = next(((e, n) for e, n, c in sorted(ahead) if c), None)
    return {
        "event_today": next((n for e, n, c in _events() if e == d), None),
        "events_next_days": [f"{e.date()} {n}" for e, n, c in sorted(ahead)],
        "core_event_within_horizon": nxt_core is not None,
        "next_core_event": None if nxt_core is None else f"{nxt_core[0].date()} {nxt_core[1]}",
        "horizon_days": horizon_days,
        "event_calendar_source": "MASTER_BACKTESTER regime_gates.py; (approx) dates need verifying",
    }
