"""
US and crypto desks (2026-10-18): playbooks, volatility / derivatives panels, tested warnings, agent notes.
Read by the US and Crypto tabs of Market Pulse and by the US / Crypto desk agents. Indications only - never orders.

  build(markets, events, catalog, options, macro, root) -> {"us": {...}, "crypto": {...}}
Uses the platform's existing per-market engine (markets["ES"], ["NQ"], ["US_STOCKS"], ["BTC"], ["ETH"], ["SOL"]) for regime,
size, ranges and levels, and adds what is specific to each market. Labels: tested / watch / context / fact.
"""
from __future__ import annotations

import json
import os

import numpy as np
import pandas as pd

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
TESTED, WATCH, CONTEXT, FACT = "tested", "watch", "context", "fact"
US_MARKETS, CRYPTO_MARKETS = ("ES", "NQ", "US_STOCKS"), ("BTC", "ETH", "SOL")
COINDCX_FEES = {"maker_pct": 0.025, "taker_pct": 0.075, "source": "CoinDCX standard tier (blog, updated Nov 2025) - confirm GST on its fees page"}


def _j(p):
    try:
        return json.load(open(p))
    except Exception:
        return None


def _csv(name):
    p = os.path.join(ROOT, "data_store", "market_data", name)
    return pd.read_csv(p) if os.path.exists(p) else None


def _ydeep(sym):
    Y = _csv("yahoo_daily_deep.csv")
    if Y is None:
        return None
    d = Y[Y["symbol"] == sym].copy()
    d["date"] = pd.to_datetime(d["date"])
    return d.drop_duplicates("date", keep="last").set_index("date").sort_index()["close"]


def _validation():
    return _j(os.path.join(ROOT, "src", "common", "regime_engine", "score_v2_validation.json")) or {}


def _warn_tested(name_part, market):
    for w in _validation().get("warnings") or []:
        if name_part in str(w.get("warning")) and w.get("market") == market:
            return bool(w.get("PASS"))
    return False


def playbook(m, x, events, catalog, options, macro):
    """Re-use the India playbook builder (same engine fields); fall back to a minimal card if it cannot."""
    try:
        import india_desk as IDK
        p = IDK.playbook(m, x, events, catalog, options, macro)
        p["name"] = x.get("name")
        return p
    except Exception as e:
        er, nd, lv = x.get("exposure_regime") or {}, x.get("next_days") or {}, x.get("levels") or {}
        return {"market": m, "name": x.get("name"), "regime": er.get("regime"), "days_in_regime": er.get("days_in_regime"),
                "size_today": er.get("size_blend"), "range_3d": (nd.get("range") or {}).get("3"), "range_5d": (nd.get("range") or {}).get("5"),
                "big_move": nd.get("big_move"), "do": (x.get("decision") or {}).get("do") or [],
                "supports": [s.get("price") for s in (lv.get("supports") or [])[:2]],
                "resistances": [s.get("price") for s in (lv.get("resistances") or [])[:2]],
                "warnings": [], "deploy": [], "avoid": [], "close": er.get("close"), "bar": er.get("bar"), "fallback": str(e)[:80]}


def us_panel():
    vix, v3 = _ydeep("^VIX"), _ydeep("^VIX3M")
    if vix is None or vix.empty:
        return {"error": "VIX history missing"}
    v, r = float(vix.iloc[-1]), None
    if v3 is not None and len(v3):
        r = round(v / float(v3.reindex(vix.index).ffill().iloc[-1]), 3)
    ch5 = round((v / float(vix.iloc[-6]) - 1) * 100, 1) if len(vix) > 6 else None
    pct = round(float((vix.iloc[-252:] <= v).mean() * 100)) if len(vix) > 252 else None
    w1 = ch5 is not None and ch5 >= 15
    warn = []
    if w1:
        warn.append({"text": f"VIX up {ch5}% in 5 days", "label": TESTED if _warn_tested("W1 VIX", "S&P 500") else WATCH,
                     "means": "a high-volatility stretch is more likely in the next 10 sessions (tested on the S&P 500 since 1990)"})
    if r is not None and r >= 1.0:
        warn.append({"text": f"VIX above VIX3M (ratio {r})", "label": CONTEXT, "means": "near-term fear above 3-month fear: stressed market"})
    return {"as_of": str(vix.index[-1].date()), "vix": round(v, 2), "vix3m": None if v3 is None or v3.empty else round(float(v3.iloc[-1]), 2),
            "ratio": r, "term": None if r is None else ("stressed (backwardation)" if r >= 1 else "calm (contango)"),
            "vix_5d_change_pct": ch5, "vix_1y_percentile": pct, "warnings": warn}


def crypto_panel():
    FD, DV, OI = _csv("crypto_funding.csv"), _csv("crypto_dvol.csv"), _csv("crypto_oi.csv")
    now = pd.Timestamp.now(tz="UTC")
    h = now.hour + now.minute / 60
    session = "US" if h >= 13.5 else "Europe" if h >= 7 else "Asia"
    weekend = now.weekday() >= 5 or (now.weekday() == 4 and h >= 16)
    coins = {}
    for c in ("BTC", "ETH", "SOL"):
        o = {}
        try:
            if FD is not None:
                f = FD[FD["coin"] == c].copy()
                f["t"] = pd.to_datetime(f["funding_time"])
                f = f.drop_duplicates("t", keep="last").set_index("t").sort_index()["rate"].astype(float)
                ann = lambda r: round(float(r) * 3 * 365 * 100, 1)
                d7 = f.iloc[-21:].mean()
                yr = f.resample("1D").mean().rolling(7, min_periods=3).mean().iloc[-365:]
                o["funding"] = {"last_pct_per_yr": ann(f.iloc[-1]), "avg_7d_pct_per_yr": ann(d7),
                                "pct_rank_1y": round(float((yr <= yr.iloc[-1]).mean() * 100)) if len(yr) > 30 else None,
                                "label": CONTEXT, "note": "Funding extremes did not predict the next 10 days in our test (crowding idea failed); shown as cost and context."}
            if DV is not None and c in ("BTC", "ETH"):
                v = DV[DV["currency"] == c].copy()
                v["date"] = pd.to_datetime(v["date"])
                v = v.drop_duplicates("date", keep="last").set_index("date").sort_index()["close"]
                ch5 = round((float(v.iloc[-1]) / float(v.iloc[-6]) - 1) * 100, 1)
                o["dvol"] = {"value": round(float(v.iloc[-1]), 1), "change_5d_pct": ch5, "as_of": str(v.index[-1].date()),
                             "pct_rank_1y": round(float((v.iloc[-365:] <= v.iloc[-1]).mean() * 100))}
                if ch5 >= 15:
                    o.setdefault("warnings", []).append({"text": f"DVOL up {ch5}% in 5 days", "label": TESTED if _warn_tested("W1 DVOL", c) else WATCH,
                                                        "means": "a high-volatility stretch is more likely in the next 10 days (tested on BTC and ETH since 2021)"})
                if ch5 <= -15 and _warn_tested("W2 DVOL", c):
                    o.setdefault("warnings", []).append({"text": f"DVOL down {ch5}% in 5 days", "label": TESTED, "means": "volatility likely to stay calmer (relief)"})
            if OI is not None:
                q = OI[OI["coin"] == c].copy()
                if len(q):
                    q["ts"] = pd.to_datetime(q["ts"])
                    q = q.sort_values("ts")
                    last = q.iloc[-1]
                    prev = q[q["ts"] <= last["ts"] - pd.Timedelta(days=7)]
                    o["open_interest"] = {"usd_bn": round(float(last["oi_usd"]) / 1e9, 2),
                                          "change_7d_pct": round((float(last["oi_usd"]) / float(prev.iloc[-1]["oi_usd"]) - 1) * 100, 1) if len(prev) else None,
                                          "long_short_ratio": None if pd.isna(last.get("long_short_ratio")) else round(float(last["long_short_ratio"]), 2),
                                          "label": CONTEXT, "note": "Only ~30 days of history so far - recording from now on for a later test."}
        except Exception as e:
            o["error"] = str(e)[:100]
        coins[c] = o
    return {"session_now": session, "weekend": weekend, "weekend_rule": "Smaller size from Friday 21:30 to Monday 05:30 IST (thin liquidity).",
            "fees": COINDCX_FEES, "coins": coins}


def build(markets: dict, events: dict | None, catalog: list, options: list, macro: list, sv2m: dict | None = None) -> dict:
    agent = lambda n: _j(os.path.join(ROOT, "data_store", "agent", "out", n))
    us = {"generated": str(pd.Timestamp.now(tz="Asia/Kolkata").floor("s")),
          "clock": "US cash session 19:00-01:30 IST (20:00-02:30 IST after the US clock change on 1 Nov); futures trade nearly 24 h.",
          "playbook": {m: playbook(m, markets[m], events, catalog, options, macro) for m in US_MARKETS if m in markets},
          "panel": us_panel(), "agent": agent("us.json"),
          "rules": ["No new position ideas after 23:30 IST unless written before the open.",
                    "Ideas pause 15 minutes either side of FOMC, CPI and jobs releases.",
                    "SPY / QQQ / US stocks: no leverage; swing and positional focus."]}
    cr = {"generated": us["generated"], "clock": "24/7; daily bar closes 05:30 IST (00:00 UTC). Sessions: Asia 05:30, Europe 12:30, US 19:00 IST.",
          "playbook": {m: playbook(m, markets[m], events, catalog, options, macro) for m in CRYPTO_MARKETS if m in markets},
          "panel": crypto_panel(), "agent": agent("crypto.json"),
          "rules": ["Leverage ceiling 25x; size from the stop; liquidation price at least 2x the stop distance away.",
                    "Every card states funding cost if held more than a day.",
                    "Shock mode on a stablecoin de-peg, an exchange outage or a liquidation spike."]}
    for k, blk, keys in (("us", us, US_MARKETS), ("crypto", cr, CRYPTO_MARKETS)):
        for m in keys:
            pb = blk["playbook"].get(m)
            if not pb:
                continue
            for w in ((blk["panel"].get("warnings") or []) if k == "us" else
                      ((blk["panel"].get("coins") or {}).get(m, {}).get("warnings") or [])):
                pb.setdefault("warnings", []).append(w)
            mk = "US" if k == "us" else "CRYPTO"
            from src.common.regime_engine.score_v2_markets_live import DASH_KEY
            sv = ((sv2m or {}).get(mk) or {}).get(DASH_KEY.get(m, m))
            if sv:
                pb["score_v2"] = {"alignment": sv.get("alignment"),
                                  "labels": {r["tf"]: {"label": r["label"], "direction": r["direction"], "status": r["status"]} for r in sv["timeframes"]}}
    return {"us": us, "crypto": cr}
