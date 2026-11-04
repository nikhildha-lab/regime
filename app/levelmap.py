"""
Rolling level map (2026-10-24). Indications only.

Where do levels pile up for the next session? Built from the last 10 sessions (and a 5-session view), in SPOT prices:
  - weekly / monthly high & low (this week / month so far, previous week / month)   weight 3  (+ alert)
  - previous day high / low 2, close 1
  - composite volume profile (Dhan futures 1-min volume moved to spot with each day's basis):
      10-day POC 2.5, VAH / VAL 1.5, 5-day POC 2, VAH / VAL 1.5, high-volume nodes 1.5
  - 30-min swing highs / lows of the 10 days, newer swings weigh more (1.0 -> 0.5)
  - option strikes from the latest option-chain snapshot: OI share x6 + fresh writing share x2 + volume share x2
Score(price) = sum of weights within +-band (Nifty 15 pts, Bank Nifty 40). Blocks = strongest peaks at least
4 bands apart, block edges where the score falls under 80 % of the peak. Strength by score (tested idea: price level +
option OI together held 77-80 % in 2024-26; levels alone broke 13-19 %).
Files read: data_store/market_data/yahoo_daily_deep.csv, nifty_futures_1m_upstox.csv, data_store/flow/option_chain/*.csv.gz
"""
from __future__ import annotations

import os
import time

import numpy as np
import pandas as pd

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
DEEP = os.path.join(ROOT, "data_store", "market_data", "yahoo_daily_deep.csv")
FUT1M = os.path.join(ROOT, "data_store", "market_data", "nifty_futures_1m_upstox.csv")
OCD = os.path.join(ROOT, "data_store", "flow", "option_chain")
YS = {"NIFTY": "^NSEI", "BANKNIFTY": "^NSEBANK"}
BAND = {"NIFTY": 15.0, "BANKNIFTY": 40.0}
STEP = {"NIFTY": 5.0, "BANKNIFTY": 10.0}
_C: dict = {}


def _daily(key):
    y = pd.read_csv(DEEP, usecols=["date", "symbol", "high", "low", "close"], parse_dates=["date"])
    return y[y.symbol == YS[key]].set_index("date").sort_index().tail(80)


def _fut(key):
    f = pd.read_csv(FUT1M, usecols=["contract", "ts", "high", "low", "close", "volume"])
    f = f[f.contract.str.startswith(key + "_FUT")]
    f["d"] = f.ts.str[:10]
    vol = f.groupby(["d", "contract"]).volume.sum().reset_index()
    near = vol.loc[vol.groupby("d").volume.idxmax()][["d", "contract"]]
    f = f.merge(near, on=["d", "contract"])
    f["t"] = f.ts.str[11:16]
    return f[(f.t >= "09:15") & (f.t <= "15:30")]


def _oc(key, asof=None):
    try:
        last = sorted(x for x in os.listdir(OCD) if x.endswith(".csv.gz") and (asof is None or x[:10] <= asof))[-1]
    except Exception:
        return None, None
    o = pd.read_csv(os.path.join(OCD, last), usecols=["ts", "symbol", "expiry", "strike", "side", "oi", "chg_oi", "volume", "underlying"])
    o = o[(o.symbol == key) & o.expiry.notna() & (o.oi > 0)]
    if o.empty:
        return None, None
    o = o[o.ts == o.ts.max()]
    o = o[o.expiry.isin(sorted(o.expiry.unique())[:2])]
    return o, last[:10]


def _profile(rows, step):
    tp = ((rows.high + rows.low + rows.close) / 3 - rows.basis) / step
    v = rows.volume.groupby((tp.round() * step).values).sum().sort_index()
    if v.empty:
        return None
    sm = v.rolling(5, center=True, min_periods=1).mean()
    poc = float(sm.idxmax())
    order = v.sort_values(ascending=False)
    vaz = order[(order.cumsum() / order.sum()) <= 0.7].index
    nodes = [float(p) for i, p in enumerate(sm.index[3:-3], 3) if sm.iloc[i] == sm.iloc[i - 3:i + 4].max() and sm.iloc[i] > sm.mean() * 1.3]
    return {"poc": poc, "vah": float(max(vaz)) if len(vaz) else poc, "val": float(min(vaz)) if len(vaz) else poc, "nodes": nodes}


def build(key: str = "NIFTY", days: int = 10, asof: str | None = None) -> dict:
    """asof = 'YYYY-MM-DD': the map as it looked that evening (for checking past days)."""
    d = _daily(key)
    if asof:
        d = d[d.index <= asof]
    spot = float(d.close.iloc[-1]); last_day = d.index[-1]
    items = []                                   # (price, weight, name, kind)
    # weekly / monthly highs and lows (alerts)
    wk = d.index.to_period("W"); mo = d.index.to_period("M")
    nxt = last_day + pd.offsets.BDay(1)                      # labels are for the NEXT session
    cw, cm = nxt.to_period("W"), nxt.to_period("M")
    for lab, mask in (("this week", wk == cw), ("last week", wk == cw - 1), ("this month", mo == cm), ("last month", mo == cm - 1)):
        z = d[mask]
        if len(z):
            items += [(float(z.high.max()), 3.0, f"{lab} high", "alert"), (float(z.low.min()), 3.0, f"{lab} low", "alert")]
    items += [(float(d.high.iloc[-1]), 2.0, "prev day high", "price"), (float(d.low.iloc[-1]), 2.0, "prev day low", "price"),
              (float(d.close.iloc[-1]), 1.0, "prev day close", "price")]
    # 2026-11-04 (Nikhil): reference levels drawn as dashed lines on the block chart (spot terms, for the next session)
    ref = [("PDH", "previous day high", float(d.high.iloc[-1])), ("PDL", "previous day low", float(d.low.iloc[-1]))]
    for lab, mask, sh in (("previous week", wk == cw - 1, "PW"), ("previous month", mo == cm - 1, "PM")):
        z = d[mask]
        if len(z):
            ref += [(sh + "H", lab + " high", float(z.high.max())), (sh + "L", lab + " low", float(z.low.min()))]
    # intraday futures -> spot
    try:
        f = _fut(key)
        if asof:
            f = f[f.d <= asof]
        sess = sorted(f.d.unique())[-days:]
        f = f[f.d.isin(sess)].copy()
        lastc = f.groupby("d").close.last()
        sc = d.close.copy(); sc.index = sc.index.strftime("%Y-%m-%d")
        basis = (lastc - sc.reindex(lastc.index)).fillna(0.0)
        f["basis"] = f.d.map(basis)
        for n, lab, w in ((days, f"{days}-day", 2.5), (5, "5-day", 2.0)):
            pr = _profile(f[f.d.isin(sess[-n:])], STEP[key])
            if pr:
                items += [(pr["poc"], w, f"{lab} POC", "price"), (pr["vah"], 1.5, f"{lab} VAH", "price"), (pr["val"], 1.5, f"{lab} VAL", "price")]
                if n == 5:
                    ref += [("VAH", "5-day value area high (rolling volume profile)", pr["vah"]), ("VAL", "5-day value area low (rolling volume profile)", pr["val"]),
                            ("POC", "5-day point of control (price with the most volume)", pr["poc"])]
                if n == days:
                    items += [(p, 1.5, f"{lab} volume node", "price") for p in pr["nodes"] if abs(p - pr["poc"]) > 3 * BAND[key]]
        zs = f[f.d == sess[-1]]                                # 2026-11-04: VWAP of the last session (futures - basis)
        if len(zs) and zs.volume.sum() > 0:
            vw = float(((zs.high + zs.low + zs.close) / 3 * zs.volume).sum() / zs.volume.sum()) - float(basis.get(sess[-1], 0))
            ref.append(("VWAP", f"VWAP of the {sess[-1][5:]} session", vw))
        z = f[(f.d == sess[-1]) & (f.t < "10:15")]          # initial balance of the last session (first hour, futures - basis)
        if len(z) >= 30:
            bb = float(basis.get(sess[-1], 0))
            ref += [("IBH", f"initial balance high {sess[-1][5:]} (09:15-10:15)", float(z.high.max()) - bb),
                    ("IBL", f"initial balance low {sess[-1][5:]} (09:15-10:15)", float(z.low.min()) - bb)]
        # 30-min swings, newer weigh more
        for i, s in enumerate(sess):
            z = f[f.d == s].reset_index(drop=True)
            if len(z) < 60:
                continue
            g = z.groupby(z.index // 30).agg(h=("high", "max"), l=("low", "min")); b = float(basis.get(s, 0))
            w = 0.5 + 0.5 * (i + 1) / len(sess)
            for j in range(1, len(g) - 1):
                if g.h[j] >= g.h[j - 1] and g.h[j] >= g.h[j + 1]:
                    items.append((float(g.h[j]) - b, w, f"{s[5:]} swing high", "price"))
                if g.l[j] <= g.l[j - 1] and g.l[j] <= g.l[j + 1]:
                    items.append((float(g.l[j]) - b, w, f"{s[5:]} swing low", "price"))
    except Exception as e:
        items.append((spot, 0.0, f"intraday history missing: {e}"[:60], "price"))
    # option strikes
    o, oc_day = _oc(key, asof)
    if o is not None:
        u = float(o.underlying.iloc[-1]) or spot
        a = o.groupby(["strike", "side"]).agg(oi=("oi", "sum"), chg=("chg_oi", "sum"), vol=("volume", "sum")).reset_index()
        for side in ("PE", "CE"):
            z = a[(a.side == side) & ((a.strike / u - 1).abs() < 0.03)]
            if z.empty:
                continue
            pos = z.chg.clip(lower=0).sum() or 1
            for r in z.itertuples():
                w = 6 * r.oi / z.oi.sum() + (2 * r.chg / pos if r.chg > 0 else 0) + 2 * r.vol / (z.vol.sum() or 1)
                if w > 0.25:
                    items.append((float(r.strike), round(w, 2), f"{'put' if side == 'PE' else 'call'} OI {int(r.strike)}", "oi"))
    # density -> blocks
    bd, st = BAND[key], STEP[key]
    lo_g, hi_g = spot * 0.96, spot * 1.04
    items = [x for x in items if lo_g <= x[0] <= hi_g and x[1] > 0]
    grid = np.arange(round(lo_g / st) * st, hi_g, st)
    P = np.array([x[0] for x in items]); Wt = np.array([x[1] for x in items])
    score = np.array([Wt[np.abs(P - g) <= bd].sum() for g in grid])
    peaks = []
    for i in np.argsort(-score):
        if score[i] < 2.5 or len(peaks) >= 12:
            break
        if all(abs(grid[i] - grid[j]) >= 4 * bd for j in peaks):
            peaks.append(i)
    blocks = []
    for i in peaks:
        pk = score[i]; lo = hi = i
        while lo > 0 and score[lo - 1] >= 0.8 * pk: lo -= 1
        while hi < len(grid) - 1 and score[hi + 1] >= 0.8 * pk: hi += 1
        lo_p, hi_p = float(grid[lo]) - bd / 2, float(grid[hi]) + bd / 2
        inside = [x for x in items if lo_p - bd <= x[0] <= hi_p + bd]
        kinds = {x[3] for x in inside}
        strength = "strong" if pk >= 8 and ("oi" in kinds and kinds & {"price", "alert"}) else "medium" if pk >= 5 else "weak"
        parts = sorted(inside, key=lambda x: -x[1])
        blocks.append({"lo": round(lo_p, 1), "hi": round(hi_p, 1), "mid": round(float(grid[i]), 1), "score": round(float(pk), 1),
                       "strength": strength, "alerts": [x[2] for x in parts if x[3] == "alert"],
                       "parts": [x[2] for x in parts][:10], "side": "resistance" if grid[i] > spot else "support"})
    blocks.sort(key=lambda b: b["mid"])
    levels = [{"short": a, "name": n, "px": round(v, 1), "dist_pct": round(100 * (v / spot - 1), 2)} for a, n, v in ref if v]
    return {"key": key, "spot": round(spot, 1), "as_of": str(last_day.date()), "days": days, "oc_snapshot": oc_day, "blocks": blocks, "levels": levels,
            "note": "Score = levels piling up within the band. Weekly / monthly highs and lows are flagged as alerts."}


def get(key: str = "NIFTY", days: int = 10) -> dict:
    c = _C.get((key, days))
    if c and time.time() - c[0] < 600:
        return c[1]
    try:
        v = build(key, days)
    except Exception as e:
        v = c[1] if c else {"key": key, "error": str(e)[:200], "blocks": []}
    _C[(key, days)] = (time.time(), v)
    return v
