"""
Live data updater for the Market Pulse app.

Brings every market-data CSV in data_store/market_data up to date, in the SAME format the
regime engine already reads (so nothing downstream changes):

  Yahoo chart API   synaptic_daily.csv, synaptic_intraday.csv, synaptic_intraday_5m_30m.csv,
                    synaptic_crypto_gold.csv, synaptic_us_commod.csv, synaptic_intraday_all.csv
  Dhan market data  nifty_futures_5m_upstox.csv  (NIFTY / BANKNIFTY futures, 5 minute; old file name kept)
                    futures_contracts.json        (live contracts + expiry, from the instrument master)
  NSE archives      nse_index_futures_2023_2026.csv  (F&O bhavcopy, end of day)
                    nse_participant_oi_2023_2026.csv (FII / DII / Pro / Client OI, end of day)
                    nse_stocks_eod.csv               (closes of the breadth stocks, end of day)

Rules
  * Only finished intraday bars are stored (a bar still forming is dropped; the next run picks it up).
  * A day's daily bar is replaced until the day is over (the engine's available_at rule ignores it until then).
  * Files are written atomically (temp file + rename) so the dashboard never reads half a file.
  * Each source fails on its own: one blocked site does not stop the others.

Run once from the repo root:   python app/updater.py
"""
from __future__ import annotations

import http.cookiejar
import io
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from datetime import date, datetime, timedelta

import pandas as pd

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
MD = os.path.join(ROOT, "data_store", "market_data")
IST = "Asia/Kolkata"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/129.0 Safari/537.36")
STATUS_FILE = os.path.join(MD, "update_status.json")


def log(msg: str):
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


# ---------------------------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------------------------
def _ssl_ctx():
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


_CTX = _ssl_ctx()


class NotFound(Exception):
    pass


# one cookie jar for the whole run: Yahoo answers 429 "Too Many Requests" to clients without its session cookie
_JAR = http.cookiejar.CookieJar()
_OPENER = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(_JAR), urllib.request.HTTPSHandler(context=_CTX))


def http_get(url: str, headers: dict | None = None, timeout: int = 30, tries: int = 3) -> bytes:
    h = {"User-Agent": UA, "Accept": "*/*", "Accept-Language": "en-US,en;q=0.9"}
    h.update(headers or {})
    err = None
    for i in range(tries):
        try:
            with _OPENER.open(urllib.request.Request(url, headers=h), timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise NotFound(url)
            err = e
            if e.code in (401, 403) and i == tries - 1:
                break
            if e.code == 429:
                time.sleep(4 * (i + 1))                      # rate limited: back off harder
        except Exception as e:  # network blip
            err = e
        time.sleep(1.5 * (i + 1))
    raise RuntimeError(f"{url}: {err}")


# ---------------------------------------------------------------------------------------------
# CSV merge
# ---------------------------------------------------------------------------------------------
def write_atomic(df: pd.DataFrame, path: str):
    tmp = path + ".tmp"
    df.to_csv(tmp, index=False)
    os.replace(tmp, path)


def merge_rows(path: str, new: pd.DataFrame, key, sort) -> int:
    """Append `new` to the CSV at `path`; rows with the same key are replaced. Returns rows added."""
    if new is None or new.empty:
        return 0
    old = pd.read_csv(path) if os.path.exists(path) else pd.DataFrame(columns=new.columns)
    new = new[old.columns] if len(old.columns) else new
    kf = key if callable(key) else (lambda d: d[key].astype(str).agg("|".join, axis=1))
    before = set(kf(old)) if len(old) else set()
    allr = pd.concat([old, new], ignore_index=True)
    allr["_k"] = kf(allr)
    allr = allr.drop_duplicates("_k", keep="last").drop(columns="_k")
    allr = allr.sort_values(sort, kind="stable")
    write_atomic(allr, path)
    return int(sum(1 for k in set(kf(new)) if k not in before))


# ---------------------------------------------------------------------------------------------
# Yahoo
# ---------------------------------------------------------------------------------------------
YAHOO_HOSTS = ("https://query1.finance.yahoo.com", "https://query2.finance.yahoo.com")
IV_MIN = {"5m": 5, "15m": 15, "30m": 30, "1h": 60, "1d": 1440}
MAX_RANGE = {"5m": "59d", "15m": "59d", "30m": "59d", "1h": "729d", "1d": "10y"}


def yahoo_range(interval: str, last: pd.Timestamp | None) -> str:
    if last is None:
        return MAX_RANGE[interval]
    gap = (pd.Timestamp.now(tz="UTC") - last).days
    for r, d in (("5d", 4), ("1mo", 27), ("3mo", 85)):
        if gap < d:
            return r
    return MAX_RANGE[interval] if interval != "1d" else "1y"


_YAHOO = {"crumb": None, "at": 0.0}
def _browser_session():
    """Yahoo rejects (HTTP 429) clients that don't look like a browser at the TLS level; curl_cffi looks like Chrome.
    It is in app/requirements.txt; if an older install lacks it, try installing it once into this Python."""
    try:
        from curl_cffi import requests as creq
    except ImportError:
        import subprocess
        try:
            r = subprocess.run([sys.executable, "-m", "pip", "install", "--quiet", "curl_cffi"], timeout=300,
                               capture_output=True, text=True)
            if r.returncode != 0:
                with open(os.path.join(MD, "pip_curl_cffi_error.log"), "w") as f:
                    f.write(r.stdout + "\n" + r.stderr)
                return None
            from curl_cffi import requests as creq
        except Exception:
            return None
    try:
        return creq.Session(impersonate="chrome")
    except Exception:
        return None


_CSESSION = _browser_session()
_CURL = "/usr/bin/curl" if os.path.exists("/usr/bin/curl") else None


def _curl_get(url: str) -> tuple[int, bytes]:
    """The system curl (macOS ships one) is accepted by Yahoo where Python's own HTTP client gets HTTP 429."""
    import subprocess
    r = subprocess.run([_CURL, "-sS", "--compressed", "--max-time", "30", "-A", UA, "-H", "Accept: application/json",
                        "-w", "\n%{http_code}", url], capture_output=True, timeout=40)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.decode(errors="ignore")[:200])
    body, _, code = r.stdout.rpartition(b"\n")
    return int(code or 0), body


def yahoo_get(url: str, tries: int = 3) -> bytes:
    if _CSESSION is None and _CURL is None:
        return http_get(url, tries=tries)
    err = None
    for i in range(tries):
        try:
            if _CSESSION is not None:
                r = _CSESSION.get(url, timeout=30)
                code, body = r.status_code, r.content
            else:
                code, body = _curl_get(url)
            if code == 200:
                return body
            if code == 404:
                raise NotFound(url)
            err = f"HTTP {code}"
            time.sleep((4 if code == 429 else 1.5) * (i + 1))
        except NotFound:
            raise
        except Exception as e:
            err = e
            time.sleep(1.5 * (i + 1))
    raise RuntimeError(f"{url}: {err}")


def yahoo_session(force: bool = False):
    """Get Yahoo's session cookie (from fc.yahoo.com) and crumb, like a browser does. Refreshed hourly."""
    if _CSESSION is not None or _CURL is not None:
        return ""                                            # the chart API needs no crumb with a browser-like client
    if not force and _YAHOO["crumb"] is not None and time.time() - _YAHOO["at"] < 3600:
        return _YAHOO["crumb"]
    for url in ("https://fc.yahoo.com/", "https://finance.yahoo.com/"):
        try:
            http_get(url, {"Accept": "text/html"}, tries=1)
        except Exception:
            pass                                             # fc.yahoo.com answers 404 but still sets the cookie
        if len(_JAR):
            break
    crumb = ""
    for host in YAHOO_HOSTS:
        try:
            c = http_get(host + "/v1/test/getcrumb", tries=2).decode().strip()
            if c and "<" not in c and len(c) < 40:
                crumb = c
                break
        except Exception:
            pass
    _YAHOO.update(crumb=crumb, at=time.time())
    return crumb


def yahoo_chart(sym: str, interval: str, rng: str):
    """-> (DataFrame[ts_utc, open, high, low, close, volume], exchange tz). Rows with no close are dropped."""
    crumb = yahoo_session()
    q = f"/v8/finance/chart/{urllib.parse.quote(sym)}?range={rng}&interval={interval}&includePrePost=false"
    if crumb:
        q += "&crumb=" + urllib.parse.quote(crumb)
    last_err = None
    for host in YAHOO_HOSTS:
        try:
            j = json.loads(yahoo_get(host + q))
            break
        except Exception as e:
            last_err = e
    else:
        raise RuntimeError(f"Yahoo {sym} {interval}: {last_err}")
    res = (j.get("chart") or {}).get("result") or []
    if not res:
        raise RuntimeError(f"Yahoo {sym} {interval}: empty result")
    x = res[0]
    tz = x["meta"].get("exchangeTimezoneName", "UTC")
    ts = x.get("timestamp") or []
    q0 = (x.get("indicators", {}).get("quote") or [{}])[0]
    df = pd.DataFrame({"ts": pd.to_datetime(ts, unit="s", utc=True),
                       **{k: q0.get(k, [None] * len(ts)) for k in ("open", "high", "low", "close", "volume")}})
    df = df.dropna(subset=["close"])
    df["volume"] = df["volume"].fillna(0)
    return df, tz


def finished_only(df: pd.DataFrame, interval: str, now=None) -> pd.DataFrame:
    """Drop intraday bars that are still forming (start + length > now, or a mid-bar timestamp)."""
    if interval == "1d" or df.empty:
        return df
    now = now or pd.Timestamp.now(tz="UTC")
    end = df["ts"] + pd.Timedelta(minutes=IV_MIN[interval])
    return df[(end <= now) & (df["ts"].dt.second == 0)]


# file -> (time column, symbol column style, timestamps stored in exchange-local or UTC)
YAHOO_FILES = {
    "synaptic_daily.csv": ("datetime_local", "symbol", "local"),
    "synaptic_intraday.csv": ("datetime_local", "symbol_interval", "local"),
    "synaptic_intraday_5m_30m.csv": ("datetime_local", "symbol_interval", "local"),
    "synaptic_crypto_gold.csv": ("datetime_utc", "symbol+interval", "utc"),
    "synaptic_us_commod.csv": ("datetime_utc", "symbol+interval", "utc"),
    "synaptic_intraday_all.csv": ("datetime_utc", "symbol+interval", "utc"),
    "synaptic_daily_extra.csv": ("datetime_local", "symbol", "local"),
}
# series a file must hold even before its first row exists (backfilled 10 years on first run)
YAHOO_SEED = {"synaptic_daily_extra.csv": [
    # implied volatility family
    "^VIX3M", "^VVIX", "^MOVE", "^OVX", "^GVZ", "^VXN", "^SKEW",
    # India breadth of the rally: Nifty 500, Midcap 50, IT
    "^CRSLDX", "^NSEMDCP50", "^CNXIT",
    # US risk appetite: small caps, equal weight, cyclicals vs defensives, semis, credit (HYG vs IEF), short rates
    "^RUT", "IWM", "RSP", "XLY", "XLP", "SMH", "HYG", "IEF", "^IRX", "^FVX",
    # round 2 (test-all request): short-term VIX, bonds, credit, EM, all US sectors, semis index, FX
    "^VIX9D", "TLT", "LQD", "EEM", "XLK", "XLF", "XLE", "XLV", "XLI", "XLU", "XLB", "XLRE", "XLC", "^SOX",
    "EURUSD=X", "JPY=X", "CNH=X", "^TYX"],
    # 2026-10-24: aluminium and natural gas join the Metals / Commodities tabs (daily + intraday)
    "synaptic_us_commod.csv": [("ALI=F", "1d"), ("NG=F", "1d"), ("^BSESN", "1d")],      # 2026-11-04: + Sensex
    "synaptic_intraday_all.csv": [(s_, iv) for s_ in ("ALI=F", "NG=F", "^BSESN") for iv in ("5m", "15m", "30m", "1h")]
                                 # 2026-11-03 (Nikhil): six more coins on the Crypto tab (daily + 1h in the crypto file, 5m / 15m / 30m here)
                                 + [(s_, iv) for s_ in ("BNB-USD", "XRP-USD", "UNI7083-USD", "ZEC-USD", "LINK-USD", "DASH-USD", "AVAX-USD") for iv in ("5m", "15m", "30m")],
    "synaptic_crypto_gold.csv": [(s_, iv) for s_ in ("BNB-USD", "XRP-USD", "UNI7083-USD", "ZEC-USD", "LINK-USD", "DASH-USD", "AVAX-USD") for iv in ("1d", "1h")]}
LOCAL_TZ = {"^NSEI": IST, "^NSEBANK": IST, "^BSESN": IST, "^INDIAVIX": IST, "^GSPC": "America/New_York", "^VIX": "America/Chicago",
            "^TNX": "America/Chicago", "DX-Y.NYB": "America/New_York", "BZ=F": "America/New_York", "INR=X": "Europe/London"}


def _series_list(path: str, style: str):
    seed = [x if isinstance(x, tuple) else (x, "1d") for x in YAHOO_SEED.get(os.path.basename(path), [])]
    if not os.path.exists(path):
        return seed
    d = pd.read_csv(path, usecols=lambda c: c in ("symbol", "symbol_interval", "interval"))
    if style == "symbol":
        return list(dict.fromkeys([(s, "1d") for s in d["symbol"].unique()] + seed))
    if style == "symbol_interval":
        return [tuple(s.rsplit("_", 1)) for s in d["symbol_interval"].unique()]
    return list(dict.fromkeys([tuple(x) for x in d[["symbol", "interval"]].drop_duplicates().itertuples(index=False)] + seed))


def update_yahoo_file(fname: str, now=None) -> dict:
    tcol, style, zone = YAHOO_FILES[fname]
    path = os.path.join(MD, fname)
    if not os.path.exists(path):
        pd.DataFrame(columns=[tcol, "symbol", "open", "high", "low", "close", "volume"]).to_csv(path, index=False)
    old = pd.read_csv(path)
    added, errors, frames = 0, [], []
    for sym, iv in _series_list(path, style):
        if style == "symbol":
            o = old[old["symbol"] == sym]
        elif style == "symbol_interval":
            o = old[old["symbol_interval"] == f"{sym}_{iv}"]
        else:
            o = old[(old["symbol"] == sym) & (old["interval"] == iv)]
        tz_guess = LOCAL_TZ.get(sym, IST)
        last = None
        if len(o):
            lt = pd.Timestamp(o[tcol].max())
            last = lt.tz_localize("UTC") if zone == "utc" else lt.tz_localize(tz_guess, ambiguous="NaT", nonexistent="NaT")
            if pd.isna(last):
                last = None
        try:
            df, tz = yahoo_chart(sym, iv, yahoo_range(iv, last))
        except Exception as e:
            errors.append(f"{sym} {iv}: {e}")
            if len(errors) >= 4 and not frames:
                break                                         # Yahoo is not answering; try again next run
            continue
        df = finished_only(df, iv, now)
        if df.empty:
            continue
        if zone == "utc":
            t = df["ts"].dt.tz_localize(None)
        else:
            t = df["ts"].dt.tz_convert(tz).dt.tz_localize(None)
        out = pd.DataFrame({tcol: t.dt.strftime("%Y-%m-%d %H:%M:%S")})
        if style == "symbol":
            out["symbol"] = sym
        elif style == "symbol_interval":
            out["symbol_interval"] = f"{sym}_{iv}"
        else:
            out["symbol"], out["interval"] = sym, iv
        for c in ("open", "high", "low", "close", "volume"):
            out[c] = df[c].values
        frames.append(out)
        time.sleep(0.6)
    if frames:
        new = pd.concat(frames, ignore_index=True)

        def key(d):
            sid = d["symbol_interval"] if "symbol_interval" in d else (
                d["symbol"] + "_" + d["interval"] if "interval" in d else d["symbol"] + "_1d")
            t = d[tcol].astype(str)
            # daily rows: one per calendar day (a live bar is replaced when the day completes);
            # intraday rows: one per bar start; rows with mid-bar timestamps (old live bars) are dropped below
            daily = sid.str.endswith("_1d")
            return sid + "|" + t.where(~daily, t.str[:10])

        added = merge_rows(path, new, key, sort=[c for c in ("symbol", "symbol_interval", "interval") if c in new] + [tcol])
        # old snapshot files hold a few still-forming intraday bars (mid-bar timestamps like 05:54:07): drop them
        d = pd.read_csv(path)
        if "interval" in d:
            intraday = d["interval"] != "1d"
        elif "symbol_interval" in d:
            intraday = pd.Series(True, index=d.index)
        else:
            intraday = pd.Series(False, index=d.index)
        live = intraday & ~d[tcol].astype(str).str.endswith(":00")
        if live.any():
            write_atomic(d[~live], path)
    return {"rows_added": added, "errors": errors}


def update_yahoo(now=None) -> dict:
    yahoo_chart("^GSPC", "1d", "5d")          # quick reachability probe: fail fast when offline
    out = {}
    for f in YAHOO_FILES:
        if os.path.exists(os.path.join(MD, f)) or f in YAHOO_SEED:
            out[f] = update_yahoo_file(f, now)
    return out


# ---------------------------------------------------------------------------------------------
# Dhan: NIFTY / BANKNIFTY futures, 5 minute (2026-10-24: replaces Upstox; same file and columns as before)
# ---------------------------------------------------------------------------------------------
CONTRACTS_FILE = os.path.join(MD, "futures_contracts.json")
FUT_5M = os.path.join(MD, "nifty_futures_5m_upstox.csv")      # file name kept so every reader keeps working; data now from Dhan
UNDERLYINGS = ("NIFTY", "BANKNIFTY")


def futures_contracts(refresh_hours: float = 12) -> list[dict]:
    cached = None
    if os.path.exists(CONTRACTS_FILE):
        cached = json.load(open(CONTRACTS_FILE))
        age = time.time() - cached.get("fetched_epoch", 0)
        if age < refresh_hours * 3600:
            return cached["contracts"]
    try:
        import dhan_live
        cons = dhan_live.futures_contracts()
        if not cons:
            raise RuntimeError("no futures in the Dhan scrip master")
        known = {c["contract"]: c for c in (cached or {}).get("contracts", [])}
        known.update({c["contract"]: c for c in cons})       # keep expired contracts for the expiry map
        allc = sorted(known.values(), key=lambda r: (r["underlying"], r["expiry"]))
        with open(CONTRACTS_FILE + ".tmp", "w") as f:
            json.dump({"fetched_epoch": time.time(), "fetched": str(datetime.now()), "source": "Dhan scrip master", "contracts": allc}, f, indent=1)
        os.replace(CONTRACTS_FILE + ".tmp", CONTRACTS_FILE)
        return allc
    except Exception:
        if cached:
            return cached["contracts"]
        raise


def dhan_futures_bars(c: dict, start: date, end: date, mins: int) -> pd.DataFrame:
    import dhan_live
    sid = c.get("security_id") or str(c["instrument_key"]).split("|")[-1]
    rows = dhan_live.intraday("NSE_FNO", sid, "FUTIDX", start, end, mins, oi=True)
    df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume", "oi"])
    if len(df):
        df["ts"] = pd.to_datetime(df["ts"]).map(lambda t: t.isoformat())
        df["volume"] = df["volume"].astype(int)
        df["oi"] = df["oi"].astype(int)
    return df


def update_futures(now=None) -> dict:
    now = now or pd.Timestamp.now(tz=IST)
    today = now.date()
    cons = [c for c in futures_contracts() if c["expiry"] >= str(today)]
    old = pd.read_csv(FUT_5M) if os.path.exists(FUT_5M) else pd.DataFrame(
        columns=["contract", "instrument_key", "ts", "open", "high", "low", "close", "volume", "oi"])
    frames, errors = [], []
    for c in cons:
        o = old[old["contract"] == c["contract"]]
        start = (pd.Timestamp(o["ts"].max()).tz_convert(IST).date() if len(o) else today - timedelta(days=95))
        try:
            df = dhan_futures_bars(c, start, today, 5)
        except Exception as e:
            errors.append(f"{c['contract']}: {e}")
            continue
        if df.empty:
            continue
        ts = pd.to_datetime(df["ts"])
        df = df[(ts + pd.Timedelta(minutes=5)) <= now]         # finished bars only
        df.insert(0, "instrument_key", c["instrument_key"])
        df.insert(0, "contract", c["contract"])
        frames.append(df)
    added = 0
    if frames:
        added = merge_rows(FUT_5M, pd.concat(frames, ignore_index=True), ["contract", "ts"], ["contract", "ts"])
    return {"rows_added": added, "errors": errors, "contracts": [c["contract"] for c in cons], "source": "Dhan"}


# ---------------------------------------------------------------------------------------------
# NSE archives (end of day)
# ---------------------------------------------------------------------------------------------
NSE = "https://nsearchives.nseindia.com/content"
NSE_HEADERS = {"Referer": "https://www.nseindia.com/", "Accept": "text/csv,application/zip,*/*"}
INDEX_FUT = os.path.join(MD, "nse_index_futures_2023_2026.csv")
PART_OI = os.path.join(MD, "nse_participant_oi_2023_2026.csv")
STOCKS_EOD = os.path.join(MD, "nse_stocks_eod.csv")
PART_COLS = ["client_type", "fut_idx_long", "fut_idx_short", "fut_stk_long", "fut_stk_short", "opt_idx_call_long",
             "opt_idx_put_long", "opt_idx_call_short", "opt_idx_put_short", "opt_stk_call_long", "opt_stk_put_long",
             "opt_stk_call_short", "opt_stk_put_short", "total_long", "total_short"]


def _unzip_csv(raw: bytes) -> pd.DataFrame:
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        return pd.read_csv(z.open(z.namelist()[0]))


def parse_fo_bhavcopy(df: pd.DataFrame) -> pd.DataFrame:
    f = df[(df["FinInstrmTp"] == "IDF") & (df["TckrSymb"].isin(UNDERLYINGS))]
    return pd.DataFrame({
        "date": f["TradDt"].values, "symbol": f["TckrSymb"].values, "expiry": f["XpryDt"].values,
        "open": f["OpnPric"].values, "high": f["HghPric"].values, "low": f["LwPric"].values, "close": f["ClsPric"].values,
        "settle": f["SttlmPric"].values,
        # the 2023-2026 history stores traded quantity (contracts x lot size)
        "volume_contracts": (f["TtlTradgVol"].astype(float) * f["NewBrdLotQty"].astype(float)).values,
        "open_interest": f["OpnIntrst"].astype(float).values, "change_in_oi": f["ChngInOpnIntrst"].astype(float).values,
        "underlying": f["UndrlygPric"].values})


def parse_participant_oi(text: str, day: date) -> pd.DataFrame:
    lines = [ln for ln in text.splitlines() if ln.strip()]
    hdr = next(i for i, ln in enumerate(lines) if ln.lower().startswith("client type"))
    rows = []
    for ln in lines[hdr + 1:]:
        p = [x.strip() for x in ln.split(",")]
        if p[0] in ("Client", "DII", "FII", "Pro"):
            rows.append([p[0]] + [int(float(x)) for x in p[1:15]])
    d = pd.DataFrame(rows, columns=PART_COLS)
    d.insert(0, "date", str(day))
    return d


def parse_cm_bhavcopy(df: pd.DataFrame, symbols: set) -> pd.DataFrame:
    f = df[(df["SctySrs"] == "EQ") & (df["TckrSymb"].isin(symbols))]
    return pd.DataFrame({"date": f["TradDt"].values, "symbol": f["TckrSymb"].values, "open": f["OpnPric"].values,
                         "high": f["HghPric"].values, "low": f["LwPric"].values, "close": f["ClsPric"].values,
                         "volume": f["TtlTradgVol"].values})


def _days(after: date, now: pd.Timestamp, max_days: int):
    """Weekdays after `after` whose EOD files should exist (today only after 19:00 IST)."""
    last = now.date() if now.hour >= 19 else now.date() - timedelta(days=1)
    d, out = after + timedelta(days=1), []
    while d <= last:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out[-max_days:]


def breadth_symbols() -> set:
    p = os.path.join(ROOT, "stocks_data.csv")
    out = set(pd.read_csv(p, usecols=["Symbol"])["Symbol"].dropna().unique()) if os.path.exists(p) else set()
    try:                                                   # 2026-11-10 (Nikhil): + the Nifty 500 list, so the monthly momentum basket (app/n500mom.py) has daily closes for every name
        n5 = os.path.join(os.environ.get("MP_DOCS") or os.path.join(os.path.expanduser("~"), "Documents"), "MASTER_BACKTESTER", "data", "nse_nifty500", "ind_nifty500list.csv")
        if os.path.exists(n5):
            out |= set(pd.read_csv(n5, usecols=["Symbol"])["Symbol"].dropna().unique())
    except Exception:
        pass
    return out


def update_nse(now=None, max_days: int = 250) -> dict:
    now = now or pd.Timestamp.now(tz=IST)
    res = {}
    # F&O bhavcopy -> index futures
    last = pd.to_datetime(pd.read_csv(INDEX_FUT, usecols=["date"])["date"]).max().date()
    rows, errs, missing = [], [], 0
    for d in _days(last, now, 40):
        try:
            rows.append(parse_fo_bhavcopy(_unzip_csv(http_get(f"{NSE}/fo/BhavCopy_NSE_FO_0_0_0_{d:%Y%m%d}_F_0000.csv.zip",
                                                              NSE_HEADERS))))
        except NotFound:
            missing += 1                                     # holiday, or not published yet
        except Exception as e:
            errs.append(f"{d}: {e}")
            if len(errs) >= 3:
                break
        time.sleep(0.3)
    n = merge_rows(INDEX_FUT, pd.concat(rows) if rows else None, ["date", "symbol", "expiry"], ["date", "symbol", "expiry"])
    res["nse_index_futures"] = {"rows_added": n, "errors": errs}
    # participant OI
    last = pd.to_datetime(pd.read_csv(PART_OI, usecols=["date"])["date"]).max().date()
    rows, errs = [], []
    for d in _days(last, now, 40):
        try:
            rows.append(parse_participant_oi(http_get(f"{NSE}/nsccl/fao_participant_oi_{d:%d%m%Y}.csv", NSE_HEADERS)
                                             .decode("utf-8", "ignore"), d))
        except NotFound:
            pass
        except Exception as e:
            errs.append(f"{d}: {e}")
            if len(errs) >= 3:
                break
        time.sleep(0.3)
    n = merge_rows(PART_OI, pd.concat(rows) if rows else None, ["date", "client_type"], ["date", "client_type"])
    res["nse_participant_oi"] = {"rows_added": n, "errors": errs}
    # cash-market closes for breadth (continues stocks_data.csv, which ends 2026-03)
    syms = breadth_symbols()
    if syms:
        if os.path.exists(STOCKS_EOD):
            last = pd.to_datetime(pd.read_csv(STOCKS_EOD, usecols=["date"])["date"]).max().date()
        else:
            sd = pd.read_csv(os.path.join(ROOT, "stocks_data.csv"), usecols=["Date"])
            last = pd.to_datetime(sd["Date"], format="%d-%m-%Y", errors="coerce").max().date()
        rows, errs = [], []
        days = _days(last, now, max_days)
        for i, d in enumerate(days):
            try:
                rows.append(parse_cm_bhavcopy(_unzip_csv(http_get(f"{NSE}/cm/BhavCopy_NSE_CM_0_0_0_{d:%Y%m%d}_F_0000.csv.zip",
                                                                  NSE_HEADERS)), syms))
            except NotFound:
                pass
            except Exception as e:
                errs.append(f"{d}: {e}")
                if len(errs) >= 3:
                    break                                    # site is blocking us; try again next run
            if i and i % 25 == 0:
                log(f"  stock closes: {i}/{len(days)} days")
            time.sleep(0.3)
        n = merge_rows(STOCKS_EOD, pd.concat(rows) if rows else None, ["date", "symbol"], ["date", "symbol"])
        res["nse_stocks_eod"] = {"rows_added": n, "errors": errs[:5]}
    if all(v["errors"] and not v["rows_added"] for v in res.values()):
        raise RuntimeError("NSE archives not reachable: " + str(next(iter(res.values()))["errors"][0])[:200])
    return res


# ---------------------------------------------------------------------------------------------
def near_futures_keys():
    today = str(datetime.now().date())
    cons = [c for c in futures_contracts() if c["expiry"] >= today]
    keys = []
    for u in UNDERLYINGS:
        keys += [c["instrument_key"] for c in sorted([c for c in cons if c["underlying"] == u], key=lambda c: c["expiry"])[:2]]
    return keys


def _flow_loop():
    """Order-flow downloads (app/flow.py), reloaded every round so fixes apply without a restart."""
    import importlib
    import traceback
    time.sleep(20)
    while True:
        try:
            import flow
            importlib.reload(flow)
            flow.step()
        except Exception:
            log("flow: " + traceback.format_exc().splitlines()[-1])
        time.sleep(20)


def _oc_loop():
    """Every ~3 minutes in market hours: NSE option chain (OI build-up) and Nifty/Bank Nifty futures 1-minute bars with OI."""
    import importlib
    import traceback
    time.sleep(15)
    last = 0.0
    while True:
        try:
            if time.time() - last >= 170:
                import optionchain
                import flow
                importlib.reload(optionchain)
                first = not os.path.exists(optionchain.LIVE)      # one run even when closed, to check the source works
                if optionchain.market_open() or first:
                    last = time.time()
                    try:
                        optionchain.step(force=first)
                    except Exception:
                        log("option chain: " + traceback.format_exc().splitlines()[-1])
                    importlib.reload(flow)
                    p = flow._prog()
                    flow.job_futures_1m(p, 60)
        except Exception:
            log("intraday loop: " + traceback.format_exc().splitlines()[-1])
        time.sleep(30)


def _intraday_api():
    """Tiny read-only server on 127.0.0.1:8051 so the page can refresh the intraday panel every minute without
    waiting for the 5-minute dashboard rebuild (the main app also serves /api/intraday after a restart)."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            p = os.path.join(root, "data_store", "flow", "oc_live.json")
            body = open(p, "rb").read() if os.path.exists(p) else b'{"symbols":{}}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
    try:
        ThreadingHTTPServer(("127.0.0.1", 8051), H).serve_forever()
    except Exception as e:
        log(f"intraday api not started: {e}")


def recorder_state():
    return "retired (Dhan replaces the Upstox tick recorder)"


def ensure_background():
    """Start the order-flow downloader, option-chain, news and live-stream threads once (works in an app that is already running)."""
    import threading
    # 2026-11-03 audit: two callers at start-up could both see "news_fast" missing and start it twice -> one lock for the
    # whole process (kept on the threading module because this file is reloaded every refresh).
    lk = getattr(threading, "_mp_bg_lock", None)
    if lk is None:
        lk = threading._mp_bg_lock = threading.Lock()
    with lk:
        _ensure_background(threading)


def _ensure_background(threading):
    names = {t.name for t in threading.enumerate()}
    if "flow_loop" not in names:
        threading.Thread(target=_flow_loop, daemon=True, name="flow_loop").start()
        log("started order-flow downloader")
    if "oc_loop" not in names:
        threading.Thread(target=_oc_loop, daemon=True, name="oc_loop").start()
        log("started option-chain / intraday OI loop")
    if "intraday_api" not in names:
        threading.Thread(target=_intraday_api, daemon=True, name="intraday_api").start()
    if "live_stream" not in names:
        def _stream():
            try:
                import stream
                stream.start()
            except Exception as e:
                log(f"live stream not started: {e}")
        threading.Thread(target=_stream, daemon=True, name="live_stream").start()
    try:
        import importlib
        import stream_hotfix                          # patches for the running stream (no restart needed)
        importlib.reload(stream_hotfix)
        stream_hotfix.apply()
    except Exception as e:
        log(f"stream hotfix: {e}")
    if "news_fast" not in names:
        def _news_loop():
            import importlib
            import traceback
            time.sleep(30)
            while True:
                n = datetime.now()
                busy = n.weekday() < 5 and 8 * 60 <= n.hour * 60 + n.minute <= 18 * 60 + 30
                try:
                    import news
                    importlib.reload(news)
                    news.run_all()
                    try:                                   # 2026-11-04: swing catalysts per market tab, every 10 min
                        import tabnews
                        importlib.reload(tabnews)
                        tabnews.run()
                    except Exception:
                        log("tabnews: " + traceback.format_exc().splitlines()[-1])
                except Exception:
                    log("news: " + traceback.format_exc().splitlines()[-1])
                time.sleep(90 if busy else 600)
        threading.Thread(target=_news_loop, daemon=True, name="news_fast").start()
        log("started news & shock watcher (every 90 s in the trading day)")


def update_all(include_nse: bool = True) -> dict:
    try:
        ensure_background()
    except Exception as e:
        log(f"background threads: {e}")
    t0 = time.time()
    status = {"started": str(pd.Timestamp.now(tz=IST).floor("s")), "sources": {}}
    import importlib
    import extras                                    # vol indices, positioning, macro (app/extras.py)
    importlib.reload(extras)
    india_jobs = []
    try:
        import india                                 # India desk data: sectors, F&O stocks, live quotes (app/india.py)
        importlib.reload(india)
        india_jobs = india.JOBS
    except Exception as e:
        log(f"india module not loaded: {e}")
    try:
        import results                               # company results for the positional persona (app/results.py)
        importlib.reload(results)
        india_jobs = india_jobs + results.JOBS
    except Exception as e:
        log(f"results module not loaded: {e}")
    try:
        import news                                  # news & shock watcher: runs in its own fast thread (see ensure_background)
    except Exception as e:
        log(f"news module not loaded: {e}")
    try:
        import health                                # system health monitor (app/health.py) - runs last
        importlib.reload(health)
        india_jobs = india_jobs + health.JOBS
    except Exception as e:
        log(f"health module not loaded: {e}")
    jobs = ([("yahoo", update_yahoo), ("futures", update_futures)] + ([("nse", update_nse)] if include_nse else [])
            + extras.JOBS + india_jobs)
    for name, fn in jobs:
        log(f"updating {name} ...")
        try:
            r = fn()
            status["sources"][name] = {"ok": True, "result": r, "at": str(pd.Timestamp.now(tz=IST).floor("s"))}
        except Exception as e:
            status["sources"][name] = {"ok": False, "error": str(e)[:300], "at": str(pd.Timestamp.now(tz=IST).floor("s"))}
        log(f"  {name}: {'ok' if status['sources'][name]['ok'] else 'FAILED - ' + status['sources'][name]['error']}")
    status["seconds"] = round(time.time() - t0, 1)
    prev = json.load(open(STATUS_FILE)) if os.path.exists(STATUS_FILE) else {}
    for k, v in status["sources"].items():          # remember the last success of each source
        if v["ok"]:
            prev.setdefault("last_ok", {})[k] = v["at"]
    status["last_ok"] = prev.get("last_ok", {})
    with open(STATUS_FILE + ".tmp", "w") as f:
        json.dump(status, f, indent=1, default=str)
    os.replace(STATUS_FILE + ".tmp", STATUS_FILE)
    return status


if __name__ == "__main__":
    s = update_all(include_nse="--no-nse" not in sys.argv)
    print(json.dumps({k: (v["ok"], v.get("error")) for k, v in s["sources"].items()}, indent=1))
