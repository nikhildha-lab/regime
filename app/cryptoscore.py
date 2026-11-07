"""CRYPTO RISE / FALL SCORE - live (2026-11-07, Nikhil: "for SOL, BTC, ETH ... create 15 min, 1 hour, 4 hour ... keep a prediction score
increasing or decreasing even though we are never sure").

Every 15 min: fresh Binance data for SOL, BTC, ETH (app/derivhist.py), then the score from the model in
data_store/research/deriv/score_model.json (scripts/research/crypto_score_fit.py - logistic regression, re-fitted when re-run):
    15m -> chance the next 1 h closes lower      1h -> next 6 h      4h -> next 24 h
Each new bar's score is logged (data_store/livedesk/cryptoscore/scores.csv) and checked once its horizon has passed, so the page shows
the model's LIVE hit rate beside its tested one. Read-only, never trades. An indication, not advice.
"""
from __future__ import annotations

import csv
import importlib.util
import json
import math
import os
import threading
import time

import pandas as pd

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
D = os.path.join(ROOT, "data_store", "research", "deriv")
OUT = os.path.join(ROOT, "data_store", "livedesk", "cryptoscore")
COINS = ("SOL", "BTC", "ETH")
TFS = {"15m": (1, 4), "1h": (4, 6), "4h": (16, 6)}
FIELDS = ["logged", "coin", "tf", "bar_ts", "price", "p_fall", "horizon_end", "outcome_ret", "fell", "hit"]
NAMES = {"ret1": "last bar move", "ret4": "last 4 bars move", "cvd1": "CVD last bar", "cvd4": "CVD last 4 bars", "oi4": "open interest change",
         "glob_rel": "crowd long / short", "top4": "top traders long / short change", "taker4": "taker buy / sell", "fund": "funding",
         "rng4": "range vs normal"}
S: dict = {"t": 0, "running": False, "last": None, "err": None}


def _fit_mod():
    p = os.path.join(ROOT, "scripts", "research", "crypto_score_fit.py")
    spec = importlib.util.spec_from_file_location("crypto_score_fit", p)
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    return m


def _model():
    try:
        return json.load(open(os.path.join(D, "score_model.json")))
    except Exception:
        return None


def _sig(x):
    return 1 / (1 + math.exp(-max(-30, min(30, x))))


def score_now():
    """Latest COMPLETE bar per coin x timeframe -> p_fall, top drivers, previous bar's score."""
    M = _model(); F = _fit_mod()
    if not M:
        return {}
    out = {}
    now_ms = time.time() * 1000
    for c in COINS:
        try:
            x = F.load15(c)
        except Exception as e:
            out[c] = {"err": str(e)[:80]}; continue
        for tf, (m, hz) in TFS.items():
            md = M["tf"].get(tf)
            if not md:
                continue
            y = F.features(F.bars(x, m), hz)
            y = y[y.ts + m * 900_000 <= now_ms + 1]                     # only bars that have closed
            y = y.dropna(subset=M["feats"])
            if not len(y):
                continue
            rows = []
            for _, r in y.tail(5).iterrows():
                z = {f: max(-4, min(4, (r[f] - md["mu"][f]) / (md["sd"][f] or 1))) for f in M["feats"]}
                contrib = {f: md["coef"][f] * z[f] for f in M["feats"]}
                p = _sig(md["coef"]["bias"] + sum(contrib.values()))
                rows.append((int(r.ts), float(r.c), p, contrib))
            ts, px, p, contrib = rows[-1]
            top = sorted(contrib.items(), key=lambda kv: -abs(kv[1]))[:3]
            out.setdefault(c, {})[tf] = {"bar_ts": ts, "bar_end": ts + m * 900_000, "price": px, "p_fall": round(p, 3),
                                         "prev": [round(q[2], 3) for q in rows[:-1]], "change": round(p - rows[-2][2], 3) if len(rows) > 1 else None,
                                         "horizon_h": md["horizon_h"],
                                         "drivers": [{"name": NAMES.get(f, f), "push": "fall" if v > 0 else "rise", "size": round(abs(v), 3)} for f, v in top]}
    return out


def _log(sc):
    os.makedirs(OUT, exist_ok=True)
    p = os.path.join(OUT, "scores.csv")
    have = set()
    if os.path.exists(p):
        with open(p) as f:
            for r in csv.DictReader(f):
                have.add((r["coin"], r["tf"], r["bar_ts"]))
    new = []
    for c, T in sc.items():
        for tf, v in (T or {}).items():
            if not isinstance(v, dict) or "p_fall" not in v:
                continue
            k = (c, tf, str(v["bar_ts"]))
            if k not in have:
                new.append({"logged": time.strftime("%Y-%m-%d %H:%M:%S"), "coin": c, "tf": tf, "bar_ts": v["bar_ts"], "price": v["price"],
                            "p_fall": v["p_fall"], "horizon_end": int(v["bar_end"] + v["horizon_h"] * 3600_000), "outcome_ret": "", "fell": "", "hit": ""})
    if new:
        w = not os.path.exists(p)
        with open(p, "a", newline="") as f:
            wr = csv.DictWriter(f, fieldnames=FIELDS)
            if w:
                wr.writeheader()
            wr.writerows(new)


def _check():
    """Fill in the outcome of logged scores whose horizon has passed (close at the horizon end vs the bar close)."""
    p = os.path.join(OUT, "scores.csv")
    if not os.path.exists(p):
        return
    L = pd.read_csv(p, dtype={"outcome_ret": object, "fell": object, "hit": object})
    now = time.time() * 1000; changed = False
    for c in COINS:
        try:
            k = pd.read_csv(os.path.join(D, f"kl15_{c}.csv"), usecols=["ts", "c"]).sort_values("ts")
        except Exception:
            continue
        k["end"] = k.ts + 900_000
        for i in L.index[(L.coin == c) & (L.outcome_ret.isna() | (L.outcome_ret == "")) & (L.horizon_end <= now - 60_000)]:
            m = k[k.end <= L.at[i, "horizon_end"]]
            if not len(m):
                continue
            ret = float(m.c.iloc[-1]) / float(L.at[i, "price"]) - 1
            fell = ret < 0
            L.at[i, "outcome_ret"] = f"{ret:.5f}"; L.at[i, "fell"] = str(int(fell)); L.at[i, "hit"] = str(int((float(L.at[i, "p_fall"]) >= 0.5) == fell))
            changed = True
    if changed:
        L.to_csv(p, index=False)


def live_stats():
    p = os.path.join(OUT, "scores.csv")
    if not os.path.exists(p):
        return {}
    L = pd.read_csv(p); L = L[L.hit.notna()]
    out = {}
    for tf in TFS:
        a = L[L.tf == tf]
        lean = a[(a.p_fall >= 0.55) | (a.p_fall <= 0.45)]
        out[tf] = {"checked": int(len(a)), "hit_%": round(100 * a.hit.mean(), 1) if len(a) else None,
                   "leaning": int(len(lean)), "hit_leaning_%": round(100 * lean.hit.mean(), 1) if len(lean) else None}
    return out


def _run():
    S["running"] = True
    try:
        DH = __import__("derivhist")
        for c in COINS:
            try:
                DH.fetch_coin(c, days15=2, quick=True)
            except Exception as e:
                S["err"] = f"{c}: {str(e)[:100]}"
        sc = score_now()
        _log(sc); _check()
        S["last"] = {"at": time.strftime("%Y-%m-%d %H:%M:%S"), "scores": sc}
    except Exception as e:
        S["err"] = f"{type(e).__name__}: {str(e)[:140]}"
    finally:
        S["running"] = False


def tick(force=False):
    """At most every 5 min, in the background (fetch + score + log + check)."""
    if S["running"] or (not force and time.time() - S["t"] < 300):
        return None
    S["t"] = time.time()
    threading.Thread(target=_run, daemon=True, name="cryptoscore").start()
    return True


def api():
    if not S.get("last"):
        try:
            S["last"] = {"at": None, "scores": score_now()}
        except Exception as e:
            S["err"] = str(e)[:140]
    M = _model() or {}
    tested = {tf: {"auc": (v.get("test") or {}).get("auc"), "hit_leaning_%": (v.get("test") or {}).get("hit_when_leaning_%"),
                   "test_bars": (v.get("test") or {}).get("test_bars"), "by_coin": v.get("test_by_coin"), "horizon_h": v.get("horizon_h"),
                   "train_days": v.get("train_days")} for tf, v in (M.get("tf") or {}).items()}
    return {**(S.get("last") or {}), "tested": tested, "live": live_stats(), "model_made": M.get("made"), "err": S.get("err"),
            "long_check": {k: (M.get("long_check_1h_2y") or {}).get(k) for k in ("auc", "hit_when_leaning_%", "test_bars")},
            "note": "Score = the model's chance that price is LOWER at the end of the horizon. 50 % = no view. Tested on the last third of "
                    "30 days of data it never saw; checked live from 7 Nov. An indication only - never a certainty, not advice."}
