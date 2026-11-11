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
SPOT_RULES = {"stfa", "touchv2", "ovn_h30", "ovn_h30f", "vcy_night", "vcy_day", "vcy_day_sk", "vcy_day_bw", "vcy_day_dh"}
TAG = {"dom_ix": "DOM", "dom_ix_break": "DOM-B", "dom_ix_hold": "DOM-H", "dom_stocks": "DOM-S", "touchv2": "Touch v2", "sorb": "ORB", "svb": "SVB", "fsb": "BuildUp v1", "fsb2": "BuildUp v2", "bkf": "BreakFade", "t55": "Trend 55", "qph": "30d High", "n5m": "N500 Mom", "n5i": "N500 Ind", "istr_n": "Nifty strangle", "icon_s": "Sensex condor", "svb_tr": "SVB trail", "ovn_h30": "Overnight", "ovn_h30f": "Overnight+flow", "vcy_night": "Vol night",
       "vcy_day": "Vol day", "vcy_day_sk": "Vol skew", "vcy_day_bw": "Vol b-wing", "vcy_day_dh": "Vol hedged", "b4h": "4H break",
       "b4h_v4": "4H v4", "b4h_h2": "4H H2", "b4h_g2": "Gold v2", "stfa": "ST-Fisher-ADX"}
_C: dict = {}
_API: dict = {}
# 2026-11-11 (Nikhil: "some trades don't have charts or incorrect"): option-premium rules are drawn on their index without price lines,
# NSE stocks / indices with no stored path fall back to Dhan intraday candles, and a feed whose price sits far from the trade's own
# entry (another contract, e.g. Natgas front month) is shifted by that gap - the same "off" idea the spot rules already use.
PREMIUM_RULES = {"icon_s", "istr_n"}
IDX_SID = {"NIFTY": 13, "BANKNIFTY": 25, "SENSEX": 51, "FINNIFTY": 27, "MIDCPNIFTY": 442}
_DH: dict = {}
# 2026-11-11 (Nikhil: "Build Up v2 should have a 4 hour chart in continuation for 6 days since it is held for 5 days at least"):
# multi-day stock rules get 4-hour NSE candles (09:15-13:15, 13:15-15:30) drawn back to back with no overnight / weekend gaps,
# one session before the entry and room for `days` sessions after it.
HOLD_4H = {"fsb2": 6, "fsb": 6}


def _base_key(key):
    """'SENSEX 2026-11-12' (an expiry-dated structure) -> 'SENSEX'."""
    return str(key or "").split(" ")[0]


def _dhan_candles(hot, key, t0, t1):
    """Dhan intraday candles for an NSE stock or index (IST), cached 2 min. Empty when Dhan is not logged in."""
    k = _base_key(key)
    span = t1 - t0
    mins = 1 if span <= 4 * 3600 else 5 if span <= 2 * 86400 else 15 if span <= 6 * 86400 else 60
    ck = (k, int(t0 // 60), int(t1 // 120), mins)
    h = _DH.get(ck)
    if h and time.time() - h[0] < 120:
        return h[1]
    out = []
    try:
        import dhan_live as DL
        if k in IDX_SID:
            seg, sid, inst = "IDX_I", IDX_SID[k], "INDEX"
        else:
            seg, sid, inst = "NSE_EQ", DL.ids_equity(k), "EQUITY"
        if sid:
            d0 = datetime.fromtimestamp(t0, IST).date(); d1 = datetime.fromtimestamp(t1, IST).date()
            for b in DL.intraday(seg, sid, inst, d0, d1, mins, oi=False):
                ts = b["ts"].timestamp()
                if t0 - mins * 60 <= ts <= t1:
                    out.append([int(ts), b["open"], b["high"], b["low"], b["close"]])
    except Exception:
        out = []
    _DH[ck] = (time.time(), (out, mins * 60))
    if len(_DH) > 500:
        _DH.clear()
    return out, mins * 60


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


def _four_hour(hot, t, t0, t1, days):
    """4H NSE candles for a multi-day trade: 2 before the entry bar, up to 2 x days from it. (candles, slots)."""
    out, _ = _dhan_candles(hot, t["key"], t0 - 5 * 86400, min(time.time(), t0 + (days + 4) * 86400) if not t1 else t1 + 86400)
    if not out:
        return [], 0
    # _dhan_candles picks the interval from the window; regroup whatever came back into the two session halves
    g = {}
    for e, o, h, l, c in out:
        d = datetime.fromtimestamp(e, IST)
        hm = d.hour * 60 + d.minute
        if not (9 * 60 + 15 <= hm < 15 * 60 + 30):
            continue
        half = 0 if hm < 13 * 60 + 15 else 1
        st = d.replace(hour=9 if half == 0 else 13, minute=15, second=0, microsecond=0).timestamp()
        b = g.get(st)
        if b is None:
            g[st] = [int(st), o, h, l, c]
        else:
            b[2] = max(b[2], h); b[3] = min(b[3], l); b[4] = c
    bars = [g[k] for k in sorted(g)]
    i0 = next((i for i, b in enumerate(bars) if b[0] + (4 * 3600 if datetime.fromtimestamp(b[0], IST).hour == 9 else 8100) > t0), len(bars))
    bars = bars[max(0, i0 - 2): i0 + 2 * days]
    return bars, (min(i0, 2) + 2 * days)


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
    note = ""
    if t.get("rule") in HOLD_4H:
        days = HOLD_4H[t["rule"]]
        cs, slots = _four_hour(hot, t, t0, t1 if t.get("status") == "closed" else None, days)
        v = {"candles": cs, "step": 14400, "src": "4h", "off": 0.0, "t0": t0, "t1": t1 if t.get("status") == "closed" else None,
             "even": True, "slots": slots, "note": f"4-hour candles, back to back - one session before entry and {days} sessions of the hold"}
        _C[cid] = {"v": v, "t": time.time(), "final": t.get("status") == "closed" and time.time() > t1 + 86400}
        return v
    cs, step, kind = candles(hot, t["key"], t0 - pad, t1 + (pad if t.get("status") == "closed" else 0))
    if not cs and t.get("key") in ("ES", "NQ", "SPY"):        # US paper trades: the same 5-min bars app/uspaper.py grades on (IST index)
        try:
            b = hot("uspaper").bars(t["key"]); a0, a1 = t0 - pad, t1 + (pad if t.get("status") == "closed" else 0)
            ts = [x.replace(tzinfo=IST).timestamp() for x in b.index.to_pydatetime()]
            cs = [[int(e), float(r.open), float(r.high), float(r.low), float(r.close)] for e, r in zip(ts, b.itertuples()) if a0 <= e <= a1]
            step, kind = 300, ("bar" if cs else kind)
        except Exception:
            cs = []
    if not cs:
        cs, step = _dhan_candles(hot, t["key"], t0 - pad, t1 + (pad if t.get("status") == "closed" else 0))
        kind = "dhan" if cs else kind
    off = 0.0
    if cs and kind == "tick" and t.get("rule") in SPOT_RULES and t.get("entry"):
        near = min(cs, key=lambda c: abs(c[0] - t0))
        off = round(near[4] - float(t["entry"]), 1)               # futures path - spot entry at the entry minute
    nolines = t.get("rule") in PREMIUM_RULES
    if nolines:
        note = f"{_base_key(t['key'])} index candles - this trade is an option structure priced in premium, so its levels are not drawn"
    elif cs and not off and t.get("entry"):
        near = min(cs, key=lambda c: abs(c[0] - t0)); e = float(t["entry"])
        if e and abs(near[4] - e) / abs(e) > 0.01:          # the chart feed is another contract than the one the rule priced
            off = round(near[4] - e, 4)
            note = f"this feed is {abs(off) / abs(e) * 100:.1f}% {'above' if off > 0 else 'below'} the rule's own price at entry (another contract) - the trade's levels are shifted by {off:+g} to line up"
    v = {"candles": cs, "step": step, "src": kind, "off": off, "t0": t0, "t1": t1 if t.get("status") == "closed" else None, "note": note, "nolines": nolines}
    _C[cid] = {"v": v, "t": time.time(), "final": t.get("status") == "closed" and time.time() > t1 + pad + 120}
    if len(_C) > 2000:
        _C.clear()
    return v


def stop_hit(hot, t):
    """2026-11-11 (Nikhil: "update the trades with error if SL has already hit sometime back"): for an OPEN trade, the first candle
    after entry whose low (long) / high (short) reached the stop, on the chart's own price path (shifted by "off"). Read-only flag:
    the rule's own record is not changed here. Returns {"t": epoch, "at": "HH:MM", "px": candle extreme} or None."""
    if t.get("status") != "open" or t.get("stop") in (None, "") or t.get("rule") in PREMIUM_RULES:
        return None
    try:
        c = chart(hot, t) or {}
    except Exception:
        return None
    cs, off, t0 = c.get("candles") or [], float(c.get("off") or 0), c.get("t0")
    if not cs or not t0:
        return None
    st = float(t["stop"]); long_ = t.get("side") == "long"; step = c.get("step") or 60
    e = float(t["entry"]) if t.get("entry") not in (None, "") else None
    # a trailed stop (moved to the profit side of entry) is only known NOW, so the past path is checked against the first stop
    trailed = e is not None and ((long_ and st > e) or (not long_ and st < e))
    st_path = float(t["stop0"]) if t.get("stop0") not in (None, "") else (None if trailed else st)
    after = [x for x in cs if x[0] >= t0]
    if st_path is not None:
        for x in after:
            lo, hi = x[3] - off, x[2] - off
            if (long_ and lo <= st_path) or (not long_ and hi >= st_path):
                return {"t": x[0], "at": datetime.fromtimestamp(x[0], IST).strftime("%m-%d %H:%M"), "px": round(lo if long_ else hi, 4), "stop_used": st_path}
    if after:                                            # the current (possibly trailed) stop against the latest close
        x = after[-1]; cl = x[4] - off
        if (long_ and cl <= st) or (not long_ and cl >= st):
            return {"t": x[0], "at": datetime.fromtimestamp(x[0], IST).strftime("%m-%d %H:%M"), "px": round(cl, 4), "stop_used": st}
    return None


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
