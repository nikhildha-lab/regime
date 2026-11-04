"""
Regime score v2 - LIVE read for Nifty / Bank Nifty (used by scripts/export_dashboard_data.py and app/regime_mcp.py).

Data (all local, nothing downloaded here):
  5m / 15m / 75m : index 1-minute history (history/upstox_<NIFTY50|NIFTYBANK>_1m_<year>.csv.gz) + recent near-month futures
                   1-minute bars (nifty_futures_1m_upstox.csv), futures rescaled to index level so the join has no jump
  1D             : yahoo_daily_deep.csv (^NSEI / ^NSEBANK), India VIX (^INDIAVIX), breadth from nse_eq_closes (lagged 1 session)
  1W             : FRED US 10Y + USD/INR (history/), crude (CL=F) and S&P 500 (^GSPC) from yahoo_daily_deep
Trust per timeframe and label comes from score_v2_validation.json (written from scripts/research/test_score_v2.py).
Only closed bars are scored. Indications only - nothing here places or sizes orders.
"""
from __future__ import annotations

import glob
import json
import os

import numpy as np
import pandas as pd

from . import score_v2 as S

IDX = {"NIFTY": ("NIFTY50", "^NSEI"), "BANKNIFTY": ("NIFTYBANK", "^NSEBANK")}
TFS = ["5m", "15m", "75m", "1D", "1W"]
TF_NAME = {"5m": "5 min", "15m": "15 min", "75m": "75 min (India 4H)", "1D": "Daily", "1W": "Weekly"}
AGG = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
_VAL = None


def validation() -> dict:
    global _VAL
    if _VAL is None:
        try:
            _VAL = json.load(open(os.path.join(os.path.dirname(__file__), "score_v2_validation.json")))
        except Exception:
            _VAL = {"results": {}}
    return _VAL


def _minute(root: str, m: str) -> pd.DataFrame | None:
    h = os.path.join(root, "data_store/market_data/history")
    fs = sorted(glob.glob(os.path.join(h, f"upstox_{IDX[m][0]}_1m_*.csv.gz")))[-3:]
    if not fs:
        return None
    d = pd.concat(pd.read_csv(f) for f in fs)
    d.index = pd.to_datetime(d["datetime"].str[:19])
    d = d[~d.index.duplicated(keep="last")].sort_index().between_time("09:15", "15:29")[["open", "high", "low", "close"]].astype(float)
    d["volume"] = 0.0
    p = os.path.join(root, "data_store/market_data/nifty_futures_1m_upstox.csv")
    if os.path.exists(p):
        try:
            f = pd.read_csv(p)
            f = f[f["contract"].str.startswith(m + "_FUT")]
            f["ts"] = pd.to_datetime(f["ts"].str[:19])
            f["day"] = f["ts"].dt.date
            v = f.groupby(["day", "contract"])["volume"].sum().reset_index()
            best = v.loc[v.groupby("day")["volume"].idxmax()]
            f = f.merge(best[["day", "contract"]], on=["day", "contract"]).set_index("ts").sort_index()
            common = f.index.intersection(d.index)
            if len(common) >= 60:
                last_day = common[-1].normalize()
                cd = common[common >= last_day]
                ratio = float((d.loc[cd, "close"] / f.loc[cd, "close"]).median())
                new = f[f.index > d.index[-1]].between_time("09:15", "15:29")
                if len(new):
                    add = new[["open", "high", "low", "close"]] * ratio
                    add["volume"] = 0.0
                    d = pd.concat([d, add])
        except Exception:
            pass
    return d


def _bars(m1: pd.DataFrame, tf: str) -> pd.DataFrame:
    last = m1.index[-1]
    if tf == "75m":
        day = m1.index.normalize()
        slot = ((m1.index - day).total_seconds() // 60 - 555) // 75
        b = m1.groupby([day, slot]).agg(AGG)
        b.index = [d + pd.Timedelta(minutes=555 + 75 * int(k) + 74) for d, k in b.index]
        b = b.sort_index()
    else:
        rule = {"5m": "5min", "15m": "15min"}[tf]
        b = m1.resample(rule, origin="start_day", offset="9h15min", label="left").agg(AGG).dropna(subset=["close"])
        b.index = b.index + pd.Timedelta(rule) - pd.Timedelta(minutes=1)
    return b[b.index <= last]                                   # closed bars only (the bar still forming is dropped)


def _ydeep(root: str, sym: str) -> pd.DataFrame | None:
    p = os.path.join(root, "data_store/market_data/yahoo_daily_deep.csv")
    if not os.path.exists(p):
        return None
    Y = pd.read_csv(p)
    d = Y[Y["symbol"] == sym].copy()
    d["date"] = pd.to_datetime(d["date"])
    d = d.drop_duplicates("date", keep="last").set_index("date").sort_index()
    return d[d["close"] > 0]


def _breadth(root: str) -> pd.Series | None:
    fs = sorted(glob.glob(os.path.join(root, "data_store/market_data/history/nse_eq_closes_*.csv.gz")))[-3:]
    if not fs:
        return None
    P = pd.concat(pd.read_csv(f, usecols=["date", "symbol", "close"]).pivot_table(index="date", columns="symbol", values="close", aggfunc="last") for f in fs).sort_index()
    P.index = pd.to_datetime(P.index)
    P = P[~P.index.duplicated(keep="last")]
    ma = P.rolling(50, min_periods=40).mean()
    valid = ma.notna() & P.notna()
    return (((P > ma) & valid).sum(axis=1) / valid.sum(axis=1).replace(0, np.nan)).shift(1)   # NSE files arrive in the evening


def _fred(root: str, name: str) -> pd.Series | None:
    p = os.path.join(root, "data_store/market_data/history", name)
    if not os.path.exists(p):
        return None
    d = pd.read_csv(p)
    return pd.Series(pd.to_numeric(d["value"], errors="coerce").values, index=pd.to_datetime(d["date"])).dropna()


def _row(tf: str, sc: pd.DataFrame, m: str) -> dict:
    sc = sc.dropna(subset=["direction"])
    if sc.empty:
        return None
    r = sc.iloc[-1]
    runs = (sc["label"] != sc["label"].shift()).cumsum()
    val = (validation().get("results", {}).get(m, {}) or {}).get(tf, {})
    lab = r["label"]
    lv = val.get(lab) or {}
    dv = val.get("direction") or {}
    trust = []
    if dv.get("pass"):
        trust.append("direction tested")
    if lab in ("Volatile", "Range") and lv.get("pass"):
        trust.append(f"{lab.lower()} label tested (moves x{lv.get('abs_ratio')})")
    elif lab in ("Trending up", "Trending down") and lv.get("pass"):
        trust.append(f"{lab.lower()} label tested")
    return {"tf": tf, "name": TF_NAME[tf], "bar": str(sc.index[-1].date()) if tf in ("1D", "1W") else str(sc.index[-1])[:16], "direction": round(float(r["direction"]), 1),
            "trend": None if pd.isna(r["trend"]) else round(float(r["trend"])), "vol": None if pd.isna(r["vol"]) else round(float(r["vol"])),
            "character": [("Trending" if (r["trend"] or 0) >= S.LABEL_RULES["trend_hi"] else "Ranging" if (r["trend"] or 0) <= S.LABEL_RULES["trend_lo"] else "In between"),
                          ("Volatile" if (r["vol"] or 0) >= S.LABEL_RULES["vol_hi"] else "Calm" if (r["vol"] or 0) <= 40 else "Normal vol")],
            "label": lab, "raw_label": r["raw"], "bars_in_label": int((runs == runs.iloc[-1]).sum()),
            "reasons": S.reasons(r), "status": "tested" if trust else "context", "trust": trust,
            "features": {k[2:]: round(float(v), 2) for k, v in r.items() if str(k).startswith("f:") and pd.notna(v)}}


def live(root: str, markets=("NIFTY", "BANKNIFTY")) -> dict:
    out = {"version": "score v2 (2026-10-17)", "validation": {k: validation().get(k) for k in ("tested_on", "method", "horizons")},
           "note": ("Direction (-100..+100) and character (trend, volatility) are scored separately against each index's own last year. "
                    "Tested: the Volatile / Range character on 5m-75m (moves really are bigger / smaller next), and 5m / 15m Nifty direction "
                    "(small edge, ~2-4 bps per hour - context for timing, not a trade on its own). Daily and weekly DIRECTION did not predict "
                    "(context only); alignment is context, not a tested filter."), "markets": {}}
    crude, spx = _ydeep(root, "CL=F"), _ydeep(root, "^GSPC")
    vixd = _ydeep(root, "^INDIAVIX")
    macro = {"us10y": _fred(root, "fred_us10y_daily.csv"), "usdinr": _fred(root, "fred_usdinr_daily.csv"),
             "crude": None if crude is None else crude["close"], "spx": None if spx is None else spx["close"]}
    try:
        br = _breadth(root)
    except Exception:
        br = None
    for m in markets:
        rows, errs = {}, []
        try:
            m1 = _minute(root, m)
            if m1 is not None and len(m1) > 5000:
                for tf in ("5m", "15m", "75m"):
                    b = _bars(m1, tf).iloc[-(S.LOOKBACK[tf] * 2):]
                    rows[tf] = _row(tf, S.score(tf, b), m)
        except Exception as e:
            errs.append(f"intraday: {str(e)[:100]}")
        try:
            d = _ydeep(root, IDX[m][1])
            if d is not None:
                d = d[["open", "high", "low", "close"]]
                rows["1D"] = _row("1D", S.score("1D", d.iloc[-1500:], {"vix": None if vixd is None else vixd["close"], "breadth": br}), m)
                w = d.resample("W-FRI").agg({"open": "first", "high": "max", "low": "min", "close": "last"}).dropna()
                if d.index[-1].weekday() != 4:                     # week not finished: drop the forming weekly bar
                    w = w.iloc[:-1]
                wext = {k: (v.resample("W-FRI").last() if v is not None else None) for k, v in macro.items()}
                rows["1W"] = _row("1W", S.score("1W", w.iloc[-400:], wext), m)
        except Exception as e:
            errs.append(f"daily/weekly: {str(e)[:100]}")
        al = S.alignment({tf: r for tf, r in rows.items() if r})
        intr = [rows[t] for t in ("5m", "15m") if rows.get(t)]
        out["markets"][m] = {"timeframes": [rows.get(t) for t in TFS if rows.get(t)], "alignment": {**al, "status": "context"},
                             "intraday_bias": (round(float(np.mean([r["direction"] for r in intr])), 1) if intr else None),
                             "errors": errs}
    return out


def explain(block: dict, m: str, tf: str | None = None) -> str:
    mk = (block.get("markets") or {}).get(m)
    if not mk:
        return f"No score v2 data for {m}."
    lines = []
    for r in mk["timeframes"]:
        if tf and r["tf"] != tf:
            continue
        lines.append(f"{r['name']}: {r['label']} (direction {r['direction']:+.0f}, trend {r['trend']}, vol {r['vol']}, "
                     f"{r['bars_in_label']} bars) [{r['status']}{': ' + '; '.join(r['trust']) if r['trust'] else ''}]. "
                     f"Why: {'; '.join(r['reasons'])}.")
    a = mk["alignment"]
    if not tf:
        lines.append(f"Alignment: {a.get('word')} ({a.get('agree_pct')}% of weight agrees), weighted direction {a.get('direction')}, "
                     f"confidence {a.get('confidence')} [context].")
    return "\n".join(lines)
