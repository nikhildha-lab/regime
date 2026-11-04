"""WEEKLY GLOBAL NARRATIVE (2026-11-04, Nikhil: "weekly global narrative ... geopolitical changes, new macro indicators,
any major turning points for global indices ... refreshed on daily level").

Two layers:
  1. facts()  - computed here from files, no AI, refreshed every 30 min by the API:
       * global indices: 1-day / 1-week / 1-month change, a "big week" flag (weekly move vs its own 1-year weekly spread),
         turning points in the last 5 sessions (crossed the 50 / 200-day average, new 52-week high / low, 10 % off the high)
       * cross-asset: gold, Brent, dollar index, US 10Y, US VIX - 1-week change
       * macro indicators from the dashboard (market_pulse_live.json -> macro): the ones at a 1-year extreme or moving hard
       * a rolling 7-day log of big headlines (severity 2-3 from the news watcher), so a week of news survives
  2. story    - data_store/agent/out/global_narrative.json, written once a day by a scheduled Claude task that reads facts()
                and searches the week's geopolitical / macro news. Shown when it is from the last 36 hours.
Context only - not a signal, never an order.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta

import pandas as pd

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
DEEP = os.path.join(ROOT, "data_store", "market_data", "yahoo_daily_deep.csv")
LIVE = os.path.join(ROOT, "data_store", "dashboard", "market_pulse_live.json")
NEWS = os.path.join(ROOT, "data_store", "agent", "news.json")
NEWS7 = os.path.join(ROOT, "data_store", "agent", "narrative_news_7d.json")
STORY = os.path.join(ROOT, "data_store", "agent", "out", "global_narrative.json")

INDICES = [("^NSEI", "Nifty 50", "India"), ("^GSPC", "S&P 500", "US"), ("NQ=F", "Nasdaq 100", "US"), ("^DJI", "Dow Jones", "US"),
           ("^STOXX50E", "Euro Stoxx 50", "Europe"), ("^N225", "Nikkei 225", "Japan"), ("^KS11", "Kospi", "Korea"),
           ("^HSI", "Hang Seng", "Hong Kong"), ("000001.SS", "Shanghai Composite", "China")]
CROSS = [("GC=F", "Gold"), ("BZ=F", "Brent crude"), ("DX-Y.NYB", "Dollar index"), ("^TNX", "US 10Y yield"), ("^VIX", "US VIX"),
         ("BTC-USD", "Bitcoin")]
_C: dict = {}


def _deep():
    x = _C.get("deep")
    if x and time.time() - x[0] < 1800:
        return x[1]
    y = pd.read_csv(DEEP, usecols=["date", "symbol", "high", "low", "close"])
    y = y[y.symbol.isin([s for s, *_ in INDICES + CROSS])]
    y["date"] = pd.to_datetime(y["date"])
    _C["deep"] = (time.time(), y)
    return y


def _pct(a, b):
    return None if a is None or b is None or not b else round((a / b - 1) * 100, 2)


def _one(d: pd.DataFrame) -> dict:
    d = d.sort_values("date").drop_duplicates("date", keep="last").set_index("date")
    c = d.close.astype(float)
    if len(c) < 30:
        return {}
    last, at = float(c.iloc[-1]), d.index[-1]
    wk = c[c.index <= at - pd.Timedelta(days=7)]
    mo = c[c.index <= at - pd.Timedelta(days=30)]
    out = {"last": round(last, 2), "as_of": str(at.date()), "chg_1d": _pct(last, float(c.iloc[-2])),
           "chg_1w": _pct(last, float(wk.iloc[-1])) if len(wk) else None, "chg_1m": _pct(last, float(mo.iloc[-1])) if len(mo) else None}
    # big week: this week's move vs the spread of weekly moves over the last year
    w = c.resample("W-FRI").last().pct_change(fill_method=None).dropna().tail(52) * 100
    if out["chg_1w"] is not None and len(w) > 20 and w.std() > 0:
        out["week_z"] = round(float(out["chg_1w"] / w.std()), 1)
    tp = []
    ma50, ma200 = c.rolling(50).mean(), c.rolling(200).mean()
    rec = c.tail(6)
    for lab, ma in (("50-day", ma50), ("200-day", ma200)):
        m = ma.reindex(rec.index)
        if m.isna().any():
            continue
        above = rec > m
        if above.iloc[-1] and not above.iloc[0]:
            tp.append(f"crossed above its {lab} average")
        elif not above.iloc[-1] and above.iloc[0]:
            tp.append(f"fell below its {lab} average")
    hi52, lo52 = d.high.astype(float).tail(252), d.low.astype(float).tail(252)
    if len(hi52) > 200:
        if float(hi52.tail(5).max()) >= float(hi52.max()):
            tp.append("new 52-week high this week")
        if float(lo52.tail(5).min()) <= float(lo52.min()):
            tp.append("new 52-week low this week")
        off = (last / float(hi52.max()) - 1) * 100
        out["off_high_pct"] = round(off, 1)
        prev_off = (float(c.iloc[-6]) / float(hi52.iloc[:-5].max()) - 1) * 100 if len(hi52) > 6 else off
        if off <= -10 < prev_off:
            tp.append("now 10 % below its 52-week high (correction)")
        if off <= -20 < prev_off:
            tp.append("now 20 % below its 52-week high (bear-market line)")
    if out.get("week_z") is not None and abs(out["week_z"]) >= 2:
        tp.append(f"biggest kind of weekly move ({out['chg_1w']:+.1f} %, {abs(out['week_z']):.1f}x a normal week)")
    out["turning"] = tp
    out["spark"] = [round(float(v), 2) for v in c.tail(30).values]
    return out


def _news7() -> list:
    """Rolling 7-day log of severity 2-3 headlines from the news watcher (news.json keeps only 48 h)."""
    try:
        log = json.load(open(NEWS7))
    except Exception:
        log = []
    try:
        n = json.load(open(NEWS))
        seen = {x["id"] for x in log}
        for g in (n.get("shock_items") or []) + (n.get("top") or []):
            if g.get("id") not in seen and int(g.get("severity") or 0) >= 2 and g.get("source") != "NSE announcement":
                log.append({"id": g["id"], "title": g["title"], "link": g.get("link"), "published": g.get("published"),
                            "severity": int(g.get("severity") or 0), "n_sources": g.get("n_sources", 1)})
                seen.add(g["id"])
    except Exception:
        pass
    cut = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d %H:%M")
    log = [x for x in log if (x.get("published") or "9") >= cut][-400:]
    tmp = NEWS7 + f".{os.getpid()}.tmp"
    with open(tmp, "w") as f:
        json.dump(log, f)
    os.replace(tmp, NEWS7)
    return log


def facts() -> dict:
    x = _C.get("facts")
    if x and time.time() - x[0] < 1800:
        return x[1]
    y = _deep()
    idx = []
    for sym, name, region in INDICES:
        r = _one(y[y.symbol == sym])
        if r:
            idx.append({"sym": sym, "name": name, "region": region, **r})
    cross = []
    for sym, name in CROSS:
        r = _one(y[y.symbol == sym])
        if r:
            cross.append({"sym": sym, "name": name, **{k: r.get(k) for k in ("last", "as_of", "chg_1d", "chg_1w", "chg_1m", "week_z", "turning")}})
    macro = []
    try:
        for t in json.load(open(LIVE)).get("macro") or []:
            if t.get("group") == "India":          # India-only readings (basis, OI, India VIX) stay on the Indian tab
                continue
            p, z = t.get("pct_1y"), t.get("z_1y")
            why = []
            if p is not None and p >= 90:
                why.append(f"at the {p}th percentile of the last year (high)")
            if p is not None and p <= 10:
                why.append(f"at the {p}th percentile of the last year (low)")
            if z is not None and abs(z) >= 2:
                why.append(f"{abs(z):.1f} standard deviations from its 1-year average")
            if why:
                macro.append({"key": t.get("key"), "label": t.get("label"), "value": t.get("value"), "change_5d": t.get("change_5d"),
                              "signal": t.get("signal"), "why": "; ".join(why), "note": t.get("note")})
    except Exception:
        pass
    heads = sorted(_news7(), key=lambda g: (g["severity"], g.get("n_sources") or 1, g.get("published") or ""), reverse=True)[:12]
    breadth = {"up_week": sum(1 for i in idx if (i.get("chg_1w") or 0) > 0), "n": len(idx)}
    out = {"built": datetime.now().strftime("%Y-%m-%d %H:%M"), "indices": idx, "cross": cross, "macro": macro,
           "headlines": heads, "breadth": breadth,
           "note": "Facts are computed from daily closes; the story is written once a day from these facts plus the week's news. Context only."}
    _C["facts"] = (time.time(), out)
    return out


def story() -> dict | None:
    try:
        s = json.load(open(STORY))
    except Exception:
        return None
    try:
        age_h = (datetime.now() - datetime.strptime(str(s.get("as_of"))[:16], "%Y-%m-%d %H:%M")).total_seconds() / 3600
    except Exception:
        age_h = None
    s["age_h"] = round(age_h, 1) if age_h is not None else None
    s["stale"] = age_h is None or age_h > 36
    return s


def api() -> dict:
    return {"facts": facts(), "story": story()}


if __name__ == "__main__":
    _C.clear()
    print(json.dumps(facts(), indent=1, default=str)[:20000])
