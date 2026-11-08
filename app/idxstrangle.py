"""INDEX WEEKLY STRANGLE / CONDOR with the gamma filter - PAPER ONLY (2026-11-09, Nikhil: "yes set these up, looks like an edge").
Never places orders.
Backtest: scripts/research/index_strangle_gamma_wf.py (NIFTY 156 weeks Oct 2023 - Sep 2026, SENSEX 138 weeks Jan 2024 - Nov 2026;
gamma from the PREVIOUS session = no look-ahead; 3 sessions before expiry was the only entry timing that worked on both indices):
  NIFTY strangle  positive-gamma weeks +26 pts / unit (~ +Rs 1,700 a lot a week), win 78 %, negative-gamma weeks about 0
  SENSEX condor   positive-gamma weeks +94 pts / unit (~ +Rs 1,870 a lot a week), win 82 %, negative-gamma weeks -76
Rule:
  every session 15:15-15:29  read each index's Dhan option chain for every expiry within 31 days and store the dealer-model gamma
                             (sum over strikes within 5 % of spot with OI: Black-Scholes gamma from Dhan's IV x OI x spot^2,
                             calls +, puts -)
  entry                      on the session 3 trading days (weekdays) before the nearest weekly expiry, at ~15:25, only if the
                             PREVIOUS session's stored gamma is positive (if it was not stored - first week - today's reading is used
                             and the trade says so). ATM = strike nearest spot; width = ATM call + put price ("the straddle")
                               NIFTY  strangle: sell the call nearest ATM + width and the put nearest ATM - width, 1 lot (65)
                               SENSEX condor:   the same strangle + buy the call / put nearest ATM +/- 2 x width, 1 lot (20)
                             prices = last traded price (bid / ask kept for reference)
  exit                       held to expiry; settled at intrinsic value on the index close of expiry day (no stop)
  costs                      per leg per side Rs 25 + 0.10 % of premium (backtester rule); ITM legs pay the exit side on intrinsic
  marking                    open positions re-priced from the chain every ~10 minutes in market hours
Judge after ~20 weeks. Forward results never change the rule.
"""
from __future__ import annotations

import json
import math
import os
import threading
import time
from datetime import date, datetime, timedelta

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
OUT = os.path.join(ROOT, "data_store", "livedesk", "idxstrangle")
CFG = {"NIFTY": {"sid": 13, "lot": 65, "kind": "strangle", "rule": "istr_n"},
       "SENSEX": {"sid": 51, "lot": 20, "kind": "condor", "rule": "icon_s"}}
R = 0.065; FEE_FIX = 25.0; FEE_PCT = 0.001; BACK = 3
S: dict = {"t": 0, "busy": False, "err": None, "mark_t": 0}
N = lambda x: 0.5 * (1 + math.erf(x / math.sqrt(2)))


def _load():
    try:
        return json.load(open(os.path.join(OUT, "state.json")))
    except Exception:
        return {"gex": {}, "trades": [], "skips": []}


def _save(st):
    os.makedirs(OUT, exist_ok=True); p = os.path.join(OUT, "state.json"); tmp = p + ".tmp"
    json.dump(st, open(tmp, "w"), indent=1, default=str); os.replace(tmp, p)


def _gamma(S0, K, T, v):
    if S0 <= 0 or K <= 0 or T <= 0 or v <= 0:
        return 0.0
    d1 = (math.log(S0 / K) + (R + 0.5 * v * v) * T) / (v * math.sqrt(T))
    return math.exp(-0.5 * d1 * d1) / math.sqrt(2 * math.pi) / (S0 * v * math.sqrt(T))


def _chain(sid, expiry):
    import dhan_live as DL
    time.sleep(3.1)                                                    # Dhan: one option-chain call every 3 s
    d = DL._post("/optionchain", {"UnderlyingScrip": sid, "UnderlyingSeg": "IDX_I", "Expiry": expiry})
    rows = {}
    for k, r in (d.get("oc") or {}).items():
        for cp, key in (("CE", "ce"), ("PE", "pe")):
            o = r.get(key) or {}
            if o:
                rows[(float(k), cp)] = {"ltp": float(o.get("last_price") or 0), "oi": float(o.get("oi") or 0), "iv": float(o.get("implied_volatility") or 0),
                                        "bid": float(o.get("top_bid_price") or 0), "ask": float(o.get("top_ask_price") or 0), "vol": float(o.get("volume") or 0)}
    return float(d.get("last_price") or 0), rows


def _expiries(sid):
    import dhan_live as DL
    time.sleep(3.1)
    return sorted(DL._post("/optionchain/expirylist", {"UnderlyingScrip": sid, "UnderlyingSeg": "IDX_I"}) or [])


def _weekdays_after(d0: date, d1: date) -> int:
    n, d = 0, d0
    while d < d1:
        d += timedelta(days=1)
        if d.weekday() < 5:
            n += 1
    return n


def _close_day(st, idx, cfg, today, now):
    """15:15-15:29: store today's gamma; on the entry day, open the week's trade if yesterday's gamma was positive."""
    exps = [e for e in _expiries(cfg["sid"]) if e >= today]
    if not exps:
        return
    near = exps[0]; within = [e for e in exps if (date.fromisoformat(e) - date.fromisoformat(today)).days <= 31]
    tot, spot, chains = 0.0, None, {}
    for e in within:
        spot_e, rows = _chain(cfg["sid"], e); chains[e] = rows; spot = spot or spot_e
        T = max(0.5, (date.fromisoformat(e) - date.fromisoformat(today)).days + (15.5 - now.hour - now.minute / 60) / 24) / 365
        for (k, cp), r in rows.items():
            if r["oi"] > 0 and r["iv"] > 0 and abs(k / spot - 1) <= 0.05:
                tot += (1 if cp == "CE" else -1) * _gamma(spot, k, T, r["iv"] / 100) * r["oi"] * spot * spot
    st["gex"].setdefault(idx, {})[today] = {"gex": tot, "spot": spot, "at": now.strftime("%H:%M"), "expiries": within}
    if _weekdays_after(date.fromisoformat(today), date.fromisoformat(near)) != BACK:
        return
    if any(t["idx"] == idx and t["expiry"] == near for t in st["trades"]) or any(s["idx"] == idx and s["expiry"] == near for s in st["skips"]):
        return
    prev = sorted(d for d in st["gex"].get(idx, {}) if d < today)
    g_used, src = (st["gex"][idx][prev[-1]]["gex"], f"previous session {prev[-1]}") if prev else (tot, "today (no previous reading stored yet)")
    if g_used <= 0:
        st["skips"].append({"idx": idx, "expiry": near, "day": today, "gex": g_used, "why": f"dealer gamma negative ({src}) - no trade this week"}); return
    rows = chains.get(near) or _chain(cfg["sid"], near)[1]
    ks = sorted({k for (k, cp) in rows if (k, "CE") in rows and (k, "PE") in rows})
    if not ks:
        return
    k0 = min(ks, key=lambda z: abs(z - spot)); ce, pe = rows[(k0, "CE")]["ltp"], rows[(k0, "PE")]["ltp"]
    if ce <= 0 or pe <= 0:
        st["skips"].append({"idx": idx, "expiry": near, "day": today, "gex": g_used, "why": "ATM option without a price - skipped"}); return
    w = ce + pe; pick = lambda tg: min(ks, key=lambda z: abs(z - tg))
    legs = [{"strike": pick(k0 + w), "cp": "CE", "q": -1}, {"strike": pick(k0 - w), "cp": "PE", "q": -1}]
    if cfg["kind"] == "condor":
        legs += [{"strike": pick(k0 + 2 * w), "cp": "CE", "q": 1}, {"strike": pick(k0 - 2 * w), "cp": "PE", "q": 1}]
    for L in legs:
        r = rows.get((L["strike"], L["cp"])) or {}
        L.update(entry=r.get("ltp", 0), bid=r.get("bid"), ask=r.get("ask"), iv=r.get("iv"))
    if any(L["entry"] <= 0 for L in legs):
        st["skips"].append({"idx": idx, "expiry": near, "day": today, "gex": g_used, "why": "a leg had no price - skipped"}); return
    credit = sum(-L["q"] * L["entry"] for L in legs)
    st["trades"].append({"idx": idx, "rule": cfg["rule"], "kind": cfg["kind"], "expiry": near, "opened": f"{today} {now:%H:%M}", "spot_in": spot,
                         "atm": k0, "width": round(w, 2), "legs": legs, "lot": cfg["lot"], "credit": round(credit, 2), "status": "open",
                         "gex": g_used, "gex_src": src,
                         "why": f"dealer gamma positive ({src}) · {BACK} sessions before the {near} expiry · width = ATM straddle {w:.1f}"})


def _value(tr, rows):
    v = 0.0
    for L in tr["legs"]:
        r = rows.get((L["strike"], L["cp"]))
        if not r or r["ltp"] <= 0:
            return None
        v += L["q"] * r["ltp"]
    return v                                                            # value of the position per unit (negative for a credit book)


def _settle(tr, close):
    lot = tr["lot"]; pts = 0.0; fees = 0.0
    for L in tr["legs"]:
        it = max(0.0, close - L["strike"]) if L["cp"] == "CE" else max(0.0, L["strike"] - close)
        L["settle"] = it; pts += L["q"] * (it - L["entry"])
        fees += FEE_FIX + FEE_PCT * L["entry"] * lot + ((FEE_FIX + FEE_PCT * it * lot) if it > 0 else 0)
    tr.update(status="closed", spot_out=close, closed=f"{tr['expiry']} 15:30", pnl_pts=round(pts, 2), pnl=round(pts * lot - fees), fees=round(fees),
              exit_reason="expiry settlement")
    for k in ("now_pts", "pnl_open"):
        tr.pop(k, None)


def _work():
    try:
        import dhan_live as DL
        st = _load(); now = datetime.now(); today = now.strftime("%Y-%m-%d"); hm = now.strftime("%H:%M")
        wk = now.weekday() < 5
        if wk and "15:15" <= hm <= "15:29":
            for idx, cfg in CFG.items():
                if (st["gex"].get(idx) or {}).get(today):
                    continue
                try:
                    _close_day(st, idx, cfg, today, now)
                except Exception as e:
                    S["err"] = f"{idx} close: {type(e).__name__}: {str(e)[:120]}"
        for tr in [t for t in st["trades"] if t["status"] == "open"]:
            cfg = CFG[tr["idx"]]
            if today > tr["expiry"] or (today == tr["expiry"] and hm >= "15:31"):
                close = None
                if today == tr["expiry"]:
                    q = (DL.quote({"IDX_I": [cfg["sid"]]}).get("IDX_I") or {}).get(str(cfg["sid"])) or {}
                    close = q.get("last_price")
                if not close:
                    bars = DL.daily("IDX_I", cfg["sid"], "INDEX", date.fromisoformat(tr["expiry"]), date.fromisoformat(tr["expiry"]))
                    close = bars[-1]["close"] if bars else None
                if close:
                    _settle(tr, float(close))
            elif wk and "09:20" <= hm <= "15:30" and time.time() - S["mark_t"] > 590:
                _, rows = _chain(cfg["sid"], tr["expiry"]); v = _value(tr, rows)
                if v is not None:
                    pts = -tr["credit"] - v                                   # credit received + current value of the book
                    tr.update(now_pts=round(pts, 2), pnl_open=round(pts * tr["lot"]), marked=hm)
        if any(t["status"] == "open" for t in st["trades"]) and wk and "09:20" <= hm <= "15:30" and time.time() - S["mark_t"] > 590:
            S["mark_t"] = time.time()
        st["at"] = now.isoformat(timespec="seconds"); _save(st)
        if (S.get("err") or "").startswith("work"):
            S["err"] = None
    except Exception as e:
        S["err"] = f"work: {type(e).__name__}: {str(e)[:160]}"
    finally:
        S["busy"] = False


def tick(force=False):
    if S["busy"] or (not force and time.time() - S["t"] < 55):
        return False
    S["t"] = time.time(); S["busy"] = True
    threading.Thread(target=_work, name="idxstrangle", daemon=True).start()
    return True


def trades():
    return [t for t in _load().get("trades", []) if t.get("status") in ("open", "closed")]


def api(q=None):
    q = q or {}
    if q.get("dry"):                                                    # check run: today's gamma + what an entry would look like, NOT saved
        import copy
        st = copy.deepcopy(_load()); now = datetime.now(); today = now.strftime("%Y-%m-%d"); out = {}
        for idx, cfg in CFG.items():
            st["gex"].setdefault(idx, {}).pop(today, None); n0 = len(st["trades"]); s0 = len(st["skips"])
            try:
                exps = [e for e in _expiries(cfg["sid"]) if e >= today]
                _close_day(st, idx, cfg, today, now)
                out[idx] = {"near": exps[:3], "sessions_to_near": _weekdays_after(date.fromisoformat(today), date.fromisoformat(exps[0])) if exps else None,
                            "gex_today": st["gex"][idx].get(today), "would_trade": st["trades"][n0:], "would_skip": st["skips"][s0:]}
            except Exception as e:
                out[idx] = {"err": f"{type(e).__name__}: {str(e)[:160]}"}
        return out
    st = _load()
    last = {i: (sorted(v.items())[-1] if v else None) for i, v in (st.get("gex") or {}).items()}
    return {"rules": {i: f"{c['kind']} · 1 lot ({c['lot']})" for i, c in CFG.items()}, "gex_last": last, "open": [t for t in st.get("trades", []) if t["status"] == "open"],
            "closed": [t for t in st.get("trades", []) if t["status"] == "closed"][-20:], "skips": st.get("skips", [])[-10:], "err": S.get("err"), "at": st.get("at")}
