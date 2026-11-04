"""
India desk (2026-10-17): the one place the Desk Analyst agent and the Desk tab read from.
Built inside export_dashboard_data.py -> out["india"]. Reads only local files; never places orders.

Labels (from our own tests, scripts/research/*):
  TESTED  passed the 4 gates (information, both halves, luck < 5%, engine improvement / clear edge)
  WATCH   small or borderline edge
  CONTEXT useful to know, not proven as a signal
Stock screen tests (test_stock_screens.py, 200 most traded NSE stocks 2009-26, excess vs the group average):
  day losers keep lagging: -0.19% next day, -0.31% next 5 (both halves, luck 0)        -> TESTED (small edge)
  day gainers: +0.12% next day, then fade (-0.09% next 5)                              -> WATCH
  20-day breakouts on volume: -0.21% next 5 days (they tend to fade)                   -> CONTEXT (do not chase)
  6-month momentum leaders: +0.13% next 20 (luck 53%)                                  -> CONTEXT
  6-month laggards: -0.52% next 20 in 2018-26 but +0.21% in 2009-17                    -> WATCH
"""
from __future__ import annotations

import json
import os

import numpy as np
import pandas as pd

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
IND = os.path.join(ROOT, "data_store", "india")
TESTED, WATCH, CONTEXT = "tested", "watch", "context"


def _load_json(p):
    try:
        return json.load(open(p)) if os.path.exists(p) else None
    except Exception:
        return None


def _r(x, n=2):
    try:
        return None if x is None or pd.isna(x) else round(float(x), n)
    except Exception:
        return None


# ------------------------------------------------------------------ sectors
def sectors(live: dict | None) -> dict | None:
    p = os.path.join(IND, "sectors_day.csv")
    if not os.path.exists(p):
        return None
    d = pd.read_csv(p)
    d["date"] = pd.to_datetime(d["date"])
    P = d.pivot_table(index="date", columns="sector", values="close").sort_index()
    q = (live or {}).get("quotes", {})
    today = {}
    for s in P.columns:
        v = q.get(s)
        if v and v.get("ltp") and v.get("prev_close"):
            today[s] = v["ltp"] / v["prev_close"] - 1
    if "Nifty 50" not in P:
        return None
    rows = []
    n50 = P["Nifty 50"]
    for s in P.columns:
        c = P[s].dropna()
        if len(c) < 70 or s in ("Nifty 50", "India VIX"):
            continue
        ex = lambda k: (c.iloc[-1] / c.iloc[-1 - k] - 1) - (n50.reindex(c.index).iloc[-1] / n50.reindex(c.index).iloc[-1 - k] - 1)
        rs60, rs20 = ex(60), ex(20)
        rs20_prev = (c.iloc[-11] / c.iloc[-31] - 1) - (n50.reindex(c.index).iloc[-11] / n50.reindex(c.index).iloc[-31] - 1)
        mom = rs20 - rs20_prev
        quad = ("Leading" if rs60 > 0 and mom > 0 else "Weakening" if rs60 > 0 else "Improving" if mom > 0 else "Lagging")
        rows.append({"sector": s.replace("Nifty ", ""), "today_pct": _r(100 * today.get(s), 2) if s in today else None,
                     "ret5_pct": _r(100 * (c.iloc[-1] / c.iloc[-6] - 1)), "ret20_pct": _r(100 * (c.iloc[-1] / c.iloc[-21] - 1)),
                     "rs20_pct": _r(100 * rs20), "rs60_pct": _r(100 * rs60), "rs_momentum": _r(100 * mom), "quadrant": quad,
                     "last_bar": str(c.index[-1].date())})
    rows.sort(key=lambda r: -(r["rs60_pct"] or 0))
    n_today = today.get("Nifty 50")
    return {"label": CONTEXT, "nifty_today_pct": _r(100 * n_today) if n_today is not None else None,
            "rows": rows, "how": "Relative strength vs Nifty 50: 60-day (trend) and change in 20-day (momentum). "
            "Leading = strong and getting stronger; Weakening = strong but fading; Improving = weak but turning up; "
            "Lagging = weak and getting weaker. Not tested as a signal yet."}


# ------------------------------------------------------------------ stocks
def _eod() -> pd.DataFrame | None:
    parts = []
    p1 = os.path.join(ROOT, "stocks_data.csv")
    if os.path.exists(p1):
        a = pd.read_csv(p1, usecols=["Date", "Symbol", "Open", "High", "Low", "Close", "Volume"])
        a.columns = ["date", "symbol", "open", "high", "low", "close", "volume"]
        a["date"] = pd.to_datetime(a["date"], format="%d-%m-%Y", errors="coerce")
        parts.append(a)
    p2 = os.path.join(ROOT, "data_store", "market_data", "nse_stocks_eod.csv")
    if os.path.exists(p2):
        b = pd.read_csv(p2)
        b["date"] = pd.to_datetime(b["date"], errors="coerce")
        parts.append(b[["date", "symbol", "open", "high", "low", "close", "volume"]])
    if not parts:
        return None
    x = pd.concat(parts).dropna(subset=["date"]).drop_duplicates(["date", "symbol"], keep="last")
    return x


def stocks(live: dict | None, univ: dict | None) -> dict | None:
    x = _eod()
    if x is None:
        return None
    C = x.pivot_table(index="date", columns="symbol", values="close").sort_index()
    V = x.pivot_table(index="date", columns="symbol", values="volume").sort_index().reindex_like(C)
    fno = set((univ or {}).get("stocks", {})) or set(C.columns)
    cols = [c for c in C.columns if c in fno]
    C, V = C[cols], V[cols]
    last = C.index[-1]
    ma20, ma50, ma200 = C.rolling(20).mean().iloc[-1], C.rolling(50).mean().iloc[-1], C.rolling(200, min_periods=150).mean().iloc[-1]
    hi20, lo20 = C.rolling(20).max().shift(1).iloc[-1], C.rolling(20).min().shift(1).iloc[-1]
    vavg20 = V.rolling(20).mean().shift(1).iloc[-1]
    mom6 = C.shift(5).iloc[-1] / C.shift(125).iloc[-1] - 1
    ret20 = C.iloc[-1] / C.iloc[-21] - 1
    hi52 = C.rolling(250, min_periods=200).max().iloc[-1]
    eod = pd.DataFrame({"close": C.iloc[-1], "ma20": ma20, "ma50": ma50, "ma200": ma200, "hi20": hi20, "lo20": lo20,
                        "vol": V.iloc[-1], "vavg20": vavg20, "mom6": mom6, "ret20": ret20, "hi52": hi52,
                        "ret1": C.iloc[-1] / C.iloc[-2] - 1})
    out = {"eod_date": str(last.date()), "universe": len(cols)}

    # --- today (live) ---
    q = (live or {}).get("quotes", {})
    day = []
    nifty = q.get("Nifty 50")
    n_pct = (nifty["ltp"] / nifty["prev_close"] - 1) if nifty and nifty.get("prev_close") else None
    now = pd.Timestamp.now(tz="Asia/Kolkata")
    frac = min(1.0, max(0.05, ((now.hour * 60 + now.minute) - 555) / 375)) if (live or {}).get("market_open") else 1.0
    for s in cols:
        v = q.get(s)
        if not v or not v.get("ltp") or not v.get("prev_close"):
            continue
        pct = v["ltp"] / v["prev_close"] - 1
        rv = (v.get("volume") or 0) / (eod.at[s, "vavg20"] * frac) if eod.at[s, "vavg20"] and eod.at[s, "vavg20"] > 0 else None
        day.append({"symbol": s, "ltp": _r(v["ltp"]), "pct": _r(100 * pct), "vs_nifty": _r(100 * (pct - (n_pct or 0))),
                    "rel_volume": _r(rv, 1), "gap_pct": _r(100 * (v["open"] / v["prev_close"] - 1)) if v.get("open") else None,
                    "above_vwap": (v["ltp"] > v["avg_price"]) if v.get("avg_price") else None})
    if day:
        dd = sorted(day, key=lambda r: -r["vs_nifty"])
        act = [r for r in dd if (r["rel_volume"] or 0) >= 1.0]
        out["day"] = {"as_of": (live or {}).get("at"), "market_open": (live or {}).get("market_open"),
                      "strong": act[:10], "weak": act[::-1][:10],
                      "labels": {"strong": WATCH, "weak": TESTED},
                      "notes": {"strong": "Today's leaders tend to edge ahead the next day (+0.12% vs peers) but fade over 5 days.",
                                "weak": "Today's laggards tend to keep lagging: -0.19% next day, -0.31% next 5 days vs peers "
                                        "(tested 2009-26, both halves). Small edge - useful for avoiding longs / picking shorts."}}
    # --- swing (EOD) ---
    e = eod.dropna(subset=["close", "ma50"])
    vr = e["vol"] / e["vavg20"]
    brk = e[(e["close"] > e["hi20"]) & (vr >= 1.5) & (e["close"] > e["ma50"])]
    bdn = e[(e["close"] < e["lo20"]) & (vr >= 1.5) & (e["close"] < e["ma50"])]
    pull = e[(e["close"] > e["ma50"]) & (e["ma50"] > e["ma200"]) & (e["close"] <= e["ma20"] * 1.01) & (e["close"] >= e["ma50"])]
    row = lambda s, r, why: {"symbol": s, "close": _r(r["close"]), "ret20_pct": _r(100 * r["ret20"]),
                             "vol_x": _r(r["vol"] / r["vavg20"], 1) if r["vavg20"] else None, "why": why}
    out["swing"] = {
        "breakouts": [row(s, r, "20-day high on 1.5x volume, above 50-day avg") for s, r in brk.sort_values("ret20", ascending=False).head(10).iterrows()],
        "breakdowns": [row(s, r, "20-day low on 1.5x volume, below 50-day avg") for s, r in bdn.sort_values("ret20").head(10).iterrows()],
        "pullbacks": [row(s, r, "uptrend (50 > 200-day avg) pulled back to the 20-day avg") for s, r in pull.sort_values("mom6", ascending=False).head(10).iterrows()],
        "labels": {"breakouts": CONTEXT, "breakdowns": CONTEXT, "pullbacks": CONTEXT},
        "notes": {"breakouts": "Careful: on NSE, 20-day breakouts on volume lagged peers by 0.2% over the next 5 days (2009-26). Do not chase; wait for a retest.",
                  "breakdowns": "Breakdowns did not keep falling on average (no edge).",
                  "pullbacks": "Not tested yet - a setup list, not a signal."}}
    # --- positional (EOD) ---
    p = eod.dropna(subset=["mom6", "ma200"])
    lead = p[p["close"] > p["ma200"]].sort_values("mom6", ascending=False).head(15)
    lag = p[p["close"] < p["ma200"]].sort_values("mom6").head(15)
    prow = lambda s, r: {"symbol": s, "close": _r(r["close"]), "mom6_pct": _r(100 * r["mom6"]),
                         "from_52w_high_pct": _r(100 * (r["close"] / r["hi52"] - 1)) if r["hi52"] else None}
    out["positional"] = {"leaders": [prow(s, r) for s, r in lead.iterrows()], "laggards": [prow(s, r) for s, r in lag.iterrows()],
                         "labels": {"leaders": CONTEXT, "laggards": WATCH},
                         "notes": {"leaders": "6-month leaders did not beat peers reliably (no edge on NSE 2009-26).",
                                   "laggards": "6-month laggards kept lagging in 2018-26 (-0.5% per 20 days) but not before 2018."}}
    # --- breadth of the F&O universe ---
    out["breadth"] = {"above_50dma_pct": _r(100 * (e["close"] > e["ma50"]).mean(), 0),
                      "above_200dma_pct": _r(100 * (p["close"] > p["ma200"]).mean(), 0),
                      "advancers_today": sum(1 for r in day if r["pct"] > 0) if day else None,
                      "decliners_today": sum(1 for r in day if r["pct"] < 0) if day else None}
    return out


# ------------------------------------------------------------------ playbook (per index)
def playbook(m: str, mk: dict, events: dict | None, catalog: list, options: list, macro: list) -> dict:
    x = mk.get("exposure_regime") or {}
    cv = x.get("candidate_v2") or {}
    dec = mk.get("decision") or {}
    nd = mk.get("next_days") or {}
    lv = mk.get("levels") or {}
    mtf = {r.get("tf"): r.get("trend") for r in ((mk.get("mtf") or {}).get("rows") or [])}
    sup = [s["price"] for s in (lv.get("supports") or [])][:2]
    res = [s["price"] for s in (lv.get("resistances") or [])][:2]
    strat = [{"strategy": c.get("strategy"), "group": c.get("group"), "action": c.get("action"), "lots": c.get("lots"),
              "reason": c.get("reason"), "record": c.get("registry")}
             for c in (catalog or []) if c.get("market") == m or (m == "NIFTY" and c.get("market") is None)]
    deploy = [s for s in strat if str(s["action"]).lower().startswith(("run", "paper: enter", "paper: conditions ok", "enter"))]
    avoid = [s for s in strat if str(s["action"]).lower().startswith(("do not run", "pause", "paper: skip"))]
    warn = [{"text": t, "label": TESTED, "means": "high volatility more likely within 10 sessions"} for t in cv.get("risk_warnings", [])]
    warn += [{"text": t, "label": TESTED, "means": "a Bear / High-vol phase more likely to end within 10 sessions"} for t in cv.get("relief_warnings", [])]
    if cv.get("bear_confirmed"):
        warn.append({"text": f"Bear confirmed ({cv.get('bear_signals')}/3 trend signals down)", "label": TESTED,
                     "means": "paper engine v2 halves size"})
    return {"market": m, "regime": x.get("regime"), "days_in_regime": x.get("days_in_regime"),
            "size_today": x.get("size_blend"), "size_v2_paper": cv.get("size_v2"),
            "timeframes": mtf, "range_3d": (nd.get("range") or {}).get("3"), "range_5d": (nd.get("range") or {}).get("5"),
            "big_move": nd.get("big_move"), "do": dec.get("do"), "supports": sup, "resistances": res,
            "warnings": warn, "deploy": deploy[:6], "avoid": avoid[:6], "events": events,
            "close": x.get("close"), "bar": x.get("bar")}


def _news():
    n = _load_json(os.path.join(ROOT, "data_store", "agent", "news.json"))
    if not n:
        return None
    keep = lambda xs: [{k: g.get(k) for k in ("title", "source", "sources", "n_sources", "severity", "published", "link", "new")} for g in (xs or [])]
    an = _load_json(os.path.join(ROOT, "data_store", "agent", "out", "news.json"))
    return {"at": n.get("at"), "shock": n.get("shock"), "analysis": an, "top": keep(n.get("top"))[:15],
            "shock_items": keep(n.get("shock_items"))[:6], "announcements": keep(n.get("nse_announcements"))[:12],
            "errors": n.get("errors"), "sentiment": n.get("sentiment"), "label": "context"}


def _governance():
    """Governor + Learner block for the Desk tab: guard verdicts, halt, scorecard, latest lessons, proposals waiting."""
    import re as _re
    ag = os.path.join(ROOT, "data_store", "agent")
    g = _load_json(os.path.join(ag, "logs", "guard_report.json")) or {}
    gov = _load_json(os.path.join(ag, "out", "governance.json")) or {}
    sc = _load_json(os.path.join(ag, "logs", "learning_scorecard.json")) or {}
    halt = _load_json(os.path.join(ag, "memory", "halt.json")) or {}
    lessons, props, calls_today, runs_today = [], 0, 0, 0
    try:
        txt = open(os.path.join(ag, "memory", "desk_memory.md")).read()
        lessons = [l[2:] for l in txt.splitlines() if l.startswith("- [L-") and "status: active" in l][:6]
    except Exception:
        pass
    try:
        props = len(_re.findall(r"(?m)^## P-\d+.*status: pending\s*$", open(os.path.join(ag, "memory", "proposals.md")).read()))
    except Exception:
        pass
    today = str(pd.Timestamp.now(tz="Asia/Kolkata").date())
    try:
        calls_today = sum(1 for l in open(os.path.join(ag, "logs", "agent_calls.csv")) if l.split(",")[1:2] and l.split(",")[1].startswith(today))
    except Exception:
        pass
    try:
        runs_today = sum(1 for l in open(os.path.join(ag, "logs", "agent_log.jsonl")) if f'"ts": "{today}' in l)
    except Exception:
        pass
    files = {k: {"verdict": v.get("verdict"), "issues": [i["msg"] for i in v.get("issues", [])][:4]}
             for k, v in (g.get("files") or {}).items()} if g.get("date") == today else {}
    return {"date": today, "guard": files, "guard_summary": g.get("summary") if g.get("date") == today else None,
            "verdict": gov.get("verdict"), "verdict_at": gov.get("as_of"), "halt": halt if halt.get("active") else None,
            "scorecard": sc.get("directional"), "range": sc.get("range"), "no_trade": sc.get("no_trade"),
            "by_persona": sc.get("by_persona"), "lessons": lessons, "proposals_pending": props,
            "calls_today": calls_today, "runs_today": runs_today}


def overnight(macro: list) -> list:
    keep = {"India VIX", "USD / INR", "US 10Y yield", "S&P 500", "Brent crude", "US VIX", "Dollar index",
            "FII index-futures long %", "Nifty futures basis"}
    out = []
    for t in macro or []:
        if t.get("label") in keep:
            out.append({"label": t["label"], "value": t.get("value"), "change_5d": t.get("change_5d"),
                        "direction": t.get("direction"), "as_of": t.get("as_of"),
                        "tag": CONTEXT if t["label"] != "US 10Y yield" else WATCH})
    return out


def build(markets: dict, events: dict | None, catalog: list, options: list, macro: list, fii: dict | None) -> dict:
    live = _load_json(os.path.join(IND, "live_quotes.json"))
    univ = _load_json(os.path.join(IND, "universe.json"))
    notes = _load_json(os.path.join(ROOT, "data_store", "agent", "desk_notes.json"))
    st = _load_json(os.path.join(IND, "status.json"))
    out = {"generated": str(pd.Timestamp.now(tz="Asia/Kolkata").floor("s")),
           "playbook": {m: playbook(m, markets[m], events, catalog, options, macro) for m in ("NIFTY", "BANKNIFTY") if m in markets},
           "overnight": overnight(macro), "fii_dii": fii, "status": st,
           "agent_notes": notes, "news": _news(), "health": _load_json(os.path.join(ROOT, "data_store", "agent", "health.json")), "governance": None}
    for k, fn in (("governance", _governance), ("sectors", lambda: sectors(live)), ("stocks", lambda: stocks(live, univ))):
        try:
            out[k] = fn()
        except Exception as e:
            out[k] = {"error": str(e)[:200]}
    return out
