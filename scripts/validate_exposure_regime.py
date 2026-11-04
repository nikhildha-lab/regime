"""
Validate the five-state exposure regime the same way the spec does - and against fair benchmarks.

Data: Yahoo daily, full history (Nifty 2007->, India VIX 2008->). Evaluation 2011-01 -> 2026-09 (as in the spec),
plus two halves (2011-2018, 2019-2026) and 5 other markets.

Checks
 1  separation   Kruskal-Wallis on next-20-day returns by regime: all days (as the spec) AND non-overlapping
                 20-day samples (overlapping windows share 19 of 20 days, which makes p-values look far stronger)
 2  persistence  stay-probabilities and average run length per regime
 3  economics    long-only exposure curve (decided at close t, traded from t+1, cost 0.03% of exposure change)
                 vs buy & hold, vs CONSTANT exposure at the engine's own average (same average risk taken),
                 vs simple volatility targeting (exposure = 10% / 20-day vol, capped at 1)
 4  luck         200 random regime paths with the same run lengths and the same exposure mix (block shuffle)
 5  robustness   all 27 corners of the threshold ranges in the spec (ER 0.25/0.30/0.35, ADX 20/22.5/25, HV 70/80/85)
"""
from __future__ import annotations

import os
import sys
import warnings

import numpy as np
import pandas as pd
from scipy import stats

warnings.filterwarnings("ignore")
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from src.common.regime_engine import exposure_regime as XR  # noqa: E402

DEEP = "data_store/market_data/yahoo_daily_deep.csv"
START, END = "2011-01-01", "2026-09-30"
COST = 0.0003
MARKETS = {"NIFTY": ("^NSEI", "^INDIAVIX"), "BANKNIFTY": ("^NSEBANK", "^INDIAVIX"), "S&P 500": ("^GSPC", "^VIX"),
           "GOLD": ("GC=F", None), "CRUDE": ("CL=F", None), "BTC": ("BTC-USD", None)}


def load(sym):
    d = pd.read_csv(DEEP)
    d = d[d["symbol"] == sym].copy()
    d["date"] = pd.to_datetime(d["date"])
    d = d.drop_duplicates("date", keep="last").set_index("date").sort_index()
    return d[["open", "high", "low", "close", "volume"]].astype(float)


def curve(b: pd.DataFrame, w: pd.Series) -> pd.Series:
    """Daily return of holding exposure w (set at close t) from close t to close t+1, minus cost on changes."""
    r = b["close"].pct_change()
    w = w.reindex(b.index).fillna(0)
    held = w.shift(1).fillna(0)
    return held * r - COST * w.diff().abs().shift(1).fillna(0)


def metrics(ret: pd.Series) -> dict:
    ret = ret.dropna()
    eq = (1 + ret).cumprod()
    yrs = len(ret) / 252
    ann = eq.iloc[-1] ** (1 / yrs) - 1
    vol = ret.std() * np.sqrt(252)
    dd = (eq / eq.cummax() - 1).min()
    return {"total %": round((eq.iloc[-1] - 1) * 100, 1), "ann. return %": round(ann * 100, 1), "ann. vol %": round(vol * 100, 1),
            "Sharpe": round(ann / vol, 2) if vol else None, "max drawdown %": round(dd * 100, 1),
            "Calmar": round(ann / abs(dd), 2) if dd else None}


def block_shuffle(lab: pd.Series, rng) -> pd.Series:
    runs = []
    cur, n = None, 0
    for x in lab.values:
        if x == cur:
            n += 1
        else:
            if cur is not None:
                runs.append((cur, n))
            cur, n = x, 1
    runs.append((cur, n))
    idx = rng.permutation(len(runs))
    vals = np.concatenate([[runs[i][0]] * runs[i][1] for i in idx])
    return pd.Series(vals, index=lab.index)


def analyse(name, sym, ivsym, rng, full=True):
    b = load(sym)
    iv = load(ivsym)["close"] if ivsym else None
    ann = 365 if sym == "BTC-USD" else 252
    f = XR.features(b, iv, ann)
    lab = XR.label(f)
    w = XR.exposure(lab)
    keep = (b.index >= START) & (b.index <= END)
    b, f, lab, w = b[keep], f[keep], lab[keep], w[keep]
    out = {"market": name}
    # 1 separation
    fwd = b["close"].shift(-20) / b["close"] - 1
    d = pd.DataFrame({"lab": lab, "fwd": fwd}).dropna()
    groups = [g["fwd"].values for _, g in d.groupby("lab")]
    H, p = stats.kruskal(*groups)
    dn = d.iloc[::20]
    Hn, pn = stats.kruskal(*[g["fwd"].values for _, g in dn.groupby("lab") if len(g) > 2])
    out.update({"KW H (all days)": round(H, 1), "p (all days)": f"{p:.1e}", "KW H (non-overlap)": round(Hn, 1),
                "p (non-overlap)": f"{pn:.1e}"})
    per = d.groupby("lab")["fwd"].agg(["count", "mean", "median", "std"])
    per["next-20d realised vol %"] = pd.DataFrame({"lab": lab, "v": np.log(b["close"]).diff().shift(-1).rolling(20).std().shift(-19) * np.sqrt(ann) * 100}).groupby("lab")["v"].mean()
    # 2 persistence
    stay = {k: round(float(((lab == k) & (lab.shift(-1) == k)).sum() / max(1, (lab == k).sum())) * 100, 1) for k in XR.ORDER}
    runs = lab.groupby((lab != lab.shift()).cumsum()).agg(["first", "size"])
    dur = runs.groupby("first")["size"].mean().round(1).to_dict()
    # 3 economics
    eng = curve(b, w)
    bh = curve(b, pd.Series(1.0, index=b.index))
    avgw = float(w.mean())
    const = curve(b, pd.Series(avgw, index=b.index))
    vt = (0.10 / f["vol_20d"]).clip(upper=1.0)
    voltgt = curve(b, vt)
    rows = {"Buy & hold": metrics(bh), "Regime engine": metrics(eng), f"Constant {avgw:.2f}x (same avg exposure)": metrics(const),
            "Vol targeting 10%": metrics(voltgt)}
    w12 = XR.exposure(lab, {**XR.EXPOSURE, "Bull_Trend": 1.2})
    rows["Regime engine, Bull 1.2x"] = metrics(curve(b, w12))
    halves = {}
    for hn, (a, z) in {"2011-2018": ("2011-01-01", "2018-12-31"), "2019-2026": ("2019-01-01", "2026-09-30")}.items():
        m = (eng.index >= a) & (eng.index <= z)
        halves[hn] = {"engine": metrics(eng[m]), "constant same exposure": metrics(const[m]), "buy & hold": metrics(bh[m])}
    # 4 luck
    sims = []
    for _ in range(200 if full else 50):
        sl = block_shuffle(lab.fillna("Range"), rng)
        sims.append(metrics(curve(b, XR.exposure(sl))))
    sims = pd.DataFrame(sims)
    em = rows["Regime engine"]
    luck = {"engine max DD better than % of random paths": round(float((sims["max drawdown %"] < em["max drawdown %"]).mean() * 100)),
            "engine Sharpe better than % of random paths": round(float((sims["Sharpe"] < em["Sharpe"]).mean() * 100))}
    # 5 robustness
    grid = []
    for er in (0.25, 0.30, 0.35):
        for ax in (20, 22.5, 25):
            for hv in (70, 80, 85):
                full_lab = XR.label(XR.features(load(sym), iv, ann), er, ax, hv)[keep]
                m = metrics(curve(b, XR.exposure(full_lab)))
                grid.append({"ER": er, "ADX": ax, "HV pct": hv, **{k: m[k] for k in ("ann. return %", "Sharpe", "max drawdown %", "Calmar")}})
    grid = pd.DataFrame(grid)
    now = {"date": str(f.index[-1].date()), "regime": lab.iloc[-1], "close": round(float(b["close"].iloc[-1]), 1),
           "iv": round(float(f["iv"].iloc[-1]), 2) if "iv" in f and pd.notna(f["iv"].iloc[-1]) else None,
           "iv_pct": None if pd.isna(f["iv_pct"].iloc[-1]) else round(float(f["iv_pct"].iloc[-1])),
           "vol_20d %": round(float(f["vol_20d"].iloc[-1]) * 100, 1), "vol_pct": round(float(f["vol_pct"].iloc[-1])),
           "er_20": round(float(f["er_20"].iloc[-1]), 2), "adx": round(float(f["adx"].iloc[-1]), 1),
           "px_vs_ema200 %": round(float(f["px_vs_ema200"].iloc[-1]) * 100, 1), "dd_60d %": round(float(f["dd_60d"].iloc[-1]) * 100, 1)}
    return out, per, stay, dur, pd.DataFrame(rows).T, halves, luck, grid, now, lab


def main():
    rng = np.random.default_rng(11)
    pd.set_option("display.width", 220)
    summary, labels = [], {}
    for name, (sym, ivs) in MARKETS.items():
        out, per, stay, dur, econ, halves, luck, grid, now, lab = analyse(name, sym, ivs, rng, full=(name == "NIFTY"))
        labels[name] = lab
        print(f"\n================ {name} ================")
        print(out)
        print("forward 20-day return by regime:\n", (per[["count", "mean", "median", "std"]] * [1, 100, 100, 100]).round(2).assign(**{"next-20d vol %": per["next-20d realised vol %"].round(1)}).to_string())
        print("stay next day %:", stay, " avg run (days):", dur)
        print(econ.to_string())
        for h, v in halves.items():
            print(h, {k: (x["ann. return %"], x["Sharpe"], x["max drawdown %"]) for k, x in v.items()})
        print("luck:", luck)
        print("robustness (27 settings): Sharpe", grid["Sharpe"].min(), "to", grid["Sharpe"].max(), "| max DD", grid["max drawdown %"].min(), "to", grid["max drawdown %"].max())
        print("now:", now)
        e = econ.loc["Regime engine"]
        c = econ[econ.index.str.startswith("Constant")].iloc[0]
        summary.append({"market": name, "engine Sharpe": e["Sharpe"], "same-exposure Sharpe": c["Sharpe"], "buy&hold Sharpe": econ.loc["Buy & hold", "Sharpe"],
                        "vol-target Sharpe": econ.loc["Vol targeting 10%", "Sharpe"], "engine maxDD": e["max drawdown %"],
                        "same-exposure maxDD": c["max drawdown %"], "buy&hold maxDD": econ.loc["Buy & hold", "max drawdown %"],
                        "vol-target maxDD": econ.loc["Vol targeting 10%", "max drawdown %"], "KW p non-overlap": out["p (non-overlap)"],
                        "beats random DD %": luck["engine max DD better than % of random paths"],
                        "beats random Sharpe %": luck["engine Sharpe better than % of random paths"], "now": now["regime"]})
        os.makedirs("data_store/backtests", exist_ok=True)
        grid.to_csv(f"data_store/backtests/exposure_regime_grid_{name.replace(' ', '').replace('&', '')}.csv", index=False)
    s = pd.DataFrame(summary)
    print("\n================ SUMMARY ================\n", s.to_string(index=False))
    s.to_csv("data_store/backtests/exposure_regime_summary.csv", index=False)
    pd.DataFrame(labels).to_csv("data_store/backtests/exposure_regime_labels.csv")


if __name__ == "__main__":
    main()
