"""
"Next 3-5 days" outlook. Everything uses data up to the close of day t; outcomes are t -> t+h.

1. Expected range   - forecast daily vol = RiskMetrics EWMA (lambda 0.94), averaged with implied vol when the
                      market has one (India VIX, VIX). Range = +-1 forecast sd over h days. A calibration factor k
                      (so that ~68% of past h-day moves fell inside) is learned from the market's OWN PAST only.
2. Big-move chance  - big move = |h-day return| above the 80th percentile of the past year's h-day moves.
                      Chance = how often a big move followed, in the past, when the vol forecast sat in the same
                      fifth (quintile) of its 1-year range as today. Vol clustering - the best-established fact in
                      market data - is the only input.
3. Reversal watch   - price-based exhaustion signs, each tested against luck before it may be shown as a warning
                      (scripts/test_next_days.py).
4. History check    - past days in the same state (exposure regime, vol tercile, 20-day-return tercile):
                      how the next h days went. Information, not a forecast.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from src.common.regime_engine import exposure_regime as XR

H = 5
SIGNS = {
    "stretch": "Price stretched more than 2 sd from its 20-day average in the trend's direction",
    "rsi": "RSI(14) above 70 in an uptrend / below 30 in a downtrend",
    "long_run": "Trend regime has lasted 30+ days",
    "fade": "Strong 20-day move but the last 5 days went the other way",
    "extreme": "At a new 1-year high (uptrend) / low (downtrend)",
}


def _pct_rank(s: pd.Series, win=252, minp=120) -> pd.Series:
    return s.rolling(win, min_periods=minp).apply(lambda a: (a <= a[-1]).mean() * 100, raw=True)


def frame(bars: pd.DataFrame, iv: pd.Series | None = None, ann: int = 252, h: int = H) -> pd.DataFrame:
    c = bars["close"].astype(float).where(lambda x: x > 0)
    lr = np.log(c / c.shift())
    f = XR.features(bars, iv, ann)
    lab = XR.label(f)
    d = pd.DataFrame(index=bars.index)
    d["close"] = c
    d["label"] = lab
    ew = np.sqrt((lr ** 2).ewm(alpha=0.06, adjust=False, min_periods=30).mean())
    sig = ew
    if iv is not None:
        ivd = (iv.reindex(bars.index, method="ffill", limit=5) / 100 / np.sqrt(ann))
        sig = pd.concat([ew, ivd], axis=1).mean(axis=1)          # equal mix of history and options market
    d["sig_d"] = sig
    d["sig_pct"] = _pct_rank(sig)
    d["ret20"] = c / c.shift(20) - 1
    d["ret20_pct"] = _pct_rank(d["ret20"])
    d["ret5"] = c / c.shift(5) - 1
    d["dir"] = np.sign(d["ret20"])
    # outcomes (future; only ever used for days at least h days before 'today')
    d["fwd"] = c.shift(-h) / c - 1
    d["fwd3"] = c.shift(-3) / c - 1
    absf = d["fwd"].abs()
    # past-year 80th percentile of |h-day move|, known at t (uses moves that ended by t)
    past_moves = (c / c.shift(h) - 1).abs()
    d["big_thr"] = past_moves.rolling(252, min_periods=120).quantile(0.8)
    d["big"] = (absf > d["big_thr"]).where(d["fwd"].notna() & d["big_thr"].notna())
    d["z_in_range"] = d["fwd"] / (d["sig_d"] * np.sqrt(h))
    # reversal signs (direction of the prevailing 20-day move)
    sma, sd = c.rolling(20).mean(), c.rolling(20).std()
    z20 = (c - sma) / sd
    up, dn = d["dir"] > 0, d["dir"] < 0
    rsi = f["rsi"]
    runs = (lab != lab.shift()).cumsum()
    age = lab.groupby(runs).cumcount() + 1
    s20 = d["sig_d"] * np.sqrt(20)
    d["s_stretch"] = (up & (z20 > 2)) | (dn & (z20 < -2))
    d["s_rsi"] = (up & (rsi > 70)) | (dn & (rsi < 30))
    d["s_long_run"] = (((lab == "Bull_Trend") & up) | ((lab == "Bear_Trend") & dn)) & (age >= 30)
    d["s_fade"] = (d["ret20"].abs() > s20) & (np.sign(d["ret5"]) == -d["dir"])
    hi, lo = c.rolling(252, min_periods=200).max(), c.rolling(252, min_periods=200).min()
    d["s_extreme"] = (up & (c >= hi)) | (dn & (c <= lo))
    d["rev"] = (np.sign(d["fwd"]) == -d["dir"]).where(d["fwd"].notna() & (d["dir"] != 0))
    d["age"] = age
    return d


def calib_k(d: pd.DataFrame, upto: int, h: int = H) -> float:
    """k such that 68% of past h-day moves fell inside +-k*sd (past only: rows before 'upto - h')."""
    z = d["z_in_range"].iloc[: max(0, upto - h)].dropna()
    if len(z) < 250:
        return 1.0
    return float(np.quantile(z.abs(), 0.6827))


def live(d: pd.DataFrame, passes: dict | None = None, h: int = H) -> dict:
    """Outlook for the last row of d. passes = {sign: 'reversal'|'continuation'} from the luck test."""
    n = len(d)
    t = d.iloc[-1]
    hist = d.iloc[: max(0, n - h)]
    k = calib_k(d, n, h)
    px, sd = float(t["close"]), float(t["sig_d"])
    rng = {}
    for hh in (3, 5):
        kk = calib_k(d.assign(z_in_range=(d["fwd3"] if hh == 3 else d["fwd"]) / (d["sig_d"] * np.sqrt(hh))), n, hh)
        m = kk * sd * np.sqrt(hh)
        rng[str(hh)] = {"lo": round(px * (1 - m), 2), "hi": round(px * (1 + m), 2), "pct": round(m * 100, 2),
                        "lo2": round(px * (1 - 2 * m), 2), "hi2": round(px * (1 + 2 * m), 2)}
    # big-move chance by vol-forecast quintile
    q = lambda p: None if pd.isna(p) else min(4, int(p // 20))
    cur_q = q(t["sig_pct"])
    hh_ = hist.dropna(subset=["big", "sig_pct"])
    big = None
    if cur_q is not None and len(hh_) > 250:
        qq = hh_["sig_pct"].apply(q)
        same = hh_[qq == cur_q]["big"].astype(float)
        base = hh_["big"].astype(float).mean()
        big = {"chance": round(float(same.mean() * 100)), "normal": round(float(base * 100)), "n": int(len(same)),
               "vol_pct": round(float(t["sig_pct"])), "quintile": cur_q + 1}
        big["word"] = ("much higher than normal" if big["chance"] >= big["normal"] * 1.5 else "higher than normal"
                       if big["chance"] >= big["normal"] * 1.2 else "lower than normal" if big["chance"] <= big["normal"] * 0.8
                       else "about normal")
    # reversal / continuation signs active today
    active = []
    for s in SIGNS:
        if bool(t.get(f"s_{s}", False)):
            v = (passes or {}).get(s)
            active.append({"sign": s, "text": SIGNS[s], "tested": v or ("not better than luck" if passes is not None else "untested")})
    all_signs = [{"sign": s_, "text": SIGNS[s_], "active": bool(t.get(f"s_{s_}", False)),
                  "tested": (passes or {}).get(s_) or ("not better than luck" if passes is not None else "untested")} for s_ in SIGNS]
    # history check
    st = {"label": t["label"], "volq": None if pd.isna(t["sig_pct"]) else int(min(2, t["sig_pct"] // 33.34)),
          "retq": None if pd.isna(t["ret20_pct"]) else int(min(2, t["ret20_pct"] // 33.34))}
    hist2 = hist.dropna(subset=["fwd", "sig_pct", "ret20_pct"])
    analog = None
    if st["volq"] is not None and st["retq"] is not None:
        m = hist2[(hist2["label"] == st["label"]) & ((hist2["sig_pct"] // 33.34).clip(upper=2) == st["volq"])
                  & ((hist2["ret20_pct"] // 33.34).clip(upper=2) == st["retq"])]
        if len(m) >= 30:
            fw = m["fwd"]
            analog = {"n": int(len(m)), "up_pct": round(float((fw > 0).mean() * 100)), "median": round(float(fw.median() * 100), 2),
                      "p10": round(float(fw.quantile(0.1) * 100), 2), "p90": round(float(fw.quantile(0.9) * 100), 2),
                      "all_up_pct": round(float((hist2["fwd"] > 0).mean() * 100)),
                      "state": f"{st['label'].replace('_', ' ')}, {['low', 'middle', 'high'][st['volq']]} volatility, "
                               f"{['weak', 'middle', 'strong'][st['retq']]} 20-day return"}
    return {"date": str(d.index[-1].date()), "close": px, "range": rng, "k": round(k, 2), "big_move": big, "signs": active, "all_signs": all_signs,
            "direction_20d": "up" if t["dir"] > 0 else "down" if t["dir"] < 0 else "flat", "regime": t["label"],
            "regime_days": int(t["age"]), "analog": analog}


def entry_timing(d: pd.DataFrame, bars: pd.DataFrame, market: str | None, pb: dict | None) -> dict:
    """Stretch, typical pullback zone and retest level for the current trend (rules from scripts/test_pullbacks.py).
    pb = {"T4": {market: depth stats}, "T1": rows, "T3": rows}."""
    from src.common.regime_engine import levels as LV
    c = bars["close"].astype(float)
    t = d.iloc[-1]
    z20 = float((c.iloc[-1] - c.iloc[-20:].mean()) / c.iloc[-20:].std())
    f = XR.features(bars)
    rsi = float(f["rsi"].iloc[-1])
    a = float(LV.atr(bars).iloc[-1])
    reg = t["label"]
    out = {"z20": round(z20, 2), "rsi": round(rsi, 1), "atr": round(a, 2), "regime": reg, "lines": []}
    dep = ((pb or {}).get("T4") or {}).get(market) or {"atr_p25": 1.2, "atr_median": 1.7, "atr_p75": 2.6, "leg_median": 0.28}
    stretched_dn = z20 < -2 or rsi < 30
    stretched_up = z20 > 2 or rsi > 70
    out["stretched"] = "down" if stretched_dn else "up" if stretched_up else None
    t1 = {(r["market"], "long" if "LONG" in r["test"] else "short"): r["verdict"] for r in (pb or {}).get("T1", [])}
    t3 = {r["market"]: r for r in (pb or {}).get("T3", [])}
    if reg in ("Bull_Trend", "Bear_Trend"):
        s = 1 if reg == "Bull_Trend" else -1
        ext = float(bars["high"].iloc[-20:].max() if s == 1 else bars["low"].iloc[-20:].min())
        z_lo = ext - s * dep["atr_p25"] * a
        z_hi = ext - s * dep["atr_p75"] * a
        out["pullback_zone"] = {"from": round(min(z_lo, z_hi), 2), "to": round(max(z_lo, z_hi), 2), "extreme": round(ext, 2),
                                "median": round(ext - s * dep["atr_median"] * a, 2)}
        word = "dip" if s == 1 else "bounce"
        out["lines"].append(f"Typical {word} in a {reg.replace('_', ' ').lower()} here: {dep['atr_p25']}-{dep['atr_p75']} ATR "
                            f"from the 20-day {'high' if s == 1 else 'low'} ({ext:,.2f}) -> zone {min(z_lo, z_hi):,.2f} - {max(z_lo, z_hi):,.2f} "
                            f"(median {ext - s * dep['atr_median'] * a:,.2f}; about {round(dep['leg_median'] * 100)}% of the last leg). "
                            "Only ~1 in 5 pullbacks stopped at the 20-day EMA, so it is not a special level.")
        hi20p, lo20p = c.iloc[-21:-1].max(), c.iloc[-21:-1].min()
        # a breakout in the last 10 days -> retest level
        rec = c.iloc[-10:]
        lvl = None
        for i in range(len(c) - 10, len(c)):
            prev = c.iloc[max(0, i - 20):i]
            if len(prev) == 20 and ((s == 1 and c.iloc[i] > prev.max()) or (s == -1 and c.iloc[i] < prev.min())):
                lvl = float(prev.max() if s == 1 else prev.min())
        if lvl is not None:
            r3 = t3.get(market)
            ev = (f" Tested here: retest came {r3['retest came %']}% of the time; entering on the retest was "
                  f"{'slightly better' if r3['verdict'] == 'retest better' else 'no better' if r3['verdict'] != 'breakout better' else 'worse'} "
                  "than entering at the breakout." if r3 else "")
            out["retest_level"] = round(lvl, 2)
            out["lines"].append(f"Recent breakout level {lvl:,.2f}: a retest of it (within 0.5 ATR, closing back on the trend side) "
                                f"is the cleaner entry.{ev}")
        if s == -1 and stretched_dn:
            out["no_new"] = "short"
            out["lines"].append(f"Stretched DOWN now (z {z20:+.1f}, RSI {rsi:.0f}): do not open NEW shorts here. On 6,600 real strategy "
                                "trades, shorts opened when the market was this stretched did clearly worse (tested against luck, both "
                                "halves). Hold existing shorts with stops; add on a bounce into the zone above or a retest.")
        if s == 1 and stretched_up:
            if t1.get((market, "long")) == "WORSE - avoid":
                out["no_new"] = "long"
                out["lines"].append(f"Stretched UP now (z {z20:+.1f}, RSI {rsi:.0f}): in this market new longs at such stretches did "
                                    "worse over 10 days (tested) - wait for a dip into the zone below.")
            else:
                out["lines"].append(f"Stretched UP now (z {z20:+.1f}, RSI {rsi:.0f}): unlike shorts at the bottom, longs opened when "
                                    "stretched up did NOT do worse on real trades (momentum) - chasing is acceptable, a dip is still a better price.")
    else:
        if stretched_dn:
            out["lines"].append(f"No trend, but stretched down (z {z20:+.1f}, RSI {rsi:.0f}): avoid fresh shorts.")
            out["no_new"] = "short"
        elif stretched_up:
            out["lines"].append(f"No trend, stretched up (z {z20:+.1f}, RSI {rsi:.0f}).")
        else:
            out["lines"].append("No trend and not stretched: no pullback/retest setup.")
    return out


def action(o: dict, iv_pct=None, size=None) -> list[str]:
    """Plain-language 'possible action' lines from the outlook. Rules fixed in advance."""
    out = []
    b, reg = o.get("big_move") or {}, o.get("regime")
    hot = b.get("word", "").endswith("higher than normal")
    calm = b.get("word") == "lower than normal"
    rev_ok = [s for s in o.get("signs", []) if s["tested"] == "reversal"]
    cont_ok = [s for s in o.get("signs", []) if s["tested"] == "continuation"]
    if hot:
        out.append("Big move more likely than usual, direction unknown: keep size at or below the blend size, use wider stops, "
                   "avoid naked short options.")
    elif calm:
        out.append("A quiet few days is more likely than usual: conditions favour option selling within the expected range.")
    else:
        out.append("Normal chance of a big move: no change to size beyond the blend rule.")
    if reg in ("Bull_Trend", "Bear_Trend"):
        side = "up" if reg == "Bull_Trend" else "down"
        if rev_ok:
            out.append(f"Trend is {side} but a tested reversal sign is active: tighten stops or take partial profit rather than add.")
        elif cont_ok:
            out.append(f"Trend is {side} and a tested continuation sign is active: trend positions can be held.")
        else:
            out.append(f"Trend is {side}: trend-following positions stay on at blend size; no tested reversal warning.")
    elif reg == "Range":
        out.append("Range: expect back-and-forth inside the range; breakout entries are less reliable.")
    elif reg == "Compression":
        out.append("Compression: a breakout may come; wait for direction before committing.")
    elif reg == "High_Volatility":
        out.append("High Volatility: smallest size; let the market settle.")
    if size is not None:
        out.append(f"Position size today: {size}x of normal.")
    return out
