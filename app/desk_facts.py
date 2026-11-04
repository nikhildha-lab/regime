"""Desk facts - a no-AI snapshot of the Indian desk, refreshed every 5 minutes.

Written to data_store/agent/desk_facts.json. FACTS ONLY (numbers copied from the
engine / trackers / log book) - no opinions, no ideas. The Desk Lead (Claude, hourly
in market hours) reads this file first and adds judgement in desk_notes.json.
Started by server.py (Market Pulse). Added 2026-10-20.
"""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timedelta, timezone

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
OUT = os.path.join(ROOT, "data_store", "agent", "desk_facts.json")
IST = timezone(timedelta(hours=5, minutes=30))
EVERY_S = 300


def _load(rel):
    try:
        with open(os.path.join(ROOT, rel)) as f:
            return json.load(f)
    except Exception:
        return None


def _age_min(rel):
    try:
        return round((time.time() - os.path.getmtime(os.path.join(ROOT, rel))) / 60, 1)
    except Exception:
        return None


def _levels(lv, close, n=2):
    out = {"supports": [], "resistances": []}
    for side in ("supports", "resistances"):
        for x in (lv or {}).get(side, [])[:n]:
            out[side].append({"price": x.get("price"), "what": ", ".join(x.get("names") or []),
                              "dist_pct": x.get("dist_pct")})
    return out


def build() -> dict:
    now = datetime.now(IST)
    mp = _load("data_store/dashboard/market_pulse_live.json") or {}
    markets, sv2 = mp.get("markets") or {}, ((mp.get("score_v2") or {}).get("markets") or {})
    idx = {}
    for k in ("NIFTY", "BANKNIFTY"):
        m, s = markets.get(k) or {}, sv2.get(k) or {}
        rg, dec = m.get("regime") or {}, m.get("decision") or {}
        al = s.get("alignment") or {}
        idx[k] = {
            "close": rg.get("close"), "daily_bar": rg.get("bar"),
            "regime": rg.get("regime_name"), "trend": rg.get("trend_state"), "vol": rg.get("vol_state"),
            "size": dec.get("size"), "range5": dec.get("range5"),
            "intraday_bias": s.get("intraday_bias"),
            "tf_alignment": {k2: al.get(k2) for k2 in ("direction", "agree_pct", "confidence", "word")},
            "levels": _levels(m.get("levels"), rg.get("close")),
        }
    live = {}
    try:
        import livedesk as LD
        v = LD.view("nifty")
        for k, i in (v.get("instruments") or {}).items():
            su = i.get("setup") or {}
            live[k] = {"spot": i.get("spot"), "regime": i.get("regime"), "day_type": i.get("day_type"),
                       "setup": {x: su.get(x) for x in ("state", "grade", "side")} if su else None,
                       "walls": i.get("walls"), "pcr": (i.get("oi") or {}).get("pcr")}
        st = (LD.status("nifty").get("desks") or {}).get("nifty") or {}
        book = {"tracker_ok": st.get("ok")}          # 2026-11-03: tracker setup cards dropped (Nikhil)
    except Exception as e:
        book = {"error": f"live desk not readable: {e}"}
    nw = _load("data_store/agent/news.json") or {}
    news = {"at": nw.get("at"), "shock": (nw.get("shock") or {}).get("active"),
            "top": [t.get("title") for t in (nw.get("top") or [])[:3]]}
    guard = _load("data_store/agent/logs/guard_report.json") or {}
    blocked = [k for k, v in guard.items() if isinstance(v, dict) and str(v.get("verdict", "")).upper() == "BLOCK"]
    return {
        "as_of": now.strftime("%Y-%m-%d %H:%M:%S IST"),
        "what": "facts only (no AI, no opinions) - refreshed every 5 min by Market Pulse",
        "market_open": now.weekday() < 5 and (9 * 60 + 15) <= now.hour * 60 + now.minute <= (15 * 60 + 30),
        "indices": idx, "live_tracker": live, "live_desk": book, "news": news,
        "blocked_outputs": blocked,
        "ages_min": {"market_pulse": _age_min("data_store/dashboard/market_pulse_live.json"),
                     "desk_notes_ai": _age_min("data_store/agent/desk_notes.json"),
                     "news": _age_min("data_store/agent/news.json")},
    }


def write_once():
    d = build()
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT + ".tmp", "w") as f:
        json.dump(d, f, indent=1, default=str)
    os.replace(OUT + ".tmp", OUT)
    return d


def _loop():
    time.sleep(20)                       # let Market Pulse / Live Desk warm up
    while True:
        try:
            write_once()
        except Exception as e:
            print("desk_facts failed:", e, flush=True)
        time.sleep(EVERY_S)


def start():
    threading.Thread(target=_loop, daemon=True, name="desk-facts").start()


if __name__ == "__main__":
    print(json.dumps(write_once(), indent=1, default=str)[:3000])
