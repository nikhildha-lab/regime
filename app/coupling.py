"""GLOBAL COUPLING - how Nifty / Bank Nifty / Sensex is moving with the world, and what the overnight implies for the open
(2026-11-09, Nikhil: "add coupling relation of index vs global indices .. test first" -> "yes you can build").

Built on MASTER_BACKTESTER/experiments/coupling/nifty_global_coupling.py (2016-26, 2,473 sessions). What that test showed, and what
this card therefore says and does not say:
  - the previous US session explains the GAP at 09:15 (corr +0.47) and nothing of the open-to-close SESSION (corr -0.05) -> the card
    gives an implied gap and an "already in the price" warning; it never gives a session direction from the overnight
  - after a strong US up-night the gap is up 89 % of the time, then the session fades -0.10 % on average (53 % fade) -> "don't chase"
  - a decoupled DAY (Nifty against the US night, both >= 0.5 %) is NOT followed by a catch-up (next day with the US 45-52 %)
  - a decoupled PERIOD (60-day corr in its lowest fifth) = normal returns, lower vol (13.9 vs 15.3 %), smaller gaps (0.35 vs 0.51 %),
    coupling back to ~+0.2 within 60 days; HIGH coupling = the risk-off phases with the big gaps
Data: data_store/market_data/yahoo_daily_deep.csv (daily OHLC, refreshed by the app). Cached 10 min. Description + overnight read only.
"""
from __future__ import annotations

import os
import time

import numpy as np
import pandas as pd

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SRC = os.path.join(ROOT, "data_store", "market_data", "yahoo_daily_deep.csv")
IDX = {"NIFTY": "^NSEI", "BANKNIFTY": "^NSEBANK", "SENSEX": "^BSESN"}
US = [("S&P 500", "^GSPC", False), ("Nasdaq fut", "NQ=F", False), ("Dow", "^DJI", False), ("Euro Stoxx", "^STOXX50E", False),
      ("DXY", "DX-Y.NYB", False), ("US 10y", "^TNX", True), ("VIX", "^VIX", False), ("WTI crude", "CL=F", False), ("Gold", "GC=F", False)]
ASIA = [("Nikkei", "^N225"), ("Hang Seng", "^HSI"), ("Kospi", "^KS11"), ("Shanghai", "000001.SS")]
S: dict = {"cache": {}, "px": None, "mt": 0}


def _load():
    mt = os.path.getmtime(SRC)
    if S["px"] is None or mt != S["mt"]:
        y = pd.read_csv(SRC, parse_dates=["date"])
        S["px"] = (y.pivot_table(index="date", columns="symbol", values="close").sort_index(), y.pivot_table(index="date", columns="symbol", values="open").sort_index())
        S["mt"] = mt
    return S["px"]


def _prev_ret(s, before, diff=False):
    s = s.dropna(); r = s.diff() if diff else s.pct_change()
    rr = r[r.index < before]
    return (float(rr.iloc[-1]), rr.index[-1]) if len(rr) else (None, None)


def build(key="NIFTY"):
    key = (key or "NIFTY").upper(); sym = IDX.get(key, "^NSEI")
    px, op = _load()
    N = pd.DataFrame({"close": px[sym], "open": op[sym]}).dropna()
    N = N[N.index >= "2016-11-01"]
    today = pd.Timestamp(time.strftime("%Y-%m-%d"))
    last = N.index[-1]
    live_today = last == today                         # today's row exists (open + running close)
    hist = N[N.index < today] if live_today else N
    hist = hist.assign(prev=hist.close.shift(1)); hist = hist.assign(gap=hist.open / hist.prev - 1, sess=hist.close / hist.open - 1, day=hist.close / hist.prev - 1)
    # previous US / Europe session before the next Indian open (= strictly before today, or before the last session if the file has no today row)
    ref = today if live_today or today > last else last
    over, X = [], {}
    for nm, ys, dif in US:
        if ys not in px:
            continue
        r, d = _prev_ret(px[ys], ref, dif)
        if r is None:
            continue
        over.append({"market": nm, "ret": round(r * (1 if dif else 100), 2), "unit": "pts" if dif else "%", "as_of": d.strftime("%a %d %b")})
        # history of this input aligned to every Indian session (previous close strictly before that session)
        rr = (px[ys].dropna().diff() if dif else px[ys].dropna().pct_change())
        X[nm] = pd.Series([rr[rr.index < d].iloc[-1] if (rr.index < d).any() else np.nan for d in hist.index], index=hist.index)
    H = hist.join(pd.DataFrame(X)).dropna(subset=["gap", "S&P 500"])
    # implied gap: 60-session regression of the gap on the S&P night (slope through origin + intercept)
    h60 = H.tail(60)
    b = np.polyfit(h60["S&P 500"], h60.gap, 1) if len(h60) >= 30 else (0.4, 0.0)
    us_now = next((o["ret"] / 100 for o in over if o["market"] == "S&P 500"), None)
    implied = (b[0] * us_now + b[1]) if us_now is not None else None
    q = H["S&P 500"].quantile([0.2, 0.8])
    night = "strong up" if us_now is not None and us_now >= q[0.8] else "strong down" if us_now is not None and us_now <= q[0.2] else "quiet"
    # historical outcome lines for this kind of night
    m = {"strong up": H["S&P 500"] >= q[0.8], "strong down": H["S&P 500"] <= q[0.2], "quiet": (H["S&P 500"] > q[0.2]) & (H["S&P 500"] < q[0.8])}[night]
    x = H[m]
    night_hist = {"n": int(len(x)), "gap_avg": round(100 * x.gap.mean(), 2), "gap_up": round(100 * (x.gap > 0).mean()), "sess_avg": round(100 * x.sess.mean(), 2),
                  "sess_up": round(100 * (x.sess > 0).mean()), "day_avg": round(100 * x.day.mean(), 2)}
    # today's actual gap (if the open is in) and where the session stands
    actual = None
    if live_today:
        r0 = N.loc[today]; pc = hist.close.iloc[-1]
        actual = {"gap": round(100 * (r0.open / pc - 1), 2), "sess_so_far": round(100 * (r0.close / r0.open - 1), 2), "open": round(float(r0.open), 1), "now": round(float(r0.close), 1)}
    # coupling table
    rows = []
    dayr = N.close.pct_change(fill_method=None)
    for nm, ys, dif in US:
        if nm not in X:
            continue
        s = X[nm].reindex(dayr.index)
        rows.append(_corr_row(nm + " (prev session)", dayr, s))
    for nm, ys in ASIA:
        if ys in px:
            rows.append(_corr_row(nm + " (same day)", dayr, px[ys].pct_change(fill_method=None).reindex(dayr.index)))
    # coupling state (60-day corr vs the US night, against its own 2016-26 fifths)
    roll = H.day.rolling(60).corr(H["S&P 500"])
    lo, hi = float(roll.quantile(0.2)), float(roll.quantile(0.8))
    c60 = float(roll.iloc[-1]) if len(roll.dropna()) else None
    state = None if c60 is None else "low" if c60 <= lo else "high" if c60 >= hi else "normal"
    # what followed each state historically (next 20 sessions)
    n20 = H.close.shift(-20) / H.close - 1
    vol20f = H.day[::-1].rolling(20).std()[::-1] * np.sqrt(252) * 100
    hist_state = {}
    for lab, mm in (("low", roll <= lo), ("normal", (roll > lo) & (roll < hi)), ("high", roll >= hi)):
        z = H[mm]
        hist_state[lab] = {"n": int(len(z)), "n20_avg": round(100 * n20[mm].mean(), 2), "n20_up": round(100 * (n20[mm] > 0).mean()), "vol": round(float(vol20f[mm].mean()), 1),
                           "gap_abs": round(100 * z.gap.abs().mean(), 2)}
    # decoupled day yesterday?
    yd = None
    if len(H) >= 1:
        last_row = H.iloc[-1]
        if abs(last_row.day) >= 0.005 and abs(last_row["S&P 500"]) >= 0.005 and np.sign(last_row.day) == -np.sign(last_row["S&P 500"]):
            yd = {"day": round(100 * last_row.day, 2), "us": round(100 * last_row["S&P 500"], 2), "date": H.index[-1].strftime("%a %d %b")}
    return {"key": key, "as_of": last.strftime("%Y-%m-%d"), "live_today": live_today, "overnight": over, "night": night, "night_hist": night_hist,
            "implied_gap": None if implied is None else round(100 * implied, 2), "beta_gap60": round(float(b[0]), 2), "actual": actual,
            "coupling": rows, "c60": None if c60 is None else round(c60, 2), "c60_lo": round(lo, 2), "c60_hi": round(hi, 2), "state": state, "state_hist": hist_state,
            "decoupled_yesterday": yd, "sessions": int(len(H)), "since": H.index[0].strftime("%Y-%m-%d"),
            "note": "From the 2016-26 test: the overnight sets the gap, not the session; decoupled days are not followed by a catch-up; low coupling = calmer, smaller gaps. Description and an open read only - never a session direction."}


def _corr_row(name, a, b):
    out = {"market": name}
    for n, lab in ((20, "c20"), (60, "c60"), (250, "c250")):
        x = pd.concat([a.tail(n), b.tail(n)], axis=1).dropna()
        out[lab] = round(float(x.iloc[:, 0].corr(x.iloc[:, 1])), 2) if len(x) >= n * 0.7 else None
    x = pd.concat([a.tail(60), b.tail(60)], axis=1).dropna()
    out["beta60"] = round(float(x.iloc[:, 0].cov(x.iloc[:, 1]) / x.iloc[:, 1].var()), 2) if len(x) >= 40 and x.iloc[:, 1].var() else None
    return out


def api(key="NIFTY"):
    key = (key or "NIFTY").upper()
    c = S["cache"].get(key)
    if c and time.time() - c[0] < 600:
        return c[1]
    try:
        r = build(key)
    except Exception as e:
        r = {"key": key, "err": f"{type(e).__name__}: {str(e)[:160]}"}
    S["cache"][key] = (time.time(), r)
    return r
