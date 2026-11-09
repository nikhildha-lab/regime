"""BREAKFADE - PAPER ONLY, RECORD-ONLY (2026-11-09, Nikhil: "ok add both" after the 132k-experiment stock pattern search).

Rule (fixed from MASTER_BACKTESTER/FINDINGS_stock_pattern_search.md - PROVISIONAL, so this is a forward RECORD, no plan account):
  Signal day d (from the evening bhavcopy, data_store/research/fno_hist/fut_<d>.csv.gz, near-month = largest-OI contract):
     close > the highest HIGH of the previous 20 sessions (a 20-day breakout)
     total futures volume >= 1.5 x the median of the previous 20 sessions
     near-month OI up >= 2 % vs the previous session, same contract (fresh longs into the breakout)
     at most 3 names a day (largest OI rise first); skip names in the F&O ban list
  Trade: SHORT the stock future at the NEXT session's open, 1 lot.
     stop   = entry + 1 ATR(14) (daily, Wilder, the contract series) - checked on each day's high; a gap above fills at the open
     target = entry - 3 x (stop - entry)                            - checked on each day's low;  a gap below fills at the open
     time   = out at the close of the 10th session
  Costs 0.10 % round trip.  Backtest (Jul 2024 - Nov 2026, 753 trades at 3 a day): +0.51 % a trade, PF 1.30, 2024 +1.2 %, 2025 flat,
  2026 +0.9 %; the train-only pick made just +0.06 % out of sample, hence record-only. Judge after 60 closed trades.
Same plumbing as app/fnoshort.py (BuildUp v2): state in data_store/livedesk/fnobreakfade/state.json, evening scan, live quotes by day.
"""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
OUT = os.path.join(ROOT, "data_store", "livedesk", "fnobreakfade")
HIST = os.path.join(ROOT, "data_store", "research", "fno_hist")
LOOK, RVOL, OI_UP, MAX_DAY, STOP_ATR, TARGET_R, HOLD, COST = 20, 1.5, 2.0, 3, 1.0, 3.0, 10, 0.10
INDEX = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50", "SENSEX", "BANKEX"}
S: dict = {"t": 0, "busy": False, "err": None}


def _load():
    try:
        return json.load(open(os.path.join(OUT, "state.json")))
    except Exception:
        return {"signals": {}, "trades": []}


def _save(st):
    os.makedirs(OUT, exist_ok=True); p = os.path.join(OUT, "state.json"); tmp = p + ".tmp"
    json.dump(st, open(tmp, "w"), indent=1, default=str); os.replace(tmp, p)


def _panel(day, n=40):
    """near-month (largest OI) stock-futures rows for the last n sessions up to `day`, with total volume and same-contract OI change."""
    fs = sorted(f for f in os.listdir(HIST) if f.startswith("fut_") and f.endswith(".csv.gz") and f[4:14] <= day)[-n:]
    F = pd.concat([pd.read_csv(os.path.join(HIST, f), usecols=["day", "sym", "kind", "expiry", "open", "high", "low", "close", "prev", "oi", "vol"]) for f in fs])
    F = F[(F.kind == "STF") & ~F.sym.isin(INDEX) & (F.close > 0)]
    tot = F.groupby(["day", "sym"]).vol.sum().rename("vol_tot").reset_index()
    N = F.sort_values("oi").groupby(["day", "sym"]).tail(1).merge(tot, on=["day", "sym"]).sort_values(["sym", "day"]).reset_index(drop=True)
    g = N.groupby("sym")
    N["oi_chg"] = np.where(N.expiry == g.expiry.shift(1), (N.oi / g.oi.shift(1) - 1) * 100, np.nan)
    N["hh20"] = g.high.transform(lambda x: x.shift(1).rolling(LOOK, min_periods=LOOK // 2).max())
    N["vmed"] = g.vol_tot.transform(lambda x: x.shift(1).rolling(LOOK, min_periods=10).median())
    pc = g.close.shift(1); tr = np.maximum(N.high - N.low, np.maximum((N.high - pc).abs(), (N.low - pc).abs()))
    N["atr"] = tr.groupby(N.sym).transform(lambda x: x.ewm(alpha=1 / 14, adjust=False, min_periods=10).mean())
    return N


def signals_for(day: str) -> list:
    N = _panel(day); x = N[N.day == day]
    x = x[(x.close > x.hh20) & (x.vol_tot >= RVOL * x.vmed) & (x.oi_chg >= OI_UP) & x.atr.notna() & (x.expiry > day)]
    out = [{"sym": r.sym, "day": day, "close": float(r.close), "hh20": float(r.hh20), "rvol": round(float(r.vol_tot / r.vmed), 2), "oi_chg": round(float(r.oi_chg), 1),
            "atr": round(float(r.atr), 2), "expiry": r.expiry} for r in x.itertuples()]
    return sorted(out, key=lambda s: -s["oi_chg"])[:MAX_DAY]


def _contract(sym, today):
    import fnoflow as FF
    U = FF.universe().get(sym) or {}
    lim = str(date.fromisoformat(today) + timedelta(days=12))
    for exp, sid, lot in U.get("futs", []):
        if exp >= lim:
            return exp, sid, lot
    return (U["futs"][-1] if U.get("futs") else (None, None, None))


def _evening(st):
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
    import dhan_live as DL
    today = datetime.now().strftime("%Y-%m-%d"); hm = datetime.now().strftime("%H:%M")
    pend = [s for d, L in st["signals"].items() if d < today for s in L
            if not any(t["sym"] == s["sym"] and t["signal_day"] == d for t in st["trades"]) and not s.get("skipped")]
    try:
        import optlab as OL
        banned = set((OL.ban() or {}).get("syms") or [])
    except Exception:
        banned = set()
    for s in pend:
        if (date.fromisoformat(today) - date.fromisoformat(s["day"])).days > 4:
            s["skipped"] = "signal too old (no session to enter)"; continue
        if s["sym"] in banned:
            s["skipped"] = "in the NSE F&O ban list on the entry day"; continue
        exp, sid, lot = _contract(s["sym"], today)
        if not sid:
            s["skipped"] = "no contract"; continue
        st["trades"].append({"sym": s["sym"], "signal_day": s["day"], "sid": sid, "expiry": exp, "lot": lot, "status": "pending", "atr": s["atr"],
                             "why": f"{s['day']}: closed above its 20-day high ({s['hh20']:.2f}) on {s['rvol']:.1f}x volume with OI {s['oi_chg']:+.1f}% (fresh longs chasing the breakout)"})
    live = [t for t in st["trades"] if t["status"] in ("pending", "open")]
    if not live:
        return
    Q = (DL.quote({"NSE_FNO": [t["sid"] for t in live]}).get("NSE_FNO") or {})
    for t in live:
        q = Q.get(str(t["sid"]))
        if not q or not str(q.get("last_trade_time", "")).startswith(datetime.now().strftime("%d/%m/%Y")):
            continue
        o = q.get("ohlc") or {}; lp = q.get("last_price")
        if t["status"] == "pending":
            if t["signal_day"] >= today or not o.get("open"):
                continue
            e = float(o["open"]); risk = STOP_ATR * float(t["atr"])
            t.update(status="open", entry=e, entry_day=today, opened=f"{today} 09:15", stop=round(e + risk, 2), target=round(e - TARGET_R * risk, 2), sessions=[today])
        if today not in t["sessions"]:
            t["sessions"].append(today)
        op = float(o.get("open") or 0); hi = float(o.get("high") or 0); lo = float(o.get("low") or 0)
        first = len(t["sessions"]) == 1
        if hi >= t["stop"]:
            px = t["stop"] if (first or op < t["stop"]) else op
            _close(t, px, f"stop {STOP_ATR:g} ATR"); continue
        if lo and lo <= t["target"]:
            px = t["target"] if (first or op > t["target"]) else op
            _close(t, px, f"target {TARGET_R:g} R"); continue
        if len(t["sessions"]) >= HOLD and hm >= "15:25":
            _close(t, float(lp), f"{HOLD} sessions"); continue
        t.setdefault("path", {})[today] = lp
        t["now"] = lp; t["pnl_open_pct"] = round((t["entry"] - lp) / t["entry"] * 100 - COST, 2)
        t["pnl_open"] = round((t["entry"] - lp - t["entry"] * COST / 100) * (t["lot"] or 0))


def _close(t, px, why):
    pct = (t["entry"] - px) / t["entry"] * 100 - COST
    t.update(status="closed", exit=round(px, 2), exit_reason=why, closed=datetime.now().strftime("%Y-%m-%d %H:%M"), pnl_pct=round(pct, 2),
             pnl=round((t["entry"] - px - t["entry"] * COST / 100) * (t["lot"] or 0)), r=round(pct / ((t["stop"] - t["entry"]) / t["entry"] * 100), 2))
    for k in ("now", "pnl_open_pct", "pnl_open"):
        t.pop(k, None)


def _work():
    try:
        st = _load(); now = datetime.now(); hm = now.strftime("%H:%M")
        if now.weekday() < 5 and "09:16" <= hm <= "15:30":
            _session(st)
        if hm >= "19:10" or hm < "08:00":
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
    threading.Thread(target=_work, name="fnobreakfade", daemon=True).start()
    return True


def trades():
    return [t for t in _load().get("trades", []) if t.get("status") in ("open", "closed")]


def api():
    st = _load()
    last = sorted(st.get("signals", {}).items())[-1] if st.get("signals") else (None, [])
    return {"name": "BreakFade", "record_only": True, "last_signal_day": last[0], "signals": last[1],
            "open": [t for t in st.get("trades", []) if t["status"] in ("pending", "open")], "closed": [t for t in st.get("trades", []) if t["status"] == "closed"][-30:],
            "err": S.get("err"), "at": st.get("at"),
            "rule": "Short the stock future at the next open after a close above the 20-day high on >= 1.5x volume with near-month OI up >= 2 % (max 3 a day); "
                    "stop 1 ATR above, target 3 R, out at the close of the 10th session. Record only - no plan account."}
