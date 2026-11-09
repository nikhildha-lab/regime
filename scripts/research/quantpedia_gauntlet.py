"""QUANTPEDIA GAUNTLET (2026-11-10, Nikhil: "let's get back to it .. both, winners first"): the 8 Nov sweep's winners re-tested the
MASTER_BACKTESTER way before any of them becomes a paper rule - 70/30 split by date, year by year, 2x / 3x costs, best 3 periods
removed, neighbouring settings. Same engine as quantpedia_sweep.py (decide at a close, earn the next period, costs per side on changes).
Candidates:
  1. Crypto x-day high   - long while the daily close is the x-day high (x 20 / 30 / 40), BTC ETH SOL (+ 12 more coins, 2 y)   costs 0.05 %/side
  2. Gold / BTC dual momentum - weekly, hold the better 8-week return if > 0 (neighbours 6 / 12 weeks), 20 % vol cap        costs 0.02 %/side
  3. Nifty 500 monthly portfolios - low-beta decile, low-vol decile, 52-week-high decile, momentum 12-1, ATH + 10-ATR trail    costs 0.15 %/side
     judged as EXCESS over the equal-weight universe (survivorship: today's members)
  4. Nifty: 60-d avg India VIX > 10-d realised vol -> long next day; crude-oil monthly forecast                            costs 0.03 %/side
Output: data_store/research/quantpedia_gauntlet.txt.  Run: python3 scripts/research/quantpedia_gauntlet.py
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
import quantpedia_sweep as Q   # noqa: E402  (loaders + engine)

OUT = os.path.join(Q.ROOT, "data_store", "research", "quantpedia_gauntlet.txt")
L: list = []


def P(*a):
    s = " ".join(str(x) for x in a); print(s); L.append(s)


def sh(r, per):
    r = r.dropna(); return r.mean() / r.std() * np.sqrt(per) if len(r) > 10 and r.std() > 0 else np.nan


def gauntlet(name, r, per, cost_per_turn, bench=None, min_years=3):
    """r = net return series per period; cost_per_turn = extra cost per period for the 2x / 3x stress (turnover x cost)."""
    r = r.dropna()
    if bench is not None:
        bench = bench.reindex(r.index).fillna(0.0)
    yrs = r.groupby(r.index.year)
    cut = r.index[int(len(r) * 0.7)]
    a, b = r[r.index < cut], r[r.index >= cut]
    eq = (1 + r).cumprod(); dd = (eq / eq.cummax() - 1).min()
    best3 = r.sort_values().iloc[:-3] if len(r) > 10 else r
    ypos = [(y, sh(g, per), (g.sum() - (bench[g.index].sum() if bench is not None else 0))) for y, g in yrs if len(g) >= per * 0.5]
    npos = sum(1 for _, _, ex in ypos if ex > 0)
    s_all, s_a, s_b = sh(r, per), sh(a, per), sh(b, per)
    r2 = r - cost_per_turn; r3 = r - 2 * cost_per_turn
    ok = s_all > 0.3 and s_a > 0.2 and s_b > 0.2 and sh(r2, per) > 0.2 and sh(best3, per) > 0.2 and npos >= 0.6 * len(ypos)
    P(f"== {name}")
    P(f"   {str(r.index.min())[:10]} .. {str(r.index.max())[:10]} · Sharpe {s_all:+.2f} · CAGR {100*((eq.iloc[-1])**(per/len(r))-1):+.1f}% · maxDD {100*dd:.0f}% · train {s_a:+.2f} / test {s_b:+.2f} (split {str(cut)[:10]})")
    P(f"   2x costs {sh(r2, per):+.2f} · 3x costs {sh(r3, per):+.2f} · best 3 periods removed {sh(best3, per):+.2f} · years beating {'benchmark' if bench is not None else 'zero'}: {npos} of {len(ypos)}")
    P("   by year: " + " · ".join(f"{y} {s:+.2f}{'' if bench is None else f' ({100*ex:+.0f}%)'}" for y, s, ex in ypos))
    P(f"   -> {'PASSES' if ok else 'DOES NOT PASS'}")
    return ok


def daily_coin(coin):
    """daily closes: MASTER_BACKTESTER deep files for BTC / ETH / SOL, else the Binance 1-hour research file (2 years)."""
    p = os.path.join(Q.MB, "crypto", f"{coin}_deep.csv")
    if os.path.exists(p):
        return Q.ohlc(p).close
    f = os.path.join(Q.DER, f"kl1h_{coin}.csv")
    k = pd.read_csv(f, usecols=["ts", "c"]); k["ts"] = pd.to_datetime(k.ts, unit="ms")
    return k.set_index("ts").c.resample("1D").last().dropna()


def g1():
    P("\n#### 1. CRYPTO x-DAY HIGH (long while the close is the x-day high; costs 0.05 %/side)")
    coins = ["BTC", "ETH", "SOL"] + [os.path.basename(f)[5:-4] for f in sorted(os.listdir(Q.DER)) if f.startswith("kl1h_") and f[5:-4] not in ("BTC", "ETH", "SOL")] if os.path.isdir(Q.DER) else ["BTC", "ETH", "SOL"]
    res = {}
    for coin in coins:
        try:
            c = daily_coin(coin)
        except Exception as e:
            P(f"   {coin}: no data ({e})"); continue
        c = c[c.index >= "2019-01-01"]; ret = c.pct_change()
        for x in (20, 30, 40):
            pos = (c >= c.rolling(x).max()).astype(float)
            r, _ = Q.run_pos(pos.shift(1), ret, Q.C_CRY)
            turn = (pos - pos.shift(1).fillna(0)).abs().shift(1).fillna(0) * Q.C_CRY
            if x == 30:
                res[coin] = gauntlet(f"{coin} 30-day high · in market {100*pos.mean():.0f}% · buy-and-hold Sharpe {sh(ret, 365):+.2f}", r, 365, turn, bench=None, min_years=2)
            else:
                P(f"   neighbour x={x}: Sharpe {sh(r, 365):+.2f} (train {sh(r[r.index < r.index[int(len(r)*0.7)]], 365):+.2f} / test {sh(r[r.index >= r.index[int(len(r)*0.7)]], 365):+.2f})")
    P(f"   PASS: {[k for k, v in res.items() if v]} · FAIL: {[k for k, v in res.items() if not v]}")
    return res


def g2():
    P("\n#### 2. GOLD / BTC DUAL MOMENTUM (weekly; hold the better N-week return if > 0; 20 % vol cap; costs 0.02 %/side)")
    g = Q.ohlc("us/GOLD_COMEX_deep.csv").close; btc = Q.ohlc("crypto/BTC_deep.csv").close
    W = pd.concat([g.rename("GC"), btc.rename("BTC")], axis=1).ffill().dropna().resample("W-WED").last()
    R = W.pct_change(); out = {}
    for n in (6, 8, 12):
        m = W.pct_change(n); vol = R.rolling(12).std() * np.sqrt(52)
        pick = pd.DataFrame(0.0, index=W.index, columns=W.columns); best = m.idxmax(axis=1)
        for dd in W.index:
            bb = best.get(dd)
            if isinstance(bb, str) and m.at[dd, bb] > 0:
                pick.at[dd, bb] = min(1.0, 0.20 / vol.at[dd, bb]) if vol.at[dd, bb] > 0 else 0
        r, _ = Q.run_weights(pick, R, Q.C_FUT, per=52)
        turn = (pick - pick.shift(1).fillna(0)).abs().sum(axis=1).shift(1).fillna(0) * Q.C_FUT
        if n == 8:
            out["gbtc"] = gauntlet(f"Gold / BTC dual momentum 8 weeks · benchmark gold Sharpe {sh(R.GC, 52):+.2f}", r, 52, turn, bench=R.GC)
        else:
            P(f"   neighbour {n} weeks: Sharpe {sh(r, 52):+.2f}")
    return out


def g3():
    P("\n#### 3. NIFTY 500 MONTHLY PORTFOLIOS (excess over the equal-weight universe; costs 0.15 %/side; today's members = survivorship)")
    IN = Q.in_stocks(); IN = IN[IN.index >= "2006-01-01"]; IN = IN.loc[:, IN.notna().sum() > 300]
    R = IN.pct_change(fill_method=None).clip(-0.5, 1.0); ew = R.mean(axis=1)
    me = Q.month_ends(IN.dropna(how="all").index); M = IN.reindex(me); valid = M.notna() & M.shift(12).notna()
    def top(score, q=0.1, high=True):
        s = score.where(valid); rk = s.rank(axis=1, pct=True, ascending=not high); return rk <= q
    def ew_w(mask):
        return mask.astype(float).div(mask.sum(axis=1).replace(0, np.nan), axis=0).fillna(0.0)
    mkt = ew; var = mkt.rolling(252).var()
    beta = pd.DataFrame({s: R[s].rolling(252, min_periods=200).cov(mkt) / var for s in R.columns}).reindex(me)
    vol = R.rolling(252).std().reindex(me); hi52 = IN.rolling(252).max().reindex(me); mom = M.shift(1) / M.shift(12) - 1
    out = {}
    for nm, W in (("low beta decile", ew_w(top(beta, high=False))), ("low volatility decile", ew_w(top(vol, high=False))), ("52-week-high decile", ew_w(top(M / hi52))), ("momentum 12-1 decile", ew_w(top(mom)))):
        r, _ = Q.run_weights(W, R, Q.C_INSTK)
        turn = (W.reindex(R.index).ffill().fillna(0) - W.reindex(R.index).ffill().fillna(0).shift(1).fillna(0)).abs().sum(axis=1).shift(1).fillna(0) * Q.C_INSTK
        ex = (r - ew.reindex(r.index)).dropna()
        P(f"   -- {nm}: Sharpe {sh(r, 252):+.2f} vs equal-weight {sh(ew.reindex(r.index), 252):+.2f} · maxDD {100*((1+r).cumprod()/(1+r).cumprod().cummax()-1).min():.0f}% vs {100*((1+ew).cumprod()/(1+ew).cumprod().cummax()-1).min():.0f}%")
        out[nm] = gauntlet(f"Nifty 500 {nm} · EXCESS return over the equal-weight universe", ex, 252, turn, bench=None)
        for q in (0.05, 0.2):
            W2 = ew_w(top({"low beta decile": beta, "low volatility decile": vol}.get(nm, M / hi52 if "52" in nm else mom), q=q, high=nm not in ("low beta decile", "low volatility decile")))
            r2, _ = Q.run_weights(W2, R, Q.C_INSTK); P(f"      neighbour top {int(q*100)}%: excess Sharpe {sh((r2 - ew.reindex(r2.index)).dropna(), 252):+.2f}")
    return out


def g4():
    P("\n#### 4. NIFTY TIMING (costs 0.03 %/side)")
    n = Q.nifty().close; v = Q.india_vix(); r = n.pct_change()
    rv = r.rolling(10).std() * np.sqrt(252) * 100; va = v.rolling(60).mean().reindex(n.index).ffill()
    pos = (va > rv).astype(float)
    rr, _ = Q.run_pos(pos.shift(1), r, Q.C_IN); turn = (pos - pos.shift(1).fillna(0)).abs().shift(1).fillna(0) * Q.C_IN
    out = {"vix": gauntlet(f"Nifty: 60-d avg India VIX > 10-d realised vol -> long · in market {100*pos.mean():.0f}% · Nifty Sharpe {sh(r, 252):+.2f}", rr, 252, turn, bench=r)}
    for k in (30, 90):
        va2 = v.rolling(k).mean().reindex(n.index).ffill(); p2 = (va2 > rv).astype(float); r2, _ = Q.run_pos(p2.shift(1), r, Q.C_IN)
        P(f"   neighbour {k}-d VIX average: Sharpe {sh(r2, 252):+.2f}")
    return out


if __name__ == "__main__":
    res = {}
    res.update({f"xhigh_{k}": v for k, v in g1().items()}); res.update(g2()); res.update(g3()); res.update(g4())
    P("\n#### SUMMARY"); P("   PASS: " + ", ".join(k for k, v in res.items() if v)); P("   FAIL: " + ", ".join(k for k, v in res.items() if not v))
    open(OUT, "w").write("\n".join(L))


def g5():
    """sweep-2 candidate: SPY correlation-acceleration hedge (threshold chosen on the TRAIN half only)."""
    import quantpedia_sweep2 as Q2
    P("\n#### 5. SPY CORRELATION-ACCELERATION HEDGE (out of SPY when the 20-d avg pairwise sector correlation rose > th in 10 sessions; costs 0.02 %/side)")
    Pz = Q2.sectors(); R = Pz.pct_change(fill_method=None); spy = Q.etf("SPY").close.pct_change().reindex(R.index).dropna()
    out = []
    for i in range(len(R)):
        if i < 20: out.append(np.nan); continue
        c = R.iloc[i - 20:i].corr().values; n = c.shape[0]; out.append((c.sum() - n) / (n * (n - 1)))
    ac = pd.Series(out, index=R.index); acc = ac - ac.shift(10)
    cut = spy.index[int(len(spy) * 0.7)]
    best, res = None, {}
    for th in (0.05, 0.08, 0.10, 0.12, 0.15, 0.20, 0.25):
        pos = (acc <= th).astype(float); r, _ = Q.run_pos(pos.shift(1), spy, Q.C_FUT)
        tr = r[r.index < cut]; res[th] = (sh(tr, 252), sh(r[r.index >= cut], 252), sh(r, 252))
        if best is None or res[th][0] > res[best][0]: best = th
    P("   threshold: train / test / all Sharpe  " + " · ".join(f"{th}: {a:+.2f} / {b:+.2f} / {c:+.2f}" for th, (a, b, c) in res.items()) + f"   (SPY train {sh(spy[spy.index < cut], 252):+.2f} / test {sh(spy[spy.index >= cut], 252):+.2f})")
    pos = (acc <= best).astype(float); r, _ = Q.run_pos(pos.shift(1), spy, Q.C_FUT); turn = (pos - pos.shift(1).fillna(0)).abs().shift(1).fillna(0) * Q.C_FUT
    return {"corr_hedge": gauntlet(f"SPY correlation-acceleration hedge · threshold {best} chosen on train · in market {100*pos.mean():.0f}%", r, 252, turn, bench=spy)}


if __name__ == "__main__" and "--g5" in sys.argv:
    g5(); open(OUT, "a").write("\n" + "\n".join(L))
