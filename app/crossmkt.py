"""
Cross-market live feed (every ~3 minutes, 24 hours): global index futures, Asia, FX, crude, BTC and GIFT Nifty,
measured from Nifty's last close. Writes data_store/flow/cross_live.json and merges it into oc_live.json under
"cross" (the page's 1-minute intraday poll reads that file).

Also keeps two history files growing for the tests:
  data_store/market_data/cross_market_1h.csv   hourly bars (Yahoo keeps ~2 years of hourly history)
  data_store/flow/gift_nifty.csv               GIFT Nifty snapshots from NSE (no free history exists, so we record it)
and adds Dow futures, Dow, Nikkei, Hang Seng, Kospi and Euro Stoxx 50 to yahoo_daily_deep.csv (full history, once).

Expected open (Nifty): gap = const + slope x (S&P futures move since Nifty's close), from scripts/test_cross_market.py
(hourly model, 2024-26: correlation 0.65, sign right 70%, 80% of errors within about +-0.45%). When GIFT Nifty is
available its own move since Nifty's close is shown next to it.
"""
from __future__ import annotations

import json
import os
import time

import pandas as pd

import updater as U

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
MD = os.path.join(ROOT, "data_store", "market_data")
FLOW = os.path.join(ROOT, "data_store", "flow")
LIVE = os.path.join(FLOW, "cross_live.json")
OC_LIVE = os.path.join(FLOW, "oc_live.json")
H1 = os.path.join(MD, "cross_market_1h.csv")
GIFT = os.path.join(FLOW, "gift_nifty.csv")
DEEP = os.path.join(MD, "yahoo_daily_deep.csv")
SYMS = {"YM=F": "Dow futures", "ES=F": "S&P 500 futures", "NQ=F": "Nasdaq futures", "^N225": "Nikkei 225",
        "^HSI": "Hang Seng", "^KS11": "Kospi", "^STOXX50E": "Euro Stoxx 50", "INR=X": "USD / INR", "DX-Y.NYB": "Dollar index",
        "BZ=F": "Brent crude", "^TNX": "US 10Y yield", "^VIX": "US VIX", "BTC-USD": "Bitcoin", "GC=F": "Gold", "^NSEI": "Nifty 50",
        "000001.SS": "Shanghai Composite"}            # 2026-11-04 (Nikhil): China on the Overview "Global indices" strip
DEEP_ADD = ["YM=F", "^DJI", "^N225", "^HSI", "^KS11", "^STOXX50E", "000001.SS", "BZ=F", "DX-Y.NYB", "^TNX", "^BSESN"]   # 2026-11-04: + Shanghai (weekly narrative)
BACKTEST = os.path.join(ROOT, "data_store", "backtests", "cross_market.json")


def nifty_last_close():
    """(time UTC, price) of the last finished Nifty session, from the 5-minute Yahoo series."""
    df, _ = U.yahoo_chart("^NSEI", "5m", "5d")
    df = df.set_index("ts")
    now = pd.Timestamp.now(tz="UTC")
    today_close = now.normalize() + pd.Timedelta(hours=10)
    cut = today_close if now >= today_close else today_close - pd.Timedelta(days=1)
    x = df[df.index <= cut]
    return x.index[-1], float(x["close"].iloc[-1]), df


def quote(sym, since):
    df, _ = U.yahoo_chart(sym, "5m", "5d")
    df = df.set_index("ts")
    last = df.iloc[-1]
    before = df[df.index <= since]
    base = float(before["close"].iloc[-1]) if len(before) else None
    day = df[df.index >= df.index[-1] - pd.Timedelta(hours=24)]
    return {"last": float(last["close"]), "at": str(df.index[-1]), "since_nifty_close_pct": None if base is None else round((float(last["close"]) / base - 1) * 100, 2),
            "chg_24h_pct": round((float(last["close"]) / float(day["close"].iloc[0]) - 1) * 100, 2),
            "spark": [round(float(v), 4) for v in df["close"].iloc[-120:].values[::4]]}


def gift_nifty():
    s = U._CSESSION
    if s is None:
        return None
    s.get("https://www.nseindia.com/", timeout=20)
    r = s.get("https://www.nseindia.com/api/marketStatus", headers={"Accept": "application/json", "Referer": "https://www.nseindia.com/"}, timeout=20)
    if r.status_code != 200:
        return None
    g = r.json().get("giftnifty") or {}
    if not g:
        return None
    rec = {"recv": pd.Timestamp.now(tz="Asia/Kolkata").strftime("%Y-%m-%d %H:%M:%S"), "last": float(str(g.get("LASTPRICE")).replace(",", "")),
           "chg": g.get("DAYCHANGE"), "pct": g.get("PERCHANGE"), "ts": g.get("TIMESTMP"), "expiry": g.get("EXPIRYDATE")}
    first = not os.path.exists(GIFT)
    pd.DataFrame([rec]).to_csv(GIFT, mode="a", index=False, header=first)
    return rec


def update_hourly():
    """Hourly history, ~2 years the first time, then the last 5 days."""
    have = pd.read_csv(H1) if os.path.exists(H1) else pd.DataFrame(columns=["symbol", "ts"])
    frames = []
    for sym in SYMS:
        rng = "5d" if (have["symbol"] == sym).any() else "730d"
        try:
            df, _ = U.yahoo_chart(sym, "1h", rng)
            frames.append(pd.DataFrame({"symbol": sym, "ts": df["ts"].dt.strftime("%Y-%m-%d %H:%M"), "open": df["open"], "high": df["high"],
                                        "low": df["low"], "close": df["close"], "volume": df["volume"]}))
        except Exception:
            pass
        time.sleep(0.4)
    if frames:
        U.merge_rows(H1, pd.concat(frames), ["symbol", "ts"], ["symbol", "ts"])


def update_deep_new():
    """Long daily history for DEEP_ADD, once. Asks Yahoo for 10 years (range=max silently returns monthly bars) and
    replaces any earlier non-daily rows of those symbols."""
    if not os.path.exists(DEEP):
        return
    cur = pd.read_csv(DEEP, usecols=["symbol", "date"])
    todo = []
    for sym in DEEP_ADD:
        dts = pd.to_datetime(cur.loc[cur["symbol"] == sym, "date"]).sort_values()
        if len(dts) < 400 or dts.diff().dt.days.median() > 1.5:
            todo.append(sym)
    frames = []
    for sym in todo:
        try:
            df, tz = U.yahoo_chart(sym, "1d", "10y")
            d = pd.DataFrame({"date": df["ts"].dt.tz_convert(tz).dt.strftime("%Y-%m-%d"), "symbol": sym, "open": df["open"].round(4),
                              "high": df["high"].round(4), "low": df["low"].round(4), "close": df["close"].round(4),
                              "volume": df["volume"].fillna(0)})
            if pd.to_datetime(d["date"]).sort_values().diff().dt.days.median() <= 1.5:
                frames.append(d)
        except Exception:
            pass
        time.sleep(0.5)
    if frames:
        new = pd.concat(frames)
        old = pd.read_csv(DEEP)
        U.write_atomic(old[~old["symbol"].isin(new["symbol"].unique())], DEEP)
        U.merge_rows(DEEP, new, ["symbol", "date"], ["symbol", "date"])


def expected_open(es_move_pct, bt):
    m = (bt or {}).get("x1", {}).get("hourly_es") or {}
    if es_move_pct is None or not m:
        return None
    g = m["const"] + m["slope"] * es_move_pct / 100
    return {"gap_pct": round(g * 100, 2), "lo_pct": round((g * 100) + m["err_p10"], 2), "hi_pct": round((g * 100) + m["err_p90"], 2),
            "slope": m["slope"], "corr": m["corr"], "sign_right_pct": m["sign_right_pct"], "days": m["days"]}


def step() -> dict:
    os.makedirs(FLOW, exist_ok=True)
    errs = []
    t_close, n_close, _ = nifty_last_close()
    quotes = {}
    for sym, nm in SYMS.items():
        try:
            q = quote(sym, t_close)
            q["name"] = nm
            quotes[sym] = q
        except Exception as e:
            errs.append(f"{sym}: {str(e)[:60]}")
        time.sleep(0.2)
    gift = None
    try:
        gift = gift_nifty()
    except Exception as e:
        errs.append(f"GIFT Nifty: {str(e)[:60]}")
    bt = json.load(open(BACKTEST)) if os.path.exists(BACKTEST) else {}
    es = (quotes.get("ES=F") or {}).get("since_nifty_close_pct")
    eo = expected_open(es, bt)
    if eo:
        eo["nifty_close"] = n_close
        eo["open_est"] = round(n_close * (1 + eo["gap_pct"] / 100), 1)
        eo["open_lo"] = round(n_close * (1 + eo["lo_pct"] / 100), 1)
        eo["open_hi"] = round(n_close * (1 + eo["hi_pct"] / 100), 1)
    if gift:
        gift["vs_nifty_close_pct"] = round((gift["last"] / n_close - 1) * 100, 2)
    now = pd.Timestamp.now(tz="UTC")
    flags = []
    vix = quotes.get("^VIX") or {}
    if (vix.get("since_nifty_close_pct") or 0) > 20:
        flags.append({"event": "US VIX up > 20% since Nifty's close", "tested": "Nifty gapped down ~1% on average, then RECOVERED +0.19% open-to-close (better than luck)"})
    if es is not None and es < -1:
        flags.append({"event": "S&P futures down > 1% since Nifty's close", "tested": "Nifty gapped down ~0.5% on average; the rest of the day was normal"})
    if es is not None and es > 1:
        flags.append({"event": "S&P futures up > 1% since Nifty's close", "tested": "Nifty gapped up ~0.6% on average; the rest of the day was normal"})
    btc = quotes.get("BTC-USD") or {}
    if now.dayofweek in (5, 6, 0) and (btc.get("chg_24h_pct") or 0) < -5:
        flags.append({"event": "BTC down > 5% over the weekend", "tested": "Monday Nifty gapped down ~0.35% on average"})
    out = {"generated": now.strftime("%Y-%m-%d %H:%M"), "nifty_close": {"at": str(t_close), "price": n_close}, "quotes": quotes,
           "gift_nifty": gift, "expected_open": eo, "flags": flags, "errors": errs}
    json.dump(out, open(LIVE + ".tmp", "w"), default=str)
    os.replace(LIVE + ".tmp", LIVE)
    # merge into oc_live.json for the page's 1-minute poll
    try:
        oc = json.load(open(OC_LIVE)) if os.path.exists(OC_LIVE) else {"symbols": {}}
    except Exception:
        oc = {"symbols": {}}
    oc["cross"] = out
    json.dump(oc, open(OC_LIVE + ".tmp", "w"), default=str)
    os.replace(OC_LIVE + ".tmp", OC_LIVE)
    # history files: hourly once an hour, the deep additions once
    stamp = os.path.join(FLOW, "cross_hourly_stamp.txt")
    if not os.path.exists(stamp) or time.time() - os.path.getmtime(stamp) > 3600:
        open(stamp, "w").write(str(now))
        update_hourly()
        update_deep_new()
    return {"quotes": len(quotes), "errors": errs}
