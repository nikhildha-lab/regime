"""OVERNIGHT - does the day before / the last hour tell us about the next day? (2026-11-06, Nikhil: "check new patterns for overnight ..
see Orderflow patterns for day before or the last hour of the day which may impacts next"). Paper research only.
Data: Nifty + Bank Nifty spot 5-min (18 Jul 2024 - 17 Jul 2026), NSE participant OI (FII index futures long - short, daily),
NSE index futures daily (near month: price + OI change). Year 1 = Jul 2024 - Jun 2025, Year 2 = Jul 2025 - Jul 2026.
Fixed BEFORE running:
  features (known after 15:30 / that evening): last-hour return (14:25 -> 15:25 close), last-30-min return, close location in the
     day range (0 = low, 1 = high), day return, FII index-futures net change (contracts, that day), futures price x OI build-up
     (long build-up / short covering / short build-up / long unwinding)
  targets (next day): gap (open / previous close), first hour (open -> 10:15), open -> close, close -> close
  pass = Spearman correlation with the SAME sign in both years, and the best-minus-worst fifth worth more than costs
     (synthetic future ~4.5 Nifty pts = ~0.02 %).
"""
import glob, os
import numpy as np, pandas as pd
DOC = os.path.expanduser("~/mnt/Documents"); MD = "data_store/market_data/"
def spot(sym):
    fs = sorted(glob.glob(f"{DOC}/nifty-live-tracker/datasets/2024-07-17_to_2026-07-19/*/{sym}_spot.csv"))
    B = pd.concat([pd.read_csv(f) for f in fs]); B["dt"] = pd.to_datetime(B.datetime); B = B.drop_duplicates("dt").sort_values("dt")
    B["day"] = B.dt.dt.strftime("%Y-%m-%d"); B["hm"] = B.dt.dt.strftime("%H:%M")
    rows = []
    for d, g in B.groupby("day"):
        g = g.set_index("hm")
        if len(g) < 60 or "14:25" not in g.index or "10:10" not in g.index: continue
        c, o, h, l = g.close.iloc[-1], g.open.iloc[0], g.high.max(), g.low.min()
        rows.append(dict(day=d, o=o, h=h, l=l, c=c, c1425=g.close["14:25"], c1455=g.close[g.index <= "14:55"].iloc[-1], c1010=g.close["10:10"]))
    D = pd.DataFrame(rows).set_index("day")
    D["lh"] = (D.c / D.c1425 - 1) * 100; D["l30"] = (D.c / D.c1455 - 1) * 100; D["cloc"] = (D.c - D.l) / (D.h - D.l)
    D["dret"] = (D.c / D.c.shift() - 1) * 100
    n = D.shift(-1)
    D["gap"] = (n.o / D.c - 1) * 100; D["h1"] = (n.c1010 / n.o - 1) * 100; D["oc"] = (n.c / n.o - 1) * 100; D["cc"] = (n.c / D.c - 1) * 100
    return D.dropna(subset=["gap"])
P = pd.read_csv(MD + "nse_participant_oi_2023_2026.csv"); P = P[P.client_type.str.upper().str.startswith("FII")]
P = P.assign(fii_net=P.fut_idx_long - P.fut_idx_short).set_index("date").fii_net; P = P.diff().rename("fii_chg")
F = pd.read_csv(MD + "nse_index_futures_2023_2026.csv")
res = []
for sym in ("NIFTY", "BANKNIFTY"):
    D = spot(sym).join(P, how="left")
    f = F[F.symbol == sym].sort_values(["date", "expiry"]).groupby("date").first()
    f = f.assign(px_chg=f.close.diff(), oi_chg=f.open_interest.diff())
    f["build"] = np.select([(f.px_chg > 0) & (f.oi_chg > 0), (f.px_chg > 0) & (f.oi_chg <= 0), (f.px_chg <= 0) & (f.oi_chg > 0)],
                           ["long build-up", "short covering", "short build-up"], "long unwinding")
    same_exp = f.expiry.eq(f.expiry.shift())                         # OI change across a roll is meaningless
    f.loc[~same_exp, "build"] = None
    D = D.join(f[["build"]], how="left"); D["year"] = np.where(D.index < "2025-07-01", "Y1", "Y2")
    print(f"\n===== {sym}: {len(D)} days =====")
    for x in ("lh", "l30", "cloc", "dret", "fii_chg"):
        for y in ("gap", "h1", "oc", "cc"):
            r = {yr: D[D.year == yr][[x, y]].dropna().corr(method="spearman").iloc[0, 1] for yr in ("Y1", "Y2")}
            q = {}
            for yr in ("Y1", "Y2"):
                Z = D[D.year == yr][[x, y]].dropna(); Z["q"] = pd.qcut(Z[x].rank(method="first"), 5, labels=False)
                m = Z.groupby("q")[y].mean(); q[yr] = m.iloc[4] - m.iloc[0]
            ok = np.sign(r["Y1"]) == np.sign(r["Y2"]) and min(abs(q["Y1"]), abs(q["Y2"])) > 0.02 and np.sign(q["Y1"]) == np.sign(q["Y2"])
            res.append(dict(sym=sym, feature=x, target=y, rho_Y1=round(r["Y1"], 3), rho_Y2=round(r["Y2"], 3),
                            top_minus_bottom_Y1=round(q["Y1"], 3), top_minus_bottom_Y2=round(q["Y2"], 3), PASS=ok))
    print("futures build-up (day t) -> next day, mean %:")
    print(D.groupby(["build", "year"])[["gap", "h1", "oc", "cc"]].mean().round(3).unstack("year").to_string())
    print(D.groupby(["build", "year"]).size().unstack().to_string())
    D.to_csv(f"data_store/research/overnight_{sym}.csv")
R = pd.DataFrame(res); print("\nfeature -> next-day target (Spearman rho by year; top-minus-bottom fifth, % points):")
print(R.to_string(index=False))
