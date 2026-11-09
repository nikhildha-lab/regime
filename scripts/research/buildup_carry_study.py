"""BUILD-UP CARRY STUDY - every F&O stock (2026-11-09, Nikhil: "test all F&O stocks .. which succeed intraday, then which carry 1 to 5 days ..
was the build continued on each day, check all").

Data : data_store/research/fno_hist/fut_<day>.csv.gz - NSE daily futures per stock (open/high/low/close, OI, OI change), Jul 2024 - Nov 2026.
       Near-month contract per stock per day (largest OI). Index futures excluded (stocks only).
Build-up on day d (known at d's close), same definition as the F&O Flow board (fnoflow._bu): price change vs the previous close and
OI change (% of OI):  |move| >= 0.3 % and |dOI| >= 2 %.
       LONG_BUILD  price up, OI up      SHORT_BUILD price down, OI up      SHORT_COVER price up, OI down      LONG_UNWIND price down, OI down
       strong = the OI change is in the top third of that stock's own |dOI| history up to that day (no look-ahead).
Direction of the "trade" = the build-up direction: long after LONG_BUILD / SHORT_COVER, short after SHORT_BUILD / LONG_UNWIND.
Outcomes, all in the build-up direction, in % (futures prices), costs NOT deducted (a label study; 0.05 %/side would come off a trade):
   intraday  d+1 open -> d+1 close                 gap  d close -> d+1 open
   carry k   d close -> d+k close, k = 1..5
   continued each day: did the SAME build-up type repeat on d+1, d+2 .. (OI kept building with the move); and the carry conditional
   on it having continued on d+1.
Split 70 / 30 by date; t-stats vs that stock universe's average day. Per-stock ranking: which names carry best (needs >= 15 events).
Outputs: data_store/research/buildup_carry/{events.csv, report.txt}.   Run: python3 scripts/research/buildup_carry_study.py
"""
from __future__ import annotations

import glob
import os

import numpy as np
import pandas as pd

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
FH = os.path.join(ROOT, "data_store", "research", "fno_hist")
OUT = os.path.join(ROOT, "data_store", "research", "buildup_carry")
INDEX = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50", "SENSEX", "BANKEX"}
FLOOR_OI, FLOOR_PX = 2.0, 0.3
K = 5


def t(x):
    x = pd.Series(x).dropna(); return x.mean() / x.std() * np.sqrt(len(x)) if len(x) > 2 and x.std() else np.nan


def load():
    fs = sorted(glob.glob(os.path.join(FH, "fut_*.csv.gz")))
    D = pd.concat([pd.read_csv(f, usecols=["day", "sym", "kind", "expiry", "open", "high", "low", "close", "prev", "oi", "chg_oi", "und"]) for f in fs], ignore_index=True)
    D = D[(D.kind == "STF") & ~D.sym.isin(INDEX) & (D.oi > 0) & (D.close > 0) & (D.open > 0)]
    # near month = the contract with the largest OI that day
    D = D.sort_values(["day", "sym", "oi"], ascending=[True, True, False]).drop_duplicates(["day", "sym"])
    # total OI across expiries would be better for roll weeks; keep near-month OI but measure the change on the same contract
    D = D.sort_values(["sym", "day"]).reset_index(drop=True)
    g = D.groupby("sym")
    same = D.expiry == g.expiry.shift(1)
    D["px_chg"] = (D.close / g.close.shift(1) - 1) * 100
    D["oi_chg"] = np.where(same, (D.oi / g.oi.shift(1) - 1) * 100, np.nan)          # no OI change across a roll
    D["bu"] = np.select([D.oi_chg.isna() | (D.px_chg.abs() < FLOOR_PX) | (D.oi_chg.abs() < FLOOR_OI),
                         (D.px_chg > 0) & (D.oi_chg > 0), (D.px_chg < 0) & (D.oi_chg > 0), (D.px_chg > 0) & (D.oi_chg < 0)],
                        ["NEUTRAL", "LONG_BUILD", "SHORT_BUILD", "SHORT_COVER"], "LONG_UNWIND")
    # strong = |dOI| in the top third of the stock's own history so far (expanding 67th percentile, previous days only)
    D["q67"] = g.oi_chg.transform(lambda s: s.abs().expanding(min_periods=40).quantile(0.67).shift(1))
    D["strong"] = D.oi_chg.abs() >= D.q67
    # forward outcomes
    for k in range(1, K + 1):
        D[f"c{k}"] = (g.close.shift(-k) / D.close - 1) * 100
        D[f"bu{k}"] = g.bu.shift(-k)
    D["gap"] = (g.open.shift(-1) / D.close - 1) * 100
    D["intra"] = (g.close.shift(-1) / g.open.shift(-1) - 1) * 100
    D["sd"] = np.select([D.bu.isin(["LONG_BUILD", "SHORT_COVER"]), D.bu.isin(["SHORT_BUILD", "LONG_UNWIND"])], [1, -1], 0)
    return D


def main():
    os.makedirs(OUT, exist_ok=True)
    D = load()
    days = sorted(D.day.unique()); cut = days[int(len(days) * 0.7)]
    L = [f"BUILD-UP CARRY STUDY · {D.sym.nunique()} F&O stocks · {len(days)} sessions {days[0]} .. {days[-1]} · futures prices, % in the build-up direction, before costs · split at {cut}\n"]
    base = {"intra": D.intra.mean(), **{f"c{k}": D[f"c{k}"].mean() for k in range(1, K + 1)}}
    L.append(f"   all stock-days: intraday {base['intra']:+.3f}% · " + " · ".join(f"{k}d {base[f'c{k}']:+.2f}%" for k in range(1, K + 1)) + "  (unsigned averages = the drift of the universe)")
    L.append("\n1. AFTER EACH BUILD-UP TYPE (signed in the build-up direction; 'up%' = share positive)")
    L.append(f"   {'type':13s} {'strong':6s} {'n':>6s} | {'gap':>7s} | {'intraday':>9s} {'up%':>4s} {'t':>5s} | " + " ".join(f"{'carry '+str(k)+'d':>9s} {'up%':>4s}" for k in range(1, K + 1)) + " | train 3d / test 3d")
    rows_out = []
    for bu in ("LONG_BUILD", "SHORT_BUILD", "SHORT_COVER", "LONG_UNWIND"):
        for strong in (False, True):
            x = D[(D.bu == bu) & ((D.strong) if strong else True)]
            if len(x) < 50:
                continue
            s = x.sd
            sg = lambda c: (x[c] * s)
            a, b_ = x[x.day < cut], x[x.day >= cut]
            line = f"   {bu:13s} {'yes' if strong else 'all':6s} {len(x):6d} | {sg('gap').mean():+7.3f} | {sg('intra').mean():+9.3f} {100*(sg('intra')>0).mean():4.0f} {t(sg('intra') - s*0):+5.1f} | "
            line += " ".join(f"{sg(f'c{k}').mean():+9.2f} {100*(sg(f'c{k}')>0).mean():4.0f}" for k in range(1, K + 1))
            line += f" | {(a.c3*a.sd).mean():+.2f} / {(b_.c3*b_.sd).mean():+.2f}"
            L.append(line)
            rows_out.append({"type": bu, "strong": strong, "n": len(x), "intra": sg("intra").mean(), **{f"c{k}": sg(f"c{k}").mean() for k in range(1, K + 1)}})
    L.append("\n2. DID THE BUILD CONTINUE? share of the following days that showed the SAME build-up type again (OI kept building with the move)")
    L.append(f"   {'type':13s} {'strong':6s} " + " ".join(f"{'day +'+str(k):>8s}" for k in range(1, K + 1)) + "   | if it continued on day +1: carry to 3d / 5d (n)      if it did NOT: 3d / 5d (n)")
    for bu in ("LONG_BUILD", "SHORT_BUILD", "SHORT_COVER", "LONG_UNWIND"):
        for strong in (False, True):
            x = D[(D.bu == bu) & ((D.strong) if strong else True)]
            if len(x) < 50:
                continue
            cont = " ".join(f"{100*(x[f'bu{k}'] == bu).mean():7.0f}%" for k in range(1, K + 1))
            c1 = x[x.bu1 == bu]; n1 = x[(x.bu1 != bu) & x.bu1.notna()]
            L.append(f"   {bu:13s} {'yes' if strong else 'all':6s} {cont}   | {(c1.c3*c1.sd).mean():+.2f}% / {(c1.c5*c1.sd).mean():+.2f}% ({len(c1)})            {(n1.c3*n1.sd).mean():+.2f}% / {(n1.c5*n1.sd).mean():+.2f}% ({len(n1)})")
    L.append("\n3. TWO DAYS IN A ROW of the same build-up (d-1 and d) -> what follows d")
    D["bu_prev"] = D.groupby("sym").bu.shift(1)
    for bu in ("LONG_BUILD", "SHORT_BUILD"):
        x = D[(D.bu == bu) & (D.bu_prev == bu)]
        if len(x) >= 50:
            L.append(f"   {bu:13s} n {len(x):5d} | intraday {(x.intra*x.sd).mean():+.3f}% (up {100*((x.intra*x.sd)>0).mean():.0f}%) | " + " · ".join(f"{k}d {(x[f'c{k}']*x.sd).mean():+.2f}%" for k in range(1, K + 1)))
    L.append("\n4. WHICH STOCKS CARRY A STRONG BUILD-UP BEST (3-day carry in the build-up direction, >= 15 strong events; top 12 / bottom 6)")
    S = D[D.strong & D.bu.isin(["LONG_BUILD", "SHORT_BUILD"])].copy(); S["c3s"] = S.c3 * S.sd; S["intras"] = S.intra * S.sd
    per = S.groupby("sym").agg(n=("c3s", "size"), c3=("c3s", "mean"), up3=("c3s", lambda v: 100 * (v > 0).mean()), intra=("intras", "mean")).query("n >= 15").sort_values("c3", ascending=False)
    for sym, r in pd.concat([per.head(12), per.tail(6)]).iterrows():
        L.append(f"   {sym:12s} n {int(r.n):3d} | intraday {r.intra:+.3f}% | 3d {r.c3:+.2f}% (up {r.up3:.0f}%)")
    L.append(f"   universe: {len(per)} stocks with >= 15 strong events · median 3d carry {per.c3.median():+.2f}% · {int((per.c3 > 0).sum())} positive")
    L.append("\n5. SHORT BUILD-UP as a short (the BuildUp v2 family): strong, by half")
    x = D[(D.bu == "SHORT_BUILD") & D.strong]
    for lab, m in (("train", x.day < cut), ("test", x.day >= cut)):
        y = x[m]
        L.append(f"   {lab:5s} n {len(y):5d} | intraday {(y.intra*y.sd).mean():+.3f}% | " + " · ".join(f"{k}d {(y[f'c{k}']*y.sd).mean():+.2f}% (t {t(y[f'c{k}']*y.sd):+.1f})" for k in (1, 3, 5)))
    D[["day", "sym", "bu", "strong", "sd", "px_chg", "oi_chg", "gap", "intra"] + [f"c{k}" for k in range(1, K + 1)] + [f"bu{k}" for k in range(1, K + 1)]].to_csv(os.path.join(OUT, "events.csv"), index=False)
    txt = "\n".join(L); print(txt); open(os.path.join(OUT, "report.txt"), "w").write(txt)


if __name__ == "__main__":
    main()
