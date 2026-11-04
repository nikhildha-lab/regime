"""Turn a confluence signal into a concrete, level-based trade plan.

Given the direction and the live analytics, this proposes a perp trade:
  * instrument      — PERP long/short (with a leverage hint), or a mean-reversion
                      scale into a liquidation magnet on rotation
  * entry           — act-now reference + the ideal pullback level
  * stop_loss       — just beyond the nearest structural level that invalidates it,
                      clamped to a volatility floor/ceiling
  * target1/target2 — next structural levels in the trade's direction
  * rr              — reward:risk on target1

Everything is derived from levels the engine already computes (VWAP, POC, VAH/VAL,
IB, liquidation magnets, swings). Decision aid, not a recommendation.
"""
from __future__ import annotations


def _r(x, n=2):
    """Round to >= n decimals but keep 6 significant figures, so sub-$1 coins
    (PEPE, SHIB, BONK...) do not collapse to 0.0 / 0.01."""
    if x is None:
        return None
    return float(f"{x:.6g}") if abs(x) < 10 ** 4 else round(x, n)


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
    add(getattr(oi, "liq_up", None), "LiqUp"); add(getattr(oi, "liq_dn", None), "LiqDn")
    for h in (vp.hvn or []):
        add(h, "HVN")
    return lv


def _choose_instrument(direction, price, pa, vp, mp, oi, imb):
    """Return (instrument, note) for a perp trade."""
    directional = (mp.day_type in ("Trend", "Normal-Variation")
                   or imb.volume_spike
                   or mp.ib_extension in ("up", "down")
                   or pa.structure_event.startswith(("BOS",)))
    dir_word = "short" if direction == "short" else "long"

    # mean-reversion into a liquidation magnet on the far side?
    magnet = None
    liq_up = getattr(oi, "liq_up", None)
    liq_dn = getattr(oi, "liq_dn", None)
    if direction == "long" and liq_dn and abs(price - liq_dn) / max(price, 1) < 0.0015:
        magnet = ("LiqDn", liq_dn)
    if direction == "short" and liq_up and abs(price - liq_up) / max(price, 1) < 0.0015:
        magnet = ("LiqUp", liq_up)

    if directional:
        return (f"PERP {dir_word} (higher leverage ok on a clean move)",
                "Directional day (trend / IB-extension / BOS) — momentum expected; "
                "trail behind structure.")
    if magnet:
        return (f"PERP {dir_word} — reversal off the {magnet[0]} {magnet[1]:g} magnet",
                "Rotation into stacked liquidation liquidity — scale in, hard stop "
                "beyond the magnet; funding may pay you to hold.")
    return (f"PERP {dir_word}",
            "Mixed day-type — size small / lower leverage until structure confirms.")


def make_plan(direction, price, pa, vp, mp, oi, imb,
              tick_size=0.1, profile_bin=25.0, rr_min=1.5, dte=None,
              atr=None) -> dict | None:
    if price is None:
        return None
    lv = _levels(pa, vp, mp, oi)
    buf = max(tick_size * 4, profile_bin * 0.5)
    # plan v2 (Oct 2026): stops must survive real execution. The old floor
    # (0.20-0.40 pct of price) meant round-trip fees+slippage (~0.11 pct) cost
    # 0.25-0.5R per trade - more than the measured edge. Floor raised to
    # 0.60 pct / 1.5x ATR, cap 1.5 pct / 3x ATR: fewer, longer, practical trades.
    min_risk = max((atr or 0) * 1.5, profile_bin * 4, price * 0.0060)
    max_risk = max((atr or 0) * 3.0, profile_bin * 12, price * 0.0150)

    if direction == "long":
        below = sorted([v for v, _ in lv if v < price - buf], reverse=True)
        beyond = sorted([v for v, _ in lv if v > price + buf])
        sl_level = (below[0] - buf) if below else (price - min_risk)
        support_lab = next((lab for v, lab in lv if below and abs(v - below[0]) < 1e-6), "structure")
    else:
        above = sorted([v for v, _ in lv if v > price + buf])
        beyond = sorted([v for v, _ in lv if v < price - buf], reverse=True)
        sl_level = (above[0] + buf) if above else (price + min_risk)
        support_lab = next((lab for v, lab in lv if above and abs(v - above[0]) < 1e-6), "structure")

    struct_risk = abs(price - sl_level)
    risk = min(max(struct_risk, min_risk), max_risk)
    sl = _r(price - risk, 2) if direction == "long" else _r(price + risk, 2)   # 6 sig figs: sub-$10 coins keep their decimals

    def dist(v):
        return (v - price) if direction == "long" else (price - v)

    near = beyond[0] if beyond else None
    if near is None or dist(near) < rr_min * risk or dist(near) > 3 * risk:
        t1 = price + 2.0 * risk if direction == "long" else price - 2.0 * risk
    else:
        t1 = near
    t1 = _r(t1, 2)
    t2 = next((v for v in beyond if dist(v) > dist(t1) * 1.05), None)

    risk = abs(price - sl)
    reward = abs(t1 - price)
    rr = round(reward / risk, 2) if risk > 0 else None

    instrument, note = _choose_instrument(direction, price, pa, vp, mp, oi, imb)

    return {
        "instrument": instrument,
        "note": note,
        "entry": _r(price, 4),
        "entry_hint": f"act on retest near {support_lab} / VWAP; avoid chasing",
        "stop_loss": _r(sl, 4),
        "target1": _r(t1, 4),
        "target2": _r(t2, 4),
        "rr": rr,
        "risk_pts": _r(risk, 4),
    }
