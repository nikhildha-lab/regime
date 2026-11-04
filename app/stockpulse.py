"""
Nifty 50 stock -> the same Market Pulse market entry the index tabs use (2026-10-25, Nikhil: "stocks should have the same
structure, sections, charts and everything as Nifty").

Runs the SAME engines as scripts/export_dashboard_data.py, on Dhan bars for the stock:
  daily states + regime name (price features + vol state)      -> src.common.master_brain.markets logic
  timeframes 5m / 15m / 4h / daily / weekly / monthly + scores -> indicator panel + trend_classic.tf_scores
  exposure regime (5 states), position size, days in regime     -> regime_engine.exposure_regime (no implied vol for stocks)
  character, next 3-5 days outlook, decision ("what to do")     -> export_dashboard_data.character / next_days_now / decision_now
Not tested on stocks (the engines were validated on indices). Context only. Indications only - no orders.
"""
from __future__ import annotations

import os
import sys
import time
from datetime import date, datetime, timedelta

import pandas as pd

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
for p in (ROOT, os.path.join(ROOT, "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)
C: dict = {}
IST = "Asia/Kolkata"
TF_MIN = {"5m": 5, "15m": 15, "4h": 240}


def _hours(t=None):
    t = t or datetime.now()
    return t.weekday() < 5 and 9 * 60 + 15 <= t.hour * 60 + t.minute <= 15 * 60 + 35


def _bars(sym):
    """(daily naive-date index, 5-min IST index) from Dhan; cached 3 min in market hours, 3 h otherwise."""
    x = C.get(("b", sym))
    if x and time.time() - x[0] < (180 if _hours() else 3 * 3600):
        return x[1]
    import dhan_live as DL
    sid = DL.ids_equity(sym)
    if not sid:
        raise RuntimeError(f"{sym}: no Dhan security id")
    d = pd.DataFrame(DL.daily("NSE_EQ", sid, "EQUITY", date.today() - timedelta(days=365 * 9), date.today()))
    d["date"] = pd.to_datetime([t.date() for t in d.ts])
    d = d.drop_duplicates("date", keep="last").set_index("date").sort_index()[["open", "high", "low", "close", "volume"]]
    i = pd.DataFrame(DL.intraday("NSE_EQ", sid, "EQUITY", date.today() - timedelta(days=75), date.today(), 5, oi=False))
    if len(i):
        i["ts"] = pd.DatetimeIndex([t.replace(tzinfo=None) for t in i.ts]).tz_localize(IST)
        i = i.drop_duplicates("ts", keep="last").set_index("ts").sort_index()[["open", "high", "low", "close", "volume"]]
        last = i.index[-1].tz_localize(None).normalize()
        if last > d.index[-1]:                                   # today's partial day from the 5-minute bars
            t = i[i.index.tz_localize(None).normalize() == last]
            d.loc[last] = [t.open.iloc[0], t.high.max(), t.low.min(), t.close.iloc[-1], t.volume.sum()]
    C[("b", sym)] = (time.time(), (d, i))
    return d, i


def _tf_bars(d, i, tf):
    agg = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    if tf == "daily":
        b = d.copy()
        b.index = b.index.tz_localize(IST)
        b["available_at"] = b.index + pd.Timedelta(hours=15, minutes=31)
        return b
    if tf in ("weekly", "monthly"):
        b = d.resample("W-FRI" if tf == "weekly" else "ME").agg(agg).dropna(subset=["close"])
        b.index = b.index.tz_localize(IST)
        b["available_at"] = b.index.normalize() + pd.Timedelta(hours=15, minutes=31)
        return b
    if not len(i):
        return None
    if tf == "5m":
        b = i.copy()
    else:
        b = i.resample(f"{TF_MIN[tf]}min", origin="start_day", offset="9h15min", label="left", closed="left").agg(agg).dropna(subset=["close"])
    b["available_at"] = b.index + pd.Timedelta(minutes=TF_MIN[tf] + 1)
    return b


def _states(b, daily):
    from src.common.regime_engine.engine import describe_state
    from src.common.regime_engine.features import price_features as PF
    from src.common.regime_engine.features import indicator_panel as IP
    from src.common.master_brain.markets import TREND_FEATURES
    f = PF.compute_price_features(b["close"])
    trend = f[TREND_FEATURES].mean(axis=1, skipna=True)
    vol = PF.vol_state(b["close"])
    p = IP.compute_panel(b, daily=daily)
    names = [describe_state(t, v if isinstance(v, str) else None) for t, v in zip(trend, vol)]
    s = pd.DataFrame({"trend_score": trend.values, "vol_state": vol.values, "trend_state": [n[0] for n in names],
                      "regime_name": [n[1] for n in names], "panel_trend": p["panel_trend"].values, "close": b["close"].values,
                      "available_at": b["available_at"].values}, index=b.index)
    s["up"], s["down"], s["ichimoku"] = p["confluence_up"].values, p["confluence_down"].values, p["T.4_ichimoku"].values
    for col in IP.FEATURES:
        s[col] = p[col].values
    return s


def _mtf(d, i, now):
    from src.common.regime_engine.features import indicator_panel as IP
    import trend_classic as TC
    rows = []
    for tf in ("5m", "15m", "4h", "daily", "weekly", "monthly"):
        b = _tf_bars(d, i, tf)
        if b is None or len(b) < 30:
            rows.append({"tf": tf, "available": False, "note": "not enough bars"})
            continue
        if tf in ("weekly", "monthly"):
            b = b[b["available_at"] <= now]                       # completed bars only
        s = _states(b, daily=tf in ("daily", "weekly", "monthly"))
        r = s.iloc[-1]
        row = {"tf": tf, "available": True, "bar": str(r.name), "regime": r["regime_name"],
               "trend": r["panel_trend"] if isinstance(r["panel_trend"], str) else None,
               "up": int(r["up"]), "down": int(r["down"]), "ichimoku": r["ichimoku"],
               "indicators": {k.split("_", 1)[1]: IP.LABELS[k].get(int(r[k]), "Mixed") if pd.notna(r[k]) else "n/a" for k in IP.FEATURES},
               "close": round(float(r["close"]), 4)}
        try:
            row["scores"] = TC.tf_scores(b, intraday=tf not in ("daily", "weekly", "monthly"))
        except Exception:
            row["scores"] = None
        rows.append(row)
    live = [r for r in rows if r["available"]]
    for k, r in enumerate(live):
        higher = live[k + 1]["trend"] if k + 1 < len(live) else None
        r["wait_zone"] = "Active" if IP.wait_zone(r["trend"], r["up"], r["down"], r["ichimoku"], higher) else "Inactive"
    ups = sum(r["trend"] == "Up" for r in live); downs = sum(r["trend"] == "Down" for r in live)
    active = sum(r.get("wait_zone") == "Active" for r in live)
    return {"market": "", "as_of": str(now), "rows": rows,
            "summary": {"trend": "Up" if ups > downs else "Down" if downs > ups else "Flat", "wait": "ON" if live and active * 2 >= len(live) else "OFF",
                        "votes": f"up {ups} / down {downs} of {len(live)} timeframes"}}


def build(sym: str) -> dict:
    sym = sym.upper().strip()
    x = C.get(("m", sym))
    if x and time.time() - x[0] < (170 if _hours() else 1800):
        return x[1]
    import export_dashboard_data as ED
    from src.common.regime_engine import exposure_regime as XR
    from src.common.regime_engine.features import character as CH
    d, i = _bars(sym)
    now = pd.Timestamp.now(tz=IST)
    clean = ED.clean
    # exposure regime (same as exposure_now, without implied vol - stocks have no VIX)
    xr = None
    if len(d) >= 260:
        f = XR.features(d, None, 252)
        lab = XR.label(f)
        r = f.iloc[-1]
        vt = float(min(1.0, 0.10 / r["vol_20d"])) if r["vol_20d"] > 0 else None
        runs = (lab != lab.shift()).cumsum()
        ex = XR.EXPOSURE.get(lab.iloc[-1])
        xr = {"regime": lab.iloc[-1], "exposure": ex, "vol_target_exposure": round(vt, 2) if vt else None,
              "size_blend": round((ex + vt) / 2, 2) if ex is not None and vt is not None else None,
              "days_in_regime": int((runs == runs.iloc[-1]).sum()), "bar": str(f.index[-1].date()), "source": f"Dhan daily since {d.index[0].year}",
              "er_20": round(float(r["er_20"]), 2), "adx": round(float(r["adx"]), 1), "vol_20d": round(float(r["vol_20d"]) * 100, 1),
              "vol_pct": None if pd.isna(r["vol_pct"]) else round(float(r["vol_pct"])), "iv_pct": None,
              "px_vs_ema200": round(float(r["px_vs_ema200"]) * 100, 1), "dd_60d": round(float(r["dd_60d"]) * 100, 1),
              "ret_20d": round(float(r["ret_20d"]) * 100, 1), "validation": None, "close": round(float(d.close.iloc[-1]), 2)}
        ED._BARS[sym] = (d, None, 252)
    ch = None
    try:
        b = d.copy(); b["available_at"] = b.index
        vol_ok = b["volume"].fillna(0).gt(0).mean() > 0.8
        fc = CH.compute(b, has_volume=vol_ok).iloc[-1]
        keys = ["character", "adx", "eff_ratio", "er_pct", "hurst", "autocorr", "chop", "vol_yz", "vol_gk", "vol_pct", "vol_of_vol",
                "bb_width", "bbw_pct", "drawdown_pct", "rel_volume", "skew_60", "atr_pct"]
        ch = {k: (None if pd.isna(fc[k]) else (fc[k] if isinstance(fc[k], str) else round(float(fc[k]), 3))) for k in keys}
    except Exception:
        pass
    sd = _states(_tf_bars(d, i, "daily"), daily=True)
    last = sd.dropna(subset=["regime_name"]).iloc[-1]
    reg = {"market": sym, "bar": str(last.name.date()), "regime_name": last["regime_name"], "trend_state": last["trend_state"],
           "vol_state": last["vol_state"], "panel_trend": last["panel_trend"], "close": round(float(last["close"]), 2), "data_age_days": 0}
    hist = [{"date": str(k.date()), "close": clean(r["close"]), "regime": r["regime_name"], "trend": clean(r["trend_score"])} for k, r in sd.tail(90).iterrows()]
    mk = {"key": sym, "class": "India", "name": f"{sym} (NSE stock)", "stock": True, "regime": clean(reg), "character": clean(ch),
          "exposure_regime": clean(xr), "models": None, "trend_score": clean(sd["trend_score"].iloc[-1]),
          "mtf": clean(_mtf(d, i, now)), "history": hist, "narrative": None}
    mk["mtf"]["market"] = sym
    try:
        mk["next_days"] = clean(ED.next_days_now(sym, xr)) if xr else None
    except Exception as e:
        mk["next_days"] = {"error": str(e)[:100]}
    mk["levels"] = None; mk["volume"] = None; mk["calendar"] = None; mk["impact"] = None
    try:
        mk["decision"] = clean(ED.decision_now(sym, mk))
    except Exception as e:
        mk["decision"] = {"do": [f"decision failed: {str(e)[:80]}"]}
    live = _hours() and len(i) and i.index[-1].date() == date.today()
    px = float(i.close.iloc[-1]) if live else float(d.close.iloc[-1])
    prev = float(d.close.iloc[-2]) if len(d) > 1 else px
    mk["live"] = {"price": px, "chg_pct": round((px / prev - 1) * 100, 2), "at": str(i.index[-1])[:16] if live else f"{d.index[-1].date()} close",
                  "src": "Dhan 5-minute bars" if live else "Dhan daily close", "open": bool(live)}
    mk = clean(mk)
    C[("m", sym)] = (time.time(), mk)
    return mk
