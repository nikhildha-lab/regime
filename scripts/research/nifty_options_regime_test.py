"""NIFTY weekly option selling + spreads, conditioned on the Options Lab readings (2026-11-08, Nikhil: "can we trade using this info ..
options, futures, MIS, spreads .. experiment deep").

Data: MASTER_BACKTESTER/data/options/chain_NIFTY.csv (NSE daily closes per strike, Oct 2023 - Oct 2026, volume, OI) and the NIFTY daily
spot (data_store/research/nifty_monthly/nifty_daily.csv, Dhan).
Trades (rules fixed before looking):
  entry   at the CLOSE, 3 sessions before each weekly expiry (the nearest expiry); only legs that traded that day (volume > 0)
  exit    held to expiry, settled at intrinsic value on the NIFTY close of expiry day. No stop (spreads cap the risk).
  STRADDLE   sell ATM call + ATM put
  STRANGLE   sell the call / put ~1 straddle-width away (K = ATM +/- straddle, nearest listed strike)
  CONDOR     STRANGLE + buy wings 2 straddle-widths away (iron condor, defined risk)
  FLY        STRADDLE + buy wings 1 straddle-width away (iron fly, defined risk)
  costs   backtester rule: per leg per side Rs 25 + 0.10 % of premium (Rs 25 = 25 / 65 points on a 65 lot); an ITM leg at expiry pays the
          exit side on its intrinsic value. Stress: 2x and 3x costs.
Readings at entry (all known at the entry close):
  IVHV    ATM IV (Black-Scholes on spot, r 6.5 %) / 20-day realised volatility of the spot
  GEX     dealer-model gamma of all listed expiries within 30 days, strikes within 5 % (calls +, puts -): positive / negative
  TERM    nearest-expiry ATM IV / next weekly's - 1  (> 0 = inverted)
  SKEW    IV of the ~3 % OTM put minus the ~3 % OTM call
Validation: first 70 % of expiries = train (any threshold is picked there: IVHV and SKEW split at the TRAIN median), last 30 % = test.
Also: daily GEX regime -> next session's NIFTY range and |move| (does positive gamma really mean smaller moves?).
Run: python3 scripts/research/nifty_options_regime_test.py
"""
from __future__ import annotations

import os
from datetime import date

import numpy as np
import pandas as pd
from scipy.special import ndtr

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
CHAIN = os.path.expanduser("~/Documents/MASTER_BACKTESTER/data/options/chain_NIFTY.csv")
if not os.path.exists(CHAIN):
    CHAIN = os.path.join(os.environ.get("HOME", ""), "mnt", "Documents", "MASTER_BACKTESTER", "data", "options", "chain_NIFTY.csv")
SPOT = os.path.join(ROOT, "data_store", "research", "nifty_monthly", "nifty_daily.csv")
R, LOT, FIX = 0.065, 65, 25 / 65
import sys
IDX = (sys.argv[1] if len(sys.argv) > 1 else "NIFTY").upper()          # 2026-11-08: also SENSEX (BSE weekly options, lot 20)
if IDX == "SENSEX":
    CHAIN = CHAIN.replace("chain_NIFTY.csv", "chain_SENSEX.csv"); LOT, FIX = 20, 25 / 20
    SPOT = CHAIN.replace(os.path.join("options", "chain_SENSEX.csv"), "SENSEX_deep_spot.csv")


def bs(S, K, T, v, call):
    d1 = (np.log(S / K) + (R + 0.5 * v * v) * T) / (v * np.sqrt(T)); d2 = d1 - v * np.sqrt(T)
    return np.where(call, S * ndtr(d1) - K * np.exp(-R * T) * ndtr(d2), K * np.exp(-R * T) * ndtr(-d2) - S * ndtr(-d1))


def iv_vec(px, S, K, T, call):
    lo, hi = np.full(len(px), 0.01), np.full(len(px), 3.0)
    for _ in range(50):
        m = (lo + hi) / 2; f = bs(S, K, T, m, call) < px; lo = np.where(f, m, lo); hi = np.where(f, hi, m)
    v = (lo + hi) / 2
    return np.where((v > 0.011) & (v < 2.99), v, np.nan)


def gamma(S, K, T, v):
    d1 = (np.log(S / K) + (R + 0.5 * v * v) * T) / (v * np.sqrt(T)); return np.exp(-0.5 * d1 * d1) / np.sqrt(2 * np.pi) / (S * v * np.sqrt(T))


def t(x):
    x = pd.Series(x).dropna(); return x.mean() / x.std() * np.sqrt(len(x)) if len(x) > 2 and x.std() else np.nan


def cost(prem, k=1.0):
    return k * (FIX + 0.001 * prem)


if __name__ == "__main__":
    C = pd.read_csv(CHAIN, usecols=["date", "expiry", "strike", "type", "close", "volume", "oi"],
                    dtype={"date": "category", "expiry": "category", "type": "category", "strike": "float32", "close": "float32", "volume": "float64", "oi": "float64"})
    C["date"] = C.date.astype(str); C["expiry"] = C.expiry.astype(str); C["type"] = C.type.astype(str)
    S = pd.read_csv(SPOT).rename(columns={"date": "day"}); S["day"] = S.day.astype(str).str[:10]; S = S.drop_duplicates("day").set_index("day").sort_index()
    spot = S.close; rv20 = np.log(spot).diff().rolling(20).std() * np.sqrt(252)
    days = sorted(set(C.date) & set(spot.index)); di = {d: i for i, d in enumerate(days)}
    C = C[C.date.isin(days)].copy(); C["S"] = C.date.map(spot); C["T"] = (pd.to_datetime(C.expiry) - pd.to_datetime(C.date)).dt.days.clip(lower=0) + 0.25
    C["T"] = C["T"] / 365; near = C[(C["T"] * 365 <= 31) & ((C.strike / C.S - 1).abs() <= 0.05) & (C.close > 0) & (C.volume > 0)].copy()
    near["iv"] = iv_vec(near.close.values, near.S.values, near.strike.values, near["T"].values, (near.type == "CE").values)
    near["g"] = gamma(near.S.values, near.strike.values, near["T"].values, near.iv.fillna(0.15).values) * near.oi * near.S ** 2 * 0.01 / 1e7
    near["dg"] = np.where(near.type == "CE", near.g, -near.g)
    G = near.groupby("date").dg.sum().rename("gex")
    # daily regime -> next day's range
    nx = pd.DataFrame({"gex": G}).join(S[["open", "high", "low", "close"]])
    nx["rng_next"] = ((S.high - S.low) / S.close.shift(1) * 100).shift(-1).reindex(nx.index)
    nx["mv_next"] = (np.log(S.close).diff().abs() * 100).shift(-1).reindex(nx.index)
    print("1. DAILY GAMMA REGIME -> NEXT SESSION (NIFTY)")
    for nm, m in (("positive dealer gamma", nx.gex > 0), ("negative dealer gamma", nx.gex <= 0)):
        x = nx[m]; print(f"   {nm:24s} n {len(x):4d} | next-day range {x.rng_next.mean():.2f}% | next-day |move| {x.mv_next.mean():.2f}%")
    q = pd.qcut(nx.gex, 5, labels=False)
    print("   by gamma quintile (1 = most negative): range " + " ".join(f"{nx.rng_next[q == k].mean():.2f}" for k in range(5)) + " | |move| " + " ".join(f"{nx.mv_next[q == k].mean():.2f}" for k in range(5)))
    # weekly trades
    rows = []
    for E in sorted(C.expiry.unique()):
        if E not in di or di[E] < 3:
            continue
        e = days[di[E] - 3]
        X = C[(C.date == e) & (C.expiry == E)]
        if not len(X) or e not in spot.index:
            continue
        S0, ST = spot[e], spot[E]; ce = X[X.type == "CE"].set_index("strike"); pe = X[X.type == "PE"].set_index("strike")
        ks = sorted(set(ce.index) & set(pe.index));
        if not ks:
            continue
        k0 = min(ks, key=lambda z: abs(z - S0))
        if not (ce.volume.get(k0, 0) > 0 and pe.volume.get(k0, 0) > 0):
            continue
        st = float(ce.close[k0] + pe.close[k0]); T = ((date.fromisoformat(E) - date.fromisoformat(e)).days + 0.25) / 365
        ivs = iv_vec(np.array([ce.close[k0], pe.close[k0]]), np.array([S0, S0]), np.array([k0, k0]), np.array([T, T]), np.array([True, False]))
        iv = np.nanmean(ivs)
        nxt = [z for z in sorted(C[C.date == e].expiry.unique()) if z > E]
        term = np.nan
        if nxt:
            Y = C[(C.date == e) & (C.expiry == nxt[0])]; cy = Y[Y.type == "CE"].set_index("strike").close; py = Y[Y.type == "PE"].set_index("strike").close
            if k0 in cy and k0 in py:
                T2 = ((date.fromisoformat(nxt[0]) - date.fromisoformat(e)).days + 0.25) / 365
                iv2 = np.nanmean(iv_vec(np.array([cy[k0], py[k0]]), np.array([S0, S0]), np.array([k0, k0]), np.array([T2, T2]), np.array([True, False])))
                term = iv / iv2 - 1
        kp3 = min(ks, key=lambda z: abs(z - S0 * 0.97)); kc3 = min(ks, key=lambda z: abs(z - S0 * 1.03))
        sk = iv_vec(np.array([pe.close[kp3], ce.close[kc3]]), np.array([S0, S0]), np.array([kp3, kc3]), np.array([T, T]), np.array([False, True]))
        pick = lambda tgt: min(ks, key=lambda z: abs(z - tgt))
        legs = {"STRADDLE": [(k0, "CE", -1), (k0, "PE", -1)],
                "STRANGLE": [(pick(k0 + st), "CE", -1), (pick(k0 - st), "PE", -1)],
                "CONDOR": [(pick(k0 + st), "CE", -1), (pick(k0 - st), "PE", -1), (pick(k0 + 2 * st), "CE", 1), (pick(k0 - 2 * st), "PE", 1)],
                "FLY": [(k0, "CE", -1), (k0, "PE", -1), (pick(k0 + st), "CE", 1), (pick(k0 - st), "PE", 1)]}
        r = {"entry": e, "expiry": E, "S0": S0, "ST": ST, "straddle": st, "iv": iv, "rv20": rv20.get(e), "ivhv": iv / rv20.get(e) if rv20.get(e) else np.nan,
             "gex": G.get(e, np.nan), "term": term, "skew": (sk[0] - sk[1]) if np.isfinite(sk).all() else np.nan, "move": (ST / S0 - 1) * 100}
        for nm, L in legs.items():
            g = c1 = 0.0; ok = True
            for k, cp, q_ in L:
                px = (ce if cp == "CE" else pe).close.get(k)
                if px is None or px <= 0:
                    ok = False; break
                intr = max(0.0, ST - k) if cp == "CE" else max(0.0, k - ST)
                g += q_ * (intr - px)                                        # short (q -1): px - intr ; long (q +1): intr - px
                c1 += cost(px) + (cost(intr) if intr > 0 else 0)
            r[nm] = g if ok else np.nan; r[nm + "_c"] = c1 if ok else np.nan
        rows.append(r)
    T_ = pd.DataFrame(rows).dropna(subset=["STRADDLE"]); n = len(T_); cut = T_.expiry.iloc[int(n * 0.7)]
    tr = T_[T_.expiry < cut]; te = T_[T_.expiry >= cut]
    ivm, skm = tr.ivhv.median(), tr["skew"].median()
    print(f"\n2. WEEKLY OPTION SELLING · {n} expiries {T_.expiry.iloc[0]} .. {T_.expiry.iloc[-1]} · train < {cut} · IV/HV train median {ivm:.2f}, skew train median {skm:.2f}")
    print("   points per lot-unit after costs (x65 = rupees per lot) | win | worst | train / test | 2x and 3x costs\n")
    def show(d, lab, nm):
        x = d[nm] - d[nm + "_c"]
        if len(x) < 5:
            return
        a, b = x[d.expiry < cut], x[d.expiry >= cut]
        x2, x3 = d[nm] - 2 * d[nm + "_c"], d[nm] - 3 * d[nm + "_c"]
        best3 = x.sort_values().iloc[:-3].mean() if len(x) > 10 else np.nan
        yrs = " ".join(f"{y}:{v:+.0f}" for y, v in x.groupby(d.expiry.str[:4]).sum().items())
        print(f"   {nm:8s} {lab:34s} n {len(x):3d} avg {x.mean():+6.1f} (t {t(x):+.1f}) tot {x.sum():+7.0f} win {100*(x>0).mean():3.0f}% worst {x.min():+7.1f} "
              f"| train {a.mean():+6.1f} test {b.mean():+6.1f} | 2x {x2.mean():+6.1f} 3x {x3.mean():+6.1f} | -best3 {best3:+6.1f} | {yrs}")
    for nm in ("STRADDLE", "STRANGLE", "CONDOR", "FLY"):
        show(T_, "all weeks", nm)
        show(T_[T_.ivhv > ivm], "IV/HV above train median", nm); show(T_[T_.ivhv <= ivm], "IV/HV below", nm)
        show(T_[T_.gex > 0], "positive gamma at entry", nm); show(T_[T_.gex <= 0], "negative gamma at entry", nm)
        show(T_[T_.term > 0], "term inverted", nm); show(T_[T_.term <= 0], "term normal", nm)
        show(T_[T_["skew"] > skm], "skew above train median", nm); show(T_[T_["skew"] <= skm], "skew below", nm)
        show(T_[(T_.ivhv > ivm) & (T_.gex > 0)], "IV/HV high AND positive gamma", nm)
        print()
    T_.to_csv(os.path.join(ROOT, "data_store", "research", f"{IDX.lower()}_options_regime_trades.csv"), index=False)
