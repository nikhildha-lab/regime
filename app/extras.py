"""
Extra data for the regime engine: implied volatility, positioning / flows, and macro.
Every row is stored with the moment it became PUBLIC (`available_at`, UTC), so the engine never
uses a number before it was known.

  crypto_dvol.csv        Deribit DVOL (BTC, ETH) daily                 public  https://www.deribit.com/api/v2/public
  crypto_funding.csv     perpetual funding rate (BTC, ETH, SOL)        public  https://fapi.binance.com
  crypto_oi.csv          open interest + long/short ratio, daily       public  (Binance keeps only 30 days: saved forward)
  cot_legacy.csv         CFTC Commitments of Traders, weekly           public  https://publicreporting.cftc.gov
  macro_fred.csv         US rates, liquidity, financial conditions     public  https://fred.stlouisfed.org
  nse_fiidii_cash.csv    FII / DII cash-market buy-sell, daily         NSE shows only the latest day: saved forward
"""
from __future__ import annotations

import io
import json
import os
import time
import urllib.parse

import numpy as np
import pandas as pd

import updater as U

MD = U.MD
DVOL = os.path.join(MD, "crypto_dvol.csv")
FUNDING = os.path.join(MD, "crypto_funding.csv")
OI = os.path.join(MD, "crypto_oi.csv")
COT = os.path.join(MD, "cot_legacy.csv")
FRED = os.path.join(MD, "macro_fred.csv")
FIIDII = os.path.join(MD, "nse_fiidii_cash.csv")
OPTIONS = os.path.join(MD, "nse_options_eod.csv")


def _iso(ts) -> str:
    return pd.Timestamp(ts).tz_convert("UTC").strftime("%Y-%m-%d %H:%M:%S") if pd.Timestamp(ts).tzinfo else \
        pd.Timestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def _get_bytes(url, headers=None) -> bytes:
    """Browser-like client when available (some APIs refuse plain Python clients), else urllib."""
    if U._CSESSION is not None:
        for i in range(3):
            try:
                r = U._CSESSION.get(url, headers=headers or {}, timeout=40)
                if r.status_code == 200:
                    return r.content
                if r.status_code == 404:
                    raise U.NotFound(url)
                err = f"HTTP {r.status_code}"
            except U.NotFound:
                raise
            except Exception as e:
                err = str(e)[:150]
            time.sleep(1.5 * (i + 1))
        raise RuntimeError(f"{url[:90]}: {err}")
    return U.http_get(url, headers)


def _get_json(url, headers=None):
    return json.loads(_get_bytes(url, headers or {"Accept": "application/json"}))


# ------------------------------------------------------------------ Deribit DVOL
def parse_dvol(rows: list, ccy: str) -> pd.DataFrame:
    d = pd.DataFrame(rows, columns=["t", "open", "high", "low", "close"])
    ts = pd.to_datetime(d["t"], unit="ms", utc=True)
    return pd.DataFrame({"currency": ccy, "date": ts.dt.strftime("%Y-%m-%d"), "open": d["open"], "high": d["high"],
                         "low": d["low"], "close": d["close"],
                         "available_at": (ts + pd.Timedelta(days=1)).dt.strftime("%Y-%m-%d %H:%M:%S")})


def update_dvol(now=None) -> dict:
    now = now or pd.Timestamp.now(tz="UTC")
    added = 0
    for ccy in ("BTC", "ETH"):
        start = pd.Timestamp("2021-03-01", tz="UTC")
        if os.path.exists(DVOL):
            o = pd.read_csv(DVOL)
            o = o[o["currency"] == ccy]
            if len(o):
                start = pd.Timestamp(o["date"].max(), tz="UTC") - pd.Timedelta(days=2)
        end, frames = now, []
        while True:                                    # API pages backwards, 1000 rows a page
            q = (f"https://www.deribit.com/api/v2/public/get_volatility_index_data?currency={ccy}"
                 f"&start_timestamp={int(start.timestamp()*1000)}&end_timestamp={int(end.timestamp()*1000)}&resolution=1D")
            r = _get_json(q)["result"]
            if not r["data"]:
                break
            frames.append(parse_dvol(r["data"], ccy))
            cont = r.get("continuation")
            if not cont or cont <= start.timestamp() * 1000:
                break
            end = pd.Timestamp(cont, unit="ms", tz="UTC")
            time.sleep(0.3)
        if frames:
            new = pd.concat(frames)
            new = new[pd.to_datetime(new["available_at"]).dt.tz_localize("UTC") <= now]   # finished days only
            added += U.merge_rows(DVOL, new, ["currency", "date"], ["currency", "date"])
    return {"rows_added": added}


# ------------------------------------------------------------------ Binance funding / OI / long-short
SYMS = {"BTC": "BTCUSDT", "ETH": "ETHUSDT", "SOL": "SOLUSDT"}
FAPI = "https://fapi.binance.com"


def update_funding(now=None) -> dict:
    added = 0
    for coin, sym in SYMS.items():
        start = pd.Timestamp("2019-09-10", tz="UTC")
        if os.path.exists(FUNDING):
            o = pd.read_csv(FUNDING)
            o = o[o["coin"] == coin]
            if len(o):
                start = pd.Timestamp(o["funding_time"].max(), tz="UTC") + pd.Timedelta(seconds=1)
        frames = []
        while True:
            rows = _get_json(f"{FAPI}/fapi/v1/fundingRate?symbol={sym}&startTime={int(start.timestamp()*1000)}&limit=1000")
            if not rows:
                break
            d = pd.DataFrame(rows)
            ts = pd.to_datetime(d["fundingTime"].astype("int64"), unit="ms", utc=True).dt.floor("min")
            frames.append(pd.DataFrame({"coin": coin, "funding_time": ts.dt.strftime("%Y-%m-%d %H:%M:%S"),
                                        "rate": d["fundingRate"].astype(float)}))
            if len(rows) < 1000:
                break
            start = ts.max() + pd.Timedelta(seconds=1)
            time.sleep(0.3)
        if frames:
            added += U.merge_rows(FUNDING, pd.concat(frames), ["coin", "funding_time"], ["coin", "funding_time"])
    return {"rows_added": added}


def update_oi(now=None) -> dict:
    frames = []
    for coin, sym in SYMS.items():
        oi = pd.DataFrame(_get_json(f"{FAPI}/futures/data/openInterestHist?symbol={sym}&period=1d&limit=30"))
        ls = pd.DataFrame(_get_json(f"{FAPI}/futures/data/globalLongShortAccountRatio?symbol={sym}&period=1d&limit=30"))
        if oi.empty:
            continue
        oi["ts"] = pd.to_datetime(oi["timestamp"].astype("int64"), unit="ms", utc=True)
        d = pd.DataFrame({"coin": coin, "ts": oi["ts"].dt.strftime("%Y-%m-%d %H:%M:%S"),
                          "oi_coins": oi["sumOpenInterest"].astype(float), "oi_usd": oi["sumOpenInterestValue"].astype(float)})
        if not ls.empty:
            ls["ts"] = pd.to_datetime(ls["timestamp"].astype("int64"), unit="ms", utc=True).dt.strftime("%Y-%m-%d %H:%M:%S")
            d = d.merge(ls[["ts", "longShortRatio"]].rename(columns={"longShortRatio": "long_short_ratio"}), on="ts", how="left")
        else:
            d["long_short_ratio"] = None
        frames.append(d)
        time.sleep(0.3)
    n = U.merge_rows(OI, pd.concat(frames), ["coin", "ts"], ["coin", "ts"]) if frames else 0
    return {"rows_added": n}


# ------------------------------------------------------------------ CFTC COT (legacy, futures only)
COT_CODES = {"GOLD": "088691", "SILVER": "084691", "COPPER": "085692", "CRUDE": "067651",
             "ES": "13874A", "NQ": "209742", "BTC": "133741"}
COT_URL = "https://publicreporting.cftc.gov/resource/6dca-aqww.json"


def parse_cot(rows: list) -> pd.DataFrame:
    d = pd.DataFrame(rows)
    inv = {v: k for k, v in COT_CODES.items()}
    rd = pd.to_datetime(d["report_date_as_yyyy_mm_dd"])
    return pd.DataFrame({
        "market": d["cftc_contract_market_code"].map(inv), "report_date": rd.dt.strftime("%Y-%m-%d"),
        "open_interest": d["open_interest_all"].astype(float),
        "noncomm_long": d["noncomm_positions_long_all"].astype(float), "noncomm_short": d["noncomm_positions_short_all"].astype(float),
        "comm_long": d["comm_positions_long_all"].astype(float), "comm_short": d["comm_positions_short_all"].astype(float),
        # Tuesday data is published Friday 15:30 ET; holiday weeks slip to Monday -> use the following Monday, to be safe
        "available_at": (rd + pd.Timedelta(days=6)).dt.strftime("%Y-%m-%d 00:00:00")})


def update_cot(now=None) -> dict:
    last = "2010-01-01"
    if os.path.exists(COT):
        last = (pd.to_datetime(pd.read_csv(COT)["report_date"]).max() - pd.Timedelta(days=14)).strftime("%Y-%m-%d")
    codes = ",".join(f"'{c}'" for c in COT_CODES.values())
    q = {"$select": "cftc_contract_market_code,report_date_as_yyyy_mm_dd,open_interest_all,noncomm_positions_long_all,"
                    "noncomm_positions_short_all,comm_positions_long_all,comm_positions_short_all",
         "$where": f"cftc_contract_market_code in({codes}) AND report_date_as_yyyy_mm_dd >= '{last}'",
         "$order": "report_date_as_yyyy_mm_dd", "$limit": "50000"}
    rows = _get_json(COT_URL + "?" + urllib.parse.urlencode(q))
    if not rows:
        return {"rows_added": 0}
    return {"rows_added": U.merge_rows(COT, parse_cot(rows), ["market", "report_date"], ["market", "report_date"])}


# ------------------------------------------------------------------ FRED (no key: the public graph CSV)
# series -> (meaning, publication lag after the observation date)
FRED_SERIES = {
    "DGS2": ("US 2Y yield", pd.Timedelta(days=1, hours=22)), "DGS10": ("US 10Y yield", pd.Timedelta(days=1, hours=22)),
    "DFII10": ("US 10Y real yield", pd.Timedelta(days=1, hours=22)), "T10Y2Y": ("10Y minus 2Y", pd.Timedelta(days=1, hours=22)),
    "DFF": ("Fed funds rate", pd.Timedelta(days=1, hours=22)), "NFCI": ("Chicago Fed financial conditions", pd.Timedelta(days=6)),
    "WALCL": ("Fed balance sheet", pd.Timedelta(days=1, hours=22)), "WTREGEN": ("Treasury cash account", pd.Timedelta(days=1, hours=22)),
    "RRPONTSYD": ("Reverse repo", pd.Timedelta(hours=20)),
    "BAMLH0A0HYM2": ("High-yield credit spread (FRED keeps 3 years)", pd.Timedelta(days=1, hours=22)),
}


def parse_fred_csv(text: str, sid: str) -> pd.DataFrame:
    d = pd.read_csv(io.StringIO(text))
    d.columns = ["date", "value"]
    d["value"] = pd.to_numeric(d["value"], errors="coerce")
    d = d.dropna()
    dt = pd.to_datetime(d["date"])
    return pd.DataFrame({"series": sid, "date": dt.dt.strftime("%Y-%m-%d"), "value": d["value"].values,
                         "available_at": (dt + FRED_SERIES[sid][1]).dt.strftime("%Y-%m-%d %H:%M:%S")})


def update_fred(now=None) -> dict:
    frames, errs = [], []
    for sid in FRED_SERIES:
        try:
            txt = _get_bytes(f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={sid}&cosd=2010-01-01").decode()
            frames.append(parse_fred_csv(txt, sid))
        except Exception as e:
            errs.append(f"{sid}: {str(e)[:80]}")
        time.sleep(0.3)
    if not frames:
        raise RuntimeError("FRED not reachable: " + "; ".join(errs[:2]))
    return {"rows_added": U.merge_rows(FRED, pd.concat(frames), ["series", "date"], ["series", "date"]), "errors": errs}


# ------------------------------------------------------------------ NSE FII / DII cash (latest day only -> keep every day)
def parse_fiidii(rows: list) -> pd.DataFrame:
    d = pd.DataFrame(rows)
    dt = pd.to_datetime(d["date"], format="%d-%b-%Y")
    return pd.DataFrame({"date": dt.dt.strftime("%Y-%m-%d"), "category": d["category"].str.replace("FII/FPI", "FII", regex=False),
                         "buy_cr": pd.to_numeric(d["buyValue"]), "sell_cr": pd.to_numeric(d["sellValue"]),
                         "net_cr": pd.to_numeric(d["netValue"]),
                         # NSE posts provisional figures in the evening
                         "available_at": (dt + pd.Timedelta(hours=14, minutes=30)).dt.strftime("%Y-%m-%d %H:%M:%S")})


def update_fiidii(now=None) -> dict:
    s = U._CSESSION
    if s is None:
        raise RuntimeError("needs curl_cffi (browser-like client) for nseindia.com")
    hdr = {"Referer": "https://www.nseindia.com/reports/fii-dii", "Accept": "application/json"}
    s.get("https://www.nseindia.com/reports/fii-dii", timeout=30)     # sets the session cookies
    r = s.get("https://www.nseindia.com/api/fiidiiTradeReact", headers=hdr, timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}")
    return {"rows_added": U.merge_rows(FIIDII, parse_fiidii(r.json()), ["date", "category"], ["date", "category"])}


# ------------------------------------------------------------------ NIFTY / BANKNIFTY options, end of day (from the F&O bhavcopy)
def options_summary(df: pd.DataFrame) -> pd.DataFrame:
    """One row per date x index: PCR (OI, volume), near-expiry max pain, call wall, put wall, ATM straddle."""
    f = df[(df["FinInstrmTp"] == "IDO") & (df["TckrSymb"].isin(["NIFTY", "BANKNIFTY"]))].copy()
    if f.empty:
        return pd.DataFrame()
    out = []
    for (d, sym), g in f.groupby(["TradDt", "TckrSymb"]):
        ce, pe = g[g["OptnTp"] == "CE"], g[g["OptnTp"] == "PE"]
        und = float(g["UndrlygPric"].dropna().iloc[0]) if g["UndrlygPric"].notna().any() else float("nan")
        later = g.loc[g["XpryDt"] > d, "XpryDt"]
        near = later.min() if len(later) else g["XpryDt"].min()        # on expiry day use the NEXT expiry
        n = g[g["XpryDt"] == near]
        nce = n[n["OptnTp"] == "CE"].groupby("StrkPric")["OpnIntrst"].sum()
        npe = n[n["OptnTp"] == "PE"].groupby("StrkPric")["OpnIntrst"].sum()
        ks = sorted(set(nce.index) | set(npe.index))
        kc, kp = nce.index.to_numpy(dtype=float), npe.index.to_numpy(dtype=float)
        pain = {s_: float((nce.values * np.clip(s_ - kc, 0, None)).sum() + (npe.values * np.clip(kp - s_, 0, None)).sum()) for s_ in ks}
        atm = min(ks, key=lambda k: abs(k - und)) if ks and und == und else None
        px = n.set_index(["StrkPric", "OptnTp"])["ClsPric"]
        straddle = float(px.get((atm, "CE"), float("nan")) + px.get((atm, "PE"), float("nan"))) if atm else float("nan")
        out.append({"date": d, "symbol": sym, "underlying": und, "near_expiry": near,
                    "pcr_oi": pe["OpnIntrst"].sum() / max(1.0, ce["OpnIntrst"].sum()),
                    "pcr_vol": pe["TtlTradgVol"].sum() / max(1.0, ce["TtlTradgVol"].sum()),
                    "pcr_oi_near": npe.sum() / max(1.0, nce.sum()),
                    "ce_oi_chg": ce["ChngInOpnIntrst"].sum(), "pe_oi_chg": pe["ChngInOpnIntrst"].sum(),
                    "max_pain": min(pain, key=pain.get) if pain else float("nan"),
                    "call_wall": float(nce.idxmax()) if len(nce) else float("nan"),
                    "put_wall": float(npe.idxmax()) if len(npe) else float("nan"),
                    "atm_strike": atm, "atm_straddle": straddle,
                    "straddle_pct": straddle / und * 100 if und == und else float("nan"),
                    "available_at": f"{d} 14:30:00", "summary_version": 2})   # bhavcopy is out by ~20:00 IST
    return pd.DataFrame(out)


OPT_PROGRESS = os.path.join(MD, "nse_options_progress.json")
OPT_FIRST = pd.Timestamp("2024-01-01").date()        # NSE's new-format (UDiFF) F&O bhavcopy starts 2024-01-01


def update_options(now=None, max_days: int = 120) -> dict:
    """Daily options summary; backfills from 2024-01 a few months per run (each day's file is ~1 MB).
    A progress marker remembers checked days, so holidays (404) are not re-checked every run."""
    now = now or pd.Timestamp.now(tz=U.IST)
    done = pd.Timestamp("2023-12-31").date()
    prog = json.load(open(OPT_PROGRESS)) if os.path.exists(OPT_PROGRESS) else {}
    if prog.get("version") == 2:
        done = max(done, pd.Timestamp(prog["checked_through"]).date())
    elif os.path.exists(OPTIONS):
        o = pd.read_csv(OPTIONS)
        if "summary_version" in o and o["summary_version"].fillna(1).min() >= 2:
            done = max(done, pd.to_datetime(o["date"]).max().date())
        # else: rows from the old rule (same-day expiry) -> rebuild from 2024-01; old rows are overwritten as it goes
    days = U._days(done, now, 10_000)[:max_days]
    rows, errs, checked = [], [], done
    for i, d in enumerate(days):
        try:
            raw = U.http_get(f"{U.NSE}/fo/BhavCopy_NSE_FO_0_0_0_{d:%Y%m%d}_F_0000.csv.zip", U.NSE_HEADERS)
            rows.append(options_summary(U._unzip_csv(raw)))
        except U.NotFound:
            pass
        except Exception as e:
            errs.append(f"{d}: {str(e)[:100]}")
            if len(errs) >= 3:
                break
        checked = d
        if rows and (i + 1) % 25 == 0:                                  # save progress as we go
            U.merge_rows(OPTIONS, pd.concat(rows), ["date", "symbol"], ["date", "symbol"])
            rows = []
            json.dump({"checked_through": str(checked), "version": 2}, open(OPT_PROGRESS, "w"))
            U.log(f"  options backfill: {d}")
        time.sleep(0.3)
    n = U.merge_rows(OPTIONS, pd.concat(rows), ["date", "symbol"], ["date", "symbol"]) if rows else 0
    if not errs:
        json.dump({"checked_through": str(checked), "version": 2}, open(OPT_PROGRESS, "w"))
    return {"rows_added": n, "errors": errs, "days_checked": len(days), "checked_through": str(checked)}


# ------------------------------------------------------------------ long daily history (Nifty 2007->, S&P 1970-> ...) kept current
DEEP = os.path.join(MD, "yahoo_daily_deep.csv")
DEEP_SEED = ()          # ETH / SOL long history now comes from Binance spot daily files (app/flow.py)


def update_deep(now=None) -> dict:
    if not os.path.exists(DEEP):
        return {"rows_added": 0, "errors": ["yahoo_daily_deep.csv missing"]}
    cur = pd.read_csv(DEEP, usecols=["symbol", "date"])
    have = cur["symbol"].value_counts()
    syms = list(dict.fromkeys(list(have.index) + list(DEEP_SEED)))
    frames, errs, reseed = [], [], []
    for sym in syms:
        # a seed symbol with no history or non-daily rows (Yahoo returns weekly bars for range=max) gets 10 years of
        # DAILY history once; everything else just the last month
        full = False
        if sym in DEEP_SEED:
            dts = pd.to_datetime(cur.loc[cur["symbol"] == sym, "date"]).sort_values()
            full = len(dts) < 400 or dts.diff().dt.days.median() > 1.5
        try:
            df, tz = U.yahoo_chart(sym, "1d", "10y" if full else "1mo")
            d = pd.DataFrame({"date": df["ts"].dt.tz_convert(tz).dt.strftime("%Y-%m-%d"), "symbol": sym,
                              "open": df["open"].round(4), "high": df["high"].round(4), "low": df["low"].round(4),
                              "close": df["close"].round(4), "volume": df["volume"].fillna(0)})
            if full:
                gap = pd.to_datetime(d["date"]).sort_values().diff().dt.days.median()
                if not gap <= 1.5:
                    errs.append(f"{sym}: Yahoo did not return daily bars (median gap {gap} days) - not saved")
                    continue
                reseed.append(sym)
            frames.append(d)
        except Exception as e:
            errs.append(f"{sym}: {str(e)[:80]}")
        time.sleep(0.5)
    if reseed:                                     # replace any earlier non-daily rows of these symbols
        old = pd.read_csv(DEEP)
        U.write_atomic(old[~old["symbol"].isin(reseed)], DEEP)
    n = U.merge_rows(DEEP, pd.concat(frames), ["symbol", "date"], ["symbol", "date"]) if frames else 0
    return {"rows_added": n, "errors": errs, **({"full_history": reseed} if reseed else {})}


# ------------------------------------------------------------------ GDELT world-news tone and attention, daily (once a day)
GDELT = os.path.join(MD, "gdelt_tone.csv")
GDELT_Q = {
    "india_markets": '(Sensex OR Nifty OR "Indian stocks" OR "Dalal Street")',
    "india_economy": '("Indian economy" OR "India GDP" OR "India inflation")',
    "rbi": '("Reserve Bank of India" OR "RBI governor")',
    "fpi": '("foreign portfolio investors" OR "FPI outflows" OR "FPI inflows")',
    "fed": '("Federal Reserve" OR FOMC OR "Fed chair")',
    "us_stocks": '("Wall Street" OR "S&P 500" OR "Dow Jones")',
    "inflation": '("inflation rate" OR "consumer prices" OR "CPI inflation")',
    "recession": '(recession OR "economic slowdown")',
    "oil": '(OPEC OR "oil supply" OR "oil prices")',
    "conflict": '(war OR missile OR airstrike)',
    "tariffs": '(tariff OR tariffs OR "trade war")',
    "crypto": '(bitcoin OR cryptocurrency)',
    "gold": '("gold price" OR "gold prices")',
    "china": '("Chinese economy" OR "China economy" OR "China stimulus")',
    "banking_stress": '("bank failure" OR "bank collapse" OR "banking crisis")',
    "india_geo": '("India Pakistan" OR "India China border" OR "Line of Control")',
    "market_crash": '("stock market crash" OR "market selloff" OR "market sell-off")',
    "ai_tech": '("artificial intelligence" stocks OR "AI stocks" OR "tech stocks")',
}


def parse_gdelt_timeline(j: dict, theme: str, col: str) -> pd.DataFrame:
    data = (j.get("timeline") or [{}])[0].get("data", [])
    d = pd.DataFrame(data)
    if d.empty:
        return pd.DataFrame(columns=["date", "theme", col])
    return pd.DataFrame({"date": pd.to_datetime(d["date"].str[:8]).dt.strftime("%Y-%m-%d"), "theme": theme, col: d["value"].astype(float)})


def update_gdelt(now=None) -> dict:
    """Last 30 days for every theme, once per day (GDELT asks for one request every 5 seconds)."""
    now = now or pd.Timestamp.now(tz="UTC")
    stamp = os.path.join(MD, "gdelt_last_run.txt")
    have0 = pd.read_csv(GDELT) if os.path.exists(GDELT) else None
    complete = have0 is not None and all(
        int(have0.loc[have0["theme"] == t_, c_].notna().sum()) >= 2000 for t_ in GDELT_Q for c_ in ("tone", "volume"))
    if complete and os.path.exists(stamp):
        last_run = pd.Timestamp(open(stamp).read().strip())
        if now - last_run < pd.Timedelta(hours=20):
            return {"rows_added": 0, "skipped": "already updated today"}
    open(stamp, "w").write(str(now))
    end = now.strftime("%Y%m%d%H%M%S")
    have = pd.read_csv(GDELT) if os.path.exists(GDELT) else pd.DataFrame(columns=["date", "theme", "tone", "volume"])
    frames, errs = [], []
    backfills = 0
    for theme, q in GDELT_Q.items():
        parts = []
        for mode, col in (("timelinetone", "tone"), ("timelinevol", "volume")):
            n_hist = int(have.loc[have["theme"] == theme, col].notna().sum()) if len(have) else 0
            if n_hist < 2000:
                if backfills >= 4:                           # series without history: 4 per run, 2017 -> now
                    continue
                start = "20170101000000"
                backfills += 1
            elif not complete:
                continue                                     # finish the history first, then refresh daily
            else:
                start = (now - pd.Timedelta(days=30)).strftime("%Y%m%d000000")
            url = (f"https://api.gdeltproject.org/api/v2/doc/doc?query={urllib.parse.quote(q)}&mode={mode}&format=json"
                   f"&startdatetime={start}&enddatetime={end}&timelinesmooth=0")
            time.sleep(12)
            try:
                parts.append(parse_gdelt_timeline(_get_json(url), theme, col))
            except Exception as e:
                errs.append(f"{theme} {mode}: {str(e)[:80]}")
        if len(parts) == 2:
            frames.append(parts[0].merge(parts[1], on=["date", "theme"], how="outer"))
        elif len(parts) == 1:
            frames.append(parts[0])
    if not frames:
        if errs:
            raise RuntimeError("GDELT not reachable: " + "; ".join(errs[:2]))
        return {"rows_added": 0}
    new = pd.concat(frames)
    new = new[pd.to_datetime(new["date"]) < now.tz_localize(None).normalize()]          # complete days only
    new["available_at"] = (pd.to_datetime(new["date"]) + pd.Timedelta(days=1, hours=1)).dt.strftime("%Y-%m-%d %H:%M:%S")
    if len(have):
        old_ = have.set_index(["date", "theme"])
        new_ = new.set_index(["date", "theme"])
        both = new_.combine_first(old_.reindex(new_.index)) if len(new_) else new_
        new = both.reset_index()
    for c in ("tone", "volume"):
        if c not in new:
            new[c] = np.nan
    new = new[["date", "theme", "tone", "volume", "available_at"]]
    return {"rows_added": U.merge_rows(GDELT, new, ["date", "theme"], ["theme", "date"]), "errors": errs}


def run_all(now=None) -> dict:
    out = {}
    for name, fn in (("dvol", update_dvol), ("funding", update_funding), ("crypto_oi", update_oi),
                     ("cot", update_cot), ("fred", update_fred), ("fii_dii_cash", update_fiidii),
                     ("nse_options", update_options), ("deep_daily", update_deep)):
        # gdelt_news removed 2026-10-17: the news-mood signal failed testing and the GDELT download stalled the refresh
        try:
            out[name] = fn(now)
        except Exception as e:
            out[name] = {"rows_added": 0, "errors": [str(e)[:200]]}
    if all(v.get("errors") and not v.get("rows_added") for v in out.values()):
        raise RuntimeError("no extra source reachable")
    return out


JOBS = [("extras", run_all)]
