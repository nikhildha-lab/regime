"""
CHANCE OF A BOUNCE (and its mirror, chance of a pullback) - walk-forward tested.

Event at the close of day t: will price rise 1 ATR (bounce) before it falls another 1 ATR, within the next 5 days?
(first touch, using daily highs and lows; if both are touched on the same day the day counts as half).
Mirror for "pullback": fall 1 ATR before rising 1 ATR.

Model: logistic regression on 7 readings known at the close of t, pooled over 10 markets:
  z20 (distance from the 20-day average in sd), RSI14, 60-day drawdown, distance from the 20-day low and high in
  ATR, realised-vol percentile, and the exposure-regime label (bull / bear / other).
Walk-forward: each year from 2015 is predicted by a model fitted only on the years before it.
Judged by the Brier score (lower = better) against the simplest honest forecast, the running base rate.
Brier skill > 0 means the model adds information; we also show a reliability table (predicted vs what happened)
and the result on the days that matter: stretched down (z20 < -2 or RSI < 30) for bounces.
Saves the final model (fitted on everything) to data_store/models/bounce_model.json for the dashboard.
"""
from __future__ import annotations

import json
import os
import sys
import warnings

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))
os.chdir(ROOT)

from src.common.regime_engine import exposure_regime as XR  # noqa: E402
from src.common.regime_engine import levels as LV  # noqa: E402
from validate_exposure_regime import load  # noqa: E402

MARKETS = {"NIFTY": ("^NSEI", "^INDIAVIX"), "BANKNIFTY": ("^NSEBANK", "^INDIAVIX"), "S&P 500": ("^GSPC", "^VIX"),
           "ES": ("ES=F", "^VIX"), "NQ": ("NQ=F", "^VIX"), "GOLD": ("GC=F", None), "SILVER": ("SI=F", None),
           "COPPER": ("HG=F", None), "CRUDE": ("CL=F", None), "BTC": ("BTC-USD", None)}
FEATS = ["z20", "rsi", "dd60", "from_low", "from_high", "vol_pct", "bull", "bear"]
H, K = 5, 1.0


def features(b: pd.DataFrame, iv=None, ann=252) -> pd.DataFrame:
    f = XR.features(b, iv, ann)
    lab = XR.label(f)
    c, h, l = b["close"], b["high"], b["low"]
    a = LV.atr(b)
    d = pd.DataFrame(index=b.index)
    d["z20"] = ((c - c.rolling(20).mean()) / c.rolling(20).std()).clip(-4, 4)
    d["rsi"] = f["rsi"] / 100
    d["dd60"] = f["dd_60d"]
    d["from_low"] = ((c - l.rolling(20).min()) / a).clip(0, 10)
    d["from_high"] = ((h.rolling(20).max() - c) / a).clip(0, 10)
    d["vol_pct"] = f["vol_pct"] / 100
    d["bull"] = (lab == "Bull_Trend").astype(float)
    d["bear"] = (lab == "Bear_Trend").astype(float)
    d["atr"] = a
    return d


def outcomes(b: pd.DataFrame, a: pd.Series):
    c, h, l = b["close"].values, b["high"].values, b["low"].values
    n = len(c)
    up = np.full(n, np.nan)
    for i in range(n - H):
        u, dn = c[i] + K * a.iloc[i], c[i] - K * a.iloc[i]
        res = None
        for j in range(i + 1, i + H + 1):
            hu, hd = h[j] >= u, l[j] <= dn
            if hu and hd:
                res = 0.5
                break
            if hu:
                res = 1.0
                break
            if hd:
                res = 0.0
                break
        up[i] = 0.5 if res is None else res          # neither touched in 5 days: count as half
    return pd.Series(up, index=b.index)


def fit(X, y):
    from sklearn.linear_model import LogisticRegression
    # fractional labels: duplicate rows with weights
    Xr = np.vstack([X, X])
    yr = np.r_[np.ones(len(y)), np.zeros(len(y))]
    w = np.r_[y, 1 - y]
    m = LogisticRegression(C=1.0, max_iter=500)
    m.fit(Xr, yr, sample_weight=w)
    return m


def main():
    rows = []
    for name, (sym, ivs) in MARKETS.items():
        b = load(sym)
        b = b[(b["close"] > 0) & (b["low"] > 0)]
        iv = load(ivs)["close"] if ivs else None
        d = features(b, iv, 365 if sym == "BTC-USD" else 252)
        d["y"] = outcomes(b, d["atr"])
        d["market"] = name
        rows.append(d[d.index >= "2009-01-01"])
    D = pd.concat(rows).dropna(subset=FEATS + ["y"])
    D["year"] = D.index.year
    preds = []
    for yr in range(2015, D["year"].max() + 1):
        tr, te = D[D.year < yr], D[D.year == yr]
        if te.empty:
            continue
        m = fit(tr[FEATS].values, tr["y"].values)
        te = te.copy()
        te["p"] = m.predict_proba(te[FEATS].values)[:, 1]
        te["base"] = tr["y"].mean()
        preds.append(te)
    P = pd.concat(preds)
    bs = lambda p, y: float(np.mean((p - y) ** 2))

    def skill(x):
        return 1 - bs(x["p"], x["y"]) / bs(x["base"], x["y"])
    sd = (P["z20"] < -2) | (P["rsi"] < 0.30)
    su = (P["z20"] > 2) | (P["rsi"] > 0.70)
    rep = {"days": len(P), "bounce rate (all days) %": round(P["y"].mean() * 100, 1),
           "Brier skill all days %": round(skill(P) * 100, 2),
           "Brier skill 2015-20 %": round(skill(P[P.year <= 2020]) * 100, 2),
           "Brier skill 2021-26 %": round(skill(P[P.year >= 2021]) * 100, 2),
           "stretched-down days": int(sd.sum()), "bounce rate when stretched down %": round(P.loc[sd, "y"].mean() * 100, 1),
           "Brier skill stretched down %": round(skill(P[sd]) * 100, 2),
           "stretched-up days": int(su.sum()), "pullback rate when stretched up %": round((1 - P.loc[su, "y"]).mean() * 100, 1),
           "Brier skill stretched up %": round(skill(P[su]) * 100, 2)}
    # block bootstrap (by month) of the Brier difference, all days
    P["mon"] = P.index.to_period("M")
    g = P.groupby("mon").apply(lambda x: pd.Series({"a": ((x.p - x.y) ** 2).sum(), "b": ((x.base - x.y) ** 2).sum(), "n": len(x)}))
    rng = np.random.default_rng(5)
    bsk = []
    for _ in range(1000):
        s = g.sample(len(g), replace=True, random_state=rng.integers(1e9))
        bsk.append(1 - s.a.sum() / s.b.sum())
    rep["Brier skill 90% interval"] = f"{np.percentile(bsk, 5) * 100:+.2f}..{np.percentile(bsk, 95) * 100:+.2f}"
    # reliability
    P["bin"] = pd.cut(P["p"], [0, .3, .4, .45, .5, .55, .6, .7, 1])
    rel = P.groupby("bin").agg(days=("y", "size"), predicted=("p", "mean"), happened=("y", "mean")).dropna()
    rel = (rel.assign(predicted=lambda x: (x.predicted * 100).round(1), happened=lambda x: (x.happened * 100).round(1)))
    per = P.groupby("market").apply(lambda x: pd.Series({"skill %": round(skill(x) * 100, 2), "days": len(x)}))
    pd.set_option("display.width", 200)
    print(json.dumps(rep, indent=1))
    print("\nReliability (walk-forward):\n", rel.to_string())
    print("\nPer market:\n", per.to_string())
    # descriptive table for the dashboard: per market x regime x stretch, how often a 1-ATR bounce came first
    D["stretch"] = np.where((D["z20"] < -2) | (D["rsi"] < 0.30), "down", np.where((D["z20"] > 2) | (D["rsi"] > 0.70), "up", "none"))
    D["reg"] = np.where(D["bull"] == 1, "Bull_Trend", np.where(D["bear"] == 1, "Bear_Trend", "other"))
    tab = {}
    for (mk, rg, st), x in D.groupby(["market", "reg", "stretch"]):
        tab.setdefault(mk, {})[f"{rg}|{st}"] = {"n": int(len(x)), "bounce": round(float(x["y"].mean() * 100), 1)}
    for mk, x in D.groupby("market"):
        tab[mk]["all"] = {"n": int(len(x)), "bounce": round(float(x["y"].mean() * 100), 1)}
    # final model on everything, saved for the dashboard
    m = fit(D[FEATS].values, D["y"].values)
    os.makedirs("data_store/models", exist_ok=True)
    json.dump({"features": FEATS, "coef": m.coef_[0].round(5).tolist(), "intercept": float(m.intercept_[0]), "h": H, "k_atr": K,
               "trained_through": str(D.index.max().date()), "test": rep,
               "reliability": rel.reset_index().astype({"bin": str}).to_dict("records"),
               "per_market": per.reset_index().to_dict("records"), "table": tab},
              open("data_store/models/bounce_model.json", "w"), indent=1)
    print("\ncoefficients:", dict(zip(FEATS, m.coef_[0].round(3))))


if __name__ == "__main__":
    main()
