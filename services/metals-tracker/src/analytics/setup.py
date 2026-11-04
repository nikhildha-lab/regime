"""Graded reversal setup — confluence scoring with a continuation/fade split.

The key idea (why regime is NOT a hard gate): regime is trend-following and lags,
while a reversal is counter-trend by nature — so at a turn the regime always looks
'opposite'. Gating a leading signal with a lagging one is self-defeating. Instead
we pick the PLAYBOOK:

  * continuation — reversal WITH the trend (buy a dip to support in an uptrend).
                   Regime alignment confirms it (+1).
  * fade         — reversal AGAINST the trend (bearish divergence + absorption at
                   a high in an uptrend). The opposite regime is EXPECTED, not a
                   penalty. What confirms a fade is a faster, objective signal:
                   Change of Character (CHoCH) — price breaking its last higher-low
                   (for a short) / lower-high (for a long). A fade WITH CHoCH is
                   tradeable now; a fade WITHOUT CHoCH in a STRONG trend goes to a
                   'watch' state (wait for the crack) instead of being vetoed.

Scoring:
    reversal at a level            required (the trigger)
    + Tier-1 level                 +2      (Tier-2 = +1)
    + strong absorption            +1
    + divergence agrees            +2      (+1 if 'strong');  opposes  -1
    + continuation & regime aligned +1
    + fade confirmed by CHoCH      +2
Points -> A+/A/B/C, with a single `min_grade` dial. `state` is 'ready' or 'watch'.
"""
from __future__ import annotations

_ORDER = {"C": 0, "B": 1, "A": 2, "A+": 3}
DEFAULT_TIER1 = ("PDH", "PDL", "PWH", "PWL", "IB-low", "IB-high")

_CHOCH = {"long": ("CHoCH_UP", "BOS_UP"), "short": ("CHoCH_DOWN", "BOS_DOWN")}


def grade(read, div, reg, pa, price, tier1=DEFAULT_TIER1, min_grade="B",
          agree_score=0, conf_a=2, conf_b=4):
    if not read:
        return None
    side = read["side"]                       # long / short
    trend = getattr(pa, "trend", None)
    structure = getattr(pa, "structure_event", None)
    pts = 0
    reasons = [f"absorption reversal at {read['level_name']} {read['level']}"]

    tier1_hit = read["level_name"] in tier1
    pts += 2 if tier1_hit else 1
    reasons.append("Tier-1 level (+2)" if tier1_hit else "Tier-2 level (+1)")

    if read.get("strength") == "strong":
        pts += 1
        reasons.append("strong absorption (+1)")

    # agreeing volume/OI/structure signals — how Tier-2 levels earn their way up
    if agree_score >= conf_b:
        pts += 2
        reasons.append(f"strong signal confluence (score {agree_score}, +2)")
    elif agree_score >= conf_a:
        pts += 1
        reasons.append(f"signal confluence (score {agree_score}, +1)")

    dcon = div.confirms(side) if div else None
    if dcon is True:
        dpts = 2 + (1 if (div.swing and div.swing.get("strength") == "strong") else 0)
        pts += dpts
        reasons.append(f"delta divergence agrees (+{dpts})")
    elif dcon is False:
        pts -= 1
        reasons.append("delta divergence opposes (-1)")

    # playbook: is this reversal WITH or AGAINST the trend?
    with_trend = (side == "long" and trend == "up") or (side == "short" and trend == "down")
    counter = (side == "long" and trend == "down") or (side == "short" and trend == "up")
    strong_trend = bool(reg and reg.regime in ("trend_up", "trend_down")
                        and reg.confidence == "strong")
    choch = structure in _CHOCH[side]
    state = "ready"
    aligned = False

    if with_trend:
        mode = "continuation"
        # align check via regime action-map (kept as a soft bonus)
        try:
            from . import regime as _rg
            aligned = _rg.aligns(reg, side, price) if reg else False
        except Exception:
            aligned = False
        if aligned:
            pts += 1
            reasons.append("with trend/regime (+1)")
    elif counter:
        mode = "fade"
        if choch:
            pts += 2
            reasons.append("change-of-character confirms the turn (+2)")
        elif strong_trend:
            state = "watch"
            reasons.append("fade vs a STRONG trend, no CHoCH yet — WATCH (wait for the crack)")
        else:
            reasons.append("early fade — no CHoCH yet")
    else:
        mode = "range"
        reasons.append("range / no clear trend — fade ok")


    # ---- shadow grade v2 (pre-registered Oct 2026) -------------------------
    # Logged on every setup, never trades. Differences from v1:
    #   opposing divergence -2 (was -1) · continuation at Tier-1 +2 (was +1)
    #   fade without CHoCH capped at B · mid-range fades (not VAH/VAL/IB) -> watch
    pts2 = 2 if tier1_hit else 1
    if read.get("strength") == "strong":
        pts2 += 1
    if agree_score >= conf_b:
        pts2 += 2
    elif agree_score >= conf_a:
        pts2 += 1
    if dcon is True:
        pts2 += 2 + (1 if (div.swing and div.swing.get("strength") == "strong") else 0)
    elif dcon is False:
        pts2 -= 2
    state2 = "ready"
    if mode == "continuation":
        if aligned:
            pts2 += 2 if tier1_hit else 1
    elif mode == "fade":
        if choch:
            pts2 += 2
        elif strong_trend:
            state2 = "watch"
    else:  # range
        if read["level_name"] not in ("VAH", "VAL", "IB-high", "IB-low"):
            state2 = "watch"
    g2 = "A+" if pts2 >= 6 else "A" if pts2 >= 4 else "B" if pts2 >= 2 else "C"
    if mode == "fade" and not choch and _ORDER[g2] > _ORDER["B"]:
        g2 = "B"
    g = "A+" if pts >= 6 else "A" if pts >= 4 else "B" if pts >= 2 else "C"
    actionable = (state == "ready") and _ORDER[g] >= _ORDER.get(min_grade, 1)
    return {
        "grade": g, "side": side, "mode": mode, "state": state,
        "points": pts, "reasons": reasons,
        "level": read["level"], "level_name": read["level_name"],
        "strength": read.get("strength"),
        "divergence": dcon is True, "choch": choch,
        "actionable": actionable,
        "v2": {"grade": g2, "points": pts2, "state": state2},
    }
