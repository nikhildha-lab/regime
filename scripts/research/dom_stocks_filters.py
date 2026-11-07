"""DOM STOCKS - would a volume spike / high-volume / ADX filter have helped? (2026-11-08, Nikhil: "DOM stocks should target high volume
stocks or a volume spike more than 4x of normal .. maybe add ADX > 25 also .. see what works from a backtesting aspect").

The DOM stock signals need the 20-level stock book, which exists only for the days it was recorded (3-6 Nov 2026). So this is not a
multi-year backtest: it takes every DOM stock paper trade actually recorded (data_store/livedesk/domcards/trades.json) and asks which
of them a filter would have kept, using the stocks' 1-min bars (data_store/research/stocks_intraday/<SYM>_1m.csv.gz).
Filter inputs, all known AT the entry minute (no look-ahead):
    spike5   volume of the 5 min before entry  /  median volume of the same 5 min of the day over the previous 10 sessions
    rvol     volume from 09:15 to entry         /  average volume to the same time over the previous 10 sessions
    adx5     ADX(14) on 5-min bars (Wilder), last completed bar before entry, previous days used for warm-up
    adx15    ADX(14) on 15-min bars
Result per trade = the recorded R after costs (r) and before costs (r_gross). Rs = R x 1,000 (the rule risks Rs 1,000 a trade).
Run: python3 scripts/research/dom_stocks_filters.py
"""
from __future__ import annotations

import json
import os

import numpy as np
import pandas as pd

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
TR = os.path.join(ROOT, "data_store", "livedesk", "domcards", "trades.json")
HIST = os.path.join(ROOT, "data_store", "research", "stocks_intraday")
INDEX = {"NIFTY", "BANKNIFTY", "SENSEX"}


def adx(b, n=14):
    h, l, c = b.high, b.low, b.close
    up, dn = h.diff(), -l.diff()
    pdm = np.where((up > dn) & (up > 0), up, 0.0); mdm = np.where((dn > up) & (dn > 0), dn, 0.0)
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    a = 1 / n
    atr = tr.ewm(alpha=a, adjust=False).mean()
    pdi = 100 * pd.Series(pdm, index=b.index).ewm(alpha=a, adjust=False).mean() / atr
    mdi = 100 * pd.Series(mdm, index=b.index).ewm(alpha=a, adjust=False).mean() / atr
    dx = 100 * (pdi - mdi).abs() / (pdi + mdi)
    return dx.ewm(alpha=a, adjust=False).mean(), pdi, mdi


def bars(d, rule):
    g = d.set_index("ts").groupby(d.set_index("ts").index.date)
    out = [x.resample(rule, origin="start_day", offset="9h15min", label="left", closed="left").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}).dropna() for _, x in g]
    return pd.concat(out)


def features(sym, rows):
    p = os.path.join(HIST, f"{sym}_1m.csv.gz")
    if not os.path.exists(p):
        return {}
    d = pd.read_csv(p, parse_dates=["ts"]).drop_duplicates("ts").sort_values("ts")
    d["day"] = d.ts.dt.date; d["mn"] = d.ts.dt.hour * 60 + d.ts.dt.minute - 555          # minutes since 09:15
    days = sorted(d.day.unique())
    vol = d.pivot_table(index="day", columns="mn", values="volume", aggfunc="sum").reindex(columns=range(0, 375)).fillna(0)
    cum = vol.cumsum(axis=1)
    b5, b15 = bars(d, "5min"), bars(d, "15min")
    a5, p5, m5 = adx(b5); a15, _, _ = adx(b15)
    out = {}
    for t in rows:
        ts = pd.Timestamp(t["opened"]); day = ts.date(); mn = ts.hour * 60 + ts.minute - 555
        if day not in vol.index or mn < 5:
            continue
        prev = [x for x in days if x < day][-10:]
        if len(prev) < 5:
            continue
        w = vol.loc[day, mn - 5:mn - 1].sum(); wn = vol.loc[prev, mn - 5:mn - 1].sum(axis=1).median()
        cu = cum.loc[day, mn - 1]; cn = cum.loc[prev, mn - 1].mean()
        k5 = b5.index[b5.index + pd.Timedelta(minutes=5) <= ts]; k15 = b15.index[b15.index + pd.Timedelta(minutes=15) <= ts]
        out[t["id"]] = {"spike5": w / wn if wn else np.nan, "rvol": cu / cn if cn else np.nan,
                        "adx5": float(a5.loc[k5[-1]]) if len(k5) else np.nan, "adx15": float(a15.loc[k15[-1]]) if len(k15) else np.nan,
                        "di_with": (1 if (p5.loc[k5[-1]] > m5.loc[k5[-1]]) == (t["side"] > 0 if isinstance(t["side"], (int, float)) else t["side"] == "long") else 0) if len(k5) else np.nan}
    return out


def line(name, x):
    if not len(x):
        return f"{name:44s}   n   0"
    days = x.groupby("day").r.sum()
    return (f"{name:44s}   n {len(x):3d} | win {100*(x.r > 0).mean():4.0f}% | avg R net {x.r.mean():+.2f} gross {x.r_gross.mean():+.2f} | "
            f"total Rs {1000*x.r.sum():+9,.0f} | days + {int((days > 0).sum())}/{len(days)}")


def run():
    T = json.load(open(TR)); T = T if isinstance(T, list) else T.get("trades", [])
    T = [t for t in T if t.get("key") not in INDEX and t.get("status") == "closed" and t.get("r") is not None]
    by = {}
    for t in T:
        by.setdefault(t["key"], []).append(t)
    F = {}
    for sym, rows in by.items():
        F.update(features(sym, rows))
    X = pd.DataFrame([{**{k: t.get(k) for k in ("id", "day", "key", "setup", "side", "opened", "r", "r_gross")}, **F[t["id"]]} for t in T if t["id"] in F])
    print(f"DOM stock trades closed: {len(T)} | with 1-min bars for the entry minute: {len(X)} | days {sorted(X.day.unique())}")
    print(f"missing bars (not tested): {len(T) - len(X)}\n")
    print(line("ALL", X))
    for s in ("hold", "break"):
        print(line(f"  setup {s}", X[X.setup == s]))
    print("\n-- volume spike (5 min before entry vs the same 5 min, last 10 sessions)")
    for k in (2, 3, 4, 6):
        print(line(f"spike5 >= {k}x", X[X.spike5 >= k]))
    print(line("spike5 <  2x", X[X.spike5 < 2]))
    print("\n-- high-volume day (volume so far vs normal to this time)")
    for k in (1.5, 2, 3):
        print(line(f"rvol >= {k}x", X[X.rvol >= k]))
    print(line("rvol <  1x", X[X.rvol < 1]))
    print("\n-- trend strength")
    for col in ("adx5", "adx15"):
        print(line(f"{col} > 25", X[X[col] > 25])); print(line(f"{col} <= 20", X[X[col] <= 20]))
    print(line("adx5 > 25 and DI with the trade", X[(X.adx5 > 25) & (X.di_with == 1)]))
    print(line("adx5 > 25 and DI against the trade", X[(X.adx5 > 25) & (X.di_with == 0)]))
    print("\n-- combinations")
    print(line("spike5 >= 4x and adx5 > 25", X[(X.spike5 >= 4) & (X.adx5 > 25)]))
    print(line("rvol >= 2x and adx5 > 25", X[(X.rvol >= 2) & (X.adx5 > 25)]))
    print(line("(spike5 >= 4x or rvol >= 2x) and adx5 > 25", X[((X.spike5 >= 4) | (X.rvol >= 2)) & (X.adx5 > 25)]))
    print(line("spike5 >= 4x and adx15 > 25", X[(X.spike5 >= 4) & (X.adx15 > 25)]))
    print("\nquartiles: spike5", X.spike5.quantile([.25, .5, .75, .9]).round(2).tolist(), "| rvol", X.rvol.quantile([.25, .5, .75, .9]).round(2).tolist(),
          "| adx5", X.adx5.quantile([.25, .5, .75]).round(1).tolist())
    out = os.path.join(ROOT, "data_store", "research", "dom_stocks_filters.csv"); X.to_csv(out, index=False); print("rows ->", out)


if __name__ == "__main__":
    run()
