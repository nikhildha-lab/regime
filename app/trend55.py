"""TREND 55 - PAPER ONLY, RECORD-ONLY (2026-11-09, Nikhil: "yes add to metals and commodities").

Rule (MASTER_BACKTESTER/FINDINGS_stock_pattern_search.md addendum - gold, silver, copper, WTI, natgas 2012-2026 + MCX crude: 878 trades,
+0.88 % a trade after 0.10 % costs, PF 1.49 both halves, positive on all 6 markets, 11 of 15 years; shorts of breakdowns lose):
  day d (UTC session) closes ABOVE the highest high of the previous 55 sessions  ->  LONG at the next session's open
  stop  = entry - 1 x ATR(14, daily, Wilder)   checked on every later bar (a gap below fills at the open)
  no target; out at the close of the 20th session.   Risk $100 a trade (units = 100 / ATR), costs 0.10 % round trip.
Markets: GOLD GC=F, SILVER SI=F, COPPER HG=F, ALUMINUM ALI=F, CRUDE CL=F, NATGAS NG=F - Yahoo 1h bars in data_store/market_data/
synaptic_intraday_all.csv (refreshed by the app), resampled to UTC sessions. One position per market. Never places orders.
State: data_store/livedesk/trend55/state.json.
"""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timezone

import numpy as np
import pandas as pd

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
OUT = os.path.join(ROOT, "data_store", "livedesk", "trend55")
INTRA_CSV = os.path.join(ROOT, "data_store", "market_data", "synaptic_intraday_all.csv")
GOLD_CSV = os.path.join(ROOT, "data_store", "market_data", "synaptic_crypto_gold.csv")
MKTS = {"GOLD": "GC=F", "SILVER": "SI=F", "COPPER": "HG=F", "ALUMINUM": "ALI=F", "CRUDE": "CL=F", "NATGAS": "NG=F"}
LOOK, STOP_ATR, HOLD, COST, RISK = 55, 1.0, 20, 0.10, 100.0
S: dict = {"t": 0, "busy": False, "err": None}
_C: dict = {}


def _load():
    try:
        return json.load(open(os.path.join(OUT, "state.json")))
    except Exception:
        return {"trades": [], "signals": {}}


def _save(st):
    os.makedirs(OUT, exist_ok=True); p = os.path.join(OUT, "state.json"); tmp = p + ".tmp"
    json.dump(st, open(tmp, "w"), indent=1, default=str); os.replace(tmp, p)


def _hourly():
    mt = os.path.getmtime(INTRA_CSV) + (os.path.getmtime(GOLD_CSV) if os.path.exists(GOLD_CSV) else 0)
    c = _C.get("h")
    if c and c[0] == mt:
        return c[1]
    cols = ["datetime_utc", "symbol", "interval", "open", "high", "low", "close", "volume"]
    g = pd.read_csv(INTRA_CSV, usecols=cols)
    try:                                                              # gold's 1h bars live in their own file (as app/break4h.py reads them)
        g = pd.concat([g, pd.read_csv(GOLD_CSV, usecols=cols)], ignore_index=True)
    except Exception:
        pass
    g = g[(g.interval == "1h") & g.symbol.isin(MKTS.values())].copy()
    g["t"] = pd.to_datetime(g.datetime_utc); g = g.sort_values("t")
    out = {}
    for mk, ys in MKTS.items():
        x = g[g.symbol == ys].set_index("t")[["open", "high", "low", "close", "volume"]].astype(float)
        x = x[~x.index.duplicated(keep="last")]
        out[mk] = x
    _C["h"] = (mt, out)
    return out


def daily(mk):
    """UTC-session bars from the 1h file; the last row may be the unfinished session."""
    h = _hourly().get(mk)
    if h is None or h.empty:
        return None
    d = h.resample("1D").agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}).dropna(subset=["close"])
    d["n"] = h.close.resample("1D").count().reindex(d.index)
    pc = d.close.shift(1); tr = np.maximum(d.high - d.low, np.maximum((d.high - pc).abs(), (d.low - pc).abs()))
    d["atr"] = tr.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    d["hh"] = d.high.shift(1).rolling(LOOK, min_periods=LOOK).max()
    d["day"] = d.index.strftime("%Y-%m-%d")
    return d, h


def _work():
    try:
        st = _load(); now = datetime.now(timezone.utc); today = now.strftime("%Y-%m-%d")
        for mk in MKTS:
            r = daily(mk)
            if r is None:
                continue
            d, h = r
            done = d[d.day < today]                                  # finished sessions only
            if len(done) < LOOK + 15:
                continue
            last = done.iloc[-1]
            # signal on the last finished session
            if last.close > last.hh and np.isfinite(last.atr):
                sig = st["signals"].setdefault(mk, {})
                if last.day not in sig:
                    sig[last.day] = {"close": float(last.close), "hh": float(last.hh), "atr": float(last.atr), "at": now.isoformat(timespec="seconds")}
                    if not any(t["mkt"] == mk and t["status"] in ("pending", "open") for t in st["trades"]):
                        st["trades"].append({"mkt": mk, "signal_day": last.day, "status": "pending", "atr": float(last.atr),
                                             "why": f"{last.day}: closed {last.close:.4g} above the 55-session high {last.hh:.4g} (trend breakout)"})
            # manage
            for t in [t for t in st["trades"] if t["mkt"] == mk and t["status"] in ("pending", "open")]:
                after = h[h.index > pd.Timestamp(t["signal_day"]) + pd.Timedelta(days=1) - pd.Timedelta(seconds=1)]
                if after.empty:
                    continue
                if t["status"] == "pending":
                    e = float(after.open.iloc[0]); risk = STOP_ATR * t["atr"]
                    t.update(status="open", entry=e, stop=round(e - risk, 6), opened=str(after.index[0]), units=RISK / risk, sessions=[after.index[0].strftime("%Y-%m-%d")])
                bars = after[after.index >= pd.Timestamp(t["opened"])]
                for ts, b in bars.iterrows():
                    dday = ts.strftime("%Y-%m-%d")
                    if dday not in t["sessions"]:
                        if len(t["sessions"]) >= HOLD:                  # the 20th session is over -> out at its close
                            prev_close = float(bars[bars.index < ts].close.iloc[-1])
                            _close(t, prev_close, f"{HOLD} sessions", str(bars[bars.index < ts].index[-1])); break
                        t["sessions"].append(dday)
                    if b.low <= t["stop"]:
                        px = min(t["stop"], float(b.open))
                        _close(t, px, f"stop {STOP_ATR:g} ATR", str(ts)); break
                if t["status"] == "open":
                    lp = float(bars.close.iloc[-1]); t["now"] = lp
                    t["pnl_open_pct"] = round((lp / t["entry"] - 1) * 100 - COST, 3); t["pnl_open"] = round((lp - t["entry"] - t["entry"] * COST / 100) * t["units"], 2)
        st["at"] = now.isoformat(timespec="seconds"); _save(st); S["err"] = None
    except Exception as e:
        S["err"] = f"{type(e).__name__}: {str(e)[:160]}"
    finally:
        S["busy"] = False


def _close(t, px, why, when):
    pct = (px / t["entry"] - 1) * 100 - COST
    t.update(status="closed", exit=round(px, 6), exit_reason=why, closed=when, pnl_pct=round(pct, 3), pnl=round((px - t["entry"] - t["entry"] * COST / 100) * t["units"], 2),
             r=round(pct / ((t["entry"] - t["stop"]) / t["entry"] * 100), 2))
    for k in ("now", "pnl_open_pct", "pnl_open"):
        t.pop(k, None)


def tick(force=False):
    if S["busy"] or (not force and time.time() - S["t"] < 290):
        return False
    S["t"] = time.time(); S["busy"] = True
    threading.Thread(target=_work, name="trend55", daemon=True).start()
    return True


def trades():
    return [t for t in _load().get("trades", []) if t.get("status") in ("open", "closed")]


def api():
    st = _load()
    return {"name": "Trend 55", "record_only": True, "open": [t for t in st.get("trades", []) if t["status"] in ("pending", "open")],
            "closed": [t for t in st.get("trades", []) if t["status"] == "closed"][-30:], "signals": st.get("signals", {}), "err": S.get("err"), "at": st.get("at"),
            "rule": "Long at the next session's open after a UTC session closes above the highest high of the previous 55 sessions; stop 1 ATR(14) below entry; no target; out at the close of the 20th session. $100 risk a trade. Record only."}
