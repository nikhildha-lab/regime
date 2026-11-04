"""
The 4 setups. Each returns signals: when, direction (+1 long / -1 short), stop, target.
All prices are in the signal instrument (NIFTYBEES / BANKBEES).

1. VALUE_REJECT  - price pokes outside yesterday's value area, sellers/buyers push it back
2. RULE_80       - opens outside value, comes back in and holds 2 bars -> go to the other edge
3. IB_BREAK      - breaks the first-hour range with cumulative delta confirming
4. ABSORB        - big volume, tiny range at a key level, then delta flips -> reversal
"""

import config as C


def _t(ts):
    return ts.strftime("%H:%M")


def _ok_time(row):
    return C.FIRST_ENTRY <= _t(row["ready"]) <= C.LAST_ENTRY


def _sig(row, setup, d, stop, target):
    return {"setup": setup, "date": row["date"], "signal_ts": row["ready"],
            "dir": d, "ref": row["close"], "stop": stop, "target": target}


def value_reject(bars, lv):
    out = []
    for date, day in bars.groupby("date"):
        if date not in lv.index:
            continue
        L = lv.loc[date]
        for _, r in day.iterrows():
            if not _ok_time(r) or r["volume"] <= 0:
                continue
            dr = r["delta"] / r["volume"]
            # short: poked above VAH, closed back below, sellers in control
            if r["high"] >= L.p_vah and r["close"] < L.p_vah and r["open"] <= L.p_vah * 1.002 and dr <= -C.DELTA_RATIO_MIN:
                out.append(_sig(r, "VALUE_REJECT", -1, r["high"] * 1.0005, L.p_poc))
            # long: poked below VAL, closed back above, buyers in control
            elif r["low"] <= L.p_val and r["close"] > L.p_val and r["open"] >= L.p_val * 0.998 and dr >= C.DELTA_RATIO_MIN:
                out.append(_sig(r, "VALUE_REJECT", 1, r["low"] * 0.9995, L.p_poc))
    return out


def rule_80(bars, lv):
    out = []
    for date, day in bars.groupby("date"):
        if date not in lv.index:
            continue
        L = lv.loc[date]
        width = L.p_vah - L.p_val
        if L.open > L.p_vah:
            side = -1   # opened above value -> come back in -> target VAL
        elif L.open < L.p_val:
            side = 1
        else:
            continue
        inside = 0
        for _, r in day.iterrows():
            in_value = L.p_val < r["close"] < L.p_vah
            inside = inside + 1 if in_value else 0
            if inside >= 2 and _ok_time(r):
                if side == -1:
                    out.append(_sig(r, "RULE_80", -1, L.p_vah + 0.25 * width, L.p_val))
                else:
                    out.append(_sig(r, "RULE_80", 1, L.p_val - 0.25 * width, L.p_vah))
                break
    return out


def ib_break(bars, lv):
    out = []
    for date, day in bars.groupby("date"):
        if date not in lv.index:
            continue
        L = lv.loc[date]
        rng = L.ib_high - L.ib_low
        if rng <= 0:
            continue
        mid = (L.ib_high + L.ib_low) / 2
        cvd_hi = cvd_lo = 0.0
        for _, r in day.iterrows():
            cvd = r["cvd"]
            if _t(r["ready"]) > C.IB_END and _ok_time(r):
                if r["close"] > L.ib_high and cvd > cvd_hi and cvd > 0:
                    out.append(_sig(r, "IB_BREAK", 1, mid, L.ib_high + rng))
                elif r["close"] < L.ib_low and cvd < cvd_lo and cvd < 0:
                    out.append(_sig(r, "IB_BREAK", -1, mid, L.ib_low - rng))
            cvd_hi, cvd_lo = max(cvd_hi, cvd), min(cvd_lo, cvd)
    return out


def absorb(bars, lv):
    out = []
    for date, day in bars.groupby("date"):
        if date not in lv.index:
            continue
        L = lv.loc[date]
        rows = day.reset_index(drop=True)
        for i in range(1, len(rows)):
            a, r = rows.iloc[i - 1], rows.iloc[i]
            if not _ok_time(r) or a["volume"] <= 0:
                continue
            levels = [L.p_vah, L.p_val, L.p_poc, L.p_high, L.p_low]
            if _t(a["ready"]) > C.IB_END:
                levels += [L.ib_high, L.ib_low]
            mid = (a["high"] + a["low"]) / 2
            at_level = any(abs(mid - x) / x <= C.LEVEL_TOUCH_PCT for x in levels)
            big_vol = a["avg_vol"] > 0 and a["volume"] >= C.ABSORB_VOL_MULT * a["avg_vol"]
            small_rng = a["avg_rng"] > 0 and (a["high"] - a["low"]) <= C.ABSORB_RANGE_MULT * a["avg_rng"]
            if not (at_level and big_vol and small_rng):
                continue
            # delta flips on the next bar -> trade the new side
            if a["delta"] < 0 < r["delta"] and r["close"] > a["high"]:
                stop = a["low"] * 0.9995
                out.append(_sig(r, "ABSORB", 1, stop, r["close"] + 2 * (r["close"] - stop)))
            elif a["delta"] > 0 > r["delta"] and r["close"] < a["low"]:
                stop = a["high"] * 1.0005
                out.append(_sig(r, "ABSORB", -1, stop, r["close"] - 2 * (stop - r["close"])))
    return out


ALL_SETUPS = {"VALUE_REJECT": value_reject, "RULE_80": rule_80, "IB_BREAK": ib_break, "ABSORB": absorb}
