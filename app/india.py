"""
India desk data (added 2026-10-17; Dhan since 2026-10-24). Read-only downloads, never places orders. Run by updater.update_all().

  universe     Dhan scrip master: NSE sector indices + F&O stocks with their Dhan security ids
  sectors_day  daily candles of every sector index (Dhan charts), topped up each day
  live_quotes  last price, day OHLC, previous close, volume for all sector indices + F&O stocks (Dhan market quote)
Files: data_store/india/{universe.json, sectors_day.csv, live_quotes.json, status.json}
Needs the Dhan login the Nifty tracker keeps alive (Dhan-Login on the Desktop). Without it: logs and skips.
"""
from __future__ import annotations

import json
import os
import time
from datetime import date, datetime, timedelta

import pandas as pd

import updater as U

ROOT = U.ROOT
OUT = os.path.join(ROOT, "data_store", "india")
UNIV = os.path.join(OUT, "universe.json")
SECT = os.path.join(OUT, "sectors_day.csv")
LIVE = os.path.join(OUT, "live_quotes.json")
STATUS = os.path.join(OUT, "status.json")

# sector / broad indices we track (matched to Dhan index names ignoring case, spaces, '&', '-')
SECTORS = {
    "Nifty 50": ["NIFTY50"], "Nifty Bank": ["NIFTYBANK"], "Nifty IT": ["NIFTYIT"], "Nifty Pharma": ["NIFTYPHARMA"],
    "Nifty Auto": ["NIFTYAUTO"], "Nifty FMCG": ["NIFTYFMCG"], "Nifty Metal": ["NIFTYMETAL"], "Nifty Realty": ["NIFTYREALTY"],
    "Nifty Energy": ["NIFTYENERGY"], "Nifty PSU Bank": ["NIFTYPSUBANK"], "Nifty Pvt Bank": ["NIFTYPVTBANK", "NIFTYPRIVATEBANK"],
    "Nifty Financial Services": ["NIFTYFINSERVICE", "NIFTYFINANCIALSERVICES", "FINNIFTY"], "Nifty Media": ["NIFTYMEDIA"],
    "Nifty Infra": ["NIFTYINFRA", "NIFTYINFRASTRUCTURE"], "Nifty Healthcare": ["NIFTYHEALTHCAREINDEX", "NIFTYHEALTHCARE"],
    "Nifty Oil & Gas": ["NIFTYOILGAS", "NIFTYOILANDGAS"], "Nifty Consumer Durables": ["NIFTYCONSUMERDURABLES", "NIFTYCONSRDURBL"],
    "Nifty CPSE": ["NIFTYCPSE"], "Nifty PSE": ["NIFTYPSE"], "Nifty Commodities": ["NIFTYCOMMODITIES"],
    "Nifty Midcap 100": ["NIFTYMIDCAP100"], "Nifty Smallcap 100": ["NIFTYSMLCAP100", "NIFTYSMALLCAP100"],
    "Nifty Chemicals": ["NIFTYCHEMICALS"], "Nifty Capital Markets": ["NIFTYCAPITALMKT", "NIFTYCAPITALMARKETS"],
    "Nifty Defence": ["NIFTYINDDEFENCE", "NIFTYINDIADEFENCE"],
    "India VIX": ["INDIAVIX"],
}


def _norm(s) -> str:
    return "".join(ch for ch in str(s).upper() if ch.isalnum())


def _status(key, val):
    os.makedirs(OUT, exist_ok=True)
    st = json.load(open(STATUS)) if os.path.exists(STATUS) else {}
    st[key] = {"at": str(datetime.now().replace(microsecond=0)), **val}
    with open(STATUS + ".tmp", "w") as f:
        json.dump(st, f, indent=1)
    os.replace(STATUS + ".tmp", STATUS)




def universe(refresh_hours: float = 12) -> dict:
    if os.path.exists(UNIV) and time.time() - os.path.getmtime(UNIV) < refresh_hours * 3600:
        u = json.load(open(UNIV))
        if u.get("source") == "Dhan":
            return u
    import dhan_live as DL
    want = {a: name for name, al in SECTORS.items() for a in al}
    sectors = {}
    for sid, disp, sym in DL.index_rows():
        for cand in (disp, sym):
            nm = want.get(_norm(cand))
            if nm and nm not in sectors:
                sectors[nm] = f"IDX_I|{sid}"
    eqn, fno = DL.equity_names(), DL.fno_stocks()
    stocks = {s_: {"key": f"NSE_EQ|{eqn[s_][0]}", "name": eqn[s_][1]} for s_ in sorted(fno) if s_ in eqn}
    if not sectors or not stocks:
        raise RuntimeError("Dhan scrip master not found or empty")
    u = {"fetched": str(datetime.now().replace(microsecond=0)), "source": "Dhan", "sectors": sectors, "stocks": stocks,
         "missing_sectors": sorted(set(SECTORS) - set(sectors))}
    os.makedirs(OUT, exist_ok=True)
    with open(UNIV + ".tmp", "w") as f:
        json.dump(u, f, indent=1)
    os.replace(UNIV + ".tmp", UNIV)
    _status("universe", {"sectors": len(sectors), "stocks": len(stocks), "missing_sectors": u["missing_sectors"], "source": "Dhan"})
    return u


def _candles_days(key: str, start: date, end: date) -> list:
    """[[date, open, high, low, close], ...] daily for one index (Dhan)."""
    import dhan_live as DL
    seg, sid = key.split("|", 1)
    return [[str(b["ts"].date()), b["open"], b["high"], b["low"], b["close"]] for b in DL.daily(seg, sid, "INDEX", start, end)]


def sector_history(u: dict) -> dict:
    old = pd.read_csv(SECT) if os.path.exists(SECT) else pd.DataFrame(columns=["date", "sector", "open", "high", "low", "close"])
    today = date.today()
    frames, errs = [old], []
    for name, key in u["sectors"].items():
        have = old[old["sector"] == name]
        start = date(2019, 1, 1) if have.empty else (pd.to_datetime(have["date"]).max().date() - timedelta(days=7))
        if not have.empty and pd.to_datetime(have["date"]).max().date() >= today - timedelta(days=1) and datetime.now().hour < 16:
            continue
        try:
            rows, a = [], start
            while a <= today:
                b = min(a + timedelta(days=360), today)
                rows += _candles_days(key, a, b)
                a = b + timedelta(days=1)
            if rows:
                d = pd.DataFrame([r[:5] for r in rows], columns=["date", "open", "high", "low", "close"])
                d["date"] = d["date"].astype(str).str[:10]
                d.insert(1, "sector", name)
                frames.append(d)
        except Exception as e:
            errs.append(f"{name}: {str(e)[:100]}")
    allx = pd.concat(frames).drop_duplicates(["date", "sector"], keep="last").sort_values(["sector", "date"])
    allx.to_csv(SECT + ".tmp", index=False)
    os.replace(SECT + ".tmp", SECT)
    _status("sector_history", {"rows": len(allx), "errors": errs[:5]})
    return {"rows": len(allx), "errors": errs[:5]}


def market_open(now=None) -> bool:
    now = now or pd.Timestamp.now(tz=U.IST)
    return now.weekday() < 5 and (9 * 60 + 15) <= now.hour * 60 + now.minute <= (15 * 60 + 35)


def live_quotes_dhan(u: dict) -> dict:
    """Same output as the Upstox version, from Dhan's market-quote API (2026-10-23: Dhan replaces Upstox)."""
    import dhan_live as DL
    req, names = {"IDX_I": [], "NSE_EQ": []}, {}
    for k, v in u["sectors"].items():
        seg, sid = v.split("|", 1) if v.startswith("IDX_I|") else ("IDX_I", DL.ids_index(v.split("|", 1)[-1]))
        if sid:
            req["IDX_I"].append(sid); names[("IDX_I", str(sid))] = ("sector", k)
    for sym, v in u["stocks"].items():
        sid = v["key"].split("|", 1)[1] if str(v.get("key", "")).startswith("NSE_EQ|") and v["key"][7:].isdigit() else DL.ids_equity(sym)
        if sid:
            req["NSE_EQ"].append(sid); names[("NSE_EQ", str(sid))] = ("stock", sym)
    out, errs = {}, []
    # Dhan's "close" / "net change" are vs the previous close only while NSE is open; after the close they move to
    # today's close. So keep the previous close seen during today's session, and say "unknown" rather than show 0%.
    is_open, today = market_open(), str(datetime.now().date())
    old = {}
    try:
        oj = json.load(open(LIVE))
        if oj.get("source") == "Dhan" and str(oj.get("at", ""))[:10] == today:
            old = {k: v.get("prev_close") for k, v in (oj.get("quotes") or {}).items() if v.get("prev_ok")}
    except Exception:
        pass
    try:
        for seg, rows in DL.quote(req).items():
            for sid, r in (rows or {}).items():
                kind, name = names.get((seg, str(sid)), (None, None))
                if name:
                    f = DL.row_fields(r)
                    out[name] = {"kind": kind, **{k: f[k] for k in ("ltp", "open", "high", "low", "prev_close", "volume", "avg_price", "ts")}}
                    if is_open:
                        out[name]["prev_ok"] = True
                    else:
                        out[name]["prev_close"] = old.get(name)
                        out[name]["prev_ok"] = name in old
    except Exception as e:
        errs.append(str(e)[:150])
    if out:
        with open(LIVE + ".tmp", "w") as f:
            json.dump({"at": str(datetime.now().replace(microsecond=0)), "market_open": market_open(), "source": "Dhan", "quotes": out}, f)
        os.replace(LIVE + ".tmp", LIVE)
    _status("live_quotes", {"ok": bool(out), "n": len(out), "source": "Dhan", "errors": errs[:3]})
    return {"quotes": len(out), "source": "Dhan", "errors": errs[:3]}


def live_quotes(u: dict, force=False) -> dict:
    if not force and not market_open() and os.path.exists(LIVE) and time.time() - os.path.getmtime(LIVE) < 6 * 3600:
        return {"skipped": "market closed"}
    import dhan_live as DL
    if not DL.ok():
        _status("live_quotes", {"ok": False, "error": "no valid Dhan login - run Dhan-Login on the Desktop"})
        return {"skipped": "no Dhan login"}
    return live_quotes_dhan(u)


def run_all() -> dict:
    res = {}
    u = universe()
    res["universe"] = {"sectors": len(u["sectors"]), "stocks": len(u["stocks"]), "missing": u.get("missing_sectors", [])[:5]}
    try:
        res["sectors"] = sector_history(u)
    except Exception as e:
        res["sectors"] = {"error": str(e)[:200]}
    res["live"] = live_quotes(u)
    return res


JOBS = [("india", run_all)]

if __name__ == "__main__":
    print(json.dumps(run_all(), indent=1, default=str))
