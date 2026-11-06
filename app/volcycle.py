"""NIFTY DAY / NIGHT VOL CYCLE - PAPER ONLY (2026-11-06, Nikhil: "move ahead" on claude/nifty-weekly-vol-book.md).
Never places orders. Research: scripts/research/nifty_weekly_vol_replay.py + nifty_vol_cycle_check.py on the recorded chain
(5 Oct - 6 Nov): ATM weekly IV falls ~2.4 vol pts from 09:20 to 15:25 and rises ~2.2 pts overnight (up on 86 % of nights).

Rules (fixed before any live result), Nifty only, nearest weekly expiry dated after today, ATM = traded strike nearest spot:
  night : BUY the ATM straddle (CE + PE) at the latest snapshot <= 15:26 (taken 15:22-15:29)
          exit next trading day at the first snapshot >= 09:45 (window to 10:15).   History: +29.9 pts/unit gross, 13 nights.
  day   : SELL the ATM straddle at the first snapshot >= 10:30 (window to 10:45), exit at the latest snapshot <= 15:25.
          History: +10.9 pts/unit gross, 14 days.
          Two versions side by side:  plain = naked straddle (as tested)
                                       wings = + buy CE / PE about 2 % out of the money (defined risk, desk rule)
  Each row records the worst move against it before the exit (mae_pts) so a stop can be judged later - the exit never changes.
  Hedge: none at entry (an ATM straddle starts near delta 0; net delta is recorded). A synthetic-future hedge is a later version.
  Prices: snapshot LTP of traded strikes (volume > 0). Costs per leg: touchtrades._costs (incl. spread allowance).
Judge after 30 graded trades per rule plus 4 weeks: net positive in both halves. Nothing changes without Nikhil's approval.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime

import numpy as np
import pandas as pd

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
OUT = os.path.join(ROOT, "data_store", "livedesk", "volcycle")
KEY, LOT, WING = "NIFTY", 65, 0.02
# 2026-11-06 (Nikhil: "retire and fully remove"): night tilt spread, night tilt straddle and day wings (iron fly) retired + removed
VERSIONS = (("night", "plain"),
            ("day", "plain"), ("day", "skew"), ("day", "bwing"), ("day", "dhedge"))
HEDGE_LOTS, HEDGE_BAND = 4, 1.0   # dhedge version: 4-lot straddle book, re-hedge with whole synthetic lots when |delta| >= 1 lot
TREND_PCT = 0.3          # day-leg trend threshold for the record-only skew / bwing versions (as tested; sensitive - see _directional)
NIGHT_IN, NIGHT_OUT, DAY_IN, DAY_OUT = "15:26", "09:45", "10:30", "15:25"
S: dict = {"t": 0}


def _p(name="trades.json"):
    return os.path.join(OUT, name)


def _load(name="trades.json"):
    try:
        return json.load(open(_p(name)))
    except Exception:
        return []


def _save(rows, name="trades.json"):
    os.makedirs(OUT, exist_ok=True)
    tmp = _p(name) + f".{os.getpid()}.tmp"
    json.dump(rows, open(tmp, "w"), indent=1, default=str)
    os.replace(tmp, _p(name))


def _TT():
    import touchtrades as TT
    return TT


def _nifty(day):
    o = _TT()._chain(day)
    if o is None:
        return None
    return o[o.symbol == KEY]


def _snap(o, at, before=True, lo=None, hi=None):
    """Rows of one snapshot: latest <= at (before) or first >= at; optional HH:MM window [lo, hi]."""
    hm = o.ts.dt.strftime("%H:%M")
    x = o[(hm <= at) if before else (hm >= at)]
    if lo:
        x = x[x.ts.dt.strftime("%H:%M") >= lo]
    if hi:
        x = x[x.ts.dt.strftime("%H:%M") <= hi]
    if not len(x):
        return None
    t = x.ts.max() if before else x.ts.min()
    return x[x.ts == t]


def _expiry(a, day):
    ex = sorted(e for e in a.expiry.unique() if str(e)[:10] > day)
    return ex[0] if ex else None


def _leg(a, exp, k, cp, qty):
    r = a[(a.expiry == exp) & (a.strike == k) & (a.side == cp) & (a.ltp > 0)]
    if not len(r):
        return None
    TT = _TT(); s0 = a.ts.iloc[0]; S_ = float(a.underlying.iloc[0])
    days = (pd.Timestamp(exp) + pd.Timedelta(hours=15.5) - s0).total_seconds() / 86400
    iv = float(r.iv.iloc[0]) if float(r.iv.iloc[0]) > 0 else None
    dl = TT._delta(S_, k, iv, days, cp) if iv else None
    return {"strike": float(k), "cp": cp, "qty": qty, "expiry": exp, "entry": float(r.ltp.iloc[0]), "iv": iv,
            "delta": round(dl, 3) if dl is not None else None}


def _near(b, cp, target):
    """Traded strike nearest a target price (research rule for the directional versions)."""
    x = b[b.side == cp]
    return None if not len(x) else float(x.strike.iloc[(x.strike - target).abs().argsort().iloc[0]])


def _prev_close(day):
    try:
        import overnight as OV
        return OV._prev_close(KEY, day)[0]
    except Exception:
        return None


def _directional(a, o, day, kind, exp, atm, S_, b, core):
    """2026-11-06 (Nikhil: "add those, go deep in greeks"): record-only directional versions beside the straddle.
    Research: scripts/research/nifty_vol_cycle_directional.py - nothing here changes plain / wings.
      night, only on strong / weak closes (day-range position >= 0.80 and up on the day, or <= 0.20 and down):
        (tilt_spread and tilt_straddle were retired + removed on 6 Nov; only the signal info is still recorded)
      day, trend = spot at entry vs the first snapshot after 09:15, |move| >= TREND_PCT:
        skew  = up-trend: bull put spread (sell ATM PE, buy PE ~2 % lower); down-trend: bear call mirror; no trend: = plain
        bwing = short straddle + wing 1 % OTM on the trend side, 3 % on the other; no trend: 2 % / 2 %
        replay 14 days at 0.3 %: skew +25.0 pts, bwing +19.2 vs plain +10.3 - but only 3 down-trend days, and at 0.4-0.5 % the gain fades."""
    out, info = {}, {}
    if o is None:
        return out, info
    if kind == "night":
        path = o[o.ts <= a.ts.iloc[0]].groupby("ts").underlying.first()
        hi, lo = float(path.max()), float(path.min()); pc = _prev_close(day)
        loc = (S_ - lo) / (hi - lo) if hi > lo else 0.5
        sig = 0 if pc is None else (1 if (loc >= 0.8 and S_ > pc) else -1 if (loc <= 0.2 and S_ < pc) else 0)
        info = {"close_loc": round(loc, 2), "prev_close": pc, "signal": {1: "strong close", -1: "weak close", 0: "none"}[sig]}
    else:
        h = o.ts.dt.strftime("%H:%M"); f = o[(h >= "09:15") & (h <= "09:30")]
        if not len(f):
            return out, info
        s0 = float(f[f.ts == f.ts.min()].underlying.iloc[0]); mv = (S_ / s0 - 1) * 100
        tr = 1 if mv >= TREND_PCT else -1 if mv <= -TREND_PCT else 0
        info = {"move_to_entry_pct": round(mv, 2), "trend": {1: "up", -1: "down", 0: "none"}[tr]}
        w = lambda cp, pct: _near(b, cp, S_ * (1 + pct / 100) if cp == "CE" else S_ * (1 - pct / 100))
        if tr > 0:
            sk = {"pe": core["pe"], "wpe": _leg(a, exp, w("PE", 2), "PE", 1)}
            bw = {**core, "wce": _leg(a, exp, w("CE", 1), "CE", 1), "wpe": _leg(a, exp, w("PE", 3), "PE", 1)}
        elif tr < 0:
            sk = {"ce": core["ce"], "wce": _leg(a, exp, w("CE", 2), "CE", 1)}
            bw = {**core, "wce": _leg(a, exp, w("CE", 3), "CE", 1), "wpe": _leg(a, exp, w("PE", 1), "PE", 1)}
        else:
            sk = dict(core)
            bw = {**core, "wce": _leg(a, exp, w("CE", 2), "CE", 1), "wpe": _leg(a, exp, w("PE", 2), "PE", 1)}
        for name, legs in (("skew", sk), ("bwing", bw)):
            if None not in legs.values():
                out[name] = legs
    return out, info


def _open(a, day, kind, o=None):
    """Build the legs for 'night' (buy straddle) or 'day' (sell straddle, plain + wings) from snapshot rows a.
    o = that day's Nifty chain (needed only for the record-only directional versions)."""
    exp = _expiry(a, day)
    if exp is None:
        return None
    S_ = float(a.underlying.iloc[0])
    b = a[(a.expiry == exp) & (a.ltp > 0) & (a.volume > 0)]
    both = sorted(set(b[b.side == "CE"].strike) & set(b[b.side == "PE"].strike))
    if not both:
        return None
    atm = min(both, key=lambda k: abs(k - S_))
    q = 1 if kind == "night" else -1
    core = {"ce": _leg(a, exp, atm, "CE", q), "pe": _leg(a, exp, atm, "PE", q)}
    if None in core.values():
        return None
    vers = {"plain": core}
    if kind == "day":
        vers["dhedge"] = dict(core)          # same straddle; P&L adds the delta-band synthetic hedge at close (greekbook.hedge_path)
    extra, info = _directional(a, o, day, kind, exp, atm, S_, b, core)
    vers.update(extra)
    for v, legs in vers.items():
        nd = sum(lg["qty"] * (lg["delta"] or 0) for lg in legs.values())
        vers[v] = {"legs": legs, "net_delta": round(nd, 3)}
    return {"expiry": exp, "atm": atm, "spot": round(S_, 2), "snap": str(a.ts.iloc[0])[11:16], "versions": vers, "context": info}


def _px(a, lg):
    r = a[(a.expiry == lg["expiry"]) & (a.strike == lg["strike"]) & (a.side == lg["cp"]) & (a.ltp > 0)]
    return float(r.ltp.iloc[0]) if len(r) else None


def _pnl(legs, px):
    TT = _TT(); gross = cost = 0.0; pts = 0.0
    for n, lg in legs.items():
        e, x = lg["entry"] * LOT, px[n] * LOT
        pts += (px[n] - lg["entry"]) * lg["qty"]
        if lg["qty"] > 0:
            gross += x - e; cost += TT._costs(e, x, KEY, LOT)
        else:
            gross += e - x; cost += TT._costs(x, e, KEY, LOT)
    return round(gross), round(cost), round(gross - cost), round(pts, 2)


def _mae(o, r, legs, t_in, t_out):
    """Worst open P&L (pts per unit, before costs) between entry and exit snapshots - record only."""
    worst = 0.0
    x = o[(o.ts > pd.Timestamp(t_in)) & (o.ts <= pd.Timestamp(t_out))]
    for _, a in x.groupby("ts"):
        px = {n: _px(a, lg) for n, lg in legs.items()}
        if None in px.values():
            continue
        worst = min(worst, sum((px[n] - lg["entry"]) * lg["qty"] for n, lg in legs.items()))
    return round(worst, 2)


def _close(r, a, o_exit=None):
    """Close every version of row r at snapshot rows a. o_exit = that day's chain (for the day rule's MAE)."""
    out = {}
    for v, V in r["versions"].items():
        px = {n: _px(a, lg) for n, lg in V["legs"].items()}
        if None in px.values():
            return False
        out[v] = px
    for v, V in r["versions"].items():
        g, c, n, pts = _pnl(V["legs"], out[v])
        V.update(exit_px=out[v], gross=g, costs=c, pnl=n, pts=pts)
        if o_exit is not None:
            V["mae_pts"] = _mae(o_exit, r, V["legs"], f"{r['day']} {r['snap']}", str(a.ts.iloc[0]))
    r.update(status="closed", closed=str(a.ts.iloc[0])[:16], exit_spot=round(float(a.underlying.iloc[0]), 2))
    return True


def _next_day(day):
    TT = _TT()
    nxt = sorted(f[:10] for f in os.listdir(TT.OCD) if f.endswith(".csv.gz") and f[:10] > day)
    return nxt[0] if nxt else None


def _row(kind, day, L, mode):
    return {"id": f"{kind}-{day}", "rule": kind, "key": KEY, "day": day, "status": "open", "mode": mode, **L,
            "why": ("buy the ATM straddle into the close: IV has risen overnight on most nights (+2.2 vol pts avg)" if kind == "night"
                    else "sell the ATM straddle after 10:30: ATM IV has fallen into the close on most days (-1.9 vol pts avg)")}


def _try_close(r, today=None, now_hm="23:59"):
    if r["rule"] == "day":
        o = _nifty(r["day"])
        if o is None or (today == r["day"] and now_hm < "15:27"):       # let the ~15:25 snapshot land first
            return False
        a = _snap(o, DAY_OUT, before=True, lo="15:15")                  # exit only on a real close snapshot (15:15-15:25)
        if a is None:
            if today is None or today > r["day"]:                         # day over and no close snapshot (feed stopped early)
                r.update(status="no exit", note="no snapshot 15:15-15:25 that day")
                return True
            return False
        ok = _close(r, a, o)
        if ok and "dhedge" in r["versions"]:
            _apply_hedge(r, o)
        return ok
    nd = _next_day(r["day"])
    if nd is None or (today == nd and now_hm < NIGHT_OUT):
        return False
    if str(r["expiry"])[:10] < nd:                                    # feed gap ran past expiry: cannot price the exit
        r.update(status="no exit", note=f"expiry {str(r['expiry'])[:10]} passed before the next snapshot day {nd}")
        return True
    o = _nifty(nd)
    a = None if o is None else _snap(o, NIGHT_OUT, before=False, hi="10:15")
    if a is None:
        if today is None or nd < today or now_hm > "10:15":
            r.update(status="no exit", note=f"no {nd} snapshot 09:45-10:15")
            return True
        return False
    if np.busday_count(r["day"], nd) != 1:
        r["note"] = f"exit on {nd}: snapshot days missing in between (holiday or feed gap)"
    return _close(r, a)


def _GB():
    import greekbook as GB
    return GB


def _apply_hedge(r, o):
    """dhedge version = plain straddle + delta-band hedge replayed through the day's snapshots (same rule as the live indication)."""
    GB = _GB(); V = r["versions"]["dhedge"]
    h = GB.hedge_path(o, V["legs"], f"{r['day']} {r['snap']}", r["closed"] + ":59", lots=HEDGE_LOTS, band=HEDGE_BAND, TT=_TT())
    V.update(pts=round(V["pts"] + h["hedge_pts"], 2), gross=round(V["gross"] + h["hedge_pts"] * LOT),
             costs=round(V["costs"] + h["hedge_cost"]), pnl=round(V["pnl"] + h["hedge_pts"] * LOT - h["hedge_cost"]),
             hedge={k: h[k] for k in ("hedge_pts", "hedge_cost", "hedge_trades", "lots", "band", "path")})


_ZERO = {"delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0, "resid": 0.0}


def _monitor(rows, day, o=None):
    """Continuous greeks: on each new snapshot, for every open row and version - book greeks, running P&L split, and the
    hedge indication for the day straddle (indication only). Logged to greeks_<day>.jsonl; latest kept on the row as r['mon']."""
    GB = _GB()
    o = o if o is not None else _nifty(day)
    if o is None or not len(o):
        return False
    a = o[o.ts == o.ts.max()]; ts = str(a.ts.iloc[0]); hm = ts[11:16]; lines = []
    for r in rows:
        if r.get("status") != "open":
            continue
        if (r["rule"] == "day" and r["day"] != day) or (r["day"] == day and hm <= r["snap"]):
            continue
        M = r.setdefault("mon", {})
        if M.get("_ts") == ts:
            continue
        for v, V in r["versions"].items():
            st = GB.state(a, V["legs"])
            if st is None:
                continue
            m = M.setdefault(v, {"attr": dict(_ZERO)})
            if "prev" not in m:                                          # first look: start from the entry snapshot
                e = _nifty(r["day"]); e = None if e is None else e[e.ts.dt.strftime("%H:%M") == r["snap"]]
                m["prev"] = GB.state(e, V["legs"]) if e is not None and len(e) else None
            if m.get("prev"):
                for k, x in GB.step_attr(m["prev"], st, V["legs"]).items():
                    m["attr"][k] = round(m["attr"][k] + x, 2)
            m["prev"] = {k: st[k] for k in ("ts", "F", "T", "legs")}
            m["now"] = {k: st[k] for k in ("ts", "F", "spot", "delta", "gamma", "theta", "vega", "pnl_pts")}
            line = {"ts": ts, "id": r["id"], "ver": v, **m["now"], "attr": m["attr"]}
            if r["rule"] == "day" and v == "dhedge":
                sig = GB.hedge_signal(st["delta"], HEDGE_LOTS, HEDGE_BAND, M.get("held", 0.0))
                if sig:
                    M["held"] = M.get("held", 0.0) + sig["synthetic_lots"]
                    M.setdefault("hedge_log", []).append({"ts": hm, **sig, "held": M["held"]})
                    line["hedge"] = sig
                M["hedge_now"] = {"held_lots": M.get("held", 0.0), "book_delta_lots": round(st["delta"] * HEDGE_LOTS + M.get("held", 0.0), 2),
                                  "book": f"{HEDGE_LOTS} lots", "band": HEDGE_BAND}
            lines.append(line)
        M["_ts"] = ts
    if lines:
        os.makedirs(OUT, exist_ok=True)
        with open(_p(f"greeks_{day}.jsonl"), "a") as f:
            for ln in lines:
                f.write(json.dumps(ln, default=str) + "\n")
    return bool(lines)


def tick(force: bool = False) -> bool:
    """Live-desk loop. 10:30-10:45 opens 'day'; 15:22-15:29 opens 'night'; closes rows when their exit snapshot exists."""
    now = datetime.now(); hm = now.strftime("%H:%M"); day = now.strftime("%Y-%m-%d")
    if not force and (time.time() - S["t"] < 45 or now.weekday() >= 5):
        return False
    S["t"] = time.time()
    rows = _load(); have = {r["id"] for r in rows}; changed = False
    for r in rows:
        if r.get("status") == "open":
            try:
                changed |= _try_close(r, day, hm)
            except Exception as e:
                r["err"] = str(e)[:120]; changed = True
    o = None
    #           kind     tick window       snapshot rule
    for kind, lo, hi, at, before, s_lo, s_hi in (("day", DAY_IN, "10:45", DAY_IN, False, None, "10:45"),
                                                ("night", "15:25", "15:29", NIGHT_IN, True, "15:22", None)):
        if not (lo <= hm <= hi) or f"{kind}-{day}" in have:
            continue
        try:
            o = o if o is not None else _nifty(day)
            a = None if o is None else _snap(o, at, before=before, lo=s_lo, hi=s_hi)
            L = a is not None and _open(a, day, kind, o)
            if L:
                rows.append({**_row(kind, day, L, "live"), "recorded": now.isoformat(timespec="seconds")}); changed = True
        except Exception as e:
            S[f"err_{kind}"] = str(e)[:160]
    if "09:15" <= hm <= "15:30":
        try:
            changed |= _monitor(rows, day, o)
        except Exception as e:
            S["err_monitor"] = str(e)[:160]
    if changed:
        _save(rows)
    return changed


def replay(save=True):
    """Re-run both rules on every recorded chain day (check that the code matches the research). Saved to replay.json,
    never mixed into the paper record."""
    TT = _TT()
    days = sorted(f[:10] for f in os.listdir(TT.OCD) if f.endswith(".csv.gz"))
    rows = []
    for d in days:
        o = _nifty(d)
        if o is None:
            continue
        for kind, at, before, lo, hi in (("day", DAY_IN, False, None, "10:45"), ("night", NIGHT_IN, True, "15:22", None)):
            a = _snap(o, at, before=before, lo=lo, hi=hi)
            L = a is not None and _open(a, d, kind, o)
            if L:
                r = _row(kind, d, L, "replay")
                _try_close(r)
                rows.append(r)
    if save:
        _save(rows, "replay.json")
    return rows


def trades():
    return _load()


def api() -> dict:
    T = _load(); cl = [r for r in T if r.get("status") == "closed"]
    rec = {}
    for rule, v in VERSIONS:
        z = [r["versions"][v]["pnl"] for r in cl if r["rule"] == rule and v in r["versions"] and "pnl" in r["versions"][v]]
        rec[f"{rule}_{v}"] = {"n": len(z), "rs": sum(z), "win_pct": round(100 * sum(x > 0 for x in z) / len(z)) if z else None}
    return {"open": [r for r in T if r.get("status") == "open"], "closed": cl[-30:][::-1], "record": rec,
            "note": "Paper only. Night: buy ATM straddle ~15:25, sell next day ~09:45. Day: sell ATM straddle 10:30, buy back 15:25."}


if __name__ == "__main__":
    import sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    R = replay()
    for rule, v in VERSIONS:
        z = [r for r in R if r["rule"] == rule and r.get("status") == "closed" and v in r["versions"] and not r.get("note")]
        pts = [r["versions"][v]["pts"] for r in z]; net = [r["versions"][v]["pnl"] for r in z]
        if z:
            h = len(pts) // 2
            print(f"{rule:<5} {v:<13} n={len(z):>2}  pts/unit {np.mean(pts):+6.1f} (halves {np.mean(pts[:h]):+5.1f} / {np.mean(pts[h:]):+5.1f})  "
                  f"net Rs/lot {np.mean(net):+7,.0f}  total {sum(net):+9,.0f}  win {np.mean([x > 0 for x in net]):.0%}")
    for r in R:
        if r.get("status") != "closed" or r.get("note"):
            print("  left out:", r["id"], r.get("status"), r.get("note", ""))
