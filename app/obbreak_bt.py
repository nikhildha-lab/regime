"""BACKTEST: ORDER-BLOCK BREAK + 2 ATR TRAILING STOP ON STOCK FUTURES (2026-11-11, Nikhil: "if we enter with a future position with a
trailing SL of 2 ATR will this strategy work?"). Research only - never orders. Runs inside the dashboard process on the Mac
(data_store/research/fno_hist is there); results to data_store/research/ob_break_bt/.

Data: NSE stock futures daily bhavcopy (compact), most-held contract per day, chained across rolls by its own prev close.
Order blocks: same as BuildUp v2 / app/obscan.py
  supply   = up day j, then within 3 sessions a close <= low[j] - 1.5 ATR(14)   zone [low j, high j], confirmed on day k
  bull OB  = down day j, then within 3 sessions a close >= high[j] + 1.5 ATR(14)
Signal (decided on day i's CLOSE, nothing later):
  LONG   close[i] > top of a supply confirmed by i-1, formed in the last 60 sessions, no close above its top from k to i-1
  SHORT  close[i] < bottom of a bull OB, same conditions mirrored
Entry: next session's OPEN (day i+1). 1 futures lot (results in % of notional and in R).
Stop: initial = entry -/+ 2 x ATR(14) known at day i's close. Trailing (chandelier on closes): after each close
  stop = max(stop, close - 2 x ATR) for longs (mirrored for shorts) - only ever tightens.
  Checked on every later day: gap through the stop fills at the open, else at the stop.
Time stop: TS sessions (variants 20 / 60), exit at that day's close.
Costs: 0.05 % a side (0.10 % round trip); stress 2x / 3x. One open trade per stock at a time.
Filters (variants): "all" = every break; "buildup" = the break day is a build-up in its direction (long: price up and total OI up;
  short: price down and total OI up) - the daily stand-in for the dashboard's conviction.
Control: same exits on build-up days with a >= 2 % move and NO order-block break (does the block add anything?).
"""
from __future__ import annotations

import json
import os
import time

import numpy as np
import pandas as pd

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
HIST = os.path.join(ROOT, "data_store", "research", "fno_hist")
OUT = os.path.join(ROOT, "data_store", "research", "ob_break_bt")
COST = 0.0010
LOOK = 60
ATR_K = 2.0


def load():
    fs = sorted(f for f in os.listdir(HIST) if f.startswith("fut_") and f.endswith(".csv.gz"))
    F = pd.concat([pd.read_csv(os.path.join(HIST, f), usecols=["day", "sym", "kind", "open", "high", "low", "close", "prev", "oi", "chg_oi"]) for f in fs])
    F = F[(F.kind == "STF") & (F.close > 0)]
    tot = F.groupby(["sym", "day"]).agg(oi_t=("oi", "sum"), chg_t=("chg_oi", "sum")).reset_index()
    M = F.sort_values("oi").groupby(["sym", "day"]).tail(1).merge(tot, on=["sym", "day"]).sort_values(["sym", "day"])
    return M


def series(g):
    g = g.copy()
    g["ret"] = (g.close / g.prev - 1).where(g.prev > 0, 0.0)
    C = (1 + g.ret.values).cumprod() * 100
    O, H, L = (g[k].values / g.close.values * C for k in ("open", "high", "low"))
    tr = np.maximum(H - L, np.maximum(np.abs(H - np.roll(C, 1)), np.abs(L - np.roll(C, 1)))); tr[0] = H[0] - L[0]
    A = pd.Series(tr).rolling(14, min_periods=14).mean().values           # ATR known at each day's close
    base = (g.oi_t - g.chg_t).values
    oic = np.where(base > 0, g.chg_t.values / np.where(base > 0, base, 1), 0.0)
    return g.day.astype(str).values, O, H, L, C, A, g.ret.values, oic


def blocks(C, O, H, L, A, up):
    out, seen = [], set()
    for k in range(len(C)):
        for j in (k - 1, k - 2, k - 3):
            if j < 0 or j in seen or not np.isfinite(A[j]):
                continue
            if up and C[j] < O[j] and C[k] >= H[j] + 1.5 * A[j]:
                out.append((j, L[j], H[j], k)); seen.add(j)
            elif not up and C[j] > O[j] and C[k] <= L[j] - 1.5 * A[j]:
                out.append((j, L[j], H[j], k)); seen.add(j)
    return out


def signals(D, O, H, L, C, A, ret, oic):
    """[(i, side, zone)] decided at day i's close. Blocks confirmed by i-1 only."""
    sup = blocks(C, O, H, L, A, False); bull = blocks(C, O, H, L, A, True)
    out = []
    for i in range(20, len(C)):
        for b in sup:
            j, lo, hi, k = b
            if k <= i - 1 and i - LOOK <= j and C[i] > hi and C[i - 1] <= hi and (C[k:i].max() <= hi):
                out.append((i, 1, b)); break
        for b in bull:
            j, lo, hi, k = b
            if k <= i - 1 and i - LOOK <= j and C[i] < lo and C[i - 1] >= lo and (C[k:i].min() >= lo):
                out.append((i, -1, b)); break
    return out


def trade(i, sd, O, H, L, C, A, ts):
    """Enter next open; 2 ATR initial + chandelier trail on closes; time stop ts sessions. Returns (exit_i, ret, R, why) or None."""
    n = len(C)
    if i + 1 >= n or not np.isfinite(A[i]):
        return None
    e = O[i + 1]; risk = ATR_K * A[i]; st = e - sd * risk
    best = None
    for d in range(i + 1, n):
        # stop check on today's range (the entry day too, after the open)
        if sd > 0 and L[d] <= st:
            px = O[d] if (d > i + 1 and O[d] <= st) else st
            return d, (px / e - 1), (px - e) / risk, "trail stop" if best is not None else "stop"
        if sd < 0 and H[d] >= st:
            px = O[d] if (d > i + 1 and O[d] >= st) else st
            return d, -(px / e - 1), (e - px) / risk, "trail stop" if best is not None else "stop"
        if d - i >= ts:
            return d, sd * (C[d] / e - 1), sd * (C[d] - e) / risk, "time"
        if np.isfinite(A[d]):
            nst = C[d] - sd * ATR_K * A[d]
            if (sd > 0 and nst > st) or (sd < 0 and nst < st):
                st = nst; best = d
    return None                                                            # still open at the end of the data


def run(variants=(("all", 20), ("all", 60), ("buildup", 20), ("buildup", 60))):
    t0 = time.time()
    M = load()
    rows = []
    for sym, g in M.groupby("sym"):
        if len(g) < 80:
            continue
        D, O, H, L, C, A, ret, oic = series(g)
        sig = signals(D, O, H, L, C, A, ret, oic)
        sigset = {(i, s) for i, s, _ in sig}
        for filt, ts in list(variants) + [("control", 20), ("control", 60)]:
            busy = -1
            cand = sig if filt != "control" else [(i, s, None) for i in range(20, len(C)) for s in (1, -1)
                                                  if (i, s) not in sigset and s * ret[i] >= 0.02 and oic[i] > 0]
            for i, sd, b in sorted(cand, key=lambda x: x[0]):
                if i <= busy:
                    continue
                if filt == "buildup" and not (sd * ret[i] > 0 and oic[i] > 0):
                    continue
                r = trade(i, sd, O, H, L, C, A, ts)
                if not r:
                    continue
                d, pr, R, why = r
                busy = d
                rows.append({"variant": f"{filt}_{ts}", "sym": sym, "side": "long" if sd > 0 else "short", "signal": D[i], "entry_day": D[i + 1],
                             "exit_day": D[d], "days": d - i, "ret_gross": pr, "R_gross": R, "why": why,
                             "risk_pct": ATR_K * A[i] / O[i + 1], "zone_age": (i - b[0]) if b else None})
    T = pd.DataFrame(rows)
    os.makedirs(OUT, exist_ok=True)
    T.to_csv(os.path.join(OUT, "trades.csv"), index=False)
    res = {"built": time.strftime("%Y-%m-%d %H:%M"), "secs": round(time.time() - t0, 1), "days": [str(M.day.min()), str(M.day.max())],
           "n_syms": int(M.sym.nunique()), "variants": {}}
    days = sorted(M.day.astype(str).unique()); cut = days[int(len(days) * 0.7)]
    res["split"] = cut
    for v, g in T.groupby("variant"):
        for side in ("long", "short", "both"):
            x = g if side == "both" else g[g.side == side]
            if len(x) < 5:
                continue
            res["variants"][f"{v}|{side}"] = stats(x, cut)
    json.dump(res, open(os.path.join(OUT, "results.json"), "w"), indent=1)
    return res


def stats(x, cut):
    def one(r, mult=1.0):
        net = r.ret_gross - COST * mult
        w, l = net[net > 0].sum(), -net[net < 0].sum()
        return {"n": int(len(net)), "win": round(float((net > 0).mean() * 100), 1), "avg_pct": round(float(net.mean() * 100), 3),
                "pf": round(float(w / l), 2) if l > 0 else None, "sum_pct": round(float(net.sum() * 100), 1)}
    x = x.sort_values("exit_day")
    s = one(x)
    s["avg_R"] = round(float((x.R_gross - COST / x.risk_pct).mean()), 3)
    s["cost2x"] = one(x, 2); s["cost3x"] = one(x, 3)
    top = x.assign(net=x.ret_gross - COST).sort_values("net").iloc[:-3] if len(x) > 3 else x
    s["minus_best3"] = one(top)
    s["train"] = one(x[x.signal < cut]); s["test"] = one(x[x.signal >= cut])
    s["test_2x"] = one(x[x.signal >= cut], 2)
    s["years"] = {y: one(g) for y, g in x.groupby(x.signal.str[:4])}
    s["quarters_pos"] = int(sum(1 for _, g in x.groupby(x.signal.str[:4] + "Q" + ((x.signal.str[5:7].astype(int) - 1) // 3 + 1).astype(str)) if (g.ret_gross - COST).sum() > 0))
    s["quarters"] = int(x.groupby(x.signal.str[:4] + "Q" + ((x.signal.str[5:7].astype(int) - 1) // 3 + 1).astype(str)).ngroups)
    eq = (x.ret_gross - COST).cumsum() * 100
    s["max_dd_pct_sum"] = round(float((eq - eq.cummax()).min()), 1)
    s["exits"] = x.why.value_counts().to_dict(); s["avg_days"] = round(float(x.days.mean()), 1)
    return s


def lookahead_check(n=30, seed=7):
    """Cut the data at a signal's day, append random future bars, the same signal must still appear (signals use closes up to i only)."""
    rng = np.random.default_rng(seed)
    M = load(); bad = ok = 0
    for sym, g in list(M.groupby("sym"))[:: max(1, M.sym.nunique() // n)]:
        D, O, H, L, C, A, ret, oic = series(g)
        sig = signals(D, O, H, L, C, A, ret, oic)
        if not sig:
            continue
        i, sd, _ = sig[len(sig) // 2]
        k = i + 1
        noise = lambda arr: np.concatenate([arr[:k], arr[k - 1] * (1 + rng.normal(0, 0.03, 15)).cumprod()])
        C2 = noise(C); O2 = np.concatenate([O[:k], C2[k:]]); H2 = np.concatenate([H[:k], C2[k:] * 1.01]); L2 = np.concatenate([L[:k], C2[k:] * 0.99])
        tr = np.maximum(H2 - L2, np.maximum(np.abs(H2 - np.roll(C2, 1)), np.abs(L2 - np.roll(C2, 1)))); tr[0] = H2[0] - L2[0]
        A2 = pd.Series(tr).rolling(14, min_periods=14).mean().values
        s2 = signals(D, O2, H2, L2, C2, A2, None, None)
        if any(a == i and b == sd for a, b, _ in s2):
            ok += 1
        else:
            bad += 1
    return {"same": ok, "missing": bad}


# ------------------------------------------------------------------ 2026-11-11 (Nikhil: "so we wait for retesting also?")
# RETEST version. After the break close (day i), wait up to RT_WAIT sessions for a retest that HOLDS:
#   long : a day d with low[d] <= block top + 0.25 ATR (price comes back to the block) and close[d] > block top (it holds)
#   short: a day d with high[d] >= block bottom - 0.25 ATR and close[d] < block bottom
# Cancelled if a close falls back inside / through the block (long: close <= top) before the retest holds - the break failed.
# Entry: next open after the retest day. Stops (variants):
#   "trail2"  initial 2 ATR, 2 ATR chandelier trail on closes (as before)
#   "zone"    initial just beyond the far side of the block (long: block bottom - 0.25 ATR), then the same 2 ATR trail once tighter
# Time stop 20 sessions. Same costs, split and checks.
RT_WAIT = 10


def retests(sig, O, H, L, C, A):
    out = []
    for i, sd, b in sig:
        j, lo, hi, k = b
        for d in range(i + 1, min(len(C), i + 1 + RT_WAIT)):
            if not np.isfinite(A[d]):
                continue
            if sd > 0:
                if L[d] <= hi + 0.25 * A[d] and C[d] > hi:
                    out.append((d, sd, b, i)); break
                if C[d] <= hi:
                    break
            else:
                if H[d] >= lo - 0.25 * A[d] and C[d] < lo:
                    out.append((d, sd, b, i)); break
                if C[d] >= lo:
                    break
    return out


def trade_zone(d0, sd, b, O, H, L, C, A, ts):
    """Entry next open after the retest day; initial stop beyond the block; 2 ATR chandelier trail once it is tighter."""
    n = len(C)
    if d0 + 1 >= n:
        return None
    j, lo, hi, k = b
    e = O[d0 + 1]; st = (lo - 0.25 * A[d0]) if sd > 0 else (hi + 0.25 * A[d0])
    if (sd > 0 and st >= e) or (sd < 0 and st <= e):
        return None                                                        # opened beyond the stop
    risk = abs(e - st); moved = False
    for d in range(d0 + 1, n):
        if sd > 0 and L[d] <= st:
            px = O[d] if (d > d0 + 1 and O[d] <= st) else st
            return d, (px / e - 1), (px - e) / risk, "trail stop" if moved else "stop", risk / e
        if sd < 0 and H[d] >= st:
            px = O[d] if (d > d0 + 1 and O[d] >= st) else st
            return d, -(px / e - 1), (e - px) / risk, "trail stop" if moved else "stop", risk / e
        if d - d0 >= ts:
            return d, sd * (C[d] / e - 1), sd * (C[d] - e) / risk, "time", risk / e
        if np.isfinite(A[d]):
            nst = C[d] - sd * ATR_K * A[d]
            if (sd > 0 and nst > st) or (sd < 0 and nst < st):
                st = nst; moved = True
    return None


def run_retest(ts=20):
    t0 = time.time()
    M = load(); rows = []
    for sym, g in M.groupby("sym"):
        if len(g) < 80:
            continue
        D, O, H, L, C, A, ret, oic = series(g)
        sig = signals(D, O, H, L, C, A, ret, oic)
        rt = retests(sig, O, H, L, C, A)
        for filt in ("all", "buildup"):
            for stop in ("trail2", "zone"):
                busy = -1
                for d0, sd, b, i in sorted(rt, key=lambda x: x[0]):
                    if d0 <= busy:
                        continue
                    if filt == "buildup" and not (sd * ret[i] > 0 and oic[i] > 0):
                        continue
                    if stop == "trail2":
                        r = trade(d0, sd, O, H, L, C, A, ts)
                        if r:
                            r = r + (ATR_K * A[d0] / O[d0 + 1],)
                    else:
                        r = trade_zone(d0, sd, b, O, H, L, C, A, ts)
                    if not r:
                        continue
                    d, pr, R, why, rp = r
                    busy = d
                    rows.append({"variant": f"retest_{filt}_{stop}", "sym": sym, "side": "long" if sd > 0 else "short", "signal": D[i],
                                 "retest": D[d0], "entry_day": D[d0 + 1], "exit_day": D[d], "days": d - d0, "ret_gross": pr, "R_gross": R,
                                 "why": why, "risk_pct": rp, "wait": d0 - i})
    T = pd.DataFrame(rows)
    os.makedirs(OUT, exist_ok=True)
    T.to_csv(os.path.join(OUT, "trades_retest.csv"), index=False)
    days = sorted(M.day.astype(str).unique()); cut = days[int(len(days) * 0.7)]
    res = {"built": time.strftime("%Y-%m-%d %H:%M"), "secs": round(time.time() - t0, 1), "split": cut, "variants": {}}
    for v, g in T.groupby("variant"):
        for side in ("long", "short", "both"):
            x = g if side == "both" else g[g.side == side]
            if len(x) >= 5:
                res["variants"][f"{v}|{side}"] = stats(x, cut)
    json.dump(res, open(os.path.join(OUT, "results_retest.json"), "w"), indent=1)
    return res
