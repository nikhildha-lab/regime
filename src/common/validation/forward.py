"""
Forward (live) validation of everything the desk concludes (2026-10-20).

Backtests say what SHOULD happen. This module checks, on the real market from now on, whether it DOES:
  record(out, root)    called by every Market Pulse export: appends what the engine said, point in time, to the ledger
                       (one row per market x timeframe x closed bar - never rewritten later, so nothing can peek ahead)
  evaluate(root)       (at most hourly) looks up what happened next and scores every claim per market and timeframe
                       -> data_store/validation/report.json (read by the Desk tab and the Learner)

Claims checked (same horizons as the backtests):
  score v2  - direction vs next move (rank correlation; expected sign from the backtest, e.g. REVERSE on the S&P daily)
            - each label: Volatile -> bigger moves, Range -> smaller, Trending up / down -> drift that way
  warnings  - fear gauge +15% in 5 days -> volatility higher over the next 10 days (VIX, India VIX, DVOL)
  ranges    - 5-day expected range: share of 5-session closes ending inside it (claimed about 2 in 3)
  big move  - stated chance of a big move vs how often one happened (calibration)
Verdicts: "too early" (not enough matured cases) / "holding" / "weak" (right direction, not convincing yet) /
          "not holding" (wrong direction with enough cases). Forward results never edit the backtest labels by themselves:
          the Learner proposes changes and Nikhil approves (memory/proposals.md).
"""
from __future__ import annotations

import csv
import json
import os
import time

import numpy as np
import pandas as pd

H = {"5m": 12, "15m": 8, "75m": 5, "1h": 6, "4h": 6, "1D": 10, "1W": 4}
MIN_N = {"5m": 120, "15m": 80, "75m": 40, "1h": 60, "4h": 40, "1D": 20, "1W": 8}
EXPECT_RATIO = {"Volatile": ">1", "Range": "<1"}
LEDGER_COLS = ["recorded_at", "desk", "market", "tf", "bar", "label", "raw_label", "direction", "trend", "vol", "status"]
EVENT_COLS = ["recorded_at", "desk", "market", "kind", "date", "value", "lo", "hi", "chance"]


def _vdir(root):
    d = os.path.join(root, "data_store", "validation")
    os.makedirs(d, exist_ok=True)
    return d


def _append(path, cols, rows):
    new = not os.path.exists(path)
    with open(path, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        if new:
            w.writeheader()
        w.writerows(rows)


def _seen(path, keycols):
    if not os.path.exists(path):
        return set()
    try:
        d = pd.read_csv(path, usecols=keycols, dtype=str)
        return set(map(tuple, d[keycols].values.tolist()))
    except Exception:
        return set()


def record(out: dict, root: str) -> int:
    """Append every timeframe's latest CLOSED-bar read (India, US, crypto) plus daily ranges / big-move chances."""
    vd = _vdir(root)
    lp, ep = os.path.join(vd, "ledger.csv"), os.path.join(vd, "events.csv")
    seen = _seen(lp, ["desk", "market", "tf", "bar"])
    now = pd.Timestamp.now(tz="Asia/Kolkata").isoformat(timespec="seconds")
    rows = []
    blocks = [("INDIA", (out.get("score_v2") or {}).get("markets") or {})]
    sm = out.get("score_v2_markets") or {}
    blocks += [("US", sm.get("US") or {}), ("CRYPTO", sm.get("CRYPTO") or {})]
    for desk, mk in blocks:
        for m, v in mk.items():
            for r in (v or {}).get("timeframes") or []:
                if not r or r.get("short_history"):
                    continue                                  # short-history rows are not proper reads yet
                k = (desk, m, r["tf"], str(r["bar"]))
                if k in seen:
                    continue
                seen.add(k)
                rows.append({"recorded_at": now, "desk": desk, "market": m, "tf": r["tf"], "bar": r["bar"], "label": r["label"],
                             "raw_label": r.get("raw_label"), "direction": r["direction"], "trend": r.get("trend"), "vol": r.get("vol"),
                             "status": r.get("status")})
    if rows:
        _append(lp, LEDGER_COLS, rows)
    # daily 5-day ranges and big-move chances (one per market per bar date)
    eseen = _seen(ep, ["desk", "market", "kind", "date"])
    ev = []
    pbs = [("INDIA", ((out.get("india") or {}).get("playbook") or {})), ("US", ((out.get("us_desk") or {}).get("playbook") or {})),
           ("CRYPTO", ((out.get("crypto_desk") or {}).get("playbook") or {}))]
    for desk, pb in pbs:
        for m, p in pb.items():
            d = str(p.get("bar") or "")[:10]
            if not d:
                continue
            r5, bm = p.get("range_5d") or {}, p.get("big_move") or {}
            if r5.get("lo") and (desk, m, "range5", d) not in eseen:
                ev.append({"recorded_at": now, "desk": desk, "market": m, "kind": "range5", "date": d, "lo": r5["lo"], "hi": r5["hi"]})
            if bm.get("chance") is not None and (desk, m, "bigmove", d) not in eseen:
                ev.append({"recorded_at": now, "desk": desk, "market": m, "kind": "bigmove", "date": d, "chance": bm["chance"]})
    for desk, pb in pbs:                                       # tested warnings that are active today
        for m, p in pb.items():
            d = str(p.get("bar") or "")[:10]
            for w in p.get("warnings") or []:
                if w.get("label") != "tested" or "volatil" not in str(w.get("means", "")).lower():
                    continue
                if d and (desk, m, "volwarn", d) not in eseen:
                    eseen.add((desk, m, "volwarn", d))
                    ev.append({"recorded_at": now, "desk": desk, "market": m, "kind": "volwarn", "date": d, "value": w.get("text")})
    if ev:
        _append(ep, EVENT_COLS, ev)
    return len(rows) + len(ev)


# ------------------------------------------------------------------ prices for evaluation
def _series(root, desk, market, tf):
    """Close series on the SAME bar stamps the live engine reports (string-keyed)."""
    if desk == "INDIA":
        from src.common.regime_engine import score_v2_live as L
        if tf in ("5m", "15m", "75m"):
            m1 = L._minute(root, market)
            b = L._bars(m1, tf)
            s = b["close"]
            s.index = [str(t)[:16] for t in s.index]
            return s
        d = L._ydeep(root, L.IDX[market][1])["close"]
    else:
        from src.common.regime_engine import score_v2_markets_live as M
        ins = M.INSTR[desk][market]
        if tf in ("5m", "15m"):
            b = M._rth(M._intra(root, ins["intra"], tf)) if desk == "US" else M._intra(root, ins["intra"], tf)
            s = b["close"]
            s.index = [str(t)[:16] for t in s.index]
            return s
        if tf == "1h":
            s = M._intra(root, ins["intra"], "1h")["close"]
            s.index = [str(t)[:16] for t in s.index]
            return s
        if tf == "4h":
            h1 = M._intra(root, ins["intra"], "1h", "synaptic_crypto_gold.csv")
            s = h1.resample("4h").agg({"close": "last"}).dropna()["close"]
            s.index = [str(t)[:16] for t in s.index]
            return s
        if desk == "US":
            d = M._ydeep(root, ins["daily"])["close"]
        elif market == "BTC":
            d = M._ydeep(root, "BTC-USD")["close"]
        else:
            bd = M._csv(root, "binance_daily.csv")
            x = bd[bd["symbol"] == market + "USDT"].copy()
            x["date"] = pd.to_datetime(x["date"])
            d = x.drop_duplicates("date", keep="last").set_index("date").sort_index()["close"]
    if tf == "1W":
        d = d.resample("W-FRI").last().dropna()
    s = d.copy()
    s.index = [str(t)[:10] for t in s.index]
    return s


def _fwd(series, bar, h):
    try:
        i = series.index.get_loc(bar)
    except KeyError:
        return None
    if isinstance(i, slice) or not isinstance(i, (int, np.integer)) or i + h >= len(series):
        return None
    return float(np.log(series.iloc[i + h] / series.iloc[i]))


def _verdict(n, need, good, strong):
    if n < need:
        return "too early"
    return "holding" if strong else "weak" if good else "not holding"


def evaluate(root: str, force: bool = False) -> dict | None:
    vd = _vdir(root)
    rp = os.path.join(vd, "report.json")
    if not force and os.path.exists(rp) and time.time() - os.path.getmtime(rp) < 3600:
        return None
    lp = os.path.join(vd, "ledger.csv")
    if not os.path.exists(lp):
        return None
    val = {}
    try:
        val = json.load(open(os.path.join(root, "src", "common", "regime_engine", "score_v2_validation.json")))
    except Exception:
        pass
    L = pd.read_csv(lp, dtype={"bar": str})
    claims, cache = [], {}
    for (desk, market, tf), g in L.groupby(["desk", "market", "tf"]):
        key = (desk, market, tf)
        try:
            if key not in cache:
                cache[key] = _series(root, desk, market, tf)
            s = cache[key]
        except Exception as e:
            claims.append({"desk": desk, "market": market, "tf": tf, "claim": "data", "verdict": "no data", "note": str(e)[:80]})
            continue
        g = g.drop_duplicates("bar").copy()
        g["fwd"] = [_fwd(s, b, H[tf]) for b in g["bar"]]
        m = g.dropna(subset=["fwd"])
        n, need = len(m), MIN_N[tf]
        res = (val.get("results") or {}).get({"INDIA": market}.get(desk, market), {}).get(tf, {})
        # direction
        d = res.get("direction") or {}
        exp_sign = -1 if d.get("reverse") else 1
        ic = float(m["direction"].rank().corr(m["fwd"].rank())) if n >= 10 else None
        claims.append({"desk": desk, "market": market, "tf": tf, "claim": "direction" + (" (reverse)" if exp_sign < 0 else ""),
                       "backtest": f"IC {d.get('ic'):+.3f} {'tested' if d.get('pass') else 'not proven'}" if d.get("ic") is not None else "not tested",
                       "live": None if ic is None else round(ic, 3), "n": n,
                       "verdict": _verdict(n, need, ic is not None and ic * exp_sign > 0, ic is not None and ic * exp_sign > 0.02 and n >= need)})
        # labels
        base = m["fwd"].abs().mean() if n else None
        for lab in ("Volatile", "Range", "Trending up", "Trending down"):
            sub = m[m["label"] == lab]
            k = len(sub)
            lb = res.get(lab) or {}
            if lab in EXPECT_RATIO:
                ratio = float(sub["fwd"].abs().mean() / base) if k and base else None
                good = ratio is not None and ((ratio > 1) if EXPECT_RATIO[lab] == ">1" else (ratio < 1))
                strong = ratio is not None and ((ratio > 1.1) if EXPECT_RATIO[lab] == ">1" else (ratio < 0.9))
                claims.append({"desk": desk, "market": market, "tf": tf, "claim": f"{lab} -> {'bigger' if lab == 'Volatile' else 'smaller'} moves",
                               "backtest": f"x{lb.get('abs_ratio')} {'tested' if lb.get('pass') else 'not proven'}" if lb else "not tested",
                               "live": None if ratio is None else round(ratio, 2), "n": k,
                               "verdict": _verdict(k, max(10, need // 6), good, strong)})
            else:
                ex = float((sub["fwd"].mean() - m["fwd"].mean()) * 1e4) if k else None
                sgn = 1 if lab == "Trending up" else -1
                claims.append({"desk": desk, "market": market, "tf": tf, "claim": f"{lab} -> drift that way (bps vs all)",
                               "backtest": f"{lb.get('excess_bps'):+.1f} bps {'tested' if lb.get('pass') else 'not proven'}" if lb.get("excess_bps") is not None else "not tested",
                               "live": None if ex is None else round(ex, 1), "n": k,
                               "verdict": _verdict(k, max(10, need // 6), ex is not None and ex * sgn > 0, ex is not None and ex * sgn > 2)})
    # ranges and big-move chances (daily, 5 sessions ahead)
    ep = os.path.join(vd, "events.csv")
    if os.path.exists(ep):
        E = pd.read_csv(ep, dtype={"date": str})
        for (desk, market), g in E.groupby(["desk", "market"]):
            try:
                key = (desk, market, "1D")
                if key not in cache:
                    if desk == "INDIA":
                        cache[key] = _series(root, "INDIA", market, "1D")
                    else:
                        from src.common.regime_engine.score_v2_markets_live import DASH_KEY
                        cache[key] = _series(root, desk, DASH_KEY.get(market, market), "1D")
                s = cache[key]
            except Exception:
                continue
            r = g[g["kind"] == "range5"].drop_duplicates("date")
            inside = []
            for _, x in r.iterrows():
                f = _fwd(s, x["date"], 5)
                if f is None:
                    continue
                c5 = float(s.loc[x["date"]] * np.exp(f))
                inside.append(float(x["lo"]) <= c5 <= float(x["hi"]))
            if len(r):
                p = float(np.mean(inside)) if inside else None
                claims.append({"desk": desk, "market": market, "tf": "1D", "claim": "5-day range: close ends inside (about 2 in 3)",
                               "backtest": "about 67% (tested)", "live": None if p is None else round(100 * p), "n": len(inside),
                               "verdict": _verdict(len(inside), 20, p is not None and 0.55 <= p <= 0.8, p is not None and 0.6 <= p <= 0.75)})
            vw = g[g["kind"] == "volwarn"].drop_duplicates("date")
            if len(vw):
                r1 = pd.Series(np.log(s.values[1:] / s.values[:-1]), index=s.index[1:]).abs()
                ratios = []
                for _, x in vw.iterrows():
                    if x["date"] not in r1.index:
                        continue
                    i = r1.index.get_loc(x["date"])
                    if i + 10 < len(r1) and i >= 60:
                        ratios.append(float(r1.iloc[i + 1:i + 11].mean() / r1.iloc[i - 60:i].mean()))
                rr = float(np.mean(ratios)) if ratios else None
                claims.append({"desk": desk, "market": market, "tf": "1D", "claim": "fear-gauge +15% warning -> volatility higher next 10 days",
                               "backtest": "tested", "live": None if rr is None else f"x{rr:.2f} vs prior 60 days", "n": len(ratios),
                               "verdict": _verdict(len(ratios), 5, rr is not None and rr > 1, rr is not None and rr > 1.15)})
            b = g[g["kind"] == "bigmove"].drop_duplicates("date")
            hits, stated = [], []
            for _, x in b.iterrows():
                f = _fwd(s, x["date"], 5)
                if f is None:
                    continue
                stated.append(float(x["chance"]) / 100)
                hits.append(abs(f))
            if len(b) and hits:
                thr = float(pd.Series(s.values).pct_change(5).abs().quantile(0.8))
                real = float(np.mean([h > thr for h in hits]))
                st = float(np.mean(stated))
                claims.append({"desk": desk, "market": market, "tf": "1D", "claim": "big-move chance: stated vs happened",
                               "backtest": "calibrated (tested)", "live": f"{round(100 * st)}% stated / {round(100 * real)}% happened",
                               "n": len(hits), "verdict": _verdict(len(hits), 20, abs(st - real) < 0.15, abs(st - real) < 0.08)})
    for c in claims:
        c["tested"] = "tested" in str(c.get("backtest", "")) and "not" not in str(c.get("backtest", ""))
    claims.sort(key=lambda c: (not c["tested"], {"not holding": 0, "weak": 1, "holding": 2, "too early": 3}.get(c.get("verdict"), 4)))
    summary = {v: sum(1 for c in claims if c["tested"] and c.get("verdict") == v) for v in ("holding", "weak", "not holding", "too early", "no data")}
    summary["untested"] = {v: sum(1 for c in claims if not c["tested"] and c.get("verdict") == v) for v in ("holding", "weak", "not holding", "too early")}
    rep = {"generated": pd.Timestamp.now(tz="Asia/Kolkata").isoformat(timespec="seconds"), "ledger_rows": int(len(L)),
           "since": str(L["recorded_at"].min())[:10] if len(L) else None, "summary": summary, "claims": claims,
           "rule": ("Live check from the day recording started. 'too early' until enough cases have matured "
                    "(e.g. 20 daily bars need 20 sessions + the 10-day horizon). Forward results only feed proposals; "
                    "labels change only after review.")}
    json.dump(rep, open(rp, "w"), indent=1, default=str)
    return rep
