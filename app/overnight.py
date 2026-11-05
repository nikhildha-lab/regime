"""OVERNIGHT HOLD - PAPER ONLY (2026-11-06, Nikhil: "Yes deploy these around 15:13 pm .. since we have CAS at 15:15 nowadays").
Never places orders. Research: claude/overnight-patterns.md (2 years of 5-min spot: a strong close tends to gap the same way).

Rule (fixed before any live result):
  signal 15:13 IST, Nifty and Bank Nifty separately, from today's 1-min index bars up to 15:12 and yesterday's close:
      long  if (spot - day low) / (day high - day low) >= 0.80 AND spot > yesterday's close
      short if that ratio <= 0.20                         AND spot < yesterday's close
  vehicle: next-week options (>= 7 days to expiry), 1 lot (Nifty 65 / Bank Nifty 30), priced from the latest option-chain snapshot
      (each leg = its snapshot LTP, moved by its delta for the spot change since that snapshot - real prices carry the futures premium,
      a zero-carry Black-Scholes price at spot does not; first replay showed errors of several hundred points). Three versions side by side:
        plain : synthetic future           long = buy ATM CE + sell ATM PE        short = buy ATM PE + sell ATM CE
        h20   : synthetic + hedge          long adds buy ~0.20-delta PE           short adds buy ~0.20-delta CE
        h30   : synthetic + hedge          long adds buy ~0.30-delta PE           short adds buy ~0.30-delta CE
  exit: next trading day, first option-chain snapshot after 09:15 (~09:16), every leg at its snapshot LTP
  costs: per leg, touchtrades._costs (brokerage, STT 0.15 % on option sells, exchange, GST, stamp, spread allowance)
Judge after ~40 signal nights: net positive in both halves, per version. Nothing changes without Nikhil's approval.
"""
from __future__ import annotations

import json
import os
import time
from datetime import date, datetime, timedelta

import pandas as pd

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
OUT = os.path.join(ROOT, "data_store", "livedesk", "overnight")
DEEP = os.path.join(ROOT, "data_store", "market_data", "yahoo_daily_deep.csv")
KEYS = ("NIFTY", "BANKNIFTY")
LOT = {"NIFTY": 65, "BANKNIFTY": 30}
DHAN_IDX = {"NIFTY": "nifty", "BANKNIFTY": "banknifty"}
YSYM = {"NIFTY": "^NSEI", "BANKNIFTY": "^NSEBANK"}
HI, LO_, AT, MIN_DAYS = 0.80, 0.20, "15:13", 7
VERS = {"plain": None, "h20": 0.20, "h30": 0.30}
S: dict = {"t": 0}


def _p():
    return os.path.join(OUT, "trades.json")


def _load():
    try:
        return json.load(open(_p()))
    except Exception:
        return []


def _save(rows):
    os.makedirs(OUT, exist_ok=True)
    tmp = _p() + f".{os.getpid()}.tmp"
    json.dump(rows, open(tmp, "w"), indent=1, default=str)
    os.replace(tmp, _p())


def _TT():
    import touchtrades as TT
    return TT


def _day_bars(key, day):
    """Today's 1-min index bars (Dhan) -> (spot, high, low, source). Fallback: the option-chain snapshots' underlying."""
    try:
        import dhan_live as DL
        sid = DL.ids_index(DHAN_IDX[key])
        d = date.fromisoformat(day)
        b = DL.intraday("IDX_I", sid, "INDEX", d, d, 1, oi=False)
        b = [x for x in b if str(x["ts"])[11:16] < AT]
        if len(b) > 30:
            return float(b[-1]["close"]), max(x["high"] for x in b), min(x["low"] for x in b), "dhan 1-min"
    except Exception:
        pass
    o = _TT()._chain(day)
    if o is None:
        return None
    u = o[(o.symbol == key) & (o.ts.dt.strftime("%H:%M") < AT)].groupby("ts").underlying.first()
    if len(u) < 10:
        return None
    return float(u.iloc[-1]), float(u.max()), float(u.min()), "option-chain snapshots"


def _prev_close(key, day):
    try:
        y = pd.read_csv(DEEP, usecols=["date", "symbol", "close"])
        y = y[(y.symbol == YSYM[key]) & (y.date.astype(str).str[:10] < day)].sort_values("date")
        if len(y):
            return float(y.close.iloc[-1]), str(y.date.iloc[-1])[:10]
    except Exception:
        pass
    TT = _TT()
    prev = sorted(f[:10] for f in os.listdir(TT.OCD) if f.endswith(".csv.gz") and f[:10] < day)
    if prev:
        o = TT._chain(prev[-1])
        if o is not None:
            u = o[o.symbol == key].sort_values("ts").underlying
            if len(u):
                return float(u.iloc[-1]), prev[-1]
    return None, None


def _snap(day, key, at):
    """Latest snapshot rows for key at or before `at` (HH:MM) on day."""
    o = _TT()._chain(day)
    if o is None:
        return None
    x = o[(o.symbol == key) & (o.ts.dt.strftime("%H:%M") <= at)]
    if not len(x):
        return None
    return x[x.ts == x.ts.max()]


def _legs(key, a, spot, sd):
    """Pick expiry + strikes from snapshot rows `a`; price each leg at `spot` with its IV (Black-Scholes)."""
    TT = _TT(); s0 = a.ts.iloc[0]
    for exp in sorted(a.expiry.dropna().unique()):
        days = (pd.Timestamp(exp) + pd.Timedelta(hours=15.5) - s0).total_seconds() / 86400
        if days < MIN_DAYS:
            continue
        b = a[(a.expiry == exp) & (a.ltp > 0) & (a.iv > 0) & (a.volume > 0)].copy()      # traded strikes only (no stale LTPs)
        S_snap = float(a.underlying.iloc[0])
        if len(b) < 6:
            continue
        atm = float(b.strike.iloc[(b.strike - spot).abs().argsort().iloc[0]])
        def leg(k, cp, qty):
            r = b[(b.strike == k) & (b.side == cp)]
            if not len(r):
                return None
            iv = float(r.iv.iloc[0]); dl = TT._delta(S_snap, k, iv, days, cp) or 0
            return {"strike": k, "cp": cp, "qty": qty, "iv": iv, "ltp_snap": float(r.ltp.iloc[0]), "snap_spot": S_snap,
                    "entry": round(max(0.05, float(r.ltp.iloc[0]) + dl * (spot - S_snap)), 2), "delta": round(dl, 2)}
        core = [leg(atm, "CE", sd), leg(atm, "PE", -sd)]            # long: +CE -PE ; short: -CE +PE
        if None in core:
            continue
        hcp = "PE" if sd > 0 else "CE"
        cand = b[(b.side == hcp)].copy()
        cand["dl"] = [abs(TT._delta(spot, k, iv, days, hcp) or 0) for k, iv in zip(cand.strike, cand.iv)]
        cand = cand[(cand.strike < atm) if sd > 0 else (cand.strike > atm)]
        hedges = {}
        for v, target in VERS.items():
            if target is None or not len(cand):
                continue
            r = cand.iloc[(cand.dl - target).abs().argsort().iloc[0]]
            hedges[v] = leg(float(r.strike), hcp, 1)
        return {"expiry": exp, "days_left": round(days, 2), "atm": atm, "core": core, "hedges": hedges, "snap": str(s0)[11:16]}
    return None


def _price_exit(day, key, legs, at="10:00"):
    """First snapshot after 09:15 on `day`: each leg at its LTP (or Black-Scholes at that snapshot if the LTP is missing)."""
    o = _TT()._chain(day)
    if o is None:
        return None
    x = o[(o.symbol == key) & (o.ts.dt.strftime("%H:%M") > "09:15") & (o.ts.dt.strftime("%H:%M") <= at)]
    if not len(x):
        return None
    a = x[x.ts == x.ts.min()]; s0 = a.ts.iloc[0]; S_ = float(a.underlying.iloc[0]); out = {}
    for name, lg in legs.items():
        r = a[(a.expiry == lg["expiry"]) & (a.strike == lg["strike"]) & (a.side == lg["cp"])]
        days = (pd.Timestamp(lg["expiry"]) + pd.Timedelta(hours=15.5) - s0).total_seconds() / 86400
        if len(r) and float(r.ltp.iloc[0]) > 0:
            out[name] = float(r.ltp.iloc[0])
        elif len(r) and float(r.iv.iloc[0]) > 0:
            out[name] = round(_TT()._bs(S_, lg["strike"], float(r.iv.iloc[0]), days, lg["cp"]), 2)
        else:
            out[name] = round(_TT()._bs(S_, lg["strike"], lg["iv"], days, lg["cp"]), 2)
    return {"snap": str(s0)[11:16], "spot": S_, "px": out}


def _pnl(key, legs, exit_px):
    """Rupees for 1 lot after per-leg costs. qty +1 = bought at entry, -1 = sold at entry."""
    TT = _TT(); lot = LOT[key]; gross = cost = 0.0
    for name, lg in legs.items():
        e, x = lg["entry"] * lot, exit_px[name] * lot
        if lg["qty"] > 0:
            gross += x - e; cost += TT._costs(e, x, key, lot)
        else:
            gross += e - x; cost += TT._costs(x, e, key, lot)
    return round(gross), round(cost), round(gross - cost)


def evaluate(key, day, spot, hi, lo, prev_c, a):
    """Signal + legs for one index on one day. `a` = snapshot rows to price from. Returns a trade row or a no-trade row."""
    rng = hi - lo
    loc = (spot - lo) / rng if rng > 0 else 0.5
    sd = 1 if (loc >= HI and spot > prev_c) else -1 if (loc <= LO_ and spot < prev_c) else 0
    base = {"id": f"{key}-{day}", "key": key, "day": day, "spot": round(spot, 2), "day_high": round(hi, 2), "day_low": round(lo, 2),
            "prev_close": round(prev_c, 2), "close_loc": round(loc, 2), "day_ret_pct": round((spot / prev_c - 1) * 100, 2)}
    if not sd:
        return {**base, "status": "no signal"}
    L = _legs(key, a, spot, sd)
    if not L:
        return {**base, "status": "no options", "side": "long" if sd > 0 else "short"}
    vers = {}
    for v in VERS:
        legs = {"core_ce": {**L["core"][0], "expiry": L["expiry"]}, "core_pe": {**L["core"][1], "expiry": L["expiry"]}}
        if v in L["hedges"] and L["hedges"][v]:
            legs["hedge"] = {**L["hedges"][v], "expiry": L["expiry"]}
        elif VERS[v] is not None:
            continue
        net_delta = sum(lg["qty"] * lg["delta"] for lg in legs.values())
        vers[v] = {"legs": legs, "net_delta": round(net_delta, 2)}
    return {**base, "status": "open", "side": "long" if sd > 0 else "short", "expiry": L["expiry"], "days_left": L["days_left"],
            "atm": L["atm"], "snap": L["snap"], "versions": vers,
            "why": f"{'near the day high' if sd > 0 else 'near the day low'} (position {loc:.2f} of the range) and "
                   f"{'up' if sd > 0 else 'down'} {abs(spot / prev_c - 1) * 100:.2f} % on the day at {AT}"}


def close_row(r):
    """Close an open row with the next trading day's first snapshot after 09:15 (if that file exists)."""
    TT = _TT()
    nxt = sorted(f[:10] for f in os.listdir(TT.OCD) if f.endswith(".csv.gz") and f[:10] > r["day"])
    if not nxt:
        return False
    for v, V in r["versions"].items():
        X = _price_exit(nxt[0], r["key"], V["legs"])
        if not X:
            return False
        g, c, n = _pnl(r["key"], V["legs"], X["px"])
        V.update(exit_px=X["px"], exit_snap=f"{nxt[0]} {X['snap']}", exit_spot=X["spot"], gross=g, costs=c, pnl=n)
    r.update(status="closed", closed=f"{nxt[0]} {X['snap']}", exit_spot=X["spot"],
             spot_pts=round((1 if r["side"] == "long" else -1) * (X["spot"] - r["spot"]), 1))
    return True


def tick(force: bool = False) -> bool:
    """Live-desk loop: at 15:13-15:20 records today's signal (once); 09:16-10:00 (and any later run) closes yesterday's rows."""
    now = datetime.now(); hm = now.strftime("%H:%M"); day = now.strftime("%Y-%m-%d")
    if not force and (time.time() - S["t"] < 45 or now.weekday() >= 5):
        return False
    S["t"] = time.time()
    rows = _load(); changed = False
    for r in rows:                                               # close open rows once the next day's snapshot exists
        if r.get("status") == "open" and r["day"] < day and hm >= "09:17":
            try:
                changed |= close_row(r)
            except Exception as e:
                r["err"] = str(e)[:120]
    if AT <= hm <= "15:20":
        have = {r["id"] for r in rows}
        for key in KEYS:
            if f"{key}-{day}" in have:
                continue
            try:
                b = _day_bars(key, day); pc, pc_day = _prev_close(key, day); a = _snap(day, key, hm)
                if not b or pc is None or a is None:
                    continue
                spot, hi, lo, src = b
                r = evaluate(key, day, spot, hi, lo, pc, a)
                r.update(source=src, prev_close_day=pc_day, recorded=now.isoformat(timespec="seconds"), mode="live")
                rows.append(r); changed = True
            except Exception as e:
                S[f"err_{key}"] = str(e)[:160]
    if changed:
        _save(rows)
    return changed


def trades():
    return _load()


def api() -> dict:
    T = _load(); cl = [r for r in T if r.get("status") == "closed"]
    rec = {}
    for v in VERS:
        z = [r["versions"][v]["pnl"] for r in cl if v in r.get("versions", {}) and "pnl" in r["versions"][v]]
        rec[v] = {"n": len(z), "rs": sum(z), "win_pct": round(100 * sum(x > 0 for x in z) / len(z)) if z else None}
    return {"open": [r for r in T if r.get("status") == "open"], "closed": cl[-30:][::-1], "record": rec,
            "note": "Paper only. Signal 15:13 (before the 15:15 closing auction), exit at the first snapshot after 09:15 next day."}
