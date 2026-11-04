"""Perp trade ticket for a graded setup.

The NSE tracker turns a setup into an OPTION ticket (strike/greeks/theta). Crypto
has no options here, so a setup becomes a PERP position ticket: risk-based size in
coin units, a suggested leverage + implied margin, an estimated isolated-margin
liquidation price (with a guard that the structural stop sits well inside it), and
the funding drag over the expected hold. All decision-aid, never an order.
"""
from __future__ import annotations


def _r(x, n=2):
    return None if x is None else round(x, n)


def build(setup, price, st, cfg, plan, now=None):
    if not setup or not setup.get("actionable") or price is None:
        return None
    side = setup["side"]                       # long / short
    key = st.ri.key
    sz_dec = st.ri.sz_decimals

    profile = str(cfg.a("risk", "profile", default="balanced"))
    capital = float(cfg.a("risk", "capital", default=10000))
    base_pct = float(cfg.a("risk", "base_risk_pct", default=1.0))
    grade_frac = float(cfg.a("risk", "size_by_grade", setup["grade"], default=0.5))
    risk_mult = float(cfg.a("risk", "profiles", profile, "risk_mult", default=1.0))
    max_lev = float(cfg.a("risk", "max_leverage", default=20))
    tgt_lev = float(cfg.a("setup", "target_leverage", default=5))
    hold_h = float(cfg.a("setup", "expected_hold_hours", default=2.0))

    sl = plan.get("stop_loss") if plan else None
    tgt = plan.get("target1") if plan else None
    sl_pts = abs(price - sl) if sl is not None else None
    tgt_pts = abs(tgt - price) if tgt is not None else None

    risk_amount = capital * (base_pct / 100.0) * grade_frac * risk_mult

    size = None
    notional = None
    if sl_pts and sl_pts > 0:
        size = round(risk_amount / sl_pts, sz_dec)      # coin units
        notional = size * price

    # leverage: use the target, but never exceed the cap or the coin's max
    lev = min(tgt_lev, max_lev, float(getattr(st.ri, "max_leverage", max_lev)))
    margin = (notional / lev) if notional else None

    # estimated isolated-margin liquidation price at that leverage
    if side == "long":
        liq_px = price * (1 - 1.0 / lev)
    else:
        liq_px = price * (1 + 1.0 / lev)
    # is the structural stop safely inside the liquidation? (want SL to hit first)
    stop_inside = None
    if sl is not None:
        stop_inside = (sl > liq_px) if side == "long" else (sl < liq_px)

    # funding drag over the hold (a long PAYS when funding is positive)
    funding = st.funding
    funding_cost = None
    if funding is not None and notional:
        signed = funding if side == "long" else -funding
        funding_cost = signed * notional * hold_h     # >0 = you pay

    rr = plan.get("rr") if plan else None
    verdict, reason = "GO", "structure + flow favor the trade"
    if rr is None:
        verdict, reason = "CAUTION", "no target/SL to size against"
    elif rr < 1.5:
        verdict, reason = "CAUTION", f"thin reward:risk ({rr}) — smaller size or wait"
    if stop_inside is False:
        verdict, reason = "NO-GO", "structural stop is beyond the liquidation price — lower leverage"
    if funding_cost is not None and tgt_pts and size and \
            funding_cost > 0.4 * (tgt_pts * size):
        verdict, reason = "CAUTION", "funding drag eats a large share of the target — hold short or size down"

    return {
        "actionable": True, "grade": setup["grade"], "side": side,
        "vehicle": f"{st.ri.coin} perp",
        "entry": _r(price, 4),
        "size_coin": size, "notional_usd": _r(notional),
        "leverage": lev, "margin_usd": _r(margin),
        "liq_price_est": _r(liq_px, 4), "stop_inside_liq": stop_inside,
        "stop_loss": _r(sl, 4), "target1": _r(tgt, 4), "rr": rr,
        "risk_amount_usd": _r(risk_amount), "risk_pct": _r(base_pct * grade_frac * risk_mult, 2),
        "sl_points": _r(sl_pts, 4), "target_points": _r(tgt_pts, 4),
        "funding_hourly": funding,
        "funding_cost_est_usd": _r(funding_cost, 2),
        "hold_hours": hold_h,
        "verdict": verdict, "reason": reason,
        "size": {"profile": profile, "grade_frac": grade_frac,
                 "risk_amount": _r(risk_amount), "lots": size},
    }
