"""VOLUME-PROFILE LOOKBACK EDGE STUDY (2026-11-06, Nikhil: "so what is creating an edge? Find out with 1 day, 2 day, 3 day, 4 day, weekdays,
rolling set of days").

Question: which volume profile's POC / VAH / VAL (built from the last 1, 2, 3, 4, 5 sessions, the previous calendar week, the week so far,
or rolling 10 / 20 sessions) gives price levels that matter - and is any of it tradable after costs?

Data: Nifty / Bank Nifty FUTURES 5-min with volume (Upstox expired contracts, Jul / Aug 2024 - Sep 2026, ~520-540 sessions).
Front month per day = the contract with the most volume that day (no roll jumps). Volume at price: each 5-min bar's volume spread evenly
over its high-low range in fixed price buckets (Nifty 5 pts, Bank Nifty 20 pts). Value area = 70 % of volume around the POC.

Test A - level touches (09:20-15:00): the first time price trades through each level in a day, coming from one side
  (previous 5-min close on that side). A symmetric bracket from the level: +B in the reversal direction vs -B through the level,
  B = 0.25 % of price (~60 Nifty / ~140 Bank Nifty pts). Same-bar hits of both = counted as through (conservative). Unresolved by
  15:20 = closed at the 15:20 price.
    p_rev = share that reversed first. Random-walk baseline 50 %. Above 50 % = fade edge, below = break-through edge.
  CONTROL: the same test on fake levels = the real level moved by a random 0.30-0.80 % (same days, same procedure). The level's own
  edge = p_rev(real) - p_rev(control).
  Money: fade or break, 1R = B, cost = 4.5 pts Nifty / 12.5 pts Bank Nifty round trip (synthetic future) in R.
Test B - the open vs yesterday-type value areas: open above VAH / inside / below VAL -> how often the day closes inside the value area,
  and the "80 % rule": open outside, then two 30-min closes back inside -> does price reach the far side of the value area that day?
Both halves of the sample are reported; nothing is changed in any live rule.
Run: python3 scripts/research/vp_lookback_edge.py
"""
from __future__ import annotations

import json
import os
import sys

import numpy as np
import pandas as pd

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
H = os.path.join(ROOT, "data_store", "market_data", "history")
OUT = os.path.join(ROOT, "data_store", "research", "vp_lookback")
FILES = {"NIFTY": "upstox_NIFTY_FUT_expired_5m.csv.gz", "BANKNIFTY": "upstox_BANKNIFTY_FUT_expired_5m.csv.gz"}
BUCKET = {"NIFTY": 5.0, "BANKNIFTY": 20.0}
COST = {"NIFTY": 4.5, "BANKNIFTY": 12.5}
B_PCT = 0.0025
LOOKS = ["1d", "2d", "3d", "4d", "5d", "prev_week", "week_so_far", "10d", "20d"]
rng = np.random.default_rng(7)


def load(key):
    d = pd.read_csv(os.path.join(H, FILES[key]), usecols=["expiry", "datetime", "open", "high", "low", "close", "volume"])
    d["dt"] = pd.to_datetime(d.datetime.str[:19]); d["day"] = d.dt.dt.strftime("%Y-%m-%d")
    d = d[(d.dt.dt.strftime("%H:%M") >= "09:15") & (d.dt.dt.strftime("%H:%M") < "15:30")]
    vol = d.groupby(["day", "expiry"]).volume.sum().reset_index()
    front = vol.loc[vol.groupby("day").volume.idxmax(), ["day", "expiry"]]
    d = d.merge(front, on=["day", "expiry"]).sort_values("dt").drop_duplicates("dt")
    return d.reset_index(drop=True)


def day_vap(g, bk):
    """{bucket_price: volume} for one day's 5-min bars."""
    out = {}
    for h, l, v in zip(g.high.values, g.low.values, g.volume.values):
        if v <= 0:
            continue
        a, b = np.floor(l / bk) * bk, np.floor(h / bk) * bk
        n = int(round((b - a) / bk)) + 1
        for i in range(n):
            p = a + i * bk
            out[p] = out.get(p, 0.0) + v / n
    return out


def va(vap):
    if not vap:
        return None
    ps = sorted(vap); vs = np.array([vap[p] for p in ps]); tot = vs.sum()
    i = int(vs.argmax()); lo = hi = i; acc = vs[i]
    while acc < 0.7 * tot and (lo > 0 or hi < len(ps) - 1):
        a = vs[lo - 1] if lo > 0 else -1; b = vs[hi + 1] if hi < len(ps) - 1 else -1
        if a >= b:
            lo -= 1; acc += a
        else:
            hi += 1; acc += b
    return {"poc": ps[i], "vah": ps[hi], "val": ps[lo]}


def merge(vaps):
    out = {}
    for m in vaps:
        for p, v in m.items():
            out[p] = out.get(p, 0.0) + v
    return out


def levels_for(days, vaps, i):
    """Value areas for day i from each lookback (only data from before day i)."""
    d = pd.Timestamp(days[i]); res = {}
    for n in (1, 2, 3, 4, 5, 10, 20):
        if i >= n:
            res[f"{n}d"] = va(merge(vaps[i - n:i]))
    wk = d.isocalendar().week; yr = d.isocalendar().year
    prev = [j for j in range(max(0, i - 10), i) if (pd.Timestamp(days[j]).isocalendar().year, pd.Timestamp(days[j]).isocalendar().week) ==
            ((pd.Timestamp(d - pd.Timedelta(days=7)).isocalendar().year), (pd.Timestamp(d - pd.Timedelta(days=7)).isocalendar().week))]
    if prev:
        res["prev_week"] = va(merge([vaps[j] for j in prev]))
    cur = [j for j in range(max(0, i - 5), i) if pd.Timestamp(days[j]).isocalendar().week == wk and pd.Timestamp(days[j]).isocalendar().year == yr]
    if cur:
        res["week_so_far"] = va(merge([vaps[j] for j in cur]))
    return res


def touch_test(g, level, B):
    """First cross of `level` between 09:20 and 15:00 -> (+1 reversed first, -1 went through first, or the 15:20 result in units of B)."""
    hm = g.dt.dt.strftime("%H:%M").values
    H_, L_, C_ = g.high.values, g.low.values, g.close.values
    for j in range(1, len(g)):
        if not ("09:20" <= hm[j] <= "15:00"):
            continue
        if not (L_[j] <= level <= H_[j]):
            continue
        side = 1 if C_[j - 1] > level else -1 if C_[j - 1] < level else 0     # +1 = came down from above (support test)
        if side == 0:
            return None
        rev, thr = level + side * B, level - side * B
        for k in range(j, len(g)):
            hit_rev = (H_[k] >= rev) if side > 0 else (L_[k] <= rev)
            hit_thr = (L_[k] <= thr) if side > 0 else (H_[k] >= thr)
            if hit_thr:                       # same-bar both = through (conservative for the fade)
                return -1.0, side, hm[j]
            if hit_rev:
                return 1.0, side, hm[j]
            if hm[k] >= "15:20":
                return float(np.clip(side * (C_[k] - level) / B, -1, 1)), side, hm[j]
        return float(np.clip(side * (C_[-1] - level) / B, -1, 1)), side, hm[j]
    return None


def main():
    os.makedirs(OUT, exist_ok=True)
    rows, opens = [], []
    for key in FILES:
        d = load(key); bk = BUCKET[key]
        days = sorted(d.day.unique()); G = {k: g.reset_index(drop=True) for k, g in d.groupby("day")}
        vaps = [day_vap(G[x], bk) for x in days]
        print(key, len(days), "sessions", days[0], "->", days[-1], flush=True)
        for i in range(20, len(days)):
            g = G[days[i]]
            if len(g) < 60:
                continue
            px = float(g.open.iloc[0]); B = px * B_PCT
            LV = levels_for(days, vaps, i)
            for lk, v in LV.items():
                if not v:
                    continue
                for nm in ("poc", "vah", "val"):
                    lvl = v[nm]
                    r = touch_test(g, lvl, B)
                    if r:
                        rows.append({"key": key, "day": days[i], "look": lk, "level": nm, "kind": "real", "out": r[0], "side": r[1], "t": r[2], "B": B})
                    f = lvl * (1 + rng.choice([-1, 1]) * rng.uniform(0.003, 0.008))
                    r = touch_test(g, f, B)
                    if r:
                        rows.append({"key": key, "day": days[i], "look": lk, "level": nm, "kind": "control", "out": r[0], "side": r[1], "t": r[2], "B": B})
                # Test B: the open vs this value area
                o = px; hm = g.dt.dt.strftime("%H:%M").values; c = g.close.values; h_ = g.high.values; l_ = g.low.values
                loc = "above" if o > v["vah"] else "below" if o < v["val"] else "inside"
                close_in = v["val"] <= c[-1] <= v["vah"]
                # 80 % rule: two consecutive 30-min closes back inside (bars ending :45 / :15 after the open's 30 min)
                rule80 = None
                if loc != "inside":
                    half = [k for k in range(len(g)) if hm[k][3:] in ("40", "10")]      # 5-min bars that close each half hour (09:45, 10:15 ...)
                    inside = [k for k in half if v["val"] <= c[k] <= v["vah"]]
                    trig = next((k for a, k in zip(half, half[1:]) if a in inside and k in inside), None)
                    if trig is not None:
                        far = v["val"] if loc == "above" else v["vah"]
                        rule80 = bool(any((l_[k] <= far) if loc == "above" else (h_[k] >= far) for k in range(trig + 1, len(g))))
                opens.append({"key": key, "day": days[i], "look": lk, "open_at": loc, "close_in": close_in, "rule80": rule80,
                              "oc_dir": (np.sign(o - v["poc"]) * (c[-1] - o) / o * 100) if loc != "inside" else 0.0})
    R = pd.DataFrame(rows); O = pd.DataFrame(opens)
    R.to_csv(os.path.join(OUT, "touches.csv"), index=False); O.to_csv(os.path.join(OUT, "opens.csv"), index=False)
    # ---------------- summary
    def summ(x):
        n = len(x); p = (x.out > 0).mean() if n else np.nan
        return pd.Series({"n": n, "p_rev": round(100 * p, 1), "avg_R_fade": round(x.out.mean(), 3)})
    days_all = sorted(R.day.unique()); mid = days_all[len(days_all) // 2]
    out = {}
    for key in FILES:
        X = R[R.key == key]; cost_R = COST[key] / X.B.mean()
        T = []
        for lk in LOOKS:
            for nm in ("poc", "vah", "val"):
                a = X[(X.look == lk) & (X.level == nm)]
                r, c_ = a[a.kind == "real"], a[a.kind == "control"]
                if len(r) < 30:
                    continue
                h1, h2 = r[r.day < mid], r[r.day >= mid]
                T.append({"look": lk, "level": nm, "n": len(r), "p_rev": round(100 * (r.out > 0).mean(), 1), "ctrl_p_rev": round(100 * (c_.out > 0).mean(), 1),
                          "edge_pts": round(100 * ((r.out > 0).mean() - (c_.out > 0).mean()), 1),
                          "fade_R_net": round(r.out.mean() - cost_R, 3), "break_R_net": round(-r.out.mean() - cost_R, 3),
                          "fade_H1": round(h1.out.mean() - cost_R, 3), "fade_H2": round(h2.out.mean() - cost_R, 3),
                          "t": round(r.out.mean() / (r.out.std(ddof=1) / np.sqrt(len(r))), 2)})
        T = pd.DataFrame(T)
        print(f"\n===== {key}: level touches (fade = bet on a reversal at the level; 1R = 0.25 %; cost {cost_R:.3f} R) =====")
        print(T.to_string(index=False))
        Ok = O[O.key == key]
        S = []
        for lk in LOOKS:
            a = Ok[Ok.look == lk]
            if not len(a):
                continue
            out_ = a[a.open_at != "inside"]; r8 = a[a.rule80.notna()]
            S.append({"look": lk, "days": len(a), "open_inside_%": round(100 * (a.open_at == "inside").mean()), "close_in|open_inside_%": round(100 * a[a.open_at == "inside"].close_in.mean()),
                      "close_in|open_outside_%": round(100 * out_.close_in.mean()), "rule80_n": len(r8), "rule80_hit_%": round(100 * r8.rule80.astype(bool).mean()) if len(r8) else None,
                      "outside_open_follow_%": round(out_.oc_dir.mean(), 3), "follow_t": round(out_.oc_dir.mean() / (out_.oc_dir.std(ddof=1) / np.sqrt(len(out_))), 2) if len(out_) > 2 else None})
        S = pd.DataFrame(S)
        print(f"\n----- {key}: the open vs each value area -----")
        print(S.to_string(index=False))
        out[key] = {"touches": T.to_dict("records"), "opens": S.to_dict("records")}
    json.dump(out, open(os.path.join(OUT, "summary.json"), "w"), indent=1, default=str)


if __name__ == "__main__":
    main()
