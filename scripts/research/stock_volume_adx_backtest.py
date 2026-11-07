"""STOCK LEVEL TRADES WITH VOLUME SPIKE / RVOL / ADX - 4-month backtest (2026-11-08, Nikhil: "check deeply and conclude best condition
which is working"; idea: "DOM stocks should target high volume stocks or a volume spike more than 4x of normal .. maybe ADX > 25").

The DOM book exists only for 3-6 Nov, so this tests the SAME levels the DOM stock rule uses (yesterday's high / low, the 09:15-09:30
opening range) on price + volume only, over all 1-min history (48 F&O stocks, data_store/research/stocks_intraday, ~82 sessions).

Setups on 5-min bars (signal bar 09:30-14:30, entry = the NEXT bar's open, nothing known later is used):
  break  close crosses the level (previous close on the other side), first time that level is crossed that day; trade the break
  hold   price comes from away (previous close > 0.1 % from the level), the bar touches the level (within 0.1 %) and closes back on
         the side it came from; trade the bounce (this is the DOM "hold" setup without the book)
Inputs at the signal bar close: spike = bar volume / median of the same 5-min slot over the previous 10 sessions; rvol = day volume
so far / mean of the previous 10 sessions to the same slot; ADX(14) and +DI / -DI (Wilder) on 5-min and 15-min bars.
Exits (1-min path, stop checked first in a minute, flat at 15:15):
  E1 stop = signal bar's far end (min 0.15 %), target 2R      E2 stop 1 ATR(5m), target 2R
  E3 stop 1 ATR(5m), target 3R                                E4 stop 1 ATR(5m), no target, out 15:15
  E5 stop 2 ATR, target 2R      E6 stop 3 ATR, target 2R      E7 stop 0.5 %, target 1 %      E8 stop 0.5 %, out 15:15      E9 stop 1 %, out 15:15
  (wider stops matter: a 5-min ATR is often ~0.15 % of price, so 0.12 % costs would eat ~0.8R of a 1-ATR stop)
Costs: 0.12 % round trip (charges ~0.08 % + slippage), the same as scripts/research/stock_intraday_tests.py; 0.08 % shown too.
Selection honesty: every filter combination is ranked on the FIRST 2/3 of the days only; the best ones are then shown on the LAST 1/3
(never used to choose). A condition "works" only if it is positive after costs in BOTH parts with enough trades.
Run: python3 scripts/research/stock_volume_adx_backtest.py build   (events -> data_store/research/stock_vadx_events.csv.gz)
     python3 scripts/research/stock_volume_adx_backtest.py grid
"""
from __future__ import annotations

import glob
import itertools
import os
import sys

import numpy as np
import pandas as pd

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
HIST = os.path.join(ROOT, "data_store", "research", "stocks_intraday")
EV = os.path.join(ROOT, "data_store", "research", "stock_vadx_events.csv.gz")
COST, COST_LO = 0.0012, 0.0008
EXITS = {"E1": ("bar", 1, 2.0), "E2": ("atr", 1, 2.0), "E3": ("atr", 1, 3.0), "E4": ("atr", 1, None),
         "E5": ("atr", 2, 2.0), "E6": ("atr", 3, 2.0), "E7": ("pct", 0.5, 2.0), "E8": ("pct", 0.5, None), "E9": ("pct", 1.0, None)}


def wilder(x, n=14):
    return x.ewm(alpha=1 / n, adjust=False).mean()


def adx_di(b):
    up, dn = b.h.diff(), -b.l.diff()
    pdm = pd.Series(np.where((up > dn) & (up > 0), up, 0.0), index=b.index)
    mdm = pd.Series(np.where((dn > up) & (dn > 0), dn, 0.0), index=b.index)
    tr = pd.concat([b.h - b.l, (b.h - b.c.shift()).abs(), (b.l - b.c.shift()).abs()], axis=1).max(axis=1)
    atr = wilder(tr); pdi = 100 * wilder(pdm) / atr; mdi = 100 * wilder(mdm) / atr
    return wilder(100 * (pdi - mdi).abs() / (pdi + mdi)), pdi, mdi, atr


def agg(d, width):
    d = d.assign(slot=d.mn // width)
    b = d.groupby(["day", "slot"], sort=True).agg(o=("open", "first"), h=("high", "max"), l=("low", "min"), c=("close", "last"), v=("volume", "sum")).reset_index()
    return b


def build_one(path):
    sym = os.path.basename(path)[:-10]
    d = pd.read_csv(path, parse_dates=["ts"]).drop_duplicates("ts").sort_values("ts")
    d["day"] = d.ts.dt.strftime("%Y-%m-%d"); d["mn"] = d.ts.dt.hour * 60 + d.ts.dt.minute - 555
    d = d[(d.mn >= 0) & (d.mn < 375)]
    days = sorted(d.day.unique())
    b = agg(d, 5); b15 = agg(d, 15)
    b["adx"], b["pdi"], b["mdi"], b["atr"] = adx_di(b)
    a15, p15, m15, _ = adx_di(b15); b15["adx15"], b15["pdi15"], b15["mdi15"] = a15, p15, m15
    # normal volume per slot (previous 10 sessions) and day volume so far vs normal
    pv = b.pivot(index="day", columns="slot", values="v").reindex(index=days)
    norm = pv.shift(1).rolling(10, min_periods=5).median()
    cum = pv.fillna(0).cumsum(axis=1); cnorm = cum.shift(1).rolling(10, min_periods=5).mean()
    b["spike"] = [pv.at[dd, s] / norm.at[dd, s] if norm.at[dd, s] else np.nan for dd, s in zip(b.day, b.slot)]
    b["rvol"] = [cum.at[dd, s] / cnorm.at[dd, s] if cnorm.at[dd, s] else np.nan for dd, s in zip(b.day, b.slot)]
    # last completed 15-min bar at the 5-min bar's close
    k15 = b15.set_index(["day", "slot"])
    m1 = {dd: g for dd, g in d.groupby("day")}
    dayhl = d.groupby("day").agg(H=("high", "max"), L=("low", "min"))
    ev = []
    for i, dd in enumerate(days):
        if i == 0:
            continue
        x = b[b.day == dd].reset_index(drop=True)
        if len(x) < 70:
            continue
        g = m1[dd]; mn = g.mn.values; O, H, L, C = g.open.values, g.high.values, g.low.values, g.close.values
        lv = {"PDH": dayhl.at[days[i - 1], "H"], "PDL": dayhl.at[days[i - 1], "L"], "ORH": x.h[:3].max(), "ORL": x.l[:3].min()}
        done = set(); X = [type("B", (), z) for z in x.to_dict("records")]
        for j in range(3, min(len(x) - 1, 63)):
            r, p = X[j], X[j - 1]
            for name, L0 in lv.items():
                fam = "pd" if name.startswith("PD") else "or"
                cands = []
                if p.c <= L0 < r.c: cands.append(("break", 1))
                if p.c >= L0 > r.c: cands.append(("break", -1))
                if p.c > L0 * 1.001 and r.l <= L0 * 1.001 and r.c > L0: cands.append(("hold", 1))
                if p.c < L0 * 0.999 and r.h >= L0 * 0.999 and r.c < L0: cands.append(("hold", -1))
                for setup, sd in cands:
                    key = (name, setup, sd)
                    if key in done:
                        continue
                    done.add(key)
                    t_in = (r.slot + 1) * 5
                    ii = np.searchsorted(mn, t_in); iend = np.searchsorted(mn, 360)
                    if ii >= len(mn) or mn[ii] >= 360 or ii >= iend:
                        continue
                    e = O[ii]; atr = r.atr
                    q15 = int(((r.slot + 1) * 5) // 15) - 1
                    f15 = k15.loc[(dd, q15)] if (dd, q15) in k15.index else None
                    row = {"sym": sym, "day": dd, "slot": int(r.slot),
                           "level": name, "fam": fam, "setup": setup, "side": sd, "entry": e, "spike": r.spike, "rvol": r.rvol,
                           "adx": r.adx, "di_with": int((r.pdi > r.mdi) == (sd > 0)), "adx15": None if f15 is None else f15.adx15,
                           "di15_with": None if f15 is None else int((f15.pdi15 > f15.mdi15) == (sd > 0)), "atr_pct": atr / e * 100}
                    hh, ll, cc = H[ii:iend], L[ii:iend], C[ii:iend]
                    for ex, (stk, mult, tgt) in EXITS.items():
                        dist = (max((e - r.l) if sd > 0 else (r.h - e), e * 0.0015) if stk == "bar" else max(mult * atr, e * 0.0015) if stk == "atr"
                                else e * mult / 100)
                        stop = e - sd * dist; tp = None if tgt is None else e + sd * tgt * dist
                        hit_s = np.where((ll <= stop) if sd > 0 else (hh >= stop))[0]
                        hit_t = np.array([]) if tp is None else np.where((hh >= tp) if sd > 0 else (ll <= tp))[0]
                        s0 = hit_s[0] if len(hit_s) else 10**9; t0 = hit_t[0] if len(hit_t) else 10**9
                        px = stop if s0 <= t0 and s0 < 10**9 else tp if t0 < 10**9 else cc[-1]
                        row[f"{ex}_r"] = sd * (px - e) / dist; row[f"{ex}_c"] = e / dist        # cost in R = cost_frac x e / dist
                    ev.append(row)
    return ev


def build():
    ev = []
    for p in sorted(glob.glob(os.path.join(HIST, "*_1m.csv.gz"))):
        try:
            ev += build_one(p)
        except Exception as ex:
            print("skip", p, ex)
    E = pd.DataFrame(ev); E.to_csv(EV, index=False)
    print("events", len(E), "days", E.day.nunique(), "stocks", E.sym.nunique())


def stats(x, ex, cost=COST):
    if not len(x):
        return None
    r = x[f"{ex}_r"] - cost * x[f"{ex}_c"]
    return {"n": len(x), "win%": round(100 * (r > 0).mean()), "R_net": round(r.mean(), 3), "R_gross": round(x[f"{ex}_r"].mean(), 3),
            "pct_net": round((r / x[f"{ex}_c"]).mean() * 100, 3)}


def grid():
    E = pd.read_csv(EV)
    days = sorted(E.day.unique()); cut = days[int(len(days) * 2 / 3)]
    E["part"] = np.where(E.day < cut, "train", "test")
    print(f"events {len(E)} | {E.sym.nunique()} stocks | days {days[0]}..{days[-1]} ({len(days)}) | train < {cut} <= test\n")
    SET = {"break pd": (E.setup == "break") & (E.fam == "pd"), "break or": (E.setup == "break") & (E.fam == "or"), "break any": E.setup == "break",
           "hold pd": (E.setup == "hold") & (E.fam == "pd"), "hold or": (E.setup == "hold") & (E.fam == "or"), "hold any": E.setup == "hold"}
    SPK = {f"spike>={k}": E.spike >= k for k in (2, 4, 6, 8)}; SPK["any vol"] = E.spike.notna() | True
    ADX = {"no adx": E.adx.notna() | True, "adx5>25": E.adx > 25, "adx5>20 with DI": (E.adx > 20) & (E.di_with == 1),
           "adx5>25 with DI": (E.adx > 25) & (E.di_with == 1), "adx5>30 with DI": (E.adx > 30) & (E.di_with == 1),
           "adx15>25 with DI": (E.adx15 > 25) & (E.di15_with == 1), "adx5<20": E.adx < 20}
    RV = {"any rvol": E.rvol.notna() | True, "rvol>=1.5": E.rvol >= 1.5, "rvol>=2": E.rvol >= 2}
    res = []
    for (sn, sm), (kn, km), (an, am), (rn, rm), ex in itertools.product(SET.items(), SPK.items(), ADX.items(), RV.items(), EXITS):
        m = sm & km & am & rm
        tr = stats(E[m & (E.part == "train")], ex); te = stats(E[m & (E.part == "test")], ex)
        if tr and tr["n"] >= 40:
            res.append({"cond": f"{sn} | {kn} | {an} | {rn} | {ex}", "tr_n": tr["n"], "tr_R": tr["R_net"], "tr_win": tr["win%"],
                        "te_n": te["n"] if te else 0, "te_R": te["R_net"] if te else np.nan, "te_win": te["win%"] if te else np.nan,
                        "te_Rg": te["R_gross"] if te else np.nan, "te_pct": te["pct_net"] if te else np.nan})
    R = pd.DataFrame(res).sort_values("tr_R", ascending=False)
    R.to_csv(os.path.join(ROOT, "data_store", "research", "stock_vadx_grid.csv"), index=False)
    pd.set_option("display.width", 250); pd.set_option("display.max_colwidth", 70)
    print(f"combinations with 40+ train trades: {len(R)} | positive in train: {(R.tr_R > 0).sum()} | positive in BOTH: {((R.tr_R > 0) & (R.te_R > 0) & (R.te_n >= 20)).sum()}")
    print("\nTOP 25 chosen on TRAIN, then their TEST (unseen) result  (R after 0.12 % costs):")
    print(R.head(25).to_string(index=False))
    both = R[(R.tr_R > 0) & (R.te_R > 0) & (R.te_n >= 25)].copy()
    both["min_R"] = both[["tr_R", "te_R"]].min(axis=1)
    print("\nPOSITIVE IN BOTH PARTS (test n >= 25), by the weaker of the two:")
    print(both.sort_values("min_R", ascending=False).head(25).to_string(index=False))
    print("\nBASELINES (all days):")
    for nm, m in (("all breaks", SET["break any"]), ("all holds", SET["hold any"]), ("hold any | spike>=4 | adx5>25 with DI", SET["hold any"] & SPK["spike>=4"] & ADX["adx5>25 with DI"]),
                  ("break any | spike>=4 | adx5>25 with DI", SET["break any"] & SPK["spike>=4"] & ADX["adx5>25 with DI"])):
        for ex in EXITS:
            s, s2 = stats(E[m], ex), stats(E[m], ex, COST_LO)
            print(f"  {nm:42s} {ex}: {s} | at 0.08 % cost R {s2['R_net'] if s2 else None}")


if __name__ == "__main__":
    build() if (sys.argv[1:] or ["build"])[0] == "build" else grid()
