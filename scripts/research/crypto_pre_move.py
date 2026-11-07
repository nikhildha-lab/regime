"""WHAT HAPPENS BEFORE BIG CRYPTO MOVES? (2026-11-07, Nikhil: "fetch all possible 30 day data and find this theory is true or not ..
identify related patterns before big moves").

Data: data_store/research/deriv/ (app/derivhist.py) - 15 Binance perpetuals.
PART 1 (last 30 days, 15-min): CVD, open interest, top-trader and all-account long / short ratios, taker buy / sell ratio, funding.
  Big move = a 4-hour return in the top 5 % of that coin's absolute 4-hour returns, first bar of each (no overlap: 12 h apart).
  For every bar we measure the "before" picture over the previous 12 h (and 24 h for the range / OI build):
     price change, CVD change (per unit of volume), OI change, the 24 h range vs normal, global long / short level vs the coin's own
     30-day median, top-trader long / short change, taker buy / sell, funding.
  Then:
   a) feature averages right before big DOWN moves, big UP moves, and all other bars (baseline)
   b) each "theory" as a rule -> how often a big move follows within 24 h, which way, and the average next-24 h return, vs baseline
      T1 bearish CVD divergence: price up over 12 h, CVD down
      T2 bullish CVD divergence: price down, CVD up
      T3 OI build in a range: 24 h range in the coin's lowest 30 %, OI +3 % or more over 24 h
      T4 = T3 + crowd long (global L/S above its 30-day median) + top traders trimming (top L/S down over 24 h)   <- Nikhil's SOL reading
      T5 = T3 + crowd short (global L/S below median) + top traders adding (top L/S up)
      T6 funding high (top 20 % for the coin) + OI rising
  Sample = 15 coins x 30 days; overlapping coins move together (BTC drives them), so counts are not independent - read with care.
PART 2 (2 years, 1-hour, CVD only - the only series Binance keeps that long): T1 / T2 on 12-hour windows -> next 12 / 24 / 48 h return,
  both halves, all coins pooled.
Run: python3 scripts/research/crypto_pre_move.py
"""
from __future__ import annotations

import glob
import json
import os

import numpy as np
import pandas as pd

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
D = os.path.join(ROOT, "data_store", "research", "deriv")
OUT = os.path.join(D, "results")


def coin_frame(c):
    k = pd.read_csv(os.path.join(D, f"kl15_{c}.csv")); d = pd.read_csv(os.path.join(D, f"der15_{c}.csv"))
    x = k.merge(d, on="ts", how="left").sort_values("ts").reset_index(drop=True)
    x["delta"] = 2 * x.taker_buy - x.vol; x["cvd"] = x.delta.cumsum()
    for col in ("oi", "top_pos", "glob_acc", "taker_bs"):
        x[col] = pd.to_numeric(x[col], errors="coerce").ffill()
    try:
        f = pd.read_csv(os.path.join(D, f"fund_{c}.csv")).sort_values("ts")
        x = pd.merge_asof(x, f.rename(columns={"rate": "fund"}), on="ts", direction="backward")
    except Exception:
        x["fund"] = np.nan
    x["coin"] = c
    n12, n24, n4 = 48, 96, 16
    x["ret_pre12"] = x.c / x.c.shift(n12) - 1
    vol12 = x.vol.rolling(n12).sum()
    x["cvd_pre12"] = (x.cvd - x.cvd.shift(n12)) / vol12                       # net taker buying as a share of the 12-h volume
    x["oi_pre24"] = x.oi / x.oi.shift(n24) - 1
    x["oi_pre12"] = x.oi / x.oi.shift(n12) - 1
    rng = (x.h.rolling(n24).max() - x.l.rolling(n24).min()) / x.c
    x["range24"] = rng; x["range_pct"] = rng.rank(pct=True)
    x["glob_rel"] = x.glob_acc / x.glob_acc.median() - 1
    x["top_chg24"] = x.top_pos - x.top_pos.shift(n24)
    x["taker12"] = x.taker_bs.rolling(n12).mean()
    x["fund_pct"] = x.fund.rank(pct=True)
    x["fwd4"] = x.c.shift(-n4) / x.c - 1
    x["fwd24"] = x.c.shift(-n24) / x.c - 1
    thr = x.fwd4.abs().quantile(0.95)
    big, last = np.zeros(len(x), dtype=int), -10**9
    for i in np.where(x.fwd4.abs() >= thr)[0]:
        if i - last >= 48:
            big[i] = 1 if x.fwd4.iloc[i] > 0 else -1; last = i
    x["big"] = big
    # did a big move START within the next 24 h? (+1 up first, -1 down first, 0 none)
    nxt = np.zeros(len(x), dtype=int); idx = np.where(big != 0)[0]; j = 0
    for i in range(len(x)):
        while j < len(idx) and idx[j] <= i:
            j += 1
        if j < len(idx) and idx[j] - i <= n24:
            nxt[i] = big[idx[j]]
    x["big_next24"] = nxt
    return x


def rules(x):
    T = {"T1 bear CVD divergence (12h: price up, CVD down)": (x.ret_pre12 > 0.005) & (x.cvd_pre12 < -0.02),
         "T2 bull CVD divergence (12h: price down, CVD up)": (x.ret_pre12 < -0.005) & (x.cvd_pre12 > 0.02),
         "T3 OI build in a tight range (24h)": (x.range_pct <= 0.30) & (x.oi_pre24 >= 0.03),
         "T4 T3 + crowd long + top traders trimming": (x.range_pct <= 0.30) & (x.oi_pre24 >= 0.03) & (x.glob_rel > 0) & (x.top_chg24 < 0),
         "T5 T3 + crowd short + top traders adding": (x.range_pct <= 0.30) & (x.oi_pre24 >= 0.03) & (x.glob_rel < 0) & (x.top_chg24 > 0),
         "T6 funding high (top 20%) + OI rising 12h": (x.fund_pct >= 0.8) & (x.oi_pre12 > 0.01)}
    return T


def part1():
    coins = sorted(os.path.basename(f)[6:-4] for f in glob.glob(os.path.join(D, "der15_*.csv")))
    X = pd.concat([coin_frame(c) for c in coins], ignore_index=True)
    X = X.dropna(subset=["fwd24", "ret_pre12", "cvd_pre12", "oi_pre24"])
    feats = ["ret_pre12", "cvd_pre12", "oi_pre12", "oi_pre24", "range_pct", "glob_rel", "top_chg24", "taker12", "fund_pct"]
    grp = X.big.map({1: "before big UP", -1: "before big DOWN", 0: "other bars"})
    A = X.groupby(grp)[feats].mean().round(4); A["n"] = X.groupby(grp).size()
    print(f"PART 1 - {len(coins)} coins, {X.ts.min() and pd.to_datetime(X.ts.min(), unit='ms'):%d %b} - {pd.to_datetime(X.ts.max(), unit='ms'):%d %b}, {len(X)} bars")
    print("\nAverage 'before' picture at the start of big 4-hour moves (top 5 % per coin):\n", A.to_string())
    base_any = (X.big_next24 != 0).mean(); base_dn = (X.big_next24 == -1).mean(); base_up = (X.big_next24 == 1).mean(); base_r = X.fwd24.mean()
    R = []
    for nm, m in rules(X).items():
        s = X[m]
        if len(s) < 20:
            R.append({"rule": nm, "bars": len(s), "coins": s.coin.nunique()}); continue
        days = s.assign(day=pd.to_datetime(s.ts, unit="ms").dt.date).groupby(["coin", "day"]).size().shape[0]
        R.append({"rule": nm, "bars": len(s), "coin_days": days, "coins": s.coin.nunique(),
                  "big_move_24h_%": round(100 * (s.big_next24 != 0).mean(), 1), "base_%": round(100 * base_any, 1),
                  "big_DOWN_%": round(100 * (s.big_next24 == -1).mean(), 1), "big_UP_%": round(100 * (s.big_next24 == 1).mean(), 1),
                  "fwd24_avg_%": round(100 * s.fwd24.mean(), 2), "base_fwd24_%": round(100 * base_r, 2),
                  "fwd24_up_share_%": round(100 * (s.fwd24 > 0).mean(), 1)})
    R = pd.DataFrame(R)
    print(f"\nRules -> what followed in the next 24 h (baseline: big move starts within 24 h on {100*base_any:.1f}% of bars - down {100*base_dn:.1f}%, up {100*base_up:.1f}%):\n", R.to_string(index=False))
    return A, R


def part2():
    out = []
    for f in sorted(glob.glob(os.path.join(D, "kl1h_*.csv"))):
        c = os.path.basename(f)[5:-4]; x = pd.read_csv(f).sort_values("ts").reset_index(drop=True)
        x["delta"] = 2 * x.taker_buy - x.vol; x["cvd"] = x.delta.cumsum()
        x["ret12"] = x.c / x.c.shift(12) - 1; x["cvd12"] = (x.cvd - x.cvd.shift(12)) / x.vol.rolling(12).sum()
        for h in (12, 24, 48):
            x[f"f{h}"] = x.c.shift(-h) / x.c - 1
        x["coin"] = c; out.append(x.dropna())
    X = pd.concat(out, ignore_index=True)
    mid = X.ts.quantile(0.5)
    rows = []
    for nm, m in {"bear divergence (price up >0.5%, CVD < -2% of vol)": (X.ret12 > 0.005) & (X.cvd12 < -0.02),
                  "bull divergence (price down >0.5%, CVD > +2% of vol)": (X.ret12 < -0.005) & (X.cvd12 > 0.02),
                  "confirmed up (price up, CVD up)": (X.ret12 > 0.005) & (X.cvd12 > 0.02),
                  "confirmed down (price down, CVD down)": (X.ret12 < -0.005) & (X.cvd12 < -0.02),
                  "all bars": X.ret12.notna()}.items():
        s = X[m]
        r = {"signal": nm, "n": len(s)}
        for h in (12, 24, 48):
            v = s[f"f{h}"]; r[f"fwd{h}_%"] = round(100 * v.mean(), 3); r[f"t{h}"] = round(v.mean() / (v.std() / np.sqrt(len(v) / h)), 2)   # / h: overlapping windows
        r["fwd24_H1_%"] = round(100 * s[s.ts < mid].f24.mean(), 3); r["fwd24_H2_%"] = round(100 * s[s.ts >= mid].f24.mean(), 3)
        rows.append(r)
    R = pd.DataFrame(rows)
    print(f"\nPART 2 - 1-hour CVD, {X.coin.nunique()} coins, {pd.to_datetime(X.ts.min(), unit='ms'):%b %Y} - {pd.to_datetime(X.ts.max(), unit='ms'):%b %Y}, {len(X)} bars")
    print("(t uses n / horizon to allow for overlapping windows - rough)\n", R.to_string(index=False))
    return R


if __name__ == "__main__":
    os.makedirs(OUT, exist_ok=True)
    A, R1 = part1(); R2 = part2()
    json.dump({"before_moves": A.reset_index().to_dict("records"), "rules_30d": R1.to_dict("records"), "cvd_2y": R2.to_dict("records")},
              open(os.path.join(OUT, "summary.json"), "w"), indent=1, default=str)
