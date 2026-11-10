"""STOCKS IN PLAY ORB - PAPER ONLY (2026-11-05, Nikhil: "a good way to handle Indian stocks for intraday ... MIS 5x ... order flow").
Never places orders. Rule = S1 in scripts/research/stock_intraday_tests.py, fixed before that test:
  universe : the 48 F&O stocks on the order-flow engine's book feed (app/stockhist.STOCKS)
  in play  : 09:15-09:30 opening-range volume >= 2 x its own 20-day average AND opening gap >= 0.5 % (direction = gap direction)
  entry    : first 5-min close beyond the opening range in the gap direction, 09:30-11:30 (one trade per stock per day)
  stop     : the other side of the opening range or 1 x ATR(14, 5-min), whichever is closer
  exits    : half off at 1.5R and stop to entry; the rest exits on a 5-min close back through VWAP; everything flat at 15:10
  limits   : max 3 trades a day - the first 3 signals (same-minute ties go to the highest opening-range volume)
  money    : MIS 5x paper - Rs 20,000 margin x 5 = Rs 1,00,000 position; costs 0.12 % round trip (charges ~0.08 % + slippage)
  record   : the 20-level book lean at entry (Dhan order-flow engine) is saved; 'book agrees' (imbalance >= +0.10 in the trade
             direction) is a RECORD-ONLY variant, untested - judged later.
History (8 Jul - 4 Nov 2026, 48 stocks, 1-min Dhan data): 61 trades, +0.165 % gross / +0.045 % net a trade (about +Rs 45 on Rs 1 lakh),
halves -0.04 / +0.13 - a small, unproven lead. Judge after ~60 live trades.
Data: 1-min bars from the Mac's Dhan login (dhan_live.intraday): all 48 stocks once at 09:31, then only the in-play stocks each minute.
"""
from __future__ import annotations

import json
import os
import time
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
OUT = os.path.join(ROOT, "data_store", "livedesk", "stockorb")
HIST = os.path.join(ROOT, "data_store", "research", "stocks_intraday")
OF_LIVE = os.path.join(os.environ.get("MP_DHAN_DIR") or os.path.join(os.path.expanduser("~"), "Documents", "dhan"), "orderflow", "data", "of", "live.json")
RV_MIN, GAP_MIN, R_HALF, MAX_DAY, COST_PCT = 2.0, 0.005, 1.5, 3, 0.12
DI_FILTER = True          # 2026-11-09: skip the entry when ADX(14, 5-min) > 25 and the DI is against the trade (app/difilter.py)
TOPVOL = True             # 2026-11-09: trade only the day's top 10 tracked F&O stocks by 09:15-09:30 traded value (app/topvol.py)
MARGIN, LEV = 20000, 5
POS = MARGIN * LEV
S: dict = {"t": 0}


def _p(day):
    return os.path.join(OUT, f"day_{day}.json")


def _load(day):
    try:
        return json.load(open(_p(day)))
    except Exception:
        return {"day": day, "in_play": None, "trades": [], "orv": {}}


def _save(day, st):
    os.makedirs(OUT, exist_ok=True)
    tmp = _p(day) + f".{os.getpid()}.tmp"
    json.dump(st, open(tmp, "w"), indent=1, default=str)
    os.replace(tmp, _p(day))


def _orv_history(sym, day):
    """Opening-range (09:15-09:30) volume of the previous 20 sessions: downloaded history + days this module saved."""
    vals = {}
    try:
        d = pd.read_csv(os.path.join(HIST, f"{sym}_1m.csv.gz"), parse_dates=["ts"])
        d = d[(d.ts.dt.strftime("%H:%M") < "09:30") & (d.ts.dt.strftime("%Y-%m-%d") < day)]
        vals.update(d.groupby(d.ts.dt.strftime("%Y-%m-%d")).volume.sum().to_dict())
    except Exception:
        pass
    for f in sorted(x for x in os.listdir(OUT) if x.startswith("day_")) if os.path.isdir(OUT) else []:
        dd = f[4:14]
        if dd < day:
            try:
                v = json.load(open(os.path.join(OUT, f))).get("orv", {}).get(sym)
                if v:
                    vals[dd] = v
            except Exception:
                pass
    last = [vals[k] for k in sorted(vals)[-20:]]
    return (float(np.mean(last)), len(last)) if last else (None, 0)


def _bars1(sym, day):
    import dhan_live as DL
    sid = DL.ids_equity(sym)
    b = DL.intraday("NSE_EQ", sid, "EQUITY", date.fromisoformat(day), date.fromisoformat(day), 1, oi=False) if sid else []
    if not b:
        return pd.DataFrame()
    d = pd.DataFrame(b); d["ts"] = pd.to_datetime(d.ts.astype(str).str[:19])
    d = d[d.ts.dt.strftime("%Y-%m-%d") == day]
    now_m = datetime.now().strftime("%H:%M")
    return d[d.ts.dt.strftime("%H:%M") < now_m].set_index("ts").sort_index()   # closed minutes only


_PREV: dict = {}


def _prev1(sym, day):
    """1-min bars of the previous session (ATR runs across days, as in the history test). Downloaded history if it ends within
    4 days before `day`, else the Mac's Dhan login (last 7 days)."""
    k = (sym, day)
    if k in _PREV:
        return _PREV[k]
    out = pd.DataFrame()
    try:
        d = pd.read_csv(os.path.join(HIST, f"{sym}_1m.csv.gz"), parse_dates=["ts"])
        d = d[d.ts.dt.strftime("%Y-%m-%d") < day]
        if len(d) and (pd.Timestamp(day) - d.ts.iloc[-1].normalize()).days <= 4:
            last = d.ts.iloc[-1].normalize(); out = d[d.ts.dt.normalize() == last].set_index("ts")
    except Exception:
        pass
    if not len(out):
        try:
            import dhan_live as DL
            sid = DL.ids_equity(sym)
            b = DL.intraday("NSE_EQ", sid, "EQUITY", date.fromisoformat(day) - timedelta(days=7), date.fromisoformat(day) - timedelta(days=1), 1, oi=False)
            d = pd.DataFrame(b); d["ts"] = pd.to_datetime(d.ts.astype(str).str[:19]); last = d.ts.iloc[-1].normalize()
            out = d[d.ts.dt.normalize() == last].set_index("ts")[["open", "high", "low", "close", "volume"]]
        except Exception:
            out = pd.DataFrame()
    _PREV[k] = out
    return out


def _five(d1, sym=None, day=None):
    """Closed 5-min bars of today with VWAP (today only) and ATR(14) computed across the previous session too."""
    def agg(x):
        return x.resample("5min", origin="start_day", offset="9h15min", label="left", closed="left").agg(
            {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}).dropna()
    b = agg(d1)
    b = b[b.index + pd.Timedelta(minutes=5) <= pd.Timestamp(datetime.now().replace(second=0, microsecond=0))]   # closed 5-min bars
    p = agg(_prev1(sym, day)) if sym else pd.DataFrame()
    allb = pd.concat([p, b]) if len(p) else b
    tr = np.maximum(allb.high - allb.low, np.maximum((allb.high - allb.close.shift()).abs(), (allb.low - allb.close.shift()).abs()))
    b = b.copy(); b["atr"] = tr.rolling(14).mean().reindex(b.index)
    tp = (b.high + b.low + b.close) / 3
    b["vwap"] = (tp * b.volume).cumsum() / b.volume.cumsum()
    b["hm"] = b.index.strftime("%H:%M")
    b.attrs["allb"] = allb                                   # 2026-11-09: previous session + today, for the DI filter at entry
    return b


def _prev_close(sym, day):
    try:
        d = pd.read_csv(os.path.join(HIST, f"{sym}_1m.csv.gz"), parse_dates=["ts"])
        d = d[d.ts.dt.strftime("%Y-%m-%d") < day]
        return float(d.close.iloc[-1]) if len(d) else None
    except Exception:
        return None


def _book_lean(sym, side):
    try:
        L = json.load(open(OF_LIVE)); B = (L.get("stocks") or {}).get(sym)
        qb, qa = sum(r[1] for r in B["bids"]), sum(r[1] for r in B["asks"])
        return round(side * (qb - qa) / (qb + qa), 3) if qb + qa else None
    except Exception:
        return None


def _scan(day, st):
    """At 09:31: who is in play today."""
    from stockhist import STOCKS
    rows = []
    for sym in STOCKS:
        try:
            d1 = _bars1(sym, day); time.sleep(0.3)
        except Exception:
            continue
        o = d1[d1.index.strftime("%H:%M") < "09:30"]
        if not len(o):
            continue
        st["orv"][sym] = float(o.volume.sum())
        st.setdefault("open_value", {})[sym] = float((o.close * o.volume).sum())     # 2026-11-09: for the top-volume universe (app/topvol.py)
        avg, n = _orv_history(sym, day); pc = _prev_close(sym, day)
        if not avg or n < 10 or not pc:
            continue
        rv = st["orv"][sym] / avg; gap = o.open.iloc[0] / pc - 1
        if rv >= RV_MIN and abs(gap) >= GAP_MIN:
            rows.append({"sym": sym, "rv": round(rv, 2), "gap_pct": round(gap * 100, 2), "side": 1 if gap > 0 else -1,
                         "or_hi": float(o.high.max()), "or_lo": float(o.low.min())})
    st["in_play"] = sorted(rows, key=lambda r: -r["rv"])
    st["scanned_at"] = datetime.now().strftime("%H:%M")
    try:                                                     # 2026-11-09 (Nikhil: "only top F&O stocks by volume for the day, max 10"): rank once, shared by every intraday stock rule
        import topvol as TV
        TV.write(day, st.get("open_value") or {}, st["scanned_at"])
    except Exception as e:
        st["err"] = f"topvol: {str(e)[:100]}"


def _manage(tr, b):
    s = tr["s"]; e = tr["entry"]; R = abs(e - tr["stop"])
    after = b[b.index > pd.Timestamp(tr["bar"])]
    left, pnl, cur = 1.0, 0.0, tr["stop"]
    for t_, x in after.iterrows():
        if x.hm >= "15:10":
            return _close(tr, pnl, left, x.close, "15:10", t_)
        if (s > 0 and x.low <= cur) or (s < 0 and x.high >= cur):
            return _close(tr, pnl, left, cur, "stop" if left == 1.0 else "breakeven", t_)
        if left == 1.0 and ((s > 0 and x.high >= e + R_HALF * R) or (s < 0 and x.low <= e - R_HALF * R)):
            pnl += 0.5 * R_HALF * R; left = 0.5; cur = e; tr["half_booked"] = str(t_)
        if left == 0.5 and s * (x.close - x.vwap) < 0:
            return _close(tr, pnl, left, x.close, "VWAP", t_)
    if len(b) and datetime.now().strftime("%H:%M") >= "15:15":   # 15:10 bar never came (short data day) - flat at the last close, like the test
        return _close(tr, pnl, left, float(b.close.iloc[-1]), "15:10", b.index[-1])
    tr["now"] = float(b.close.iloc[-1]) if len(b) else None
    tr["pnl_open"] = round(POS * ((pnl + left * s * ((tr["now"] or e) - e)) / e), 0) if tr["now"] else None
    return False


def _close(tr, pnl, left, px, why, t_):
    pts = pnl + left * tr["s"] * (px - tr["entry"])
    g = pts / tr["entry"] * 100
    tr.update(status="closed", exit=round(float(px), 2), exit_reason=why, closed=str(t_)[:16], gross_pct=round(g, 3),
              net_pct=round(g - COST_PCT, 3), pnl=round(POS * (g - COST_PCT) / 100), pnl_margin_pct=round(LEV * (g - COST_PCT), 2))
    return True


def tick(force=False) -> bool:
    """Called from the live-desk loop; the work runs in its own thread (the 09:31 scan makes ~48 data calls)."""
    now = datetime.now(); hm = now.strftime("%H:%M")
    if S.get("busy") or (not force and (time.time() - S["t"] < 60 or now.weekday() >= 5 or not ("09:31" <= hm <= "15:15"))):
        return False
    import nsecal as NSEC
    if not force and not NSEC.session_ok():                 # 2026-11-10 (Nikhil): no Indian paper trades on NSE holidays / frozen chains
        return False
    S["t"] = time.time(); S["busy"] = True
    import threading
    threading.Thread(target=_work, name="stockorb", daemon=True).start()
    return True


def _work():
    try:
        _work_inner()
    except Exception as e:
        S["err"] = str(e)[:160]
    finally:
        S["busy"] = False


def _work_inner():
    now = datetime.now(); hm = now.strftime("%H:%M")
    day = now.strftime("%Y-%m-%d"); st = _load(day)
    if st.get("in_play") is None:
        _scan(day, st); _save(day, st)
    taken = {t["sym"] for t in st["trades"]}
    for c in st["in_play"] or []:
        sym = c["sym"]; open_tr = next((t for t in st["trades"] if t["sym"] == sym and t["status"] == "open"), None)
        if sym in taken and not open_tr:
            continue
        if not open_tr and (len(st["trades"]) >= MAX_DAY or hm > "11:35"):
            continue
        if not open_tr and TOPVOL:                             # 2026-11-09: day's universe = top 10 by opening traded value
            import topvol as TV
            ok, note = TV.allowed(sym, day)
            if not ok:
                if sym not in {x["sym"] for x in st.get("skipped", [])}:
                    st.setdefault("skipped", []).append({"sym": sym, "why": note}); taken.add(sym)
                continue
        try:
            b = _five(_bars1(sym, day), sym, day); time.sleep(0.2)
        except Exception as e:
            st["err"] = f"{sym}: {str(e)[:100]}"; continue
        if not len(b):
            continue
        if open_tr:
            _manage(open_tr, b); continue
        s = c["side"]
        w = b[(b.hm >= "09:30") & (b.hm <= "11:30")]
        hit = w[(w.close > c["or_hi"]) if s > 0 else (w.close < c["or_lo"])]
        if not len(hit):
            continue
        t0 = hit.index[0]
        if t0 < b.index[-1] - pd.Timedelta(minutes=10):        # signal bar is old (e.g. after a restart) - never enter at a stale price
            st.setdefault("missed", []).append({"sym": sym, "bar": str(t0)}); taken.add(sym); continue
        x = b.loc[t0]; e = float(x.close); a = float(x.atr) if np.isfinite(x.atr) else None
        st_or = c["or_lo"] if s > 0 else c["or_hi"]; st_atr = e - s * a if a else st_or
        stop = st_or if abs(e - st_or) < abs(e - st_atr) else st_atr
        if s * (e - stop) <= 0:
            taken.add(sym); continue
        di = None
        if DI_FILTER:                                          # 2026-11-09 (Nikhil: "update the logic"): no entry against a trending 5-min DI
            try:
                import difilter as DF
                allb = b.attrs.get("allb"); di = DF.check_bars("long" if s > 0 else "short", allb[allb.index < t0] if allb is not None else None)
            except Exception as ex:
                di = {"ok": True, "note": f"DI check failed ({type(ex).__name__}) - allowed"}
            if not di["ok"]:
                st.setdefault("skipped", []).append({"sym": sym, "bar": str(t0), "side": "long" if s > 0 else "short", "di": di["note"]}); taken.add(sym); continue
        ln = _book_lean(sym, s)
        tr = {"sym": sym, "day": day, "s": s, "side": "long" if s > 0 else "short", "bar": str(t0), "opened": f"{day} {t0:%H:%M}",
              "entry": round(e, 2), "stop": round(stop, 2), "risk_pct": round(abs(e - stop) / e * 100, 2), "rv": c["rv"], "gap_pct": c["gap_pct"],
              "or": [c["or_lo"], c["or_hi"]], "book_lean": ln, "book_agrees": ln is not None and ln >= 0.10, "status": "open",
              "qty": int(POS // e), "margin": MARGIN, "leverage": LEV,
              "why": f"in play: opening volume {c['rv']}x normal, gap {c['gap_pct']:+.2f}% - 5-min close beyond the opening range", "di": (di or {}).get("note")}
        st["trades"].append(tr); taken.add(sym)
        _manage(tr, b)
    st["at"] = now.isoformat(timespec="seconds"); _save(day, st)


def trades(days=40):
    files = sorted(f for f in os.listdir(OUT) if f.startswith("day_") and f.endswith(".json")) if os.path.isdir(OUT) else []
    out = []
    for f in files[-days:]:
        try:
            out += json.load(open(os.path.join(OUT, f))).get("trades", [])
        except Exception:
            pass
    return out
