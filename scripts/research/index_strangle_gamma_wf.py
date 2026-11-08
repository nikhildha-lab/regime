"""FULL BACKTEST - does selling the weekly index strangle work better in POSITIVE dealer-gamma weeks? (2026-11-09, Nikhil: "can we back
test this: selling weekly strangles worked better in positive-gamma weeks")

Same trade as scripts/research/nifty_options_regime_test.py (sell ~1 ATM-straddle-width strangle at the close a few sessions before the
weekly expiry, hold to expiry, settle at intrinsic; iron-condor version with wings 2 widths away), run through the backtester gauntlet:
  1. NO LOOK-AHEAD variant: the gamma reading comes from the PREVIOUS session's end-of-day chain (the same-day reading uses OI that is
     only published after the close, so "same day" is shown only for comparison)
  2. neighbouring settings: entry 2 / 3 / 4 sessions before expiry x strike width 0.75 / 1.0 / 1.25 straddles = 9 settings per
     strategy - the gamma effect must hold across them, not in one cell
  3. 70 / 30 date split; year by year; walk-forward (each test year judged with nothing fitted - the filter has no threshold: gamma > 0)
  4. stress: 2x and 3x costs, the best 3 weeks removed, max drawdown, worst week
Costs (backtester rule): per leg per side Rs 25 + 0.10 % of premium; ITM legs at expiry pay the exit side on intrinsic.
Data: MASTER_BACKTESTER/data/options/chain_<IDX>.csv (NSE / BSE daily closes per strike), spot daily (NIFTY: Dhan, SENSEX: deep spot).
Run: python3 scripts/research/index_strangle_gamma_wf.py NIFTY      (or SENSEX)
"""
from __future__ import annotations

import os
import sys
from datetime import date

import numpy as np
import pandas as pd
from scipy.special import ndtr

IDX = (sys.argv[1] if len(sys.argv) > 1 else "NIFTY").upper()
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
MB = os.path.join(os.environ.get("HOME", ""), "mnt", "Documents", "MASTER_BACKTESTER", "data")
if not os.path.isdir(MB):
    MB = os.path.expanduser("~/Documents/MASTER_BACKTESTER/data")
LOT = {"NIFTY": 65, "SENSEX": 20}[IDX]; FIX = 25 / LOT; R = 0.065


def bs(S, K, T, v, call):
    d1 = (np.log(S / K) + (R + 0.5 * v * v) * T) / (v * np.sqrt(T)); d2 = d1 - v * np.sqrt(T)
    return np.where(call, S * ndtr(d1) - K * np.exp(-R * T) * ndtr(d2), K * np.exp(-R * T) * ndtr(-d2) - S * ndtr(-d1))


def iv_vec(px, S, K, T, call):
    lo, hi = np.full(len(px), 0.01), np.full(len(px), 3.0)
    for _ in range(45):
        m = (lo + hi) / 2; f = bs(S, K, T, m, call) < px; lo = np.where(f, m, lo); hi = np.where(f, hi, m)
    v = (lo + hi) / 2
    return np.where((v > 0.011) & (v < 2.99), v, np.nan)


def t(x):
    x = pd.Series(x).dropna(); return x.mean() / x.std() * np.sqrt(len(x)) if len(x) > 2 and x.std() else np.nan


if __name__ == "__main__":
    C = pd.read_csv(os.path.join(MB, "options", f"chain_{IDX}.csv"), usecols=["date", "expiry", "strike", "type", "close", "volume", "oi"])
    if IDX == "NIFTY":
        S = pd.read_csv(os.path.join(ROOT, "data_store", "research", "nifty_monthly", "nifty_daily.csv")).set_index("day").sort_index()
    else:
        S = pd.read_csv(os.path.join(MB, "SENSEX_deep_spot.csv")).rename(columns={"date": "day"}); S["day"] = S.day.astype(str).str[:10]
        S = S.drop_duplicates("day").set_index("day").sort_index()
    spot = S.close; days = sorted(set(C.date) & set(spot.index)); di = {d: i for i, d in enumerate(days)}
    C = C[C.date.isin(days) & C.expiry.notna()].copy(); C["expiry"] = C.expiry.astype(str); C["S"] = C.date.map(spot); C["T"] = ((pd.to_datetime(C.expiry) - pd.to_datetime(C.date)).dt.days.clip(lower=0) + 0.25) / 365
    G = C[(C["T"] * 365 <= 31) & ((C.strike / C.S - 1).abs() <= 0.05) & (C.close > 0) & (C.volume > 0)].copy()
    G["iv"] = iv_vec(G.close.values, G.S.values, G.strike.values, G["T"].values, (G.type == "CE").values)
    G = G.dropna(subset=["iv"])
    d1 = (np.log(G.S / G.strike) + (R + 0.5 * G.iv ** 2) * G["T"]) / (G.iv * np.sqrt(G["T"]))
    G["dg"] = np.exp(-0.5 * d1 ** 2) / np.sqrt(2 * np.pi) / (G.S * G.iv * np.sqrt(G["T"])) * G.oi * G.S ** 2 * np.where(G.type == "CE", 1, -1)
    gex = G.groupby("date").dg.sum()
    book = {(d, e): x for (d, e), x in C.groupby(["date", "expiry"])}
    rows = []
    for E in sorted(C.expiry.unique()):
        if E not in di:
            continue
        for back in (2, 3, 4):
            if di[E] < back + 1:
                continue
            e = days[di[E] - back]; X = book.get((e, E))
            if X is None or not len(X):
                continue
            S0, ST = spot[e], spot[E]; ce = X[X.type == "CE"].set_index("strike"); pe = X[X.type == "PE"].set_index("strike")
            ks = sorted(set(ce.index) & set(pe.index))
            if not ks:
                continue
            k0 = min(ks, key=lambda z: abs(z - S0))
            if not (ce.volume.get(k0, 0) > 0 and pe.volume.get(k0, 0) > 0):
                continue
            st = float(ce.close[k0] + pe.close[k0]); pick = lambda tg: min(ks, key=lambda z: abs(z - tg))
            g_same, g_prev = gex.get(e, np.nan), gex.get(days[di[e] - 1], np.nan)
            for w in (0.75, 1.0, 1.25):
                for nm, legs in (("STRANGLE", [(pick(k0 + w * st), "CE", -1), (pick(k0 - w * st), "PE", -1)]),
                                 ("CONDOR", [(pick(k0 + w * st), "CE", -1), (pick(k0 - w * st), "PE", -1), (pick(k0 + 2 * w * st), "CE", 1), (pick(k0 - 2 * w * st), "PE", 1)])):
                    g_ = c_ = 0.0; ok = True
                    for k, cp, q in legs:
                        px = (ce if cp == "CE" else pe).close.get(k)
                        if px is None or px <= 0:
                            ok = False; break
                        it = max(0.0, ST - k) if cp == "CE" else max(0.0, k - ST)
                        g_ += q * (it - px); c_ += FIX + 0.001 * px + ((FIX + 0.001 * it) if it > 0 else 0)
                    if ok:
                        rows.append({"expiry": E, "back": back, "w": w, "strat": nm, "gross": g_, "cost": c_, "gex_same": g_same, "gex_prev": g_prev})
    T = pd.DataFrame(rows); T["net"] = T.gross - T.cost
    T.to_csv(os.path.join(ROOT, "data_store", "research", f"{IDX.lower()}_strangle_gamma_wf.csv"), index=False)
    ex = sorted(T.expiry.unique()); cut = ex[int(len(ex) * 0.7)]
    print(f"{IDX}: {len(ex)} weekly expiries {ex[0]} .. {ex[-1]} · train < {cut} · points per unit (x{LOT} = Rs per lot)\n")
    print("A. EVERY SETTING - positive-gamma weeks vs negative (gamma from the PREVIOUS session = no look-ahead)")
    print("   setting                 all wks      +gamma (n, avg, t)        -gamma (n, avg)    +gamma minus -gamma | +g train / test")
    summ = []
    for nm in ("STRANGLE", "CONDOR"):
        for back in (2, 3, 4):
            for w in (0.75, 1.0, 1.25):
                x = T[(T.strat == nm) & (T.back == back) & (T.w == w)].dropna(subset=["gex_prev"])
                p, n_ = x[x.gex_prev > 0], x[x.gex_prev <= 0]
                summ.append({"strat": nm, "back": back, "w": w, "pos": p.net.mean(), "neg": n_.net.mean(), "diff": p.net.mean() - n_.net.mean()})
                print(f"   {nm:8s} {back}d w{w:<4}  {x.net.mean():+7.1f}   n {len(p):3d} {p.net.mean():+7.1f} (t {t(p.net):+.1f})   n {len(n_):3d} {n_.net.mean():+7.1f}      {p.net.mean()-n_.net.mean():+7.1f}"
                      f"        | {p[p.expiry < cut].net.mean():+6.1f} / {p[p.expiry >= cut].net.mean():+6.1f}")
    Sm = pd.DataFrame(summ)
    print(f"\n   positive gamma beat negative in {int((Sm['diff'] > 0).sum())} of {len(Sm)} settings; +gamma profitable in {int((Sm.pos > 0).sum())} of {len(Sm)}")
    print("\nB. MAIN SETTING (3 sessions before expiry, width 1.0) in detail")
    for nm in ("STRANGLE", "CONDOR"):
        for lab, col in (("gamma from previous session (no look-ahead)", "gex_prev"), ("gamma same day (needs that day's OI)", "gex_same")):
            x = T[(T.strat == nm) & (T.back == 3) & (T.w == 1.0)].dropna(subset=[col]).sort_values("expiry")
            for side, m in (("+gamma", x[col] > 0), ("-gamma", x[col] <= 0), ("all", x[col] == x[col])):
                y = x[m]; s2, s3 = (y.gross - 2 * y.cost), (y.gross - 3 * y.cost)
                eq = y.net.cumsum(); dd = (eq - eq.cummax()).min(); yr = y.groupby(y.expiry.str[:4]).net.sum()
                print(f"   {nm:8s} {lab:44s} {side:6s} n {len(y):3d} avg {y.net.mean():+7.1f} (t {t(y.net):+.1f}) Rs {y.net.mean()*LOT:+7,.0f}/lot/wk | win {100*(y.net>0).mean():3.0f}% "
                      f"worst {y.net.min():+7.1f} maxDD {dd:+8.1f} | 2x {s2.mean():+6.1f} 3x {s3.mean():+6.1f} -best3 {y.net.sort_values().iloc[:-3].mean():+6.1f} "
                      f"| train {y[y.expiry < cut].net.mean():+6.1f} test {y[y.expiry >= cut].net.mean():+6.1f} | years " + " ".join(f"{k}:{v:+.0f}" for k, v in yr.items()))
            print()
    print("C. WALK-FORWARD - each year on its own, +gamma vs -gamma (main setting, previous-session gamma; nothing is fitted)")
    for nm in ("STRANGLE", "CONDOR"):
        x = T[(T.strat == nm) & (T.back == 3) & (T.w == 1.0)].dropna(subset=["gex_prev"])
        for y_, g in x.groupby(x.expiry.str[:4]):
            p, n_ = g[g.gex_prev > 0].net, g[g.gex_prev <= 0].net
            print(f"   {nm:8s} {y_}: +gamma n {len(p):3d} avg {p.mean():+7.1f} | -gamma n {len(n_):3d} avg {n_.mean():+7.1f} | +gamma better: {'yes' if p.mean() > n_.mean() else 'NO'}")
