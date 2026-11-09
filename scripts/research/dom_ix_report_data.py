"""DOM Index - data for the PDF report (2026-11-09, Nikhil: "prepare a pdf explaining the logic ... instrument like future, synthetic
future or 0.6 delta option buying .. previous 5-7 days trades .. pnl comparison between the instruments .. overall conclusion").

For every DOM Index paper trade (Nifty / Bank Nifty, data_store/livedesk/domcards/trades.json; trades flagged late_levels are listed
but kept out of the totals, exactly as Paper Trades does):
  1. index points the signal made (entry and exit are the near-month FUTURES mid seen by the engine)
  2. rupees for 1 lot in three instruments:
       FUTURES             points x lot - futures round-trip cost: STT 0.05 % of the sell side + exchange / SEBI / stamp / GST +
                           Rs 40 brokerage + 1 tick-ish slippage a side  ->  NIFTY ~17 pts, BANKNIFTY ~40 pts (index points per unit)
       SYNTHETIC FUTURE    buy ATM call + sell ATM put (what the paper rule books): points x lot - 4.5 / 12.5 pts (domcards.SYN_PTS)
       0.6-DELTA OPTION    buy the call (long) / put (short) whose delta is 0.6 at entry, first expiry 5+ days away ("next week");
                           priced with Black-Scholes from the index (futures - that day's basis) and the ATM IV of the nearest
                           3-minute option-chain snapshot at entry and at exit (so time decay and IV changes are included);
                           costs Rs 25 + 0.10 % of premium per side (backtester rule) + 0.5 / 1.5 pt spread a side
  3. trailing-stop what-ifs on the real per-second futures path (dhan/orderflow/data/of/<day>/sec_<SYM>.csv), synthetic costs:
       BASE     the live rule (fixed stop, target = next level >= 2R, out after 60 min / 15:20)
       BE       stop moves to entry once the trade is +1R
       TRAIL1R  once +1R, stop trails 1R behind the best price; target kept
       RUNNER   once +1R, trail 1R; NO target; still out after 60 min / 15:20
Writes data_store/research/dom_ix_report/{trades.csv, summary.json}.
Run: python3 scripts/research/dom_ix_report_data.py
"""
from __future__ import annotations

import json
import math
import os
from datetime import date, datetime

import numpy as np
import pandas as pd

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
OUTD = os.path.join(ROOT, "data_store", "research", "dom_ix_report")
OFD = os.path.join(os.environ.get("HOME", ""), "mnt", "Documents", "dhan", "orderflow", "data", "of")
if not os.path.isdir(OFD):
    OFD = os.path.expanduser("~/Documents/dhan/orderflow/data/of")
LOT = {"NIFTY": 65, "BANKNIFTY": 30}
SYN = {"NIFTY": 4.5, "BANKNIFTY": 12.5}
SPREAD = {"NIFTY": 0.5, "BANKNIFTY": 1.5}
STEP = {"NIFTY": 50, "BANKNIFTY": 100}
R = 0.065
N = lambda x: 0.5 * (1 + math.erf(x / math.sqrt(2)))


def fut_cost_pts(k, px):
    lot = LOT[k]; sell = px * lot; buy = px * lot
    rs = 0.0005 * sell + 0.0000173 * (buy + sell) + 0.000001 * (buy + sell) + 0.00002 * buy + 40
    rs += 0.18 * (0.0000173 * (buy + sell) + 40)                              # GST on exchange + brokerage
    return rs / lot + (1.0 if k == "NIFTY" else 3.0) * 2                      # + slippage a side


def bs(S, K, T, v, call):
    d1 = (math.log(S / K) + (R + 0.5 * v * v) * T) / (v * math.sqrt(T)); d2 = d1 - v * math.sqrt(T)
    return S * N(d1) - K * math.exp(-R * T) * N(d2) if call else K * math.exp(-R * T) * N(-d2) - S * N(-d1)


def strike_06(S, T, v, call, step):
    from statistics import NormalDist
    d1 = NormalDist().inv_cdf(0.6) if call else -NormalDist().inv_cdf(0.6)     # call delta 0.6 / put delta -0.6
    K = S * math.exp(-(d1 * v * math.sqrt(T) - (R + 0.5 * v * v) * T))
    return round(K / step) * step


def snap_iv(day, k, hm):
    p = os.path.join(ROOT, "data_store", "flow", "option_chain", f"{day}.csv.gz")
    if not os.path.exists(p):
        return None
    c = SNAPS.get(day)
    if c is None:
        c = pd.read_csv(p); c = c[c.expiry.notna() & (c.iv > 0)]; SNAPS[day] = c
    x = c[c.symbol == k]
    if not len(x):
        return None
    ts = sorted(x.ts.unique()); want = f"{day} {hm}:00"
    t = min(ts, key=lambda z: abs((datetime.fromisoformat(z) - datetime.fromisoformat(want)).total_seconds()))
    y = x[(x.ts == t)]; y = y[y.expiry == sorted(y.expiry.unique())[0]]
    S0 = float(y.underlying.iloc[0]); atm = y.iloc[(y.strike - S0).abs().argsort()[:2]]
    return float(atm.iv.mean()) / 100, S0


SNAPS: dict = {}


def expiry_next(day, k):
    files = sorted(f for f in os.listdir(os.path.join(ROOT, "data_store", "research", "chains")) if f[3:13] <= day)
    c = pd.read_csv(os.path.join(ROOT, "data_store", "research", "chains", files[-1]), usecols=["sym", "expiry"])
    ex = sorted(c[c.sym == k].expiry.unique())
    return next(e for e in ex if (date.fromisoformat(e) - date.fromisoformat(day)).days >= 5)


def path(day, k):
    p = os.path.join(OFD, day, f"sec_{k}.csv")
    d = pd.read_csv(p, usecols=["t", "mid"]).dropna()
    return d[d.mid > 0]


def sim(tr, P, mode):
    sd = 1 if tr["side"] == "long" else -1; e, risk = tr["entry"], tr["risk"]; stop = tr["stop"]; tgt = tr["target"]
    flat = datetime.fromisoformat(f"{tr['day']} 15:20").timestamp()
    x = P[P.t >= tr["t0"]]; best = e
    for t, m in zip(x.t.values, x.mid.values):
        if (m - stop) * sd <= 0:
            return m, t, "stop" if stop == tr["stop"] else ("breakeven" if abs(stop - e) < 1e-6 else "trail stop")
        if mode != "RUNNER" and (m - tgt) * sd >= 0:
            return tgt, t, "target"
        if t - tr["t0"] >= 3600 or t >= flat:
            return m, t, "time"
        best = max(best, m) if sd > 0 else min(best, m)
        if (best - e) * sd >= risk:
            if mode == "BE":
                stop = max(stop, e) if sd > 0 else min(stop, e)
            elif mode in ("TRAIL1R", "RUNNER"):
                ns = best - sd * risk; stop = max(stop, ns) if sd > 0 else min(stop, ns)
    return (x.mid.values[-1], x.t.values[-1], "end") if len(x) else (e, tr["t0"], "no data")


if __name__ == "__main__":
    os.makedirs(OUTD, exist_ok=True)
    T = json.load(open(os.path.join(ROOT, "data_store", "livedesk", "domcards", "trades.json")))
    T = T if isinstance(T, list) else T.get("trades", [])
    import sys
    sys.path.insert(0, os.path.join(ROOT, "app"))
    try:
        import domcards as DC
        T = DC.plan_ix_fill([dict(t) for t in T])
    except Exception as e:
        print("plan fill failed", e)
    T = [t for t in T if t["key"] in LOT and t["status"] == "closed"]
    rows = []; paths = {}
    for tr in T:
        k, day = tr["key"], tr["day"]; lot = LOT[k]; sd = 1 if tr["side"] == "long" else -1
        pts = (tr["exit"] - tr["entry"]) * sd
        lvp = os.path.join(ROOT, "data_store", "livedesk", "domcards", f"levels_{day}.json")
        basis = (json.load(open(lvp)).get(k) or {}).get("basis", 0.0) if os.path.exists(lvp) else 0.0
        r = {"day": day, "key": k, "setup": tr["setup"], "side": tr["side"], "opened": tr["opened"][11:], "closed": str(tr.get("closed", ""))[11:],
             "entry": tr["entry"], "stop": tr["stop"], "target": tr["target"], "exit": tr["exit"], "exit_reason": tr["exit_reason"], "risk_pts": round(tr["risk"], 1),
             "level": tr.get("level"), "why": tr.get("why"), "late_levels": bool(tr.get("late_levels")), "plan": bool(tr.get("plan_ix")), "pts": round(pts, 1), "r_gross": round(pts / tr["risk"], 2)}
        fc = fut_cost_pts(k, tr["entry"])
        r["fut_cost_pts"] = round(fc, 1); r["pnl_fut"] = round((pts - fc) * lot)
        r["pnl_syn"] = round((pts - SYN[k]) * lot)
        try:
            ex = expiry_next(day, k); hm0, hm1 = tr["opened"][11:16], str(tr["closed"])[11:16]
            iv0, _ = snap_iv(day, k, hm0); iv1, _ = snap_iv(day, k, hm1)
            S0, S1 = tr["entry"] - basis, tr["exit"] - basis
            T0 = ((datetime.fromisoformat(ex + " 15:30") - datetime.fromisoformat(tr["opened"])).total_seconds()) / 86400 / 365
            T1 = ((datetime.fromisoformat(ex + " 15:30") - datetime.fromisoformat(f"{day} {hm1}")).total_seconds()) / 86400 / 365
            call = tr["side"] == "long"; K = strike_06(S0, T0, iv0, call, STEP[k])
            p0, p1 = bs(S0, K, T0, iv0, call), bs(S1, K, T1, iv1, call)
            cost = 2 * (25 + 0.001 * 0.5 * (p0 + p1) * lot) + 2 * SPREAD[k] * lot
            r.update(opt_expiry=ex, opt_strike=K, opt_cp="CE" if call else "PE", opt_iv_in=round(iv0 * 100, 1), opt_iv_out=round(iv1 * 100, 1),
                     opt_in=round(p0, 1), opt_out=round(p1, 1), opt_pts=round(p1 - p0, 1), pnl_opt=round((p1 - p0) * lot - cost))
        except Exception as e:
            r["opt_err"] = str(e)[:80]
        try:
            key = (day, k)
            if key not in paths:
                paths[key] = path(day, k)
            P = paths[key]
            for mode in ("BASE", "BE", "TRAIL1R", "RUNNER"):
                px, t_, why = sim(tr, P, mode)
                g = (px - tr["entry"]) * sd
                r[f"pts_{mode}"] = round(g, 1); r[f"why_{mode}"] = why; r[f"pnl_{mode}"] = round((g - SYN[k]) * lot)
        except Exception as e:
            r["sim_err"] = str(e)[:80]
        rows.append(r)
    D = pd.DataFrame(rows).sort_values(["day", "opened"])
    D.to_csv(os.path.join(OUTD, "trades.csv"), index=False)
    U = D[~D.late_levels]
    summ = {"n": int(len(U)), "days": sorted(U.day.unique().tolist()), "late_excluded": int(D.late_levels.sum()),
            "inst": {c: {"total": int(U[c].sum()), "avg": round(float(U[c].mean()), 0), "win": round(float((U[c] > 0).mean() * 100), 0),
                         "by_day": {d: int(v) for d, v in U.groupby("day")[c].sum().items()}} for c in ("pnl_fut", "pnl_syn", "pnl_opt") if c in U},
            "trail": {m: {"total": int(U[f"pnl_{m}"].sum()), "avg_r": round(float((U[f"pts_{m}"] / U.risk_pts).mean()), 2), "win": round(float((U[f"pnl_{m}"] > 0).mean() * 100), 0)}
                      for m in ("BASE", "BE", "TRAIL1R", "RUNNER") if f"pnl_{m}" in U},
            "plan": {c: int(U[U.plan][c].sum()) for c in ("pnl_fut", "pnl_syn", "pnl_opt") if c in U}, "plan_n": int(U.plan.sum()),
            "base_matches_live": int(((U.pts_BASE - U.pts).abs() <= 2).sum()) if "pts_BASE" in U else None}
    json.dump(summ, open(os.path.join(OUTD, "summary.json"), "w"), indent=1)
    pd.set_option("display.width", 250)
    print(D[["day", "key", "side", "setup", "opened", "closed", "exit_reason", "pts", "r_gross", "pnl_fut", "pnl_syn", "pnl_opt", "opt_strike", "opt_in", "opt_out",
             "pts_BASE", "pnl_BE", "pnl_TRAIL1R", "pnl_RUNNER", "plan", "late_levels"]].to_string(index=False))
    print(json.dumps(summ, indent=1))
