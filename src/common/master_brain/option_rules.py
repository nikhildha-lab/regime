"""
Option-selling strategy rules adopted by the MASTER_BACKTESTER session (Oct 2026), encoded so the
master brain can apply them live. Source: MASTER_BACKTESTER/experiments/options/PAPER_TRADE_SHORTLIST.md,
OPTIONS_REGIME_STUDY.md and regime_gates.py. Basis: REAL NSE option chains Oct 2023 - Oct 2026.

These are BACKTESTER's validated rules (both-halves test, economic rationale). The master brain adds:
  - the live India VIX reading and the event calendar to evaluate them,
  - an ADVISORY note from our own regime engine: weekly short-vol did better in sideways regimes
    (calm/choppy range) in both halves, but not significantly (permutation p 0.18-0.48) - paper-test only.
Stage for every strategy below: PAPER TRADING. Nothing here is cleared for live money.
"""
from __future__ import annotations

OPTION_STRATEGIES = {
    "fly_rescue NIFTY": dict(tier="A", status="Paper-trade shortlist", entry="monthly, 40-50 DTE, 3:15 PM",
                             vix_min=None, vix_max=None, lots_rule=[(14.0, 2), (12.0, 1), (0, 1)],
                             core_event_skip=False, skip_strong_uptrend=True,
                             note="Trades through events; optional skip in strong up-trend (ADX>25 and 20d return>0)"),
    "fly_rescue BANKNIFTY": dict(tier="A", status="Paper-trade shortlist", entry="monthly, 40-50 DTE, 3:15 PM",
                                 vix_min=None, vix_max=None, lots_rule=[(14.0, 2), (12.0, 1), (0, 1)],
                                 core_event_skip=False, skip_strong_uptrend=False, note=""),
    "condor_clock NIFTY": dict(tier="B", status="Paper-trade shortlist (thin sample)", entry="monthly, 40-50 DTE, 3:15 PM",
                               vix_min=14.5, vix_max=None, lots_rule=[(0, 1)], core_event_skip=False,
                               skip_strong_uptrend=False, note="Below VIX 14.5 the premium is too thin (9 stop-outs ungated)"),
    "condor_clock BANKNIFTY": dict(tier="Conditional", status="Conditional - needs Rs 1.9L per lot", entry="monthly, 40-50 DTE, 3:15 PM",
                                   vix_min=None, vix_max=18.0, lots_rule=[(0, 1)], core_event_skip=False,
                                   skip_strong_uptrend=False, note="DD -29% of margin"),
    "straddle_recenter NIFTY": dict(tier="B", status="Paper-trade shortlist (event skip is load-bearing)", entry="monthly, ~40 DTE, 3:15 PM",
                                    vix_min=13.0, vix_max=None, lots_rule=[(14.0, 2), (13.0, 1)], core_event_skip=True,
                                    skip_strong_uptrend=False, note="Skip if Budget / election result / tariff decision within 36 days"),
    "volcano_calendar NIFTY": dict(tier="Watch", status="Watch-list, 1 lot diversifier", entry="last Friday of month, 3:30 PM",
                                   vix_min=None, vix_max=12.5, lots_rule=[(-11.0, 2), (0, 1)], core_event_skip=False,
                                   skip_strong_uptrend=False, note="Long-vega: works in LOW VIX; 6%/yr - capital-inefficient"),
    "delta_hedged NIFTY": dict(tier="Not yet", status="NOT YET - DD -54% of margin; needs roll-level event skip", entry="weekly",
                               vix_min=None, vix_max=None, lots_rule=[(0, 1)], core_event_skip=True,
                               skip_strong_uptrend=False, note=""),
}
REJECTED = ["volcano_calendar BANKNIFTY", "ratio_flip NIFTY", "ratio_flip BANKNIFTY", "yearly_covered",
            "regime_spreads NIFTY", "regime_spreads BANKNIFTY", "strangle_shield NIFTY", "split_condor NIFTY",
            "reverse_calendar_m1 NIFTY"]
WEEKLY_SHORT_VOL = {"strangle_shield NIFTY", "split_condor NIFTY", "delta_hedged NIFTY"}


import pandas as pd

EXPIRY_SWITCH = pd.Timestamp("2025-09-01")      # NIFTY monthly expiry: last Thursday before, last Tuesday after


def _exchange_expiries():
    """Actual NIFTY monthly futures expiries seen in NSE / Upstox data (includes holiday shifts)."""
    try:
        from src.common.regime_engine import data as D
        f = D.load_index_futures()
        ex = set(pd.to_datetime(f.loc[f["symbol"] == "NIFTY", "expiry"]).dt.normalize())
        ex |= {pd.Timestamp(v) for v in D.fut_expiry_map().values()}
        return ex
    except Exception:
        return set()


_EXP = None


def monthly_expiry(year: int, month: int) -> pd.Timestamp:
    """Exchange date when known; otherwise the calendar rule (last Thu before Sep-2025, last Tue after)."""
    global _EXP
    if _EXP is None:
        _EXP = _exchange_expiries()
    known = [e for e in _EXP if e.year == year and e.month == month]
    if known:
        return max(known)
    last = pd.Timestamp(year=year, month=month, day=1) + pd.offsets.MonthEnd(0)
    wd = 3 if last < EXPIRY_SWITCH else 1
    return last - pd.Timedelta(days=(last.weekday() - wd) % 7)     # rule-based fallback: holiday shifts not modelled


def entry_window(name: str, day: pd.Timestamp) -> dict:
    """Is `day` inside the strategy's entry window? If not, when is the next one?"""
    day = pd.Timestamp(day).normalize()
    strat = name.split()[0]
    if strat == "volcano_calendar":                                   # last Friday of the month
        def last_fri(y, m):
            last = pd.Timestamp(year=y, month=m, day=1) + pd.offsets.MonthEnd(0)
            return last - pd.Timedelta(days=(last.weekday() - 4) % 7)
        lf = last_fri(day.year, day.month)
        nxt = lf if lf >= day else last_fri((day + pd.offsets.MonthBegin(1)).year, (day + pd.offsets.MonthBegin(1)).month)
        return {"in_window": day == lf, "next_window": str(nxt.date()), "rule": "last Friday of the month, 3:30 PM"}
    if strat == "delta_hedged":
        return {"in_window": True, "next_window": str(day.date()), "rule": "weekly"}
    lo, hi = (34, 46) if strat == "straddle_recenter" else (40, 50)
    exps = [monthly_expiry((day + pd.DateOffset(months=k)).year, (day + pd.DateOffset(months=k)).month) for k in range(0, 4)]
    for e in exps:
        if lo <= (e - day).days <= hi:
            return {"in_window": True, "expiry": str(e.date()), "dte": (e - day).days, "rule": f"{lo}-{hi} DTE monthly"}
    starts = [e - pd.Timedelta(days=hi) for e in exps if e - pd.Timedelta(days=hi) > day]
    starts = [d + pd.offsets.BDay(0) for d in starts]                  # roll a weekend start to Monday
    return {"in_window": False, "next_window": str(min(starts).date()) if starts else None, "rule": f"{lo}-{hi} DTE monthly"}


def lots_for(rule, vix):
    if vix is None:
        return 1
    for thr, lots in rule:
        if thr < 0:                       # negative threshold = "VIX <= |thr|" (long-vega sizing)
            if vix <= -thr:
                return lots
        elif vix >= thr:
            return lots
    return 1


def evaluate(name: str, vix: float, events: dict, regime: dict, day=None) -> dict:
    if name in REJECTED:
        return {"strategy": name, "action": "Do not run", "lots": 0, "reason": "rejected by BACKTESTER on real chains (PF < 1.1 or negative)"}
    r = OPTION_STRATEGIES[name]
    why = []
    action = "Paper: enter"
    if vix is None:
        action, why = "Paper: wait", ["India VIX unavailable"]
    else:
        if r["vix_min"] is not None and vix < r["vix_min"]:
            action = "Paper: skip"; why.append(f"VIX {vix:.2f} < {r['vix_min']}")
        if r["vix_max"] is not None and vix > r["vix_max"]:
            action = "Paper: skip"; why.append(f"VIX {vix:.2f} > {r['vix_max']}")
    if r["core_event_skip"] and events.get("core_event_within_horizon"):
        action = "Paper: skip"; why.append(f"core event ahead: {events['next_core_event']}")
    if r["skip_strong_uptrend"] and regime.get("regime_name") == "Strong uptrend":
        action = "Paper: skip"; why.append("strong up-trend (optional rule)")
    win = entry_window(name, day) if day is not None else {"in_window": True}
    lots = lots_for(r["lots_rule"], vix) if action == "Paper: enter" else 0
    if action == "Paper: enter":
        why.append(f"VIX {vix:.2f} -> {lots} lot(s)")
        if not win["in_window"]:
            action = "Paper: conditions OK"
            why.append(f"not in entry window ({win['rule']}); next window opens {win.get('next_window')}")
        elif win.get("expiry"):
            why.append(f"entry window open: expiry {win['expiry']} at {win['dte']} DTE")
            if day is not None and pd.Timestamp(day).weekday() >= 5:
                nxt = (pd.Timestamp(day) + pd.offsets.BDay(1)).date()
                why.append(f"market closed today; enter at the next session ({nxt}, 3:15 PM)")
    if "NOT YET" in r["status"]:
        action, lots, why = "Not ready", 0, [r["status"]]
    adv = None
    if name in WEEKLY_SHORT_VOL:
        side = regime.get("regime_name") in ("Calm range", "Choppy range")
        adv = ("sideways regime now - historically better for weekly short-vol (not significant)" if side
               else "trending regime now - weekly short-vol historically weaker here (not significant)")
    return {"strategy": name, "tier": r["tier"], "status": r["status"], "entry_rule": r["entry"],
            "action": action, "lots": lots, "reason": "; ".join(why), "note": r["note"], "regime_advisory": adv,
            "entry_window": win}
