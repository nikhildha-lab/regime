"""CRYPTO RISE / FALL SCORE - fit and honest check (2026-11-07, Nikhil: "for SOL, BTC, ETH I can expect a fall .. create 15 min, 1 hour,
4 hour .. do the analysis and then keep a prediction score increasing or decreasing even though we are never sure").

A simple, transparent model (logistic regression, plain numpy) per timeframe:
    15m bars -> chance the NEXT 1 HOUR closes lower
    1h  bars -> chance the NEXT 6 HOURS close lower
    4h  bars -> chance the NEXT 24 HOURS close lower
Inputs (all known at the bar close): last-bar and last-4-bar return, CVD (net market buying) over 1 and 4 bars as a share of volume,
open-interest change over 4 bars, all-account long / short vs its 30-day median, top-trader long / short change over 4 bars,
taker buy / sell ratio, funding, and the 4-bar range vs normal.
Data: data_store/research/deriv (15 Binance coins, last 30 days at 15 min). Pooled over all 15 coins.
Honest check: fit on the first 2/3 of the days, score the last 1/3 never seen in the fit (walk-forward). Reported: hit rate when the
model leans (P >= 55 % or <= 45 %), AUC (0.5 = coin flip), calibration by score band.
Also a 2-year check with the only inputs Binance keeps that long (1h candles: return + CVD, funding).
Writes data_store/research/deriv/score_model.json (coefficients, input scaling, test results) - read by app/cryptoscore.py.
"""
from __future__ import annotations

import glob
import json
import os

import numpy as np
import pandas as pd

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
D = os.path.join(ROOT, "data_store", "research", "deriv")
TF = {"15m": (1, 4), "1h": (4, 6), "4h": (16, 6)}          # (15-min bars per bar, horizon in bars)
FEATS = ["ret1", "ret4", "cvd1", "cvd4", "oi4", "glob_rel", "top4", "taker4", "fund", "rng4"]


def load15(c):
    k = pd.read_csv(os.path.join(D, f"kl15_{c}.csv")); d = pd.read_csv(os.path.join(D, f"der15_{c}.csv"))
    x = k.merge(d, on="ts", how="left").sort_values("ts").reset_index(drop=True)
    for col in ("oi", "top_pos", "glob_acc", "taker_bs"):
        x[col] = pd.to_numeric(x[col], errors="coerce").ffill()
    try:
        f = pd.read_csv(os.path.join(D, f"fund_{c}.csv")).sort_values("ts")
        x = pd.merge_asof(x, f.rename(columns={"rate": "fund"}), on="ts", direction="backward")
    except Exception:
        x["fund"] = 0.0
    return x


def bars(x, m):
    """Aggregate 15-min rows into m x 15-min bars (UTC-aligned)."""
    if m == 1:
        y = x.copy()
    else:
        g = (x.ts // (m * 900_000))
        y = x.groupby(g).agg(ts=("ts", "first"), o=("o", "first"), h=("h", "max"), l=("l", "min"), c=("c", "last"), vol=("vol", "sum"),
                             taker_buy=("taker_buy", "sum"), oi=("oi", "last"), top_pos=("top_pos", "last"), glob_acc=("glob_acc", "last"),
                             taker_bs=("taker_bs", "mean"), fund=("fund", "last"), n=("c", "size")).reset_index(drop=True)
        y = y[y.n == m].drop(columns="n")
    return y.reset_index(drop=True)


def features(y, hz):
    y = y.copy()
    y["delta"] = 2 * y.taker_buy - y.vol
    y["ret1"] = y.c.pct_change() * 100
    y["ret4"] = (y.c / y.c.shift(4) - 1) * 100
    y["cvd1"] = y.delta / y.vol
    y["cvd4"] = y.delta.rolling(4).sum() / y.vol.rolling(4).sum()
    y["oi4"] = (y.oi / y.oi.shift(4) - 1) * 100
    y["glob_rel"] = y.glob_acc / y.glob_acc.rolling(len(y), min_periods=20).median() - 1      # vs median so far (no look-ahead)
    y["top4"] = y.top_pos - y.top_pos.shift(4)
    y["taker4"] = y.taker_bs.rolling(4).mean() - 1
    y["fund"] = y.fund * 1e4                                                                   # basis points
    r = (y.h.rolling(4).max() - y.l.rolling(4).min()) / y.c
    y["rng4"] = r / r.rolling(len(y), min_periods=20).median() - 1
    y["fwd"] = y.c.shift(-hz) / y.c - 1
    y["y"] = (y.fwd < 0).astype(float)
    return y


def fit_logit(X, yv, l2=1.0, it=3000, lr=0.1):
    w = np.zeros(X.shape[1] + 1); Xb = np.c_[np.ones(len(X)), X]
    for _ in range(it):
        p = 1 / (1 + np.exp(-Xb @ w))
        g = Xb.T @ (p - yv) / len(yv) + l2 * np.r_[0, w[1:]] / len(yv)
        w -= lr * g
    return w


def auc(p, yv):
    o = np.argsort(p); r = np.empty(len(p)); r[o] = np.arange(1, len(p) + 1)
    n1 = yv.sum(); n0 = len(yv) - n1
    return float((r[yv == 1].sum() - n1 * (n1 + 1) / 2) / (n1 * n0)) if n1 and n0 else float("nan")


def evaluate(p, te):
    yv = te.y.values; lean = (p >= 0.55) | (p <= 0.45)
    hit = ((p[lean] >= 0.5) == (yv[lean] == 1)).mean() if lean.any() else float("nan")
    bands = []
    for lo_, hi_ in ((0, .40), (.40, .45), (.45, .50), (.50, .55), (.55, .60), (.60, 1.01)):
        m = (p >= lo_) & (p < hi_)
        if m.sum() >= 30:
            bands.append({"score": f"{int(lo_*100)}-{min(100, int(hi_*100))}%", "n": int(m.sum()), "fell_%": round(100 * yv[m].mean(), 1),
                          "avg_fwd_%": round(100 * te.fwd.values[m].mean(), 3)})
    return {"test_bars": int(len(te)), "auc": round(auc(p, yv), 3), "lean_share_%": round(100 * lean.mean(), 1),
            "hit_when_leaning_%": round(100 * hit, 1), "base_fell_%": round(100 * yv.mean(), 1), "bands": bands}


def run_tf(name, m, hz):
    rows = []
    for f in sorted(glob.glob(os.path.join(D, "der15_*.csv"))):
        c = os.path.basename(f)[6:-4]
        y = features(bars(load15(c), m), hz); y["coin"] = c
        rows.append(y)
    X = pd.concat(rows, ignore_index=True).replace([np.inf, -np.inf], np.nan).dropna(subset=FEATS + ["fwd"])
    cut = X.ts.quantile(2 / 3)
    tr, te = X[X.ts < cut], X[X.ts >= cut + hz * m * 900_000]                     # gap = horizon, so no overlap leaks
    mu, sd = tr[FEATS].mean(), tr[FEATS].std().replace(0, 1)
    clip = lambda Z: Z.clip(-4, 4)
    w = fit_logit(clip((tr[FEATS] - mu) / sd).values, tr.y.values)
    p = 1 / (1 + np.exp(-(np.c_[np.ones(len(te)), clip((te[FEATS] - mu) / sd).values] @ w)))
    res = evaluate(p, te)
    by_coin = {}
    for c in ("SOL", "BTC", "ETH"):
        mk = (te.coin == c).values
        if mk.sum() > 30:
            by_coin[c] = {"auc": round(auc(p[mk], te.y.values[mk]), 3), "n": int(mk.sum())}
    # final model on all days (what goes live); its honest quality = the walk-forward numbers above
    mu2, sd2 = X[FEATS].mean(), X[FEATS].std().replace(0, 1)
    w2 = fit_logit(clip((X[FEATS] - mu2) / sd2).values, X.y.values)
    coef = dict(zip(["bias"] + FEATS, [round(float(v), 4) for v in w2]))
    print(f"\n== {name}: bars of {m*15} min, predicts the next {hz*m*15/60:g} h | train {len(tr)} test {len(te)} rows ==")
    print("  walk-forward test:", {k: v for k, v in res.items() if k != "bands"})
    for b in res["bands"]:
        print("   ", b)
    print("  SOL / BTC / ETH test AUC:", by_coin)
    print("  live coefficients (+ = pushes toward FALL):", coef)
    return {"bar_min": m * 15, "horizon_h": hz * m * 15 / 60, "coef": coef, "mu": mu2.round(6).to_dict(), "sd": sd2.round(6).to_dict(),
            "test": res, "test_by_coin": by_coin, "train_days": round((X.ts.max() - X.ts.min()) / 86400_000, 1)}


def long_check():
    """2 years, 1-hour bars, inputs that exist that long: return 1 / 4 bars, CVD 1 / 4 bars, funding -> next 6 h lower?"""
    rows = []
    for f in sorted(glob.glob(os.path.join(D, "kl1h_*.csv"))):
        c = os.path.basename(f)[5:-4]; x = pd.read_csv(f).sort_values("ts").reset_index(drop=True)
        try:
            fu = pd.read_csv(os.path.join(D, f"fund_{c}.csv")).sort_values("ts")
            x = pd.merge_asof(x, fu.rename(columns={"rate": "fund"}), on="ts", direction="backward")
        except Exception:
            x["fund"] = 0.0
        x["delta"] = 2 * x.taker_buy - x.vol
        x["ret1"] = x.c.pct_change() * 100; x["ret4"] = (x.c / x.c.shift(4) - 1) * 100
        x["cvd1"] = x.delta / x.vol; x["cvd4"] = x.delta.rolling(4).sum() / x.vol.rolling(4).sum(); x["fund"] = x.fund * 1e4
        x["fwd"] = x.c.shift(-6) / x.c - 1; x["y"] = (x.fwd < 0).astype(float); x["coin"] = c
        rows.append(x)
    F = ["ret1", "ret4", "cvd1", "cvd4", "fund"]
    X = pd.concat(rows, ignore_index=True).replace([np.inf, -np.inf], np.nan).dropna(subset=F + ["fwd"])
    cut = X.ts.quantile(0.5)
    tr, te = X[X.ts < cut], X[X.ts >= cut + 6 * 3600_000]
    mu, sd = tr[F].mean(), tr[F].std()
    w = fit_logit(((tr[F] - mu) / sd).clip(-4, 4).values, tr.y.values)
    p = 1 / (1 + np.exp(-(np.c_[np.ones(len(te)), ((te[F] - mu) / sd).clip(-4, 4).values] @ w)))
    res = evaluate(p, te)
    print("\n== 2-year check, 1h bars, next 6 h (train year 1, test year 2):", {k: v for k, v in res.items() if k != "bands"})
    for b in res["bands"]:
        print("   ", b)
    print("   coefficients:", dict(zip(["bias"] + F, np.round(w, 4))))
    return res


if __name__ == "__main__":
    out = {"made": pd.Timestamp.now().isoformat(timespec="seconds"), "feats": FEATS, "tf": {}}
    for nm, (m, hz) in TF.items():
        out["tf"][nm] = run_tf(nm, m, hz)
    out["long_check_1h_2y"] = long_check()
    json.dump(out, open(os.path.join(D, "score_model.json"), "w"), indent=1, default=str)
