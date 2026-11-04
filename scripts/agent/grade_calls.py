#!/usr/bin/env python3
"""
Synaptic desk - grade the agents' registered calls (2026-10-17). Run each evening by the Learner (LR1), after Market Pulse
has saved the day's 1-minute futures bars and the NSE end-of-day file.

Kinds (logs/agent_calls.csv, written by agent_log.py call)
  directional  entry zone touched within the entry window -> first of stop / target, else exit at the horizon close.
               Same bar hits both = stop (conservative). R = signed move / |entry - stop|, net of a fixed cost.
               Baseline: the same direction entered at market at call time (does the entry zone help?).
  range        did price stay inside [range_lo, range_hi] for the whole horizon?
  no_trade     move over the horizon vs the median move of the last 60 sessions at the same horizon (< 1 = quiet: good call)
Data: NIFTY / BANKNIFTY minutes = near-month futures 1-min (nifty_futures_1m_upstox.csv) rescaled to index level with the
previous session's index / futures close ratio; days = yahoo_daily_deep (index) or nse_stocks_eod.csv (stocks).
Outputs: logs/calls_graded.csv (one row per call, final) and logs/learning_scorecard.json (by persona, activity, label,
regime, v2 character, confidence bucket; calibration; "too early" below 30 graded calls).
Option structures are graded on the underlying's path only (premium, IV and theta are not modelled) - say so when quoting.
"""
import csv, datetime as dt, json, os, re, sys
import numpy as np
import pandas as pd

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
AG = os.environ.get("SYNAPTIC_AGENT_DIR") or os.path.join(ROOT, "data_store", "agent")
LOGS = os.path.join(AG, "logs")
MD = os.path.join(ROOT, "data_store", "market_data")
COST_PTS = {"NIFTY": 2.0, "BANKNIFTY": 5.0}          # round trip, futures, points
COST_PCT_STOCK = 0.0006                              # 0.06% round trip
IDX = {"NIFTY": "^NSEI", "BANKNIFTY": "^NSEBANK"}
YDEEP = {"SP500": "^GSPC", "ES": "ES=F", "NDX": "NQ=F", "NQ": "NQ=F", "BTC": "BTC-USD"}
US_DAILY = {"SPY", "QQQ", "RSP", "IWM", "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA"}
INTRA = {"SP500": "ES=F", "ES": "ES=F", "NDX": "NQ=F", "NQ": "NQ=F", "SPY": "SPY", "BTC": "BTC-USD", "ETH": "ETH-USD", "SOL": "SOL-USD"}
COST_PCT = {"BTC": 0.0015, "ETH": 0.0015, "SOL": 0.0015, "SPY": 0.001, "QQQ": 0.001}   # CoinDCX taker both ways; US platform approx
GRADED_COLS = ["call_id", "graded_at", "symbol", "kind", "direction", "result", "entry_px", "exit_px", "exit_reason", "r", "r_net",
               "mfe_r", "mae_r", "baseline_r", "held", "breach_pct", "move_ratio", "note"]
_CACHE = {}
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))


def hz(h):
    n, u = int(h[:-1]), h[-1]
    return n, u


def daily(sym):
    if sym in _CACHE:
        return _CACHE[sym]
    if sym in IDX or sym in YDEEP:
        y = pd.read_csv(os.path.join(MD, "yahoo_daily_deep.csv"))
        d = y[y["symbol"] == (IDX.get(sym) or YDEEP[sym])].copy()
    elif sym in ("ETH", "SOL"):
        y = pd.read_csv(os.path.join(MD, "binance_daily.csv"))
        d = y[y["symbol"] == sym + "USDT"].copy()
    elif sym in US_DAILY:
        y = pd.read_csv(os.path.join(MD, "history", "yahoo_us_daily.csv"))
        d = y[y["symbol"] == sym].copy()
    else:
        y = pd.read_csv(os.path.join(MD, "nse_stocks_eod.csv"))
        d = y[y["symbol"] == sym].copy()
    d["date"] = pd.to_datetime(d["date"])
    d = d.drop_duplicates("date", keep="last").set_index("date").sort_index()[["open", "high", "low", "close"]].astype(float)
    _CACHE[sym] = d
    return d


def minutes(sym):
    k = "m:" + sym
    if k in _CACHE:
        return _CACHE[k]
    f = pd.read_csv(os.path.join(MD, "nifty_futures_1m_upstox.csv"))
    f = f[f["contract"].str.startswith(sym + "_FUT")].copy()
    f["ts"] = pd.to_datetime(f["ts"].str[:19])
    f["day"] = f["ts"].dt.normalize()
    v = f.groupby(["day", "contract"])["volume"].sum().reset_index()
    best = v.loc[v.groupby("day")["volume"].idxmax()]
    f = f.merge(best[["day", "contract"]], on=["day", "contract"]).set_index("ts").sort_index()
    # index / futures ratio from the previous session's closes (index level for 'index' basis calls)
    dly = daily(sym)["close"]
    fut_close = f.groupby("day")["close"].last()
    ratio = (dly.reindex(fut_close.index) / fut_close).shift(1).ffill()
    f["ratio"] = f["day"].map(ratio).fillna(1.0)
    _CACHE[k] = f
    return f


def path(call):
    """Bars after the call time up to the horizon end, at the call's price basis; None if not enough data yet."""
    ts = pd.Timestamp(call["ts"][:19])
    n, u = hz(call["horizon"])
    sym = call["symbol"]
    if u == "m":
        if sym not in COST_PTS:
            if sym not in INTRA:
                return None, "intraday grading not available for this symbol"
            D = pd.read_csv(os.path.join(MD, "synaptic_intraday_all.csv"))
            D = D[(D["symbol"] == INTRA[sym]) & (D["interval"] == "5m")].copy()
            D["t"] = pd.to_datetime(D["datetime_utc"].astype(str).str[:19]) + pd.Timedelta(hours=5, minutes=30)   # to IST like the call ts
            D = D.drop_duplicates("t").set_index("t").sort_index()
            b = D[D.index > ts].iloc[: max(1, n // 5)][["open", "high", "low", "close"]].astype(float)
            if len(b) < max(1, n // 5):
                return None, "intraday data not complete yet"
            return b, None
        f = minutes(sym)
        b = f[(f.index > ts) & (f["day"] == ts.normalize())].iloc[:n]
        if len(b) < min(n, 10) or (b.index[-1] < ts + pd.Timedelta(minutes=min(n, 375) - 1) and b.index[-1].hour < 15):
            return None, "session data not complete yet"
        if call.get("basis", "index") == "index":
            b = b.copy()
            for c in ("open", "high", "low", "close"):
                b[c] = b[c] * b["ratio"]
        return b[["open", "high", "low", "close"]], None
    d = daily(sym)
    start = ts.normalize() + (pd.Timedelta(days=1) if ts.hour * 60 + ts.minute >= 930 else pd.Timedelta(0))
    b = d[d.index >= start].iloc[:n]
    if len(b) < n:
        return None, f"{len(b)}/{n} sessions so far"
    return b, None


def grade_directional(call, b):
    long_ = call["direction"] == "long"
    stop, tgt = float(call["stop"]), (float(call["target"]) if call["target"] else None)
    lo = float(call["entry_lo"]) if call["entry_lo"] else None
    hi = float(call["entry_hi"]) if call["entry_hi"] else lo
    n, u = hz(call["horizon"])
    win = min(n, 60) if u == "m" else min(n, 2)
    entry, k0 = None, None
    for i, (t, r) in enumerate(b.iterrows()):
        if i >= win:
            break
        if lo is None:
            entry, k0 = r["open"], i
            break
        if r["low"] <= hi and r["high"] >= lo:
            entry, k0 = min(max(r["open"], lo), hi), i
            break
    base_entry = b["open"].iloc[0]
    out = {"baseline_r": None}

    def run(entry, k0):
        risk = abs(entry - stop)
        if risk <= 0:
            return None
        mfe = mae = 0.0
        for t, r in b.iloc[k0:].iterrows():
            fav = (r["high"] - entry) if long_ else (entry - r["low"])
            adv = (entry - r["low"]) if long_ else (r["high"] - entry)
            mfe, mae = max(mfe, fav), max(mae, adv)
            hit_stop = r["low"] <= stop if long_ else r["high"] >= stop
            hit_tgt = tgt is not None and (r["high"] >= tgt if long_ else r["low"] <= tgt)
            if hit_stop:
                return stop, "stop", risk, mfe, mae
            if hit_tgt:
                return tgt, "target", risk, mfe, mae
        return b["close"].iloc[-1], "time", risk, mfe, mae

    if (long_ and base_entry > stop) or (not long_ and base_entry < stop):
        rb = run(base_entry, 0)
        if rb:
            out["baseline_r"] = round(((rb[0] - base_entry) if long_ else (base_entry - rb[0])) / rb[2], 2)
    if entry is None:
        return {**out, "result": "not_filled", "note": f"entry zone not reached in {win} {'min' if u == 'm' else 'sessions'}"}
    if (long_ and entry <= stop) or (not long_ and entry >= stop):
        return {**out, "result": "invalid", "note": "entry beyond stop"}
    ex, why, risk, mfe, mae = run(entry, k0)
    r = ((ex - entry) if long_ else (entry - ex)) / risk
    cost = (COST_PTS[call["symbol"]] if call["symbol"] in COST_PTS else entry * COST_PCT.get(call["symbol"], COST_PCT_STOCK)) / risk
    res = "win" if r > 0 else "loss" if r < 0 else "flat"
    return {**out, "result": res, "entry_px": round(entry, 2), "exit_px": round(ex, 2), "exit_reason": why, "r": round(r, 2),
            "r_net": round(r - cost, 2), "mfe_r": round(mfe / risk, 2), "mae_r": round(mae / risk, 2)}


def grade_range(call, b):
    lo, hi = float(call["range_lo"]), float(call["range_hi"])
    up, dn = b["high"].max(), b["low"].min()
    held = up <= hi and dn >= lo
    breach = max(0.0, up / hi - 1, 1 - dn / lo) * 100
    return {"result": "held" if held else "broke", "held": held, "breach_pct": round(breach, 2), "exit_px": round(b["close"].iloc[-1], 2)}


def grade_no_trade(call, b):
    n, u = hz(call["horizon"])
    move = abs(b["close"].iloc[-1] / b["open"].iloc[0] - 1)
    if u == "m":
        f = minutes(call["symbol"])
        hist = []
        for day, g in f.groupby("day"):
            g = g[g.index.time >= b.index[0].time()].iloc[:n]
            if len(g) == min(n, len(g)) and len(g) > 5 and day < b.index[0].normalize():
                hist.append(abs(g["close"].iloc[-1] / g["open"].iloc[0] - 1))
        hist = hist[-60:]
    else:
        c = daily(call["symbol"])["close"]
        c = c[c.index < b.index[0]]
        hist = list((c / c.shift(n) - 1).abs().dropna().iloc[-60:])
    if len(hist) < 10:
        return {"result": "too_little_history", "note": f"{len(hist)} comparable windows"}
    ratio = move / float(np.median(hist))
    return {"result": "quiet" if ratio < 1 else "moved", "move_ratio": round(ratio, 2)}


def scorecard(G, C):
    g = G.merge(C[["call_id", "persona", "activity", "labels", "regime", "v2_character", "confidence"]], on="call_id", how="left")
    d = g[g["kind"] == "directional"]
    d = d[d["result"].isin(["win", "loss", "flat"])].copy()
    d["r_net"] = pd.to_numeric(d["r_net"], errors="coerce")
    d["baseline_r"] = pd.to_numeric(d["baseline_r"], errors="coerce")

    def block(x):
        n = len(x)
        if n == 0:
            return None
        return {"n": n, "hit_pct": round(100 * (x["result"] == "win").mean()), "avg_r_net": round(float(x["r_net"].mean()), 2),
                "avg_baseline_r": None if x["baseline_r"].isna().all() else round(float(x["baseline_r"].mean()), 2),
                "verdict": "too early" if n < 30 else ("adds value" if x["r_net"].mean() > 0.05 else "not adding value")}

    out = {"generated": dt.datetime.now(IST).isoformat(timespec="seconds"), "directional": block(d),
           "not_filled": int((g["result"] == "not_filled").sum())}
    for key in ("persona", "activity", "regime"):
        out["by_" + key] = {k: block(v) for k, v in d.groupby(key)}
    d["lab_top"] = d["labels"].fillna("").str.split("|").str[0]
    out["by_label"] = {k: block(v) for k, v in d.groupby("lab_top")}
    d["conf"] = pd.to_numeric(d["confidence"], errors="coerce")
    d["conf_b"] = pd.cut(d["conf"], [0, 40, 55, 70, 101], labels=["<40", "40-55", "55-70", "70+"])
    out["calibration"] = {str(k): {"n": len(v), "stated": round(float(v["conf"].mean())) if len(v) else None,
                                   "actual_hit_pct": round(100 * (v["result"] == "win").mean()) if len(v) else None}
                          for k, v in d.groupby("conf_b", observed=True)}
    rg = g[g["kind"] == "range"]
    out["range"] = {"n": len(rg), "held_pct": round(100 * (rg["result"] == "held").mean()) if len(rg) else None}
    nt = g[g["kind"] == "no_trade"]
    out["no_trade"] = {"n": len(nt), "quiet_pct": round(100 * (nt["result"] == "quiet").mean()) if len(nt) else None}
    return out


def main():
    cp = os.path.join(LOGS, "agent_calls.csv")
    if not os.path.exists(cp):
        print("no calls registered yet"); return
    C = pd.read_csv(cp, dtype=str).fillna("")
    gp = os.path.join(LOGS, "calls_graded.csv")
    done = set(pd.read_csv(gp, dtype=str)["call_id"]) if os.path.exists(gp) else set()
    rows, waiting = [], []
    for _, c in C.iterrows():
        if c["call_id"] in done:
            continue
        try:
            b, why = path(c)
        except Exception as e:
            b, why = None, f"data error: {str(e)[:80]}"
        if b is None:
            waiting.append(f"{c['call_id']}: {why}")
            continue
        g = {"directional": grade_directional, "range": grade_range, "no_trade": grade_no_trade}[c["kind"]](c, b)
        rows.append({"call_id": c["call_id"], "graded_at": dt.datetime.now(IST).isoformat(timespec="seconds"), "symbol": c["symbol"],
                     "kind": c["kind"], "direction": c["direction"], **g})
    if rows:
        new = not os.path.exists(gp)
        with open(gp, "a", newline="") as f:
            w = csv.DictWriter(f, fieldnames=GRADED_COLS, extrasaction="ignore")
            if new:
                w.writeheader()
            w.writerows(rows)
    G = pd.read_csv(gp, dtype=str) if os.path.exists(gp) else pd.DataFrame(columns=GRADED_COLS)
    sc = scorecard(G, C) if len(G) else {"directional": None}
    sc["waiting"] = waiting[:50]
    json.dump(sc, open(os.path.join(LOGS, "learning_scorecard.json"), "w"), indent=1, default=str)
    print(f"graded {len(rows)} new, {len(waiting)} waiting, {len(G)} total")
    for r in rows:
        print("  ", r["call_id"], r["result"], r.get("r_net", ""), r.get("note", ""))
    print("overall:", sc.get("directional"))


if __name__ == "__main__":
    main()
