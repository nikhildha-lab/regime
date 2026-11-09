"""SWEEP 3 - more public, rule-based strategies our data can test (2026-11-10, Nikhil: "can we add more"). Classics from the literature
and the quant blogs that the two Quantpedia sweeps did not cover, same engine and honesty checks (quantpedia_sweep.py).
  1. Dual momentum (GEM-lite)      monthly: SPY vs QQQ by 12-m return; hold the winner if its 12-m return > TLT's, else TLT
  2. Golden cross 50/200            daily: long when the 50-d SMA > 200-d SMA   (S&P 500 1990+, Nifty 2014+, BTC 2017+, gold 2000+)
  3. Connors RSI(2)                 long when RSI(2) < 10 and close > 200-d SMA; exit when RSI(2) > 70  (S&P 500, Nifty, BTC)
  4. VIX spike mean reversion       long S&P when VIX > 1.3 x its 20-d average; out when it is back below  (1990+)
  5. Gold trend + dollar filter     long gold when above its 200-d SMA and DXY below its 50-d SMA (either alone shown too)
  6. Crude inventory day            long / short WTI only on Wednesdays (EIA report day), 2001+
  7. Nifty vs Bank Nifty rotation   monthly: hold the index with the better 6-m return (if > 0), else cash
  8. Nifty 500 industry rotation    monthly: equal-weight industries (Nifty 500 list), hold the top 3 by 6-m return
  9. Momentum + market filter       Nifty 500 12-1 top decile only when Nifty is above its 200-d SMA, else cash
 10. Momentum + low-vol double sort Nifty 500: top-30 % momentum AND bottom-30 % volatility, equal weight, monthly
 11. Altcoin breadth rotation       15 perps, 2 y: long the 5 strongest 4-week alts when > 60 % of coins are above their 50-d SMA, else BTC only
 12. Nifty / Bank Nifty ratio MR    long the cheap one / short the rich one when the 60-d z-score of the log ratio passes 2, exit at 0
Output: data_store/research/quantpedia_sweep3.csv.  Run: python3 scripts/research/quantpedia_sweep3.py
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
import quantpedia_sweep as Q   # noqa: E402

Q.RES.clear()
OUT = os.path.join(Q.ROOT, "data_store", "research", "quantpedia_sweep3.csv")


def spx():
    return Q.ohlc("us/SPX_cash_deep.csv").close


def t1():
    spy, qqq, tlt = Q.etf("SPY").close, Q.etf("QQQ").close, Q.etf("TLT").close
    P = pd.concat([spy.rename("SPY"), qqq.rename("QQQ"), tlt.rename("TLT")], axis=1).dropna(); me = Q.month_ends(P.index); M = P.reindex(me)
    m12 = M / M.shift(12) - 1; W = pd.DataFrame(0.0, index=me, columns=P.columns)
    for dt in me:
        r = m12.loc[dt]
        if r.isna().any(): continue
        eq = "SPY" if r.SPY >= r.QQQ else "QQQ"
        W.at[dt, eq if r[eq] > r.TLT else "TLT"] = 1.0
    r, e = Q.run_weights(W, P.pct_change(), Q.C_FUT); Q.record("Dual momentum (GEM-lite: SPY / QQQ vs TLT)", "US ETFs", "12-m return, monthly", r, e, P.SPY.pct_change())


def t2():
    for nm, c, per, cost in (("S&P 500", spx(), 252, Q.C_FUT), ("Nifty", Q.nifty().close, 252, Q.C_IN), ("BTC", Q.ohlc("crypto/BTC_deep.csv").close, 365, Q.C_CRY), ("Gold", Q.ohlc("us/GOLD_COMEX_deep.csv").close, 252, Q.C_FUT)):
        c = c[c > 0]; r = c.pct_change(); pos = (c.rolling(50).mean() > c.rolling(200).mean()).astype(float)
        rr, e = Q.run_pos(pos.shift(1), r, cost, per); Q.record("Golden cross 50/200 (long only)", nm, "long while SMA50 > SMA200", rr, e, r, per)


def rsi(c, n=2):
    d = c.diff(); up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean(); dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + up / dn.replace(0, np.nan))


def t3():
    for nm, c, per, cost in (("S&P 500", spx(), 252, Q.C_FUT), ("Nifty", Q.nifty().close, 252, Q.C_IN), ("BTC", Q.ohlc("crypto/BTC_deep.csv").close, 365, Q.C_CRY)):
        c = c[c > 0]; r = c.pct_change(); R2 = rsi(c); sma = c.rolling(200).mean()
        pos = np.zeros(len(c)); st = 0
        for i in range(len(c)):
            if st == 0 and R2.iloc[i] < 10 and c.iloc[i] > sma.iloc[i]: st = 1
            elif st == 1 and R2.iloc[i] > 70: st = 0
            pos[i] = st
        rr, e = Q.run_pos(pd.Series(pos, index=c.index).shift(1), r, cost, per); Q.record("Connors RSI(2) < 10 above 200-SMA, exit RSI > 70", nm, "mean reversion in an uptrend", rr, e, r, per)


def t4():
    c = spx(); v = Q.ohlc("us/VIX_cash_deep.csv").close.reindex(c.index).ffill(); r = c.pct_change()
    pos = np.zeros(len(c)); st = 0; ma = v.rolling(20).mean()
    for i in range(len(c)):
        if st == 0 and v.iloc[i] > 1.3 * ma.iloc[i]: st = 1
        elif st == 1 and v.iloc[i] < ma.iloc[i]: st = 0
        pos[i] = st
    rr, e = Q.run_pos(pd.Series(pos, index=c.index).shift(1), r, Q.C_FUT); Q.record("VIX spike mean reversion (VIX > 1.3x its 20-d avg -> long)", "S&P 500", "out when VIX is back below its average", rr, e, r)


def t5():
    g = Q.ohlc("us/GOLD_COMEX_deep.csv").close; g = g[g.index >= "2001-01-01"]; r = g.pct_change()
    dxy = pd.read_csv(os.path.join(Q.MB, "clean", "IDX_DXY_1d.csv"), parse_dates=["datetime"]).set_index("datetime").close.reindex(g.index).ffill()
    tr = (g > g.rolling(200).mean()).astype(float); dw = (dxy < dxy.rolling(50).mean()).astype(float)
    for nm, pos in (("Gold above 200-SMA", tr), ("Gold above 200-SMA AND dollar below its 50-SMA", tr * dw), ("Dollar below its 50-SMA only", dw)):
        rr, e = Q.run_pos(pos.shift(1), r, Q.C_FUT); Q.record(nm, "Gold", "long only", rr, e, r)


def t6():
    c = Q.ohlc("us/CRUDE_WTI_deep.csv").close; c = c[(c > 0) & (c.index >= "2001-01-01")]; r = c.pct_change().dropna(); r = r[r.abs() < 0.3]
    pos = pd.Series((r.index.weekday == 2).astype(float), index=r.index)
    rr, e = Q.run_pos(pos, r, Q.C_FUT); Q.record("WTI on Wednesdays (EIA day), long", "WTI", "long only on Wednesdays", rr, e, r)
    rr, e = Q.run_pos(-pos, r, Q.C_FUT); Q.record("WTI on Wednesdays (EIA day), short", "WTI", "short only on Wednesdays", rr, e, r)


def t7():
    n, b = Q.nifty().close, Q.banknifty().close; P = pd.concat([n.rename("NIFTY"), b.rename("BANKNIFTY")], axis=1).dropna(); me = Q.month_ends(P.index); M = P.reindex(me)
    m6 = M / M.shift(6) - 1; W = pd.DataFrame(0.0, index=me, columns=P.columns)
    for dt in me:
        x = m6.loc[dt]
        if x.isna().any(): continue
        k = x.idxmax()
        if x[k] > 0: W.at[dt, k] = 1.0
    r, e = Q.run_weights(W, P.pct_change(), Q.C_IN); Q.record("Nifty vs Bank Nifty rotation (6-m momentum, cash if negative)", "Nifty + Bank Nifty", "monthly", r, e, P.NIFTY.pct_change())


def _n500():
    IN = Q.in_stocks(); IN = IN[IN.index >= "2006-01-01"]; IN = IN.loc[:, IN.notna().sum() > 300]
    ind = pd.read_csv(os.path.join(Q.MB, "nse_nifty500", "ind_nifty500list.csv")).set_index("Symbol").Industry
    return IN, ind


def t8():
    IN, ind = _n500(); R = IN.pct_change(fill_method=None).clip(-0.5, 1.0)
    groups = {g: [s for s in IN.columns if ind.get(s) == g] for g in ind.unique() if pd.notna(g)}
    IR = pd.DataFrame({g: R[s].mean(axis=1) for g, s in groups.items() if len(s) >= 5})
    IP = (1 + IR.fillna(0)).cumprod(); me = Q.month_ends(IP.index); M = IP.reindex(me); m6 = M / M.shift(6) - 1
    rk = m6.rank(axis=1, ascending=False); W = (rk <= 3).astype(float) / 3
    r, e = Q.run_weights(W, IR, Q.C_INSTK); Q.record(f"Nifty 500 industry rotation (top 3 of {IR.shape[1]} by 6-m return)", "Nifty 500 industries", "monthly, equal-weight industries", r, e, R.mean(axis=1))


def t9():
    IN, ind = _n500(); R = IN.pct_change(fill_method=None).clip(-0.5, 1.0); ew = R.mean(axis=1)
    me = Q.month_ends(IN.index); M = IN.reindex(me); valid = M.notna() & M.shift(12).notna()
    mom = (M.shift(1) / M.shift(12) - 1).where(valid); rk = mom.rank(axis=1, pct=True, ascending=False)
    top = (rk <= 0.1); W = top.astype(float).div(top.sum(axis=1).replace(0, np.nan), axis=0).fillna(0.0)
    n = Q.nifty().close.reindex(me, method="ffill"); filt = (n > Q.nifty().close.rolling(200).mean().reindex(me, method="ffill")).astype(float)
    W2 = W.mul(filt, axis=0)
    r, e = Q.run_weights(W, R, Q.C_INSTK); Q.record("Nifty 500 momentum 12-1 (reference)", "Nifty 500", "top decile, monthly", r, e, ew)
    r, e = Q.run_weights(W2, R, Q.C_INSTK); Q.record("Momentum 12-1 + market filter (cash when Nifty < 200-SMA)", "Nifty 500", "monthly", r, e, ew)
    vol = R.rolling(252).std().reindex(me); rv = vol.where(valid).rank(axis=1, pct=True)
    both = (rk <= 0.3) & (rv <= 0.3); W3 = both.astype(float).div(both.sum(axis=1).replace(0, np.nan), axis=0).fillna(0.0)
    r, e = Q.run_weights(W3, R, Q.C_INSTK); Q.record("Momentum top 30 % AND low-vol bottom 30 % (double sort)", "Nifty 500", "monthly, equal weight", r, e, ew)


def t11():
    import glob
    cl = {}
    for f in glob.glob(os.path.join(Q.DER, "kl1h_*.csv")):
        k = pd.read_csv(f, usecols=["ts", "c"]); k["ts"] = pd.to_datetime(k.ts, unit="ms"); cl[os.path.basename(f)[5:-4]] = k.set_index("ts").c.resample("1D").last()
    P = pd.DataFrame(cl).dropna(how="all").ffill(); R = P.pct_change(fill_method=None)
    above = (P > P.rolling(50).mean()).mean(axis=1); alts = [c for c in P.columns if c != "BTC"]
    we = P.resample("W-SUN").last().index; m4 = (P / P.shift(28) - 1).reindex(we)
    W = pd.DataFrame(0.0, index=we, columns=P.columns)
    for dt in we:
        if pd.isna(above.get(dt)): continue
        if above[dt] > 0.6:
            top = m4.loc[dt, alts].dropna().sort_values(ascending=False).head(5).index
            for s in top: W.at[dt, s] = 0.2
        else:
            W.at[dt, "BTC"] = 1.0
    r, e = Q.run_weights(W, R, Q.C_CRY); Q.record("Altcoin breadth rotation (5 strongest alts when > 60 % above 50-SMA, else BTC)", f"{P.shape[1]} perps", "weekly", r, e, R.BTC, per=365, note="2 years only")


def t12():
    n, b = Q.nifty().close, Q.banknifty().close; P = pd.concat([n.rename("N"), b.rename("B")], axis=1).dropna(); R = P.pct_change()
    lr = np.log(P.N / P.B); z = (lr - lr.rolling(60).mean()) / lr.rolling(60).std()
    W = pd.DataFrame(0.0, index=P.index, columns=P.columns); st = 0
    for dt, zz in z.items():
        if pd.isna(zz): continue
        if st == 0 and zz > 2: st = -1
        elif st == 0 and zz < -2: st = 1
        elif st != 0 and abs(zz) < 0.2: st = 0
        W.at[dt, "N"] = st * 0.5; W.at[dt, "B"] = -st * 0.5
    r, e = Q.run_weights(W, R, Q.C_IN); Q.record("Nifty / Bank Nifty ratio mean reversion (60-d z 2 -> 0)", "Nifty + Bank Nifty", "long cheap / short rich", r, e, R.N * 0)


if __name__ == "__main__":
    for fn in (t1, t2, t3, t4, t5, t6, t7, t8, t9, t11, t12):
        try:
            fn()
        except Exception as e:
            print(f"{fn.__name__} failed: {type(e).__name__}: {str(e)[:140]}")
    pd.DataFrame(Q.RES).to_csv(OUT, index=False); print("->", OUT, len(Q.RES), "tests")
