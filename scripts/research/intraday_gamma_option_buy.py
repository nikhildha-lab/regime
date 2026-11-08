"""INTRADAY OPTION BUYING with the gamma regime (2026-11-08, Nikhil: "no option buy strategy with gamma exposure, intraday?").

Idea: when dealers are SHORT gamma (negative dealer GEX) their hedging chases the move, so breakouts should run further - the days when
buying options intraday can pay. When they are long gamma (positive) moves get absorbed and bought options bleed.
Data:
  options  MASTER_BACKTESTER/data/external/dhan/orderflow/data/<IDX>_options/*.csv - Dhan 1-minute OHLC per strike (ATM +- N, nearest
           expiry), with spot, IV, OI, Oct 2024 - Oct 2026
  regime   previous session's dealer GEX + gamma flip from the end-of-day chain (data/options/chain_<IDX>.csv) - known before the open
Trades (rules fixed before looking, one trade a day per rule, nearest-expiry ATM option bought at the NEXT minute's open):
  ORB      09:15-09:30 opening range of spot; first 1-min close outside it between 09:31 and 13:00 -> buy CE (up) / PE (down)
  MOM      at 10:15, spot moved >= 0.4 % from the day's open -> buy in that direction
  FLIP     first 1-min close that crosses the previous close's gamma flip level (09:31-13:00) -> buy in the crossing direction
  exits    option stop -25 %, target +50 % (checked on each 1-min bar's low / high; both in one bar = stop), else out at 15:15 close
  costs    backtester rule: per side Rs 25 + 0.10 % of premium (lot 65 NIFTY / 30 BANKNIFTY); stress +0.5 and +1 point slippage a side
Split by the regime (negative / positive dealer gamma; spot below / above the flip), 70 / 30 by date. Expiry-day trades shown separately
(gamma is extreme on expiry day).
Run: python3 scripts/research/intraday_gamma_option_buy.py NIFTY   (or BANKNIFTY)
"""
from __future__ import annotations

import glob
import os
import sys
from datetime import date

import numpy as np
import pandas as pd
from scipy.special import ndtr

IDX = (sys.argv[1] if len(sys.argv) > 1 else "NIFTY").upper()
MB = os.path.join(os.environ.get("HOME", ""), "mnt", "Documents", "MASTER_BACKTESTER", "data")
if not os.path.isdir(MB):
    MB = os.path.expanduser("~/Documents/MASTER_BACKTESTER/data")
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
LOT = 65 if IDX == "NIFTY" else 30
R = 0.065; STOP, TGT = float(os.environ.get("IGB_STOP", -0.25)), float(os.environ.get("IGB_TGT", 0.50))   # exit variants for the stress check


def bs(S, K, T, v, call):
    d1 = (np.log(S / K) + (R + 0.5 * v * v) * T) / (v * np.sqrt(T)); d2 = d1 - v * np.sqrt(T)
    return np.where(call, S * ndtr(d1) - K * np.exp(-R * T) * ndtr(d2), K * np.exp(-R * T) * ndtr(-d2) - S * ndtr(-d1))


def iv_vec(px, S, K, T, call):
    lo, hi = np.full(len(px), 0.01), np.full(len(px), 3.0)
    for _ in range(45):
        m = (lo + hi) / 2; f = bs(S, K, T, m, call) < px; lo = np.where(f, m, lo); hi = np.where(f, hi, m)
    v = (lo + hi) / 2
    return np.where((v > 0.011) & (v < 2.99), v, np.nan)


def gamma(S, K, T, v):
    d1 = (np.log(S / K) + (R + 0.5 * v * v) * T) / (v * np.sqrt(T)); return np.exp(-0.5 * d1 * d1) / np.sqrt(2 * np.pi) / (S * v * np.sqrt(T))


def t(x):
    x = pd.Series(x).dropna(); return x.mean() / x.std() * np.sqrt(len(x)) if len(x) > 2 and x.std() else np.nan


def regimes(spot_close):
    C = pd.read_csv(os.path.join(MB, "options", f"chain_{IDX}.csv"), usecols=["date", "expiry", "strike", "type", "close", "volume", "oi"])
    C = C[C.date.isin(spot_close.index)].copy(); C["S"] = C.date.map(spot_close)
    C["T"] = ((pd.to_datetime(C.expiry) - pd.to_datetime(C.date)).dt.days.clip(lower=0) + 0.25) / 365
    C = C[(C["T"] * 365 <= 31) & ((C.strike / C.S - 1).abs() <= 0.05) & (C.close > 0) & (C.volume > 0)].copy()
    C["iv"] = iv_vec(C.close.values, C.S.values, C.strike.values, C["T"].values, (C.type == "CE").values)
    C = C.dropna(subset=["iv"]); C["sg"] = np.where(C.type == "CE", 1.0, -1.0)
    out = {}
    for d, x in C.groupby("date"):
        S0 = x.S.iloc[0]; grid = np.linspace(S0 * 0.97, S0 * 1.03, 49)
        tot = np.array([(gamma(s, x.strike.values, x["T"].values, x.iv.values) * x.oi.values * x.sg.values * s * s).sum() for s in grid])
        now = (gamma(S0, x.strike.values, x["T"].values, x.iv.values) * x.oi.values * x.sg.values * S0 * S0).sum()
        flip = None
        for i in range(len(grid) - 1):
            if (tot[i] < 0) != (tot[i + 1] < 0):
                f = grid[i] + (grid[i + 1] - grid[i]) * (-tot[i]) / (tot[i + 1] - tot[i])
                if flip is None or abs(f - S0) < abs(flip - S0):
                    flip = f
        out[d] = {"gex": now, "flip": flip}
    return out


def trade(O, S, side, t0):
    """Buy the option of `side` nearest the money (spot S at the signal minute) at the open of minute t0, manage on 1-min bars."""
    cp = "CE" if side > 0 else "PE"
    cand = O[(O.type == cp) & (O.ts == t0)]
    if not len(cand):
        return None
    k = cand.iloc[(cand.strike - S).abs().argsort().iloc[0]].strike
    path = O[(O.type == cp) & (O.strike == k) & (O.ts >= t0) & (O.ts <= t0[:11] + "15:15:00")].sort_values("ts")
    if len(path) < 3:
        return None
    e = float(path.iloc[0].open)
    if e <= 0:
        return None
    sp, tp = e * (1 + STOP), e * (1 + TGT); out, why = float(path.iloc[-1].close), "time"
    for r in path.itertuples():
        if r.low <= sp:
            out, why = min(sp, r.open), "stop"; break
        if r.high >= tp:
            out, why = max(tp, r.open), "target"; break
    fee = 25 / LOT * 2 + 0.001 * (e + out)
    return {"strike": k, "cp": cp, "entry": e, "exit": out, "why": why, "gross": out - e, "net": out - e - fee, "pct": (out - e - fee) / e * 100}


if __name__ == "__main__":
    files = sorted(glob.glob(os.path.join(MB, "external", "dhan", "orderflow", "data", f"{IDX}_options", "*.csv")))
    days = {}
    for f in files:
        D = pd.read_csv(f, usecols=["strike", "spot", "open", "high", "low", "close", "ts", "type"])
        D["d"] = D.ts.str[:10]
        for d, x in D.groupby("d"):
            if d not in days:
                days[d] = x
    sc = pd.Series({d: x.sort_values("ts").spot.iloc[-1] for d, x in days.items()}).sort_index()
    print(f"{IDX}: {len(days)} sessions with 1-min options {sc.index[0]} .. {sc.index[-1]}"); sys.stdout.flush()
    RG = regimes(sc); dl = sorted(days); rows = []
    for i, d in enumerate(dl):
        if i == 0 or dl[i - 1] not in RG:
            continue
        rg = RG[dl[i - 1]]; O = days[d]
        sp = O.groupby("ts").spot.first().sort_index(); sp = sp[(sp.index >= d + " 09:15:00") & (sp.index <= d + " 15:30:00")]
        if len(sp) < 300:
            continue
        orr = sp[sp.index < d + " 09:30:00"]; hi, lo = orr.max(), orr.min(); op = sp.iloc[0]
        base = {"day": d, "gex": rg["gex"], "flip": rg["flip"], "open": op, "expiry_day": False}
        win = sp[(sp.index >= d + " 09:31:00") & (sp.index <= d + " 13:00:00")]
        nxt = lambda ts: sp.index[sp.index.get_loc(ts) + 1] if sp.index.get_loc(ts) + 1 < len(sp) else None
        brk = win[(win > hi) | (win < lo)]
        if len(brk):
            ts = brk.index[0]; side = 1 if brk.iloc[0] > hi else -1; t0 = nxt(ts)
            r = trade(O, brk.iloc[0], side, t0) if t0 else None
            if r: rows.append({**base, "rule": "ORB", "side": side, **r})
        at = d + " 10:15:00"
        if at in sp.index:
            mv = sp[at] / op - 1
            if abs(mv) >= 0.004:
                t0 = nxt(at); r = trade(O, sp[at], 1 if mv > 0 else -1, t0) if t0 else None
                if r: rows.append({**base, "rule": "MOM", "side": 1 if mv > 0 else -1, **r})
        if rg["flip"]:
            f = rg["flip"]; prev = sp.shift(1)
            cross = win[((prev.reindex(win.index) < f) & (win >= f)) | ((prev.reindex(win.index) > f) & (win <= f))]
            if len(cross):
                ts = cross.index[0]; side = 1 if cross.iloc[0] >= f else -1; t0 = nxt(ts)
                r = trade(O, cross.iloc[0], side, t0) if t0 else None
                if r: rows.append({**base, "rule": "FLIP", "side": side, **r})
    T = pd.DataFrame(rows); T.to_csv(os.path.join(ROOT, "data_store", "research", f"intraday_gamma_buy_{IDX.lower()}.csv"), index=False)
    ud = sorted(T.day.unique()); cut = ud[int(len(ud) * 0.7)]
    print(f"trades {len(T)} · train < {cut}\n   option points per unit after costs (x{LOT} = rupees per lot) | % of premium | win | stops / targets | train / test | +0.5 / +1 pt slippage a side\n")
    for rule in ("ORB", "MOM", "FLIP"):
        X = T[T.rule == rule]
        for lab, m in (("all days", X.gex == X.gex), ("NEGATIVE dealer gamma (prev close)", X.gex <= 0), ("positive dealer gamma", X.gex > 0),
                       ("spot opened BELOW the flip", X.open < X.flip.fillna(-1e9)), ("spot opened ABOVE the flip", X.open >= X.flip.fillna(1e18))):
            x = X[m]
            if len(x) < 8:
                print(f"   {rule:4s} {lab:36s} n {len(x)}"); continue
            a, b = x[x.day < cut].net, x[x.day >= cut].net
            print(f"   {rule:4s} {lab:36s} n {len(x):3d} | avg {x.net.mean():+6.1f} pts (t {t(x.net):+.1f}) = Rs {x.net.mean()*LOT:+7,.0f}/lot | {x.pct.mean():+5.1f}% | win {100*(x.net>0).mean():3.0f}% "
                  f"| stop {100*(x.why=='stop').mean():3.0f}% tgt {100*(x.why=='target').mean():3.0f}% | train {a.mean():+6.1f} test {b.mean():+6.1f} | slip {(x.net-1).mean():+6.1f} / {(x.net-2).mean():+6.1f}")
        print()
