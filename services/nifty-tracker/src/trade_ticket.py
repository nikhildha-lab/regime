"""Option trade ticket for a graded setup.

Given a setup (direction) and the live option chain, pick the strike whose delta
is closest to a target (~0.55 = roughly 1-strike ITM), read its LIVE premium from
the streamed quote, back out IV and greeks, and produce a go/no-go on whether the
greeks allow an option buy — or whether theta/expiry argue for the future instead.

Everything in premium POINTS (delta-scaled underlying move), so it's vehicle-
comparable without asserting a rupee figure. Approximate by design: greeks come
from ~1/sec snapshot LTPs and a floored time-to-expiry.
"""
from __future__ import annotations

import datetime as dt

from .analytics import greeks as gk


def _hours_left(cfg, now=None):
    now = now or dt.datetime.now()
    close = dt.datetime.combine(now.date(), cfg.market_close)
    return max(0.0, (close - now).total_seconds() / 3600.0)


def build(setup, price, st, cfg, plan, dte, now=None):
    """Return a trade-ticket dict, or None if not actionable / no chain."""
    if not setup or not setup.get("actionable"):
        return None
    side = setup["side"]                       # long / short
    leg = "CE" if side == "long" else "PE"
    key = st.ri.key
    step = st.ri.cfg.strike_step
    r = float(cfg.a("vehicles", "risk_free", default=0.065))
    tgt_delta = float(cfg.a("setup", "target_delta", default=0.55))
    hold_h = float(cfg.a("setup", "expected_hold_hours", default=1.0))
    lot = int(cfg.a("vehicles", "per_instrument", key, "lot", default=0) or 0)

    hours_left = _hours_left(cfg, now)
    T = gk.time_to_expiry(dte, hours_left)
    expiry = st.ri.expiry.isoformat() if st.ri.expiry else None

    atm = round(price / step) * step
    # candidate strikes near ATM for this leg that have a live premium
    cands = []
    for off in range(-4, 5):
        strike = atm + off * step
        q = st.options.get((float(strike), leg))
        if q is None or not q.ltp or q.ltp <= 0:
            continue
        iv = gk.implied_vol(q.ltp, price, strike, r, T, leg)
        if iv is None:
            continue
        d = gk.greeks(price, strike, r, T, iv, leg)["delta"]
        cands.append((strike, q.ltp, iv, d))
    if not cands:
        return {"actionable": True, "side": side, "leg": leg, "expiry": expiry,
                "verdict": "NO-GO", "reason": "no live option quotes for this leg",
                "grade": setup["grade"]}

    # pick the strike whose |delta| is closest to the target
    strike, prem, iv, delta = min(cands, key=lambda c: abs(abs(c[3]) - tgt_delta))
    g = gk.greeks(price, strike, r, T, iv, leg)
    theta = g["theta"]                          # per day (negative)
    adelta = abs(delta)

    # plan-derived risk / reward in underlying points
    sl_pts = abs(price - plan["stop_loss"]) if plan and plan.get("stop_loss") else None
    tgt_pts = abs(plan["target1"] - price) if plan and plan.get("target1") else None

    # premium math (points of premium)
    sl_prem = min(prem, adelta * sl_pts) if sl_pts else None
    tgt_prem = adelta * tgt_pts if tgt_pts else None
    theta_cost = abs(theta) * (hold_h / 24.0)   # decay over the expected hold
    cost_pts = max(2.0, prem * 0.005)           # rough round-trip friction
    net_edge = (tgt_prem - theta_cost - cost_pts) if tgt_prem is not None else None
    rr = (net_edge / sl_prem) if (net_edge is not None and sl_prem) else None

    # breakeven distance (underlying pts from spot)
    if leg == "CE":
        be_pts = (strike + prem) - price
    else:
        be_pts = price - (strike - prem)

    # ---- conviction-based position sizing --------------------------------
    profile = str(cfg.a("risk", "profile", default="balanced"))
    capital = float(cfg.a("risk", "capital", default=500000))
    base_pct = float(cfg.a("risk", "base_risk_pct", default=1.0))
    grade_frac = float(cfg.a("risk", "size_by_grade", setup["grade"], default=0.5))
    risk_mult = float(cfg.a("risk", "profiles", profile, "risk_mult", default=1.0))
    risk_amount = capital * (base_pct / 100.0) * grade_frac * risk_mult
    lots = 0
    prem_outlay = None
    risk_per_lot = (sl_prem * lot) if (sl_prem and lot) else None
    if risk_per_lot and risk_per_lot > 0:
        lots = int(risk_amount // risk_per_lot)
        prem_outlay = round(prem * lot * lots)
    size = {
        "profile": profile, "grade_frac": grade_frac, "lots": lots,
        "risk_amount": round(risk_amount),
        "risk_pct": round(risk_amount / capital * 100, 2) if capital else None,
        "premium_outlay": prem_outlay,
        "note": None if lots > 0 else "risk/lot exceeds budget — reduce risk or skip",
    }

    # go / no-go
    expiry_day = (dte is not None and dte <= 0)
    verdict, reason = "GO", "greeks favor the option"
    if net_edge is None or rr is None:
        verdict, reason = "CAUTION", "no target/SL to size against"
    elif net_edge <= 0:
        verdict, reason = "NO-GO", "theta+cost exceeds expected premium gain — use future/ITM"
    elif rr < 1.5:
        verdict, reason = "CAUTION", f"thin reward:risk ({rr:.1f}) — smaller size or wait"
    if expiry_day and tgt_prem and theta_cost > 0.5 * tgt_prem:
        verdict, reason = "NO-GO", "expiry-day theta too heavy — go ITM or trade the future"

    return {
        "actionable": True, "grade": setup["grade"], "side": side, "leg": leg,
        "strike": int(strike), "expiry": expiry, "dte": dte,
        "delta": round(delta, 2), "theta_day": round(theta, 1), "iv": round(iv * 100, 1),
        "entry_premium": round(prem, 1),
        "sl_premium": None if sl_prem is None else round(sl_prem, 1),
        "target_premium": None if tgt_prem is None else round(tgt_prem, 1),
        "theta_cost": round(theta_cost, 1),
        "breakeven_pts": round(be_pts, 1),
        "rr": None if rr is None else round(rr, 1),
        "verdict": verdict, "reason": reason,
        "sl_points": None if sl_pts is None else round(sl_pts, 1),
        "target_points": None if tgt_pts is None else round(tgt_pts, 1),
        "lot": lot, "size": size,
    }
