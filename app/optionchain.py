"""
Live option-chain OI build-up for NIFTY and BANKNIFTY, refreshed every 3 minutes in market hours.

Source: NSE's public option-chain API (browser-like session, same as the FII/DII job); Dhan's option chain is the fallback.

For every strike near the money (ATM +-8 strikes, nearest expiry; the next expiry on expiry day) and each side:
    price up   + OI up   = LONG BUILD-UP      price down + OI up   = SHORT BUILD-UP
    price up   + OI down = SHORT COVERING     price down + OI down = LONG UNWINDING
What it means for the INDEX:
    calls: long build-up / short covering = bullish;  short build-up (call writing) / long unwinding = bearish
    puts:  long build-up / short covering = bearish;  short build-up (put writing) / long unwinding = bullish
Three windows: since yesterday's close (NSE's own change columns), last 3 minutes and last 15 minutes (our own
snapshots). Bias score = OI-change-weighted sum of the bullish/bearish reads, scaled to -100..+100.
Snapshots: data_store/flow/option_chain/YYYY-MM-DD.csv.gz; latest summary: data_store/flow/oc_live.json.
"""
from __future__ import annotations

import json
import os
import time

import numpy as np
import pandas as pd

import updater as U

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
OUT = os.path.join(ROOT, "data_store", "flow", "option_chain")
LIVE = os.path.join(ROOT, "data_store", "flow", "oc_live.json")
SYMS = {"NIFTY": 50, "BANKNIFTY": 100}
COLS = ["ts", "symbol", "expiry", "strike", "side", "oi", "chg_oi", "ltp", "chg", "volume", "iv", "underlying"]


def nse_hours(now=None) -> bool:
    now = now or pd.Timestamp.now(tz="Asia/Kolkata")
    return now.weekday() < 5 and (9, 15) <= (now.hour, now.minute) <= (15, 32)


def market_open(now=None) -> bool:
    """Gate used by the app's 3-minute loop. Always open now: the cross-market feed runs 24 hours;
    step() itself only fetches the option chain in NSE hours."""
    return True


# ------------------------------------------------------------------ fetch + parse (parsers are unit-tested offline)
def parse_nse(j: dict, symbol: str, ts: str) -> pd.DataFrame:
    rec = j.get("records") or j
    data = rec.get("data") or []
    und = rec.get("underlyingValue")
    rows = []
    for r in data:
        for side in ("CE", "PE"):
            o = r.get(side)
            if not o:
                continue
            rows.append({"ts": ts, "symbol": symbol, "expiry": r.get("expiryDate") or o.get("expiryDate"),
                         "strike": float(r.get("strikePrice") or o.get("strikePrice")), "side": side,
                         "oi": float(o.get("openInterest") or 0), "chg_oi": float(o.get("changeinOpenInterest") or 0),
                         "ltp": float(o.get("lastPrice") or 0), "chg": float(o.get("change") or 0),
                         "volume": float(o.get("totalTradedVolume") or 0), "iv": float(o.get("impliedVolatility") or 0),
                         "underlying": float(o.get("underlyingValue") or und or np.nan)})
    d = pd.DataFrame(rows, columns=COLS)
    if len(d):
        d["expiry"] = pd.to_datetime(d["expiry"], format="mixed", dayfirst=True).dt.strftime("%Y-%m-%d")
    return d


def parse_dhan(d: dict, symbol: str, ts: str, expiry: str) -> pd.DataFrame:
    """Dhan /v2/optionchain 'data' -> the same columns as parse_nse."""
    rows, und = [], d.get("last_price")
    for k, r in (d.get("oc") or {}).items():
        for side, key in (("CE", "ce"), ("PE", "pe")):
            o = r.get(key) or {}
            if not o:
                continue
            oi, poi = float(o.get("oi") or 0), float(o.get("previous_oi") or 0)
            ltp, pc = float(o.get("last_price") or 0), float(o.get("previous_close_price") or 0)
            rows.append({"ts": ts, "symbol": symbol, "expiry": expiry, "strike": float(k), "side": side,
                         "oi": oi, "chg_oi": oi - poi, "ltp": ltp, "chg": ltp - pc if pc else 0.0,
                         "volume": float(o.get("volume") or 0), "iv": float(o.get("implied_volatility") or 0),
                         "underlying": float(und) if und is not None else np.nan})
    return pd.DataFrame(rows, columns=COLS)


def fetch_nse(symbol: str, ts: str) -> pd.DataFrame:
    s = U._CSESSION
    if s is None:
        raise RuntimeError("needs curl_cffi")
    hdr = {"Referer": "https://www.nseindia.com/option-chain", "Accept": "application/json"}
    s.get("https://www.nseindia.com/option-chain", timeout=30)
    # newer API first (contract info -> nearest expiries -> chain), then the classic endpoint
    try:
        ci = s.get(f"https://www.nseindia.com/api/option-chain-contract-info?symbol={symbol}", headers=hdr, timeout=30)
        if ci.status_code == 200:
            exps = (ci.json().get("expiryDates") or [])[:2]
            parts = []
            for e in exps:
                r = s.get(f"https://www.nseindia.com/api/option-chain-v3?type=Indices&symbol={symbol}&expiry={e}", headers=hdr, timeout=30)
                if r.status_code == 200:
                    parts.append(parse_nse(r.json(), symbol, ts))
            parts = [p for p in parts if len(p)]
            if parts:
                return pd.concat(parts, ignore_index=True)
    except Exception:
        pass
    r = s.get(f"https://www.nseindia.com/api/option-chain-indices?symbol={symbol}", headers=hdr, timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"NSE HTTP {r.status_code}")
    return parse_nse(r.json(), symbol, ts)


def fetch_dhan(symbol: str, ts: str) -> pd.DataFrame:
    """PRIMARY source since 10 Nov 2026 (Nikhil: premiums from Dhan): Dhan option chain, nearest two expiries (one chain call every 3 s)."""
    import dhan_live as DL
    sid = {"NIFTY": 13, "BANKNIFTY": 25}[symbol]
    exps = sorted(DL._post("/optionchain/expirylist", {"UnderlyingScrip": sid, "UnderlyingSeg": "IDX_I"}) or [])[:2]
    parts = []
    for e in exps:
        time.sleep(3.2)                                           # Dhan: one option-chain call every 3 s
        parts.append(parse_dhan(DL._post("/optionchain", {"UnderlyingScrip": sid, "UnderlyingSeg": "IDX_I", "Expiry": e}), symbol, ts, e))
    parts = [p for p in parts if len(p)]
    return pd.concat(parts, ignore_index=True) if parts else pd.DataFrame(columns=COLS)


# ------------------------------------------------------------------ classification
STATE = {(1, 1): "long build-up", (-1, 1): "short build-up", (1, -1): "short covering", (-1, -1): "long unwinding"}
# effect on the index: +1 bullish, -1 bearish
EFFECT = {("CE", "long build-up"): 1, ("CE", "short covering"): 1, ("CE", "short build-up"): -1, ("CE", "long unwinding"): -1,
          ("PE", "long build-up"): -1, ("PE", "short covering"): -1, ("PE", "short build-up"): 1, ("PE", "long unwinding"): 1}


def classify(d_price: float, d_oi: float) -> str | None:
    if d_oi == 0 or d_price == 0 or np.isnan(d_oi) or np.isnan(d_price):
        return None
    return STATE[(int(np.sign(d_price)), int(np.sign(d_oi)))]


def window(now_df: pd.DataFrame, then_df: pd.DataFrame | None, strikes, label: str) -> dict:
    """then_df None -> use NSE's since-yesterday columns."""
    a = now_df[now_df["strike"].isin(strikes)].set_index(["strike", "side"])
    if then_df is None:
        dp, doi = a["chg"], a["chg_oi"]
    else:
        b = then_df[then_df["strike"].isin(strikes)].set_index(["strike", "side"]).reindex(a.index)
        dp, doi = a["ltp"] - b["ltp"], a["oi"] - b["oi"]
    rows, score, tot = [], 0.0, 0.0
    counts = {}
    for (k, side), p_, o_ in zip(a.index, dp.values, doi.values):
        st = classify(p_, o_)
        if st is None:
            continue
        eff = EFFECT[(side, st)]
        score += eff * abs(o_)
        tot += abs(o_)
        counts[f"{side} {st}"] = counts.get(f"{side} {st}", 0) + abs(o_)
        rows.append({"strike": k, "side": side, "state": st, "d_oi": float(o_), "d_price": round(float(p_), 2), "effect": eff})
    bias = None if tot == 0 else int(round(score / tot * 100))
    top = sorted(rows, key=lambda r: -abs(r["d_oi"]))[:6]
    return {"window": label, "bias": bias, "oi_moved": float(tot), "mix": {k: round(v / tot * 100) for k, v in counts.items()} if tot else {},
            "top": top}


def futures_state(sym: str, minutes: int) -> dict | None:
    p = os.path.join(ROOT, "data_store", "market_data", "nifty_futures_1m_upstox.csv")
    if not os.path.exists(p):
        return None
    d = pd.read_csv(p)
    d = d[d["contract"].str.startswith(sym + "_")]
    if d.empty:
        return None
    d["ts"] = pd.to_datetime(d["ts"])
    last_day = d["ts"].dt.date.max()
    d = d[d["ts"].dt.date == last_day]
    c = d.groupby("contract")["volume"].sum().idxmax()
    g = d[d["contract"] == c].sort_values("ts")
    if len(g) <= minutes:
        return None
    a, b = g.iloc[-1], g.iloc[-1 - minutes]
    st = classify(a["close"] - b["close"], a["oi"] - b["oi"])
    return {"contract": c, "minutes": minutes, "state": st, "d_price": round(float(a["close"] - b["close"]), 2),
            "d_oi": float(a["oi"] - b["oi"]), "as_of": str(a["ts"])}


def summarize(sym: str, snaps: pd.DataFrame) -> dict:
    snaps = snaps[(snaps["symbol"] == sym) & snaps["expiry"].notna()]
    snaps = snaps.assign(expiry=snaps["expiry"].astype(str))
    times = sorted(snaps["ts"].unique())
    now_df = snaps[snaps["ts"] == times[-1]]
    und = float(now_df["underlying"].dropna().iloc[0])
    today = str(pd.Timestamp(times[-1]).date())
    exps = sorted(now_df["expiry"].unique())
    near = next((e for e in exps if e > today), exps[0]) if exps[0] == today and len(exps) > 1 else exps[0]
    now_df = now_df[now_df["expiry"] == near]
    step = SYMS[sym]
    atm = round(und / step) * step
    strikes = [atm + i * step for i in range(-8, 9)]

    def at(minutes):
        t0 = pd.Timestamp(times[-1]) - pd.Timedelta(minutes=minutes)
        earlier = [t for t in times if pd.Timestamp(t) <= t0 + pd.Timedelta(seconds=60)]
        if not earlier:
            return None
        x = snaps[(snaps["ts"] == earlier[-1]) & (snaps["expiry"] == near)]
        return x if len(x) else None
    ce, pe = now_df[now_df.side == "CE"].set_index("strike")["oi"], now_df[now_df.side == "PE"].set_index("strike")["oi"]
    band = lambda x: x[(x.index >= und * 0.92) & (x.index <= und * 1.08)]          # walls: within +-8% of spot
    ks = sorted(set(ce.index) | set(pe.index))
    pain = {k: float((ce * np.clip(k - ce.index.values, 0, None)).sum() + (pe * np.clip(pe.index.values - k, 0, None)).sum()) for k in ks}
    wins = [window(now_df, None, strikes, "since yesterday")]
    for mnt in (3, 15):
        th = at(mnt)
        if th is not None:
            wins.append(window(now_df, th, strikes, f"last {mnt} min"))
    near_strikes = now_df[now_df["strike"].isin(strikes)]
    return {"symbol": sym, "as_of": times[-1], "expiry": near, "underlying": und, "atm": atm,
            "pcr": round(float(pe.sum() / max(1, ce.sum())), 2),
            "pcr_near_atm": round(float(near_strikes[near_strikes.side == "PE"]["oi"].sum() / max(1, near_strikes[near_strikes.side == "CE"]["oi"].sum())), 2),
            "max_pain": min(pain, key=pain.get) if pain else None,
            "call_wall": float(band(ce).idxmax()) if len(band(ce)) else None, "put_wall": float(band(pe).idxmax()) if len(band(pe)) else None,
            "windows": wins, "futures": [futures_state(sym, m) for m in (3, 15)],
            "strikes": [{"strike": k, "ce_oi": float(ce.get(k, 0)), "pe_oi": float(pe.get(k, 0)),
                         "ce_chg": float(now_df[(now_df.strike == k) & (now_df.side == "CE")]["chg_oi"].sum()),
                         "pe_chg": float(now_df[(now_df.strike == k) & (now_df.side == "PE")]["chg_oi"].sum())} for k in strikes]}


def step(force=False) -> dict:
    res = {}
    now = pd.Timestamp.now(tz="Asia/Kolkata")
    if force or nse_hours(now):
        try:
            res = chain_step(now)
        except Exception as e:
            res = {"error": str(e)[:200]}
    try:
        import importlib
        import crossmkt
        importlib.reload(crossmkt)
        res["cross"] = crossmkt.step()
    except Exception as e:
        res["cross_error"] = str(e)[:200]
    return res


def chain_step(now) -> dict:
    ts = now.strftime("%Y-%m-%d %H:%M:%S")
    frames, errs = [], []
    try:
        import nsecal as NSEC
        if not NSEC.is_trading_day(now.date()):                # 2026-11-10 (Nikhil): no snapshots on NSE holidays (the site serves a frozen chain)
            return {"generated": ts, "skipped": "NSE holiday", "errors": []}
    except Exception:
        pass
    # 2026-11-10 (Nikhil: "for all option-based strategies get premium prices from Dhan"): Dhan is the primary source for premiums (the
    # same chain every option rule prices from - touch, overnight, vol cycle); NSE's website is the fallback.
    for sym in SYMS:
        try:
            frames.append(fetch_dhan(sym, ts))
        except Exception as e:
            errs.append(f"{sym} Dhan: {str(e)[:80]}")
            try:
                frames.append(fetch_nse(sym, ts))
            except Exception as e2:
                errs.append(f"{sym} NSE: {str(e2)[:80]}")
    frames = [f for f in frames if len(f)]
    if not frames:
        raise RuntimeError("; ".join(errs) or "no option-chain data")
    new = pd.concat(frames, ignore_index=True)
    os.makedirs(OUT, exist_ok=True)
    path = os.path.join(OUT, f"{now:%Y-%m-%d}.csv.gz")
    import gzip
    first = not os.path.exists(path)
    with gzip.open(path, "at") as f:
        new.to_csv(f, index=False, header=first)
    snaps = pd.read_csv(path)
    snaps = snaps[snaps["ts"] != "ts"]
    for c in ("strike", "oi", "chg_oi", "ltp", "chg", "volume", "iv", "underlying"):
        snaps[c] = pd.to_numeric(snaps[c], errors="coerce")
    out = {"generated": ts, "errors": errs, "symbols": {}}
    for sym in SYMS:
        if (snaps["symbol"] == sym).any():
            try:
                out["symbols"][sym] = summarize(sym, snaps)
            except Exception as e:
                out["errors"].append(f"{sym} summary: {str(e)[:100]}")
    try:                                           # keep the cross-market block written by crossmkt.py
        old = json.load(open(LIVE))
        if "cross" in old:
            out["cross"] = old["cross"]
    except Exception:
        pass
    tmp = LIVE + ".tmp"
    json.dump(out, open(tmp, "w"), default=str)
    os.replace(tmp, LIVE)
    return {"rows": len(new), "errors": errs}
