"""BACKTEST: INDEX CALENDAR ARBITRAGE WITH SYNTHETIC FUTURES (2026-11-11, Nikhil pasted a full rule: synthetic future SF = K + C - P at the
ATM strike, near vs next monthly expiry; fair spread = SF_near x (e^((r - q) dt) - 1); trade when |actual - fair| > costs + buffer; exit when
the excess falls below 20 % of entry, stop if it widens, close 1-2 days before the near expiry). Research only - never orders.

What data exists (no bid / ask history anywhere):
  A. data_store/research/fno_hist/fut_<day>.csv.gz - daily index futures, every monthly expiry, Jul 2024 - Nov 2026. By put-call parity the
     ATM synthetic tracks the future of the same expiry, so the futures calendar spread is the same quantity the rule trades. The NSE futures
     close is the volume-weighted price of the last 30 minutes, so near and far closes are close to simultaneous.
  B. data_store/flow/option_chain/<day>.csv.gz - 3-minute option-chain snapshots (LTP, no bid / ask), Nifty and Bank Nifty, about 20 sessions
     since Oct 2026 - the real synthetic (K + C - P), near and next monthly.
Costs (8 option orders: brokerage, STT, exchange, GST, stamp and slippage) in index points, per the rule: NIFTY 15, BANKNIFTY 40,
FINNIFTY 20, MIDCPNIFTY 12. Stress 2x / 3x.
Fair carry: (1) a dated schedule of the 91-day T-bill minus a 1.2 % dividend yield (approximate - see FINDINGS), and (2) model-free:
the median implied carry of the previous 20 sessions (no look-ahead).
"""
from __future__ import annotations

import json
import math
import os
import time

import numpy as np
import pandas as pd

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
HIST = os.path.join(ROOT, "data_store", "research", "fno_hist")
OC = os.path.join(ROOT, "data_store", "flow", "option_chain")
OUT = os.path.join(ROOT, "data_store", "research", "calarb_bt")
IDX = ["NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY"]
COST = {"NIFTY": 15.0, "BANKNIFTY": 40.0, "FINNIFTY": 20.0, "MIDCPNIFTY": 12.0}
Q = 0.012
# approximate 91-day T-bill (annual) by month start - a schedule, see FINDINGS for the caveat
TBILL = [("2024-01", 0.0690), ("2024-07", 0.0680), ("2024-10", 0.0650), ("2025-01", 0.0655), ("2025-03", 0.0640), ("2025-05", 0.0590),
         ("2025-07", 0.0545), ("2025-10", 0.0550), ("2026-01", 0.0545), ("2026-07", 0.0540)]


def r_at(day):
    r = TBILL[0][1]
    for m, v in TBILL:
        if day[:7] >= m:
            r = v
    return r


def daily_pairs():
    """One row per index per day: near / next monthly future close, OI, volume, days between expiries."""
    fs = sorted(f for f in os.listdir(HIST) if f.startswith("fut_") and f.endswith(".csv.gz"))
    rows = []
    for f in fs:
        d = pd.read_csv(os.path.join(HIST, f), usecols=["day", "sym", "kind", "expiry", "close", "oi", "vol", "und"])
        d = d[(d.kind == "IDF") & d.sym.isin(IDX) & (d.close > 0)]
        for sym, g in d.groupby("sym"):
            g = g[g.expiry >= g.day.iloc[0]].sort_values("expiry")
            if len(g) < 2:
                continue
            n, x = g.iloc[0], g.iloc[1]
            rows.append({"day": str(n.day), "sym": sym, "near_exp": str(n.expiry), "far_exp": str(x.expiry), "near": float(n.close), "far": float(x.close),
                         "near_oi": float(n.oi), "far_oi": float(x.oi), "far_vol": float(x.vol), "und": float(n.und)})
    P = pd.DataFrame(rows).sort_values(["sym", "day"]).reset_index(drop=True)
    P["dt"] = (pd.to_datetime(P.far_exp) - pd.to_datetime(P.near_exp)).dt.days / 365.0
    P["dte"] = (pd.to_datetime(P.near_exp) - pd.to_datetime(P.day)).dt.days
    P["spread"] = P.far - P.near
    P["carry_impl"] = np.log(P.far / P.near) / P.dt                    # implied (r - q), annual
    P["fair_sched"] = P.near * (np.exp((P.day.map(r_at) - Q) * P.dt) - 1)
    med = P.groupby("sym").carry_impl.transform(lambda s: s.shift(1).rolling(20, min_periods=10).median())
    P["fair_roll"] = P.near * (np.exp(med * P.dt) - 1)
    P["liq"] = (P.far_oi >= 0.05 * P.near_oi) & (P.far_vol > 0)
    return P


def simulate(P, fair_col, entry_lag, buffer_mult, stop_mult=1.0, cost_mult=1.0, exit_dte=2):
    """Daily simulation, one position per index. Signal at day t close; entry at the same close (lag 0) or the next close (lag 1).
    Position 'short spread' (short far, long near) when excess > costs + buffer, mirrored when excess < -(costs + buffer).
    P&L (points) = dir x (spread_entry - spread_exit) - costs. Exit: |excess| < 20 % of entry excess (target), excess widened by
    stop_mult x |entry excess| (stop), or near expiry within exit_dte days (time)."""
    trades = []
    for sym, g in P.groupby("sym"):
        g = g.reset_index(drop=True)
        c = COST[sym] * cost_mult; pos = None; pend = None
        for t in range(len(g)):
            r = g.iloc[t]
            ex = r.spread - r[fair_col] if np.isfinite(r[fair_col]) else np.nan
            if pos is not None:
                same_pair = r.near_exp == pos["near_exp"]
                why = None
                if not same_pair or r.dte <= exit_dte:
                    why = "time"
                elif np.isfinite(ex) and abs(ex) < 0.2 * abs(pos["ex"]) or (np.isfinite(ex) and np.sign(ex) != np.sign(pos["ex"])):
                    why = "target"
                elif np.isfinite(ex) and abs(ex) - abs(pos["ex"]) >= stop_mult * abs(pos["ex"]) and np.sign(ex) == np.sign(pos["ex"]):
                    why = "stop"
                if why:
                    sp = r.spread if same_pair else pos["last_sp"]
                    pnl = pos["dir"] * (pos["sp"] - sp) - c
                    trades.append({"sym": sym, "entry": pos["day"], "exit": r.day if same_pair else pos["last_day"], "dir": pos["dir"], "ex_entry": pos["ex"],
                                   "ex_exit": ex if same_pair else np.nan, "pnl_pts": pnl, "gross_pts": pnl + c, "cost_pts": c, "why": why,
                                   "pnl_pct": pnl / r.near * 100})
                    pos = None
                else:
                    pos["last_sp"] = r.spread; pos["last_day"] = r.day
            if pend is not None and pos is None:
                if r.near_exp == pend["near_exp"] and r.dte > exit_dte and r.liq:
                    pos = dict(pend, sp=r.spread, day=r.day, last_sp=r.spread, last_day=r.day)
                pend = None
            if pos is None and np.isfinite(ex) and r.liq and r.dte > exit_dte + 1:
                if abs(ex) > c + buffer_mult * COST[sym]:
                    sig = {"dir": 1 if ex > 0 else -1, "ex": ex, "near_exp": r.near_exp}
                    if entry_lag == 0:
                        pos = dict(sig, sp=r.spread, day=r.day, last_sp=r.spread, last_day=r.day)
                    else:
                        pend = sig
    return pd.DataFrame(trades)


def stats(T, cut):
    if T.empty:
        return {"n": 0}
    def one(x):
        if x.empty:
            return {"n": 0}
        w, l = x.pnl_pts[x.pnl_pts > 0].sum(), -x.pnl_pts[x.pnl_pts < 0].sum()
        return {"n": int(len(x)), "win": round(float((x.pnl_pts > 0).mean() * 100)), "avg_pts": round(float(x.pnl_pts.mean()), 1),
                "avg_gross": round(float(x.gross_pts.mean()), 1), "pf": round(float(w / l), 2) if l > 0 else None, "sum_pts": round(float(x.pnl_pts.sum()))}
    s = one(T); s["train"] = one(T[T.entry < cut]); s["test"] = one(T[T.entry >= cut])
    s["years"] = {y: one(g) for y, g in T.groupby(T.entry.str[:4])}
    s["minus_best3"] = one(T.sort_values("pnl_pts").iloc[:-3]) if len(T) > 3 else None
    s["exits"] = T.why.value_counts().to_dict()
    return s


def excess_profile(P):
    """How big the excess usually is, per index (all days, liquid pairs only)."""
    out = {}
    for sym, g in P[P.liq].groupby("sym"):
        for fc in ("fair_sched", "fair_roll"):
            e = (g.spread - g[fc]).dropna()
            if len(e) < 20:
                continue
            out[f"{sym}|{fc}"] = {"days": int(len(e)), "median_abs": round(float(e.abs().median()), 1), "p90_abs": round(float(e.abs().quantile(0.9)), 1),
                                  "share_beyond_cost": round(float((e.abs() > COST[sym]).mean() * 100), 1), "mean": round(float(e.mean()), 1),
                                  "carry_impl_median": round(float(g.carry_impl.median() * 100), 2)}
    return out


def snapshots():
    """Intraday check on the real synthetic (LTP, 3-min): ATM K + C - P for the near and next monthly expiry."""
    rows = []
    for f in sorted(os.listdir(OC)):
        if not f.endswith(".csv.gz"):
            continue
        d = pd.read_csv(os.path.join(OC, f), usecols=["ts", "symbol", "expiry", "strike", "side", "ltp", "oi", "underlying"])
        d = d[d.ltp > 0]
        for (ts, sym), g in d.groupby(["ts", "symbol"]):
            u = float(g.underlying.iloc[0]); day = str(ts)[:10]
            exps = sorted(e for e in g.expiry.dropna().unique() if e >= day)
            # monthly = the last expiry of its calendar month among those listed
            mon = sorted({max(e for e in exps if e[:7] == m) for m in {e[:7] for e in exps}})
            if len(mon) < 2:
                continue
            ne, fe = mon[0], mon[1]
            a = g[g.expiry == ne].pivot_table(index="strike", columns="side", values="ltp", aggfunc="last")
            b = g[g.expiry == fe].pivot_table(index="strike", columns="side", values="ltp", aggfunc="last")
            ks = sorted(set(a.dropna().index) & set(b.dropna().index), key=lambda k: abs(k - u))
            if not ks or "CE" not in a or "PE" not in a or "CE" not in b or "PE" not in b:
                continue
            k = ks[0]
            sfn = k + a.loc[k, "CE"] - a.loc[k, "PE"]; sff = k + b.loc[k, "CE"] - b.loc[k, "PE"]
            dt = (pd.Timestamp(fe) - pd.Timestamp(ne)).days / 365.0
            fair = sfn * (math.exp((r_at(day) - Q) * dt) - 1)
            rows.append({"ts": str(ts), "day": day, "sym": sym, "near_exp": ne, "far_exp": fe, "k": k, "sf_near": sfn, "sf_far": sff,
                         "spread": sff - sfn, "fair": fair, "excess": sff - sfn - fair, "und": u})
    S = pd.DataFrame(rows)
    if S.empty:
        return {"rows": 0}
    out = {"rows": int(len(S)), "days": int(S.day.nunique())}
    for sym, g in S.groupby("sym"):
        e = g.excess
        d1 = g.groupby("day").excess.agg(["min", "max", "last"])
        out[sym] = {"snaps": int(len(g)), "median_abs_excess": round(float(e.abs().median()), 1), "p90_abs": round(float(e.abs().quantile(0.9)), 1),
                    "share_beyond_cost": round(float((e.abs() > COST[sym]).mean() * 100), 1),
                    "median_intraday_range": round(float((d1["max"] - d1["min"]).median()), 1),
                    "snap_to_snap_sd": round(float(g.sort_values("ts").groupby("day").excess.diff().std()), 1)}
    S.to_csv(os.path.join(OUT, "snapshots_sf.csv.gz"), index=False)
    return out


def run():
    t0 = time.time()
    os.makedirs(OUT, exist_ok=True)
    P = daily_pairs()
    P.to_csv(os.path.join(OUT, "daily_pairs.csv.gz"), index=False)
    days = sorted(P.day.unique()); cut = days[int(len(days) * 0.7)]
    res = {"built": time.strftime("%Y-%m-%d %H:%M"), "days": [days[0], days[-1]], "split": cut, "profile": excess_profile(P), "grid": {}}
    allT = []
    for fc in ("fair_sched", "fair_roll"):
        for lag in (0, 1):
            for buf in (0.0, 1.0):
                T = simulate(P, fc, lag, buf)
                if T.empty:
                    res["grid"][f"{fc}|lag{lag}|buf{buf}"] = {"n": 0}; continue
                T["variant"] = f"{fc}|lag{lag}|buf{buf}"; allT.append(T)
                st = {"all": stats(T, cut)}
                for sym, g in T.groupby("sym"):
                    st[sym] = stats(g, cut)
                T2 = simulate(P, fc, lag, buf, cost_mult=2.0); T3 = simulate(P, fc, lag, buf, cost_mult=3.0)
                st["cost2x"] = stats(T2, cut) if not T2.empty else {"n": 0}; st["cost3x"] = stats(T3, cut) if not T3.empty else {"n": 0}
                res["grid"][f"{fc}|lag{lag}|buf{buf}"] = st
    if allT:
        pd.concat(allT).to_csv(os.path.join(OUT, "trades.csv"), index=False)
    try:
        res["snapshots"] = snapshots()
    except Exception as e:
        res["snapshots"] = {"err": f"{type(e).__name__}: {str(e)[:160]}"}
    res["secs"] = round(time.time() - t0, 1)
    json.dump(res, open(os.path.join(OUT, "results.json"), "w"), indent=1, default=str)
    return res
