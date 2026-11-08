"""QUANTPEDIA SWEEP - every free Quantpedia strategy our data can test (2026-11-08, Nikhil: "extract all which can be fetched and use
source platform data or master backtester data").

Rules: Quantpedia's "simple trading strategy" text (82 free strategy pages listed on quantpedia.com/screener, 27 read in full) plus the
blog ideas already shortlisted. Rules are taken as published and adapted only where the universe does not exist here (noted per test).
Data: ~/Documents/MASTER_BACKTESTER/data (US futures 2000+, SPX/NDX/VIX cash, 23 US ETFs 2016+, ~500 S&P 500 stocks 2016+, Nifty 500
stocks 2005+, Nifty / Bank Nifty spot 2014+, India VIX 2008+, BTC / ETH daily 2017+) and the platform's data_store (BTC 5-min 2020+,
Binance 1-hour for 15 coins, 2 years).
Engine: positions are decided at a close and earn the NEXT period's return (no look-ahead). Costs per side on every change in position:
US futures / ETFs / indexes 0.02 %, US stocks 0.05 %, Indian index 0.03 %, Indian stocks 0.15 % (delivery STT), crypto 0.05 %.
Each test reports CAGR, volatility, Sharpe, max drawdown, time in market, the same for its benchmark, and Sharpe in the first and
second half of the period (the honesty check: a rule that only worked in one half is not trusted).
Caveats: stock universes are TODAY's index members (survivorship bias flatters long-only stock results); Yahoo continuous futures
include roll jumps; nothing was re-optimised.
Run: python3 scripts/research/quantpedia_sweep.py  -> data_store/research/quantpedia_sweep.csv
"""
from __future__ import annotations

import glob
import os

import numpy as np
import pandas as pd

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
MB = os.path.join(os.environ.get("MP_DOCS") or os.path.join(os.path.expanduser("~"), "Documents"), "MASTER_BACKTESTER", "data")
MD = os.path.join(ROOT, "data_store", "market_data")
DER = os.path.join(ROOT, "data_store", "research", "deriv")
OUT = os.path.join(ROOT, "data_store", "research", "quantpedia_sweep.csv")
C_FUT, C_USSTK, C_IN, C_INSTK, C_CRY = 0.0002, 0.0005, 0.0003, 0.0015, 0.0005
RES = []
MEF = "ME" if tuple(int(x) for x in pd.__version__.split(".")[:2]) >= (2, 2) else "M"


# ------------------------------------------------------------------ data
def ohlc(path, date_col="date"):
    d = pd.read_csv(os.path.join(MB, path))
    d[date_col] = pd.to_datetime(d[date_col].astype(str).str[:10])
    x = d.set_index(date_col)
    return x[~x.index.duplicated()].sort_index()


FUT = {"ES": "us/ES_deep.csv", "NQ": "us/NQ_deep.csv", "YM": "us/YM_deep.csv", "RTY": "us/RTY_deep.csv", "GC": "us/GOLD_COMEX_deep.csv",
       "SI": "us/SILVER_deep.csv", "HG": "us/COPPER_deep.csv", "CL": "us/CRUDE_WTI_deep.csv", "NG": "us/NATGAS_deep.csv",
       "ZN": "us/ZN_10Y_deep.csv", "ZB": "us/ZB_30Y_deep.csv", "6E": "us/EURUSD_FUT_deep.csv"}


def fut_closes():
    return pd.DataFrame({k: ohlc(v).close for k, v in FUT.items()}).sort_index()


def etf(s):
    return ohlc(f"us_etf/{s}_daily.csv")


def us_stocks():
    px = {}
    for f in glob.glob(os.path.join(MB, "us_stocks", "*_daily.csv")):
        s = os.path.basename(f)[:-10]
        try:
            px[s] = pd.read_csv(f, usecols=["date", "close"], index_col=0, parse_dates=True).close
        except Exception:
            pass
    P = pd.DataFrame(px); P.index = pd.to_datetime(P.index.astype(str).str[:10]); P = P[~P.index.duplicated()].sort_index()
    return P


def in_stocks():
    d = pd.concat([pd.read_csv(f) for f in sorted(glob.glob(os.path.join(MB, "nse_nifty500", "nifty500_daily_part*.csv")))])
    d = d[~d.sym.str.startswith("^")]
    d["date"] = pd.to_datetime(d.day, unit="D")
    P = d.pivot_table(index="date", columns="sym", values="c").sort_index()
    return P


def nifty():
    a = ohlc("NIFTY_deep_spot.csv"); return a


def banknifty():
    return ohlc("BANKNIFTY_deep_spot.csv")


def india_vix():
    v = pd.read_csv(os.path.join(MB, "INDIAVIX.csv"), parse_dates=["date"]).set_index("date").close
    return v[~v.index.duplicated()].sort_index()


def btc_hourly():
    a = pd.read_csv(os.path.join(MD, "history", "binance_BTC_5m.csv.gz"), usecols=["ts", "open", "close"], parse_dates=["ts"])
    h = a.set_index("ts").resample("1h").agg({"open": "first", "close": "last"}).dropna()
    k = pd.read_csv(os.path.join(DER, "kl1h_BTC.csv")); k["ts"] = pd.to_datetime(k.ts, unit="ms")
    k = k.set_index("ts")[["o", "c"]].rename(columns={"o": "open", "c": "close"})
    return pd.concat([h, k[k.index > h.index.max()]]).sort_index()


# ------------------------------------------------------------------ engine
def metrics(r, per=252):
    r = r.dropna()
    if len(r) < 20 or r.std() == 0:
        return dict(cagr=np.nan, vol=np.nan, sharpe=np.nan, mdd=np.nan)
    eq = (1 + r).cumprod(); yrs = len(r) / per
    return dict(cagr=eq.iloc[-1] ** (1 / yrs) - 1, vol=r.std() * np.sqrt(per), sharpe=r.mean() / r.std() * np.sqrt(per), mdd=(eq / eq.cummax() - 1).min())


def run_weights(W, R, cost, per=252):
    """W: weights decided at each date's close (index = dates); R: per-period simple returns. Weights earn the NEXT period's return."""
    W = W.reindex(R.index).ffill().fillna(0.0); R = R.reindex(columns=W.columns).fillna(0.0)
    held = W.shift(1).fillna(0.0)
    turn = (W - W.shift(1).fillna(0.0)).abs().sum(axis=1).shift(1).fillna(0.0)
    return (held * R).sum(axis=1) - turn * cost, (held.abs().sum(axis=1) > 0).mean()


def run_pos(pos, ret, cost, per=252):
    """pos: exposure DURING each period (already known at the previous close); ret: that period's return."""
    pos = pos.reindex(ret.index).fillna(0.0)
    turn = (pos - pos.shift(1).fillna(0.0)).abs()
    return pos * ret - turn * cost, (pos != 0).mean()


def record(name, market, rule, r, expo, bench, per=252, note=""):
    r = r.dropna(); bench = bench.reindex(r.index).dropna()
    m, b = metrics(r, per), metrics(bench, per)
    h = len(r) // 2; s1, s2 = metrics(r.iloc[:h], per)["sharpe"], metrics(r.iloc[h:], per)["sharpe"]
    row = {"strategy": name, "market": market, "rule": rule, "from": str(r.index.min())[:10], "to": str(r.index.max())[:10],
           "CAGR_%": round(100 * m["cagr"], 1), "vol_%": round(100 * m["vol"], 1), "Sharpe": round(m["sharpe"], 2), "maxDD_%": round(100 * m["mdd"], 1),
           "in_mkt_%": round(100 * expo, 0), "bench_CAGR_%": round(100 * b["cagr"], 1), "bench_Sharpe": round(b["sharpe"], 2), "bench_maxDD_%": round(100 * b["mdd"], 1),
           "Sharpe_1st_half": round(s1, 2), "Sharpe_2nd_half": round(s2, 2), "note": note}
    ok = m["sharpe"] > 0.3 and s1 > 0.2 and s2 > 0.2
    better = True if not np.isfinite(b["sharpe"]) else (m["sharpe"] >= b["sharpe"] - 0.05) or (m["mdd"] > b["mdd"] * 0.6 and m["sharpe"] >= b["sharpe"] * 0.8)
    row["verdict"] = "WORKS" if ok and better else "weak" if ok else "no"
    RES.append(row)
    print(f"{row['verdict']:5s} {name[:40]:40s} {market[:12]:12s} CAGR {row['CAGR_%']:6.1f} Sh {row['Sharpe']:5.2f} DD {row['maxDD_%']:6.1f} "
          f"| bench Sh {row['bench_Sharpe']:5.2f} DD {row['bench_maxDD_%']:6.1f} | halves {row['Sharpe_1st_half']:5.2f}/{row['Sharpe_2nd_half']:5.2f} | in {row['in_mkt_%']:.0f}%")


def month_ends(idx):
    s = pd.Series(idx, index=idx)
    return pd.DatetimeIndex(s.groupby([idx.year, idx.month]).last().values)


# ------------------------------------------------------------------ A. asset allocation / futures
def A_tests():
    F = fut_closes(); F = F[F.index >= "2001-01-01"]; R = F.pct_change().clip(-0.25, 0.25)
    me = month_ends(F.dropna(how="all").index)
    M = F.reindex(me)
    basket = ["ES", "ZN", "GC", "CL", "HG"]
    ew = R[basket].mean(axis=1)
    # A1 asset-class trend-following: each 20 % slice held only above its 10-month SMA
    W = (M[basket] > M[basket].rolling(10).mean()).astype(float) / len(basket)
    r, e = run_weights(W, R[basket], C_FUT); record("Asset class trend-following (10-month SMA)", "US futures 5", "ES ZN GC CL HG, 20 % each if above 10-m SMA, monthly", r, e, ew)
    # A2 momentum asset allocation: top 3 of 5 by 12-month return
    mom = M[basket].pct_change(12); rk = mom.rank(axis=1, ascending=False)
    W = (rk <= 3).astype(float) / 3
    r, e = run_weights(W, R[basket], C_FUT); record("Momentum asset allocation (top 3 of 5)", "US futures 5", "12-m return, hold top 3 equal, monthly", r, e, ew)
    # A4 paired switching ES vs ZN quarterly
    qe = pd.DatetimeIndex([d for d in me if d.month in (3, 6, 9, 12)])
    Q = F.reindex(qe)[["ES", "ZN"]]; pr = Q.pct_change()
    W = pd.DataFrame({"ES": (pr.ES >= pr.ZN).astype(float), "ZN": (pr.ES < pr.ZN).astype(float)}, index=qe)
    r, e = run_weights(W, R[["ES", "ZN"]], C_FUT); record("Paired switching (stocks vs bonds)", "ES / ZN", "hold the better of last quarter, quarterly", r, e, 0.6 * R.ES + 0.4 * R.ZN, note="benchmark 60/40")
    # A5 time-series momentum, 12 futures, vol-scaled (40 % / asset vol, /N)
    U = list(FUT); vol = R[U].rolling(60).std() * np.sqrt(252)
    sig = np.sign(M[U].pct_change(12)); w = sig * (0.40 / vol.reindex(me)) / len(U)
    W = w.clip(-0.5, 0.5)
    r, e = run_weights(W, R[U], C_FUT); record("Time-series momentum (12 futures)", "US futures 12", "sign of 12-m return, 40 %/vol, monthly", r, e, R[U].mean(axis=1))
    # A6 commodity momentum (5 commodities): long best, short worst 12-m return
    cm = ["GC", "SI", "HG", "CL", "NG"]; mom = M[cm].pct_change(12); rk = mom.rank(axis=1)
    W = ((rk == 5).astype(float) - (rk == 1).astype(float)) * 0.5
    r, e = run_weights(W, R[cm], C_FUT); record("Momentum in commodities (best minus worst)", "5 commodities", "12-m return, long top / short bottom, monthly", r, e, R[cm].mean(axis=1), note="only 5 commodities (paper: 10-31)")


def A_etf():
    secs = ["XLB", "XLC", "XLE", "XLF", "XLI", "XLK", "XLP", "XLRE", "XLU", "XLV", "XLY"]
    P = pd.DataFrame({s: etf(s).close for s in secs + ["SPY", "TLT", "GLD", "QQQ"]}); R = P.pct_change()
    me = month_ends(P.index); M = P.reindex(me)
    mom = M[secs].pct_change(12); rk = mom.rank(axis=1, ascending=False)
    W = (rk <= 3).astype(float) / 3
    r, e = run_weights(W, R[secs], C_FUT); record("Sector momentum rotation (top 3 of 11)", "US sector ETFs", "12-m return, hold top 3, monthly", r, e, R.SPY, note="benchmark SPY")
    qe = pd.DatetimeIndex([d for d in me if d.month in (3, 6, 9, 12)]); Q = P.reindex(qe)[["SPY", "TLT"]].pct_change()
    W = pd.DataFrame({"SPY": (Q.SPY >= Q.TLT).astype(float), "TLT": (Q.SPY < Q.TLT).astype(float)}, index=qe)
    r, e = run_weights(W, R[["SPY", "TLT"]], C_FUT); record("Paired switching (SPY vs TLT)", "US ETFs", "hold the better of last quarter", r, e, 0.6 * R.SPY + 0.4 * R.TLT, note="benchmark 60/40")


# ------------------------------------------------------------------ B. index calendar / timing
def idx_series():
    S = {"S&P 500": ohlc("us/SPX_cash_deep.csv"), "Nasdaq 100": ohlc("us/NDX_cash_deep.csv"), "Nifty": nifty(), "Bank Nifty": banknifty()}
    S["S&P 500"] = S["S&P 500"][S["S&P 500"].index >= "1990-01-01"]; S["Nasdaq 100"] = S["Nasdaq 100"][S["Nasdaq 100"].index >= "1990-01-01"]
    return S


def cost_of(mk):
    return C_IN if mk in ("Nifty", "Bank Nifty") else C_FUT


def expiry_days(idx, india):
    """US: third Friday of each month (or the trading day before it). India: last Thursday (last Tuesday from Sep 2025) or day before."""
    out = []
    s = pd.Series(idx, index=idx)
    for (y, m), g in s.groupby([idx.year, idx.month]):
        days = pd.date_range(f"{y}-{m:02d}-01", periods=pd.Timestamp(f"{y}-{m:02d}-01").days_in_month)
        if india:
            wd = 1 if pd.Timestamp(f"{y}-{m:02d}-01") >= pd.Timestamp("2025-09-01") else 3
            tgt = [d for d in days if d.weekday() == wd][-1]
        else:
            tgt = [d for d in days if d.weekday() == 4][2]
        cand = g[g.index <= tgt]
        if len(cand):
            out.append(cand.index[-1])
    return pd.DatetimeIndex(out)


def B_tests():
    S = idx_series()
    vix = ohlc("us/VIX_cash_deep.csv").close; ivix = india_vix()
    oil = ohlc("us/CRUDE_WTI_deep.csv").close
    for mk, d in S.items():
        c = d.close; r = c.pct_change().dropna(); idx = r.index; cost = cost_of(mk); bh = r
        india = mk in ("Nifty", "Bank Nifty")
        s = pd.Series(np.arange(len(idx)), index=idx)
        g = s.groupby([idx.year, idx.month])
        first = g.transform(lambda x: x - x.min()); last = g.transform(lambda x: x.max() - x)
        # B1 turn of the month: last trading day + first 3 days
        pos = ((last == 0) | (first <= 2)).astype(float)
        rr, e = run_pos(pos, r, cost); record("Turn of the month (-1 .. +3)", mk, "hold last day of month + first 3 days", rr, e, bh)
        # B2 pre-holiday
        nxt = pd.Series(idx[1:].tolist() + [pd.NaT], index=idx)
        gap = (nxt - idx.to_series()).dt.days
        pre = ((gap > 1) & ~((idx.weekday == 4) & (gap == 3))).astype(float)
        rr, e = run_pos(pre, r, cost); record("Pre-holiday effect", mk, "hold only the trading day before a holiday", rr, e, bh)
        # B3 option-expiration week
        ex = expiry_days(idx, india); pos = pd.Series(0.0, index=idx)
        for x in ex:
            k = idx.get_loc(x); pos.iloc[max(0, k - 4):k + 1] = 1.0
        rr, e = run_pos(pos, r, cost); record("Option-expiration week", mk, "hold the 5 trading days ending on monthly expiry", rr, e, bh)
        # B4 payday (US only: first trading day on/after the 16th)
        if not india:
            pos = pd.Series(0.0, index=idx)
            for (y, m), gg in pd.Series(idx, index=idx).groupby([idx.year, idx.month]):
                c16 = gg[gg.index.day >= 16]
                if len(c16):
                    pos.loc[c16.index[0]] = 1.0
            rr, e = run_pos(pos, r, cost); record("Payday anomaly (16th)", mk, "hold the first trading day on/after the 16th", rr, e, bh)
        # B5 January barometer
        ym = c.groupby([c.index.year, c.index.month]).last()
        janup = {y: bool(ym.get((y, 1), np.nan) > ym.get((y - 1, 12), np.nan)) for y in sorted(set(idx.year))}
        pos = pd.Series([1.0 if (dd.month != 1 and janup.get(dd.year, False)) else 0.0 for dd in idx], index=idx)
        rr, e = run_pos(pos, r, cost); record("January barometer", mk, "Feb-Dec invested only if January was up", rr, e, bh)
        # B6 Halloween / Nov-Apr
        pos = pd.Series(((idx.month >= 11) | (idx.month <= 4)).astype(float), index=idx)
        rr, e = run_pos(pos, r, cost); record("Market seasonality (Nov-Apr)", mk, "invested Nov-Apr only", rr, e, bh)
        # B7 crude oil predicts equity (expanding regression, monthly)
        me = c.resample(MEF).last().pct_change(); om = oil.resample(MEF).last().pct_change()
        J = pd.concat([me.rename("y"), om.shift(1).rename("x")], axis=1).dropna()
        sig = pd.Series(np.nan, index=J.index)
        for k in range(36, len(J)):
            a = J.iloc[:k]; b = np.polyfit(a.x, a.y, 1)
            x_now = J.x.iloc[k]                       # oil return of the PREVIOUS month - known when month k starts
            sig.iloc[k] = 1.0 if (b[0] * x_now + b[1]) > 0 else 0.0
        # signal for month k uses oil return of month k-1 -> exposure for the whole of month k
        pos = pd.Series([sig.get(dd + pd.offsets.MonthEnd(0), np.nan) for dd in idx], index=idx).fillna(0.0)
        rr, e = run_pos(pos, r, cost); record("Crude oil predicts equity returns", mk, "monthly regression on last month's oil return; in if forecast > 0", rr, e, bh)
        # B8 overnight with trend + VIX filter
        dd_ = etf("SPY") if mk == "S&P 500" else etf("QQQ") if mk == "Nasdaq 100" else d      # cash-index opens are not real opens; use the ETF
        o, c_ = dd_.open, dd_.close; on = (o / c_.shift(1) - 1).dropna()
        on = on[(dd_.open != dd_.close).reindex(on.index).fillna(False).astype(bool)]
        on = on[on.index >= "2000-01-01"]
        if len(on) > 500:
            vv = (ivix if india else vix).reindex(c.index).ffill()
            vv = vv.reindex(c_.index).ffill()
            filt = ((c_ > c_.rolling(20).mean()) & (vv < vv.rolling(20).mean())).shift(1).reindex(on.index).fillna(False).astype(bool)
            rr = filt.astype(float) * (on - 2 * cost); e = filt.mean()          # buy at the close and sell at the open every night it is on
            record("Overnight hold + trend + VIX filter", mk, "close -> next open, only if index > 20-d avg and VIX < its 20-d avg", rr, e, on, note="benchmark = every overnight")
        # B9 pullback in uptrend
        sig = ((c > c.rolling(200).mean()) & (r < 0) & (r.shift(1) < 0) & (r.shift(2) < 0)).astype(float)
        rr, e = run_pos(sig.shift(1), r, cost); record("Pullback: above 200-d SMA + 3 down days", mk, "buy the close, hold 1 day", rr, e, bh)
        # B10 VIX above realized volatility
        vv = (ivix if india else vix).reindex(c.index).ffill()
        rv = (r.rolling(10).std() * np.sqrt(252) * 100).reindex(c.index)
        sig = (vv.rolling(60).mean().shift(1) > rv).astype(float)
        rr, e = run_pos(sig.shift(1).reindex(r.index), r, cost); record("VIX average above realized volatility", mk, "60-d avg VIX > 10-d realized vol -> long next day", rr, e, bh)


# ------------------------------------------------------------------ C. stock cross-section
def xs_tests(P, mk, cost):
    P = P.loc[:, P.notna().sum() > 300]
    R = P.pct_change(fill_method=None).clip(-0.5, 1.0)
    ew = R.mean(axis=1)
    me = month_ends(P.dropna(how="all").index); M = P.reindex(me)
    valid = M.notna() & M.shift(12).notna()

    def top(score, q=0.1, high=True):
        s = score.where(valid); rk = s.rank(axis=1, pct=True, ascending=not high)
        return (rk <= q)

    def ew_w(mask):
        return mask.astype(float).div(mask.sum(axis=1).replace(0, np.nan), axis=0).fillna(0.0)

    mom = M.shift(1) / M.shift(12) - 1
    L = ew_w(top(mom)); S = ew_w(top(mom, high=False))
    r, e = run_weights(L, R, cost); record("Momentum 12-1 (top decile, long only)", mk, "past 12 m skipping last month, monthly", r, e, ew, note="benchmark equal-weight universe")
    r, e = run_weights(L - S, R, cost); record("Momentum 12-1 (long-short)", mk, "top minus bottom decile", r, e, ew * 0)
    vol = R.rolling(252).std().reindex(me)
    L = ew_w(top(vol, high=False)); r, e = run_weights(L, R, cost); record("Low volatility (lowest decile)", mk, "1-y daily volatility, monthly", r, e, ew)
    hi52 = P.rolling(252).max().reindex(me); L = ew_w(top(M / hi52)); r, e = run_weights(L, R, cost)
    record("52-week high (nearest decile)", mk, "price / 52-w high, top decile, monthly (stock level)", r, e, ew, note="paper sorts industries")
    cyc = M.shift(11) / M.shift(12) - 1; L = ew_w(top(cyc)); S = ew_w(top(cyc, high=False))
    r, e = run_weights(L - S, R, cost); record("12-month cycle (same month last year)", mk, "top minus bottom decile", r, e, ew * 0)
    a, b = M.shift(1) / M.shift(7) - 1, M / M.shift(6) - 1
    both = top(a) & top(b); bothS = top(a, high=False) & top(b, high=False)
    co = ew_w(both) - ew_w(bothS)
    co = sum(co.shift(k) for k in range(1, 7)) / 6                       # 1-month skip, 6 overlapping cohorts
    r, e = run_weights(co, R, cost); record("Consistent momentum", mk, "top decile in both t-7..t-1 and t-6..t, skip 1 m, hold 6 m", r, e, ew * 0)
    mkt = ew
    var = mkt.rolling(252).var(); bet = pd.DataFrame({s: R[s].rolling(252, min_periods=200).cov(mkt) / var for s in R.columns}).reindex(me)
    lo, hi = top(bet, high=False), top(bet)
    bl = bet.where(lo).mean(axis=1); bh_ = bet.where(hi).mean(axis=1)
    W = ew_w(lo).div(bl, axis=0) - ew_w(hi).div(bh_, axis=0)
    r, e = run_weights(W.clip(-1, 1), R, cost); record("Betting against beta", mk, "low-beta decile scaled to beta 1 minus high-beta decile", r, e, ew * 0)
    # short-term reversal, weekly: long 10 weakest of last week (largest-price-history names)
    fe = P.resample("W-FRI").last(); wr = fe.pct_change(fill_method=None)
    rk = wr.rank(axis=1); n = wr.notna().sum(axis=1)
    L = (rk <= 10).astype(float) / 10; S = (rk.gt(n - 10, axis=0)).astype(float) / 10
    RW = fe.pct_change(fill_method=None).clip(-0.5, 1.0)
    r, e = run_weights(L, RW, cost, per=52); record("Short-term reversal (10 weakest, weekly)", mk, "long last week's 10 worst, weekly", r, e, RW.mean(axis=1), per=52)
    r, e = run_weights(L - S, RW, cost, per=52); record("Short-term reversal (long-short)", mk, "10 worst minus 10 best, weekly", r, e, RW.mean(axis=1) * 0, per=52)


def trend_ath(P, mk, cost):
    """All-time-high entries with a 10 x ATR trailing stop (close-only ATR proxy = 10-d mean |change|), equal weight across holdings."""
    P = P.loc[:, P.notna().sum() > 500]
    R = P.pct_change(fill_method=None).clip(-0.5, 1.0)
    atr = P.diff().abs().rolling(10).mean()
    hist_max = P.cummax().shift(1)
    inpos = pd.DataFrame(False, index=P.index, columns=P.columns)
    state = np.zeros(P.shape[1], bool); peak = np.zeros(P.shape[1]); Pv, Av, Hv = P.values, atr.values, hist_max.values
    warm = 252
    out = np.zeros(P.shape, bool)
    for t in range(warm, len(P)):
        p = Pv[t]
        ent = (~state) & (p >= Hv[t]) & np.isfinite(p) & np.isfinite(Hv[t])
        state = state | ent; peak = np.where(ent, p, np.where(state, np.fmax(peak, p), 0))
        ex = state & (p < peak - 10 * Av[t])
        state = state & ~ex
        out[t] = state
    inpos = pd.DataFrame(out, index=P.index, columns=P.columns)
    W = inpos.astype(float).div(inpos.sum(axis=1).replace(0, np.nan), axis=0).fillna(0.0)
    r, e = run_weights(W, R, cost + 0.001)
    record("Trend following: all-time high + 10-ATR stop", mk, "buy new all-time highs, 10 x ATR trailing stop, equal weight", r, e, R.mean(axis=1), note="0.5 % round trip as in the paper")


# ------------------------------------------------------------------ D. crypto
def D_tests():
    btc = ohlc("crypto/BTC_deep.csv"); eth = ohlc("crypto/ETH_deep.csv")
    for nm, d in (("BTC", btc), ("ETH", eth)):
        c = d.close; r = c.pct_change().dropna()
        sig = ((c > c.rolling(200).mean()) & (r < 0) & (r.shift(1) < 0) & (r.shift(2) < 0)).astype(float)
        rr, e = run_pos(sig.shift(1), r, C_CRY); record("Pullback: above 200-d SMA + 3 down days", nm, "buy the close, hold 1 day", rr, e, r, per=365)
    # rebalancing premium: 15 coins, 2 years, daily
    cl = {}
    for f in glob.glob(os.path.join(DER, "kl1h_*.csv")):
        k = pd.read_csv(f, usecols=["ts", "c"]); k["ts"] = pd.to_datetime(k.ts, unit="ms"); cl[os.path.basename(f)[5:-4]] = k.set_index("ts").c.resample("1D").last()
    P = pd.DataFrame(cl).dropna(); R = P.pct_change().dropna()
    ew_reb = R.mean(axis=1)
    bh = (P / P.iloc[0]).mean(axis=1).pct_change().dropna()
    turn = (R.sub(ew_reb, axis=0).abs().sum(axis=1) / len(P.columns))
    rr = ew_reb - turn * C_CRY - 0.7 * bh
    record("Rebalancing premium (crypto)", f"{P.shape[1]} coins", "long daily-rebalanced equal weight, short 70 % buy-and-hold", rr, 1.0, ew_reb, per=365, note="2 years only")
    # BTC two-timeframe MACD
    h = btc_hourly(); c = h.close
    def macd(x):
        m = x.ewm(span=12, adjust=False).mean() - x.ewm(span=26, adjust=False).mean(); return m - m.ewm(span=9, adjust=False).mean()
    hm = macd(c); dm = macd(c.resample("1D").last()).shift(1).reindex(c.index, method="ffill")
    cross = (hm > 0) & (hm.shift(1) <= 0) & (dm > 0)
    red = h.close < h.open
    pos = np.zeros(len(c)); st = False; cr, rd = cross.values, red.values
    for i in range(len(c)):
        if st and rd[i - 1]:
            st = False
        if not st and i > 0 and cr[i - 1]:
            st = True
        pos[i] = 1.0 if st else 0.0
    hr = c.pct_change().fillna(0)
    rr, e = run_pos(pd.Series(pos, index=c.index), hr, C_CRY)
    record("BTC two-timeframe MACD (daily filter, hourly entry)", "BTC hourly", "H1 MACD cross up while D1 MACD up; exit first red hour", rr.resample("1D").sum(), e, hr.resample("1D").sum(), per=365)
    # BTC overnight (US close -> US open) on 10-day-high days
    et = c.tz_localize("UTC").tz_convert("America/New_York")
    at16 = et[et.index.hour == 15]; at9 = et[et.index.hour == 9]                  # bar 15:00-16:00 closes at the US close; 09:00-10:00 bar ~ the open
    a16 = pd.Series(at16.values, index=at16.index.normalize()); a9 = pd.Series(at9.values, index=at9.index.normalize())
    a16 = a16[a16.index.weekday < 5]; nxt = a9[a9.index.weekday < 5]
    sig = a16 >= a16.rolling(10).max()
    rows = []
    for dd, v in a16.items():
        n = nxt[nxt.index > dd]
        if not len(n):
            continue
        rows.append((dd, n.iloc[0] / v - 1, bool(sig.get(dd, False))))
    T = pd.DataFrame(rows, columns=["d", "r", "s"]).set_index("d")
    rr, e = run_pos(T.s.astype(float), T.r, C_CRY)
    record("BTC overnight on 10-day-high days", "BTC", "US close -> next US morning, only when at a 10-day high", rr, e, T.r, per=252, note="benchmark every US overnight")
    # QQQ / BTC Donchian rotation (variant A, 20 days)
    q = etf("QQQ").close; b = btc.close.reindex(q.index).ffill()
    P2 = pd.concat([q.rename("QQQ"), b.rename("BTC")], axis=1).dropna()
    def don(x, n=20):
        st, out = 0, []
        hi, lo = x.rolling(n).max().shift(1), x.rolling(n).min().shift(1)
        for v, h_, l_ in zip(x, hi, lo):
            if not st and v > h_: st = 1
            elif st and v < l_: st = 0
            out.append(st)
        return pd.Series(out, index=x.index)
    dq, db = don(P2.QQQ), don(P2.BTC)
    W = pd.DataFrame({"QQQ": dq.astype(float), "BTC": ((dq == 0) & (db == 1)).astype(float)})
    rr, e = run_weights(W, P2.pct_change(), C_FUT); record("Nasdaq / Bitcoin breakout rotation", "QQQ + BTC", "20-d Donchian, QQQ first, else BTC, else cash", rr, e, P2.QQQ.pct_change())
    # Gold / BTC dual momentum weekly (8 weeks), 20 % vol cap
    g = ohlc("us/GOLD_COMEX_deep.csv").close
    W8 = pd.concat([g.rename("GC"), btc.close.rename("BTC")], axis=1).ffill().dropna().resample("W-WED").last()
    m8 = W8.pct_change(8); vol = W8.pct_change().rolling(12).std() * np.sqrt(52)
    pick = pd.DataFrame(0.0, index=W8.index, columns=W8.columns)
    best = m8.idxmax(axis=1)
    for dd in W8.index:
        bb = best.get(dd)
        if isinstance(bb, str) and m8.at[dd, bb] > 0:
            pick.at[dd, bb] = min(1.0, 0.20 / vol.at[dd, bb]) if vol.at[dd, bb] > 0 else 0
    rr, e = run_weights(pick, W8.pct_change(), C_FUT, per=52)
    record("Gold / Bitcoin dual momentum", "GC + BTC", "weekly, hold the better 8-week return if > 0, 20 % vol cap", rr, e, W8.GC.pct_change(), per=52, note="benchmark gold")


if __name__ == "__main__":
    print("A. futures / ETFs"); A_tests(); A_etf()
    print("B. index calendar / timing"); B_tests()
    print("C. stocks");
    US = us_stocks(); xs_tests(US, "US S&P 500 stocks", C_USSTK); trend_ath(US, "US S&P 500 stocks", C_USSTK)
    IN = in_stocks(); IN = IN[IN.index >= "2006-01-01"]; xs_tests(IN, "Nifty 500 stocks", C_INSTK); trend_ath(IN, "Nifty 500 stocks", C_INSTK)
    print("D. crypto"); D_tests()
    pd.DataFrame(RES).to_csv(OUT, index=False); print("->", OUT, len(RES), "tests")
