"""
Overnight prep + "don't trade" guard (2026-10-24). Indications only - never places orders; Nikhil decides.

prep()  -> the morning card: expected open (GIFT Nifty), global moves since the Nifty close, FII / Pro / Client positions
           (published after the close), FII / DII cash, events, shock state and the level map for Nifty / Bank Nifty.
guard() -> rules the trade setups obey (tested in data_store/research/overnight/REPORT.md):
   Budget / election-result day      -> stand aside all day        (range 2.5x / 8x normal)
   RBI policy day                    -> no new entries 09:55-10:35 (10:00-10:30 range 1.5x normal)
   big opening gap (> 0.6 x avg day range) -> half size, no entry before 10:15 (bigger range; big gaps fill only ~25%)
   15-min "Volatile" label           -> half size                  (moves 1.4x - tested)
   shock mode (news / market shock)  -> no new setups while active (safety rule, untested)
What did NOT pass (shown as context only): FII / Pro / Client positions for direction after the open, overnight US 10Y,
USD-INR and crude moves, crude / US 10Y 20-day trends (sign flips by era).
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
CROSS = os.path.join(ROOT, "data_store", "flow", "cross_live.json")
NEWS = os.path.join(ROOT, "data_store", "agent", "news.json")
MP_LIVE = os.path.join(ROOT, "data_store", "dashboard", "market_pulse_live.json")
PART = os.path.join(ROOT, "data_store", "market_data", "nse_participant_oi_2023_2026.csv")
CASH = os.path.join(ROOT, "data_store", "market_data", "nse_fiidii_cash.csv")
DEEP = os.path.join(ROOT, "data_store", "market_data", "yahoo_daily_deep.csv")
EVENTS = os.path.join(ROOT, "src", "common", "regime_engine", "calendars", "events_2023_2027.json")
YSYM = {"NIFTY": "^NSEI", "BANKNIFTY": "^NSEBANK"}
BIG_EVENTS = ("Union", "Interim", "Lok", "Budget", "Election")
GAP_BIG = 0.6
_C: dict = {}
OPEN: dict = {}          # session open seen by the live poll {day: {key: px}}


def _cached(name, ttl, fn):
    c = _C.get(name)
    if c and time.time() - c[0] < ttl:
        return c[1]
    try:
        v = fn()
    except Exception as e:
        v = c[1] if c else {"error": str(e)[:160]}
    _C[name] = (time.time(), v)
    return v


def _j(p):
    try:
        return json.load(open(p))
    except Exception:
        return {}


def _daily():
    y = pd.read_csv(DEEP, usecols=["date", "symbol", "high", "low", "close"], parse_dates=["date"])
    out = {}
    for k, s in YSYM.items():
        q = y[y.symbol == s].set_index("date").sort_index()
        q["rng"] = (q.high - q.low) / q.close.shift(1)
        out[k] = q.tail(40)
    cl = y[y.symbol == "CL=F"].set_index("date").close.sort_index()
    out["crude20"] = float(cl.pct_change(20).iloc[-1] * 100) if len(cl) > 21 else None
    return out


def daily():
    return _cached("daily", 900, _daily)


def prev_close(key, day: str):
    q = daily().get(key)
    if q is None or not len(q):
        return None, None
    q = q[q.index < pd.Timestamp(day)]
    if len(q) < 21 or (pd.Timestamp(day) - q.index[-1]).days > 6:
        return None, None
    return float(q.close.iloc[-1]), float(q.rng.tail(20).mean())


def events_for(day: str):
    ev = _cached("events", 3600, lambda: _j(EVENTS).get("events", []))
    d = pd.Timestamp(day)
    today = [e["name"] for e in ev if pd.Timestamp(e["date"]) == d]
    # FOMC is announced in the US evening -> it lands on the next India session
    prev_us = [e["name"] for e in ev if e["name"].startswith(("FOMC", "US")) and d - timedelta(days=4) <= pd.Timestamp(e["date"]) < d]
    wk0 = d - timedelta(days=d.weekday())
    week = [f'{e["name"]} ({pd.Timestamp(e["date"]).strftime("%a %d %b")})' for e in ev if wk0 <= pd.Timestamp(e["date"]) < wk0 + timedelta(days=7)]
    nxt = [f'{e["name"]} ({pd.Timestamp(e["date"]).strftime("%d %b")})' for e in ev if d < pd.Timestamp(e["date"]) <= d + timedelta(days=35)][:4]
    return {"today": today, "after_us_event": prev_us, "this_week": week, "next": nxt}


def shock():
    s = (_j(NEWS).get("shock") or {})
    return s if s.get("active") else None


def vol_label(key):
    m = ((_j(MP_LIVE).get("score_v2") or {}).get("markets") or {}).get(key) or {}
    tf = next((t for t in m.get("timeframes", []) if t.get("tf") == "15m"), {})
    return tf.get("label"), tf.get("bar")


def note_open(key, t: datetime, px: float):
    if t.hour * 60 + t.minute >= 9 * 60 + 15 and px:
        OPEN.setdefault(t.strftime("%Y-%m-%d"), {}).setdefault(key, px)


def gap(key, day: str, open_px=None):
    pc, ar = prev_close(key, day)
    op = open_px or (OPEN.get(day) or {}).get(key)
    if not (pc and op and ar):
        return None
    g = op / pc - 1
    return {"open": round(op, 1), "prev_close": round(pc, 1), "gap_pct": round(100 * g, 2), "avg_range_pct": round(100 * ar, 2),
            "gap_x_range": round(g / ar, 2), "big": abs(g) > GAP_BIG * ar}


def guard(key, t: datetime) -> dict:
    """{'stand_aside': reason|None, 'no_entry': reason|None, 'half': [reasons], 'notes': [...]}"""
    day, m = t.strftime("%Y-%m-%d"), t.hour * 60 + t.minute
    g = {"stand_aside": None, "no_entry": None, "half": [], "notes": []}
    ev = events_for(day)
    big = [n for n in ev["today"] if n.startswith(BIG_EVENTS)]
    if big:
        g["stand_aside"] = f"{big[0]} day - ranges 2.5x+ normal"
    if any(n.startswith("RBI") for n in ev["today"]) and 9 * 60 + 55 <= m < 10 * 60 + 35:
        g["no_entry"] = "RBI policy 09:55-10:35"
    if ev["after_us_event"]:
        g["notes"].append(f"after {ev['after_us_event'][-1]} (ranges were normal in the test)")
    s = shock()
    if s:
        g["stand_aside"] = g["stand_aside"] or "shock mode: " + "; ".join(str(x) for x in (s.get("reasons") or [])[:2])[:120]
    gp = gap(key, day)
    if gp and gp["big"]:
        g["half"].append(f"big gap {gp['gap_pct']:+.2f}%")
        if m < 10 * 60 + 15:
            g["no_entry"] = g["no_entry"] or "big gap: no entry before 10:15"
    lb, _ = vol_label(key)
    if lb == "Volatile":
        g["half"].append("15-min Volatile")
    return g


# ------------------------------------------------------------------ the morning card
def _positions():
    p = pd.read_csv(PART, parse_dates=["date"]).sort_values("date")
    days = sorted(p.date.unique())[-6:]
    out = {"as_of": str(pd.Timestamp(days[-1]).date()), "rows": []}
    for ct in ("FII", "Pro", "Client", "DII"):
        q = p[p.client_type == ct].set_index("date").reindex(days)
        fnet = q.fut_idx_long - q.fut_idx_short
        onet = (q.opt_idx_call_long - q.opt_idx_call_short) - (q.opt_idx_put_long - q.opt_idx_put_short)
        out["rows"].append({"who": ct, "fut_long_pct": round(100 * q.fut_idx_long.iloc[-1] / (q.fut_idx_long.iloc[-1] + q.fut_idx_short.iloc[-1]), 1),
                            "fut_net": int(fnet.iloc[-1]), "fut_net_1d": int(fnet.diff().iloc[-1]), "fut_net_5d": int(fnet.iloc[-1] - fnet.iloc[0]),
                            "opt_net": int(onet.iloc[-1]), "opt_net_1d": int(onet.diff().iloc[-1]),
                            "puts_written_1d": int(q.opt_idx_put_short.diff().iloc[-1]), "calls_written_1d": int(q.opt_idx_call_short.diff().iloc[-1])})
    try:
        c = pd.read_csv(CASH)
        c = c.tail(10)
        out["cash"] = [{"date": r[0], "who": r[1], "net_cr": round(float(r[4]), 0)} for r in c.values.tolist()[-2:]]
    except Exception:
        out["cash"] = []
    fii = next(r for r in out["rows"] if r["who"] == "FII")
    lean = "up" if fii["opt_net_1d"] > 0 else "down" if fii["opt_net_1d"] < 0 else "flat"
    out["read"] = (f"FII option book moved {lean} on {out['as_of']}. In the test this leaned the next OPEN the same way by only ~0.05%, "
                   "and said nothing after the open - context, not a trade.")
    return out


def _levels(key, spot):
    try:
        from app import setups as S
    except Exception:
        import setups as S
    import levelmap as LM
    mp = LM.get(key, 10)
    f = lambda b: {"px": b["mid"], "lo": b["lo"], "hi": b["hi"], "name": ", ".join(b["parts"][:4]), "strength": b["strength"],
                   "score": b["score"], "alerts": b.get("alerts") or []}
    bl = mp.get("blocks") or []
    below = [f(b) for b in bl if b["hi"] < spot][-5:][::-1]
    above = [f(b) for b in bl if b["lo"] > spot][:5]
    return {"spot": round(spot, 1), "below": below, "above": above, "map": "rolling 10 sessions + weekly / monthly highs-lows + option OI"}


def _build():
    now = datetime.now(ZoneInfo("Asia/Kolkata")).replace(tzinfo=None)
    cx = _j(CROSS)
    eo = cx.get("expected_open") or {}
    gf = cx.get("gift_nifty") or {}
    day = now.strftime("%Y-%m-%d") if now.hour < 16 else (now + timedelta(days=1)).strftime("%Y-%m-%d")
    while pd.Timestamp(day).weekday() >= 5:
        day = (pd.Timestamp(day) + timedelta(days=1)).strftime("%Y-%m-%d")
    names = {"ES=F": "S&P fut", "NQ=F": "Nasdaq fut", "^N225": "Nikkei", "^HSI": "Hang Seng", "^KS11": "Kospi", "BZ=F": "Brent",
             "^TNX": "US 10Y", "INR=X": "USD/INR", "DX-Y.NYB": "Dollar idx", "^VIX": "US VIX", "GC=F": "Gold"}
    q = cx.get("quotes") or {}
    glob = [{"name": n, "last": q[s].get("last"), "since_close_pct": q[s].get("since_nifty_close_pct"), "chg_24h_pct": q[s].get("chg_24h_pct")}
            for s, n in names.items() if s in q]
    _, ar = prev_close("NIFTY", day)
    gx = (eo.get("gap_pct") or 0) / (100 * ar) if ar else None
    lv = {}
    for k in ("NIFTY", "BANKNIFTY"):
        pc, _ = prev_close(k, day)
        if pc:
            lv[k] = _levels(k, pc)
    ev = events_for(day)
    pos = _cached("positions", 600, _positions)
    rules = []
    big = [n for n in ev["today"] if n.startswith(BIG_EVENTS)]
    if big: rules.append(f"{big[0]}: stand aside all day")
    if any(n.startswith("RBI") for n in ev["today"]): rules.append("RBI policy: no new entries 09:55-10:35")
    if gx is not None and abs(gx) > GAP_BIG: rules.append(f"Expected gap {eo.get('gap_pct'):+.2f}% is big: half size, no entry before 10:15, don't fade it (big gaps fill ~25%)")
    if shock(): rules.append("Shock mode active: no new setups")
    lb, _ = vol_label("NIFTY")
    live = now.strftime("%Y-%m-%d") == day and 9 * 60 + 15 <= now.hour * 60 + now.minute <= 15 * 60 + 30
    if lb == "Volatile" and live: rules.append("Nifty 15-min is Volatile right now: half size")
    ctx = []
    if daily().get("crude20") is not None:
        ctx.append(f"Crude {daily()['crude20']:+.1f}% in 20 days (context: its effect on Nifty flips sign across years)")
    return {"for_day": day, "built": now.strftime("%Y-%m-%d %H:%M"),
            "open": {"gift": gf.get("last"), "gift_at": gf.get("recv"), "nifty_close": eo.get("nifty_close"), "gap_pct": eo.get("gap_pct"),
                     "open_est": eo.get("open_est"), "open_lo": eo.get("open_lo"), "open_hi": eo.get("open_hi"),
                     "gap_x_range": round(gx, 2) if gx is not None else None, "model": f"corr {eo.get('corr')}, direction right {eo.get('sign_right_pct')}%"},
            "global": glob, "positions": pos, "events": ev, "shock": shock(), "levels": lv, "rules": rules or ["No blocking rule: normal day"],
            "context": ctx, "tested": "data_store/research/overnight/REPORT.md"}


def _verdicts(key: str, eo: dict) -> dict:
    """Short verdicts (trend now, not a forecast) + blocks grouped strong / medium / weak."""
    mp = _j(MP_LIVE)
    pb = ((mp.get("india") or {}).get("playbook") or {}).get(key) or {}
    tf = pb.get("timeframes") or {}
    word = {"Up": "Bullish", "Down": "Bearish", "Flat": "Sideways"}
    import levelmap as LM
    lm = LM.get(key, 10)
    spot = lm.get("spot") or pb.get("close")
    bl = lm.get("blocks") or []
    alerts = {a: b for b in bl for a in (b.get("alerts") or [])}
    def rng(b):
        return f"{b['lo']:,.0f}–{b['hi']:,.0f}"
    sup = [b for b in bl if b["hi"] < spot]; res = [b for b in bl if b["lo"] > spot]
    near_s = max(sup, key=lambda b: b["hi"], default=None); near_r = min(res, key=lambda b: b["lo"], default=None)
    strong_s = max([b for b in sup if b["strength"] == "strong"], key=lambda b: b["hi"], default=None)
    strong_r = min([b for b in res if b["strength"] == "strong"], key=lambda b: b["lo"], default=None)
    intr = " & ".join(sorted({tf.get("15m", "?"), tf.get("4h", "?")}))
    r5 = pb.get("range_5d") or {}
    mlo = next((b for a, b in alerts.items() if a.startswith("this month low")), None) or next((b for a, b in alerts.items() if "month low" in a), None)
    mhi = next((b for a, b in alerts.items() if a.startswith("this month high")), None) or next((b for a, b in alerts.items() if "month high" in a), None)
    # 2026-10-25 (Nikhil): every block mention carries weak / medium / strong; several levels per horizon
    def pick(side, within_pct, strengths, n=3, star_ok=False):
        xs = sup if side == "S" else res
        xs = sorted(xs, key=lambda b: abs(b["mid"] - spot))
        out = [b for b in xs if abs(b["mid"] / spot - 1) * 100 <= within_pct and (b["strength"] in strengths or (star_ok and b.get("alerts")))]
        return [{"range": rng(b), "strength": b["strength"], "star": bool(b.get("alerts"))} for b in out[:n]]
    lim5 = max(abs(r5["hi"] / spot - 1), abs(r5["lo"] / spot - 1)) * 100 if r5.get("lo") else 3.0
    v = {
        "intraday": {"word": word.get(tf.get("15m"), "Mixed") if tf.get("15m") == tf.get("4h") else "Mixed", "line": "",
                     "res": pick("R", 1.2, ("strong", "medium", "weak")), "sup": pick("S", 1.2, ("strong", "medium", "weak"))},
        "weekly": {"word": word.get(tf.get("weekly"), "Mixed"),
                   "line": f"Likely 5-day range {r5['lo']:,.0f}–{r5['hi']:,.0f}" if r5.get("lo") else "",
                   "res": pick("R", lim5, ("strong", "medium")), "sup": pick("S", lim5, ("strong", "medium"))},
        "monthly": {"word": word.get(tf.get("monthly"), "Mixed"),
                    "line": (f"Month low block {rng(mlo)} {mlo['strength']}" if mlo else "") + (" · " if mlo and mhi else "") + (f"month high block {rng(mhi)} {mhi['strength']}" if mhi else ""),
                    "res": pick("R", 7, ("strong",), star_ok=True), "sup": pick("S", 7, ("strong",), star_ok=True)},
    }
    ins = [{"range": rng(b), "strength": b["strength"], "star": bool(b.get("alerts"))} for b in bl
           if b["lo"] <= spot <= b["hi"] and b["strength"] != "weak"]     # 2026-11-03: price sitting inside a block (was silently dropped)
    for h in v.values():
        h["inside"] = ins
    for h in v.values():                                   # plain-text form for places that show one line
        txt = lambda xs: ", ".join(f"{x['range']} {x['strength']}{' ★' if x['star'] else ''}" for x in xs)
        h["text"] = " · ".join(t for t in [h["line"], ("price inside " + txt(h["inside"])) if h.get("inside") else "", ("resistance " + txt(h["res"])) if h["res"] else "", ("support " + txt(h["sup"])) if h["sup"] else ""] if t)
    groups = {}
    for st in ("strong", "medium", "weak"):
        groups[st] = [{"range": rng(b), "lo": b["lo"], "hi": b["hi"], "side": "R" if b["lo"] > spot else "S" if b["hi"] < spot else "IN", "star": bool(b.get("alerts")), "why": ", ".join(list(dict.fromkeys((b.get("alerts") or []) + [x for x in b["parts"] if x not in (b.get("alerts") or [])]))[:3])}
                      for b in sorted(bl, key=lambda b: -b["mid"]) if b["strength"] == st]
    return {"spot": spot, "verdict": v, "blocks": groups, "levels": lm.get("levels") or [], "range5": {"lo": r5.get("lo"), "hi": r5.get("hi")} if r5.get("lo") else None, "trend_note": "verdict = trend now on that timeframe, not a forecast"}


def _oi_short(key: str) -> dict:
    oc = ((_j(os.path.join(ROOT, "data_store", "flow", "oc_live.json")).get("symbols") or {}).get(key)) or {}
    out = {"pcr": oc.get("pcr"), "max_pain": oc.get("max_pain"), "call_wall": oc.get("call_wall"), "put_wall": oc.get("put_wall"), "as_of": oc.get("as_of")}
    try:
        import setups as S
        st = S.oi_strikes(key)
        out["puts"] = [int(p) for p, n, k in st if k == "oi" and n.startswith("put")][:3]
        out["calls"] = [int(p) for p, n, k in st if k == "oi" and n.startswith("call")][:3]
        out["put_writing"] = [int(p) for p, n, k in st if k == "oi_add" and n.startswith("put")]
        out["call_writing"] = [int(p) for p, n, k in st if k == "oi_add" and n.startswith("call")]
    except Exception:
        pass
    return out


def _build_short():
    full = _build()
    eo = full.get("open") or {}
    out = {"for_day": full.get("for_day"), "built": full.get("built"), "rules": full.get("rules"), "events": (full.get("events") or {}).get("this_week") or []}
    for k in ("NIFTY", "BANKNIFTY"):
        try:
            out[k] = _verdicts(k, eo)
            out[k]["oi"] = _oi_short(k)
        except Exception as e:
            out[k] = {"error": str(e)[:120]}
    pos = full.get("positions") or {}
    sgn = lambda n: "▲" if n > 0 else "▼" if n < 0 else "·"
    out["participants"] = [{"who": r["who"], "fut": f"{sgn(r['fut_net_1d'])} futures {r['fut_net_1d']:+,}", "opt": f"{sgn(r['opt_net_1d'])} options {r['opt_net_1d']:+,}"}
                           for r in pos.get("rows", []) if r["who"] in ("FII", "Pro", "Client")]
    out["participants_as_of"] = pos.get("as_of")
    out["full"] = full
    return out


def prep():
    return _cached("prep", 60, _build_short)
