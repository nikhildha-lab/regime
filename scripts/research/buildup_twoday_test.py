"""BUILDUP variant test - "two strong short build-ups in a row" (2026-11-09, Nikhil, after the carry study: "yes").

Same trade mechanics as BuildUp v2 (app/fnoshort.py): SHORT the stock's most-held future at the NEXT session's open, stop 5 % above entry
checked on each day's high (filled at the stop, or at the open if it gaps above), exit at the close of the k-th session, costs 0.06 %
round trip, at most 5 signals a day (largest OI rise first). Data: data_store/research/fno_hist/fut_<day>.csv.gz, Jul 2024 - Nov 2026.
Signals (all known at the signal day's close):
  v2        BuildUp v2 as live: fall >= 2 %, total futures OI (all months) up >= 10 %, close in the lower half of the range, |move| <= 20 %,
            and NOT "late" (fnoshort._structure: near the 60-day low or below the last bullish order block)
  two-day   SHORT_BUILD on day d AND day d-1 (price down >= 0.3 %, near-month OI up >= 2 % both days), the OI rise on d in the top third of
            the stock's own history (expanding, previous days only); |move| <= 20 %
  two-day + late filter      the same, skipping "late" names like v2 does
  one-day strong             a single strong SHORT_BUILD day (for reference - the carry study's row)
Gauntlet: 70/30 by date, year by year, 2x / 3x costs, best 3 removed, hold 3 / 5 / 7 sessions, stop 3 / 5 / none.
Outputs: data_store/research/buildup_twoday/{trades.csv, report.txt}.   Run: python3 scripts/research/buildup_twoday_test.py
"""
from __future__ import annotations

import glob
import os
import sys

import numpy as np
import pandas as pd

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "app"))
FH = os.path.join(ROOT, "data_store", "research", "fno_hist")
OUT = os.path.join(ROOT, "data_store", "research", "buildup_twoday")
INDEX = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50", "SENSEX", "BANKEX"}
COST, MAXDAY = 0.06, 5


def t(x):
    x = pd.Series(x).dropna(); return x.mean() / x.std() * np.sqrt(len(x)) if len(x) > 2 and x.std() else np.nan


def load():
    fs = sorted(glob.glob(os.path.join(FH, "fut_*.csv.gz")))
    D = pd.concat([pd.read_csv(f, usecols=["day", "sym", "kind", "expiry", "open", "high", "low", "close", "prev", "oi"]) for f in fs], ignore_index=True)
    D = D[(D.kind == "STF") & ~D.sym.isin(INDEX) & (D.close > 0) & (D.open > 0)]
    tot = D.groupby(["day", "sym"]).oi.sum().rename("oi_tot").reset_index()
    N = D.sort_values(["day", "sym", "oi"], ascending=[True, True, False]).drop_duplicates(["day", "sym"]).merge(tot, on=["day", "sym"])
    N = N.sort_values(["sym", "day"]).reset_index(drop=True); g = N.groupby("sym")
    N["move"] = (N.close / N.prev - 1) * 100
    N["oi_tot_chg"] = (N.oi_tot / g.oi_tot.shift(1) - 1) * 100
    same = N.expiry == g.expiry.shift(1)
    N["oi_chg"] = np.where(same, (N.oi / g.oi.shift(1) - 1) * 100, np.nan)
    N["lower_half"] = N.close <= (N.high + N.low) / 2
    N["sb"] = (N.move <= -0.3) & (N.oi_chg >= 2.0)
    N["q67"] = g.oi_chg.transform(lambda s: s.abs().expanding(min_periods=40).quantile(0.67).shift(1))
    N["strong"] = N.oi_chg.abs() >= N.q67
    N["sb_prev"] = g.sb.shift(1).fillna(False).astype(bool)
    return N


def late_flags(N, sig):
    import fnoshort as FS
    out = {}
    for day, g in sig.groupby("day"):
        try:
            r = FS._structure(day, list(g.sym.unique()))
            for s, v in r.items():
                out[(day, s)] = bool(v[0]) if isinstance(v, (tuple, list)) else bool((v or {}).get("late"))
        except Exception:
            pass
    return out


def trade(N_by_sym, sym, day, hold, stop_pct):
    g = N_by_sym[sym]; i = g.index.get_loc(day)
    if i + hold >= len(g):
        return None
    e = float(g.open.iloc[i + 1]); stop = e * (1 + stop_pct / 100) if stop_pct else None
    for k in range(1, hold + 1):
        r = g.iloc[i + k]
        if stop is not None and k >= 1:
            if r.open >= stop:
                px = r.open; return {"pct": (e / px - 1) * 100 - COST, "why": "stop gap", "days": k}
            if r.high >= stop:
                return {"pct": (e / stop - 1) * 100 - COST, "why": "stop", "days": k}
    px = float(g.close.iloc[i + hold])
    return {"pct": (e / px - 1) * 100 - COST, "why": "time", "days": hold}


def main():
    os.makedirs(OUT, exist_ok=True)
    N = load(); days = sorted(N.day.unique())
    # signal sets
    v2 = N[(N.move <= -2) & (N.oi_tot_chg >= 10) & N.lower_half & (N.move.abs() <= 20)].copy()
    two = N[N.sb & N.sb_prev & N.strong & (N.move.abs() <= 20)].copy()
    one = N[N.sb & N.strong & (N.move.abs() <= 20)].copy()
    late = late_flags(N, pd.concat([v2, two]))
    v2["late"] = [late.get((d, s), False) for d, s in zip(v2.day, v2.sym)]; two["late"] = [late.get((d, s), False) for d, s in zip(two.day, two.sym)]
    sets = {"BuildUp v2 (live)": v2[~v2.late], "two-day strong SB": two, "two-day strong SB + late filter": two[~two.late], "one-day strong SB (ref)": one}
    by = {s: g.set_index("day") for s, g in N.groupby("sym")}
    L = [f"BUILDUP VARIANT TEST · {N.sym.nunique()} F&O stocks · {days[0]} .. {days[-1]} · short next open, 5 % stop on daily highs, exit close of 5th session, 0.06 % costs, max 5 signals a day\n"]
    cut = days[int(len(days) * 0.7)]
    rows_all = []
    for name, S_ in sets.items():
        S_ = S_.sort_values(["day", "oi_chg"], ascending=[True, False]).groupby("day").head(MAXDAY)
        res = []
        for d, s in zip(S_.day, S_.sym):
            r = trade(by, s, d, 5, 5.0)
            if r:
                res.append({"set": name, "day": d, "sym": s, **r})
        R = pd.DataFrame(res); rows_all.append(R)
        if R.empty:
            L.append(f"== {name}: no trades"); continue
        a, b_ = R[R.day < cut], R[R.day >= cut]
        eq = R.sort_values("day").pct.cumsum(); dd = (eq - eq.cummax()).min()
        yrs = " · ".join(f"{y}: {v:+.2f}% (n {n})" for y, (v, n) in R.groupby(R.day.str[:4]).pct.agg(["mean", "size"]).iterrows())
        L.append(f"== {name}: n {len(R)} · avg {R.pct.mean():+.2f}% (t {t(R.pct):+.1f}) · win {100*(R.pct>0).mean():.0f}% · total {R.pct.sum():+.0f}% · maxDD {dd:+.1f}% · stops {100*(R.why.str.startswith('stop')).mean():.0f}%")
        L.append(f"   train {a.pct.mean():+.2f}% (n {len(a)}) / test {b_.pct.mean():+.2f}% (n {len(b_)}) · years {yrs}")
        L.append(f"   stress: 2x costs {R.pct.mean()-COST:+.2f}% · 3x costs {R.pct.mean()-2*COST:+.2f}% · best 3 removed {R.pct.sort_values().iloc[:-3].mean():+.2f}% · signals/day avg {len(R)/R.day.nunique():.1f}")
    L.append("\nNEIGHBOURS for 'two-day strong SB + late filter' (hold x stop)")
    S_ = sets["two-day strong SB + late filter"].sort_values(["day", "oi_chg"], ascending=[True, False]).groupby("day").head(MAXDAY)
    for hold in (3, 5, 7):
        for stp in (3.0, 5.0, None):
            res = [trade(by, s, d, hold, stp) for d, s in zip(S_.day, S_.sym)]
            R = pd.DataFrame([dict(r, day=d) for r, d in zip(res, S_.day) if r])
            a, b_ = R[R.day < cut], R[R.day >= cut]
            L.append(f"   hold {hold}d · stop {stp if stp else 'none':>4} : n {len(R)} · avg {R.pct.mean():+.2f}% (t {t(R.pct):+.1f}) · win {100*(R.pct>0).mean():.0f}% · train {a.pct.mean():+.2f} / test {b_.pct.mean():+.2f}")
    pd.concat(rows_all).to_csv(os.path.join(OUT, "trades.csv"), index=False)
    txt = "\n".join(L); print(txt); open(os.path.join(OUT, "report.txt"), "w").write(txt)


if __name__ == "__main__":
    main()
