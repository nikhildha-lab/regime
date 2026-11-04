"""Prior-period reference levels: previous day / week / month high & low.

These are STATIC for the whole session (unlike IB/VWAP/VA/POC which develop
intraday), which is exactly why they make reliable reversal anchors — a footprint
absorption at PDL is unambiguous because PDL doesn't move.

Derived for free from the daily candles already fetched for the 1D/1W chart
(src/seed.fetch_daily), so no extra API call. Live-only (daily is fetched at
live start); empty in sim/replay.
"""
from __future__ import annotations

import datetime as dt


def _parse(iso):
    try:
        return dt.date.fromisoformat(str(iso))
    except Exception:
        return None


def compute_prior_levels(daily, today=None, want=("pdh", "pdl", "pwh", "pwl")):
    """From a list of daily dicts ({iso,h,l,...}) return prior-period H/L.

    Returns a dict with keys pdh,pdl,pwh,pwl,pmh,pml (None where unavailable or
    not requested). 'today' is excluded so we only use *completed* periods.
    """
    out = {k: None for k in ("pdh", "pdl", "pwh", "pwl", "pmh", "pml")}
    if not daily:
        return out
    today = today or dt.date.today()

    rows = []
    for c in daily:
        if not isinstance(c, dict):
            continue
        d = _parse(c.get("iso"))
        if d is None or "h" not in c or "l" not in c:
            continue
        rows.append((d, c["h"], c["l"]))
    rows.sort(key=lambda r: r[0])
    past = [r for r in rows if r[0] < today]   # completed days only
    if not past:
        return out

    # previous day
    out["pdh"], out["pdl"] = past[-1][1], past[-1][2]

    # previous week (ISO year+week)
    cur_wk = tuple(today.isocalendar()[:2])
    weeks: dict = {}
    for d, h, l in past:
        wk = tuple(d.isocalendar()[:2])
        weeks.setdefault(wk, []).append((h, l))
    pw = sorted(w for w in weeks if w < cur_wk)
    if pw:
        hl = weeks[pw[-1]]
        out["pwh"] = max(x[0] for x in hl)
        out["pwl"] = min(x[1] for x in hl)

    # previous calendar month
    cur_mo = (today.year, today.month)
    months: dict = {}
    for d, h, l in past:
        mk = (d.year, d.month)
        months.setdefault(mk, []).append((h, l))
    pm = sorted(m for m in months if m < cur_mo)
    if pm:
        hl = months[pm[-1]]
        out["pmh"] = max(x[0] for x in hl)
        out["pml"] = min(x[1] for x in hl)

    return {k: (round(v, 1) if (k in want and v is not None) else None)
            for k, v in out.items()}
