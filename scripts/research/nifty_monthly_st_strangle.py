"""NIFTY monthly Supertrend short strangle (2026-11-08, Nikhil):
"Every month over last 5 years, if last month supertrend closed red, sell current monthly expiry OTM-10 call, and next monthly expiry
OTM-10 put, exit at end of current month."  Clarified: NIFTY; strike closest to 10 delta; no stop - each leg held to the month-end exit.

How it is run (choices fixed before looking at results):
  * Supertrend(10, 3) on MONTHLY NIFTY spot candles (ATR = Wilder / RMA, bands on (high+low)/2, the usual TradingView definition), from 2014
    so it is warmed up. "Red" = the Supertrend is in a down-trend at the close of the previous month.
  * Months Nov 2021 - Oct 2026 (60). Entry = CLOSE of the month's first trading day (daily NSE bhavcopy - no intraday option prices).
  * Expiries: the monthly expiry of a month = the last NIFTY option expiry listed in that calendar month (weeklies sit earlier).
    Call = this month's monthly expiry; put = next month's monthly expiry.
  * 10 delta: Black-76 on the futures price of the same expiry (put-call parity at the money if no future), r = 6.5 %, time to 15:30 IST;
    IV solved from each strike's close, only strikes that traded that day. The OTM call / put with |delta| nearest 0.10 is sold.
  * Exit: the call is held to its expiry and settles at its intrinsic value on the NIFTY close of expiry day; the put is bought back at its
    close on the month's last trading day (settlement price if it did not trade). No stop.
  * Costs per leg: 1 index point of slippage per fill + 0.15 % of the premium per fill (STT, exchange, GST, stamp). The call has one
    fill (expiry is free for a short OTM leg; an ITM short call pays nothing extra here).
  * P&L in NIFTY points per unit; rupees for 1 lot at 75 units (today's lot) so months are comparable.
  * Shown alongside: the same trade every month (no filter) and only when the Supertrend was GREEN - to see what the filter adds.
Data: data_store/research/nifty_monthly/ (app/buresearch.py: Dhan NIFTY daily + NSE bhavcopies).
Run: python3 scripts/research/nifty_monthly_st_strangle.py
"""
from __future__ import annotations

import math
import os
from datetime import date, datetime

import numpy as np
import pandas as pd

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
NM = os.path.join(ROOT, "data_store", "research", "nifty_monthly")
R, LOT, SLIP, FEE = 0.065, 75, 1.0, 0.0015
N = lambda x: 0.5 * (1 + math.erf(x / math.sqrt(2)))


def b76(F, K, T, v, cp):
    if T <= 0 or v <= 0:
        return max(0.0, (F - K) if cp == "CE" else (K - F))
    d1 = (math.log(F / K) + 0.5 * v * v * T) / (v * math.sqrt(T)); d2 = d1 - v * math.sqrt(T); df = math.exp(-R * T)
    return df * (F * N(d1) - K * N(d2)) if cp == "CE" else df * (K * N(-d2) - F * N(-d1))


def iv(px, F, K, T, cp):
    lo, hi = 0.01, 3.0
    if px <= b76(F, K, T, lo, cp) or px >= b76(F, K, T, hi, cp):
        return None
    for _ in range(80):
        m = (lo + hi) / 2
        lo, hi = (m, hi) if b76(F, K, T, m, cp) < px else (lo, m)
    return (lo + hi) / 2


def delta(F, K, T, v, cp):
    d1 = (math.log(F / K) + 0.5 * v * v * T) / (v * math.sqrt(T)); df = math.exp(-R * T)
    return df * N(d1) if cp == "CE" else -df * N(-d1)


def supertrend_monthly(d, n_atr=10, mult=3.0):
    m = d.assign(m=d.day.str[:7]).groupby("m").agg(open=("open", "first"), high=("high", "max"), low=("low", "min"), close=("close", "last"))
    h, l, c = m.high.values, m.low.values, m.close.values; n = len(m)
    tr = np.r_[h[0] - l[0], np.maximum(h[1:] - l[1:], np.maximum(abs(h[1:] - c[:-1]), abs(l[1:] - c[:-1])))]
    atr = np.zeros(n); atr[0] = tr[0]
    for i in range(1, n):
        atr[i] = (atr[i - 1] * (n_atr - 1) + tr[i]) / n_atr if i >= n_atr else tr[: i + 1].mean()
    hl2 = (h + l) / 2; ub, lb = hl2 + mult * atr, hl2 - mult * atr; fu, fl = ub.copy(), lb.copy(); up = np.ones(n, bool)
    for i in range(1, n):
        fu[i] = ub[i] if ub[i] < fu[i - 1] or c[i - 1] > fu[i - 1] else fu[i - 1]
        fl[i] = lb[i] if lb[i] > fl[i - 1] or c[i - 1] < fl[i - 1] else fl[i - 1]
        up[i] = (c[i] > fu[i - 1]) if not up[i - 1] else not (c[i] < fl[i - 1])
    m["st_up"] = up; m["st_line"] = np.where(up, fl, fu)
    return m


def pick(b, exp, day, side):
    o = b[(b.kind == "OPT") & (b.expiry == exp)]
    fut = b[(b.kind == "FUT") & (b.expiry == exp)]
    T = ((datetime.fromisoformat(exp) - datetime.fromisoformat(day)).days) / 365.0
    if len(fut):
        F = float(fut.close.iloc[0])
    else:                                                                      # put-call parity at the strike with the smallest |C - P|
        ce = o[o.cp == "CE"].set_index("strike").close; pe = o[o.cp == "PE"].set_index("strike").close; k = (ce - pe).abs().dropna().idxmin()
        F = k + (ce[k] - pe[k]) * math.exp(R * T)
    q = o[(o.cp == side) & (o.vol > 0) & (o.close > 0) & ((o.strike > F) if side == "CE" else (o.strike < F))]
    best = None
    for r in q.itertuples():
        v = iv(r.close, F, r.strike, T, side)
        if v is None:
            continue
        dl = abs(delta(F, r.strike, T, v, side))
        if best is None or abs(dl - 0.10) < abs(best[1] - 0.10):
            best = (r.strike, dl, r.close, v)
    return best, F, T


if __name__ == "__main__":
    d = pd.read_csv(os.path.join(NM, "nifty_daily.csv")); spot = d.set_index("day").close
    st = supertrend_monthly(d)
    d["m"] = d.day.str[:7]; first = d.groupby("m").day.min(); last = d.groupby("m").day.max()
    months = [m for m in st.index if "2021-11" <= m <= "2026-10"]
    rows = []
    for m in months:
        prev = st.index[st.index.get_loc(m) - 1]; red = not st.loc[prev, "st_up"]
        e, x = first[m], last[m]
        fe, fx = os.path.join(NM, f"bhav_{e}.csv.gz"), os.path.join(NM, f"bhav_{x}.csv.gz")
        if not (os.path.exists(fe) and os.path.exists(fx)):
            rows.append({"month": m, "red": red, "err": "missing bhavcopy"}); continue
        B, X = pd.read_csv(fe), pd.read_csv(fx)
        exps = sorted(B[B.kind == "OPT"].expiry.unique())
        nxt = (pd.Period(m) + 1).strftime("%Y-%m")
        cur = [z for z in exps if z[:7] == m and z > e]; nx = [z for z in exps if z[:7] == nxt]
        if not cur or not nx:
            rows.append({"month": m, "red": red, "err": "expiry not found"}); continue
        ec, ep = cur[-1], nx[-1]
        (c, Fc, Tc), (p, Fp, Tp) = pick(B, ec, e, "CE"), pick(B, ep, e, "PE")
        if not c or not p:
            rows.append({"month": m, "red": red, "err": "no 10-delta strike"}); continue
        s_exp = spot.get(ec)
        if s_exp is None:                                                       # expiry on a non-index day? use the last close before
            s_exp = spot[spot.index <= ec].iloc[-1]
        call_out = max(0.0, s_exp - c[0])
        px = X[(X.kind == "OPT") & (X.expiry == ep) & (X.cp == "PE") & (X.strike == p[0])]
        if not len(px):
            rows.append({"month": m, "red": red, "err": "put not in exit file"}); continue
        pr = px.iloc[0]; put_out = float(pr.close) if pr.vol > 0 and pr.close > 0 else float(pr.settle); stale = not (pr.vol > 0)
        call_g, put_g = c[2] - call_out, p[2] - put_out
        call_n = call_g - SLIP - FEE * c[2]
        put_n = put_g - 2 * SLIP - FEE * (p[2] + put_out)
        rows.append({"month": m, "red": red, "entry": e, "spot_in": round(spot[e], 1), "call_exp": ec, "call_k": c[0], "call_delta": round(c[1], 3), "call_prem": c[2],
                     "call_iv": round(c[3] * 100, 1), "spot_call_exp": round(s_exp, 1), "call_pnl": round(call_n, 1), "put_exp": ep, "put_k": p[0],
                     "put_delta": round(p[1], 3), "put_prem": p[2], "put_iv": round(p[3] * 100, 1), "exit": x, "spot_out": round(spot[x], 1), "put_out": put_out,
                     "put_stale": stale, "put_pnl": round(put_n, 1), "pnl": round(call_n + put_n, 1), "gross": round(call_g + put_g, 1)})
    T = pd.DataFrame(rows); T.to_csv(os.path.join(ROOT, "data_store", "research", "nifty_monthly_st_strangle.csv"), index=False)
    ok = T.dropna(subset=["pnl"]) if "pnl" in T else T.iloc[:0]
    errs = T[T.get("err").notna()] if "err" in T else T.iloc[:0]
    if len(errs):
        print("skipped months:", errs[["month", "err"]].values.tolist())
    print(f"months {len(T)} · priced {len(ok)} · Supertrend red in {int(T.red.sum())} of them\n")
    def show(x, lab):
        if not len(x):
            print(f"{lab}: no trades"); return
        cum = x.pnl.cumsum(); dd = (cum - cum.cummax()).min()
        print(f"{lab:34s} n {len(x):3d} | total {x.pnl.sum():+8.1f} pts (Rs {x.pnl.sum()*LOT:+,.0f} on 1 lot of {LOT}) | avg {x.pnl.mean():+6.1f} | win {100*(x.pnl>0).mean():3.0f}% "
              f"| worst {x.pnl.min():+7.1f} ({x.loc[x.pnl.idxmin(),'month']}) | max DD {dd:+.1f} pts | calls {x.call_pnl.sum():+.1f} puts {x.put_pnl.sum():+.1f} | gross {x.gross.sum():+.1f}")
    cols0 = ["month", "red", "spot_in", "call_k", "call_delta", "call_prem", "spot_call_exp", "call_pnl", "put_k", "put_delta", "put_prem", "spot_out", "put_out", "put_pnl", "pnl"]
    show(ok[ok.red], "RULE: Supertrend red last month"); show(ok, "every month (no filter)"); show(ok[~ok.red], "only green months")
    print("\nSENSITIVITY - the same priced months, filtered by other Supertrend settings (monthly candles):")
    for n_atr, mult in ((10, 3), (10, 2), (10, 1.5), (10, 1), (7, 3), (7, 2), (7, 1)):
        S2 = supertrend_monthly(d, n_atr, mult); redm = {mm for mm in months if not S2.loc[S2.index[S2.index.get_loc(mm) - 1], "st_up"]}
        x = ok[ok.month.isin(redm)]; y = ok[~ok.month.isin(redm)]
        f = lambda z: f"n {len(z):2d} total {z.pnl.sum():+8.1f} avg {z.pnl.mean() if len(z) else 0:+6.1f} win {100*(z.pnl>0).mean() if len(z) else 0:3.0f}% worst {z.pnl.min() if len(z) else 0:+7.1f}"
        print(f"  ST({n_atr},{mult}): red months {len(redm):2d} -> red: {f(x)} | green: {f(y)}")
    print("\nEVERY MONTH (all 60), for reference:")
    print(ok[cols0].to_string(index=False))
    print("\nRULE trades:")
    cols = ["month", "spot_in", "call_k", "call_delta", "call_prem", "spot_call_exp", "call_pnl", "put_k", "put_delta", "put_prem", "spot_out", "put_out", "put_pnl", "pnl"]
    print(ok[ok.red][cols].to_string(index=False))
