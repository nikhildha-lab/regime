"""
Export everything the Market Pulse dashboard shows into one JSON file.

    python scripts/export_dashboard_data.py --as-of "2026-10-02 09:00"
    -> data_store/dashboard/market_pulse_{date}.json

All numbers come from the deterministic engine (no LLM). The "why" sentences are templated from
the same numbers, so every word on the page traces back to a value in this file.
"""
from __future__ import annotations

import argparse
import glob
import json
import math
import re
import os
import sys
import warnings

import numpy as np
import pandas as pd

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)
warnings.filterwarnings("ignore")

from src.common.regime_engine import data as D  # noqa: E402
from src.common.regime_engine.engine import RegimeEngine, FEATURE_GROUPS, SIGN_CONVENTION  # noqa: E402
from src.common.regime_engine.features import market_features as MF  # noqa: E402
from src.common.master_brain import markets as M  # noqa: E402
from src.common.master_brain.scorecard import build_scorecard  # noqa: E402
from src.common.master_brain.recommender import recommend  # noqa: E402
from src.common.master_brain import universe as U  # noqa: E402
from src.common.master_brain import vol_positioning as VP  # noqa: E402
from src.common.master_brain import macro_labels as ML  # noqa: E402
from src.common.master_brain import correlations as XC  # noqa: E402
from src.common.master_brain import news as NW  # noqa: E402
from src.common.regime_engine.features import character as CH  # noqa: E402


from src.common.regime_engine import exposure_regime as XR  # noqa: E402
from src.common.regime_engine import next_days as ND  # noqa: E402
from src.common.regime_engine import levels as LV  # noqa: E402

XR_IV = {"NIFTY": "^INDIAVIX", "BANKNIFTY": "^INDIAVIX", "SENSEX": "^INDIAVIX", "ES": "^VIX", "NQ": "^VIX", "US_STOCKS": "^VIX"}
XR_DEEP = {"NIFTY": "^NSEI", "BANKNIFTY": "^NSEBANK", "SENSEX": "^BSESN", "ES": "ES=F", "NQ": "NQ=F", "US_STOCKS": "^GSPC", "GOLD": "GC=F",
           "SILVER": "SI=F", "COPPER": "HG=F", "CRUDE": "CL=F", "BTC": "BTC-USD", "ALUMINUM": "ALI=F", "NATGAS": "NG=F"}
HM_DEEP = dict(XR_DEEP)                                             # HMM / GMM long daily history (Yahoo)
HM_BINANCE = {"ETH": "ETHUSDT", "SOL": "SOLUSDT"}                   # ... and Binance spot daily for these (app/flow.py)
XR_VERDICT = {}
_vs = "data_store/backtests/exposure_regime_vol_target_variants.csv"
if os.path.exists(_vs):
    _v = pd.read_csv(_vs)
    for _m, _g in _v.groupby("market"):
        _g = _g.set_index("variant")
        XR_VERDICT[_m] = {"engine_sharpe": float(_g.loc["engine", "Sharpe"]), "voltarget_sharpe": float(_g.loc["vol target", "Sharpe"]),
                          "engine_dd": float(_g.loc["engine", "max drawdown %"]), "voltarget_dd": float(_g.loc["vol target", "max drawdown %"])}
XR_VKEY = {"NIFTY": "NIFTY", "BANKNIFTY": "BANKNIFTY", "ES": "S&P 500", "US_STOCKS": "S&P 500", "GOLD": "GOLD", "CRUDE": "CRUDE", "BTC": "BTC"}
HM_KEY = {**XR_VKEY, "NQ": "NQ", "SILVER": "SILVER", "COPPER": "COPPER", "ETH": "ETH", "SOL": "SOL"}   # HMM / GMM test results


_BARS = {}
ND_KEY = {"NIFTY": "NIFTY", "BANKNIFTY": "BANKNIFTY", "US_STOCKS": "S&P 500", "ES": "ES", "NQ": "NQ", "GOLD": "GOLD",
          "SILVER": "SILVER", "COPPER": "COPPER", "CRUDE": "CRUDE", "BTC": "BTC"}
try:
    _NDP = json.load(open(os.path.join(ROOT, "data_store/backtests/next_days_passes.json")))
except Exception:
    _NDP = {"per_market": {}, "pooled": {}}
try:
    _NDA = pd.read_csv(os.path.join(ROOT, "data_store/backtests/next_days_range_bigmove.csv"))
except Exception:
    _NDA = None


try:
    _LVV = json.load(open(os.path.join(ROOT, "data_store/backtests/levels_verdicts.json")))
except Exception:
    _LVV = {"per_market": {}, "pooled": {}}
BINANCE_SYM = {"BTC": "BTCUSDT", "ETH": "ETHUSDT", "SOL": "SOLUSDT"}


def _intraday_1m(m, as_of):
    """Recent 1-minute bars: Dhan futures (Nifty/Bank Nifty, near-month by volume) or Binance klines (crypto)."""
    try:
        if m in ("NIFTY", "BANKNIFTY"):
            p = os.path.join(ROOT, "data_store/market_data/nifty_futures_1m_upstox.csv")
            if not os.path.exists(p):
                return None
            d = pd.read_csv(p)
            d = d[d["contract"].str.startswith(m + "_")]
            d["ts"] = pd.to_datetime(d["ts"])
            d = d[d["ts"] <= as_of]
            d["day"] = d["ts"].dt.date
            v = d.groupby(["day", "contract"])["volume"].sum().reset_index()
            best = v.loc[v.groupby("day")["volume"].idxmax()]
            d = d.merge(best[["day", "contract"]], on=["day", "contract"])
            return d.set_index("ts").sort_index()
        if m in BINANCE_SYM:
            fs = sorted(glob.glob(os.path.join(ROOT, "data_store/flow/binance_1m", BINANCE_SYM[m], "*.csv.gz")))[-2:]
            if not fs:
                return None
            d = pd.concat([pd.read_csv(f) for f in fs])
            d["ts"] = pd.to_datetime(d["ts"]).dt.tz_localize("UTC")
            d = d[d["ts"] <= as_of].set_index("ts").sort_index()
            d["day"] = d.index.date
            return d
    except Exception:
        return None
    return None


def levels_now(m, as_of):
    """Nearest supports / resistances (daily), 60-day profile, and intraday volume profiles where 1-min data exists."""
    if m not in _BARS:
        return None
    b, _, _ = _BARS[m]
    key = ND_KEY.get(m)
    ver = _LVV["per_market"].get(key) if key else None
    try:
        o = LV.nearest(b, ver or {t: f"untested here (all markets: {v})" for t, v in _LVV.get("pooled", {}).items()})
    except Exception as e:
        return {"error": str(e)}
    o["tested_market"] = ver is not None
    m1 = _intraday_1m(m, as_of)
    if m1 is not None and len(m1):
        days = sorted(set(m1["day"]))
        o["intraday"] = {"last_session": {"date": str(days[-1]), **(LV.intraday_profile(m1[m1["day"] == days[-1]]) or {})},
                         "last_5_sessions": {"from": str(days[max(0, len(days) - 5)]),
                                             **(LV.intraday_profile(m1[m1["day"].isin(days[-5:])]) or {})}}
        if "oi" in m1:
            last = m1[m1["day"] == days[-1]]
            o["intraday"]["oi_change_last_session"] = float(last["oi"].iloc[-1] - last["oi"].iloc[0])
            o["intraday"]["price_change_last_session_pct"] = round(float(last["close"].iloc[-1] / last["open"].iloc[0] - 1) * 100, 2)
    return o


try:
    _BNC = json.load(open(os.path.join(ROOT, "data_store/models/bounce_model.json")))
except Exception:
    _BNC = {}


def _bounce_table(m):
    """Same table as scripts/test_bounce.py, built from this market's own daily bars (used for markets outside the test)."""
    import test_bounce as TB
    b, iv, ann = _BARS[m]
    d = TB.features(b, iv, ann)
    d["y"] = TB.outcomes(b, d["atr"])
    d = d.dropna(subset=TB.FEATS + ["y"])
    st = np.where((d["z20"] < -2) | (d["rsi"] < 0.30), "down", np.where((d["z20"] > 2) | (d["rsi"] > 0.70), "up", "none"))
    rg = np.where(d["bull"] == 1, "Bull_Trend", np.where(d["bear"] == 1, "Bear_Trend", "other"))
    tab = {"all": {"n": int(len(d)), "bounce": round(float(d["y"].mean() * 100), 1)}}
    for (r_, s_), x in d.groupby([rg, st]):
        tab[f"{r_}|{s_}"] = {"n": int(len(x)), "bounce": round(float(x["y"].mean() * 100), 1)}
    return tab


try:
    _REV2 = json.load(open(os.path.join(ROOT, "data_store/backtests/reversal_v2.json")))
except Exception:
    _REV2 = {}


def at_level_now(b, mk_levels=None):
    """Is price at a level right now, and is it running INTO it (momentum) - from the event-based reversal test."""
    a = float(LV.atr(b).iloc[-1])
    sh, sl = LV.swings(b)
    lv = LV.levels_at(b.iloc[:-1], sh[sh.index < b.index[-1]], sl[sl.index < b.index[-1]], a)
    c, h, l = b["close"].values, b["high"].values, b["low"].values
    prev = c[-2]
    hits = []
    for x in lv:
        p = x["price"]
        if p < prev and l[-1] <= p + 0.25 * a:
            hits.append((1, p, x["name"]))
        elif p > prev and h[-1] >= p - 0.25 * a:
            hits.append((-1, p, x["name"]))
    t = _REV2.get("tests") or {}
    base = _REV2.get("base_rate")
    if not hits:
        return {"at_level": False, "base": base, "events": _REV2.get("events")}
    sup = [hh for hh in hits if hh[0] == 1]
    res = [hh for hh in hits if hh[0] == -1]
    # the level nearest to the close
    side, p, nm = min(sup + res, key=lambda x: abs(x[1] - c[-1]))
    run = 0
    k = len(c) - 1
    while k > 0 and np.sign(c[k] - c[k - 1]) == -side:
        run += 1
        k -= 1
    z = (c[-1] - b["close"].iloc[-20:].mean()) / b["close"].iloc[-20:].std() * -side
    mom = bool(run >= 4 or z >= 2)
    key = "trend stretch: 4+ closes in a row into the level" if run >= 4 else "stretched 2+ sd into the level"
    r = t.get(key, {})
    return {"at_level": True, "side": "support" if side == 1 else "resistance", "level": round(float(p), 2), "name": nm,
            "run_days": run, "z_into": round(float(z), 2), "momentum": mom, "base": base, "events": _REV2.get("events"),
            "reversal_if_momentum": r.get("reversal % with"), "reversal_otherwise": r.get("reversal % without")}


def bounce_now(key, regime, stretched, close, atr, m=None):
    """Background odds of a 1-ATR bounce before a further 1-ATR drop within 5 days, from matching past days."""
    tab = (_BNC.get("table") or {}).get(key)
    own = False
    if not tab and m in _BARS:
        try:
            tab, own = _bounce_table(m), True
        except Exception:
            tab = None
    if not tab:
        return None
    rg = regime if regime in ("Bull_Trend", "Bear_Trend") else "other"
    st = stretched or "none"
    cell = tab.get(f"{rg}|{st}")
    basis = "same regime and stretch"
    if not cell or cell["n"] < 25:
        cell, basis = tab.get("all"), "all days (too few matching days)"
    n, p = cell["n"], cell["bounce"] / 100
    # small samples are pulled towards the same cell pooled over all 10 tested markets (100 pseudo-days)
    key_ = f"{rg}|{st}" if basis.startswith("same") else "all"
    pool = [(v[key_]["n"], v[key_]["bounce"]) for v in (_BNC.get("table") or {}).values() if key_ in v]
    raw = round(p * 100)
    if pool:
        pn = sum(a_ for a_, _ in pool)
        pp = sum(a_ * b_ for a_, b_ in pool) / pn / 100
        p = (n * p + 100 * pp) / (n + 100)
        basis += f"; small samples pulled towards all 10 markets ({pp * 100:.0f}%)" if n < 300 else ""
    z = 1.645
    den = 1 + z * z / n
    mid = (p + z * z / (2 * n)) / den
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    t = _BNC.get("test") or {}
    return {"chance": round(p * 100), "lo": round((mid - half) * 100), "hi": round((mid + half) * 100), "n": n, "basis": basis,
            "normal": tab["all"]["bounce"], "up_to": round(close + atr, 2) if close and atr else None,
            "down_to": round(close - atr, 2) if close and atr else None,
            "skill": t.get("Brier skill all days %"), "skill_sd": t.get("Brier skill stretched down %"),
            "own_history": own, "raw": raw}


try:
    _PB = json.load(open(os.path.join(ROOT, "data_store/backtests/pullback_tests.json")))
except Exception:
    _PB = {}
try:
    _VOLV = json.load(open(os.path.join(ROOT, "data_store/backtests/volume_verdicts.json")))["per_market"]
except Exception:
    _VOLV = {}
try:
    _CAL = json.load(open(os.path.join(ROOT, "data_store/backtests/crypto_calendar.json")))
except Exception:
    _CAL = {}
SESSIONS = [("Asia", 0, 8), ("Europe", 8, 13), ("US", 13, 21), ("Late", 21, 24)]


def volume_now(m):
    """Volume readings for today + whether today is a breakout, with the tested meaning for this market."""
    if m not in _BARS:
        return None
    b = _BARS[m][0]
    v = b["volume"].where(b["volume"] > 0)
    if v.iloc[-60:].notna().mean() < 0.8:
        return {"available": False, "note": "No reliable volume for this market (index volume missing on many days)."}
    c = b["close"]
    up = (c > c.shift()).astype(float)
    rel = float(v.iloc[-1] / v.iloc[-51:-1].median())
    ud = float((v * up).iloc[-20:].sum() / (v * (1 - up)).iloc[-20:].sum())
    tr = float(v.iloc[-20:].mean() / v.iloc[-100:].mean())
    hi20, lo20 = c.iloc[-1] >= c.iloc[-20:].max(), c.iloc[-1] <= c.iloc[-20:].min()
    key = ND_KEY.get(m)
    ver = (_VOLV.get(key) or {}).get("V2")
    out = {"available": True, "rel_vol": round(rel, 2), "updown_20d": round(ud, 2), "vol_trend": round(tr, 2),
           "breakout": "up" if hi20 else "down" if lo20 else None, "high_volume": rel > 1.5, "breakout_test": ver}
    lines = [f"Today's volume is {rel:.1f}x its 50-day normal; over 20 days, up-day volume is {ud:.2f}x down-day volume"
             f" ({'buyers busier' if ud > 1.1 else 'sellers busier' if ud < 0.9 else 'balanced'}); "
             f"volume trend {'rising' if tr > 1.1 else 'falling' if tr < 0.9 else 'flat'} ({tr:.2f})."]
    if out["breakout"]:
        txt = f"Today closed at a 20-day {'high' if hi20 else 'low'} on {'HIGH' if rel > 1.5 else 'normal'} volume. "
        if rel > 1.5 and ver == "adds value":
            txt += "Tested here: high-volume breakouts tend to continue."
        elif rel > 1.5 and ver and "warning" in ver:
            txt += "Tested here: high-volume breakouts more often FAIL (exhaustion) - be careful chasing it."
        else:
            txt += "Volume did not change breakout odds in tests for this market."
        lines.append(txt)
    lines.append("Tests: volume did not improve the regime label or position size (Sharpe unchanged); it matters only for "
                 "breakouts in some markets (gold: high volume = continuation; S&P/Nasdaq futures: high volume = exhaustion).")
    out["lines"] = lines
    return out


def calendar_now(m, as_of):
    """Crypto: weekend and time-zone session context from the tests."""
    if m not in ("BTC", "ETH", "SOL") or not _CAL:
        return None
    t = pd.Timestamp(as_of).tz_convert("UTC")
    ses = next(n for n, a, z in SESSIONS if a <= t.hour < z)
    coin = m
    w1 = next((r for r in _CAL.get("W1", []) if r["coin"] == coin and r["period"] == "all"), None)
    s1 = [r for r in _CAL.get("S1", []) if r["coin"] == coin]
    w2 = _CAL.get("W2") or {}
    lines = []
    if w1:
        lines.append(f"Weekends: {coin} moves about {round((1 - w1['avg |move| weekend %'] / w1['avg |move| weekday %']) * 100)}% less "
                     f"than on weekdays, on {round((1 - w1['weekend volume vs weekday']) * 100)}% less volume.")
    if w2:
        lines.append(f"Strategy trades entered at weekends: {w2.get('verdict', '')} vs weekdays ({w2.get('trades weekend')} weekend trades, "
                     "tested against shuffled days) - so weekends are traded normally; the vol-target part of the size already "
                     "scales up a little for the calmer days.")
    us = next((r for r in s1 if r["session"] == "US"), None)
    if us:
        lines.append(f"Sessions (UTC): the US session 13-21 (18:30-02:30 IST) carries {us['share of daily movement %']}% of the daily "
                     f"movement and {us['share of volume %']}% of volume; its average return was negative in both halves of the data "
                     "(too small to trade, but expect the big moves then and size stops for it).")
    s2 = [r for r in _CAL.get("S2", []) if r["coin"] == coin]
    if s2:
        wk = next((r for r in s2 if r["slice"] == "Weekend"), {})
        lines.append(f"Order-flow absorption signal (tested useful): right {min(r['hit %'] for r in s2):.0f}-{max(r['hit %'] for r in s2):.0f}% "
                     f"of the time by session, but the average move is only about {max(r['avg move in signal direction (bp)'] for r in s2):.0f} bp "
                     "or less - below the ~10 bp round-trip taker cost, so it is a timing aid, not a stand-alone trade"
                     + (f" (best at weekends: {wk.get('hit %')}%)." if wk else "."))
    return {"weekend": t.dayofweek >= 5, "session_now": ses, "lines": lines, "sessions": s1}


def next_days_now(m, xr):
    """3-5 day outlook (range, big-move chance, tested reversal/continuation signs, history check, action)."""
    if m not in _BARS:
        return None
    b, iv, ann = _BARS[m]
    try:
        d = ND.frame(b, iv, ann)
        key = ND_KEY.get(m)
        o = ND.live(d, _NDP["per_market"].get(key, {}) if key else None)
        o["tested_market"] = key is not None
        o["action"] = ND.action(o, (xr or {}).get("iv_pct"), (xr or {}).get("size_blend"))
        try:
            o["entry"] = ND.entry_timing(d, b, key, _PB)
            o["entry"]["bounce"] = bn = bounce_now(key, o["entry"].get("regime"), o["entry"].get("stretched"), o.get("close"), o["entry"].get("atr"), m)
            try:
                o["entry"]["at_level"] = at_level_now(b, mk_levels=None)
            except Exception as e:
                o["entry"]["at_level"] = {"error": str(e)[:80]}
            if bn:
                up = o.get("direction_20d") != "down"          # reversal = a 1-ATR move AGAINST the 20-day direction first
                flip = lambda v: round(100 - v) if up else round(v)
                bn["reversal"] = {"dir": "down" if up else "up", "chance": flip(bn["chance"]),
                                  "lo": flip(bn["hi"]) if up else bn["lo"], "hi": flip(bn["lo"]) if up else bn["hi"],
                                  "normal": flip(bn["normal"]), "against_to": bn["down_to"] if up else bn["up_to"],
                                  "with_to": bn["up_to"] if up else bn["down_to"]}
            if o["entry"].get("no_new"):
                o["action"].insert(1, f"No NEW {o['entry']['no_new']} entries right now (market stretched) - see entry timing.")
        except Exception as e:
            o["entry"] = {"lines": [f"entry timing failed: {e}"]}
        if _NDA is not None and key:
            a = _NDA[_NDA.market == key]
            o["test"] = {"inside_1x": [float(v) for v in a["inside 1x range %"]], "inside_2x": [float(v) for v in a["inside 2x range %"]],
                         "big_low": [float(v) for v in a["big move % when vol forecast low (bottom fifth)"]],
                         "big_high": [float(v) for v in a["big move % when vol forecast high (top fifth)"]]}
        return o
    except Exception as e:
        return {"error": str(e)}


def exposure_now(m, as_of):
    """Five-state exposure regime on the long daily history (Nifty index from 2007), point in time."""
    deep = "data_store/market_data/yahoo_daily_deep.csv"
    d = pd.DataFrame()
    if m in XR_DEEP and os.path.exists(deep):
        d = pd.read_csv(deep)
        d = d[d["symbol"] == XR_DEEP[m]].copy()
    if len(d):                                   # 2026-10-24: markets without deep history (aluminium, natural gas) use their own bars
        d["date"] = pd.to_datetime(d["date"])
        b = d.drop_duplicates("date", keep="last").set_index("date").sort_index()[["open", "high", "low", "close", "volume"]]
        india = m in ("NIFTY", "BANKNIFTY", "SENSEX")
        avail = (b.index + (pd.Timedelta(hours=15, minutes=31) if india else pd.Timedelta(hours=27, minutes=30))).tz_localize(D.IST)
        if m == "BTC":
            avail = (b.index + pd.Timedelta(days=1, hours=5, minutes=30)).tz_localize(D.IST)
        b = b[avail <= as_of]
        src = f"{XR_DEEP[m]} daily since {b.index.min().year}"
    else:
        b = M.market_bars(m)
        b = b[pd.DatetimeIndex(b["available_at"]).tz_convert(D.IST) <= as_of]
        src = "market bars"
    if len(b) < 260:
        return None
    iv = None
    if m in XR_IV:
        y = VP.yahoo_daily(XR_IV[m])
        if y is not None:
            y = y[y["available_at"] <= as_of.tz_convert("UTC")]
            iv = y["value"]
            iv.index = pd.DatetimeIndex(iv.index).normalize()
    _BARS[m] = (b, iv, 365 if m in M.CRYPTO else 252)
    f = XR.features(b, iv, 365 if m in M.CRYPTO else 252)
    lab = XR.label(f)
    r = f.iloc[-1]
    vt = float(min(1.0, 0.10 / r["vol_20d"])) if r["vol_20d"] > 0 else None
    runs = (lab != lab.shift()).cumsum()
    v = XR_VERDICT.get(XR_VKEY.get(m, ""), None)
    ex = XR.EXPOSURE.get(lab.iloc[-1])
    pv = None
    try:                                     # 2026-10-31: paper variants of the live regime - record only, never used for size
        from src.common.regime_engine import tf_regime as TR
        pv = TR.paper_variants(b, f, lab)
        lp = os.path.join(ROOT, "data_store", "regime_variants", "log.csv")
        os.makedirs(os.path.dirname(lp), exist_ok=True)
        key = f"{m},{f.index[-1].date()},"
        seen = open(lp).read() if os.path.exists(lp) else "market,bar,live,dir_score,confirm_2d,close,logged_at\n"
        if key not in seen:
            with open(lp, "w" if not os.path.exists(lp) else "a") as fh:
                if not os.path.exists(lp) or seen.startswith("market,"):
                    pass
                fh.write(("" if os.path.getsize(lp) else "market,bar,live,dir_score,confirm_2d,close,logged_at\n")
                         + f"{key}{lab.iloc[-1]},{pv['dir_score']['regime']},{pv['confirm_2d']['regime']},{float(b['close'].loc[:f.index[-1]].iloc[-1])},{pd.Timestamp.now().isoformat(timespec='minutes')}\n")
    except Exception as e:
        pv = {"error": str(e)[:120]}
    blend = round((ex + vt) / 2, 2) if ex is not None and vt is not None else None
    cand = None
    try:                                     # candidate engine v2: PAPER ONLY, never changes size_blend
        from src.common.regime_engine import candidate_v2 as CV2
        cand = CV2.candidate(m, b, iv)
        CV2.log(m, cand, ROOT)
    except Exception as e:
        cand = {"error": str(e)[:120]}
    return {"regime": lab.iloc[-1], "exposure": ex, "vol_target_exposure": round(vt, 2) if vt else None, "size_blend": blend,
            "candidate_v2": cand, "paper_variants": pv,
            # 2026-10-31 (Nikhil): last 90 sessions of the SAME 5-state regime (the history chart used older character names)
            "hist90": [{"date": str(i.date()), "regime": (None if pd.isna(rg) else rg), "close": round(float(cl), 4)}
                       for i, rg, cl in zip(f.index[-90:], lab.iloc[-90:], b["close"].reindex(f.index).iloc[-90:]) if pd.notna(cl)],
            "days_in_regime": int((runs == runs.iloc[-1]).sum()), "bar": str(f.index[-1].date()), "source": src,
            "er_20": round(float(r["er_20"]), 2), "adx": round(float(r["adx"]), 1), "vol_20d": round(float(r["vol_20d"]) * 100, 1),
            "vol_pct": None if pd.isna(r["vol_pct"]) else round(float(r["vol_pct"])),
            "iv_pct": None if pd.isna(r["iv_pct"]) else round(float(r["iv_pct"])),
            "px_vs_ema200": round(float(r["px_vs_ema200"]) * 100, 1), "dd_60d": round(float(r["dd_60d"]) * 100, 1),
            "ret_20d": round(float(r["ret_20d"]) * 100, 1), "validation": v,
            "close": round(float(b["close"].loc[:f.index[-1]].iloc[-1]), 2)}


OPT_STEP = {"NIFTY": 50, "BANKNIFTY": 100}
SPREAD_WHERE = {"NIFTY": "NSE index options", "BANKNIFTY": "NSE index options", "BTC": "Deribit options", "ETH": "Deribit options",
                "ES": "SPX / ES options", "US_STOCKS": "SPY options", "NQ": "QQQ / NQ options", "GOLD": "GLD / MCX gold options",
                "CRUDE": "USO / MCX crude options", "SILVER": "SLV options", "COPPER": "COMEX copper options"}


def options_now(m, as_of):
    """Latest NSE options summary for NIFTY/BANKNIFTY usable at as_of (bhavcopy out ~20:00 IST)."""
    p = "data_store/market_data/nse_options_eod.csv"
    if m not in OPT_STEP or not os.path.exists(p):
        return None
    try:
        o = pd.read_csv(p)
        o = o[o["symbol"] == m]
        t = pd.Timestamp(as_of)
        t = (t.tz_convert("UTC") if t.tzinfo else t.tz_localize(D.IST).tz_convert("UTC")).tz_localize(None)
        o = o[pd.to_datetime(o["available_at"]) <= t].sort_values("date")
        return None if o.empty else o.iloc[-1].to_dict()
    except Exception:
        return None


def _weekend_idea(m, x, close, as_of, vp):
    """Weekend iron condor on Deribit (Fri 08:00 UTC -> Sun 08:00 UTC expiry). Proxy test: selling the 2-day ATM
    straddle at DVOL-implied prices made money at weekends in both halves of 2021-26, better than midweek."""
    w3 = [r for r in _CAL.get("W3", []) if r["coin"] == m]
    if not w3:
        return None
    t = pd.Timestamp(as_of).tz_convert("UTC")
    dv = VP.dvol(m)
    ivv = None
    if dv is not None and len(dv):
        ivv = float(dv[dv["available_at"] <= t]["value"].dropna().iloc[-1])
    reg = x.get("regime")
    wkd = next((r for r in w3 if r["window"].startswith("weekend")), {})
    mid = next((r for r in w3 if r["window"].startswith("weekday")), {})
    if ivv is None:
        return None
    move = close * ivv / 100 * (2 / 365) ** 0.5
    rnd = lambda v: float(f"{v:.3g}")
    legs = f"sell {rnd(close + move):g} call / buy {rnd(close + 2 * move):g} call, sell {rnd(close - move):g} put / buy {rnd(close - 2 * move):g} put"
    ok = reg != "High_Volatility"
    when = "this weekend" if t.dayofweek in (3, 4) else "next weekend (set up Friday)" if t.dayofweek < 3 else "weekend already under way"
    action = "Paper: idea" if ok and t.dayofweek in (3, 4) else "Paper: skip"
    why = (f"Weekend option selling: crypto moves ~35% less at weekends. Proxy test 2021-26 (premium from DVOL {ivv:.0f}): selling a Fri->Sun "
           f"straddle earned {wkd.get('avg P&L % of price')}% of price on average, win rate {wkd.get('win %')}%, both halves positive "
           f"({wkd.get('halves')}); the same trade midweek earned {mid.get('avg P&L % of price')}%. Worst weekend {wkd.get('worst %')}%, "
           f"so use an iron condor (capped loss). Expected 2-day move +-{move:,.0f}. "
           + ("Skip: high-volatility regime. " if not ok else "") + f"{when.capitalize()}. Caveat: real weekend options are priced "
           "lower than DVOL, so the true edge is smaller - paper trade first. Not backtested on real option prices.")
    return {"group": "Directional spreads", "strategy": f"Weekend iron condor {m}", "market": m, "leg": legs,
            "registry": "new · proxy-tested", "action": action, "lots": None, "reason": why, "source": "weekend rule v1",
            "size": x.get("size_blend")}


def spread_ideas(markets, as_of, vp=None):
    """Directional option spreads picked from the exposure regime + implied-vol percentile. Rules fixed in advance:
    Bull Trend -> bull call spread (debit) if IV pct < 50 else bull put spread (credit); Bear Trend -> bear put spread
    (debit) if IV pct < 50 else bear call spread (credit); Range / Compression / High Volatility -> no directional spread.
    Width = one expected move (ATM straddle on NSE; else 20-day vol over ~1 month). Size = position-size blend.
    Not backtested yet: paper only."""
    rows = []
    for m, mk in markets.items():
        x = mk.get("exposure_regime") or {}
        reg, close = x.get("regime"), x.get("close")
        if not reg or not close or m not in SPREAD_WHERE:
            continue
        iv = x.get("iv_pct")
        if iv is None and vp:
            ivr = next((r.get("implied_vol") or {} for r in vp if r.get("market") == m), {})
            iv = ivr.get("pct_1y") if ivr.get("available") else None
        ivlow = iv is None or iv < 50
        opt = options_now(m, as_of)
        step = OPT_STEP.get(m)
        if opt and opt.get("atm_straddle") == opt.get("atm_straddle"):
            atm, move = float(opt["atm_strike"]), float(opt["atm_straddle"])
            src = f"ATM {atm:.0f}, straddle {move:.0f} pts (expected move to {opt['near_expiry']}), PCR {opt['pcr_oi']:.2f}, max pain {opt['max_pain']:.0f}, NSE {opt['date']}"
            expiry = f"expiry {opt['near_expiry']}"
        else:
            atm = close
            move = close * (x.get("vol_20d") or 15) / 100 * (21 / 252) ** 0.5
            src = f"price {close:,.2f}, 1-month expected move ±{move:,.2f} from 20-day vol {x.get('vol_20d')}%"
            expiry = "~1 month expiry"
        rnd = (lambda v: round(v / step) * step) if step else (lambda v: float(f"{v:.3g}"))
        size = x.get("size_blend")
        ivtxt = f"implied-vol pct {iv}" if iv is not None else "no implied-vol series (treated as low)"
        struct = []
        if reg == "Bull_Trend":
            if ivlow:
                name, legs = "Bull call spread (debit)", f"buy {rnd(atm):g} call / sell {rnd(atm + move):g} call"
                struct = [("C", rnd(atm), 1), ("C", rnd(atm + move), -1)]
            else:
                name, legs = "Bull put spread (credit)", f"sell {rnd(atm - move / 2):g} put / buy {rnd(atm - 1.5 * move):g} put"
                struct = [("P", rnd(atm - move / 2), -1), ("P", rnd(atm - 1.5 * move), 1)]
            action = "Paper: idea"
        elif reg == "Bear_Trend":
            if ivlow:
                name, legs = "Bear put spread (debit)", f"buy {rnd(atm):g} put / sell {rnd(atm - move):g} put"
                struct = [("P", rnd(atm), 1), ("P", rnd(atm - move), -1)]
            else:
                name, legs = "Bear call spread (credit)", f"sell {rnd(atm + move / 2):g} call / buy {rnd(atm + 1.5 * move):g} call"
                struct = [("C", rnd(atm + move / 2), -1), ("C", rnd(atm + 1.5 * move), 1)]
            action = "Paper: idea"
        else:
            name, legs = "No directional spread", "–"
            action = "Paper: skip"
        ent = ((mk.get("next_days") or {}).get("entry") or {})
        nn = ent.get("no_new")
        if action == "Paper: idea" and ((nn == "short" and reg == "Bear_Trend") or (nn == "long" and reg == "Bull_Trend")):
            action = "Paper: wait"
            pz = ent.get("pullback_zone") or {}
            legs += f" - WAIT: market stretched; set up on a {'bounce' if nn == 'short' else 'dip'} into {pz.get('from')}-{pz.get('to')}"
        why = (f"{reg.replace('_', ' ')} for {x.get('days_in_regime')} days, {ivtxt} -> "
               + ({"Paper: idea": ("buy the spread (options cheap)" if ivlow else "sell the spread (options rich)"),
                  "Paper: wait": ("buy the spread (options cheap)" if ivlow else "sell the spread (options rich)") + ", but wait for the bounce/dip"}.get(action,
                  "no clear direction, so no directional spread") )
               + (f". Legs: {legs}, {expiry}" if action != "Paper: skip" else "") + f". Size {size}x of a normal position. Based on {src}. Rule not backtested yet.")
        if m in ("BTC", "ETH"):
            wk = _weekend_idea(m, x, close, as_of, vp)
            if wk:
                rows.append(wk)
        rows.append({"group": "Directional spreads", "strategy": f"{name} {m}", "market": m, "leg": legs if action != "Paper: skip" else "",
                     "registry": "new · not backtested", "action": action, "lots": None, "reason": why, "source": "spread rule v1",
                     "size": size, "legs_struct": [{"type": t_, "k": float(k_), "q": q_} for t_, k_, q_ in struct], "spot": float(close),
                     "vol_pct_ann": float(x.get("vol_20d") or 15), "dte": 30})
    return rows


NATURE = [  # (kind, keywords) - first match wins
    ("Option selling", ["condor", "fly", "straddle", "strangle", "ratio_flip", "calendar", "iron", "odte", "delta_hedged", "covered"]),
    ("Directional spread", ["spread"]),
    ("Carry / value", ["carry", "value", "funding", "skew", "xsect"]),
    ("Mean reversion", ["meanrev", "rsi", "bbrsi", "divergence", "pullback", "reclaim", "8020", "pivot", "saucer", "vwap",
                        "smt", "liquidity", "fvg", "cpr", "retest", "measured"]),
    ("Trend following", ["trend", "breakout", "ewmac", "accel", "momentum", "channel", "darvas", "ema", "psar", "heikin", "ma18",
                         "minervini", "vcp", "vortex", "wave", "thrust", "impulse", "escape", "martin", "macd", "tori", "bos",
                         "combined", "ensemble", "ml_signal", "log_xz", "volume_cluster", "blackbox", "gamechanger", "okala"]),
]
NATURE_TEXT = {"Trend following": "earns when prices keep moving one way; loses in back-and-forth markets",
               "Mean reversion": "buys dips / sells rips; earns in ranges, loses when a strong trend runs over it",
               "Option selling": "collects option premium; earns when the market stays inside a range, hurt by sudden big moves",
               "Directional spread": "a capped-risk bet on direction with options",
               "Carry / value": "earns a steady premium or value gap; mostly hurt by sharp stress",
               "Other": "mixed style"}
FIT = {  # nature -> exposure regime -> (fit, words)
    "Trend following": {"Bull_Trend": ("good", "clear trend: suits this style"), "Bear_Trend": ("good", "clear trend: suits this style (shorts)"),
                        "Range": ("poor", "range: expect false breakouts"), "Compression": ("wait", "squeeze: a breakout may come, but direction is unknown"),
                        "High_Volatility": ("mixed", "wild swings: stops get hit more often")},
    "Mean reversion": {"Bull_Trend": ("poor", "strong trend: fading moves is risky"), "Bear_Trend": ("poor", "strong trend: fading moves is risky"),
                       "Range": ("good", "range: suits buying dips / selling rips"), "Compression": ("good", "quiet market: small reversals work"),
                       "High_Volatility": ("mixed", "big reversals but also big losses")},
    "Option selling": {"Bull_Trend": ("ok", "steady trend: fine if strikes are wide"), "Bear_Trend": ("mixed", "falling market: put side at risk, premiums rich"),
                       "Range": ("good", "range: best setting for premium selling"), "Compression": ("ok", "calm, but premiums are thin and a breakout may come"),
                       "High_Volatility": ("poor", "high volatility: big moves can blow through strikes")},
    "Directional spread": {"Bull_Trend": ("good", "uptrend: bullish spread"), "Bear_Trend": ("good", "downtrend: bearish spread"),
                           "Range": ("none", "no clear direction"), "Compression": ("none", "direction unknown until the squeeze breaks"),
                           "High_Volatility": ("none", "too wild for a directional bet")},
    "Carry / value": {"Bull_Trend": ("good", "calm trend suits carry"), "Bear_Trend": ("mixed", "stress can hit carry"), "Range": ("ok", "neutral"),
                      "Compression": ("ok", "neutral"), "High_Volatility": ("poor", "stress is the main risk for carry")},
}
BACKDROP = {"NIFTY": ["India", "Global"], "BANKNIFTY": ["India", "Global"], "SENSEX": ["India", "Global"], "BTC": ["Risk appetite", "Positioning"],
            "ETH": ["Risk appetite", "Positioning"], "SOL": ["Risk appetite", "Positioning"], "GOLD": ["Macro", "Global"],
            "SILVER": ["Macro", "Global"], "COPPER": ["Risk appetite", "Macro"], "CRUDE": ["Global", "Volatility"],
            "ES": ["Risk appetite", "Macro"], "NQ": ["Risk appetite", "Macro"], "US_STOCKS": ["Risk appetite", "Macro"]}


def nature_of(name: str) -> str:
    n = name.lower()
    for kind, keys in NATURE:
        if any(k in n for k in keys):
            return kind
    return "Other"


def regime_record():
    """Past trades of each strategy grouped by the exposure regime on the day before entry (from test_strategy_router)."""
    p = "data_store/backtests/strategy_router_trades.csv"
    if not os.path.exists(p):
        return {}
    t = pd.read_csv(p)
    out = {}
    for (m, st), g in t.groupby(["market", "strategy"]):
        allr = {"n": len(g), "avg": float(g.pnl.mean()), "win": float((g.pnl > 0).mean() * 100)}
        by = {lab: {"n": len(x), "avg": float(x.pnl.mean()), "win": float((x.pnl > 0).mean() * 100)} for lab, x in g.groupby("label")}
        out[(m, st)] = {"all": allr, "by": by}
    return out


def _hold_days():
    """Median holding period (days) of each strategy's real backtest trades."""
    try:
        t = pd.read_csv(os.path.join(ROOT, "data_store/backtests/strategy_router_trades.csv"), parse_dates=["entry", "exit"])
        t["hold"] = (t["exit"] - t["entry"]).dt.total_seconds() / 86400
        return t.groupby("strategy")["hold"].median().to_dict()
    except Exception:
        return {}


_HOLD = None
OPT_WEEKLY = ("delta_hedged", "odte", "split_condor", "strangle_shield", "reverse_calendar", "regime_spreads", "iron_butterfly", "weirdor")
OPT_MONTHLY = ("fly_rescue", "condor_clock", "straddle_recenter", "volcano", "ratio_flip", "yearly_covered")
INTRA = ("intraday", "925", "manish", "london", "15min", "rabbit_liquidity")


def horizon_of(r):
    """Positional (> 7 days typical hold), Swing (1-7 days), Intraday (same day), Options weekly / monthly / weekend.
    Uses the median holding period of the strategy's real backtest trades when available, else its timeframe."""
    global _HOLD
    if _HOLD is None:
        _HOLD = _hold_days()
    nm, leg, grp = r["strategy"].lower(), (r.get("leg") or "").lower(), r["group"]
    base = r["strategy"].split(" ")[0]
    if "weekend" in nm:
        return "Options · weekend"
    if grp == "Directional spreads" or any(k in nm for k in OPT_MONTHLY) or "monthly" in leg or "last friday" in leg:
        return "Options · monthly"
    if grp == "Option selling" or any(k in nm for k in OPT_WEEKLY) or "weekly" in leg or re.search(r"(bull|bear)_(call|put)_spread", nm):
        return "Options · weekly" if (any(k in nm for k in OPT_WEEKLY) or "weekly" in leg) else "Options · monthly"
    if any(k in nm for k in INTRA) or "15min" in leg:
        return "Intraday"
    h = _HOLD.get(base)
    if h is not None:
        return "Intraday" if h < 1 else "Swing" if h <= 7 else "Positional"
    if re.search(r"\b(1h|4h|8h)\b", leg) or "4h" in nm or "swing" in nm or "reversal" in nm:
        return "Swing"
    return "Positional"


def why_lines(r, markets, sections, rec):
    m = r["market"]
    leg_m = (r.get("leg") or "").split(" ")[0] if r["group"] == "All markets" else m
    leg_m = leg_m if leg_m in markets else m
    mk = markets.get(leg_m) or {}
    x, ch = mk.get("exposure_regime") or {}, mk.get("character") or {}
    lines = []
    reg = x.get("regime")
    if reg:
        lines.append(f"{mk.get('name', leg_m)} now: {reg.replace('_', ' ')} for {x.get('days_in_regime')} days "
                     f"(trend strength ADX {x.get('adx')}, efficiency {x.get('er_20')}, 20-day vol {x.get('vol_20d')}% = {_ord(x.get('vol_pct') or 0)} pct"
                     + (f", implied vol {_ord(x.get('iv_pct'))} pct" if x.get("iv_pct") is not None else "")
                     + (f"; character {ch.get('character')}" if ch.get("character") else "") + f"). Position size {x.get('size_blend')}x.")
    base = r["strategy"].split(" ")[0]
    nat = "Directional spread" if r["group"] == "Directional spreads" else nature_of(base)
    lines.append(f"Strategy type: {nat.lower()} - {NATURE_TEXT.get(nat, '')}.")
    f = FIT.get(nat, {}).get(reg)
    if f:
        lines.append(f"Fit with today's regime: {f[0]} - {f[1]}.")
    key = (leg_m, base)
    if key in rec and reg:
        a, b = rec[key]["all"], rec[key]["by"].get(reg)
        if b and b["n"] >= 5:
            lines.append(f"Its record in past {reg.replace('_', ' ')} periods: {b['n']} trades, win rate {b['win']:.0f}%, "
                         f"avg {b['avg']:+,.0f} per trade vs {a['avg']:+,.0f} over all {a['n']} trades"
                         + (" (small sample)" if b["n"] < 20 else "") + ".")
        elif b:
            lines.append(f"Only {b['n']} past trades in this regime: too few to judge.")
        else:
            lines.append(f"No past trades in a {reg.replace('_', ' ')} period ({a['n']} trades in other regimes): untested here.")
    elif reg and r["group"] != "Directional spreads":
        lines.append("No trade log for this market in the backtest bundle, so no regime record.")
    cal = mk.get("calendar")
    if cal and cal.get("weekend"):
        lines.append("It is the weekend: crypto is calmer and thinner; weekend entries did no worse in tests.")
    vo = mk.get("volume") or {}
    if vo.get("breakout") and vo.get("high_volume") and vo.get("breakout_test"):
        lines.append(f"Volume: high-volume breakout today ({vo['breakout_test']} in tests for this market).")
    secs = {s_["section"]: s_ for s_ in (sections or {}).get("sections", [])}
    bd = [f"{g} {secs[g]['score']:+d} ({secs[g]['word']})" for g in BACKDROP.get(leg_m, []) if g in secs]
    if bd:
        lines.append("Backdrop: " + ", ".join(bd) + " (context only).")
    return lines


# ---------------------------------------------------------------------------------------------------------
# IMPACT SCORES: every reading we compute, per market, on one -1..+1 scale, grouped like the backdrop sections.
# Item score: +1 = strongly supportive for the market going UP (except "Stretch": +1 = stretched up).
# Group score = average of its items x 100. Groups say what they are USED FOR in decisions.
# ---------------------------------------------------------------------------------------------------------
def _c1(v):
    return None if v is None or (isinstance(v, float) and math.isnan(v)) else float(max(-1.0, min(1.0, v)))


def _item(name, score, value=None, note="", unit=""):
    return {"name": name, "score": None if score is None else round(_c1(score), 2), "value": value, "note": note, "unit": unit}


def _group(key, name, items, used, meaning, signed=True):
    sc = [i["score"] for i in items if i["score"] is not None]
    g = {"key": key, "name": name, "used": used, "meaning": meaning, "items": items, "n": len(sc),
         "score": None if not sc else int(round(float(np.mean(sc)) * 100))}
    if sc:
        w = 1 / len(sc)
        for i in items:
            i["weight"] = round(w, 3) if i["score"] is not None else 0
            i["points"] = None if i["score"] is None else round(i["score"] * w * 100, 1)
        best = max((i for i in items if i["score"] is not None), key=lambda i: i["score"])
        worst = min((i for i in items if i["score"] is not None), key=lambda i: i["score"])
        v = g["score"]
        if signed:
            word = ("strongly supportive" if v >= 60 else "supportive" if v >= 25 else "slightly supportive" if v >= 10 else
                    "strongly against" if v <= -60 else "against" if v <= -25 else "slightly against" if v <= -10 else "neutral")
        else:
            word = ("stretched up" if v >= 40 else "leaning up" if v >= 15 else "stretched down" if v <= -40 else
                    "leaning down" if v <= -15 else "not stretched")
        g["word"] = word
        txt = f"{word.capitalize()} ({v / 10:+.1f})."
        if best["score"] > 0.25:
            txt += f" Biggest push up: {best['name']}."
        if worst["score"] < -0.25:
            txt += f" Biggest push down: {worst['name']}."
        g["summary"] = txt
    return g


def impact_now(m, mk, out, as_of):
    x = mk.get("exposure_regime") or {}
    nd = mk.get("next_days") or {}
    en = nd.get("entry") or {}
    groups = []
    # 1. Trend
    sgn = 1 if (x.get("ret_20d") or 0) > 0 else -1
    rows = (mk.get("mtf") or {}).get("rows") or []
    live = [r for r in rows if r.get("available")]
    up = sum(r.get("trend") == "Up" for r in live)
    dn = sum(r.get("trend") == "Down" for r in live)
    dly = next((r for r in live if r.get("tf") == "daily"), None)
    items = [
        _item("20-day return", (x.get("ret_20d") or 0) / max(1e-9, (x.get("vol_20d") or 15) * 0.3), f"{x.get('ret_20d')}%",
              "scaled by volatility: a normal (about 1 sd) 20-day move = full score"),
        _item("Price vs 200-day average", (x.get("px_vs_ema200") or 0) / max(1e-9, (x.get("vol_20d") or 15) * math.sqrt(200 / 252)),
              f"{x.get('px_vs_ema200')}%", "scaled by volatility: one normal 200-day move away = full score (was a fixed 10% for every market)"),
        _item("Trend efficiency x direction", sgn * (x.get("er_20") or 0) / 0.6, x.get("er_20"), "0 = choppy, 1 = straight line"),
        _item("Trend strength (ADX) x direction", sgn * max(0.0, ((x.get("adx") or 0) - 15) / 25), x.get("adx"), "ADX above 40 = full score"),
        _item("Timeframes agreeing (5m..daily)", (up - dn) / max(1, len(live)), f"up {up} / down {dn}"),
    ]
    if dly:
        items.append(_item("Daily indicator panel (7)", (dly["up"] - dly["down"]) / 7, f"up {dly['up']} / down {dly['down']}"))
    try:  # classic trend indicators (EMA stack, SMA 50/200, Supertrend, Ichimoku, Bollinger, VWAP, PSAR) - context only
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__))); import trend_classic as TC
        _b = M.market_bars(m)
        _b = _b[pd.DatetimeIndex(_b["available_at"]).tz_convert(D.IST) <= as_of]
        classic = TC.items(_b, _intraday_1m(m, as_of), _item)
    except Exception as _e:
        classic = [_item("Classic trend indicators", None, "not available", str(_e)[:80])]
    # TREND (redesigned 2026-10-23, data_store/research/trend_review): the old Trend, Moving averages, Trend signals, Price position and
    # Timeframe agreement sections were one reading counted five times (correlation 0.64-0.93, 65% of the overall). Now ONE Trend section:
    #   Long-term direction  = price vs 200-day (vol-scaled) + SMA 50/200        (half of the section)
    #   Medium-term momentum = 20-day return (vol-scaled) + EMA 9/21/50 stack + one vote of Supertrend / PSAR / Ichimoku   (other half)
    #   Trend strength       = ADX + efficiency, shown as a label only (no direction, no points)
    # Price position (Bollinger, VWAP) moved to Stretch (timing). Timeframe agreement stays as context without the daily panel.
    allr = items + classic
    one = lambda n: next((i for i in allr if i["name"].startswith(n)), None)
    sig = [i for i in (one("Supertrend"), one("Parabolic SAR"), one("Ichimoku")) if i and i["score"] is not None]
    vote = _item("Trend signals vote (Supertrend, PSAR, Ichimoku)", float(np.mean([i["score"] for i in sig])) if sig else None,
                 " · ".join(f"{i['name'].split(' (')[0]} {'up' if i['score'] > 0 else 'down' if i['score'] < 0 else 'mixed'}" for i in sig),
                 "the three classic trend-following signals averaged into one vote")
    subs_def = [("Long-term direction", [one("Price vs 200-day"), one("SMA 50/200")]),
                ("Medium-term momentum", [one("20-day return"), one("EMA 9/21/50"), vote])]
    titems, subs = [], []
    for sname, its in subs_def:
        its = [i for i in its if i]
        sc = [i["score"] for i in its if i["score"] is not None]
        for i in its:
            i["sub"] = sname
            i["weight"] = round(0.5 / len(sc), 3) if (sc and i["score"] is not None) else 0
            i["points"] = None if i["score"] is None or not sc else round(i["score"] * i["weight"] * 100, 1)
        subs.append({"name": sname, "score": None if not sc else int(round(float(np.mean(sc)) * 100)), "n": len(sc)})
        titems += its
    adx_v, er_v = x.get("adx"), x.get("er_20")
    if adx_v is not None or er_v is not None:
        st = np.mean([v for v in (None if adx_v is None else max(0.0, min(1.0, (adx_v - 15) / 25)), None if er_v is None else min(1.0, er_v / 0.6)) if v is not None])
        word = "strong, clean trend" if st >= 0.6 else "moderate trend" if st >= 0.3 else "weak / choppy"
        sitem = _item("Trend strength (ADX + efficiency)", None, f"{word} · ADX {adx_v} · efficiency {er_v}", "how clean the move is - no direction, no points")
        sitem["sub"] = "Trend strength (label only)"; sitem["weight"] = 0; sitem["strength"] = round(float(st), 2)
        titems.append(sitem); subs.append({"name": "Trend strength (label only)", "score": None, "n": 1})
    ss = [sb["score"] for sb in subs if sb["score"] is not None]
    g = _group("trend", "Trend", [i for i in titems if i["score"] is not None], "Regime label -> position size; strategy regime records",
               "Direction of the trend on the daily chart: half long-term direction, half medium-term momentum. Strength is shown as a label.")
    g["items"] = titems; g["subs"] = subs
    g["score"] = None if not ss else int(round(float(np.mean(ss))))
    if g["score"] is not None:
        v = g["score"]
        g["word"] = ("strongly supportive" if v >= 60 else "supportive" if v >= 25 else "slightly supportive" if v >= 10 else
                     "strongly against" if v <= -60 else "against" if v <= -25 else "slightly against" if v <= -10 else "neutral")
        g["summary"] = (f"{g['word'].capitalize()} ({v / 10:+.1f}). Long-term {subs[0]['score'] if subs[0]['score'] is not None else '-'}, "
                        f"momentum {subs[1]['score'] if subs[1]['score'] is not None else '-'}" + (f"; {word}." if adx_v is not None or er_v is not None else "."))
    groups.append(g)
    ta = one("Timeframes agreeing")
    if ta:
        groups.append(_group("tf_agree", "Timeframe agreement", [ta], "Context",
                             "Do the 5-minute, hourly, daily and weekly trends point the same way. Context only - not in the overall score."))
    price_pos_items = [i for i in (one("Bollinger position"), one("Above / below VWAP")) if i]
    # 2. Volatility (+ = calm)
    items = [_item("Realised volatility (1y percentile)", None if x.get("vol_pct") is None else (50 - x["vol_pct"]) / 50,
                   f"{x.get('vol_20d')}% · {x.get('vol_pct')}th pct", "calm = supportive")]
    if x.get("iv_pct") is not None:
        items.append(_item("Implied volatility (1y percentile)", (50 - x["iv_pct"]) / 50, f"{x['iv_pct']}th pct"))
    else:
        vp = next((r for r in (out.get("vol_pos") or []) if r.get("market") == m), None)
        ivr = (vp or {}).get("implied_vol") or {}
        if ivr.get("available"):
            items.append(_item(f"Implied volatility ({ivr.get('source')})", (50 - ivr["pct_1y"]) / 50, f"{ivr.get('value')} · {ivr['pct_1y']}th pct"))
    b = nd.get("big_move") or {}
    if b.get("normal"):
        items.append(_item("Chance of a big 5-day move", (b["normal"] - b["chance"]) / max(1, b["normal"]), f"{b['chance']}% vs {b['normal']}%",
                           "tested: vol clustering"))
    ch = mk.get("character") or {}
    if ch.get("vol_of_vol") is not None:
        items.append(_item("Volatility of volatility", (0.3 - ch["vol_of_vol"]) / 0.3, ch["vol_of_vol"]))
    groups.append(_group("vol", "Volatility", items, "Position size (vol target) and big-move warning",
                         "How calm or wild the market is. Plus = calm (bigger size allowed), minus = stressed."))
    # 3. Stretch (+ = stretched up)
    items = []
    if en.get("z20") is not None:
        items.append(_item("Distance from 20-day average (sd)", en["z20"] / 2, en["z20"], "2 sd = full"))
    if en.get("rsi") is not None:
        items.append(_item("RSI 14", (en["rsi"] - 50) / 25, en["rsi"], "75 = +1, 25 = -1"))
    try:   # position inside the 60-day high-low range: top = +1, bottom = -1 (replaces 'drawdown from 60-day high', which could only be <= 0)
        _r = M.market_bars(m)
        _r = _r[pd.DatetimeIndex(_r["available_at"]).tz_convert(D.IST) <= as_of].tail(60)
        _hi, _lo, _px = float(_r["high"].max()), float(_r["low"].min()), float(_r["close"].iloc[-1])
        if _hi > _lo:
            items.append(_item("Position in 60-day range", (_px - _lo) / (_hi - _lo) * 2 - 1,
                               f"{(_px / _hi - 1) * 100:+.1f}% from high, {(_px / _lo - 1) * 100:+.1f}% from low", "top of range = +1, bottom = -1"))
    except Exception:
        if x.get("dd_60d") is not None:
            items.append(_item("Drawdown from 60-day high", x["dd_60d"] / 10, f"{x['dd_60d']}%", "-10% = -1"))
    items += price_pos_items            # Bollinger %B and VWAP: where price sits right now (moved here from Trend, 2026-10-23)
    if items:
        g = _group("stretch", "Stretch", items, "Entry timing: no new shorts when stretched down (tested on real trades)",
                   "How far price has run without a pause, and where it sits in its Bollinger band and against VWAP. Not a direction call: stretched down means don't START shorts here.", signed=False)
        groups.append(g)
    # 4. Volume & flows
    vo = mk.get("volume") or {}
    items = []
    if vo.get("available"):
        items.append(_item("Up-day vs down-day volume (20d)", math.log(max(1e-6, vo["updown_20d"])) / math.log(2), vo["updown_20d"],
                           "2x = +1, 0.5x = -1"))
        items.append(_item("Today's volume vs normal", None, f"{vo['rel_vol']}x", "context only (unsigned)"))
    lv = (mk.get("levels") or {}).get("intraday") or {}
    if lv.get("oi_change_last_session") is not None:
        upp, oiu = lv["price_change_last_session_pct"] > 0, lv["oi_change_last_session"] > 0
        st = {(True, True): ("long build-up", 1), (False, True): ("short build-up", -1), (True, False): ("short covering", 0.5),
              (False, False): ("long unwinding", -0.5)}[(upp, oiu)]
        items.append(_item("Futures OI last session", st[1], st[0], "OI with price direction"))
    fc = out.get("fii_cash") or {}
    if m in ("NIFTY", "BANKNIFTY") and fc.get("available"):
        items.append(_item("FII cash market (latest)", fc["fii_net_cr"] / 10000, f"{fc['fii_net_cr']:+,.0f} cr", "Rs 10,000 cr = full"))
        items.append(_item("DII cash market (latest)", fc["dii_net_cr"] / 10000, f"{fc['dii_net_cr']:+,.0f} cr"))
    if items:
        groups.append(_group("flow", "Volume and flows", items, "Context (volume only matters for breakouts in gold / US futures)",
                             "Who is doing the buying and selling."))
    # 5. Positioning
    items = []
    vp = next((r for r in (out.get("vol_pos") or []) if r.get("market") == m), None)
    po = (vp or {}).get("positioning") or {}
    if po.get("available"):
        pct = po.get("pct_3y", po.get("pct_1y"))
        if "FII" in (po.get("metric") or ""):
            items.append(_item("FII index-futures long %", None if pct is None else (pct - 50) / 50, f"{po['value']:.1f}% · {pct}th pct",
                               "more FII longs = supportive"))
        elif "funding" in (po.get("metric") or "").lower():
            items.append(_item("Perpetual funding (crowding)", -((po["value"] or 0) - 10.95) / 25, f"{po['value']:.1f}%/yr",
                               "vs the normal ~11%/yr base rate: well above = crowded longs (against), below = supportive"))
        else:
            items.append(_item("Speculators net long (CFTC)", None if pct is None else -(pct - 50) / 50, f"{po['value']:.1f}% · {pct}th pct",
                               "crowded long = against (contrarian)"))
    opt = options_now(m, as_of)
    if opt:
        items.append(_item("Put/call ratio, near expiry", (float(opt["pcr_oi_near"]) - 1) / 0.5, round(float(opt["pcr_oi_near"]), 2),
                           "puts written > calls = supportive"))
        if opt.get("atm_straddle"):
            items.append(_item("Spot vs max pain", (float(opt["max_pain"]) - float(opt["underlying"])) / float(opt["atm_straddle"]),
                               f"max pain {float(opt['max_pain']):.0f}", "pull toward max pain"))
    if items:
        groups.append(_group("pos", "Positioning", items, "Context only (not yet tested as a signal)",
                             "How crowded traders are. Very crowded on one side is a warning, not a support."))
    # 6. Models
    md = mk.get("models") or {}
    items = []
    if md.get("hmm"):
        p = md["hmm"]["p"]
        items.append(_item("Hidden Markov model: calm minus stressed", p[0] - p[2], md["hmm"]["state"]))
    if md.get("gmm"):
        p = md["gmm"]["p"]
        items.append(_item("Gaussian mixture: calm minus stressed", p[0] - p[2], md["gmm"]["state"]))
    if items:
        groups.append(_group("models", "Statistical models", items, "Context only (not consistent enough in tests)",
                             "Data-driven state detection from return and volatility."))
    # 7. Backdrop (sections + macro labels)
    secs = {s_["section"]: s_ for s_ in ((out.get("sections") or {}).get("sections") or [])}
    items = [_item(f"{g} backdrop", secs[g]["score"] / 100, secs[g]["score"]) for g in BACKDROP.get(m, []) if g in secs]
    ml = out.get("macro_labels") or {}
    lab_items = [("Risk appetite (India)" if m in ("NIFTY", "BANKNIFTY", "SENSEX") else "Risk appetite (global)",
                  ml.get("india_score") if m in ("NIFTY", "BANKNIFTY", "SENSEX") else ml.get("global_score"), 1),
                 ("Money conditions (tight = against)", ml.get("money_score"), -1), ("Growth", ml.get("growth_score"), 1)]
    for nm, v, sg in lab_items:
        if v is not None:
            items.append(_item(nm, sg * v / 1.5, round(v, 2), "1.5 sd = full"))
    if (out.get("sections") or {}).get("overall") is not None:
        items.append(_item("Overall world backdrop (8 sections)", out["sections"]["overall"] / 100, out["sections"]["overall"]))
    g = _group("backdrop", "Macro backdrop", items, "Context only (no macro reading beat luck as a forecast)",
               "The macro and cross-asset sections most relevant to this market, plus the macro labels. All eight world sections are listed below.")
    g["sections"] = [{"section": s_["section"], "score": s_["score"], "word": s_["word"], "summary": s_["summary"],
                      "relevant": s_["section"] in BACKDROP.get(m, [])} for s_ in ((out.get("sections") or {}).get("sections") or [])]
    groups.append(g)
    # 8. News
    nw = out.get("news") or {}
    items = []
    if nw.get("global_stress") is not None:
        items.append(_item("Global stress news", -nw["global_stress"] / 2, nw["global_stress"]))
    if m in ("NIFTY", "BANKNIFTY", "SENSEX") and nw.get("india_mood") is not None:
        items.append(_item("India news mood", nw["india_mood"] / 2, nw["india_mood"]))
    if items:
        groups.append(_group("news", "News", items, "Context only", "World news tone and stress themes (GDELT)."))
    # 9. Nifty engine (research)
    if m == "NIFTY":
        ne = out.get("nifty_engine") or {}
        items = [_item(g["name"], g["score"], g["score"], f"{g['available']}/{g['total']} inputs; draft weight {g['weight']}") for g in ne.get("groups", [])]
        g = _group("nifty_engine", "Nifty engine · six groups", items, "Research only (5-day forecast failed its accuracy test)",
                   "The original Nifty meta-score: six input groups, each -1..+1.")
        g["detail"] = [{"group": gg["name"], "items": gg["items"]} for gg in ne.get("groups", [])]
        groups.append(g)
    signed = [g["score"] for g in groups if g["key"] not in ("stretch", "nifty_engine", "vol", "models") and g["score"] is not None]
    net = int(round(float(np.mean(signed)))) if signed else None
    # weighted overall score on -10..+10 (2026-10-21). Weights: tested file if present, else starting weights.
    W0 = {"trend": 40, "flow": 30, "pos": 30}          # 2026-10-23: one Trend section (was 5 overlapping trend sections = 65%)
    W, wsrc = dict(W0), "starting weights (not tested yet)"
    try:
        _wf = json.load(open(os.path.join(ROOT, "data_store/research/impact_weights.json")))
        _wm = (_wf.get("markets") or {}).get(m) or _wf.get("default")
        if _wm and _wf.get("use"):
            W, wsrc = {k: float(v) for k, v in _wm.items()}, _wf.get("label", "tested weights")
        elif _wf.get("made"):
            wsrc = f"starting weights - weight test {_wf['made'][:10]}: no section predicted the next 5 days, so not a forecast"
    except Exception:
        pass
    have = {g["key"]: g["score"] for g in groups if g["score"] is not None and W.get(g["key"], 0) > 0}
    wsum = sum(W[k] for k in have)
    overall = None
    if wsum > 0:
        overall = {"score": round(sum(W[k] * v for k, v in have.items()) / wsum / 10, 1),
                   "weights": {k: round(W[k] / wsum * 100, 1) for k in have},
                   "source": wsrc, "label": "context",
                   "left_out": "Timeframe agreement (context), Volatility (position size), Stretch (timing), Statistical models, Macro backdrop, News, Nifty engine"}
    return {"groups": groups, "net": net, "overall": overall,
            "note": ("Net pressure = plain average of the direction groups (Stretch, Volatility, Statistical models and the research engine left out - calm vs stressed is not a direction). It shows what is pushing on "
                     "the market right now - it is NOT a forecast: no combination of these beat luck at predicting direction.")}


# ---------------------------------------------------------------------------------------------------------
# REGIME OVERLAY (2026-10-22, data_store/research/strategy_regime/REPORT.md). Only rules that improved results on
# BOTH the old (before 2024-07) and the new data AND beat a shuffled-label luck test are used:
#   O1 GOLD / BTC directional strategies: only trade with the 200-day average (skip trades against it)
#   O2 trend-following strategies: no new entries when our regime engine says Bear_Trend
#   O3 trend-following strategies: half size when implied vol (India VIX / VIX / DVOL) is in the top third of its year
# Advisory: shown next to the strategy's action, it does not change the action itself.
# ---------------------------------------------------------------------------------------------------------
def _overlay_states():
    path = "data_store/research/strategy_regime/states_today.json"
    try:
        st = json.load(open(path))
        if st.get("date") == pd.Timestamp.now(tz=D.IST).strftime("%Y-%m-%d"):
            return st["markets"]
    except Exception:
        pass
    sys.path.insert(0, os.path.join(ROOT, "scripts", "research"))
    import strategy_regime_study as SRS
    mk = {}
    for m in ("NIFTY", "BANKNIFTY", "BTC", "ETH", "SOL", "GOLD", "US"):
        try:
            x = SRS.states(m).dropna(how="all").iloc[-1]
            mk[m] = {k: (None if pd.isna(v) else str(v)) for k, v in x.items()}
            mk[m]["as_of"] = str(SRS.states(m).dropna(how="all").index[-1].date())
        except Exception as e:
            mk[m] = {"error": str(e)[:60]}
    json.dump({"date": pd.Timestamp.now(tz=D.IST).strftime("%Y-%m-%d"), "markets": mk}, open(path, "w"), indent=1)
    return mk


def regime_overlay(rows):
    sys.path.insert(0, os.path.join(ROOT, "scripts", "research"))
    from strategy_regime_study import family
    st = _overlay_states()
    for r in rows:
        m = {"US_STOCKS": "US", "ES": "US", "NQ": "US"}.get(r.get("market"), r.get("market"))
        s = st.get(m)
        if not s or "error" in s:
            continue
        fam = "short_vol" if r.get("group") == "Option selling" else family(r.get("strategy", ""))
        size, notes = 1.0, []
        if fam in ("trend", "meanrev", "other") and m in ("GOLD", "BTC") and s.get("direction"):
            notes.append(f"only {'longs' if s['direction'] == 'above 200d' else 'shorts'} ({m} {s['direction']}; trades against the 200-day lost money in both test periods)")
        if fam == "trend" and s.get("regime") == "Bear_Trend":
            size = 0.0
            notes.append("no new entries: regime engine says Bear_Trend (trend strategies lost money there in both periods)")
        if fam == "trend" and s.get("ivol") == "High" and size > 0:
            size = min(size, 0.5)
            notes.append("half size: implied vol in the top third of its year (trend trades earned less per unit of risk there)")
        r["overlay"] = {"family": fam, "size_x": size, "notes": notes, "states": {k: s.get(k) for k in ("trend", "direction", "vol", "ivol", "ivchg", "regime")},
                        "as_of": s.get("as_of"), "label": "tested overlay - advisory"}


def decision_now(m, mk):
    """The few things to act on, in one place."""
    x, nd = mk.get("exposure_regime") or {}, mk.get("next_days") or {}
    en, b, rg = nd.get("entry") or {}, nd.get("big_move") or {}, (nd.get("range") or {}).get("5") or {}
    do = []
    if en.get("no_new"):
        pz = en.get("pullback_zone") or {}
        nf = (lambda v: f"{v:,.0f}") if (mk.get("next_days") or {}).get("close", 0) > 1000 else (lambda v: f"{v:,.2f}")
        do.append(f"No new {en['no_new']}s - market stretched. Re-enter on a {'bounce' if en['no_new'] == 'short' else 'dip'} to "
                  f"{nf(pz.get('from'))} - {nf(pz.get('to'))}" + (f" or a retest of {nf(en['retest_level'])}." if en.get("retest_level") else ".")
                  if pz else f"No new {en['no_new']}s - market stretched.")
    reg = x.get("regime")
    if reg == "Bull_Trend":
        do.append("Uptrend: trend-following longs on at the position size; buy dips into the pullback zone.")
    elif reg == "Bear_Trend":
        do.append("Downtrend: shorts / hedges on at the position size; longs only small.")
    elif reg == "Range":
        do.append("Range: favour mean-reversion and premium selling; breakouts are less reliable.")
    elif reg == "Compression":
        do.append("Compression: wait for the breakout direction before committing.")
    elif reg == "High_Volatility":
        do.append("High Volatility: smallest size, wider stops, no naked short options.")
    if b.get("word", "").endswith("higher than normal"):
        do.append("Big move more likely than usual (direction unknown): keep size at or below the rule.")
    elif b.get("word") == "lower than normal":
        do.append("Quiet days more likely: premium selling within the expected range is favoured.")
    return {"regime": reg, "days": x.get("days_in_regime"), "size": x.get("size_blend"), "character": (mk.get("character") or {}).get("character"),
            "range5": rg, "big_move": b, "pullback_zone": en.get("pullback_zone"), "retest": en.get("retest_level"),
            "stretched": en.get("stretched"), "do": do, "close": nd.get("close"), "date": nd.get("date")}


ENGINES = [
    {"name": "Exposure regime (5 states)", "role": "Labels the market Bull Trend / Bear Trend / Range / Compression / High Volatility from price trend and volatility.",
     "feeds": "Position size (with the vol target) and the strategy desk", "status": "Tested: blend matched buy & hold return with ~half the drawdown"},
    {"name": "Multi-timeframe trend engine", "role": "Uptrend / downtrend / range / panic on 5m to daily bars, with the 7-indicator panel.",
     "feeds": "Each strategy's past record in this regime (strategy desk)", "status": "Descriptive"},
    {"name": "Next 3-5 days outlook", "role": "Expected range, big-move chance, stretch, pullback zone and retest level.",
     "feeds": "Entry timing (no new shorts when stretched), spreads, possible action", "status": "Range and big-move chance tested; direction is not predicted"},
    {"name": "Strategy router / desk", "role": "Run / paper / pause for every strategy, directional and weekend option spreads.",
     "feeds": "What to trade", "status": "Regime-based on/off switching did not beat luck, so it only pauses on clear evidence"},
    {"name": "Macro backdrop and labels", "role": "Eight sections (India, Global, Volatility, Risk appetite, Positioning, Macro, Cross-asset, News) and four macro labels.",
     "feeds": "Context only", "status": "No macro reading beat luck at predicting direction"},
    {"name": "Statistical models (HMM, GMM)", "role": "Data-driven calm / stressed states.", "feeds": "Context only",
     "status": "Not consistent enough across periods"},
    {"name": "Nifty meta-score engine (six groups)", "role": "The original Nifty bullish/bearish 5-day call.", "feeds": "Research only",
     "status": "Failed its accuracy (go/no-go) test"},
]


def _ensure(pkgs):
    import importlib.util
    import subprocess
    miss = [p for p, mod in pkgs if importlib.util.find_spec(mod) is None]
    if miss:
        subprocess.run([sys.executable, "-m", "pip", "install", "--quiet", *miss], timeout=600)


LAG = {}
_lp = "data_store/backtests/detection_lag.csv"
if os.path.exists(_lp):
    _l = pd.read_csv(_lp)
    for _m, _g in _l.groupby("market"):
        LAG[_m] = _g.drop(columns=["market"]).where(pd.notna(_g.drop(columns=["market"])), None).to_dict("records")
_hp = "data_store/backtests/hmm_challenger.csv"
MODEL_TEST = {}
if os.path.exists(_hp):
    _h = pd.read_csv(_hp)
    for (_m, _e), _g in _h.groupby(["market", "engine"]):
        MODEL_TEST.setdefault(_m, {})[_e] = {r["period"]: r["Sharpe"] for _, r in _g.iterrows()}


def models_now(m, as_of):
    """HMM + GMM state probabilities (challengers; not used for sizing) on the long daily history."""
    deep = "data_store/market_data/yahoo_daily_deep.csv"
    spot = "data_store/market_data/binance_daily.csv"
    src, sym = (spot, HM_BINANCE[m]) if m in HM_BINANCE else (deep, HM_DEEP.get(m))
    if sym is None or not os.path.exists(src):
        return None
    try:
        _ensure([("hmmlearn", "hmmlearn"), ("scikit-learn", "sklearn")])
        from src.common.regime_engine import hmm_regime as HM
        d = pd.read_csv(src)
        d = d[d["symbol"] == sym].copy()
        d["date"] = pd.to_datetime(d["date"])
        c = d.drop_duplicates("date", keep="last").set_index("date").sort_index()["close"]
        india = m in ("NIFTY", "BANKNIFTY", "SENSEX")
        avail = (c.index + (pd.Timedelta(hours=15, minutes=31) if india else pd.Timedelta(hours=27, minutes=30))).tz_localize(D.IST)
        if m in HM_BINANCE:                               # UTC daily candle, complete at 00:00 UTC next day
            avail = (c.index + pd.Timedelta(days=1, hours=5, minutes=30)).tz_localize(D.IST)
        c = c[avail <= as_of]
        if len(c) < 600:                                  # history not downloaded yet
            return None
        out = HM.live_probs(c, sym.replace("^", "").replace("=", ""), "data_store/models",
                            rolling_years=3 if m in M.CRYPTO else None)
        if not out:
            return None                                   # not enough history yet (needs 500 days before 1 January)
        key = HM_KEY.get(m)
        out["lag"] = LAG.get(key)
        out["test"] = MODEL_TEST.get(key)
        return out
    except Exception as e:
        return {"error": str(e)[:200]}


def character_now(m, as_of):
    b = M.market_bars(m)
    b = b[pd.DatetimeIndex(b["available_at"]).tz_convert(D.IST) <= as_of]
    if len(b) < 260:
        return None
    vol_ok = m not in ("NIFTY", "BANKNIFTY") or b["volume"].fillna(0).gt(0).mean() > 0.8
    f = CH.compute(b, has_volume=vol_ok).iloc[-1]
    keys = ["character", "adx", "eff_ratio", "er_pct", "hurst", "autocorr", "chop", "vol_yz", "vol_gk", "vol_pct", "vol_of_vol",
            "bb_width", "bbw_pct", "drawdown_pct", "rel_volume", "skew_60", "atr_pct"]
    return {k: (None if pd.isna(f[k]) else (f[k] if isinstance(f[k], str) else round(float(f[k]), 3))) for k in keys}

MARKET_INFO = {
    "NIFTY": ("India", "Nifty 50 futures"), "BANKNIFTY": ("India", "Bank Nifty futures"), "SENSEX": ("India", "Sensex"),
    "BTC": ("Crypto", "Bitcoin"), "ETH": ("Crypto", "Ethereum"), "SOL": ("Crypto", "Solana"),
    "GOLD": ("Commodities", "Gold"), "SILVER": ("Commodities", "Silver"), "COPPER": ("Commodities", "Copper"),
    "CRUDE": ("Commodities", "Crude oil (WTI)"), "ES": ("US", "S&P 500 futures"), "NQ": ("US", "Nasdaq-100 futures"),
    "US_STOCKS": ("US", "US stocks (SPY)"),
    "ALUMINUM": ("Commodities", "Aluminium"), "NATGAS": ("Commodities", "Natural gas"),
    "BNB": ("Crypto", "BNB"), "XRP": ("Crypto", "XRP"), "UNI": ("Crypto", "Uniswap"), "ZEC": ("Crypto", "Zcash"),
    "LINK": ("Crypto", "Chainlink"), "DASH": ("Crypto", "Dash"),          # 2026-11-03 (Nikhil)
}
FEATURE_NAMES = {
    "C.6a_ema21_distance": "Distance from 21-EMA", "C.6b_sma5_8_stack": "SMA 5/8 stack", "C.6c_return_z": "20-bar return",
    "C.6d_ema50_slope": "50-EMA slope", "C.12a_realized_vol_pct": "Realized vol (low = calm)", "C.12b_vol_change": "Vol change",
    "C.5a_pct_above_50dma": "% stocks above 50-DMA", "C.5b_adv_dec_10d": "Advance/decline 10d",
    "C.1a_india_vix_z": "India VIX level", "C.1b_vix_change": "India VIX change", "C.14_iv_rv_spread": "IV minus realized vol",
    "C.2a_futures_basis": "Futures basis", "C.2b_oi_buildup": "OI build-up", "C.4a_fii_fut_long_ratio": "FII index-futures long %",
    "C.4b_fii_ratio_change": "FII long % change", "C.3p_fii_option_bias": "FII option bias",
    "A.1_us10y_change": "US 10Y yield trend", "A.2a_us_vix": "US VIX", "A.2b_spx_trend": "S&P 500 trend",
    "A.2c_dollar_index": "Dollar index trend", "A.3_brent": "Brent crude trend", "B.3_usdinr": "USD/INR trend",
    "T.1_ema_9_21_50": "EMA 9/21/50", "T.2_sma_50_200": "SMA 50/200", "T.3_supertrend": "Supertrend",
    "T.4_ichimoku": "Ichimoku", "T.5_bollinger": "Bollinger", "T.6_vwap": "VWAP", "T.7_parabolic_sar": "Parabolic SAR",
}
GROUP_NAMES = {"TREND": "Price trend", "VOLATILITY": "Volatility", "BREADTH": "Market breadth",
               "POSITIONING": "Positioning (FII / OI)", "GLOBAL": "Global macro", "TECHNICALS": "Technical panel"}


def clean(x):
    if isinstance(x, (np.floating, float)):
        return None if (math.isnan(x) or math.isinf(x)) else round(float(x), 4)
    if isinstance(x, (np.integer,)):
        return int(x)
    if isinstance(x, dict):
        return {k: clean(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [clean(v) for v in x]
    if isinstance(x, pd.Timestamp):
        return str(x)
    return x


def tone(v, pos=0.15):
    if v is None:
        return "na"
    return "bull" if v > pos else "bear" if v < -pos else "neutral"


def series(s: pd.Series, n=90):
    s = s.dropna().iloc[-n:]
    return [[str(i.date()) if hasattr(i, "date") else str(i), clean(v)] for i, v in s.items()]


def _z1y(s: pd.Series):
    """Today's level vs the past year of daily values (z-score and percentile). None if < 60 days."""
    s = s.dropna()
    s = s.groupby(pd.DatetimeIndex(s.index).normalize()).last()
    h = s.iloc[-253:-1]
    if len(h) < 60 or h.std() == 0:
        return None, None
    return float((s.iloc[-1] - h.mean()) / h.std()), float((h <= s.iloc[-1]).mean() * 100)


# +1 = a high reading is good for risk assets, -1 = a high reading is bad, 0 = context only (not scored)
SCORE_SIGN = {"btc_funding": -1, "curve": 1, "gold": -1, "oi": 0, "btc_nq": 0}


def macro_tiles(as_of: pd.Timestamp):
    asd = as_of.tz_localize(None).normalize() if as_of.tzinfo else as_of.normalize()
    tiles = []

    def add(key, label, s, unit, fmt, good_when, note, src, india=False):
        s = s.dropna()
        # point-in-time: India EOD known at 15:30 IST that day; US/global daily known ~22:00 UTC (03:30 IST next day)
        known = (s.index + (pd.Timedelta(hours=15, minutes=30) if india else pd.Timedelta(hours=22 + 5.5))).tz_localize(D.IST)
        s = s[known <= as_of]
        if s.empty:
            return
        last, prev5 = float(s.iloc[-1]), float(s.iloc[-6]) if len(s) > 6 else float(s.iloc[0])
        chg = last - prev5
        direction = "up" if chg > 0 else "down" if chg < 0 else "flat"
        signal = "neutral"
        if good_when == "low":
            signal = "bull" if chg < 0 else "bear" if chg > 0 else "neutral"
        elif good_when == "high":
            signal = "bull" if chg > 0 else "bear" if chg < 0 else "neutral"
        tiles.append({"key": key, "label": label, "value": round(last, 4), "unit": unit, "fmt": fmt,
                      "change_5d": round(chg, 4), "direction": direction, "signal": signal, "note": note,
                      "as_of": str(s.index[-1].date()), "source": src, "series": series(s, 90),
                      "_z": _z1y(s), "_gw": good_when})

    y = lambda sym: D.load_yahoo_daily(sym)["close"]
    add("vix", "India VIX", y("^INDIAVIX"), "", "1", "low", "Fear gauge; option sellers want it above ~13-14.5", "NSE via Yahoo", india=True)
    poi = D.load_participant_oi()
    fii = poi[poi["client_type"] == "FII"].set_index("date").sort_index()
    add("fii_long", "FII index-futures long %", 100 * fii["fut_idx_long"] / (fii["fut_idx_long"] + fii["fut_idx_short"]),
        "%", "1", "high", "Share of FII index futures that are long", "NSE participant OI", india=True)
    fut = D.load_futures_daily("NIFTY")
    spot = y("^NSEI")
    add("basis", "Nifty futures basis", (fut["close"] / spot.reindex(fut.index) - 1) * 100, "%", "2", "high",
        "Near-month futures premium over spot", "NSE futures + index", india=True)
    add("oi", "Nifty futures OI", fut["oi_total"] / 1e6, "M", "2", "none", "Total open interest, all expiries (million units)", "NSE futures", india=True)
    add("usdinr", "USD / INR", y("INR=X"), "", "2", "low", "Rupee weakness (rising) is usually negative for Nifty", "Yahoo")
    add("usvix", "US VIX", y("^VIX"), "", "1", "low", "US fear gauge", "CBOE via Yahoo")
    add("dxy", "Dollar index", y("DX-Y.NYB"), "", "1", "low", "Strong dollar pulls money out of emerging markets", "ICE via Yahoo")
    add("brent", "Brent crude", y("BZ=F"), "$", "1", "low", "India imports most of its oil", "ICE via Yahoo")
    add("us10y", "US 10Y yield", y("^TNX"), "%", "2", "low", "Higher US yields compete with EM equities", "CBOE via Yahoo")
    add("spx", "S&P 500", y("^GSPC"), "", "0", "high", "Global risk appetite", "Yahoo")
    gold = M.market_bars("GOLD")["close"]
    gold.index = gold.index.tz_localize(None) if getattr(gold.index, "tz", None) else gold.index
    add("gold", "Gold", gold, "$", "0", "none", "Safe-haven demand", "COMEX via Yahoo")

    # ---- step-1 additions: series that carry their own publication time (available_at, UTC) ----
    def add_pit(key, label, df, unit, fmt, good_when, note, src, group):
        if df is None or df.empty:
            return
        k = df[df["available_at"] <= as_of.tz_convert("UTC")]["value"].dropna()
        if k.empty:
            return
        k.index = pd.DatetimeIndex(k.index).tz_localize(None) if getattr(k.index, "tz", None) else pd.DatetimeIndex(k.index)
        last, prev5 = float(k.iloc[-1]), float(k.iloc[-6]) if len(k) > 6 else float(k.iloc[0])
        chg = last - prev5
        sig = "neutral"
        if good_when == "low":
            sig = "bull" if chg < 0 else "bear" if chg > 0 else "neutral"
        elif good_when == "high":
            sig = "bull" if chg > 0 else "bear" if chg < 0 else "neutral"
        daily = k.groupby(k.index.normalize()).last()
        tiles.append({"key": key, "label": label, "value": round(last, 4), "unit": unit, "fmt": fmt, "group": group,
                      "change_5d": round(chg, 4), "direction": "up" if chg > 0 else "down" if chg < 0 else "flat",
                      "signal": sig, "note": note, "as_of": str(k.index[-1].date()), "source": src, "series": series(daily, 90),
                      "_z": _z1y(daily), "_gw": good_when})

    def ratio(a, b):
        if a is None or b is None:
            return None
        j = a[["value", "available_at"]].join(b[["value", "available_at"]], rsuffix="_b", how="inner")
        return pd.DataFrame({"value": j["value"] / j["value_b"], "available_at": j[["available_at", "available_at_b"]].max(axis=1)},
                            index=j.index)

    def fred(sid):
        f = VP._csv("macro_fred.csv")
        if f is None:
            return None
        f = f[f["series"] == sid].copy()
        f["date"] = pd.to_datetime(f["date"])
        f = f.set_index("date").sort_index()
        return pd.DataFrame({"value": f["value"], "available_at": pd.to_datetime(f["available_at"]).dt.tz_localize("UTC")})

    yd = VP.yahoo_daily
    add_pit("vix_term", "VIX / VIX3M", ratio(yd("^VIX"), yd("^VIX3M")), "", "2", "low",
            "Above 1 = near-term fear higher than 3-month: stress", "CBOE via Yahoo", "Volatility")
    add_pit("move", "MOVE (bond volatility)", yd("^MOVE"), "", "0", "low", "Bond-market fear; spikes before equity stress", "ICE via Yahoo", "Volatility")
    add_pit("ovx", "OVX (crude volatility)", yd("^OVX"), "", "1", "low", "Oil-market fear", "CBOE via Yahoo", "Volatility")
    add_pit("btc_dvol", "BTC DVOL", VP.dvol("BTC"), "", "1", "low", "Bitcoin 30-day implied volatility", "Deribit", "Volatility")
    add_pit("credit", "Credit appetite (HYG / IEF)", ratio(yd("HYG"), yd("IEF")), "", "3", "high",
            "Junk bonds vs Treasuries; falling = risk-off", "Yahoo", "Risk appetite")
    add_pit("hy_spread", "High-yield spread", fred("BAMLH0A0HYM2"), "%", "2", "low", "Widening = credit stress (FRED keeps 3 years)", "ICE BofA via FRED", "Risk appetite")
    add_pit("midcap", "Midcap / Nifty", ratio(yd("^NSEMDCP50"), yd("^NSEI")), "", "3", "high", "Rising = Indian risk appetite", "NSE via Yahoo", "Risk appetite")
    add_pit("xly_xlp", "Cyclicals / defensives (XLY / XLP)", ratio(yd("XLY"), yd("XLP")), "", "2", "high", "Rising = US risk-on", "Yahoo", "Risk appetite")
    add_pit("smallcap", "Small caps / large (IWM / RSP)", ratio(yd("IWM"), yd("RSP")), "", "3", "high", "Rising = broad risk appetite", "Yahoo", "Risk appetite")
    hg, gc = M.market_bars("COPPER"), M.market_bars("GOLD")
    cg = pd.DataFrame({"value": hg["close"] / gc["close"].reindex(hg.index) * 1000,
                       "available_at": pd.DatetimeIndex(hg["available_at"]).tz_convert("UTC")}, index=hg.index.tz_localize(None) if hg.index.tz else hg.index).dropna()
    add_pit("copper_gold", "Copper / gold (x1000)", cg, "", "2", "high", "Rising = growth over fear", "COMEX via Yahoo", "Risk appetite")
    add_pit("btc_funding", "BTC funding (7d, %/yr)", VP.funding_annual("BTC"), "%", "1", "none",
            "Above ~25 = crowded longs; below 0 = crowded shorts", "Binance", "Positioning")
    add_pit("us2y", "US 2Y yield", fred("DGS2"), "%", "2", "low", "Where markets see Fed policy", "FRED", "Macro")
    add_pit("curve", "US 10Y minus 2Y", fred("T10Y2Y"), "%", "2", "none", "Below 0 = inverted curve (recession signal)", "FRED", "Macro")
    add_pit("real10", "US 10Y real yield", fred("DFII10"), "%", "2", "low", "Biggest driver of gold; high = tight money", "FRED", "Macro")
    add_pit("nfci", "Financial conditions (NFCI)", fred("NFCI"), "", "2", "low", "Above 0 = tighter than average", "Chicago Fed via FRED", "Macro")
    w, t, r = fred("WALCL"), fred("WTREGEN"), fred("RRPONTSYD")
    if w is not None and t is not None and r is not None:
        idx = w.index
        liq = (w["value"] - t["value"].reindex(idx, method="ffill") - 1000 * r["value"].reindex(idx, method="ffill")) / 1e6
        av = pd.concat([w["available_at"], t["available_at"].reindex(idx, method="ffill"),
                        r["available_at"].reindex(idx, method="ffill")], axis=1).max(axis=1)
        add_pit("net_liq", "Fed net liquidity ($ trillion)", pd.DataFrame({"value": liq, "available_at": av}).dropna(), "", "2", "high",
                "Fed balance sheet minus Treasury cash minus reverse repo; rising helps risk assets", "FRED", "Macro")
    xs = XC.series_for_tiles()
    add_pit("avg_corr", "Average correlation, 12 markets", xs["avg_corr"], "", "2", "low",
            "60-day; high = everything moving together (systemic stress)", "computed", "Cross-asset")
    add_pit("stock_bond", "Stock-bond correlation", xs["stock_bond"], "", "2", "low",
            "S&P vs Treasuries, 60-day; above +0.3 = they fall together (inflation regime)", "computed", "Cross-asset")
    add_pit("curve3m", "US 10Y minus 3M", xs["curve_10y3m"], "%", "2", "high", "Below 0 = inverted (classic recession warning)", "Yahoo", "Cross-asset")
    add_pit("btc_nq", "BTC vs Nasdaq correlation", xs["btc_vs_nq"], "", "2", "none", "60-day; high = crypto trading as a tech/risk asset", "computed", "Cross-asset")
    for t in tiles:
        t.setdefault("group", "India" if t["key"] in ("vix", "fii_long", "basis", "oi", "usdinr") else "Global")
        z, pct = t.pop("_z")
        gw = t.pop("_gw")
        sign = SCORE_SIGN.get(t["key"], {"high": 1, "low": -1}.get(gw, 0))
        t["z_1y"] = None if z is None else round(z, 2)
        t["pct_1y"] = None if pct is None else round(pct)
        t["score_sign"] = sign
        t["score"] = None if (z is None or sign == 0) else round(float(np.clip(z * sign, -3, 3)), 2)
    return tiles


SECTION_ORDER = ["India", "Global", "Volatility", "Risk appetite", "Positioning", "Macro", "Cross-asset", "News"]
SECTION_MEANING = {
    "India": "Indian fear gauge, foreign-investor futures positioning, futures premium and the rupee.",
    "Global": "US fear gauge, dollar, oil, US yields, S&P 500 and gold as a fear hedge.",
    "Volatility": "How nervous option markets are across stocks, bonds, oil and crypto.",
    "Risk appetite": "Whether investors are choosing riskier assets (junk bonds, small and mid caps, cyclicals, copper) over safe ones.",
    "Positioning": "How crowded traders are. Very crowded longs are a warning, not a support.",
    "Macro": "US rates, financial conditions and Fed liquidity: the cost and supply of money.",
    "Cross-asset": "Whether markets move together (stress) and what the yield curve says about recession risk.",
    "News": "World news tone and attention to stress themes (war, recession, banking, crash talk, tariffs) from GDELT.",
}


def _ord(n):
    n = int(n)
    return f"{n}{'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


def _word(v):
    return ("strongly supportive" if v >= 60 else "supportive" if v >= 25 else "slightly supportive" if v >= 10 else
            "strongly negative" if v <= -60 else "negative" if v <= -25 else "slightly negative" if v <= -10 else "neutral")


def section_scores(tiles: list, news: dict | None) -> dict:
    """Composite score per section. Method (fixed, not fitted): each reading is turned into a z-score of today's
    level against its own past year, the sign is set so that + = good for risk assets, clipped at +-3, and the
    readings in a section are averaged with EQUAL weights. Score = average z x 50, capped at +-100
    (so +100 = readings on average 2 standard deviations on the supportive side)."""
    comp = {g: [] for g in SECTION_ORDER}
    for t in tiles:
        comp.setdefault(t["group"], []).append({"key": t["key"], "label": t["label"], "z": t.get("z_1y"), "pct_1y": t.get("pct_1y"),
                                                "sign": t.get("score_sign"), "score": t.get("score"), "value": t.get("value")})
    if news:
        if news.get("global_stress") is not None:
            comp["News"].append({"key": "global_stress", "label": "Global stress news", "z": news["global_stress"],
                                 "pct_1y": news.get("global_stress_pct_1y"), "sign": -1,
                                 "score": round(float(np.clip(-news["global_stress"], -3, 3)), 2), "value": news["global_stress"]})
        if news.get("india_mood") is not None:
            comp["News"].append({"key": "india_mood", "label": "India news mood", "z": news["india_mood"], "pct_1y": None, "sign": 1,
                                 "score": round(float(np.clip(news["india_mood"], -3, 3)), 2), "value": news["india_mood"]})
    out = []
    for g in SECTION_ORDER:
        items = comp.get(g) or []
        scored = [c for c in items if c["score"] is not None]
        if not scored:
            continue
        w = 1 / len(scored)
        for c in items:
            c["weight"] = round(w, 3) if c["score"] is not None else 0
            c["contribution"] = None if c["score"] is None else round(c["score"] * w * 50, 1)
        avg = float(np.mean([c["score"] for c in scored]))
        sc = int(round(np.clip(avg * 50, -100, 100)))
        best = max(scored, key=lambda c: c["score"])
        worst = min(scored, key=lambda c: c["score"])
        def why(c):
            z = c["z"] or 0
            lvl = "near its normal level" if abs(z) < 0.5 else ("high" if z > 0 else "low") + " for its past year"
            return f"{c['label']} is {lvl}" + (f" ({_ord(c['pct_1y'])} percentile)" if c.get("pct_1y") is not None else "")
        txt = f"{_word(sc).capitalize()} ({sc:+d})."
        if best["score"] > 0.3:
            txt += f" Main support: {why(best)}."
        if worst["score"] < -0.3:
            txt += f" Main drag: {why(worst)}."
        ctx = [c["label"] for c in items if c["score"] is None]
        out.append({"section": g, "score": sc, "word": _word(sc), "n": len(scored), "meaning": SECTION_MEANING.get(g, ""),
                    "summary": txt, "context_only": ctx, "items": items})
    overall = int(round(np.mean([o["score"] for o in out]))) if out else None
    return {"overall": overall, "overall_word": None if overall is None else _word(overall), "sections": out,
            "method": ("Each reading is compared with its own past year (z-score of today's level), flipped so that plus = good for "
                       "risk assets, capped at 3, and averaged with equal weights inside each section. Score = average x 50, "
                       "capped at +-100. The overall backdrop is the plain average of the sections. Equal weights are used on "
                       "purpose: in our tests fitted weights did not hold up out of sample, and equal-weight standardised "
                       "composites are the standard transparent method used by financial-stress and leading-indicator indices."),
            "how_used": ("These scores are context. They do not change the regime label (that comes from price trend and "
                         "volatility only) and they do not switch strategies on or off: in tests no macro or news reading "
                         "predicted direction better than luck, and the regime-based strategy router also did not beat luck. "
                         "Position size uses the exposure regime and volatility (the blend), which did pass.")}


def narrative(mkt: str, reg: dict, mtf: dict, nifty_call=None, vix=None):
    parts = []
    name = MARKET_INFO[mkt][1]
    s = mtf["summary"]
    parts.append(f"{name} is in a {reg['regime_name'].lower()} on the daily chart with {reg['vol_state'].lower()} volatility.")
    live = [r for r in mtf["rows"] if r["available"]]
    ups = sum(r["trend"] == "Up" for r in live)
    downs = sum(r["trend"] == "Down" for r in live)
    parts.append(f"{ups} of {len(live)} timeframes point up and {downs} point down; the wait zone is {s['wait']}.")
    d = next((r for r in mtf["rows"] if r["tf"] == "daily" and r["available"]), None)
    if d:
        parts.append(f"Daily indicators: {d['up']} bullish, {d['down']} bearish.")
    if nifty_call:
        f = {x["feature_id"]: x["score"] for x in nifty_call["profiles"]["daily"]["features"]}
        notes = []
        if vix is not None:
            notes.append(f"India VIX at {vix:.1f}")
        if f.get("C.4a_fii_fut_long_ratio") is not None:
            notes.append("FII index-futures positioning " + ("lighter than usual on the long side" if f["C.4a_fii_fut_long_ratio"] < 0 else "heavier on the long side than usual"))
        if f.get("A.2b_spx_trend") is not None:
            notes.append("global equities " + ("supportive" if f["A.2b_spx_trend"] > 0 else "weak"))
        if notes:
            txt = "; ".join(notes)
            parts.append(txt[0].upper() + txt[1:] + ".")
    return " ".join(parts)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--as-of", default="now", help='"now" (live) or a past IST time like "2026-10-04 06:00"')
    ap.add_argument("--out", default=None, help="output path (default data_store/dashboard/market_pulse_{date}.json)")
    a = ap.parse_args()
    os.chdir(ROOT)
    live = a.as_of == "now"
    as_of = pd.Timestamp.now(tz=D.IST).floor("min") if live else pd.Timestamp(a.as_of).tz_localize(D.IST)
    upd_path = "data_store/market_data/update_status.json"
    updates = json.load(open(upd_path)) if os.path.exists(upd_path) else None

    eng = RegimeEngine()
    call = eng.call(as_of)
    card, _, _ = build_scorecard(ROOT, eng)
    rec = recommend(call, card)
    ucard, _ = U.build_universe_scorecard(ROOT)
    urec, uregimes = U.recommend_universe(ROOT, as_of, ucard)

    # ---- per market ----
    markets = {}
    for m in M.MARKETS:
        reg = M.regime_now(m, as_of)
        st = M.market_states(m)
        st = st[st["available_at"] <= as_of].tail(90)
        mtf = M.mtf_table(m, as_of)
        try:  # per-timeframe section scores (-10..+10) for the "Timeframes at a glance" card (2026-10-21)
            sys.path.insert(0, os.path.dirname(os.path.abspath(__file__))); import trend_classic as TC
            for _r in mtf.get("rows", []):
                if not _r.get("available"):
                    continue
                _b = (TC.binance_bars(m, _r["tf"]) if live else None)   # crypto intraday: live Binance bars with real buy/sell volume
                if _b is None or len(_b) < 60:
                    _b = M.market_bars_tf(m, _r["tf"])
                _b = _b[pd.DatetimeIndex(_b["available_at"]).tz_convert(D.IST) <= as_of]
                _r["scores"] = TC.tf_scores(_b, intraday=_r["tf"] not in ("daily", "weekly", "monthly"))
                try:                        # 2026-10-31: same 5 regime names on this timeframe's own bars (understanding only)
                    from src.common.regime_engine import tf_regime as TR
                    _r["tf_regime"] = TR.tf_regime(_b)
                except Exception as _e2:
                    _r["tf_regime"] = {"error": str(_e2)[:80]}
        except Exception as _e:
            print("tf scores failed:", m, _e, flush=True)
        hist = [{"date": str(i.date()), "close": clean(r["close"]), "regime": r["regime_name"], "trend": clean(r["trend_score"])}
                for i, r in st.iterrows()]
        markets[m] = {"key": m, "class": MARKET_INFO[m][0], "name": MARKET_INFO[m][1], "regime": clean(reg),
                      "character": clean(character_now(m, as_of)),
                      "exposure_regime": clean(exposure_now(m, as_of)),
                      "models": clean(models_now(m, as_of)),
                      "trend_score": clean(st["trend_score"].iloc[-1]) if len(st) else None,
                      "mtf": clean(mtf), "history": hist,
                      "narrative": narrative(m, reg, mtf, call if m == "NIFTY" else None,
                                             (call.get("india_vix") or {}).get("value") if m == "NIFTY" else None)}
        markets[m]["next_days"] = clean(next_days_now(m, markets[m]["exposure_regime"]))
        markets[m]["levels"] = clean(levels_now(m, as_of))
        markets[m]["volume"] = clean(volume_now(m))
        markets[m]["calendar"] = clean(calendar_now(m, as_of))

    # ---- NIFTY engine detail ----
    d = call["profiles"]["daily"]
    feats = {x["feature_id"]: x["score"] for x in d["features"]}
    w = call and eng.weights["profiles"]["daily"]["groups"]
    groups = []
    for g, gname in GROUP_NAMES.items():
        items = [{"id": fid, "name": FEATURE_NAMES.get(fid, fid), "score": clean(feats.get(fid)), "tone": tone(feats.get(fid)),
                  "meaning": SIGN_CONVENTION.get(fid, "")} for fid, gg in FEATURE_GROUPS.items() if gg == g]
        gs = d["group_scores"].get(g)
        groups.append({"key": g, "name": gname, "score": clean(gs), "tone": tone(gs), "weight": clean(w.get(g, 0)),
                       "available": sum(1 for i in items if i["score"] is not None), "total": len(items), "items": items})
    contrib = sorted([{"name": FEATURE_NAMES.get(fid, fid), "group": GROUP_NAMES[FEATURE_GROUPS[fid]], "score": clean(s)}
                      for fid, s in feats.items() if s is not None], key=lambda x: -abs(x["score"]))

    # ---- data health ----
    def last_date(s):
        return str(pd.Series(s).dropna().index.max().date())
    health = [
        {"source": "Nifty / Bank Nifty futures, daily", "provider": "NSE", "last": last_date(D.load_futures_daily("NIFTY")["close"])},
        {"source": "Nifty futures, 5-minute", "provider": "Dhan", "last": str(D.load_futures_5m("NIFTY").index.max().date())},
        {"source": "India VIX", "provider": "NSE via Yahoo", "last": last_date(D.load_yahoo_daily("^INDIAVIX")["close"])},
        {"source": "FII / DII participant OI", "provider": "NSE archives", "last": str(D.load_participant_oi()["date"].max().date())},
        {"source": "Stock breadth (229 NSE stocks)", "provider": "Backtrader_App stocks_data.csv", "last": str(D.load_stock_panel()["date"].max().date())},
        {"source": "Global macro (US VIX, S&P, DXY, Brent, US10Y, USD/INR)", "provider": "Yahoo", "last": last_date(D.load_yahoo_daily("^GSPC")["close"])},
        {"source": "Crypto, gold, US futures (all timeframes)", "provider": "Yahoo", "last": last_date(M.market_bars("BTC")["close"])},
        {"source": "Strategy trade logs", "provider": "MASTER_BACKTESTER + Backtrader_App", "last": "2026-10-03 snapshot"},
    ]
    asd = as_of.tz_localize(None).normalize()
    for h in health:
        try:
            age = (asd - pd.Timestamp(h["last"])).days
            h["age_days"] = max(age, 0)
            h["status"] = "fresh" if age <= 3 else "stale" if age <= 30 else "old"
        except Exception:
            h["age_days"], h["status"] = None, "snapshot"

    out = {
        "meta": {"as_of": str(as_of), "generated": str(pd.Timestamp.now(tz=D.IST).floor("s")), "mode": "live" if live else "snapshot",
                 "updates": updates,
                 "price_source": call["price_source"], "weights": call["weight_set_version"],
                 "weights_status": call["weight_set_status"],
                 "forecast_note": "The Bullish/Bearish forecast has not passed the accuracy test (BRD go/no-go); the regime itself is descriptive.",
                 "data_note": ("Live data: prices refresh every few minutes from Yahoo and Dhan (Nifty / Bank Nifty live every 2 s); NSE end-of-day files are added each evening. "
                               if live else "Snapshot data downloaded 2026-10-03/04. ")
                 + "5m-30m bars cover the last 60 days; Nifty intraday futures start 2026-07-29."},
        "markets": markets,
        "nifty_engine": {"forecast_label": d["regime_label"], "forecast_confidence": d["confidence_tier"], "meta_score": d["meta_score"],
                         "data_completeness": d["data_completeness"], "groups": groups, "contributions": contrib,
                         "india_vix": call.get("india_vix")},
        "macro": macro_tiles(as_of),
        "vol_pos": clean(VP.table(M.MARKETS, as_of)),
        "fii_cash": clean(VP.fii_cash(as_of)),
        "macro_labels": clean(ML.now(as_of)),
        "cross_asset": clean(XC.now(as_of)),
        "news": clean(NW.now(as_of)),
        "sections": None,
        "events": {"next": call["calendar"]["events_next_days"], "next_core": call["calendar"]["next_core_event"],
                   "today": call["calendar"].get("event_today"), "weekly_expiry": call["calendar"]["is_weekly_expiry"]},
        "options": rec["option_strategies"],
        "catalog": [],
        "health": health,
    }
    try:
        out["sections"] = clean(section_scores(out["macro"], out["news"]))
    except Exception as e:
        print("section scores failed:", e)
    out["engines"] = ENGINES
    try:
        cm = json.load(open(os.path.join(ROOT, "data_store/backtests/cross_market.json")))
        out["cross_market"] = clean({"x1": cm.get("x1"), "x2": cm.get("x2"), "x3": cm.get("x3"),
                                     "x4": {k: {kk: vv for kk, vv in v.items() if kk != "series"} for k, v in (cm.get("x4") or {}).items()}})
    except Exception:
        out["cross_market"] = None
    for m_, mk_ in markets.items():
        try:
            mk_["impact"] = clean(impact_now(m_, mk_, out, as_of))
            mk_["decision"] = clean(decision_now(m_, mk_))
        except Exception as e:
            print("impact failed", m_, e)
    # ---- strategy catalog ----
    rows = []
    for o in rec["option_strategies"]:
        rows.append({"group": "Option selling", "strategy": o["strategy"], "market": "BANKNIFTY" if "BANKNIFTY" in o["strategy"] else "NIFTY",
                     "leg": o.get("entry_rule", ""), "registry": o.get("tier", ""), "action": o["action"], "lots": o["lots"],
                     "reason": o["reason"], "source": "MASTER_BACKTESTER options"})
    for r in rec["recommendations"]:
        rows.append({"group": "Nifty directional", "strategy": r["strategy"], "market": "NIFTY", "leg": "NIFTY",
                     "registry": "", "action": r["action"], "lots": None, "reason": r["reason"], "source": "Backtrader_App"})
    for _, r in urec.iterrows():
        rows.append({"group": "All markets", "strategy": r["strategy"], "market": r["market"], "leg": r["leg"],
                     "registry": r["registry"], "action": r["action"], "lots": None, "reason": r["reason"], "source": r["source"]})
    try:
        rows = spread_ideas(markets, as_of, out.get("vol_pos")) + rows
    except Exception as e:
        print("spread ideas failed:", e)
    try:
        rec = regime_record()
        for r in rows:
            r["why"] = why_lines(r, markets, out.get("sections"), rec)
            r["nature"] = "Directional spread" if r["group"] == "Directional spreads" else nature_of(r["strategy"].split(" ")[0])
            r["horizon"] = horizon_of(r)
            h_ = _HOLD.get(r["strategy"].split(" ")[0]) if _HOLD else None
            r["hold_days"] = None if h_ is None else round(float(h_), 1)
    except Exception as e:
        print("why lines failed:", e)
    try:
        regime_overlay(rows)
    except Exception as e:
        print("regime overlay failed:", e)
    out["catalog"] = clean(rows)
    # ---- paper-trading journal: log once per market per daily bar (live builds only), then score ----
    try:
        from src.common.master_brain import journal as JR
        if live:
            cx = None
            pth = os.path.join(ROOT, "data_store", "flow", "oc_live.json")
            if os.path.exists(pth):
                cx = (json.load(open(pth)) or {}).get("cross")
            JR.log(markets, out["catalog"], cx)
        out["journal"] = clean(JR.score({m_: v[0] for m_, v in _BARS.items()}))
    except Exception as e:
        print("journal failed:", e)
        out["journal"] = None
    # ---- Regime score v2: direction + character per timeframe (5m / 15m / 75m / 1D / 1W), trust from walk-forward tests ----
    try:
        from src.common.regime_engine import score_v2_live as SV2
        out["score_v2"] = clean(SV2.live(ROOT))
    except Exception as e:
        print("score v2 failed:", e)
        out["score_v2"] = {"error": str(e)[:200]}
    # ---- US + crypto desks (2026-10-18): score v2 per market, playbooks, VIX / derivatives panels (scripts/global_desk.py) ----
    try:
        from src.common.regime_engine import score_v2_markets_live as SV2M
        out["score_v2_markets"] = clean(SV2M.live(ROOT))
    except Exception as e:
        print("score v2 markets failed:", e)
        out["score_v2_markets"] = {"error": str(e)[:200]}
    try:
        import global_desk as GDK
        g = GDK.build(markets, out.get("events"), out.get("catalog") or [], out.get("options") or [], out.get("macro") or [],
                      out.get("score_v2_markets"))
        out["us_desk"], out["crypto_desk"] = clean(g["us"]), clean(g["crypto"])
    except Exception as e:
        print("global desks failed:", e)
        out["us_desk"] = out["crypto_desk"] = {"error": str(e)[:200]}
    # ---- India desk: playbook, sectors, strong / weak stocks (scripts/india_desk.py) - read by the Desk tab and the agent ----
    try:
        import india_desk as IDK
        out["india"] = clean(IDK.build(markets, out.get("events"), out.get("catalog") or [], out.get("options") or [],
                                       out.get("macro") or [], out.get("fii_cash")))
    except Exception as e:
        print("india desk failed:", e)
        out["india"] = {"error": str(e)[:200]}
    try:                                     # one-line v2 read inside each Desk playbook
        for m_, pb in ((out.get("india") or {}).get("playbook") or {}).items():
            sv = ((out.get("score_v2") or {}).get("markets") or {}).get(m_)
            if sv:
                pb["score_v2"] = {"alignment": sv.get("alignment"), "intraday_bias": sv.get("intraday_bias"),
                                  "labels": {r["tf"]: {"label": r["label"], "direction": r["direction"], "status": r["status"]} for r in sv["timeframes"]}}
    except Exception as e:
        print("score v2 -> desk failed:", e)
    os.makedirs("data_store/dashboard", exist_ok=True)
    try:                                     # forward validation (src/common/validation/forward.py) - point-in-time ledger
        from src.common.validation import forward as FWD
        if not a.out or "market_pulse_live" in str(a.out):
            FWD.record(out, ROOT)
            FWD.evaluate(ROOT)
        vp = os.path.join(ROOT, "data_store", "validation", "report.json")
        out["validation"] = json.load(open(vp)) if os.path.exists(vp) else None
    except Exception as e:
        print("forward validation failed:", e)
    path = a.out or f"data_store/dashboard/market_pulse_{as_of:%Y-%m-%d}.json"
    with open(path + ".tmp", "w") as f:
        json.dump(clean(out), f, default=str)
    os.replace(path + ".tmp", path)
    print(path, os.path.getsize(path), "bytes;", len(rows), "catalog rows")


if __name__ == "__main__":
    main()
