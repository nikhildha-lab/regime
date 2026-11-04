"""
PAPER-TRADING JOURNAL - a forward record of what the dashboard said, scored against what happened.

Logged once per market per daily bar (the first dashboard build after a new daily close), point in time:
  forecasts.csv  regime, position size, likely 3/5-day ranges, big-move chance (and the size that counts as big),
                 reversal-first chance (+ its 1-ATR targets), stretch / no-new-entries flag, expected open (India)
  recs.csv       every actionable strategy recommendation (run / paper enter / idea / wait / pause) with horizon
  spreads.csv    each new directional-spread idea with its strikes, priced at entry with Black-Scholes using
                 realised vol (no historical option prices are free), settled at expiry (~30 days) at intrinsic value

Scoring (only when enough later bars exist):
  range        did the close 5 bars later land inside the likely 5-day range? (target 68%)
  big move     Brier score of the big-move chance vs always saying the normal rate
  reversal     Brier score of the reversal chance vs the normal rate
  size book    daily return of holding the blend size (long only) vs holding 1.0x (buy & hold)
  no new X     the next 10-day move in the direction we told you NOT to enter (should be poor)
  spreads      settled P&L in % of the underlying
Risk rules on the paper size book (fixed): drawdown > 10% -> halve sizes; 5 losing days in a row -> pause new entries.
"""
from __future__ import annotations

import math
import os

import numpy as np
import pandas as pd

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
J = os.path.join(ROOT, "data_store", "journal")
F_FC, F_RECS, F_SP = (os.path.join(J, n) for n in ("forecasts.csv", "recs.csv", "spreads.csv"))


def _append(path, rows):
    if not rows:
        return
    os.makedirs(J, exist_ok=True)
    df = pd.DataFrame(rows)
    first = not os.path.exists(path)
    df.to_csv(path, mode="a", index=False, header=first)


def _logged(path, keys):
    if not os.path.exists(path):
        return set()
    d = pd.read_csv(path, usecols=keys)
    return set(map(tuple, d.astype(str).values))


def _bs(S, K, T, vol, typ):
    if T <= 0 or vol <= 0:
        return max(0.0, (S - K) if typ == "C" else (K - S))
    d1 = (math.log(S / K) + 0.5 * vol * vol * T) / (vol * math.sqrt(T))
    d2 = d1 - vol * math.sqrt(T)
    N = lambda x: 0.5 * (1 + math.erf(x / math.sqrt(2)))
    return S * N(d1) - K * N(d2) if typ == "C" else K * N(-d2) - S * N(-d1)


def log(markets: dict, catalog: list, cross: dict | None = None):
    """Called by the dashboard export; writes each market's rows once per daily bar."""
    done = _logged(F_FC, ["market", "date"])
    fc, recs, sps = [], [], []
    now = str(pd.Timestamp.now(tz="Asia/Kolkata").floor("s"))
    for m, mk in markets.items():
        nd = mk.get("next_days") or {}
        d = nd.get("date")
        if not d or (m, d) in done:
            continue
        x, en = mk.get("exposure_regime") or {}, nd.get("entry") or {}
        r3, r5 = (nd.get("range") or {}).get("3") or {}, (nd.get("range") or {}).get("5") or {}
        b, bn = nd.get("big_move") or {}, (en.get("bounce") or {})
        rv = bn.get("reversal") or {}
        eo = ((cross or {}).get("expected_open") or {}) if m in ("NIFTY", "BANKNIFTY") else {}
        fc.append({"market": m, "date": d, "logged_at": now, "close": nd.get("close"), "regime": x.get("regime"),
                   "size_blend": x.get("size_blend"), "regime_exp": x.get("exposure"), "vt_exp": x.get("vol_target_exposure"),
                   "r3_lo": r3.get("lo"), "r3_hi": r3.get("hi"), "r5_lo": r5.get("lo"), "r5_hi": r5.get("hi"), "r5_pct": r5.get("pct"),
                   "big_chance": b.get("chance"), "big_normal": b.get("normal"), "atr": en.get("atr"),
                   "rev_chance": rv.get("chance"), "rev_normal": rv.get("normal"), "rev_dir": rv.get("dir"),
                   "rev_against_to": rv.get("against_to"), "rev_with_to": rv.get("with_to"),
                   "stretched": en.get("stretched"), "no_new": en.get("no_new"),
                   "pz_from": (en.get("pullback_zone") or {}).get("from"), "pz_to": (en.get("pullback_zone") or {}).get("to"),
                   "exp_open_pct": eo.get("gap_pct")})
        for r in catalog:
            if r.get("market") != m or not str(r.get("action", "")).startswith(("Run", "Paper", "Pause")):
                continue
            recs.append({"market": m, "date": d, "strategy": r["strategy"], "group": r["group"], "horizon": r.get("horizon"),
                         "action": r["action"], "lots": r.get("lots"), "size": r.get("size")})
            if r["group"] == "Directional spreads" and r["action"] in ("Paper: idea",) and r.get("legs_struct"):
                S, vol, T = r["spot"], r["vol_pct_ann"] / 100, r.get("dte", 30) / 365
                cost = sum(l["q"] * _bs(S, l["k"], T, vol, l["type"]) for l in r["legs_struct"])
                sps.append({"market": m, "date": d, "strategy": r["strategy"], "spot": S, "vol": vol, "dte": r.get("dte", 30),
                            "legs": ";".join(f"{l['q']:+d}{l['type']}{l['k']:g}" for l in r["legs_struct"]),
                            "entry_cost": round(cost, 4), "size": r.get("size")})
    _append(F_FC, fc)
    _append(F_RECS, recs)
    # one open idea per market+strategy until it settles
    if sps and os.path.exists(F_SP):
        old = pd.read_csv(F_SP)
        open_ = set(zip(old["market"], old["strategy"], old.get("settled", pd.Series([np.nan] * len(old))).isna()))
        sps = [s_ for s_ in sps if (s_["market"], s_["strategy"], True) not in open_]
    _append(F_SP, sps)
    return {"forecasts": len(fc), "recs": len(recs), "spreads": len(sps)}


def _settle_spreads(bars: dict):
    if not os.path.exists(F_SP):
        return
    sp = pd.read_csv(F_SP)
    for col in ("settled", "exit_spot", "pnl_pct"):
        if col not in sp:
            sp[col] = np.nan
    sp["settled"] = sp["settled"].astype(object)
    changed = False
    for i, r in sp[sp["settled"].isna()].iterrows():
        b = bars.get(r["market"])
        if b is None:
            continue
        exp = pd.Timestamp(r["date"]) + pd.Timedelta(days=int(r["dte"]))
        after = b[b.index >= exp]
        if after.empty:
            continue
        S1 = float(after["close"].iloc[0])
        val = 0.0
        for leg in str(r["legs"]).split(";"):
            q, typ, k = int(leg[:2]), leg[2], float(leg[3:])
            val += q * max(0.0, (S1 - k) if typ == "C" else (k - S1))
        sp.loc[i, ["settled", "exit_spot", "pnl_pct"]] = [str(after.index[0].date()), S1, round((val - r["entry_cost"]) / r["spot"] * 100, 3)]
        changed = True
    if changed:
        sp.to_csv(F_SP, index=False)


def score(bars: dict) -> dict | None:
    """bars: market -> daily OHLC (DatetimeIndex). Returns the scorecard shown on the dashboard."""
    if not os.path.exists(F_FC):
        return None
    _settle_spreads(bars)
    fc = pd.read_csv(F_FC)
    out = {"started": str(fc["date"].min()), "days_logged": int(fc["date"].nunique()), "rows": int(len(fc)), "markets": {}}
    rng_hits, big_b, big_n, rev_b, rev_n, eo_err = [], [], [], [], [], []
    book, bh, nn_moves = [], [], []
    for m, g in fc.groupby("market"):
        b = bars.get(m)
        if b is None or b.empty:
            continue
        c, h, l = b["close"], b["high"], b["low"]
        idx = b.index
        mrow = {"logged": int(len(g))}
        for _, r in g.iterrows():
            t = pd.Timestamp(r["date"])
            pos = idx.searchsorted(t)
            if pos >= len(idx) or idx[pos] != t:
                continue
            # size book: next bar's return at the logged size
            if pos + 1 < len(idx) and pd.notna(r["size_blend"]):
                ret = float(c.iloc[pos + 1] / c.iloc[pos] - 1)
                book.append((idx[pos + 1], m, ret * float(r["size_blend"])))
                bh.append((idx[pos + 1], m, ret))
            if pos + 5 < len(idx):
                c5 = float(c.iloc[pos + 5])
                if pd.notna(r["r5_lo"]):
                    rng_hits.append(1.0 if r["r5_lo"] <= c5 <= r["r5_hi"] else 0.0)
                move = abs(c5 / float(c.iloc[pos]) - 1)
                # "big" = beyond the 80th pct of the past year's 5-day moves (re-measured from the bars, point in time)
                past = (c.iloc[max(0, pos - 252):pos + 1] / c.shift(5).iloc[max(0, pos - 252):pos + 1] - 1).abs().dropna()
                if len(past) > 100 and pd.notna(r["big_chance"]):
                    y = 1.0 if move > past.quantile(0.8) else 0.0
                    big_b.append((r["big_chance"] / 100 - y) ** 2)
                    big_n.append((r["big_normal"] / 100 - y) ** 2)
                if pd.notna(r["rev_chance"]) and pd.notna(r["rev_against_to"]):
                    y = 0.5
                    for j in range(pos + 1, pos + 6):
                        up_hit = h.iloc[j] >= max(r["rev_against_to"], r["rev_with_to"])
                        dn_hit = l.iloc[j] <= min(r["rev_against_to"], r["rev_with_to"])
                        a_hit = up_hit if r["rev_dir"] == "up" else dn_hit
                        w_hit = dn_hit if r["rev_dir"] == "up" else up_hit
                        if a_hit and not w_hit:
                            y = 1.0
                            break
                        if w_hit and not a_hit:
                            y = 0.0
                            break
                        if a_hit and w_hit:
                            break
                    rev_b.append((r["rev_chance"] / 100 - y) ** 2)
                    rev_n.append((r["rev_normal"] / 100 - y) ** 2)
            if pos + 10 < len(idx) and isinstance(r["no_new"], str):
                mv = float(c.iloc[pos + 10] / c.iloc[pos] - 1)
                nn_moves.append(-mv if r["no_new"] == "short" else mv)     # return of the trade we said NOT to open
            if pd.notna(r.get("exp_open_pct")) and pos + 1 < len(idx) and "open" in b:
                gap = float(b["open"].iloc[pos + 1] / c.iloc[pos] - 1) * 100
                eo_err.append(gap - float(r["exp_open_pct"]))
        out["markets"][m] = mrow
    sk = lambda a, b_: None if not a else round((1 - sum(a) / sum(b_)) * 100, 1) if sum(b_) > 0 else None
    out["range"] = {"n": len(rng_hits), "hit_pct": None if not rng_hits else round(float(np.mean(rng_hits)) * 100, 1), "target": 68}
    out["big_move"] = {"n": len(big_b), "brier_skill_pct": sk(big_b, big_n)}
    out["reversal"] = {"n": len(rev_b), "brier_skill_pct": sk(rev_b, rev_n)}
    out["no_new"] = {"n": len(nn_moves), "avg_10d_pct": None if not nn_moves else round(float(np.mean(nn_moves)) * 100, 2)}
    out["expected_open"] = {"n": len(eo_err), "mean_abs_err_pct": None if not eo_err else round(float(np.mean(np.abs(eo_err))), 2)}
    # paper size book: equal weight across markets each day
    if book:
        B = pd.DataFrame(book, columns=["d", "m", "r"]).groupby("d")["r"].mean()
        H = pd.DataFrame(bh, columns=["d", "m", "r"]).groupby("d")["r"].mean()
        eq, eqh = (1 + B).cumprod(), (1 + H).cumprod()
        dd = float((eq / eq.cummax() - 1).min())
        cur_dd = float(eq.iloc[-1] / eq.max() - 1)
        streak = 0
        for v in B.values[::-1]:
            if v < 0:
                streak += 1
            else:
                break
        rules = []
        if cur_dd < -0.10:
            rules.append("Paper book is more than 10% below its peak: halve all position sizes until it recovers.")
        if streak >= 5:
            rules.append(f"{streak} losing days in a row: pause new entries for a day and review.")
        out["book"] = {"days": int(len(B)), "ret_pct": round((float(eq.iloc[-1]) - 1) * 100, 2), "bh_ret_pct": round((float(eqh.iloc[-1]) - 1) * 100, 2),
                       "max_dd_pct": round(dd * 100, 2), "bh_max_dd_pct": round(float((eqh / eqh.cummax() - 1).min()) * 100, 2),
                       "cur_dd_pct": round(cur_dd * 100, 2), "losing_streak": streak, "rules": rules,
                       "curve": [[str(i.date()), round(float(v), 4), round(float(eqh.loc[i]), 4)] for i, v in eq.items()]}
    if os.path.exists(F_SP):
        sp = pd.read_csv(F_SP)
        st = sp[sp["settled"].notna()] if "settled" in sp else sp.iloc[0:0]
        out["spreads"] = {"opened": int(len(sp)), "settled": int(len(st)),
                          "avg_pnl_pct": None if st.empty else round(float(st["pnl_pct"].mean()), 3),
                          "win_pct": None if st.empty else round(float((st["pnl_pct"] > 0).mean() * 100), 1),
                          "open": sp[sp["settled"].isna()][["market", "date", "strategy", "legs"]].to_dict("records") if "settled" in sp else sp[["market", "date", "strategy", "legs"]].to_dict("records")}
    if os.path.exists(F_RECS):
        rc = pd.read_csv(F_RECS)
        last = rc[rc["date"] == rc.groupby("market")["date"].transform("max")]
        out["recs"] = {"rows": int(len(rc)), "latest_by_horizon": last.groupby("horizon").size().to_dict()}
    return out
