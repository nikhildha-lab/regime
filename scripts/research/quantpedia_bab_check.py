import importlib.util, numpy as np, pandas as pd
sp = importlib.util.spec_from_file_location("q", "scripts/research/quantpedia_sweep.py"); q = importlib.util.module_from_spec(sp); sp.loader.exec_module(q)
for nm, P, cost in (("US", q.us_stocks(), q.C_USSTK), ("India", q.in_stocks().loc["2006":], q.C_INSTK)):
    P = P.loc[:, P.notna().sum() > 300]; R = P.pct_change(fill_method=None); R = R.mask((R < -0.35) | (R > 0.5), 0.0)
    ew = R.mean(axis=1); me = q.month_ends(P.dropna(how="all").index)
    var = ew.rolling(252).var(); bet = pd.DataFrame({s: R[s].rolling(252, min_periods=200).cov(ew) / var for s in R.columns}).reindex(me)
    rk = bet.rank(axis=1, pct=True); lo, hi = rk <= 0.1, rk >= 0.9
    print(nm, "avg beta low decile", round(bet.where(lo).mean(axis=1).mean(), 2), "high decile", round(bet.where(hi).mean(axis=1).mean(), 2))
    w = lambda m: m.astype(float).div(m.sum(axis=1).replace(0, np.nan), axis=0).fillna(0)
    for lab, W in (("unlevered low minus high", w(lo) - w(hi)), ("low-beta decile long only", w(lo))):
        r, e = q.run_weights(W, R, cost); m = q.metrics(r); h = len(r.dropna()) // 2
        print(f"  {lab:28s} CAGR {100*m['cagr']:5.1f} Sh {m['sharpe']:.2f} DD {100*m['mdd']:.1f} | halves {q.metrics(r.dropna().iloc[:h])['sharpe']:.2f}/{q.metrics(r.dropna().iloc[h:])['sharpe']:.2f}")
