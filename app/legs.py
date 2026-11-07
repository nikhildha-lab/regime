"""INSTRUMENT LEGS - which instrument each paper trade uses, with the exact strikes and premiums (2026-11-06, Nikhil: "under plan add
info about what instrument was deployed like option buying, option selling, future, synthetic future with or without hedge, exact strikes
with premiums .. not sure premiums are correct or not").

Read-only. It never changes a trade's own result; it shows the legs beside it and checks the premiums. Paper only, never orders.

How a premium is priced (checked on 6 Nov: raw snapshot LTPs are not in sync - strike + CE - PE jumped 30-50 pts between
snapshots while the futures barely moved, so a premium read straight off a snapshot can be 20-40 pts wrong):
    1. "true forward" at time t = futures mid at t (order-flow 1-sec file, real time) - the day's typical gap between the futures
       and the options' own forward (median over the snapshots within 45 min of t)
    2. leg price at t = that strike's LTP in the nearest snapshot (every ~3 min) + its delta x (true forward at t - forward that
       snapshot's prices imply, from the 3 strikes nearest the money)
    A strike with no trade yet today (LTP 0) is priced by Black-Scholes at its IV and marked "model". No futures file -> spot used.

Checks shown to Nikhil:
    synthetic : costs on the real premiums (both legs, touchtrades._costs) vs the flat 4.5 / 12.5 pts the rule assumes, and the
                premium move vs the points move (should match within a few pts; a big gap = a data problem, flagged).
    option buy: the entry premium shown on the card vs the market-anchored price (old cards used a Black-Scholes price at spot).
Results are cached in data_store/livedesk/legs/cache.json (closed trades for good, open trades refreshed every 60 s).
"""
from __future__ import annotations

import json
import math
import os
import time
from datetime import datetime, timedelta, timezone

import pandas as pd

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
OUT = os.path.join(ROOT, "data_store", "livedesk", "legs")
IST = timezone(timedelta(hours=5, minutes=30))
LOT = {"NIFTY": 65, "BANKNIFTY": 30}
NAME = {"NIFTY": "Nifty", "BANKNIFTY": "Bank Nifty"}
SNAP_WIN = 8 * 60                     # use a snapshot only if it is within 8 min of the moment priced
CARRY_RATE = 0.065                    # rough cost of carry for the parity check (a range, not a precise rate)
VERSION = "v4"                        # bump when the pricing changes: older cache rows are recomputed
_OC: dict = {}
_C: dict = {"rows": None, "dirty": False, "saved": 0}


def _TT():
    import touchtrades as TT
    return TT


def _DOM():
    import domcards as D
    return D


# ---------------------------------------------------------------- data
def _chain(day):
    """Option-chain snapshots for one day (kept for 3 days, so a page with several days of trades does not reload files)."""
    p = os.path.join(_TT().OCD, f"{day}.csv.gz")
    try:
        mt = os.path.getmtime(p)
    except OSError:
        return None
    c = _OC.get(day)
    if c and c[0] == mt:
        return c[1]
    o = pd.read_csv(p, usecols=["ts", "symbol", "expiry", "strike", "side", "ltp", "volume", "iv", "underlying"])
    o = o[o.expiry.notna()].copy(); o["expiry"] = o.expiry.astype(str); o["ts"] = pd.to_datetime(o.ts)
    if len(_OC) >= 3:
        _OC.pop(sorted(_OC, key=lambda k: _OC[k][2])[0])
    _OC[day] = (mt, o, time.time())
    return o


def _ist(epoch):
    return datetime.fromtimestamp(epoch, IST).replace(tzinfo=None)


def _epoch(day, hhmm):
    """'2026-11-06' + '09:48' (IST) -> epoch seconds."""
    try:
        return datetime.fromisoformat(f"{day} {str(hhmm)[:5]}").replace(tzinfo=IST).timestamp()
    except ValueError:
        return None


def _snap(day, key, t):
    """Rows of the snapshot nearest to epoch t (within SNAP_WIN), or None."""
    o = _chain(day)
    if o is None:
        return None
    x = o[o.symbol == key]
    if not len(x):
        return None
    ts = pd.Series(x.ts.unique())
    tt = pd.Timestamp(_ist(t))
    i = (ts - tt).abs().idxmin()
    s0 = ts[i]
    if abs((s0 - tt).total_seconds()) > SNAP_WIN:
        return None
    return x[x.ts == s0]


def _fut_at(day, key, t):
    """Futures mid at epoch t from the order-flow 1-sec file (nearest second within 2 min)."""
    try:
        ts, px = _DOM()._path(day, key)
    except Exception:
        return None
    if not ts:
        return None
    import bisect
    i = bisect.bisect_left(ts, t)
    best = None
    for j in (i - 1, i):
        if 0 <= j < len(ts) and abs(ts[j] - t) <= 120 and (best is None or abs(ts[j] - t) < abs(ts[best] - t)):
            best = j
    return px[best] if best is not None else None


def _days_left(exp, s0):
    return (pd.Timestamp(exp) + pd.Timedelta(hours=15.5) - pd.Timestamp(s0)).total_seconds() / 86400


# ---------------------------------------------------------------- pricing
def _snap_fwd(a, exp):
    """Forward the snapshot's own prices imply: median of strike + CE - PE over the 3 traded strikes nearest the money."""
    b = a[(a.expiry == str(exp)) & (a.ltp > 0)]
    ce = b[b.side == "CE"].set_index("strike").ltp; pe = b[b.side == "PE"].set_index("strike").ltp
    ks = [k for k in ce.index if k in pe.index]
    if not ks:
        return None
    u = float(a.underlying.iloc[0])
    ks = sorted(ks, key=lambda k: abs(k - u))[:3]
    return float(pd.Series([k + ce[k] - pe[k] for k in ks]).median())


_BAS: dict = {}


def _basis(day, key, exp, t, kind="fut"):
    """Typical gap within 45 min of t, median over those snapshots. kind 'fut': futures - options forward;
    kind 'spot': options forward - snapshot spot. Cached per 5-min bucket."""
    ck = (day, key, str(exp), int(t // 300), kind)
    if ck in _BAS:
        return _BAS[ck]
    o = _chain(day)
    if o is None:
        return None
    x = o[(o.symbol == key) & (o.expiry == str(exp))]
    tt = pd.Timestamp(_ist(t)); z = []
    for ts, a in x[(x.ts >= tt - pd.Timedelta(minutes=45)) & (x.ts <= tt + pd.Timedelta(minutes=45))].groupby("ts"):
        w = _snap_fwd(a, exp)
        if not w:
            continue
        if kind == "fut":
            f = _fut_at(day, key, pd.Timestamp(ts).tz_localize(IST).timestamp())
            if f:
                z.append(f - w)
        else:
            z.append(w - float(a.underlying.iloc[0]))
    v = float(pd.Series(z).median()) if len(z) >= 3 else None
    if len(_BAS) > 5000:
        _BAS.clear()
    _BAS[ck] = v
    return v


def _true_fwd(day, key, exp, t, a, spot=None, fut=None, t_anchor=None):
    """Options forward at t. One basis per trade (read at t_anchor = entry), so basis noise cannot create a fake result.
    fut given (futures-scale trade) -> fut - basis; spot given -> spot + basis; neither -> futures at t from the 1-sec file."""
    ta = t_anchor or t
    if fut is not None:
        b = _basis(day, key, exp, ta, "fut")
        if b is not None:
            return fut - b, "futures"
    if spot is not None:
        b = _basis(day, key, exp, ta, "spot")
        if b is not None:
            return spot + b, "spot"
    f = _fut_at(day, key, t); b = _basis(day, key, exp, ta, "fut") if f else None
    if f and b is not None:
        return f - b, "futures"
    return _snap_fwd(a, exp), "snapshot"


def price_leg(day, key, exp, strike, cp, t, spot=None, fut=None, t_anchor=None):
    """One option leg at epoch t -> {px, raw, snap, src, delta, fwd} or None. raw = the snapshot LTP as it stood."""
    a = _snap(day, key, t)
    if a is None:
        return None
    TT = _TT()
    s0 = a.ts.iloc[0]; days = _days_left(exp, s0)
    r = a[(a.expiry == str(exp)) & (a.strike == float(strike)) & (a.side == cp)]
    if not len(r):
        return None
    ltp, iv = float(r.ltp.iloc[0]), float(r.iv.iloc[0])
    F, how = _true_fwd(day, key, exp, t, a, spot, fut, t_anchor)
    w = _snap_fwd(a, exp)
    sh = (F - w) if (F is not None and w is not None) else 0.0
    S = F if F is not None else float(a.underlying.iloc[0])
    if ltp > 0:
        dl = (TT._delta(w or S, float(strike), iv, days, cp) if iv > 0 else None) or (0.5 if cp == "CE" else -0.5)
        px, src = ltp + dl * sh, "LTP"
    elif iv > 0:
        dl = TT._delta(S, float(strike), iv, days, cp) or 0
        px, src = TT._bs(S, float(strike), iv, days, cp), "model"
    else:
        return None
    return {"px": round(max(0.05, px), 2), "raw": ltp, "snap": str(s0)[11:16], "src": src, "delta": round(dl, 2),
            "spot": round(S, 2), "fwd": round(F, 1) if F is not None else None, "anchor": how}


def _pick_atm(day, key, t, spot=None, fut=None):
    """Next-week expiry (7+ days left, same as the live rules) and the ATM strike among traded strikes."""
    a = _snap(day, key, t)
    if a is None:
        return None
    TT = _TT()
    s0 = a.ts.iloc[0]; u = float(a.underlying.iloc[0])
    for exp in sorted(a.expiry.unique()):
        if _days_left(exp, s0) < TT.MIN_DAYS_LEFT:
            continue
        F, _ = _true_fwd(day, key, exp, t, a, spot, fut); w = _snap_fwd(a, exp)
        S = u + (F - w) if (F is not None and w is not None) else (spot or u)      # spot at t, moved with the futures
        b = a[(a.expiry == exp) & (a.ltp > 0) & (a.volume > 0)]
        ks = sorted(set(b[b.side == "CE"].strike) & set(b[b.side == "PE"].strike))
        if len(ks) < 3:
            continue
        return exp, min(ks, key=lambda k: abs(k - S))
    return None


def _leg_money(key, legs, lot):
    TT = _TT(); g = c = 0.0
    for lg in legs:
        if lg.get("exit") is None:
            continue
        e, x = lg["entry"] * lot, lg["exit"] * lot
        if lg["act"] == "buy":
            g += x - e; c += TT._costs(e, x, key, lot)
        else:
            g += e - x; c += TT._costs(x, e, key, lot)
    return round(g), round(c), round(g - c)


def synthetic(key, day, side, t0, t1=None, spot0=None, spot1=None, fut0=None, fut1=None, pts_result=None):
    """Synthetic future: long = buy ATM CE + sell ATM PE, short = buy ATM PE + sell ATM CE. Strike fixed at entry."""
    sd = 1 if side == "long" else -1
    pk = _pick_atm(day, key, t0, spot0, fut0)
    if not pk:
        return {"kind": "synthetic", "ok": False, "why": "no option-chain snapshot near the entry"}
    exp, K = pk
    lot = LOT[key]
    acts = {"CE": "buy" if sd > 0 else "sell", "PE": "sell" if sd > 0 else "buy"}
    legs, chk = [], []
    for cp in ("CE", "PE"):
        e = price_leg(day, key, exp, K, cp, t0, spot0, fut0, t0)
        x = price_leg(day, key, exp, K, cp, t1, spot1, fut1, t0) if t1 else None
        if not e:
            return {"kind": "synthetic", "ok": False, "why": f"no {cp} price at entry"}
        legs.append({"act": acts[cp], "strike": K, "cp": cp, "entry": e["px"], "exit": x["px"] if x else None,
                     "src": e["src"] if not x or x["src"] == e["src"] else "LTP/model", "snap_in": e["snap"], "snap_out": x["snap"] if x else None})
        chk.append((e, x))
    out = {"kind": "synthetic", "ok": True, "expiry": exp, "strike": K, "lot": lot, "legs": legs,
           "anchor": chk[0][0].get("anchor"), "raw_in": {cp: c[0]["raw"] for cp, c in zip(("CE", "PE"), chk)}}
    (ce_e, ce_x), (pe_e, pe_x) = chk
    flags = []
    if t1 and ce_x and pe_x:
        g, c, n = _leg_money(key, legs, lot)
        out.update(gross=g, costs=c, net=n, costs_assumed=round(_TT().SYN_PTS[key] * lot))
        prem_pts = sd * ((ce_x["px"] - ce_e["px"]) - (pe_x["px"] - pe_e["px"]))
        out["prem_pts"] = round(prem_pts, 1)
        if pts_result is not None:
            out["pts_result"] = round(pts_result, 1)
            d = prem_pts - pts_result
            out["diff_pts"] = round(d, 1)
            if abs(d) > max(3.0 if key == "NIFTY" else 8.0, 0.15 * abs(pts_result)):
                flags.append(f"premium move {prem_pts:+.1f} pts vs points {pts_result:+.1f} (gap {d:+.1f})")
    elif t1:
        out["note"] = "no option-chain snapshot near the exit"
    out["flags"] = flags
    return out


def single(key, day, act, exp, K, cp, t0, t1=None, spot0=None, spot1=None, fut0=None, fut1=None, card_result=None, card_entry=None):
    """One option (option buying = act 'buy')."""
    e = price_leg(day, key, exp, K, cp, t0, spot0, fut0, t0)
    if not e:
        return {"kind": "option " + act, "ok": False, "why": "no option-chain snapshot near the entry"}
    x = price_leg(day, key, exp, K, cp, t1, spot1, fut1, t0) if t1 else None
    lot = LOT[key]
    lg = {"act": act, "strike": float(K), "cp": cp, "entry": e["px"], "exit": x["px"] if x else None, "src": e["src"],
          "snap_in": e["snap"], "snap_out": x["snap"] if x else None, "delta": e["delta"]}
    out = {"kind": "option " + act, "ok": True, "expiry": exp, "strike": float(K), "lot": lot, "legs": [lg], "flags": []}
    if card_entry:
        d = card_entry - e["px"]
        out["card_entry"] = card_entry
        if abs(d) > max(2.0, 0.03 * e["px"]):
            out["flags"].append(f"shown entry {card_entry:.1f} vs market {e['px']:.1f} ({d:+.1f}) - old model price")
    if x:
        g, c, n = _leg_money(key, [lg], lot)
        out.update(gross=g, costs=c, net=n)
        if card_result is not None:
            out["card_result"] = card_result
    return out


# ---------------------------------------------------------------- cache
def _cp():
    return os.path.join(OUT, "cache.json")


def _cache():
    if _C["rows"] is None:
        try:
            _C["rows"] = json.load(open(_cp()))
        except Exception:
            _C["rows"] = {}
    return _C["rows"]


def _flush():
    if not _C["dirty"] or time.time() - _C["saved"] < 5:
        return
    os.makedirs(OUT, exist_ok=True)
    tmp = _cp() + f".{os.getpid()}.tmp"
    json.dump(_C["rows"], open(tmp, "w"), default=str)
    os.replace(tmp, _cp())
    _C["dirty"] = False; _C["saved"] = time.time()


def cached(cid, closed, fn):
    """Closed trades: computed once and kept. Open trades: refreshed at most every 60 s."""
    R = _cache()
    h = R.get(cid)
    if h and h.get("_v") == VERSION and (h.get("_closed") or (not closed and time.time() - h.get("_t", 0) < 60)):
        return h
    try:
        v = fn()
    except Exception as e:                                   # never break a page over a pricing gap
        v = {"ok": False, "why": f"pricing error: {type(e).__name__}"}
    v["_closed"] = bool(closed and v.get("ok")); v["_t"] = time.time(); v["_v"] = VERSION
    R[cid] = v; _C["dirty"] = True
    _flush()
    return v


# ---------------------------------------------------------------- per rule
def for_dom(x):
    """DOM trade (domcards row). Index: synthetic on the futures scale. Stocks: NSE cash, qty from the Rs 1,000 risk."""
    D = _DOM()
    if x["key"] not in D.INDEX:
        q = int(D.RISK_RS / x["risk"]) if x.get("risk") else None
        return {"kind": "stock", "ok": True, "text": f"NSE stock (cash, intraday) · {'buy' if x['side'] == 'long' else 'sell'} qty {q} "
                f"({x['key']}, Rs {D.RISK_RS:,} risk / {x.get('risk', 0):.2f} a share)" if q else "NSE stock (cash, intraday)"}
    closed = x.get("status") == "closed"
    t0 = x.get("t0") or _epoch(x["day"], str(x["opened"])[11:16])
    t1 = _epoch(x["day"], str(x.get("closed", ""))[11:16]) if closed else None
    pts = x.get("pts_gross")
    return cached("dom:" + x["id"], closed, lambda: synthetic(x["key"], x["day"], x["side"], t0, t1, fut0=x["entry"],
                                                               fut1=x.get("exit"), pts_result=pts))


def for_touch(c, synth=False):
    """Touch-minute card. Old rule: option buying (the card's own strike). v2 / combo: synthetic on the spot scale."""
    closed = c.get("status") == "closed"
    t0 = c.get("t_entry") or _epoch(c["day"], c["time"])
    t1 = _epoch(c["day"], c.get("closed", "")) if closed else None
    if synth:
        return cached(f"touchsyn:{c['day']}:{c['id']}", closed, lambda: synthetic(c["key"], c["day"], c["side"], t0, t1,
                      spot0=c.get("spot_entry"), spot1=c.get("spot_exit"),
                      pts_result=c.get("spot_pts") if c.get("spot_pts") is not None else None))
    o = c.get("option") or {}
    if not o:
        return {"kind": "option buy", "ok": False, "why": "no option chosen (no chain at entry)"}
    return cached(f"touch:{c['day']}:{c['id']}", closed, lambda: single(c["key"], c["day"], "buy", o["expiry"], o["strike"], o["cp"], t0, t1,
                  spot0=c.get("spot_entry"), spot1=c.get("spot_exit"), card_result=c.get("rs_net_mkt", c.get("rs_net")),
                  card_entry=c.get("prem_entry")))


def for_ovn(r, v):
    """Overnight: the legs are already priced by overnight.py - just lay them out."""
    V = (r.get("versions") or {}).get(v) or {}
    legs = [{"act": "buy" if lg["qty"] > 0 else "sell", "strike": lg["strike"], "cp": lg["cp"], "entry": lg.get("entry"),
             "exit": (V.get("exit_px") or {}).get(n), "src": "LTP", "snap_in": lg.get("snap") or r.get("snap"), "snap_out": V.get("exit_snap")}
            for n, lg in (V.get("legs") or {}).items()]
    kind = "synthetic" if v == "plain" else "synthetic + 0.3-delta hedge (order-flow filtered)" if v == "h30_flow" else f"synthetic + 0.{v[1:]}-delta hedge"
    return {"kind": kind, "ok": bool(legs), "expiry": r.get("expiry"), "lot": LOT.get(r["key"]), "legs": legs,
            "gross": V.get("gross"), "costs": V.get("costs"), "net": V.get("pnl"), "flags": []}


def for_vcy(r, v):
    """2026-11-06 vol cycle: legs already priced by volcycle.py - just lay them out."""
    V = (r.get("versions") or {}).get(v) or {}
    legs = [{"act": "buy" if lg["qty"] > 0 else "sell", "strike": lg["strike"], "cp": lg["cp"], "entry": lg.get("entry"),
             "exit": (V.get("exit_px") or {}).get(n), "src": "LTP", "snap_in": r.get("snap"), "snap_out": r.get("closed")}
            for n, lg in (V.get("legs") or {}).items()]
    kind = {"plain": "long straddle" if r.get("rule") == "night" else "short straddle", "wings": "short iron fly (2 % wings)",
            "tilt_spread": "debit spread (close direction)", "tilt_straddle": "long straddle + extra leg (close direction)",
            "skew": "credit spread (away from the morning trend)", "bwing": "broken-wing fly (1 % / 3 % wings)",
            "dhedge": "short straddle, delta-hedged with synthetic futures"}.get(v, v)
    if v in ("skew", "bwing") and (r.get("context") or {}).get("trend") == "none":
        kind = "short straddle (no morning trend, same as plain)" if v == "skew" else "short iron fly (no morning trend, 2 % wings)"
    return {"kind": kind, "ok": bool(legs), "expiry": r.get("expiry"), "lot": LOT.get(r["key"]), "legs": legs,
            "gross": V.get("gross"), "costs": V.get("costs"), "net": V.get("pnl"), "flags": []}


# ---------------------------------------------------------------- text
def text(L, key=None):
    """One plain line, e.g. 'Synthetic future · 17 Nov · buy 24500 CE 212.4 -> 230.1 · sell 24500 PE 198.0 -> 181.2'."""
    if not L:
        return ""
    if L.get("text"):
        return L["text"]
    if not L.get("ok"):
        return f"{L.get('kind', '')}: {L.get('why', 'not priced')}"
    try:
        e = datetime.fromisoformat(str(L["expiry"])[:10]).strftime("%d %b")
    except Exception:
        e = str(L.get("expiry", ""))
    nm = {"synthetic": "Synthetic future", "option buy": "Option buying", "option sell": "Option selling"}.get(L["kind"], L["kind"].capitalize())
    f = lambda v: "–" if v is None else f"{v:,.1f}"
    legs = " · ".join(f"{lg['act']} {lg['strike']:.0f} {lg['cp']} {f(lg['entry'])}" + (f" → {f(lg['exit'])}" if lg.get("exit") is not None else "")
                      + (" (model)" if lg.get("src") == "model" else "") for lg in L["legs"])
    return f"{nm} · {e} expiry · {L.get('lot', '')} qty · {legs}"
