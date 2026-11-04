"""
Dhan market data for Market Pulse (2026-10-23) - replaces Upstox for live prices.
Uses the Dhan login the Nifty tracker already keeps alive (~/Documents/nifty-live-tracker/.dhan_token; renewed daily by
its keep-alive). The token is read from the file, never printed or logged. Read-only: nothing here places orders.
  quote(req)      POST /v2/marketfeed/quote  {segment: [security ids]} -> last price, day OHLC, volume, OI, net change
  ids_index()     NSE index name (lower case) -> Dhan security id (IDX_I)
  ids_equity()    NSE symbol -> Dhan security id (NSE_EQ)
  near_futures()  {"NIFTY": id, "BANKNIFTY": id} nearest index futures (NSE_FNO)
Dhan allows about one market-quote call a second: every call here goes through one shared limiter.
"""
from __future__ import annotations

import csv
import json
import os
import threading
import time
import urllib.request
from datetime import date, datetime

HOME = os.path.expanduser("~")
TOKEN_FILE = os.path.join(os.environ.get("MP_NIFTY_TRACKER_DIR") or os.path.join(HOME, "Documents", "nifty-live-tracker"), ".dhan_token")
ENV_FILE = os.path.join(os.environ.get("MP_DHAN_DIR") or os.path.join(HOME, "Documents", "dhan"), ".env")
MASTER = os.path.join(os.environ.get("MP_NIFTY_TRACKER_DIR") or os.path.join(HOME, "Documents", "nifty-live-tracker"), "data", "dhan-scrip-master.csv")
API = "https://api.dhan.co/v2"
_LOCK = threading.Lock()
_LAST = [0.0]
_CACHE: dict = {}
ALIASES = {"nifty fin service": "finnifty", "nifty smlcap 100": "nifty smallcap 100", "nifty 50": "nifty", "nifty bank": "banknifty"}


def creds() -> tuple[str, str] | None:
    try:
        d = json.load(open(TOKEN_FILE))
        exp = str(d.get("expiry") or "").strip()
        # 2026-11-01: Dhan writes the expiry as "DD/MM/YYYY HH:MM"; the old text compare treated every such token as expired
        exp_dt = None
        for fmt in ("%d/%m/%Y %H:%M", "%d/%m/%Y %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M"):
            try:
                exp_dt = datetime.strptime(exp[:19], fmt); break
            except ValueError:
                continue
        if exp_dt is not None and exp_dt < datetime.now():
            return None
        if d.get("client_id") and d.get("access_token"):
            return str(d["client_id"]).strip(), str(d["access_token"]).strip()
    except Exception:
        pass
    try:
        env = dict(l.strip().split("=", 1) for l in open(ENV_FILE) if "=" in l and not l.startswith("#"))
        cid, tok = env.get("DHAN_CLIENT_ID", "").strip('"\' '), env.get("DHAN_ACCESS_TOKEN", "").strip('"\' ')
        return (cid, tok) if cid and tok else None
    except Exception:
        return None


def ok() -> bool:
    return creds() is not None


def _post(path: str, body: dict, timeout: float = 15) -> dict:
    c = creds()
    if not c:
        raise RuntimeError("no valid Dhan login (renew it with Dhan-Login on the Desktop)")
    with _LOCK:                                   # ~1 call a second across the whole app
        wait = 1.05 - (time.time() - _LAST[0])
        if wait > 0:
            time.sleep(wait)
        req = urllib.request.Request(API + path, data=json.dumps(body).encode(), method="POST",
                                     headers={"access-token": c[1], "client-id": c[0], "Content-Type": "application/json", "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                out = json.loads(r.read().decode())
        finally:
            _LAST[0] = time.time()
    if out.get("status") not in (None, "success"):
        raise RuntimeError(f"Dhan: {str(out)[:120]}")
    return out.get("data") or {}


def quote(req: dict) -> dict:
    """{segment: [ids]} -> {segment: {id(str): row}}; splits big requests (1000 per call)."""
    out: dict = {}
    flat = [(seg, i) for seg, ids in req.items() for i in ids]
    for k in range(0, len(flat), 900):
        part: dict = {}
        for seg, i in flat[k:k + 900]:
            part.setdefault(seg, []).append(int(i))
        for seg, rows in _post("/marketfeed/quote", part).items():
            out.setdefault(seg, {}).update(rows or {})
    return out


def _master():
    m = _CACHE.get("master")
    try:
        mt = os.path.getmtime(MASTER)
    except OSError:
        return {"idx": {}, "eq": {}, "fut": []}
    if m and m[0] == mt:
        return m[1]
    idx, eq, fut = {}, {}, []
    with open(MASTER, newline="") as f:
        for r in csv.DictReader(f):
            if r.get("EXCH_ID") != "NSE":
                continue
            seg = r.get("SEGMENT")
            if seg == "I":
                for k in (r.get("DISPLAY_NAME"), r.get("SYMBOL_NAME")):
                    if k:
                        idx.setdefault(k.strip().lower(), r["SECURITY_ID"])
            elif seg == "E" and r.get("SERIES") == "EQ":
                eq[(r.get("UNDERLYING_SYMBOL") or "").strip()] = r["SECURITY_ID"]
            elif seg == "D" and r.get("INSTRUMENT") == "FUTIDX" and r.get("UNDERLYING_SYMBOL") in ("NIFTY", "BANKNIFTY"):
                fut.append((r["UNDERLYING_SYMBOL"], r.get("SM_EXPIRY_DATE", "")[:10], r["SECURITY_ID"]))
                try:
                    _CACHE.setdefault("lots", {})[r["SECURITY_ID"]] = int(float(r.get("LOT_SIZE") or 0)) or None
                except ValueError:
                    pass
    res = {"idx": idx, "eq": eq, "fut": fut}
    _CACHE["master"] = (mt, res)
    return res


def ids_index(name: str) -> str | None:
    k = name.strip().lower()
    return _master()["idx"].get(ALIASES.get(k, k))


def ids_equity(sym: str) -> str | None:
    return _master()["eq"].get(sym)


def near_futures() -> dict:
    today = str(date.today())
    out = {}
    for u in ("NIFTY", "BANKNIFTY"):
        c = sorted((e, i) for uu, e, i in _master()["fut"] if uu == u and e >= today)
        if c:
            out[u] = c[0][1]
    return out


def row_fields(r: dict) -> dict:
    """Dhan quote row -> the fields Market Pulse uses."""
    o = r.get("ohlc") or {}
    lp, nc = r.get("last_price"), r.get("net_change")
    prev = (lp - nc) if lp is not None and nc is not None else o.get("close")
    return {"ltp": lp, "open": o.get("open"), "high": o.get("high"), "low": o.get("low"), "prev_close": prev,
            "volume": r.get("volume"), "avg_price": r.get("average_price"), "oi": r.get("oi"), "ts": r.get("last_trade_time"),
            "buy_qty": r.get("buy_quantity"), "sell_qty": r.get("sell_quantity")}


# ---------------------------------------------------------------------------- history (2026-10-24: replaces Upstox history)
from datetime import timedelta, timezone

IST_TZ = timezone(timedelta(hours=5, minutes=30))


def _bars(d: dict, intraday: bool) -> list[dict]:
    """Dhan chart arrays -> [{ts (IST-aware datetime), open, high, low, close, volume, oi}]."""
    ts = d.get("timestamp") or d.get("start_Time") or []
    if not ts:
        return []
    ts = [t / 1000.0 if t > 1e12 else float(t) for t in ts]
    as_utc = [datetime.fromtimestamp(t, tz=timezone.utc).astimezone(IST_TZ) for t in ts]
    if intraday:                    # some Dhan builds encode IST wall clock as epoch: pick whichever lands in NSE hours
        as_raw = [datetime.fromtimestamp(t, tz=timezone.utc).replace(tzinfo=IST_TZ) for t in ts]
        inside = lambda xs: sum(1 for x in xs if 9 * 60 <= x.hour * 60 + x.minute <= 15 * 60 + 45)
        stamps = as_raw if inside(as_raw) > inside(as_utc) else as_utc
    else:
        stamps = as_utc
    o, h, l, c = d.get("open", []), d.get("high", []), d.get("low", []), d.get("close", [])
    v = d.get("volume") or [0] * len(ts)
    oi = d.get("open_interest") or [0] * len(ts)
    out = []
    for i, t in enumerate(stamps):
        try:
            out.append({"ts": t, "open": float(o[i]), "high": float(h[i]), "low": float(l[i]), "close": float(c[i]),
                        "volume": float(v[i] or 0), "oi": float(oi[i] or 0)})
        except (IndexError, TypeError, ValueError):
            continue
    return out


def intraday(seg: str, sid, inst: str, start: date, end: date, mins: int = 5, oi: bool = True) -> list[dict]:
    """Intraday bars (1/5/15/25/60 min) from start to end inclusive, 85-day chunks."""
    out, cur = [], datetime.combine(start, datetime.min.time())
    stop = datetime.combine(end, datetime.min.time()) + timedelta(days=1)
    while cur < stop:
        nxt = min(stop, cur + timedelta(days=85))
        body = {"securityId": str(sid), "exchangeSegment": seg, "instrument": inst, "interval": str(mins), "oi": bool(oi),
                "fromDate": cur.strftime("%Y-%m-%d %H:%M:%S"), "toDate": nxt.strftime("%Y-%m-%d %H:%M:%S")}
        out += _bars(_post_raw("/charts/intraday", body), intraday=True)
        cur = nxt
    seen = {}
    for b in out:
        seen[b["ts"]] = b
    return [seen[k] for k in sorted(seen)]


def daily(seg: str, sid, inst: str, start: date, end: date) -> list[dict]:
    """Daily bars; indices sometimes refuse the daily endpoint, then daily is built from 60-min bars."""
    to = min(end + timedelta(days=1), date.today())
    base = {"securityId": str(sid), "exchangeSegment": seg, "instrument": inst,
            "fromDate": start.strftime("%Y-%m-%d"), "toDate": to.strftime("%Y-%m-%d")}
    for body in (dict(base, expiryCode=0, oi=False), dict(base, oi=False), dict(base)):
        try:
            rows = _bars(_post_raw("/charts/historical", body), intraday=False)
            for r in rows:
                r["ts"] = r["ts"].replace(hour=0, minute=0, second=0)
            return rows
        except Exception as e:
            if "HTTP Error 4" not in str(e) and "DH-9" not in str(e):
                raise
    per = {}
    for b in intraday(seg, sid, inst, start, end, 60, oi=False):
        k = b["ts"].date()
        if k not in per:
            per[k] = dict(b, ts=b["ts"].replace(hour=0, minute=0))
            continue
        x = per[k]
        x["high"], x["low"], x["close"] = max(x["high"], b["high"]), min(x["low"], b["low"]), b["close"]
        x["volume"] += b["volume"]
    return [per[k] for k in sorted(per)]


def _post_raw(path: str, body: dict) -> dict:
    """Chart endpoints return the arrays at the top level (not under 'data')."""
    c = creds()
    if not c:
        raise RuntimeError("no valid Dhan login (renew it with Dhan-Login on the Desktop)")
    with _LOCK:
        wait = 0.35 - (time.time() - _LAST[0])          # data APIs allow a few calls a second
        if wait > 0:
            time.sleep(wait)
        req = urllib.request.Request(API + path, data=json.dumps(body).encode(), method="POST",
                                     headers={"access-token": c[1], "client-id": c[0], "Content-Type": "application/json", "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                out = json.loads(r.read().decode())
        finally:
            _LAST[0] = time.time()
    return out.get("data", out) if isinstance(out, dict) else {}


def futures_contracts() -> list[dict]:
    """NIFTY / BANKNIFTY index futures listed now, in the same shape the app always used."""
    m = _master()
    lots = _CACHE.get("lots") or {}
    out = []
    for u, exp, sid in m["fut"]:
        try:
            d = date.fromisoformat(exp)
        except ValueError:
            continue
        out.append({"underlying": u, "instrument_key": f"NSE_FO|{sid}", "security_id": str(sid), "expiry": exp,
                    "lot_size": lots.get(sid), "contract": f"{u}_FUT_{d:%b}{d:%y}".upper()})
    return sorted(out, key=lambda r: (r["underlying"], r["expiry"]))


def fno_stocks() -> set:
    """Stocks that have NSE futures (the F&O list)."""
    s = _CACHE.get("fno")
    if s is None:
        s = set()
        try:
            with open(MASTER, newline="") as f:
                for r in csv.DictReader(f):
                    if r.get("EXCH_ID") == "NSE" and r.get("SEGMENT") == "D" and r.get("INSTRUMENT") == "FUTSTK":
                        s.add((r.get("UNDERLYING_SYMBOL") or "").strip())
        except Exception:
            pass
        _CACHE["fno"] = s
    return s


def equity_names() -> dict:
    e = _CACHE.get("eqn")
    if e is None:
        e = {}
        try:
            with open(MASTER, newline="") as f:
                for r in csv.DictReader(f):
                    if r.get("EXCH_ID") == "NSE" and r.get("SEGMENT") == "E" and r.get("SERIES") == "EQ":
                        e[(r.get("UNDERLYING_SYMBOL") or "").strip()] = (r["SECURITY_ID"], r.get("DISPLAY_NAME") or r.get("SYMBOL_NAME"))
        except Exception:
            pass
        _CACHE["eqn"] = e
    return e


def index_rows() -> list[tuple[str, str, str]]:
    """(security id, display name, symbol name) for every NSE index."""
    r_ = _CACHE.get("idxrows")
    if r_ is None:
        r_ = []
        with open(MASTER, newline="") as f:
            for r in csv.DictReader(f):
                if r.get("EXCH_ID") == "NSE" and r.get("SEGMENT") == "I":
                    r_.append((r["SECURITY_ID"], r.get("DISPLAY_NAME") or "", r.get("SYMBOL_NAME") or ""))
        _CACHE["idxrows"] = r_
    return r_
