"""F&O FLOW - is there an edge? (2026-11-08, Nikhil: "understand very deeply, take a good amount of experiments and build a strategy
around this .. assume you are a quant expert .. can we get some edge out of this").

Data: every NSE F&O bhavcopy since 8 Jul 2024 (data_store/research/fno_hist, from app/fnoflow.backfill): stock & index futures (all
expiries: close, prev close, open, high, low, OI, OI change, volume) and per-underlying option aggregates (nearest expiry: call / put OI,
OI change, volume, biggest put OI strike at or below spot = support, biggest call OI strike at or above = resistance).
Panel (stock, day t), everything known at day t's close:
  ret      futures return of the most-held contract (not expiring that day) vs its own previous close
  oi_chg   total futures OI change, all expiries (%)
  bu       build-up from (ret, oi_chg)
  vol_rel  futures volume vs its 20-day median
  lean     (put OI added - call OI added) / total option OI   (writers adding puts = bullish)
  pcr_chg  change in put/call OI ratio
  basis    futures premium over spot (%), and its change
  sup_d / res_d  distance of spot from support / resistance (%)
  sector   from the Nifty 500 list; sector median return
Targets (same contract, so no roll jumps): next day close-to-close (r1), overnight (close -> next open), next day open -> close,
3- and 5-day returns.
Tests: (1) build-up classes, (2) daily cross-sectional rank IC and top-minus-bottom quintile portfolios for each signal, net of
0.06 % round-trip futures costs per side change, both halves of the sample, (3) market-wide breadth -> Nifty next day,
(4) composites. Everything is out-of-sample-style: no parameters are fitted except quintile sorts.
Run: python3 scripts/research/fno_flow_research.py
"""
from __future__ import annotations

import glob
import os

import numpy as np
import pandas as pd

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
H = os.path.join(ROOT, "data_store", "research", "fno_hist")
DOCS = os.environ.get("MP_DOCS") or os.path.join(os.path.expanduser("~"), "Documents")
N500 = os.path.join(DOCS, "MASTER_BACKTESTER", "data", "nse_nifty500", "ind_nifty500list.csv")
COST = 0.0006            # futures round trip (brokerage + STT 0.02 % on sell + exchange + slippage), per full turnover
FLOOR_OI, FLOOR_PX = 2.0, 0.3


def load():
    F = pd.concat([pd.read_csv(f) for f in sorted(glob.glob(os.path.join(H, "fut_*.csv.gz")))], ignore_index=True)
    O = pd.concat([pd.read_csv(f) for f in sorted(glob.glob(os.path.join(H, "opt_*.csv.gz")))], ignore_index=True)
    return F, O


def panel(F, O):
    F = F[F.close > 0].copy()
    days = sorted(F.day.unique()); nxt = {d: days[i + 1] for i, d in enumerate(days[:-1])}
    for k in (2, 3, 5):
        globals()[f"N{k}"] = {d: days[i + k] for i, d in enumerate(days[:-k])}
    tot = F.groupby(["day", "sym"]).agg(oi_tot=("oi", "sum"), chg_tot=("chg_oi", "sum"), vol_tot=("vol", "sum"), kind=("kind", "first")).reset_index()
    live = F[F.expiry > F.day]                                         # not expiring today
    main = live.sort_values("oi").groupby(["day", "sym"]).tail(1)[["day", "sym", "expiry", "open", "high", "low", "close", "prev", "und"]]
    P = tot.merge(main, on=["day", "sym"])
    key = F.set_index(["day", "sym", "expiry"])[["open", "close"]]
    def fwd(nmap, col):
        idx = [(nmap.get(d), s, e) for d, s, e in zip(P.day, P.sym, P.expiry)]
        ok = [i[0] is not None and i in key.index for i in idx]
        out = np.full(len(P), np.nan)
        sel = np.array(ok)
        if sel.any():
            out[sel] = key.loc[[i for i, o in zip(idx, ok) if o], col].values
        return out
    P["c1"], P["o1"] = fwd(nxt, "close"), fwd(nxt, "open")
    P["c3"], P["c5"] = fwd(N3, "close"), fwd(N5, "close")
    P["ret"] = (P.close / P.prev - 1) * 100
    P["r1"] = (P.c1 / P.close - 1) * 100; P["ron"] = (P.o1 / P.close - 1) * 100; P["rid"] = (P.c1 / P.o1 - 1) * 100
    P["r3"] = (P.c3 / P.close - 1) * 100; P["r5"] = (P.c5 / P.close - 1) * 100
    P["oi_chg"] = P.chg_tot / (P.oi_tot - P.chg_tot).replace(0, np.nan) * 100
    P["basis"] = (P.close / P.und - 1) * 100
    P = P.sort_values(["sym", "day"])
    P["vol_rel"] = P.vol_tot / P.groupby("sym").vol_tot.transform(lambda x: x.rolling(20, min_periods=10).median().shift(1))
    P["basis_chg"] = P.basis - P.groupby("sym").basis.shift(1)
    P["range"] = (P.high - P.low) / P.close * 100
    P["clv"] = ((P.close - P.low) - (P.high - P.close)) / (P.high - P.low).replace(0, np.nan)      # close location in the day's range
    O = O.copy(); O["tot"] = O.ce_oi + O.pe_oi
    O["lean"] = (O.pe_chg - O.ce_chg) / O.tot.replace(0, np.nan)
    O["pcr"] = O.pe_oi / O.ce_oi.replace(0, np.nan)
    O = O.sort_values(["sym", "day"]); O["pcr_chg"] = O.pcr - O.groupby("sym").pcr.shift(1)
    O["sup_d"] = (O.und / O.sup - 1) * 100; O["res_d"] = (O.res / O.und - 1) * 100
    O["res_prev"] = O.groupby("sym").res.shift(1); O["sup_prev"] = O.groupby("sym").sup.shift(1)
    P = P.merge(O[["day", "sym", "lean", "pcr", "pcr_chg", "sup_d", "res_d", "res_prev", "sup_prev", "ce_vol", "pe_vol"]], on=["day", "sym"], how="left")
    P["brk_up"] = (P.und > P.res_prev).astype(float); P["brk_dn"] = (P.und < P.sup_prev).astype(float)
    sec = {}
    try:
        s = pd.read_csv(N500); sec = dict(zip(s.Symbol.str.strip(), s.Industry.str.strip()))
    except Exception:
        pass
    P["sector"] = P.sym.map(sec).fillna("Other")
    P["bu"] = np.select([(P.oi_chg.abs() < FLOOR_OI) | (P.ret.abs() < FLOOR_PX), (P.ret > 0) & (P.oi_chg > 0), (P.ret < 0) & (P.oi_chg > 0), (P.ret > 0) & (P.oi_chg < 0)],
                        ["NEUTRAL", "LONG_BUILD", "SHORT_BUILD", "SHORT_COVER"], "LONG_UNWIND")
    return P


def t_stat(x):
    x = x.dropna(); return x.mean() / x.std() * np.sqrt(len(x)) if len(x) > 2 and x.std() else np.nan


def ls_portfolio(S, sig, target="r1", q=0.2, cost=COST):
    """Each day: long the top quintile of `sig`, short the bottom, equal weight; daily return of the spread (in %), net of costs."""
    rows = []
    prev_l, prev_s = set(), set()
    for d, g in S.dropna(subset=[sig, target]).groupby("day"):
        if len(g) < 40:
            continue
        lo, hi = g[sig].quantile(q), g[sig].quantile(1 - q)
        L, Sh = g[g[sig] >= hi], g[g[sig] <= lo]
        turn = (len(set(L.sym) ^ prev_l) / max(1, len(L)) + len(set(Sh.sym) ^ prev_s) / max(1, len(Sh))) / 2      # share of the book that changed
        prev_l, prev_s = set(L.sym), set(Sh.sym)
        gross = L[target].mean() - Sh[target].mean()
        ic = g[sig].rank().corr(g[target].rank())
        rows.append((d, gross, gross - turn * cost * 100 * 2, L[target].mean() - g[target].mean(), ic))
    return pd.DataFrame(rows, columns=["day", "gross", "net", "long_ex", "ic"]).set_index("day")


def report(name, R, hold=1):
    if not len(R):
        print(f"  {name:46s} no data"); return None
    h = len(R) // 2; a, b = R.iloc[:h], R.iloc[h:]
    ann = lambda x: x.mean() / x.std() * np.sqrt(252 / hold) if x.std() else np.nan
    out = {"signal": name, "days": len(R), "IC": R.ic.mean(), "IC_t": t_stat(R.ic), "gross_bps": R.gross.mean() * 100, "net_bps": R.net.mean() * 100,
           "Sharpe_net": ann(R.net), "Sh_1st": ann(a.net), "Sh_2nd": ann(b.net), "long_vs_avg_bps": R.long_ex.mean() * 100}
    print(f"  {name:46s} IC {out['IC']:+.3f} (t {out['IC_t']:+.1f}) | spread {out['gross_bps']:+6.1f} bps gross {out['net_bps']:+6.1f} net/day | "
          f"Sharpe net {out['Sharpe_net']:+.2f} (halves {out['Sh_1st']:+.2f} / {out['Sh_2nd']:+.2f}) | long leg vs avg {out['long_vs_avg_bps']:+.1f} bps")
    return out


if __name__ == "__main__":
    F, O = load()
    P = panel(F, O)
    S = P[P.kind == "STF"].copy()
    days = sorted(S.day.unique())
    print(f"panel: {len(S):,} stock-days, {S.sym.nunique()} stocks, {len(days)} days {days[0]} .. {days[-1]}\n")
    pd.set_option("display.width", 220)

    print("1) BUILD-UP CLASS -> next day (bps, futures)  [mean, t-stat, n]   r1 = close-close, ron = overnight, rid = next day open-close, r5 = 5 days")
    g = S.dropna(subset=["r1"]).groupby("bu")
    tab = pd.DataFrame({k: g[k].mean() * 100 for k in ("r1", "ron", "rid", "r3", "r5")}); tab["t_r1"] = g.r1.apply(t_stat); tab["n"] = g.size()
    tab["r1_1st"] = S[S.day < days[len(days)//2]].groupby("bu").r1.mean() * 100; tab["r1_2nd"] = S[S.day >= days[len(days)//2]].groupby("bu").r1.mean() * 100
    print(tab.round(2).to_string()); print()
    # relative to the day's average (removes the market move)
    S["r1x"] = S.r1 - S.groupby("day").r1.transform("mean"); S["r5x"] = S.r5 - S.groupby("day").r5.transform("mean")
    g = S.dropna(subset=["r1x"]).groupby("bu")
    print("   same, minus that day's average stock (pure stock-specific effect):")
    print(pd.DataFrame({"r1x_bps": g.r1x.mean() * 100, "t": g.r1x.apply(t_stat), "r5x_bps": g.r5x.mean() * 100, "t5": g.r5x.apply(t_stat)}).round(2).to_string()); print()

    print("2) CROSS-SECTIONAL SIGNALS: long top quintile / short bottom quintile each day, hold 1 day (net of 0.06 % round trip)")
    S["sec_med"] = S.groupby(["day", "sector"]).ret.transform("median")
    S["signed_oi"] = np.sign(S.ret) * S.oi_chg
    S["ret_x_oi"] = S.ret * S.oi_chg.clip(-50, 50)
    S["rev"] = -S.ret
    S["oi_only"] = S.oi_chg
    S["flow_conv"] = (np.sign(S.ret) * ((S.bu.isin(["LONG_BUILD", "SHORT_BUILD"])).astype(int) + (np.sign(S.lean.fillna(0)) == np.sign(S.ret)).astype(int)
                      + (np.sign(S.sec_med) == np.sign(S.ret)).astype(int)))
    S["vol_ret"] = S.ret * np.log(S.vol_rel.clip(0.2, 10))
    S["basis_chg_s"] = S.basis_chg
    S["near_res"] = -S.res_d; S["near_sup"] = -S.sup_d
    S["clv_s"] = S.clv
    sigs = ["ret", "rev", "oi_only", "signed_oi", "ret_x_oi", "lean", "pcr_chg", "basis_chg_s", "flow_conv", "vol_ret", "clv_s", "near_res", "near_sup", "brk_up"]
    res = []
    for sgn in sigs:
        r = report(sgn, ls_portfolio(S, sgn)); res.append(r) if r else None
    print("\n   same signals, 5-day hold (spread per 5 days, Sharpe annualised for 5-day steps, non-overlapping days only):")
    S5 = S[S.day.isin(days[::5])]
    for sgn in sigs:
        report(sgn + " (5d)", ls_portfolio(S5, sgn, "r5"), hold=5)
    print("\n   overnight only (close -> next open) and next-day intraday (open -> close):")
    for sgn in ("ret", "signed_oi", "lean", "flow_conv", "clv_s", "basis_chg_s"):
        report(sgn + " -> overnight", ls_portfolio(S, sgn, "ron")); report(sgn + " -> next day open-close", ls_portfolio(S, sgn, "rid"))

    print("\n3) MARKET TIMING: breadth of build-ups at day t -> Nifty futures next day")
    B = S.groupby("day").apply(lambda g: pd.Series({"score": ((g.bu.isin(["LONG_BUILD", "SHORT_COVER"])).sum() - (g.bu.isin(["SHORT_BUILD", "LONG_UNWIND"])).sum()) / len(g),
                                                    "adv": (g.ret > 0).mean(), "oi_flow": (np.sign(g.ret) * g.oi_chg).median(), "lean": g.lean.median()}))
    N = P[P.sym == "NIFTY"].set_index("day")[["ret", "r1", "ron", "rid", "oi_chg", "bu"]]
    M = B.join(N, how="inner").dropna(subset=["r1"])
    for col in ("score", "adv", "oi_flow", "lean"):
        q = pd.qcut(M[col], 5, labels=False, duplicates="drop")
        print(f"  {col:8s} corr with next-day Nifty {M[col].corr(M.r1):+.3f} | by quintile (bps): " + " ".join(f"{100*M.r1[q==k].mean():+5.0f}" for k in range(5))
              + f" | overnight corr {M[col].corr(M.ron):+.3f} | halves corr {M[col].iloc[:len(M)//2].corr(M.r1.iloc[:len(M)//2]):+.3f} / {M[col].iloc[len(M)//2:].corr(M.r1.iloc[len(M)//2:]):+.3f}")
    g = M.groupby("bu")
    print("  Nifty's own build-up -> next day (bps):", {k: (round(100 * v.r1.mean(), 1), len(v)) for k, v in g})
    S.to_csv(os.path.join(ROOT, "data_store", "research", "fno_flow_panel.csv.gz"), index=False, compression="gzip")
    pd.DataFrame([r for r in res if r]).to_csv(os.path.join(ROOT, "data_store", "research", "fno_flow_signals.csv"), index=False)
