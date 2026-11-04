"""
Regime score v2 - LIVE read for the US and crypto desks (used by scripts/global_desk.py through the export).
US:     S&P 500 (ES futures intraday, ^GSPC daily, VIX / VIX3M) and Nasdaq 100 (NQ futures)
CRYPTO: BTC, ETH, SOL (Yahoo 5m / 15m / 1h -> 4h, Binance / Yahoo daily, Binance funding, Deribit DVOL)
5m / 15m have only ~60 days of history here, so their percentiles use the history available and are marked
"short history"; they become proper once the Binance / US intraday downloads land. Trust comes from score_v2_validation.json.
Indications only - nothing here places or sizes orders.
"""
from __future__ import annotations

import json
import os

import numpy as np
import pandas as pd

from . import score_v2 as S
from . import score_v2_markets as SM

INSTR = {"US": {"SP500": {"name": "S&P 500", "intra": "ES=F", "daily": "^GSPC", "peer": "NQ=F"},
                "NDX": {"name": "Nasdaq 100", "intra": "NQ=F", "daily": "NQ=F", "peer": "ES=F"}},
         "CRYPTO": {"BTC": {"name": "Bitcoin", "intra": "BTC-USD"}, "ETH": {"name": "Ethereum", "intra": "ETH-USD"},
                    "SOL": {"name": "Solana", "intra": "SOL-USD"}}}
DASH_KEY = {"ES": "SP500", "US_STOCKS": "SP500", "NQ": "NDX", "BTC": "BTC", "ETH": "ETH", "SOL": "SOL"}
_C = {}


def _val():
    try:
        return json.load(open(os.path.join(os.path.dirname(__file__), "score_v2_validation.json")))
    except Exception:
        return {"results": {}}


def _csv(root, name, **kw):
    k = (name, tuple(sorted(kw.items())))
    if k not in _C:
        p = os.path.join(root, "data_store/market_data", name)
        _C[k] = pd.read_csv(p, **kw) if os.path.exists(p) else None
    return _C[k]


def _ydeep(root, sym):
    Y = _csv(root, "yahoo_daily_deep.csv")
    if Y is None:
        return None
    d = Y[Y["symbol"] == sym].copy()
    d["date"] = pd.to_datetime(d["date"])
    d = d.drop_duplicates("date", keep="last").set_index("date").sort_index()
    return d[d["close"] > 0][["open", "high", "low", "close"]]


def _intra(root, sym, iv, file="synaptic_intraday_all.csv"):
    D = _csv(root, file)
    if D is None:
        return None
    d = D[(D["symbol"] == sym) & (D["interval"] == iv)].copy()
    if d.empty:
        return None
    d["t"] = pd.to_datetime(d["datetime_utc"].astype(str).str[:19])
    return d.drop_duplicates("t", keep="last").set_index("t").sort_index()[["open", "high", "low", "close", "volume"]].astype(float)


def _rth(b):
    et = b.index.tz_localize("UTC").tz_convert("America/New_York")
    m = (et.hour * 60 + et.minute >= 570) & (et.hour * 60 + et.minute < 960)
    out = b[m].copy()
    out.index = et[m].tz_localize(None)
    return out


def _weekly(d):
    w = d.resample("W-FRI").agg({"open": "first", "high": "max", "low": "min", "close": "last"}).dropna()
    if len(d) and d.index[-1].weekday() != 4:
        w = w.iloc[:-1]
    return w


def _score(market, tf, b, ext=None):
    """Score with the market's lookback, shortened to the history available (marked) when the store is still short."""
    default = SM.LOOKBACK[market][tf]
    short = len(b) < default * 1.3
    if short:
        SM.LOOKBACK[market][tf] = max(200, int(len(b) * 0.6))
    try:
        return SM.score(market, tf, b, ext), short
    finally:
        SM.LOOKBACK[market][tf] = default


def _row(market, key, tf, sc, short):
    sc = sc.dropna(subset=["direction"])
    if sc.empty:
        return None
    r = sc.iloc[-1]
    runs = (sc["label"] != sc["label"].shift()).cumsum()
    val = ((_val().get("results") or {}).get(key) or {}).get(tf) or {}
    lab, trust = r["label"], []
    dv = val.get("direction") or {}
    if dv.get("pass"):
        trust.append("direction tested - REVERSAL: a high score has meant weaker next 10 days" if dv.get("reverse") else "direction tested")
    lv = val.get(lab) or {}
    if lab in ("Volatile", "Range") and lv.get("pass"):
        trust.append(f"{lab.lower()} label tested (moves x{lv.get('abs_ratio')})")
    elif lab in ("Trending up", "Trending down") and lv.get("pass"):
        trust.append(f"{lab.lower()} label tested")
    tr, vo = r["trend"], r["vol"]
    return {"tf": tf, "name": SM.TF_NAME[tf], "bar": str(sc.index[-1].date()) if tf in ("1D", "1W") else str(sc.index[-1])[:16],
            "direction": round(float(r["direction"]), 1), "trend": None if pd.isna(tr) else round(float(tr)),
            "vol": None if pd.isna(vo) else round(float(vo)),
            "character": [("Trending" if (tr or 0) >= S.LABEL_RULES["trend_hi"] else "Ranging" if (tr or 0) <= S.LABEL_RULES["trend_lo"] else "In between"),
                          ("Volatile" if (vo or 0) >= S.LABEL_RULES["vol_hi"] else "Calm" if (vo or 0) <= 40 else "Normal vol")],
            "label": lab, "raw_label": r["raw"], "bars_in_label": int((runs == runs.iloc[-1]).sum()), "reasons": S.reasons(r),
            "status": "tested" if trust else "context", "trust": trust, "short_history": bool(short),
            "untested_note": val.get("note"),
            "features": {k[2:]: round(float(v), 2) for k, v in r.items() if str(k).startswith("f:") and pd.notna(v)}}


def _fred(root, name):
    p = os.path.join(root, "data_store/market_data/history", name)
    if not os.path.exists(p):
        return None
    d = pd.read_csv(p)
    return pd.Series(pd.to_numeric(d["value"], errors="coerce").values, index=pd.to_datetime(d["date"])).dropna()


def _dxy(root):
    D = _csv(root, "synaptic_daily.csv")
    if D is None:
        return None
    d = D[D["symbol"] == "DX-Y.NYB"].copy()
    d["d"] = pd.to_datetime(d["datetime_local"].astype(str).str[:10])
    return d.drop_duplicates("d", keep="last").set_index("d")["close"]


def live(root: str) -> dict:
    out = {"version": "score v2 markets (2026-10-18)", "tested_on": _val().get("markets_tested_on"),
           "note": ("Same method as Nifty. Tested so far: the Volatile character on the 1-hour (US) and daily S&P / Nasdaq bars, "
                    "Range and Volatile on Bitcoin's 4-hour bars, and on the daily S&P 500 the direction score works in REVERSE "
                    "(a high score has been followed by weaker 10 days). Crypto daily / weekly direction did not predict. "
                    "5 / 15-minute rows use only ~60 days of history until the intraday downloads land - context only."),
           "US": {}, "CRYPTO": {}}
    vix, vix3m = _ydeep(root, "^VIX"), _ydeep(root, "^VIX3M")
    dxy, us10y = _dxy(root), _fred(root, "fred_us10y_daily.csv")
    wk = lambda s: None if s is None else s.resample("W-FRI").last()
    # ---- US ----
    for key, ins in INSTR["US"].items():
        rows, errs = {}, []
        try:
            for tf in ("5m", "15m"):
                b = _intra(root, ins["intra"], tf)
                if b is not None and len(b) > 300:
                    b = _rth(b)
                    ext = None
                    if tf == "5m":
                        p = _intra(root, ins["peer"], "5m")
                        ext = {"peer": _rth(p)["close"]} if p is not None else None
                    sc, short = _score("US", tf, b, ext)
                    rows[tf] = _row("US", key, tf, sc, short)
            b = _intra(root, ins["intra"], "1h")
            if b is not None and len(b) > 300:
                sc, short = _score("US", "1h", b)
                rows["1h"] = _row("US", key, "1h", sc, short)
        except Exception as e:
            errs.append(f"intraday: {str(e)[:100]}")
        try:
            d = _ydeep(root, ins["daily"])
            if d is not None:
                d = d[d.index >= "1990-01-01"]
                sc, short = _score("US", "1D", d.iloc[-2000:], {"vix": None if vix is None else vix["close"], "vix3m": None if vix3m is None else vix3m["close"]})
                rows["1D"] = _row("US", key, "1D", sc, short)
                sc, short = _score("US", "1W", _weekly(d).iloc[-500:], {"us10y": wk(us10y), "dxy": wk(dxy)})
                rows["1W"] = _row("US", key, "1W", sc, short)
        except Exception as e:
            errs.append(f"daily/weekly: {str(e)[:100]}")
        tl = [rows.get(t) for t in SM.TFS["US"] if rows.get(t)]
        out["US"][key] = {"name": ins["name"], "timeframes": tl, "alignment": {**SM.alignment({r["tf"]: r for r in tl}), "status": "context"},
                          "errors": errs}
    # ---- CRYPTO ----
    FD = _csv(root, "crypto_funding.csv")
    DV = _csv(root, "crypto_dvol.csv")
    BD = _csv(root, "binance_daily.csv")
    nq = _ydeep(root, "NQ=F")
    for key, ins in INSTR["CRYPTO"].items():
        rows, errs = {}, []
        fund = dvol = None
        try:
            if FD is not None:
                f = FD[FD["coin"] == key].copy()
                f["t"] = pd.to_datetime(f["funding_time"])
                fund = f.drop_duplicates("t", keep="last").set_index("t").sort_index()["rate"].astype(float)
            if DV is not None and key in ("BTC", "ETH"):
                v = DV[DV["currency"] == key].copy()
                v["date"] = pd.to_datetime(v["date"])
                dvol = v.drop_duplicates("date", keep="last").set_index("date").sort_index()["close"]
        except Exception as e:
            errs.append(f"derivatives: {str(e)[:80]}")
        try:
            for tf in ("5m", "15m"):
                b = _intra(root, ins["intra"], tf)
                if b is not None and len(b) > 300:
                    sc, short = _score("CRYPTO", tf, b)
                    rows[tf] = _row("CRYPTO", key, tf, sc, short)
            h1 = _intra(root, ins["intra"], "1h", "synaptic_crypto_gold.csv")
            if h1 is not None:
                b4 = h1.resample("4h").agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}).dropna()
                b4 = b4.iloc[:-1]                                   # the 4-hour bar still forming
                sc, short = _score("CRYPTO", "4h", b4, {"funding": fund})
                rows["4h"] = _row("CRYPTO", key, "4h", sc, short)
        except Exception as e:
            errs.append(f"intraday: {str(e)[:100]}")
        try:
            if key == "BTC":
                d = _ydeep(root, "BTC-USD")
            else:
                d = BD[BD["symbol"] == key + "USDT"].copy() if BD is not None else None
                if d is not None:
                    d["date"] = pd.to_datetime(d["date"])
                    d = d.drop_duplicates("date", keep="last").set_index("date").sort_index()[["open", "high", "low", "close"]]
            if d is not None and len(d) > 400:
                sc, short = _score("CRYPTO", "1D", d.iloc[-2500:], {"funding": fund, "dvol": dvol})
                rows["1D"] = _row("CRYPTO", key, "1D", sc, short)
                sc, short = _score("CRYPTO", "1W", _weekly(d).iloc[-400:], {"nq": wk(None if nq is None else nq["close"]), "dxy": wk(dxy)})
                rows["1W"] = _row("CRYPTO", key, "1W", sc, short)
        except Exception as e:
            errs.append(f"daily/weekly: {str(e)[:100]}")
        tl = [rows.get(t) for t in SM.TFS["CRYPTO"] if rows.get(t)]
        out["CRYPTO"][key] = {"name": ins["name"], "timeframes": tl, "alignment": {**SM.alignment({r["tf"]: r for r in tl}), "status": "context"},
                              "errors": errs}
    return out
