"""RISE / FALL SCORE - Indian index futures, metals, commodities (2026-11-07, Nikhil: "can you add this indian, metals and commodity also").

Model: data_store/research/deriv/mscore_model.json (scripts/research/market_score_fit.py). Same page card and same logging / live
checking as the crypto score (app/cryptoscore.py); inputs are price, volume, open interest (Indian futures only) and the day's range.
    india       : Nifty, Bank Nifty futures (live from the 1-min futures file)   15m -> next 1 h, 1h -> next 6 h, 1D -> next day
    metals      : Gold, Silver, Copper, Aluminium (Yahoo 15-min)                  15m -> 1 h, 1h -> 6 h, 4h -> 24 h
    commodities : WTI crude, natural gas (Yahoo 15-min)                           same
Read-only, never trades. An indication, not advice.
"""
from __future__ import annotations

import csv
import importlib.util
import json
import math
import os
import time

import pandas as pd

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
MODEL = os.path.join(ROOT, "data_store", "research", "deriv", "mscore_model.json")
OUT = os.path.join(ROOT, "data_store", "livedesk", "marketscore")
FIELDS = ["logged", "group", "market", "tf", "bar_ts", "price", "p_fall", "horizon_bars", "outcome_ret", "fell", "hit"]
NAMES = {"ret1": "last bar move", "ret4": "last 4 bars move", "vol_rel": "volume vs normal", "oi4": "open interest change",
         "rng4": "range vs normal", "loc_day": "place in today's range", "ret_day": "today's move so far"}
VIEW_GROUP = {"india": "india", "metals": "metals", "global": "commodities"}
S: dict = {"cache": {}}


def _fit():
    if "fit" not in S:
        p = os.path.join(ROOT, "scripts", "research", "market_score_fit.py")
        spec = importlib.util.spec_from_file_location("market_score_fit", p); m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
        S["fit"] = m
    return S["fit"]


def _model():
    try:
        return json.load(open(MODEL))
    except Exception:
        return None


def _sig(x):
    return 1 / (1 + math.exp(-max(-30, min(30, x))))


def _bars15(group, key, ysym):
    F = _fit()
    return F.india_15m(key, live_only=True) if group == "india" else F.yahoo_15m(ysym)


def score_group(group):
    M = _model(); F = _fit()
    G = (M or {}).get("groups", {}).get(group)
    if not G:
        return {}
    out = {}; now_ms = time.time() * 1000
    for key, ysym in G["markets"].items():
        try:
            b = _bars15(group, key, ysym)
        except Exception as e:
            out[key] = {"err": str(e)[:80]}; continue
        for tf, md in G["tf"].items():
            m = md["bars"]
            y = F.features(F.to_bars(b, m, group == "india"), md["hz"])
            span = 86400_000 if m == "D" else m * 900_000
            if m == "D":
                y = y[y.day < time.strftime("%Y-%m-%d")] if time.strftime("%H:%M") < "15:30" else y      # today's day bar only after the close
            else:
                y = y[y.ts + span <= now_ms + 1]
            y = y.dropna(subset=M["feats"])
            if not len(y):
                continue
            rows = []
            for _, r in y.tail(5).iterrows():
                z = {f: max(-4, min(4, (r[f] - md["mu"][f]) / (md["sd"][f] or 1))) if pd.notna(r[f]) else 0.0 for f in M["feats"]}
                c = {f: md["coef"][f] * z[f] for f in M["feats"]}
                rows.append((int(r.ts), float(r.c), _sig(md["coef"]["bias"] + sum(c.values())), c))
            ts, px, p, c = rows[-1]
            top = sorted(c.items(), key=lambda kv: -abs(kv[1]))[:3]
            out.setdefault(key, {})[tf] = {"bar_ts": ts, "price": px, "p_fall": round(p, 3), "prev": [round(q[2], 3) for q in rows[:-1]],
                                           "change": round(p - rows[-2][2], 3) if len(rows) > 1 else None, "label": md["label"], "hz_bars": md["hz"],
                                           "drivers": [{"name": NAMES.get(f, f), "push": "fall" if v > 0 else "rise", "size": round(abs(v), 3)} for f, v in top]}
    return out


def _log(group, sc):
    os.makedirs(OUT, exist_ok=True); p = os.path.join(OUT, "scores.csv")
    have = set()
    if os.path.exists(p):
        with open(p) as f:
            for r in csv.DictReader(f):
                have.add((r["group"], r["market"], r["tf"], r["bar_ts"]))
    new = [{"logged": time.strftime("%Y-%m-%d %H:%M:%S"), "group": group, "market": k, "tf": tf, "bar_ts": v["bar_ts"], "price": v["price"],
            "p_fall": v["p_fall"], "horizon_bars": v["hz_bars"], "outcome_ret": "", "fell": "", "hit": ""}
           for k, T in sc.items() for tf, v in (T or {}).items() if isinstance(v, dict) and "p_fall" in v and (group, k, tf, str(v["bar_ts"])) not in have]
    if new:
        w = not os.path.exists(p)
        with open(p, "a", newline="") as f:
            wr = csv.DictWriter(f, fieldnames=FIELDS)
            if w:
                wr.writeheader()
            wr.writerows(new)


def _check(group):
    """Outcome = close `horizon_bars` bars of the same timeframe later (in trading bars, so nights / weekends are skipped)."""
    p = os.path.join(OUT, "scores.csv")
    if not os.path.exists(p):
        return
    L = pd.read_csv(p, dtype={"outcome_ret": object, "fell": object, "hit": object}); M = _model(); F = _fit()
    G = M["groups"][group]; changed = False
    todo = L[(L.group == group) & (L.outcome_ret.isna() | (L.outcome_ret == ""))]
    for (k, tf), sub in todo.groupby(["market", "tf"]):
        md = G["tf"].get(tf)
        if not md:
            continue
        try:
            y = F.to_bars(_bars15(group, k, G["markets"][k]), md["bars"], group == "india").reset_index(drop=True)
        except Exception:
            continue
        pos = {int(t): i for i, t in enumerate(y.ts)}
        for i in sub.index:
            j = pos.get(int(L.at[i, "bar_ts"]))
            if j is None or j + int(L.at[i, "horizon_bars"]) >= len(y):
                continue
            ret = float(y.c.iloc[j + int(L.at[i, "horizon_bars"])]) / float(L.at[i, "price"]) - 1
            L.at[i, "outcome_ret"] = f"{ret:.5f}"; L.at[i, "fell"] = str(int(ret < 0)); L.at[i, "hit"] = str(int((float(L.at[i, "p_fall"]) >= .5) == (ret < 0)))
            changed = True
    if changed:
        L.to_csv(p, index=False)


def live_stats(group):
    p = os.path.join(OUT, "scores.csv")
    if not os.path.exists(p):
        return {}
    L = pd.read_csv(p); L = L[(L.group == group) & L.hit.notna()]
    out = {}
    for tf in L.tf.unique():
        a = L[L.tf == tf]; lean = a[(a.p_fall >= .55) | (a.p_fall <= .45)]
        out[tf] = {"checked": int(len(a)), "hit_%": round(100 * a.hit.mean(), 1), "leaning": int(len(lean)),
                   "hit_leaning_%": round(100 * lean.hit.mean(), 1) if len(lean) else None}
    return out


def api(view):
    group = VIEW_GROUP.get(view, view)
    c = S["cache"].get(group)
    if c and time.time() - c[0] < 120:
        return c[1]
    M = _model() or {}
    G = (M.get("groups") or {}).get(group) or {}
    err = None
    try:
        sc = score_group(group); _log(group, sc); _check(group)
    except Exception as e:
        sc, err = {}, f"{type(e).__name__}: {str(e)[:140]}"
    tested = {tf: {"auc": (v.get("test") or {}).get("auc"), "hit_leaning_%": (v.get("test") or {}).get("hit_when_leaning_%"),
                   "test_bars": (v.get("test") or {}).get("test_bars"), "train_days": v.get("days"), "label": v.get("label")} for tf, v in (G.get("tf") or {}).items()}
    res = {"group": group, "at": time.strftime("%Y-%m-%d %H:%M:%S"), "scores": sc, "tested": tested, "live": live_stats(group), "err": err,
           "tfs": list((G.get("tf") or {}).keys()), "model_made": M.get("made"),
           "note": "Score = the model's chance that price is LOWER at the end of the window (price, volume, open interest and the day's range "
                   "only - no order-book history exists for these). Tested on unseen days; checked live from 7 Nov. Indication only, not advice."}
    S["cache"][group] = (time.time(), res)
    return res
