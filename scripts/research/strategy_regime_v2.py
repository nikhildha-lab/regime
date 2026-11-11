"""Strategy x regime matrix on the NEW two-axis regime (2026-11-11, Nikhil: "research tab need to be updated with new regime
classification and update the page").

Same trades, same units, same tests as strategy_regime_3y.py (MASTER_BACKTESTER trade logs from 2023-11-01, each trade in
units of its own leg's P&L spread, cells set on Nov 2023 - Apr 2025 and judged on May 2025 onwards). Only the regime changes:
the Regime Board's two readings instead of the old five states.

  Trend      - Bullish / Sideways / Bearish: efficiency ratio(20) > 0.30 and ADX(14) > 22.5, with the 20-day return and
               price vs the 200-day EMA agreeing on the direction. Otherwise Sideways.
  Volatility - Very Volatile / Volatile / Normal / Calm: 20-day realised volatility's percentile in its own last 252 bars,
               80+ / 60-80 / 30-60 / under 30.

Exactly the formulas in app/regimeboard.py full_regime(). Every trade gets the labels of its market's PREVIOUS daily close
(no look-ahead). One timeframe for every strategy - the daily board - because that is the regime the page shows. (The old
matrix judged intraday strategies on a 15-min regime and positional ones on a weekly regime; the board has no such labels.)

Cells (per strategy, per trend label and per volatility label) use the same rule as before: Trade / half size / quarter size /
Don't. "Follow regime" = each trade sized by the SMALLER of its trend cell and its volatility cell, with both sets of cells
picked on the first half only and applied to the second half - the honest test. Profit factor shown before and after costs.

Run on the Mac:  cd ~/Documents/synaptic-main/Backtrader_App && python3 scripts/research/strategy_regime_v2.py
Writes data_store/research/strategy_regime_3y/matrix_v2.json and FINDINGS_regime_v2_matrix.md. Research only - no orders.
"""
import json
import math
import os
import sys
from datetime import datetime

import numpy as np
import pandas as pd

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "scripts", "research")); sys.path.insert(0, ROOT)
import strategy_regime_3y as M3          # load(), cell(), luck(), ts(), GROUPS - its __main__ blocks do not run on import
import strategy_regime_study as S0       # S0.MKT: daily bars per market

OUT = M3.OUT
TRENDS = ["Up", "Flat", "Down"]
VOLS = ["Calm", "Normal", "Busy", "Hot"]
T_NAME = {"Up": "Bullish", "Flat": "Sideways", "Down": "Bearish"}
V_NAME = {"Calm": "Calm", "Normal": "Normal", "Busy": "Volatile", "Hot": "Very Volatile"}
ER_LINE, ADX_LINE, HV_LINE, COMP_VOL = 0.30, 22.5, 80, 30
WT = {"works": 1.0, "plus": 0.5, "few": 0.25}


def _adx(h, l, c, n=14):
    up, dn = h.diff(), -l.diff()
    pdm = up.where((up > dn) & (up > 0), 0.0)
    ndm = dn.where((dn > up) & (dn > 0), 0.0)
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / n, adjust=False).mean()
    pdi = 100 * pdm.ewm(alpha=1 / n, adjust=False).mean() / atr.replace(0, np.nan)
    ndi = 100 * ndm.ewm(alpha=1 / n, adjust=False).mean() / atr.replace(0, np.nan)
    dx = 100 * (pdi - ndi).abs() / (pdi + ndi).replace(0, np.nan)
    return dx.ewm(alpha=1 / n, adjust=False).mean()


def labels(m):
    """Trend and volatility label for every daily bar of market m, known from the next day (previous close rule)."""
    b = S0.MKT[m][0]
    c = pd.to_numeric(b["close"], errors="coerce").dropna()
    h = pd.to_numeric(b["high"], errors="coerce").reindex(c.index).fillna(c) if "high" in b else c
    l = pd.to_numeric(b["low"], errors="coerce").reindex(c.index).fillna(c) if "low" in b else c
    lr = np.log(c / c.shift())
    vol20 = lr.rolling(20).std() * math.sqrt(252)
    vol_pct = vol20.rolling(252, min_periods=120).apply(lambda a: (a <= a[-1]).mean() * 100, raw=True)
    er = (c - c.shift(20)).abs() / c.diff().abs().rolling(20).sum().replace(0, np.nan)
    adx = _adx(h, l, c)
    ret20 = c / c.shift(20) - 1
    ema = c / c.ewm(span=200, adjust=False).mean() - 1
    on = (er > ER_LINE) & (adx > ADX_LINE)
    trend = pd.Series("Flat", index=c.index).mask(on & (ret20 > 0) & (ema > 0), "Up").mask(on & (ret20 < 0) & (ema < 0), "Down")
    vol = pd.Series(np.select([vol_pct >= HV_LINE, vol_pct >= 60, vol_pct >= COMP_VOL], ["Hot", "Busy", "Normal"], "Calm"), index=c.index)
    ok = vol_pct.notna() & adx.notna()
    idx = pd.to_datetime(c.index)
    if getattr(idx, "tz", None) is not None:
        idx = idx.tz_localize(None)
    out = pd.DataFrame({"known_at": idx.normalize() + pd.Timedelta(days=1), "trend": trend.values, "vol": vol.values})[ok.values]
    return out.sort_values("known_at")


def tag2(T):
    parts = []
    for m, g in T.groupby("market"):
        if m not in S0.MKT:
            continue
        L = labels(m)
        g = g.sort_values("entry")
        g = pd.merge_asof(g, L, left_on="entry", right_on="known_at", direction="backward",
                          tolerance=pd.Timedelta(days=5)).drop(columns="known_at")
        parts.append(g)
    T = pd.concat(parts, ignore_index=True)
    return T.dropna(subset=["trend", "vol"])


def pf(x):
    x = np.asarray(x, dtype=float)
    gw, gl = x[x > 0].sum(), -x[x < 0].sum()
    return None if gw == 0 and gl == 0 else (99.0 if gl == 0 else round(float(gw / gl), 2))


def oos_weights(g, key, keys):
    """Cells picked on the first half only, applied to the second half: one size per second-half trade."""
    h1, h2 = g[g.half == "H1"], g[g.half == "H2"]
    c1 = {k: M3.cell(h1[h1[key] == k]) for k in keys}
    return h2[key].map(lambda r: WT.get(c1[r]["status"], 0.0)).values


def verdict(a, b):
    if a is None or b is None:
        return "na"
    return "helps" if a > b * 1.05 else "hurts" if a < b * 0.95 else "neutral"


def matrices2(T):
    out = {}
    for grp, mk in M3.GROUPS.items():
        G = T[T.market.isin(mk)]
        rows, removed = [], []
        for s, g in G.groupby("strategy"):
            ct = {k: M3.cell(g[g.trend == k]) for k in TRENDS}
            cv = {k: M3.cell(g[g.vol == k]) for k in VOLS}
            judged = [c for c in list(ct.values()) + list(cv.values()) if c["status"] != "few"]
            if g.u_gross.mean() < 0 or (judged and all((c["avg"] or 0) <= 0 for c in judged)):
                removed.append(dict(strategy=s, all=round(g.u_gross.mean(), 3), n=len(g)))
                continue
            h2 = g[g.half == "H2"]
            wt, wv = oos_weights(g, "trend", TRENDS), oos_weights(g, "vol", VOLS)
            wb = np.minimum(wt, wv)
            ug, un = h2.u_gross.values, h2.u_net.values
            cmp = dict(pf_n=len(h2), pf_flat=pf(ug), pf_flat_net=pf(un),
                       pf_trend=pf(ug * wt), pf_trend_net=pf(un * wt), taken_trend=int((wt > 0).sum()),
                       pf_vol=pf(ug * wv), pf_vol_net=pf(un * wv), taken_vol=int((wv > 0).sum()),
                       pf_reg=pf(ug * wb), pf_reg_net=pf(un * wb), pf_taken=int((wb > 0).sum()),
                       has_costs=bool((ug != un).any()))
            cmp.update(v_trend=verdict(cmp["pf_trend"], cmp["pf_flat"]), v_vol=verdict(cmp["pf_vol"], cmp["pf_flat"]),
                       v_reg=verdict(cmp["pf_reg"], cmp["pf_flat"]))
            lt = M3.luck(g.assign(regime=g.trend), 300) if len(g) >= 40 else None
            lv = M3.luck(g.assign(regime=g.vol), 300) if len(g) >= 40 else None
            good_t = [T_NAME[k] for k in TRENDS if ct[k]["status"] in ("works", "plus")]
            good_v = [V_NAME[k] for k in VOLS if cv[k]["status"] in ("works", "plus")]
            bad = [T_NAME[k] for k in TRENDS if ct[k]["status"] in ("avoid", "minus")] + [V_NAME[k] for k in VOLS if cv[k]["status"] in ("avoid", "minus")]
            if cmp["v_reg"] != "helps":
                plan = "Regime did not help in the out-of-sample test - trade it at normal size whatever the regime"
            else:
                plan = "Follow the regime: best in " + (", ".join(good_t) or "any trend") + " · " + (", ".join(good_v) or "any volatility") + \
                       ("; switch off in " + ", ".join(bad) if bad else "")
            fin = lambda v: None if v is None or not np.isfinite(v) else round(float(v), 3)
            rows.append(dict(strategy=s, family=g.family.iloc[0], markets=",".join(sorted(g.market.unique())), n=len(g),
                             style=g["style"].mode().iloc[0], tf="daily", all=round(g.u_gross.mean(), 2), all_net=round(g.u_net.mean(), 2),
                             cells_trend=ct, cells_vol=cv, cmp=cmp, luck_trend=fin(lt), luck_vol=fin(lv), plan=plan))
        try:
            L = pd.read_csv(os.path.join(OUT, "sizing_lab.csv"))
            okset = set(L[(L.group == grp) & (L.rule == "R6 iv_level") & L.helps].strategy) if grp == "crypto" else set()
        except Exception:
            okset = set()
        for r in rows:
            r["sizing"] = "dvol" if r["strategy"] in okset else "flat"
        rows.sort(key=lambda r: -(r["all"] * np.sqrt(r["n"])))
        mix_t = G.trend.value_counts(normalize=True).round(3).to_dict() if len(G) else {}
        mix_v = G.vol.value_counts(normalize=True).round(3).to_dict() if len(G) else {}
        out[grp] = dict(markets=mk, period=f"{M3.START.date()} .. {str(G.entry.max().date()) if len(G) else ''}", trades=len(G),
                        rows=rows, removed=removed, mix_trend=mix_t, mix_vol=mix_v)
    meta = dict(engine="v2", built=datetime.now().isoformat(timespec="minutes"),
                trend_names=T_NAME, vol_names=V_NAME, rule="daily Regime Board labels at the previous close; cells set on Nov 2023 - Apr 2025, judged May 2025 onwards")

    def clean(o):
        if isinstance(o, dict):
            return {k: clean(v) for k, v in o.items()}
        if isinstance(o, list):
            return [clean(v) for v in o]
        if isinstance(o, (np.floating,)):
            o = float(o)
        if isinstance(o, (np.integer,)):
            return int(o)
        if isinstance(o, (np.bool_,)):
            return bool(o)
        if isinstance(o, float) and not np.isfinite(o):
            return None
        return o
    res = clean({"_meta": meta, **out})
    json.dump(res, open(os.path.join(OUT, "matrix_v2.json"), "w"), default=str, allow_nan=False)
    return res


def findings(res):
    L = [f"# FINDINGS - Strategy x Regime Matrix on the Two-Axis Regime ({res['_meta']['built'][:10]})", "",
         "Regime: the Regime Board's Trend (Bullish / Sideways / Bearish) and Volatility (Calm / Normal / Volatile / Very Volatile)",
         "at the previous daily close. Same trades and tests as the old five-state matrix (strategy_regime_3y.py). Research only.", "",
         "MASTER_BACKTESTER checks: no look-ahead (labels from the previous close); split = cells set on Nov 2023 - Apr 2025 (~55 %),",
         "judged on May 2025 onwards (~45 %, not exactly 70/30 - kept identical to the old matrix so the two compare); year-by-year,",
         "2x / 3x costs and best-three-removed are NOT run here - this is a regime filter study, not a new strategy. A strategy that",
         "moves to live trading on the back of it still needs those.", ""]
    for grp, d in res.items():
        if grp == "_meta" or not d.get("rows"):
            continue
        R = d["rows"]
        cnt = lambda k, v: sum(1 for r in R if r["cmp"].get(k) == v)
        L += [f"## {grp.title()} - {d['trades']} trades, {len(R)} strategies kept, {len(d['removed'])} removed (lose in every regime)", "",
              f"- Trend filter alone: helps {cnt('v_trend','helps')}, hurts {cnt('v_trend','hurts')}, no difference {cnt('v_trend','neutral')}",
              f"- Volatility filter alone: helps {cnt('v_vol','helps')}, hurts {cnt('v_vol','hurts')}, no difference {cnt('v_vol','neutral')}",
              f"- Both (smaller of the two sizes): helps {cnt('v_reg','helps')}, hurts {cnt('v_reg','hurts')}, no difference {cnt('v_reg','neutral')}",
              f"- Time spent: trend {d.get('mix_trend')} · volatility {d.get('mix_vol')}", ""]
        top = [r for r in R if r["cmp"].get("v_reg") == "helps" and (r["cmp"].get("pf_reg") or 0) >= 1.2][:10]
        if top:
            L += ["| Strategy | Trades | PF Always | PF Follow Regime | Plan |", "|---|---|---|---|---|"]
            L += [f"| {r['strategy']} | {r['n']} | {r['cmp']['pf_flat']} | {r['cmp']['pf_reg']} | {r['plan']} |" for r in top]
            L += [""]
    L += ["Verdict rule: 'helps' = second-half profit factor with the regime sizing at least 5 % above always trading.",
          "Small samples per cell are common (12 cells instead of 5); a cell under 15 trades is quarter size, not a verdict."]
    open(os.path.join(OUT, "FINDINGS_regime_v2_matrix.md"), "w").write("\n".join(L) + "\n")


if __name__ == "__main__":
    T = tag2(M3.load())
    res = matrices2(T)
    findings(res)
    for grp, d in res.items():
        if grp != "_meta":
            print(grp, d["trades"], "trades", len(d["rows"]), "strategies", len(d["removed"]), "removed")
    print("wrote", os.path.join(OUT, "matrix_v2.json"))
