"""PAPER TRADE CHARTS (2026-11-06, Nikhil: "i need new section for strategies paper trade with charts").

One small price chart per paper trade: candles around the trade (from 30 min before the entry to 30 min after the exit), with its
entry, stop, target(s) and exit. Read-only - it never changes a trade. Paper only, never orders.

Candles come from the same price paths as the trade review (app/tradereview.py):
    Nifty / Bank Nifty : order-flow engine 1-second FUTURES mid -> 1-min (5-min when the window is longer than 4 h) candles
    NSE stocks          : 20-level depth mid -> 1-min / 5-min candles
    crypto / metals / FX / commodities : stored 15-min footprint bars (1 h when the window is longer than 3 days)
Index rules priced on SPOT (Touch v2, overnight, vol cycle) are drawn on the futures candles with their lines moved by the gap
between the futures path and the trade's own entry price at the entry minute ("off").
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))
SPOT_RULES = {"touchv2", "ovn_h30", "ovn_h30f", "vcy_night", "vcy_day", "vcy_day_sk", "vcy_day_bw", "vcy_day_dh"}
TAG = {"dom_ix": "DOM", "dom_ix_break": "DOM-B", "dom_ix_hold": "DOM-H", "dom_stocks": "DOM-S", "touchv2": "Touch v2", "sorb": "ORB", "svb": "SVB", "fsb": "BuildUp v1", "fsb2": "BuildUp v2", "istr_n": "Nifty strangle", "icon_s": "Sensex condor", "svb_tr": "SVB trail", "ovn_h30": "Overnight", "ovn_h30f": "Overnight+flow", "vcy_night": "Vol night",
       "vcy_day": "Vol day", "vcy_day_sk": "Vol skew", "vcy_day_bw": "Vol b-wing", "vcy_day_dh": "Vol hedged", "b4h": "4H break",
       "b4h_v4": "4H v4", "b4h_h2": "4H H2", "b4h_g2": "Gold v2"}
_C: dict = {}
_API: dict = {}


def _ep(s):
    try:
        return datetime.fromisoformat(str(s)[:16]).replace(tzinfo=IST).timestamp()
    except ValueError:
        return None


def _agg(rows, step):
    """[(t, price)] -> [[t, o, h, l, c]] buckets of `step` seconds."""
    out, cur = [], None
    for t, p in rows:
        b = int(t // step * step)
        if cur is None or cur[0] != b:
            if cur:
                out.append(cur)
            cur = [b, p, p, p, p]
        else:
            cur[2] = max(cur[2], p); cur[3] = min(cur[3], p); cur[4] = p
    if cur:
        out.append(cur)
    return out


def candles(hot, key, t0, t1):
    """(candles, step_seconds, kind) for the window [t0, t1]."""
    D = hot("domcards"); TR = hot("tradereview")
    days = sorted({datetime.fromtimestamp(x, IST).strftime("%Y-%m-%d") for x in (t0, t1)})
    ticks = []
    for d in days:
        try:
            ts, px = D._path(d, key)
        except Exception:
            ts, px = [], []
        ticks += [(t, p) for t, p in zip(ts, px) if t0 <= t <= t1]
    if ticks:
        step = 300 if t1 - t0 > 4 * 3600 else 60
        return _agg(ticks, step), step, "tick"
    bars = TR._fp_bars(key, t0, t1)
    if bars:
        if t1 - t0 > 3 * 86400:                       # long 4H-break trades: merge into 1 h candles
            hb = []
            for b in bars:
                k = int(b[0] // 3600 * 3600)
                if hb and hb[-1][0] == k:
                    hb[-1][2] = max(hb[-1][2], b[2]); hb[-1][3] = min(hb[-1][3], b[3]); hb[-1][4] = b[4]
                else:
                    hb.append([k, b[1], b[2], b[3], b[4]])
            return hb, 3600, "bar"
        return [[int(b[0]), b[1], b[2], b[3], b[4]] for b in bars], 900, "bar"
    return [], 60, "none"


def chart(hot, t):
    """Chart data for one papertrades row."""
    t0 = _ep(t.get("opened")); t1 = _ep(t.get("closed")) if t.get("status") == "closed" else time.time()
    if not t0:
        return None
    t1 = max(t1 or t0, t0 + 60)
    pad = 1800 if t1 - t0 <= 4 * 3600 else (6 * 3600 if t1 - t0 <= 3 * 86400 else 86400)
    cid = f"{t.get('rule')}|{t.get('key')}|{t.get('opened')}|{t.get('side')}"
    h = _C.get(cid)
    if h and (h["final"] or time.time() - h["t"] < 60):
        return h["v"]
    cs, step, kind = candles(hot, t["key"], t0 - pad, t1 + (pad if t.get("status") == "closed" else 0))
    off = 0.0
    if cs and kind == "tick" and t.get("rule") in SPOT_RULES and t.get("entry"):
        near = min(cs, key=lambda c: abs(c[0] - t0))
        off = round(near[4] - float(t["entry"]), 1)               # futures path - spot entry at the entry minute
    v = {"candles": cs, "step": step, "src": kind, "off": off, "t0": t0, "t1": t1 if t.get("status") == "closed" else None}
    _C[cid] = {"v": v, "t": time.time(), "final": t.get("status") == "closed" and time.time() > t1 + pad + 120}
    if len(_C) > 2000:
        _C.clear()
    return v


def api(hot, px, group=None, days=1) -> dict:
    """Paper trades of the last `days` sessions (opened or closed), each with its chart. Cached 20 s per (group, days)."""
    ck = (group, days)
    h = _API.get(ck)
    if h and time.time() - h[0] < 20:
        return h[1]
    PT = hot("papertrades")
    P = PT.api(hot, px, None, group)
    rows = (P.get("open") or []) + (P.get("closed") or [])
    since = (datetime.now(IST) - timedelta(days=days + (2 if days == 1 else 4))).strftime("%Y-%m-%d")
    tdays = sorted({str(t.get("closed") or t.get("opened"))[:10] for t in rows if str(t.get("closed") or t.get("opened"))[:10] >= since})[-days:]
    rows = [t for t in rows if t.get("status") == "open" or str(t.get("closed") or t.get("opened"))[:10] in tdays]
    out = []
    for t in rows:
        try:
            c = chart(hot, t)
        except Exception as e:
            c = {"candles": [], "err": f"{type(e).__name__}: {str(e)[:80]}"}
        keep = {k: t.get(k) for k in ("rule", "rule_label", "market", "key", "side", "status", "opened", "closed", "entry", "stop", "target", "targets",
                                     "exit", "exit_reason", "pnl", "pnl_open", "now", "ccy", "r", "why", "how", "legs_text", "review")}
        keep["tag"] = TAG.get(t.get("rule"), t.get("rule"))
        out.append({**keep, "chart": c})
    out.sort(key=lambda t: (t["status"] != "open", str(t.get("closed") or t.get("opened"))), reverse=False)
    open_ = [t for t in out if t["status"] == "open"]; cl = sorted([t for t in out if t["status"] != "open"], key=lambda t: str(t.get("closed")), reverse=True)
    res = {"trades": open_ + cl, "days": tdays, "at": datetime.now(IST).strftime("%H:%M:%S"), "errors": P.get("errors")}
    _API[ck] = (time.time(), res)
    return res


def one(hot, rule, key, opened, side=None) -> dict:
    """2026-11-09 (Nikhil: "under All Paper Trades .. make each row clickable which shows the same flash card chart"): the chart card for
    ONE row of data_store/livedesk/trade_log.csv (matched by rule, key, opened time and side), in the same shape as api() trades."""
    import csv, os
    pth = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data_store", "livedesk", "trade_log.csv")
    row = None
    try:
        with open(pth, newline="") as f:
            for r in csv.DictReader(f):
                if r.get("rule") == rule and r.get("key") == key and str(r.get("opened_ist"))[:16] == str(opened)[:16] and (not side or r.get("side") == side):
                    row = r
    except OSError:
        return {"err": "no trade log yet"}
    if row is None:
        return {"err": "trade not found in the log"}
    num = lambda v: (float(v) if v not in (None, "", "-") else None)
    t = {"rule": row.get("rule"), "rule_label": row.get("rule_label"), "market": row.get("market"), "key": row.get("key"), "side": row.get("side"),
         "status": row.get("status"), "opened": row.get("opened_ist"), "closed": row.get("closed_ist") or None, "ccy": row.get("ccy"),
         "exit_reason": row.get("exit_reason"), "why": row.get("why_taken"), "legs_text": row.get("legs") or None,
         "review": {"right": row.get("went_right"), "wrong": row.get("went_wrong"), "improve": row.get("where_to_improve")} if (row.get("went_right") or row.get("went_wrong") or row.get("where_to_improve")) else None}
    for k in ("entry", "stop", "target", "exit", "pnl", "pnl_open", "now", "r"):
        try:
            t[k] = num(row.get(k))
        except ValueError:
            t[k] = None
    t["tag"] = TAG.get(t["rule"], t["rule"])
    try:
        c = chart(hot, t)
    except Exception as e:
        c = {"candles": [], "err": f"{type(e).__name__}: {str(e)[:80]}"}
    t["chart"] = c
    return t
