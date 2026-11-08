"""BUILDUP v1 (named by Nikhil 8 Nov; was "F&O Short Build-up v1") - PAPER ONLY (2026-11-08, Nikhil: "build a strategy around this [F&O flow] .. can we get some edge .. can take
real data feed trades"). Never places orders.

Research (scripts/research/fno_flow_research.py .. research4.py, NSE F&O bhavcopy 8 Jul 2024 - 6 Nov 2026, 578 sessions, ~280 stocks):
  * build-up classes on ordinary days, breadth, writer levels, pinning, daily long-short sorts: no edge after costs.
  * ONE effect held everywhere it was checked: a stock future that FALLS >= 2 % on the day while total futures OI RISES >= 10 %
    (fresh shorts, not longs leaving) keeps underperforming. Smooth across every threshold tried (OI 5-30 %, fall 1.5-5 %); a fall with
    flat OI does the opposite (bounces); the long-side mirror shows nothing beyond the market.
  * Rule below (picked from 27 variants of hold / stop / filter, all positive): 596 trades, +0.72 % a trade after 0.06 % costs (t 3.6),
    win 54 %, halves +0.76 / +0.68, every year positive (2024 +0.78, 2025 +0.82, 2026 +0.55).
Rule:
  signal   after the close (NSE F&O bhavcopy, same numbers the test used): most-held futures contract down >= 2 % vs its previous
           close, total futures OI (all months) up >= 10 %, close in the lower half of the day's range; |move| <= 20 % (corporate
           actions out). At most 5 a day - the biggest OI rises.
  entry    SHORT that stock's future at the next session's open (Dhan quote: day open), 1 lot
  stop     5 % above entry, checked every minute on the day's high (filled at the stop, or the open if it gaps above)
  exit     close of the 5th session (15:25 last price)
  roll     if the contract expires inside the hold, the next month is used from the start
Paper P&L: % after 0.06 % round trip, and rupees for 1 lot. Judge after ~40 trades.

BUILDUP v2 (added 8 Nov beside v1, scripts/research/fno_flow_research5.py): the same trade, but SKIP "late" shorts - the stock is already
within 1 % of its 60-session low, or it has closed below its most recent bullish order block (a down candle followed within 3 sessions by
a close >= its high + 1.5 ATR, last 60 sessions). From Oct 2024 (when 60 sessions of history exist): v1 +0.43 % a trade (t 1.9, n 424),
v2 +0.77 % (t 2.7, n 210, both halves, every year, Sharpe ~3.3, max DD ~-6 %); the skipped half made +0.10 %. Order-flow proxies from
5-min bars (VWAP, last hour, volume delta) and a 09:30 confirmation entry did not help and are not used.

2026-11-08 later (Nikhil: "we will not use v1, retire that and then we use v2"): v1 RETIRED - only signals that pass the v2 check open trades;
the others are kept in the signal list marked "skipped: v1 retired". Each signal also carries a small chart (last 40 closes of the most-held
future, chained across rolls and anchored to the signal-day close) with the bullish order block below, the bearish order block (supply) above
and the 60-day low, for the trade cards on the F&O Flow tab.
"""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import date, datetime, timedelta

import pandas as pd

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
OUT = os.path.join(ROOT, "data_store", "livedesk", "fnoshort")
HIST = os.path.join(ROOT, "data_store", "research", "fno_hist")
FALL, OI_UP, MAX_DAY, STOP, HOLD, COST = -2.0, 10.0, 5, 5.0, 5, 0.06
S: dict = {"t": 0, "busy": False, "err": None}


def _load():
    try:
        return json.load(open(os.path.join(OUT, "state.json")))
    except Exception:
        return {"signals": {}, "trades": []}


def _save(st):
    os.makedirs(OUT, exist_ok=True); p = os.path.join(OUT, "state.json"); tmp = p + ".tmp"
    json.dump(st, open(tmp, "w"), indent=1, default=str); os.replace(tmp, p)


def signals_for(day: str) -> list:
    """The rule on one session's compact bhavcopy (data_store/research/fno_hist/fut_<day>.csv.gz)."""
    f = pd.read_csv(os.path.join(HIST, f"fut_{day}.csv.gz"))
    f = f[(f.kind == "STF") & (f.close > 0)]
    tot = f.groupby("sym").agg(oi=("oi", "sum"), chg=("chg_oi", "sum"))
    live = f[f.expiry > day].sort_values("oi").groupby("sym").tail(1).set_index("sym")
    out = []
    for sym, r in live.iterrows():
        t = tot.loc[sym]; base = t.oi - t.chg
        if base <= 0 or not r.prev:
            continue
        ret = (r.close / r.prev - 1) * 100; oic = t.chg / base * 100
        clv = ((r.close - r.low) - (r.high - r.close)) / (r.high - r.low) if r.high > r.low else 0
        if ret <= FALL and oic >= OI_UP and clv < -0.5 and abs(ret) <= 20:
            out.append({"sym": sym, "day": day, "ret": round(ret, 2), "oi_chg": round(float(oic), 1), "clv": round(float(clv), 2), "close": float(r.close), "expiry": r.expiry})
    out = sorted(out, key=lambda x: -x["oi_chg"])[:MAX_DAY]
    try:
        stc = _structure(day, [x["sym"] for x in out])
        for x in out:
            late, why, ch = stc.get(x["sym"], (False, "no history", None))
            x["v2"] = not late; x["v2_note"] = f"v2 skips: {why}" if late else ("v2 takes it" + (f" ({why})" if why else "")); x["chart"] = ch
    except Exception as e:
        for x in out:
            x["v2"] = None; x["v2_note"] = f"v2 check failed: {str(e)[:60]}"
    return out


def _structure(day, syms):
    """v2 flags for the signal names on `day` from the last ~90 compact futures files: {sym: (late: bool, why: str)}."""
    import numpy as np
    fs = sorted(f for f in os.listdir(HIST) if f.startswith("fut_") and f.endswith(".csv.gz") and f[4:14] <= day)[-90:]
    F = pd.concat([pd.read_csv(os.path.join(HIST, f), usecols=["day", "sym", "kind", "open", "high", "low", "close", "prev", "oi"]) for f in fs])
    F = F[(F.kind == "STF") & F.sym.isin(syms) & (F.close > 0)].sort_values("oi").groupby(["sym", "day"]).tail(1).sort_values("day")
    out = {}
    for sym, g in F.groupby("sym"):
        g = g.copy(); g["ret"] = (g.close / g.prev - 1).where(g.prev > 0, 0)
        C = (1 + g.ret).cumprod().values * 100; O, H, L = (g[k].values / g.close.values * C for k in ("open", "high", "low"))
        tr = np.maximum(H - L, np.maximum(np.abs(H - np.roll(C, 1)), np.abs(L - np.roll(C, 1)))); tr[0] = H[0] - L[0]
        A = pd.Series(tr).rolling(14, min_periods=5).mean().values; n = len(C); i = n - 1
        if n < 61 or g.day.iloc[-1] != day:
            out[sym] = (False, "short history", None); continue
        bull = []
        for k in range(n):
            for j in (k - 1, k - 2, k - 3):
                if j >= 0 and np.isfinite(A[j]) and C[j] < O[j] and C[k] >= H[j] + 1.5 * A[j] and all(b[0] != j for b in bull):
                    bull.append((j, L[j], H[j]))
        bl = [b for b in bull if i - 60 <= b[0] < i - 1]
        low60 = C[i] <= L[-60:].min() * 1.01
        broken = bool(bl) and C[i] < bl[-1][1]
        bear = []
        for k in range(n):
            for j in (k - 1, k - 2, k - 3):
                if j >= 0 and np.isfinite(A[j]) and C[j] > O[j] and C[k] <= L[j] - 1.5 * A[j] and all(b[0] != j for b in bear):
                    bear.append((j, L[j], H[j]))
        sup = [b for b in bear if i - 60 <= b[0] < i - 1 and b[1] > C[i]]
        sc = float(g.close.iloc[-1]) / C[i]; r2 = lambda v: round(float(v) * sc, 2)
        chart = {"days": [str(d) for d in g.day.values[-40:]], "c": [r2(v) for v in C[-40:]],
                 "ob": [r2(bl[-1][1]), r2(bl[-1][2])] if bl else None, "ob_day": str(g.day.values[bl[-1][0]]) if bl else None,
                 "supply": [r2(sup[-1][1]), r2(sup[-1][2])] if sup else None, "low60": r2(L[-60:].min())}
        out[sym] = (low60 or broken, "at its 60-day low" if low60 else "closed below its last bullish order block" if broken else "", chart)
    return out


def _contract(sym, today):
    """Futures security id + lot + expiry for the hold: nearest expiry at least 8 calendar days away."""
    import fnoflow as FF
    U = FF.universe().get(sym) or {}
    lim = str(date.fromisoformat(today) + timedelta(days=8))
    for exp, sid, lot in U.get("futs", []):
        if exp >= lim:
            return exp, sid, lot
    return (U["futs"][-1] if U.get("futs") else (None, None, None))


def _evening(st):
    """After the bhavcopy is out: make sure the compact file exists (fnoflow.backfill skips files it has) and record the signals."""
    import fnoflow as FF
    d = date.today()
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    day = str(d)
    if day in st["signals"]:
        return
    FF.backfill(str(d - timedelta(days=6)))
    if os.path.exists(os.path.join(HIST, f"fut_{day}.csv.gz")):
        st["signals"][day] = signals_for(day)


def _session(st):
    """Market hours: open new trades from the latest unfilled signal day, then manage every open trade on live quotes."""
    import dhan_live as DL
    today = datetime.now().strftime("%Y-%m-%d"); hm = datetime.now().strftime("%H:%M")
    pend = [s for d, L in st["signals"].items() if d < today for s in L
            if not any(t["sym"] == s["sym"] and t["signal_day"] == d for t in st["trades"]) and not s.get("skipped")]
    for s in pend:
        if not s.get("v2"):                                          # 2026-11-08: v1 retired - only v2 signals trade
            s["skipped"] = "v1 retired - " + (s.get("v2_note") or "v2 check missing"); continue
        exp, sid, lot = _contract(s["sym"], today)
        if not sid:
            s["skipped"] = "no contract"; continue
        st["trades"].append({"sym": s["sym"], "signal_day": s["day"], "sid": sid, "expiry": exp, "lot": lot, "status": "pending", "v2": s.get("v2"), "v2_note": s.get("v2_note"), "chart": s.get("chart"), "signal_ret": s.get("ret"), "signal_oi": s.get("oi_chg"),
                             "why": f"{s['day']}: futures {s['ret']:+.2f}% with OI {s['oi_chg']:+.1f}% (fresh shorts), closed near the day's low"})
    live = [t for t in st["trades"] if t["status"] in ("pending", "open")]
    if not live:
        return
    Q = (DL.quote({"NSE_FNO": [t["sid"] for t in live]}).get("NSE_FNO") or {})
    for t in live:
        q = Q.get(str(t["sid"]))
        if not q or not str(q.get("last_trade_time", "")).startswith(datetime.now().strftime("%d/%m/%Y")):
            continue                                                 # no trade today yet (holiday / pre-open)
        o = q.get("ohlc") or {}; lp = q.get("last_price")
        if t["status"] == "pending":
            if t["signal_day"] >= today or not o.get("open"):
                continue
            e = float(o["open"])
            t.update(status="open", entry=e, entry_day=today, opened=f"{today} 09:15", stop=round(e * (1 + STOP / 100), 2), sessions=[today])
        bk = {"buy": q.get("buy_quantity"), "sell": q.get("sell_quantity")}      # 2026-11-08: forward log of the futures book (DOM filter can't be back-tested)
        if bk["buy"] or bk["sell"]:
            t.setdefault("book", {})[f"{today} {hm}"[:15] + "0"] = bk            # one sample per 10 min
        if today not in t["sessions"]:
            t["sessions"].append(today)
        hi = float(o.get("high") or 0)
        if hi >= t["stop"]:
            px = t["stop"] if float(o.get("open") or 0) < t["stop"] or len(t["sessions"]) == 1 and o.get("open") == t["entry"] else float(o["open"])
            _close(t, px, "stop 5 %"); continue
        if len(t["sessions"]) >= HOLD and hm >= "15:25":
            _close(t, float(lp), f"{HOLD} sessions"); continue
        t.setdefault("path", {})[today] = lp
        t["now"] = lp; t["pnl_open_pct"] = round((t["entry"] - lp) / t["entry"] * 100 - COST, 2)
        t["pnl_open"] = round((t["entry"] - lp - t["entry"] * COST / 100) * (t["lot"] or 0))


def _close(t, px, why):
    pct = (t["entry"] - px) / t["entry"] * 100 - COST
    t.update(status="closed", exit=round(px, 2), exit_reason=why, closed=datetime.now().strftime("%Y-%m-%d %H:%M"), pnl_pct=round(pct, 2),
             pnl=round((t["entry"] - px - t["entry"] * COST / 100) * (t["lot"] or 0)))
    for k in ("now", "pnl_open_pct", "pnl_open"):
        t.pop(k, None)


def _work():
    try:
        st = _load(); now = datetime.now(); hm = now.strftime("%H:%M")
        if now.weekday() < 5 and "09:16" <= hm <= "15:30":
            _session(st)
        if hm >= "19:05" or hm < "08:00":
            _evening(st)
        st["at"] = now.isoformat(timespec="seconds"); _save(st); S["err"] = None
    except Exception as e:
        S["err"] = f"{type(e).__name__}: {str(e)[:160]}"
    finally:
        S["busy"] = False


def tick(force=False):
    if S["busy"] or (not force and time.time() - S["t"] < 55):
        return False
    S["t"] = time.time(); S["busy"] = True
    threading.Thread(target=_work, name="fnoshort", daemon=True).start()
    return True


def trades():
    return [t for t in _load().get("trades", []) if t.get("status") in ("open", "closed")]


def api():
    st = _load()
    last = sorted(st.get("signals", {}).items())[-1] if st.get("signals") else (None, [])
    return {"version": "v2 (v1 retired 8 Nov)", "last_signal_day": last[0], "signals": last[1], "open": [t for t in st.get("trades", []) if t["status"] in ("pending", "open")],
            "closed": [t for t in st.get("trades", []) if t["status"] == "closed"][-30:], "err": S.get("err"), "at": st.get("at"),
            "name": "BuildUp v2", "rule": "v2: skip if the stock is at its 60-day low or closed below its last bullish order block. Short the stock future at the next open after a day with futures down >= 2 %, total OI up >= 10 % and a close in the lower half of "
                    "the range (max 5 a day, biggest OI rises); stop 5 %; out at the close of the 5th session. Paper only."}
