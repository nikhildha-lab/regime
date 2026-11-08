"""STOCK OPTIONS - is there a volatility premium to sell (or a cheap one to buy), and do the Options Lab readings pick the good months?
(2026-11-08, Nikhil: "can we trade using this info .. options, futures, MIS, spreads .. experiment deep")

Data: data_store/research/chains/ch_<day>.csv.gz (NSE bhavcopy, every F&O stock, strikes within 12 %), iv_hist (IV readings), Jul 2024 - Nov 2026.
Trades (fixed before looking):
  entry   at the CLOSE, 10 sessions before each monthly stock expiry (variant: 5 sessions); every F&O stock whose ATM call AND put both
          traded that day (volume > 0)
  exit    held to expiry, settled at intrinsic value on the stock's close of expiry day (NSE settles stock options at the close)
  SHORT STRADDLE  sell ATM call + put        SHORT STRANGLE  sell strikes ~1 straddle-width away
  IRON CONDOR     strangle + wings 2 widths away (defined risk)        LONG STRADDLE  the opposite of the short straddle
Costs (backtester rule): per leg per side Rs 25 + 0.10 % of premium (Rs 25 turned into points with today's lot size); ITM legs at expiry
pay the exit side on intrinsic. Stock options are thin, so the bid-ask spread is shown separately: 1 % and 3 % of premium per side.
Readings at entry: IV/HV (ATM IV / 20-day realised), IV rank (vs 252 sessions), term (near IV / next IV - 1), skew.
Split: first 70 % of expiries = train (median thresholds picked there), last 30 % = test. P&L in % of the stock price per trade, and
the equal-weight average per expiry (what a book of all names earns).
Run: python3 scripts/research/stock_options_vol_test.py
"""
from __future__ import annotations

import glob
import os

import numpy as np
import pandas as pd

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
RS = os.path.join(ROOT, "data_store", "research")
INDEX = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50"}


def t(x):
    x = pd.Series(x).dropna(); return x.mean() / x.std() * np.sqrt(len(x)) if len(x) > 2 and x.std() else np.nan


def lots():
    try:
        import sys
        sys.path.insert(0, os.path.join(ROOT, "app")); import fnoflow as FF
        return {s: (d["futs"][0][2] if d.get("futs") else None) for s, d in FF.universe().items()}
    except Exception:
        return {}


def ch(day):
    return pd.read_csv(os.path.join(RS, "chains", f"ch_{day}.csv.gz"))


if __name__ == "__main__":
    days = sorted(f[3:13] for f in os.listdir(os.path.join(RS, "chains")) if f.startswith("ch_"))
    di = {d: i for i, d in enumerate(days)}; LOT = lots()
    F = pd.concat([pd.read_csv(f, usecols=["day", "kind", "expiry"]) for f in sorted(glob.glob(os.path.join(RS, "fno_hist", "fut_*.csv.gz")))[::5]])
    exps = sorted(e for e in F[F.kind == "STF"].expiry.unique() if e in di)
    IV = pd.concat([pd.read_csv(f) for f in sorted(glob.glob(os.path.join(RS, "iv_hist", "iv_*.csv.gz")))], ignore_index=True).sort_values(["sym", "day"])
    g = IV.groupby("sym"); IV["rv20"] = g.und.transform(lambda s: np.log(s).diff().rolling(20).std() * np.sqrt(252) * 100)
    IV["ivhv"] = IV.near_iv / IV.rv20; IV["term"] = IV.near_iv / IV.next_iv - 1
    IV["ivrank"] = g.near_iv.transform(lambda s: s.rolling(253, min_periods=121).apply(lambda w: (w[:-1] < w[-1]).mean(), raw=True))
    IV = IV.set_index(["day", "sym"])
    rows = []
    for back in (10, 5):
        for E in exps:
            if di[E] < back:
                continue
            e = days[di[E] - back]; A = ch(e); Z = ch(E)
            A = A[(A.expiry == E) & ~A.sym.isin(INDEX)]; Z = Z[(Z.expiry == E) & ~Z.sym.isin(INDEX)]
            und_T = Z.groupby("sym").und.first()
            for sym, x in A.groupby("sym"):
                if sym not in und_T:
                    continue
                S0 = float(x.und.iloc[0]); ST = float(und_T[sym]); ce = x[x.cp == "CE"].set_index("strike"); pe = x[x.cp == "PE"].set_index("strike")
                ks = sorted(set(ce.index) & set(pe.index))
                if len(ks) < 5:
                    continue
                k0 = min(ks, key=lambda z: abs(z - S0))
                if not (ce.vol.get(k0, 0) > 0 and pe.vol.get(k0, 0) > 0 and ce.close[k0] > 0 and pe.close[k0] > 0):
                    continue
                st = float(ce.close[k0] + pe.close[k0]); pick = lambda tg: min(ks, key=lambda z: abs(z - tg)); fix = 25 / (LOT.get(sym) or 500)
                L = {"STRADDLE": [(k0, "CE", -1), (k0, "PE", -1)], "STRANGLE": [(pick(k0 + st), "CE", -1), (pick(k0 - st), "PE", -1)],
                     "CONDOR": [(pick(k0 + st), "CE", -1), (pick(k0 - st), "PE", -1), (pick(k0 + 2 * st), "CE", 1), (pick(k0 - 2 * st), "PE", 1)]}
                r = {"back": back, "entry": e, "expiry": E, "sym": sym, "S0": S0, "ST": ST, "move": (ST / S0 - 1) * 100, "st_pct": st / S0 * 100}
                try:
                    iv = IV.loc[(e, sym)]; r.update(ivhv=iv.ivhv, ivrank=iv.ivrank, term=iv.term, skew=iv["skew"])
                except KeyError:
                    pass
                for nm, legs in L.items():
                    gp = fee = prem = 0.0; ok = True
                    for k, cp, q in legs:
                        px = (ce if cp == "CE" else pe).close.get(k)
                        if px is None or px <= 0:
                            ok = False; break
                        intr = max(0.0, ST - k) if cp == "CE" else max(0.0, k - ST)
                        gp += q * (intr - px); fee += fix + 0.001 * px + ((fix + 0.001 * intr) if intr > 0 else 0); prem += px + intr
                    if ok:
                        r[nm] = gp / S0 * 100; r[nm + "_fee"] = fee / S0 * 100; r[nm + "_prem"] = prem / S0 * 100
                rows.append(r)
    T = pd.DataFrame(rows); T.to_csv(os.path.join(RS, "stock_options_vol_trades.csv"), index=False)
    for back in (10, 5):
        D = T[T.back == back].dropna(subset=["STRADDLE"]); ex = sorted(D.expiry.unique()); cut = ex[int(len(ex) * 0.7)]
        tr = D[D.expiry < cut]; med = {c: tr[c].median() for c in ("ivhv", "ivrank", "term", "skew")}
        print(f"\n===== entry {back} sessions before expiry · {len(D):,} stock-trades · {len(ex)} expiries {ex[0]} .. {ex[-1]} · train < {cut}")
        print("      train medians: " + ", ".join(f"{k} {v:.2f}" for k, v in med.items()))
        print("      % of stock price per trade after fees | spread 1 % / 3 % of premium a side | per-expiry book t | train / test | win\n")
        def show(d, lab, nm, sign=1):
            if len(d) < 30 or nm not in d:
                return
            net = sign * d[nm] - d[nm + "_fee"]; s1 = net - 0.01 * d[nm + "_prem"]; s3 = net - 0.03 * d[nm + "_prem"]
            book = net.groupby(d.expiry).mean(); a, b = book[book.index < cut], book[book.index >= cut]
            print(f"   {('LONG ' if sign < 0 else '') + nm:15s} {lab:30s} n {len(d):5d} | net {net.mean():+.2f}% | spread1 {s1.mean():+.2f}% spread3 {s3.mean():+.2f}% "
                  f"| book t {t(book):+.1f} train {a.mean():+.2f} test {b.mean():+.2f} | win {100*(net>0).mean():.0f}% | worst {net.min():+.1f}%")
        for nm in ("STRADDLE", "STRANGLE", "CONDOR"):
            show(D, "all", nm)
            show(D[D.ivhv > med["ivhv"]], "IV/HV high", nm); show(D[D.ivhv <= med["ivhv"]], "IV/HV low", nm)
            show(D[D.ivrank > med["ivrank"]], "IV rank high", nm); show(D[D.ivrank <= med["ivrank"]], "IV rank low", nm)
            show(D[D.term > med["term"]], "term high (near priced up)", nm); show(D[D.term <= med["term"]], "term low", nm)
            show(D[D["skew"] > med["skew"]], "skew high", nm); show(D[D["skew"] <= med["skew"]], "skew low", nm)
            print()
        show(D, "all", "STRADDLE", -1); show(D[D.ivhv <= med["ivhv"]], "IV/HV low (cheap)", "STRADDLE", -1)
        show(D[(D.ivhv <= med["ivhv"]) & (D.ivrank <= med["ivrank"])], "IV/HV low AND rank low", "STRADDLE", -1)
