"""QUANTPEDIA SWEEP 2 (2026-11-10, Nikhil: "extract more strategies"): new candidates from the Quantpedia blog, the QuantConnect strategy
library and our own list, tested on the same engine and honesty checks as quantpedia_sweep.py (decide at a close, earn the next period,
costs per side on every change, Sharpe overall and in both halves, benchmark). Rules as published, adapted only where our data differs.
  1. Sectoral intramonth momentum cycle   sector ETFs: top-3 by 252-d return long on the month's first session; days 2-3 the reverse (bottom 3 long / top 3 short)
  2. Correlation acceleration hedge       SPY, out of the market when the 20-d average pairwise correlation of the 9 sector ETFs rose > 0.15 over 10 sessions
  3. Overnight anomaly                    SPY and ES: long close -> next open every day (vs day session open -> close)
  4. VIX percentile                       SPX: long when VIX is in the top 20 % of its trailing 2-year range, short when in the bottom 20 %
  5. Same-calendar-month seasonality      SPX, Nifty: invested in months whose average return over the previous 10 years was positive
  6. FX 12-month momentum                 23 pairs vs USD: long the 3 strongest, short the 3 weakest, monthly
  7. Crypto funding carry                 15 Binance perps, 2 y: short the 3 highest-funding coins, long the 3 lowest, weekly (funding earned as carry)
  8. Crypto day-of-week                   BTC / ETH / SOL: long only on the historically best weekday(s), chosen on the first half only
  9. Sector-ETF pairs                     3 closest pairs by sum of squared deviations (12 m), z-score entry 2 / exit 0.5, rolling
 10. Nifty weekly-expiry day              long / short Nifty on Thursday (weekly expiry day), vs other days
Output: data_store/research/quantpedia_sweep2.csv (+ printed table).  Run: python3 scripts/research/quantpedia_sweep2.py
"""
from __future__ import annotations

import glob
import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
import quantpedia_sweep as Q   # noqa: E402

Q.RES.clear()
OUT = os.path.join(Q.ROOT, "data_store", "research", "quantpedia_sweep2.csv")
SECT = ["XLB", "XLC", "XLE", "XLF", "XLI", "XLK", "XLP", "XLRE", "XLU", "XLV", "XLY"]


def sectors():
    out = {}
    for s in SECT:
        p = os.path.join(Q.MB, "us_etf", f"{s}_daily.csv")
        if os.path.exists(p):
            out[s] = Q.ohlc(p).close
    return pd.DataFrame(out).dropna(how="all")


def t1():
    P = sectors(); R = P.pct_change(fill_method=None); spy = Q.etf("SPY").close.pct_change().reindex(R.index)
    mom = P.pct_change(252)
    idx = R.index; first = pd.Series(idx, index=idx).groupby([idx.year, idx.month]).transform("min") == pd.Series(idx, index=idx)
    pos_in_month = pd.Series(idx, index=idx).groupby([idx.year, idx.month]).cumcount() + 1
    rk = mom.rank(axis=1, ascending=False)
    top = (rk <= 3).astype(float) / 3; bot = (rk > rk.notna().sum(axis=1).sub(3).clip(lower=0).values[:, None]).astype(float) / 3
    W = pd.DataFrame(0.0, index=idx, columns=P.columns)
    # decided at the previous close: for the first session hold top 3 long (and bottom 3 short); for sessions 2-3 the reverse
    nxt = pos_in_month.shift(-1)
    W[nxt == 1] = (top - bot)[nxt == 1]; W[(nxt == 2) | (nxt == 3)] = (bot - top)[(nxt == 2) | (nxt == 3)]
    r, e = Q.run_weights(W, R, Q.C_FUT); Q.record("Sectoral intramonth momentum cycle (L/S)", "US sector ETFs", "top 3 by 252-d return long on day 1, reverse on days 2-3", r, e, spy * 0)
    WL = W.clip(lower=0) * 2; r, e = Q.run_weights(WL, R, Q.C_FUT); Q.record("Sectoral intramonth momentum cycle (long legs only)", "US sector ETFs", "long legs only, x2", r, e, spy)


def t2():
    P = sectors(); R = P.pct_change(fill_method=None); spy = Q.etf("SPY").close.pct_change().reindex(R.index).dropna()
    def avgcorr(win):
        out = []
        for i in range(len(R)):
            if i < win: out.append(np.nan); continue
            c = R.iloc[i - win:i].corr().values; n = c.shape[0]; out.append((c.sum() - n) / (n * (n - 1)))
        return pd.Series(out, index=R.index)
    ac = avgcorr(20); acc = ac - ac.shift(10)
    for th in (0.10, 0.15, 0.25):
        pos = (acc <= th).astype(float)
        r, e = Q.run_pos(pos.shift(1), spy, Q.C_FUT); Q.record(f"Correlation acceleration hedge (>{th:.2f} in 10 d -> out)", "SPY", "out of SPY when sector correlations accelerate", r, e, spy)


def t3():
    for nm, d in (("SPY", Q.etf("SPY")), ("ES", Q.ohlc("us/ES_deep.csv"))):
        d = d[(d.open > 0) & (d.close > 0)]
        on = d.open / d.close.shift(1) - 1; day = d.close / d.open - 1; full = d.close.pct_change()
        Q.record("Overnight anomaly (close -> next open)", nm, "long every night", on - 2 * Q.C_FUT, 1.0, full)
        Q.record("Day session only (open -> close)", nm, "long every day session", day - 2 * Q.C_FUT, 1.0, full)


def t4():
    spx = Q.ohlc("us/SPX_deep.csv").close if os.path.exists(os.path.join(Q.MB, "us/SPX_deep.csv")) else Q.ohlc("us/ES_deep.csv").close
    vix = Q.ohlc("us/VIX_deep.csv").close if os.path.exists(os.path.join(Q.MB, "us/VIX_deep.csv")) else None
    if vix is None:
        for cand in glob.glob(os.path.join(Q.MB, "**", "*VIX*.csv"), recursive=True):
            try: vix = Q.ohlc(os.path.relpath(cand, Q.MB)).close; break
            except Exception: pass
    if vix is None: print("no VIX"); return
    r = spx.pct_change(); v = vix.reindex(spx.index).ffill()
    pct = v.rolling(504).apply(lambda x: (x[-1] >= x).mean(), raw=True)
    pos = pd.Series(0.0, index=spx.index); pos[pct >= 0.8] = 1.0; pos[pct <= 0.2] = -1.0
    rr, e = Q.run_pos(pos.shift(1), r, Q.C_FUT); Q.record("VIX percentile (top 20 % long / bottom 20 % short)", "S&P 500", "VIX vs its 2-year range", rr, e, r)
    posL = (pct >= 0.8).astype(float); rr, e = Q.run_pos(posL.shift(1), r, Q.C_FUT); Q.record("VIX percentile (top 20 % long only)", "S&P 500", "long only when VIX is high", rr, e, r)


def t5():
    for nm, px in (("S&P 500", (Q.ohlc("us/SPX_deep.csv").close if os.path.exists(os.path.join(Q.MB, "us/SPX_deep.csv")) else Q.ohlc("us/ES_deep.csv").close)), ("Nifty", Q.nifty().close)):
        m = px.resample(Q.MEF).last().pct_change()
        hist = {}
        pos = pd.Series(0.0, index=m.index)
        for i, dt in enumerate(m.index):
            same = m[(m.index.month == dt.month) & (m.index < dt) & (m.index >= dt - pd.DateOffset(years=10))]
            if len(same) >= 5 and same.mean() > 0: pos.iloc[i] = 1.0
        rr, e = Q.run_pos(pos, m, Q.C_FUT, per=12); Q.record("Same-calendar-month seasonality", nm, "invested when the month's 10-y average return is positive", rr, e, m, per=12)


def t6():
    fx = {}
    for f in glob.glob(os.path.join(Q.MB, "clean", "FX_*USD_1d.csv")) + glob.glob(os.path.join(Q.MB, "clean", "FX_USD*_1d.csv")):
        s = os.path.basename(f)[3:-7]; d = pd.read_csv(f, parse_dates=["datetime"]).set_index("datetime").close
        fx[s] = (1 / d) if s.startswith("USD") else d            # everything as foreign currency per USD-long = long the foreign currency
    P = pd.DataFrame(fx).dropna(how="all").ffill(); P = P[P.index >= "2005-01-01"]
    me = Q.month_ends(P.index); M = P.reindex(me); mom = M / M.shift(12) - 1
    rk = mom.rank(axis=1, ascending=False); n = mom.notna().sum(axis=1)
    L = (rk <= 3).astype(float) / 3; S = rk.gt(n - 3, axis=0).astype(float) / 3
    R = P.pct_change(fill_method=None)
    r, e = Q.run_weights(L - S, R, Q.C_FUT); Q.record("FX 12-month momentum (3 vs 3)", f"{P.shape[1]} currencies vs USD", "long 3 strongest, short 3 weakest, monthly", r, e, R.mean(axis=1) * 0)


def t7():
    cl, fd = {}, {}
    for f in glob.glob(os.path.join(Q.DER, "kl1h_*.csv")):
        c = os.path.basename(f)[5:-4]; k = pd.read_csv(f, usecols=["ts", "c"]); k["ts"] = pd.to_datetime(k.ts, unit="ms"); cl[c] = k.set_index("ts").c.resample("1D").last()
        ff = os.path.join(Q.DER, f"fund_{c}.csv")
        if os.path.exists(ff):
            g = pd.read_csv(ff); g["ts"] = pd.to_datetime(g.ts, unit="ms"); fd[c] = g.set_index("ts").rate.resample("1D").sum()     # funding per day (3 x 8 h)
    P = pd.DataFrame(cl).dropna(how="all"); F = pd.DataFrame(fd).reindex(P.index).fillna(0.0)
    R = P.pct_change(fill_method=None)
    we = P.resample("W-SUN").last().index; f7 = F.rolling(7).sum().reindex(we)
    rk = f7.rank(axis=1, ascending=False); n = f7.notna().sum(axis=1)
    S = (rk <= 3).astype(float) / 3; L = rk.gt(n - 3, axis=0).astype(float) / 3
    W = (L - S)
    held = W.reindex(R.index).ffill().shift(1).fillna(0.0)
    carry = (held * (-F)).sum(axis=1)                  # a short pays nothing and receives funding when positive; a long pays it
    r, e = Q.run_weights(W, R, Q.C_CRY); r = r + carry.reindex(r.index).fillna(0.0)
    Q.record("Crypto funding carry (short 3 highest / long 3 lowest funding, weekly)", f"{P.shape[1]} perps", "price P&L + funding received", r, e, R.mean(axis=1) * 0, per=365, note="2 years only")


def t8():
    for coin in ("BTC", "ETH", "SOL"):
        p = os.path.join(Q.MB, "crypto", f"{coin}_deep.csv")
        if not os.path.exists(p): continue
        c = Q.ohlc(p).close; r = c.pct_change().dropna(); r = r[r.index >= "2018-01-01"]
        h = len(r) // 2; a = r.iloc[:h]
        by = a.groupby(a.index.weekday).mean(); good = [d for d, v in by.items() if v > 0 and (a[a.index.weekday == d].mean() / a[a.index.weekday == d].std() * np.sqrt(len(a) / 7)) > 1.0]
        pos = pd.Series(r.index.weekday.isin(good).astype(float), index=r.index)
        rr, e = Q.run_pos(pos, r, Q.C_CRY, per=365); Q.record(f"Day-of-week (days chosen on 1st half: {good})", coin, "long only on the chosen weekdays", rr, e, r, per=365)


def t9():
    P = sectors().dropna(); R = P.pct_change(fill_method=None)
    N = P / P.iloc[0]; cols = list(P.columns); out = []
    pos = pd.DataFrame(0.0, index=P.index, columns=cols)
    yrs = sorted(set(P.index.year))
    for y in yrs[1:]:
        form = N[(N.index.year == y - 1)]; trade = P[P.index.year == y]
        if form.empty or trade.empty: continue
        ssd = []
        for i in range(len(cols)):
            for j in range(i + 1, len(cols)):
                a, b = form[cols[i]] / form[cols[i]].iloc[0], form[cols[j]] / form[cols[j]].iloc[0]
                ssd.append((((a - b) ** 2).sum(), cols[i], cols[j]))
        pairs = sorted(ssd)[:3]
        for _, a, b in pairs:
            spread = np.log(P[a]) - np.log(P[b]); mu = spread[form.index].mean(); sd = spread[form.index].std()
            z = ((spread - mu) / sd)[trade.index]; st = 0; 
            for dt, zz in z.items():
                if st == 0 and zz > 2: st = -1
                elif st == 0 and zz < -2: st = 1
                elif st != 0 and abs(zz) < 0.5: st = 0
                pos.at[dt, a] += st / 6; pos.at[dt, b] -= st / 6
    r, e = Q.run_weights(pos, R, Q.C_FUT); Q.record("Sector-ETF pairs (3 closest by SSD, z 2 / 0.5)", "US sector ETFs", "formation = previous year, trade = next year", r, e, R.mean(axis=1) * 0)


def t10():
    n = Q.nifty().close; r = n.pct_change().dropna(); r = r[r.index >= "2019-02-11"]      # weekly Nifty expiry from Feb 2019
    for wd, lab in ((3, "Thursday"),):
        pos = pd.Series((r.index.weekday == wd).astype(float), index=r.index)
        rr, e = Q.run_pos(pos, r, Q.C_IN); Q.record(f"Nifty on {lab} (weekly expiry day), long", "Nifty", "long only on expiry day", rr, e, r)
        rr, e = Q.run_pos(-pos, r, Q.C_IN); Q.record(f"Nifty on {lab} (weekly expiry day), short", "Nifty", "short only on expiry day", rr, e, r)


if __name__ == "__main__":
    for fn in (t1, t2, t3, t4, t5, t6, t7, t8, t9, t10):
        try:
            fn()
        except Exception as e:
            print(f"{fn.__name__} failed: {type(e).__name__}: {str(e)[:120]}")
    pd.DataFrame(Q.RES).to_csv(OUT, index=False); print("->", OUT, len(Q.RES), "tests")
