"""Turn a confluence signal into a concrete, level-based trade plan.

Given the direction and the live analytics, this proposes:
  * instrument      — FUTURES / BUY_OPTION / SELL_OPTION (from day-type, vol, expiry)
  * entry           — act-now reference + the ideal pullback level
  * stop_loss       — just beyond the nearest structural level that invalidates it
  * target1/target2 — next structural levels in the trade's direction
  * rr              — reward:risk on target1

Everything is derived from levels the engine already computes (VWAP, POC, VAH/VAL,
IB, OI walls, swings) — no arbitrary point counts. It is a decision aid, not a
recommendation.
"""
from __future__ import annotations


def _r(x, n=2):
    return None if x is None else round(x, n)


def _levels(pa, vp, mp, oi):
    """Collect candidate structural levels with labels."""
    lv = []
    def add(v, lab):
        if v is not None:
            lv.append((float(v), lab))
    add(pa.vwap, "VWAP")
    add(vp.poc, "VP-POC"); add(vp.vah, "VAH"); add(vp.val, "VAL")
    add(mp.tpo_poc, "TPO-POC"); add(mp.ib_high, "IB-H"); add(mp.ib_low, "IB-L")
    add(pa.orb_high, "ORB-H"); add(pa.orb_low, "ORB-L")
    add(pa.last_swing_high, "swingH"); add(pa.last_swing_low, "swingL")
    add(oi.call_wall, "CallWall"); add(oi.put_wall, "PutWall")
    for h in (vp.hvn or []):
        add(h, "HVN")
    return lv


def _choose_instrument(direction, price, pa, vp, mp, oi, imb, dte):
    """Return (instrument, note). Directional -> futures/buy; rotation -> sell."""
    directional = (mp.day_type in ("Trend", "Normal-Variation")
                   or imb.volume_spike
                   or mp.ib_extension in ("up", "down")
                   or pa.structure_event.startswith(("BOS",)))
    # mean-reversion at a wall / value edge?
    near = None
    if direction == "long" and oi.put_wall and abs(price - oi.put_wall) / max(price, 1) < 0.0015:
        near = ("PutWall", oi.put_wall)
    if direction == "short" and oi.call_wall and abs(price - oi.call_wall) / max(price, 1) < 0.0015:
        near = ("CallWall", oi.call_wall)

    expiry_soon = dte is not None and dte <= 0

    if directional:
        opt = "BUY_PUT" if direction == "short" else "BUY_CALL"
        note = "Directional day (trend/IB-extension/BOS) — clean move expected."
        if expiry_soon:
            note += " Expiry today: theta punishes buyers — futures or ITM only."
        return f"FUTURES ({'short' if direction=='short' else 'long'}) or {opt} (ATM/ITM)", note
    if near:
        sell = "SELL_CALL" if direction == "short" else "SELL_PUT"
        return (f"{sell} at {near[0]} {near[1]:g} (credit)",
                "Rotation/mean-reversion into an OI wall — theta favours the seller. "
                "Hard SL beyond the wall is mandatory.")
    # default balanced
    opt = "BUY_PUT" if direction == "short" else "BUY_CALL"
    base = f"FUTURES ({'short' if direction=='short' else 'long'}) or {opt}"
    if expiry_soon:
        return base, "No strong day-type read; expiry today favours selling over buying."
    return base, "Mixed day-type — size small until structure confirms."


def make_plan(direction, price, pa, vp, mp, oi, imb,
              tick_size=0.05, profile_bin=5.0, rr_min=1.5, dte=None,
              atr=None) -> dict | None:
    if price is None:
        return None
    lv = _levels(pa, vp, mp, oi)
    buf = max(tick_size * 4, profile_bin * 0.5)
    # a stop must be at least this wide (or noise takes it out) and no wider than
    # this (or the trade isn't clean) — keeps R:R realistic when price is at an edge
    min_risk = max((atr or 0) * 1.2, profile_bin * 3, price * 0.0010)
    max_risk = max((atr or 0) * 2.5, profile_bin * 8, price * 0.0030)

    if direction == "long":
        below = sorted([v for v, _ in lv if v < price - buf], reverse=True)
        beyond = sorted([v for v, _ in lv if v > price + buf])   # potential targets
        sl_level = (below[0] - buf) if below else (price - min_risk)
        support_lab = next((lab for v, lab in lv if below and abs(v - below[0]) < 1e-6), "structure")
    else:  # short
        above = sorted([v for v, _ in lv if v > price + buf])
        beyond = sorted([v for v, _ in lv if v < price - buf], reverse=True)
        sl_level = (above[0] + buf) if above else (price + min_risk)
        support_lab = next((lab for v, lab in lv if above and abs(v - above[0]) < 1e-6), "structure")

    struct_risk = abs(price - sl_level)
    risk = min(max(struct_risk, min_risk), max_risk)
    sl = round(price - risk, 2) if direction == "long" else round(price + risk, 2)

    def dist(v):
        return (v - price) if direction == "long" else (price - v)

    near = beyond[0] if beyond else None
    if near is None or dist(near) < rr_min * risk or dist(near) > 3 * risk:
        # nearest structural target missing / too close / too far -> measured 2R
        t1 = price + 2.0 * risk if direction == "long" else price - 2.0 * risk
    else:
        t1 = near
    t1 = round(t1, 2)
    # T2 = first structural level clearly beyond T1 (the far wall becomes the runner)
    t2 = next((v for v in beyond if dist(v) > dist(t1) * 1.05), None)

    risk = abs(price - sl)
    reward = abs(t1 - price)
    rr = round(reward / risk, 2) if risk > 0 else None

    instrument, note = _choose_instrument(direction, price, pa, vp, mp, oi, imb, dte)

    return {
        "instrument": instrument,
        "note": note,
        "entry": _r(price),
        "entry_hint": f"act on retest near {support_lab} / VWAP; avoid chasing",
        "stop_loss": _r(sl),
        "target1": _r(t1),
        "target2": _r(t2),
        "rr": rr,
        "risk_pts": _r(risk),
    }
