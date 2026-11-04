"""Dhan (DhanHQ v2) data client - a drop-in for the parts of KiteConnect the
tracker uses:

    instruments("NSE" | "NFO")     -> Kite-shaped instrument rows
    historical_data(token, from, to, interval, oi=False)
                                   -> Kite-shaped OHLC(V/OI) dicts, IST naive

Tokens: Dhan identifies an instrument by (exchange segment, security id). The
tracker wants one int "instrument_token", so we encode
    token = SEGMENT_CODE * 10**10 + security_id
(e.g. NIFTY 50 index = IDX_I/13 -> 13; a NIFTY future = NSE_FNO/xxxxx -> 2e10+xxxxx)
and decode it again in the feed and in historical_data.

Docs: https://dhanhq.co/docs/v2/  (instruments, historical-data, annexure)
"""
from __future__ import annotations

import csv
import datetime as dt
import time
from pathlib import Path

import requests

from .config import ROOT

API = "https://api.dhan.co/v2"
SCRIP_URL = "https://images.dhan.co/api-data/api-scrip-master-detailed.csv"
SEG = {"IDX_I": 0, "NSE_EQ": 1, "NSE_FNO": 2, "NSE_CURRENCY": 3, "BSE_EQ": 4,
       "MCX_COMM": 5, "BSE_CURRENCY": 7, "BSE_FNO": 8}
SEG_NAME = {v: k for k, v in SEG.items()}
TOK_BASE = 10 ** 10
IST = dt.timedelta(hours=5, minutes=30)

# config index_symbol -> names Dhan may use for the same index
INDEX_ALIASES = {
    "NIFTY 50": ["NIFTY 50", "NIFTY"],
    "NIFTY BANK": ["NIFTY BANK", "BANKNIFTY", "BANK NIFTY"],
    "NIFTY FIN SERVICE": ["NIFTY FIN SERVICE", "FINNIFTY"],
    "NIFTY MID SELECT": ["NIFTY MID SELECT", "MIDCPNIFTY"],
}
INDEX_FALLBACK_IDS = {"NIFTY 50": 13, "NIFTY BANK": 25}   # Dhan IDX_I security ids
STOCKS: set = set()      # 2026-10-23: underlying symbols of configured stock instruments (set by instruments.resolve_all)


def enc(seg_name: str, secid) -> int:
    return SEG[seg_name] * TOK_BASE + int(secid)


def dec(token) -> tuple[str, str]:
    token = int(token)
    return SEG_NAME[token // TOK_BASE], str(token % TOK_BASE)


def _col(row: dict, *names: str) -> str:
    for n in names:
        v = row.get(n)
        if v not in (None, ""):
            return str(v).strip()
    return ""


def _date(v: str):
    try:
        return dt.datetime.strptime(v[:10], "%Y-%m-%d").date()
    except Exception:
        return None


class DhanError(RuntimeError):
    pass


class DhanClient:
    def __init__(self, client_id: str, access_token: str, timeout: float = 20.0):
        self.client_id = str(client_id).strip()
        self.token = str(access_token).strip()
        self.timeout = timeout
        self.s = requests.Session()
        self.s.headers.update({
            "access-token": self.token, "client-id": self.client_id,
            "Content-Type": "application/json", "Accept": "application/json",
        })
        self._nse: list | None = None
        self._nfo: list | None = None
        self._inst_type: dict[int, str] = {}
        self._last = 0.0

    # ------------------------------------------------------------------ http
    def _post(self, path: str, body: dict) -> dict:
        wait = 0.25 - (time.time() - self._last)          # stay well under rate limits
        if wait > 0:
            time.sleep(wait)
        r = self.s.post(API + path, json=body, timeout=self.timeout)
        self._last = time.time()
        if r.status_code != 200:
            raise DhanError(f"{path} -> HTTP {r.status_code}: {r.text[:300]}")
        return r.json()

    def profile(self) -> dict:
        r = self.s.get(API + "/profile", timeout=self.timeout)
        if r.status_code != 200:
            raise DhanError(f"/profile -> HTTP {r.status_code}: {r.text[:300]}")
        return r.json()

    # ----------------------------------------------------------- instruments
    def _load_master(self):
        if self._nse is not None:
            return
        cache = ROOT / "data" / "dhan-scrip-master.csv"
        cache.parent.mkdir(parents=True, exist_ok=True)
        fresh = cache.exists() and \
            dt.date.fromtimestamp(cache.stat().st_mtime) == dt.date.today()
        if not fresh:
            print("[dhan] downloading scrip master…")
            r = requests.get(SCRIP_URL, timeout=120)
            r.raise_for_status()
            cache.write_bytes(r.content)
        nse, nfo = [], []
        with open(cache, newline="", encoding="utf-8", errors="ignore") as f:
            for row in csv.DictReader(f):
                exch = _col(row, "EXCH_ID", "SEM_EXM_EXCH_ID")
                if exch != "NSE":
                    continue
                inst = _col(row, "INSTRUMENT", "SEM_INSTRUMENT_NAME").upper()
                sid = _col(row, "SECURITY_ID", "SEM_SMST_SECURITY_ID", "SMST_SECURITY_ID")
                if not sid or not sid.isdigit():
                    continue
                sym = _col(row, "SYMBOL_NAME", "SM_SYMBOL_NAME", "SEM_TRADING_SYMBOL")
                disp = _col(row, "DISPLAY_NAME", "SEM_CUSTOM_SYMBOL") or sym
                if inst == "INDEX":
                    tok = enc("IDX_I", sid)
                    self._inst_type[tok] = "INDEX"
                    for name in {disp.upper(), sym.upper()}:
                        nse.append({"instrument_token": tok, "tradingsymbol": name,
                                    "name": name, "segment": "INDICES",
                                    "instrument_type": "EQ", "exchange": "NSE"})
                elif inst == "EQUITY" and STOCKS and _col(row, "SERIES", "SEM_SERIES") == "EQ" \
                        and (_col(row, "UNDERLYING_SYMBOL") or "").upper() in STOCKS:
                    # 2026-10-23: cash stocks (spot leg) for the configured stock list
                    tok = enc("NSE_EQ", sid)
                    self._inst_type[tok] = "EQUITY"
                    tsym = _col(row, "UNDERLYING_SYMBOL").upper()
                    nse.append({"instrument_token": tok, "tradingsymbol": tsym, "name": tsym, "segment": "EQ",
                                "instrument_type": "EQ", "exchange": "NSE"})
                elif inst in ("FUTIDX", "OPTIDX") or (inst in ("FUTSTK", "OPTSTK") and STOCKS
                                                        and (_col(row, "UNDERLYING_SYMBOL") or "").upper() in STOCKS):
                    und = _col(row, "UNDERLYING_SYMBOL") or \
                        (_col(row, "SEM_TRADING_SYMBOL") or sym).split("-")[0]
                    tok = enc("NSE_FNO", sid)
                    self._inst_type[tok] = inst
                    if inst in ("FUTIDX", "FUTSTK"):
                        itype, strike = "FUT", 0.0
                    else:
                        itype = _col(row, "OPTION_TYPE", "SEM_OPTION_TYPE").upper()
                        if itype not in ("CE", "PE"):
                            continue
                        try:
                            strike = float(_col(row, "STRIKE_PRICE", "SEM_STRIKE_PRICE") or 0)
                        except ValueError:
                            continue
                    try:
                        lot = int(float(_col(row, "LOT_SIZE", "SEM_LOT_UNITS") or 0))
                    except ValueError:
                        lot = 0
                    nfo.append({
                        "instrument_token": tok, "tradingsymbol": disp,
                        "name": und.upper(), "segment": "NFO-" + ("FUT" if itype == "FUT" else "OPT"),
                        "instrument_type": itype, "exchange": "NFO",
                        "expiry": _date(_col(row, "SM_EXPIRY_DATE", "SEM_EXPIRY_DATE", "EXPIRY_DATE")),
                        "strike": strike, "lot_size": lot,
                    })
        # make sure the configured index names always resolve
        have = {r["tradingsymbol"] for r in nse}
        for cfg_name, aliases in INDEX_ALIASES.items():
            if cfg_name in have:
                continue
            hit = next((r for r in nse if r["tradingsymbol"] in aliases), None)
            tok = hit["instrument_token"] if hit else (
                enc("IDX_I", INDEX_FALLBACK_IDS[cfg_name]) if cfg_name in INDEX_FALLBACK_IDS else None)
            if tok is not None:
                self._inst_type[tok] = "INDEX"
                nse.append({"instrument_token": tok, "tradingsymbol": cfg_name, "name": cfg_name,
                            "segment": "INDICES", "instrument_type": "EQ", "exchange": "NSE"})
        self._nse, self._nfo = nse, nfo
        print(f"[dhan] scrip master: {len(nse)} index rows, {len(nfo)} index F&O rows")

    def instruments(self, exchange: str) -> list[dict]:
        self._load_master()
        return list(self._nse if exchange == "NSE" else self._nfo)

    # ------------------------------------------------------------ historical
    _MINS = {"minute": 1, "3minute": 3, "5minute": 5, "10minute": 10, "15minute": 15,
             "25minute": 25, "30minute": 30, "60minute": 60}
    _NATIVE = (1, 5, 15, 25, 60)

    def historical_data(self, instrument_token, from_date, to_date, interval,
                        continuous=False, oi=False):
        seg, sid = dec(instrument_token)
        inst = self._inst_type.get(int(instrument_token)) or \
            ("INDEX" if seg == "IDX_I" else "EQUITY" if seg == "NSE_EQ" else "FUTIDX")
        if interval == "day":
            return self._daily(seg, sid, inst, from_date, to_date, oi)
        mins = self._MINS.get(interval)
        if mins is None:
            raise DhanError(f"unsupported interval {interval!r}")
        native = mins if mins in self._NATIVE else max(n for n in self._NATIVE if mins % n == 0)
        bars = self._intraday(seg, sid, inst, from_date, to_date, native, oi)
        return bars if native == mins else _resample(bars, mins)

    def _intraday(self, seg, sid, inst, frm, to, mins, oi):
        frm = _naive(frm); to = _naive(to)
        out, cur = [], frm
        while cur < to:
            end = min(to, cur + dt.timedelta(days=85))      # API max 90 days / call
            body = {"securityId": sid, "exchangeSegment": seg, "instrument": inst,
                    "interval": str(mins), "oi": bool(oi),
                    "fromDate": cur.strftime("%Y-%m-%d %H:%M:%S"),
                    "toDate": end.strftime("%Y-%m-%d %H:%M:%S")}
            try:
                data = self._post("/charts/intraday", body)
            except DhanError as e:
                if "HTTP 4" in str(e):                       # some builds want an int interval
                    body["interval"] = int(mins)
                    data = self._post("/charts/intraday", body)
                else:
                    raise
            out += _rows(data, intraday=True)
            cur = end
        dedup = {r["date"]: r for r in out}
        return [dedup[k] for k in sorted(dedup)]

    def _daily(self, seg, sid, inst, frm, to, oi):
        frm = _naive(frm)
        # toDate is exclusive, and Dhan rejects (DH-905) a toDate in the future -> cap at today
        to = min(_naive(to) + dt.timedelta(days=1),
                 dt.datetime.combine(dt.date.today(), dt.time(0, 0)))
        base = {"securityId": sid, "exchangeSegment": seg, "instrument": inst,
                "fromDate": frm.strftime("%Y-%m-%d"), "toDate": to.strftime("%Y-%m-%d")}
        variants = [dict(base, expiryCode=0, oi=bool(oi)),      # documented shape
                    dict(base, oi=False),                        # indices: no expiryCode
                    dict(base)]                                  # bare minimum
        last = None
        for body in variants:
            try:
                data = self._post("/charts/historical", body)
                break
            except DhanError as e:
                last = e
                if "HTTP 4" not in str(e):
                    raise
        else:
            # Daily endpoint refuses this instrument (seen for indices: DH-905).
            # Build daily bars from 60-min intraday instead - same OHLC.
            print(f"[dhan] daily endpoint refused {seg}/{sid} ({str(last)[:60]}…) "
                  f"- building daily from 60m bars")
            return _to_daily(self._intraday(seg, sid, inst, frm,
                                             _naive(to) + dt.timedelta(days=1), 60, oi))
        rows = _rows(data, intraday=False)
        for r in rows:
            r["date"] = dt.datetime.combine(r["date"].date(), dt.time(0, 0))
        return rows


# -------------------------------------------------------------------- helpers
def _naive(v) -> dt.datetime:
    if isinstance(v, dt.datetime):
        return v.replace(tzinfo=None)
    if isinstance(v, dt.date):
        return dt.datetime.combine(v, dt.time(0, 0))
    return dt.datetime.fromisoformat(str(v)[:19])


def _rows(data: dict, intraday: bool) -> list[dict]:
    d = data.get("data", data) if isinstance(data, dict) else {}
    ts = d.get("timestamp") or d.get("start_Time") or []
    if not ts:
        return []
    ts = [t / 1000.0 if t > 1e12 else float(t) for t in ts]
    # Dhan returns epoch seconds. Decide between "true UTC epoch" (add +5:30)
    # and "IST wall-clock encoded as epoch" by which one puts intraday bars
    # inside NSE hours - robust to either convention.
    as_utc = [dt.datetime.utcfromtimestamp(t) + IST for t in ts]
    if intraday:
        as_raw = [dt.datetime.utcfromtimestamp(t) for t in ts]
        inside = lambda xs: sum(1 for x in xs if dt.time(9, 0) <= x.time() <= dt.time(15, 45))
        stamps = as_raw if inside(as_raw) > inside(as_utc) else as_utc
    else:
        stamps = as_utc
    o, h, l, c = d.get("open", []), d.get("high", []), d.get("low", []), d.get("close", [])
    v = d.get("volume") or [0] * len(ts)
    oi = d.get("open_interest") or [0] * len(ts)
    out = []
    for i, t in enumerate(stamps):
        try:
            out.append({"date": t, "open": float(o[i]), "high": float(h[i]),
                        "low": float(l[i]), "close": float(c[i]),
                        "volume": float(v[i] or 0), "oi": float(oi[i] or 0)})
        except (IndexError, TypeError, ValueError):
            continue
    return out


def _to_daily(bars: list[dict]) -> list[dict]:
    out = {}
    for b in bars:
        d = b["date"].date()
        if d not in out:
            out[d] = dict(b, date=dt.datetime.combine(d, dt.time(0, 0)))
        else:
            o = out[d]
            o["high"] = max(o["high"], b["high"]); o["low"] = min(o["low"], b["low"])
            o["close"] = b["close"]; o["volume"] += b["volume"]; o["oi"] = b["oi"]
    return [out[k] for k in sorted(out)]


def _resample(bars: list[dict], mins: int) -> list[dict]:
    out, cur, key = [], None, None
    for b in bars:
        t = b["date"]
        m = (t.hour * 60 + t.minute - 555) // mins          # anchor 09:15
        k = (t.date(), m)
        if cur is None or k != key:
            key = k
            start = dt.datetime.combine(t.date(), dt.time(0, 0)) + dt.timedelta(minutes=555 + m * mins)
            cur = dict(b, date=start)
            out.append(cur)
        else:
            cur["high"] = max(cur["high"], b["high"]); cur["low"] = min(cur["low"], b["low"])
            cur["close"] = b["close"]; cur["volume"] += b["volume"]; cur["oi"] = b["oi"]
    return out
