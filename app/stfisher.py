"""ST + FISHER + ADX - NIFTY OPTION BUYING, LIVE PAPER RULE (2026-11-11, Nikhil: "onboard this to our paper trade").
Rules from ST_Fisher_ADX_Option_Buying_Strategy.pdf v1.0 (MASTER_BACKTESTER/experiments/st_fisher_adx/backtest.py, final settings
run(df, vix, dict(flat_bars=0, delta=0.6, next_week=True))). PROVISIONAL - paper only, never orders. Not advice.

Setup      Nifty 50 spot index, 5-minute candles (Dhan IDX_I 13). Supertrend(10, 3), Fisher Transform(9) with signal = Fisher one bar back,
           ADX(14, 14) - the backtest's own TradingView-style functions, copied below unchanged.
Arm        long: Fisher crosses above its signal while Fisher < 0 (and Fisher is not sideways: max - min over 20 bars >= 1.0);
           a cross the other way cancels it. Mirror for puts.
Signal     on a CLOSED 5-min candle between 09:30 and 14:30: armed, Supertrend green (red for puts), ADX rising, Fisher >= 0 (<= 0 for puts).
Trigger    buy when a later candle trades above the signal candle's high (puts: below its low). Valid for the next 3 five-minute candles
           and until 14:45; cancelled if Supertrend changes colour first. Checked live on 1-minute candles.
Option     next-week expiry (the one after the nearest), the strike whose delta is closest to 0.6 on the in-the-money side (Black-Scholes
           with each strike's own Dhan IV), 1 lot. Paper fill = the live ASK (buy) / BID (sell) from the Dhan option chain; the last
           traded price and the backtest's model price are logged beside it (the paper-trade plan's fill check).
Stop       on the index: calls - lower of the signal candle's low and the candle before it; puts - higher of the two highs. Exit at once
           when touched (1-minute check).
Exits      Supertrend changes colour on a 5-min close -> exit at once (next candle open); 15:15 time stop; no target; no flat-line exit.
Costs      Rs 25 + 0.10 % of premium per side. One position at a time. Lot from the Dhan F&O master (fallback 65).
Go / no-go (from the PDF): 2-3 months, every signal; real fills within about 1 point of the model and profit factor above 1.2 over 60+ trades.
"""
from __future__ import annotations

import json
import math
import os
import time
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
OUT = os.path.join(ROOT, "data_store", "livedesk", "stfisher")
SID = 13
LOT_FALLBACK = 65
R = 0.065
COST_FIXED, COST_PCT = 25.0, 0.0010
CFG = dict(st_len=10, st_mult=3.0, fish_len=9, adx_len=14, nz=0.0, flat_rng=1.0, flat_win=20, wait=3,
           sig_start="09:30", sig_end="14:30", fill_end="14:45", exit_time="15:15", delta=0.6)
EVERY = 30
S: dict = {"t": 0.0, "busy": False, "err": None, "hist": None, "hist_day": None, "lot": None}


# ---------------- indicators: copied unchanged from MASTER_BACKTESTER/experiments/st_fisher_adx/backtest.py ----------------
def rma(x, n):
    return pd.Series(x).ewm(alpha=1.0 / n, adjust=False).mean().values


def supertrend(h, l, c, n, mult):
    tr = np.maximum(h - l, np.maximum(np.abs(h - np.roll(c, 1)), np.abs(l - np.roll(c, 1))))
    tr[0] = h[0] - l[0]
    atr = rma(tr, n)
    hl2 = (h + l) / 2
    up = hl2 - mult * atr
    dn = hl2 + mult * atr
    trend = np.ones(len(c), dtype=int)
    for i in range(1, len(c)):
        if c[i - 1] > up[i - 1]:
            up[i] = max(up[i], up[i - 1])
        if c[i - 1] < dn[i - 1]:
            dn[i] = min(dn[i], dn[i - 1])
        t = trend[i - 1]
        if t == -1 and c[i] > dn[i - 1]:
            t = 1
        elif t == 1 and c[i] < up[i - 1]:
            t = -1
        trend[i] = t
    line = np.where(trend == 1, up, dn)
    return trend, line


def fisher(h, l, n):
    hl2 = pd.Series((h + l) / 2)
    hi = hl2.rolling(n, min_periods=1).max().values
    lo = hl2.rolling(n, min_periods=1).min().values
    x = hl2.values
    val = np.zeros(len(x)); fish = np.zeros(len(x))
    for i in range(len(x)):
        rng = hi[i] - lo[i]
        ratio = (x[i] - lo[i]) / rng if rng > 0 else 0.5
        pv = val[i - 1] if i else 0.0
        v = 0.66 * (ratio - 0.5) + 0.67 * pv
        v = max(min(v, 0.999), -0.999)
        val[i] = v
        pf = fish[i - 1] if i else 0.0
        fish[i] = 0.5 * math.log((1 + v) / (1 - v)) + 0.5 * pf
    trig = np.concatenate([[0.0], fish[:-1]])
    return fish, trig


def adx(h, l, c, n):
    up = np.diff(h, prepend=h[0]); dn = -np.diff(l, prepend=l[0])
    pdm = np.where((up > dn) & (up > 0), up, 0.0)
    mdm = np.where((dn > up) & (dn > 0), dn, 0.0)
    tr = np.maximum(h - l, np.maximum(np.abs(h - np.roll(c, 1)), np.abs(l - np.roll(c, 1))))
    tr[0] = h[0] - l[0]
    trur = rma(tr, n)
    pdi = 100 * rma(pdm, n) / trur
    mdi = 100 * rma(mdm, n) / trur
    s = pdi + mdi
    dx = np.abs(pdi - mdi) / np.where(s == 0, 1, s)
    return 100 * rma(dx, n)


def ncdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def bs(Sp, K, T, sig, typ):
    if T <= 0 or sig <= 0:
        return max(Sp - K, 0.0) if typ == "CE" else max(K - Sp, 0.0)
    d1 = (math.log(Sp / K) + (R + 0.5 * sig * sig) * T) / (sig * math.sqrt(T))
    d2 = d1 - sig * math.sqrt(T)
    if typ == "CE":
        return Sp * ncdf(d1) - K * math.exp(-R * T) * ncdf(d2)
    return K * math.exp(-R * T) * ncdf(-d2) - Sp * ncdf(-d1)


def bs_delta(Sp, K, T, sig, typ):
    if T <= 0 or sig <= 0:
        return (1.0 if Sp > K else 0.0) if typ == "CE" else (-1.0 if Sp < K else 0.0)
    d1 = (math.log(Sp / K) + (R + 0.5 * sig * sig) * T) / (sig * math.sqrt(T))
    return ncdf(d1) if typ == "CE" else ncdf(d1) - 1.0


# ---------------- state ----------------
def _path():
    return os.path.join(OUT, "state.json")


def _load():
    try:
        return json.load(open(_path()))
    except Exception:
        return {"trades": [], "day": None, "armed": {"1": False, "-1": False}, "pending": None, "pos": None, "last5": None, "last1": None, "log": []}


def _save(st):
    os.makedirs(OUT, exist_ok=True)
    tmp = _path() + ".tmp"
    json.dump(st, open(tmp, "w"), indent=1, default=str)
    os.replace(tmp, _path())


def _log(st, msg):
    st.setdefault("log", []).append(f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}")
    st["log"] = st["log"][-200:]


# ---------------- data ----------------
def _bars(mins, d0, d1):
    import dhan_live as DL
    b = DL.intraday("IDX_I", SID, "INDEX", d0, d1, mins, oi=False)
    return [{"ts": x["ts"].replace(tzinfo=None), "o": x["open"], "h": x["high"], "l": x["low"], "c": x["close"]} for x in b]


def _hist5(today):
    """Previous sessions' 5-minute candles (warm-up for the indicators), fetched once a day."""
    if S.get("hist_day") == today and S.get("hist"):
        return S["hist"]
    d = date.fromisoformat(today)
    h = [b for b in _bars(5, d - timedelta(days=12), d - timedelta(days=1)) if "09:15" <= b["ts"].strftime("%H:%M") < "15:30"]
    S.update(hist=h, hist_day=today)
    return h


def _five_from_one(b1, now):
    """Closed 5-minute candles (09:15 grid) from today's closed 1-minute candles."""
    g = {}
    for b in b1:
        t = b["ts"]; m = t.hour * 60 + t.minute - (9 * 60 + 15)
        if m < 0 or t.strftime("%H:%M") >= "15:30":
            continue
        k = t.replace(hour=9, minute=15, second=0) + timedelta(minutes=(m // 5) * 5)
        x = g.get(k)
        if x is None:
            g[k] = {"ts": k, "o": b["o"], "h": b["h"], "l": b["l"], "c": b["c"]}
        else:
            x["h"] = max(x["h"], b["h"]); x["l"] = min(x["l"], b["l"]); x["c"] = b["c"]
    return [g[k] for k in sorted(g) if k + timedelta(minutes=5) <= now]


def _lot():
    if S.get("lot"):
        return S["lot"]
    lot = LOT_FALLBACK
    try:
        import fnoflow as FF
        fs = (FF.universe().get("NIFTY") or {}).get("futs") or []
        if fs and fs[0][2]:
            lot = int(fs[0][2])
    except Exception:
        pass
    S["lot"] = lot
    return lot


def _chain(expiry):
    import idxstrangle as IS                        # same Dhan option-chain call (3 s pacing) the strangle rule uses
    return IS._chain(SID, expiry)


def _expiries():
    import idxstrangle as IS
    return IS._expiries(SID)


def _pick(side, spot, now):
    """Next-week expiry, strike with |delta| closest to 0.6 on the ITM side, live quotes. Returns dict or None."""
    today = now.strftime("%Y-%m-%d")
    exps = [e for e in _expiries() if e >= today]
    if len(exps) < 2:
        return None
    exp = exps[1]                                   # the one after the nearest
    sp, rows = _chain(exp)
    typ = "CE" if side == 1 else "PE"
    T = max((datetime.fromisoformat(exp + " 15:30") - now).total_seconds(), 60) / (365 * 86400)
    K0 = round(spot / 50) * 50
    best = None
    for k in range(-20, 21):
        K = K0 + 50 * k
        q = rows.get((float(K), typ))
        if not q or not (q.get("ask") or q.get("ltp")):
            continue
        iv = (q.get("iv") or 0) / 100.0
        if iv <= 0:
            continue
        dlt = abs(bs_delta(spot, K, T, iv, typ))
        if best is None or abs(dlt - CFG["delta"]) < abs(best["delta"] - CFG["delta"]):
            best = {"exp": exp, "K": K, "typ": typ, "delta": round(dlt, 3), "iv": round(iv * 100, 2), "bid": q.get("bid"), "ask": q.get("ask"),
                    "ltp": q.get("ltp"), "oi": q.get("oi"), "T": T, "model": round(bs(spot, K, T, iv, typ), 2)}
    return best


def _quote(pos):
    sp, rows = _chain(pos["exp"])
    q = rows.get((float(pos["K"]), pos["typ"])) or {}
    return q, sp


def _cost(p0, p1, lot):
    return 2 * COST_FIXED + COST_PCT * (p0 + p1) * lot


# ---------------- the engine ----------------
def _indicators(c5):
    h = np.array([b["h"] for b in c5], float); l = np.array([b["l"] for b in c5], float); c = np.array([b["c"] for b in c5], float)
    trend, stl = supertrend(h, l, c, CFG["st_len"], CFG["st_mult"])
    fish, trig = fisher(h, l, CFG["fish_len"])
    ax = adx(h, l, c, CFG["adx_len"])
    frng = pd.Series(fish).rolling(CFG["flat_win"], min_periods=CFG["flat_win"])
    frng = (frng.max() - frng.min()).values
    return trend, fish, trig, ax, frng


def _armed_replay(c5, upto):
    """Armed flags as the backtest would have them after bar `upto` (crosses only; used at the start of a day)."""
    trend, fish, trig, ax, frng = _indicators(c5)
    armed = {1: False, -1: False}
    for i in range(1, upto + 1):
        if fish[i] > trig[i] and fish[i - 1] <= trig[i - 1]:
            armed[-1] = False
            if fish[i] < 0:
                armed[1] = not (CFG["flat_rng"] and not (frng[i] >= CFG["flat_rng"]))
        if fish[i] < trig[i] and fish[i - 1] >= trig[i - 1]:
            armed[1] = False
            if fish[i] > 0:
                armed[-1] = not (CFG["flat_rng"] and not (frng[i] >= CFG["flat_rng"]))
    return armed


def _open(st, side, idx_fill, sl, now, sig_ts):
    lot = _lot()
    pk = _pick(side, idx_fill, now)
    if not pk or not (pk.get("ask") or pk.get("ltp")):
        _log(st, f"signal {'CALL' if side == 1 else 'PUT'} at {idx_fill:.1f}: no usable option quote - skipped")
        st.setdefault("missed", []).append({"ts": now.strftime("%Y-%m-%d %H:%M"), "side": side, "why": "no option quote"})
        return
    entry = float(pk["ask"] or pk["ltp"])
    st["pos"] = {"side": side, "opened": now.strftime("%Y-%m-%d %H:%M"), "sig_ts": sig_ts, "idx_entry": round(idx_fill, 2), "sl": round(sl, 2),
                 "exp": pk["exp"], "K": pk["K"], "typ": pk["typ"], "delta": pk["delta"], "iv": pk["iv"], "lot": lot,
                 "entry": entry, "entry_ltp": pk["ltp"], "entry_bid": pk["bid"], "entry_model": pk["model"], "oi": pk["oi"], "mark": entry, "idx_now": idx_fill}
    _log(st, f"OPEN {pk['typ']} {pk['K']} {pk['exp']} @ ask {entry} (ltp {pk['ltp']}, model {pk['model']}), index {idx_fill:.1f}, stop {sl:.1f}")


def _close(st, idx_px, why, now):
    p = st["pos"]
    try:
        q, _ = _quote(p)
    except Exception as e:
        q = {}; _log(st, f"exit quote failed: {e}")
    px = float(q.get("bid") or q.get("ltp") or p.get("mark") or 0)
    lot = p["lot"]; gross = (px - p["entry"]) * lot; cost = _cost(p["entry"], px, lot)
    t = dict(p, status="closed", closed=now.strftime("%Y-%m-%d %H:%M"), exit=px, exit_ltp=q.get("ltp"), idx_exit=round(idx_px, 2), exit_reason=why,
             gross=round(gross), cost=round(cost), pnl=round(gross - cost),
             r_idx=round((idx_px - p["idx_entry"]) * p["side"] / abs(p["idx_entry"] - p["sl"]), 3) if abs(p["idx_entry"] - p["sl"]) > 0 else None)
    st["trades"].append(t); st["pos"] = None
    _log(st, f"CLOSE {p['typ']} {p['K']} @ bid {px} ({why}), index {idx_px:.1f}, P&L Rs {t['pnl']}")


def _work():
    st = _load()
    now = datetime.now().replace(microsecond=0)
    today = now.strftime("%Y-%m-%d"); hm = now.strftime("%H:%M")
    if now.weekday() >= 5 or not ("09:16" <= hm <= "15:35"):
        return
    b1 = [b for b in _bars(1, now.date(), now.date()) if b["ts"] + timedelta(minutes=1) <= now]
    if not b1:
        return                                                          # holiday or no feed yet
    hist = _hist5(today)
    if st.get("day") != today:                                          # new session: carry the armed flags from the warm-up bars
        st["armed"] = {str(k): v for k, v in _armed_replay(hist, len(hist) - 1).items()} if len(hist) > 30 else {"1": False, "-1": False}
        st["day"] = today; st["pending"] = None; st["last1"] = None; st["last5"] = None
        if st.get("pos"):                                               # should not happen (15:15 exit) - close it on the first quote
            _close(st, b1[0]["o"], "eod_guard", now)
    c5 = hist + _five_from_one(b1, now)
    n_hist = len(hist)
    last1 = st.get("last1")
    new1 = [b for b in b1 if last1 is None or b["ts"].strftime("%Y-%m-%d %H:%M") > last1]
    new5_idx = [i for i in range(n_hist, len(c5)) if st.get("last5") is None or c5[i]["ts"].strftime("%Y-%m-%d %H:%M") > st["last5"]]
    # 1) minute-level: stop and time exits, pending trigger fills (in time order)
    for b in new1:
        bh = b["ts"].strftime("%H:%M")
        p = st.get("pos")
        if p:
            if bh >= CFG["exit_time"]:
                _close(st, b["o"], "time 15:15", now); p = None
            elif (p["side"] == 1 and b["l"] <= p["sl"]) or (p["side"] == -1 and b["h"] >= p["sl"]):
                gap = (p["side"] == 1 and b["o"] <= p["sl"]) or (p["side"] == -1 and b["o"] >= p["sl"])
                _close(st, b["o"] if gap else p["sl"], "stop (gap)" if gap else "stop", now); p = None
            else:
                p["idx_now"] = b["c"]
        pend = st.get("pending")
        if pend and not st.get("pos"):
            if bh > CFG["fill_end"] or b["ts"].strftime("%Y-%m-%d %H:%M") >= pend["expires"]:
                _log(st, f"trigger expired ({'CALL' if pend['side'] == 1 else 'PUT'} level {pend['level']})"); st["pending"] = None
            elif (pend["side"] == 1 and b["h"] >= pend["level"]) or (pend["side"] == -1 and b["l"] <= pend["level"]):
                fill = max(b["o"], pend["level"]) if pend["side"] == 1 else min(b["o"], pend["level"])
                st["pending"] = None
                _open(st, pend["side"], fill, pend["sl"], now, pend["sig_ts"])
                p = st.get("pos")
                if p and ((p["side"] == 1 and b["l"] <= p["sl"]) or (p["side"] == -1 and b["h"] >= p["sl"])):
                    _close(st, p["sl"], "stop (fill minute)", now)
        st["last1"] = b["ts"].strftime("%Y-%m-%d %H:%M")
    # 2) five-minute closes: trend exit, cancel pending on colour change, arm / signal
    if new5_idx:
        trend, fish, trig, ax, frng = _indicators(c5)
        armed = {1: bool(st["armed"].get("1")), -1: bool(st["armed"].get("-1"))}
        for i in new5_idx:
            bar = c5[i]; tod = bar["ts"].strftime("%H:%M"); tclose = (bar["ts"] + timedelta(minutes=5)).strftime("%H:%M")
            p = st.get("pos")
            if p and trend[i] != p["side"]:
                _close(st, bar["c"], "supertrend flip", now)
            pend = st.get("pending")
            if pend and trend[i] != pend["side"]:
                _log(st, "trigger cancelled: Supertrend changed colour"); st["pending"] = None
            if fish[i] > trig[i] and fish[i - 1] <= trig[i - 1]:
                armed[-1] = False
                if fish[i] < 0:
                    armed[1] = not (CFG["flat_rng"] and not (frng[i] >= CFG["flat_rng"]))
            if fish[i] < trig[i] and fish[i - 1] >= trig[i - 1]:
                armed[1] = False
                if fish[i] > 0:
                    armed[-1] = not (CFG["flat_rng"] and not (frng[i] >= CFG["flat_rng"]))
            if st.get("pos") is None and st.get("pending") is None and CFG["sig_start"] <= tod <= CFG["sig_end"]:
                for s in (1, -1):
                    if not armed[s] or trend[i] != s or not (ax[i] > ax[i - 1]):
                        continue
                    if not (fish[i] >= -CFG["nz"] if s == 1 else fish[i] <= CFG["nz"]):
                        continue
                    sl = min(c5[i - 1]["l"], bar["l"]) if s == 1 else max(c5[i - 1]["h"], bar["h"])
                    exp_ts = (bar["ts"] + timedelta(minutes=5 * (CFG["wait"] + 1))).strftime("%Y-%m-%d %H:%M")   # the next 3 candles
                    st["pending"] = {"side": s, "level": round(bar["h"] if s == 1 else bar["l"], 2), "sl": round(sl, 2), "expires": exp_ts,
                                     "sig_ts": bar["ts"].strftime("%Y-%m-%d %H:%M"), "adx": round(float(ax[i]), 1), "fish": round(float(fish[i]), 2)}
                    armed[s] = False
                    _log(st, f"SIGNAL {'CALL' if s == 1 else 'PUT'} candle {tod}: trigger {st['pending']['level']}, stop {st['pending']['sl']}, valid to {exp_ts[11:]}")
                    break
            st["last5"] = bar["ts"].strftime("%Y-%m-%d %H:%M")
        st["armed"] = {"1": armed[1], "-1": armed[-1]}
        st["ind"] = {"at": c5[-1]["ts"].strftime("%H:%M"), "trend": int(trend[-1]), "fish": round(float(fish[-1]), 2), "adx": round(float(ax[-1]), 1),
                     "adx_rising": bool(ax[-1] > ax[-2]), "armed": st["armed"]}
    # 3) mark the open option once a minute
    p = st.get("pos")
    if p and time.time() - float(p.get("_mt") or 0) >= 55:
        try:
            q, _ = _quote(p)
            p["mark"] = float(q.get("bid") or q.get("ltp") or p["mark"]); p["mark_ltp"] = q.get("ltp"); p["_mt"] = time.time()
        except Exception:
            pass
    st["at"] = now.strftime("%Y-%m-%d %H:%M:%S")
    _save(st)


def tick(force=False):
    if S["busy"] or (not force and time.time() - S["t"] < EVERY):
        return False
    S["t"], S["busy"] = time.time(), True
    try:
        _work(); S["err"] = None
    except Exception as e:
        S["err"] = f"{type(e).__name__}: {str(e)[:200]}"
    finally:
        S["busy"] = False
    return True


def trades():
    """Open + closed paper trades for app/papertrades.py."""
    st = _load(); out = []
    for t in st.get("trades", []):
        out.append(t)
    if st.get("pos"):
        p = dict(st["pos"]); lot = p["lot"]; mk = float(p.get("mark") or p["entry"])
        p.update(status="open", pnl_open=round((mk - p["entry"]) * lot - _cost(p["entry"], mk, lot)))
        out.append(p)
    return out


def api(q=None):
    st = _load()
    return {"pos": st.get("pos"), "pending": st.get("pending"), "ind": st.get("ind"), "trades": st.get("trades", [])[-50:],
            "log": st.get("log", [])[-40:], "at": st.get("at"), "err": S.get("err"), "missed": st.get("missed", [])[-20:]}


def probe():
    """Health check (any time): today's / last session's 5-min candles from Dhan, the indicators, and the option the rule would buy now."""
    now = datetime.now().replace(microsecond=0); out = {"now": str(now)}
    d = now.date()
    c5 = [b for b in _bars(5, d - timedelta(days=12), d) if "09:15" <= b["ts"].strftime("%H:%M") < "15:30"]
    out["candles"] = len(c5); out["last"] = str(c5[-1]["ts"]) if c5 else None
    if len(c5) > 30:
        trend, fish, trig, ax, frng = _indicators(c5)
        out["ind"] = {"close": c5[-1]["c"], "trend": int(trend[-1]), "fish": round(float(fish[-1]), 2), "adx": round(float(ax[-1]), 1), "adx_rising": bool(ax[-1] > ax[-2]),
                      "armed": _armed_replay(c5, len(c5) - 1)}
        try:
            pk = _pick(int(trend[-1]), c5[-1]["c"], now)
            out["option"] = {k: v for k, v in (pk or {}).items() if k != "T"}
        except Exception as e:
            out["option_err"] = f"{type(e).__name__}: {e}"[:160]
    out["lot"] = _lot()
    return out
