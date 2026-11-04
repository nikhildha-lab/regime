"""
Company results for the Positional persona (2026-10-17). Runs every Market Pulse cycle on the Mac. Read-only.

  results_calendar.json  F&O stocks with a board meeting for financial results in the next 21 days (NSE event calendar)
  results/<SYMBOL>.json  NSE quarterly results comparison (last ~5 quarters), raw, refreshed weekly or after results
  results_summary.json   per stock: latest quarter sales / net profit, growth vs a year ago (or vs last quarter),
                         margin, and a simple results trend: improving / steady / weakening  (CONTEXT, untested)
Fetches a few stocks per cycle so NSE is not hammered; the full F&O list fills in over a few hours the first time.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta

import updater as U

ROOT = U.ROOT
D = os.path.join(ROOT, "data_store", "india")
RD = os.path.join(D, "results")
UNIV = os.path.join(D, "universe.json")
CAL = os.path.join(D, "results_calendar.json")
SUMM = os.path.join(D, "results_summary.json")
NSE = "https://www.nseindia.com"
PER_CYCLE = 8


def _nse_json(path):
    s = U._CSESSION                       # 2026-10-23 audit: plain requests get HTTP 403 from NSE; the browser-like session works (FII/DII uses it)
    if s is not None:
        s.get(NSE + "/", timeout=20)
        r = s.get(NSE + path, headers={"Accept": "application/json", "Referer": NSE + "/"}, timeout=25)
        if r.status_code != 200:
            raise RuntimeError(f"NSE HTTP {r.status_code}")
        return r.json()
    U.http_get(NSE + "/", headers={"Accept": "text/html"}, timeout=15, tries=1)        # cookie
    raw = U.http_get(NSE + path, headers={"Accept": "application/json", "Referer": NSE + "/"}, timeout=25, tries=2)
    return json.loads(raw)


def calendar(symbols: set) -> dict:
    if os.path.exists(CAL) and time.time() - os.path.getmtime(CAL) < 6 * 3600:
        return json.load(open(CAL))
    rows = _nse_json("/api/event-calendar")
    rows = rows if isinstance(rows, list) else rows.get("data", [])
    out = []
    for r in rows:
        sym, purpose, dt = r.get("symbol"), str(r.get("purpose") or r.get("bm_purpose") or ""), r.get("date") or r.get("bm_date")
        if symbols and sym not in symbols:
            continue
        if "result" not in purpose.lower():
            continue
        try:
            d = datetime.strptime(dt, "%d-%b-%Y").date()
        except Exception:
            continue
        if datetime.now().date() - timedelta(days=3) <= d <= datetime.now().date() + timedelta(days=21):
            out.append({"symbol": sym, "date": str(d), "purpose": purpose[:80]})
    out.sort(key=lambda x: x["date"])
    c = {"at": datetime.now().strftime("%Y-%m-%d %H:%M"), "upcoming": out}
    with open(CAL, "w") as f:
        json.dump(c, f, indent=1)
    return c


def _num(x):
    try:
        return float(str(x).replace(",", ""))
    except Exception:
        return None


def _pick(row: dict, *needles):
    for k, v in row.items():
        kl = k.lower()
        if all(n in kl for n in needles):
            val = _num(v)
            if val is not None:
                return val
    return None


def summarise(sym: str, raw) -> dict | None:
    rows = raw.get("resCmpData") if isinstance(raw, dict) else raw
    if not isinstance(rows, list) or not rows:
        return None
    q = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        sales = _pick(r, "net_sale") or _pick(r, "total_income") or _pick(r, "income")
        profit = _pick(r, "pro_loss_aft_tax") or _pick(r, "net_profit") or _pick(r, "profit")
        per = r.get("re_to_dt") or r.get("toDate") or r.get("to_date")
        q.append({"period": per, "sales": sales, "profit": profit})
    q = [x for x in q if x["period"]]
    try:
        q.sort(key=lambda x: datetime.strptime(x["period"], "%d-%b-%Y"), reverse=True)
    except Exception:
        pass
    if not q or q[0]["sales"] is None:
        return {"symbol": sym, "parse_ok": False}
    cur = q[0]
    try:                                   # 2026-10-23 audit: NSE's comparison API can return quarters that are years old - never show those
        if (datetime.now() - datetime.strptime(cur["period"], "%d-%b-%Y")).days > 200:
            return {"symbol": sym, "parse_ok": False, "stale": cur["period"]}
    except Exception:
        pass
    base = q[4] if len(q) >= 5 else (q[1] if len(q) >= 2 else None)
    basis = "vs a year ago" if len(q) >= 5 else "vs last quarter"
    g = lambda a, b: None if a is None or b in (None, 0) else round(100 * (a / abs(b) - 1), 1)
    sg = g(cur["sales"], base["sales"]) if base else None
    pg = g(cur["profit"], base["profit"]) if base and cur["profit"] is not None else None
    margin = round(100 * cur["profit"] / cur["sales"], 1) if cur["profit"] is not None and cur["sales"] else None
    trend = "not enough data"
    if sg is not None and pg is not None:
        trend = "improving" if sg > 8 and pg > 10 else "weakening" if (sg < 0 or pg < -10) else "steady"
    return {"symbol": sym, "parse_ok": True, "quarter_end": cur["period"], "sales": cur["sales"], "profit": cur["profit"],
            "sales_growth_pct": sg, "profit_growth_pct": pg, "growth_basis": basis, "margin_pct": margin,
            "trend": trend, "quarters": len(q), "label": "context"}


def run_all() -> dict:
    os.makedirs(RD, exist_ok=True)
    univ = json.load(open(UNIV)) if os.path.exists(UNIV) else {}
    syms = sorted((univ.get("stocks") or {}).keys())
    res = {}
    try:
        cal = calendar(set(syms))
        res["calendar"] = len(cal["upcoming"])
    except Exception as e:
        cal = {"upcoming": []}
        res["calendar_error"] = str(e)[:120]
    recent = {x["symbol"] for x in cal.get("upcoming", []) if x["date"] <= datetime.now().strftime("%Y-%m-%d")}
    def stale(s):
        p = os.path.join(RD, f"{s}.json")
        return (not os.path.exists(p)) or time.time() - os.path.getmtime(p) > 7 * 86400 or \
               (s in recent and time.time() - os.path.getmtime(p) > 86400)
    todo = [s for s in syms if stale(s)][:PER_CYCLE]
    errs = []
    for s in todo:
        try:
            raw = _nse_json(f"/api/results-comparision?symbol={s}")
            with open(os.path.join(RD, f"{s}.json"), "w") as f:
                json.dump(raw, f)
        except Exception as e:
            errs.append(f"{s}: {str(e)[:60]}")
        time.sleep(1.0)
    summ = {}
    for s in syms:
        p = os.path.join(RD, f"{s}.json")
        if os.path.exists(p):
            try:
                x = summarise(s, json.load(open(p)))
                if x:
                    summ[s] = x
            except Exception:
                pass
    with open(SUMM, "w") as f:
        json.dump({"at": datetime.now().strftime("%Y-%m-%d %H:%M"), "stocks": summ,
                   "coverage": f"{len(summ)}/{len(syms)}", "errors": errs[:5]}, f, indent=1)
    res.update(fetched=len(todo) - len(errs), coverage=f"{len(summ)}/{len(syms)}", errors=errs[:3])
    return res


JOBS = [("results", run_all)]

if __name__ == "__main__":
    print(json.dumps(run_all(), indent=1))
