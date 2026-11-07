"""RISE / FALL SCORE FOR INDIAN INDEX FUTURES, METALS AND COMMODITIES - fit and honest check (2026-11-07, Nikhil: "can you add this
indian, metals and commodity also").

Same idea as the crypto score (scripts/research/crypto_score_fit.py), but these markets have no Binance-style long / short or taker data
in history, so the inputs are what exists for them:
  Nifty / Bank Nifty futures (2 years, 5-min with volume AND open interest, Upstox): last-bar and 4-bar return, volume vs normal,
      open-interest change over 4 bars, range vs normal, where the price sits in today's range, today's move so far
      timeframes (trading-session bars from 09:15): 15m -> next 1 h, 1h -> next 6 h (about one session), 1D -> next day
  Metals (Gold, Silver, Copper, Aluminium) and commodities (WTI crude, natural gas): Yahoo 15-min futures with volume (~100 days):
      the same inputs without open interest; 15m -> next 1 h, 1h -> next 6 h, 4h -> next 24 h
Logistic regression (plain numpy), pooled per group. Walk-forward: fit on the first 2/3 of the days, test on the last 1/3 never seen.
Writes data_store/research/deriv/mscore_model.json - read by app/marketscore.py.
"""
from __future__ import annotations

import json
import os

import numpy as np
import pandas as pd

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
MD = os.path.join(ROOT, "data_store", "market_data")
OUTP = os.path.join(ROOT, "data_store", "research", "deriv", "mscore_model.json")
GROUPS = {
    "india": {"markets": {"NIFTY": "NIFTY", "BANKNIFTY": "BANKNIFTY"}, "tf": {"15m": (1, 4, "next 1 h"), "1h": (4, 6, "next 6 h (about 1 session)"), "1D": ("D", 1, "next day")}},
    "metals": {"markets": {"GOLD": "GC=F", "SILVER": "SI=F", "COPPER": "HG=F", "ALUMINIUM": "ALI=F"}, "tf": {"15m": (1, 4, "next 1 h"), "1h": (4, 6, "next 6 h"), "4h": (16, 6, "next 24 h")}},
    "commodities": {"markets": {"CRUDE": "CL=F", "NATGAS": "NG=F"}, "tf": {"15m": (1, 4, "next 1 h"), "1h": (4, 6, "next 6 h"), "4h": (16, 6, "next 24 h")}},
}
FEATS = ["ret1", "ret4", "vol_rel", "oi4", "rng4", "loc_day", "ret_day"]


# ---------------------------------------------------------------- 15-min bars per market
def india_15m(sym, live_only=False):
    """Continuous front-month futures, 15-min session bars (09:15 ...) with volume and OI. History: Upstox expired-contract 5-min
    files + the rolling 5-min file; live: the 1-min file (Dhan), aggregated the same way."""
    parts = []
    if not live_only:
        h = os.path.join(MD, "history", f"upstox_{sym}_FUT_expired_5m.csv.gz")
        if os.path.exists(h):
            x = pd.read_csv(h); x["contract"] = x.expiry.astype(str); x["dt"] = pd.to_datetime(x.datetime.str[:19]); parts.append(x)
        r = pd.read_csv(os.path.join(MD, "nifty_futures_5m_upstox.csv"))
        r = r[r.contract.str.startswith(sym + "_FUT")]; r["dt"] = pd.to_datetime(r.ts.str[:19]); parts.append(r)
    m1 = pd.read_csv(os.path.join(MD, "nifty_futures_1m_upstox.csv"))
    m1 = m1[m1.contract.str.startswith(sym + "_FUT")]; m1["dt"] = pd.to_datetime(m1.ts.str[:19]); parts.append(m1)
    x = pd.concat([p[["contract", "dt", "open", "high", "low", "close", "volume", "oi"]] for p in parts], ignore_index=True)
    x = x[(x.dt.dt.strftime("%H:%M") >= "09:15") & (x.dt.dt.strftime("%H:%M") < "15:30")]
    x["day"] = x.dt.dt.strftime("%Y-%m-%d")
    front = x.groupby(["day", "contract"]).volume.sum().reset_index()
    front = front.loc[front.groupby("day").volume.idxmax(), ["day", "contract"]]
    x = x.merge(front, on=["day", "contract"])
    mins = x.dt.dt.hour * 60 + x.dt.dt.minute
    x["slot"] = (mins - 555) // 15
    g = x.sort_values("dt").groupby(["day", "slot"])
    b = g.agg(o=("open", "first"), h=("high", "max"), l=("low", "min"), c=("close", "last"), vol=("volume", "sum"), oi=("oi", "last"), dt=("dt", "first")).reset_index()
    b["ts"] = (b.dt.dt.tz_localize("Asia/Kolkata").astype("int64") // 10**6)
    return b.sort_values("ts").reset_index(drop=True)


_Y: dict = {}


def yahoo_15m(ysym):
    p = os.path.join(MD, "synaptic_intraday_all.csv")
    mt = os.path.getmtime(p)
    if _Y.get("mt") != mt:
        d = pd.read_csv(p); _Y.update(mt=mt, d=d[d.interval == "15m"])
    d = _Y["d"]; x = d[d.symbol == ysym].copy()
    x["dt"] = pd.to_datetime(x.datetime_utc.str[:19]); x = x.drop_duplicates("dt").sort_values("dt")
    x = x.rename(columns={"open": "o", "high": "h", "low": "l", "close": "c", "volume": "vol"})
    x["oi"] = np.nan; x["day"] = x.dt.dt.strftime("%Y-%m-%d")
    x["ts"] = x.dt.astype("int64") // 10**6
    return x[["ts", "day", "o", "h", "l", "c", "vol", "oi"]].reset_index(drop=True)


def to_bars(b, m, india):
    if m == 1:
        return b.copy()
    if m == "D":
        y = b.groupby("day").agg(ts=("ts", "first"), o=("o", "first"), h=("h", "max"), l=("l", "min"), c=("c", "last"), vol=("vol", "sum"), oi=("oi", "last"), n=("c", "size")).reset_index()
        return y[y.n >= 20].drop(columns="n")
    if india:
        b = b.copy(); b["grp"] = b.slot // m
        y = b.groupby(["day", "grp"]).agg(ts=("ts", "first"), o=("o", "first"), h=("h", "max"), l=("l", "min"), c=("c", "last"), vol=("vol", "sum"), oi=("oi", "last"), n=("c", "size")).reset_index()
    else:
        b = b.copy(); b["grp"] = b.ts // (m * 900_000)
        y = b.groupby("grp").agg(day=("day", "first"), ts=("ts", "first"), o=("o", "first"), h=("h", "max"), l=("l", "min"), c=("c", "last"), vol=("vol", "sum"), oi=("oi", "last"), n=("c", "size")).reset_index()
    return y[y.n == m].drop(columns="n").sort_values("ts").reset_index(drop=True)


def features(y, hz):
    y = y.copy()
    y["ret1"] = y.c.pct_change() * 100
    y["ret4"] = (y.c / y.c.shift(4) - 1) * 100
    y["vol_rel"] = np.log((y.vol + 1) / (y.vol.rolling(20, min_periods=5).median() + 1))
    y["oi4"] = ((y.oi / y.oi.shift(4) - 1) * 100).fillna(0) if y.oi.notna().any() else 0.0
    r = (y.h.rolling(4).max() - y.l.rolling(4).min()) / y.c
    y["rng4"] = r / r.rolling(60, min_periods=10).median() - 1
    dh = y.groupby("day").h.cummax(); dl = y.groupby("day").l.cummin(); do = y.groupby("day").o.transform("first")
    y["loc_day"] = ((y.c - dl) / (dh - dl)).fillna(0.5) - 0.5
    y["ret_day"] = (y.c / do - 1) * 100
    y["fwd"] = y.c.shift(-hz) / y.c - 1
    y["y"] = (y.fwd < 0).astype(float)
    return y


def fit_logit(X, yv, l2=1.0, it=3000, lr=0.1):
    w = np.zeros(X.shape[1] + 1); Xb = np.c_[np.ones(len(X)), X]
    for _ in range(it):
        p = 1 / (1 + np.exp(-Xb @ w)); w -= lr * (Xb.T @ (p - yv) / len(yv) + l2 * np.r_[0, w[1:]] / len(yv))
    return w


def auc(p, yv):
    o = np.argsort(p); r = np.empty(len(p)); r[o] = np.arange(1, len(p) + 1); n1 = yv.sum(); n0 = len(yv) - n1
    return float((r[yv == 1].sum() - n1 * (n1 + 1) / 2) / (n1 * n0)) if n1 and n0 else float("nan")


def run_group(gname, G):
    india = gname == "india"
    raw = {k: (india_15m(s) if india else yahoo_15m(s)) for k, s in G["markets"].items()}
    out = {"markets": G["markets"], "tf": {}}
    for tf, (m, hz, label) in G["tf"].items():
        X = []
        for k, b in raw.items():
            y = features(to_bars(b, m, india), hz); y["mk"] = k; X.append(y)
        X = pd.concat(X, ignore_index=True).replace([np.inf, -np.inf], np.nan).dropna(subset=FEATS + ["fwd"])
        days = sorted(X.day.unique()); cut_day = days[int(len(days) * 2 / 3)]
        tr = X[X.day < cut_day]; te = X[X.day > cut_day]
        mu, sd = tr[FEATS].mean(), tr[FEATS].std().replace(0, 1).fillna(1)
        z = lambda D: ((D[FEATS] - mu) / sd).clip(-4, 4).fillna(0).values
        w = fit_logit(z(tr), tr.y.values)
        p = 1 / (1 + np.exp(-(np.c_[np.ones(len(te)), z(te)] @ w)))
        yv = te.y.values; lean = (p >= 0.55) | (p <= 0.45)
        test = {"test_bars": int(len(te)), "auc": round(auc(p, yv), 3), "hit_when_leaning_%": round(100 * ((p[lean] >= .5) == (yv[lean] == 1)).mean(), 1) if lean.any() else None,
                "lean_share_%": round(100 * lean.mean(), 1), "base_fell_%": round(100 * yv.mean(), 1)}
        by = {k: round(auc(p[(te.mk == k).values], yv[(te.mk == k).values]), 3) for k in G["markets"] if (te.mk == k).sum() > 30}
        mu2, sd2 = X[FEATS].mean(), X[FEATS].std().replace(0, 1).fillna(1)
        w2 = fit_logit(((X[FEATS] - mu2) / sd2).clip(-4, 4).fillna(0).values, X.y.values)
        out["tf"][tf] = {"bars": m, "hz": hz, "label": label, "coef": dict(zip(["bias"] + FEATS, [round(float(v), 4) for v in w2])),
                         "mu": mu2.round(6).to_dict(), "sd": sd2.round(6).to_dict(), "test": test, "test_by_market": by,
                         "days": len(days), "from": days[0], "to": days[-1]}
        print(f"{gname:12s} {tf:4s} ({label}) days {days[0]}..{days[-1]} | test {test} | by market {by}")
    return out


if __name__ == "__main__":
    M = {"made": pd.Timestamp.now().isoformat(timespec="seconds"), "feats": FEATS, "groups": {}}
    for g, G in GROUPS.items():
        M["groups"][g] = run_group(g, G)
    json.dump(M, open(OUTP, "w"), indent=1, default=str)
