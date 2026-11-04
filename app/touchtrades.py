"""
Touch-minute confirmation paper trades - Nifty / Bank Nifty (2026-10-30, Nikhil: "start taking trades with this rule").
PAPER ONLY, never orders. Built from the level-touch log (app/touchlog.py), which already records every key-level touch
09:30-14:30 IST with the touch minute's own delta (tb_delta) and the move from that minute's close 15 / 30 / 60 min later.

Rule (the lead from 21-23 Oct, claude/levels-fii-footprint.md - a hypothesis, NOT a tested edge):
  signal : a key level is touched and the touch minute closes with buyers clearly in control at support
           (delta / volume > +0.2) or sellers clearly in control at resistance (< -0.2)
  trade  : enter at that minute's close in the bounce direction (long at support, short at resistance)
  exit   : 30 minutes later, at the market (no stop, no target - exactly as the lead was measured)
  limits : one open trade per index; touches of merged levels in the same minute count once
Graded gross first (bps and index points on spot); net = 0.6-delta next-week option round trip in index points (Nifty ~5.5, Bank Nifty ~15; was futures 18 / 45,
claude/real-orderflow-test.md). Lot: Nifty 65, Bank Nifty 30.
'replay' = trades rebuilt from touches logged 26-28 Oct, before the rule was switched on (same rule, recorded data);
'live' = from 30 Oct. Judged by scripts/research/touch_review.py after ~35 sessions; nothing changes without Nikhil's approval.
"""
from __future__ import annotations

import glob
import json
import os
from datetime import datetime, timedelta

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
TDIR = os.path.join(ROOT, "data_store", "livedesk", "touches")
THR = 0.2
HOLD = 30
LIVE_FROM = "2026-10-30"
LOT = {"NIFTY": 65, "BANKNIFTY": 30}
# 2026-11-04 (Nikhil: "I will use options ... always 0.6 delta ... next week expiry"): the plain record is now netted with the
# index-point equivalent of a 0.6-delta next-week option round trip (charges + spread + ~30-60 min decay) / (0.6 x lot).
# Was 18 / 45 pts (1-lot futures). Rough estimate, not measured fills.
COST_PTS = {"NIFTY": 5.5, "BANKNIFTY": 15.0}
MIN_DAYS_LEFT = 7                                  # 2026-11-04: next-week expiry (>= 7 days left) - less decay; Bank Nifty = nearest monthly with 7+ days
SPREAD_PTS = {"NIFTY": 0.5, "BANKNIFTY": 1.5}
SYN_PTS = {"NIFTY": 4.5, "BANKNIFTY": 12.5}        # 2026-11-04: synthetic future (buy ATM call + sell ATM put) round trip, index points - record-only comparison
# 2026-11-04 TOUCH + DOM combo v1 (Nikhil: "another version ... combination of touch min and DOM ... start taking trades from tomorrow ...
# always use synthetic future"). Same touch-minute signal, PLUS the DOM book lean at entry: 20-level imbalance (futures, Dhan engine),
# mean of the last 30 s minus its own 30-min median >= +0.10 for a long / <= -0.10 for a short. Same exits as the card.
# Vehicle: synthetic future (buy ATM call + sell ATM put, next-week expiry), 1 lot, rupees after SYN_PTS. Paper only.
# History check (touch_dom_combo.py + touch_dom_review.py, 4 Nov): FAILED - combo -Rs 4,551 (4 trades, replay levels) and -Rs 24,361 (19 trades, live touch log); book lean flips sign between indices and halves (luck p 0.2-0.8).
COMBO_FROM, DOM_LEAN = "2026-11-05", 0.10
OF_SEC = os.path.join(os.environ.get("MP_DHAN_DIR") or os.path.join(os.path.expanduser("~"), "Documents", "dhan"), "orderflow", "data", "of")


def dom_lean(key, day, t_epoch):
    """Book lean at entry: mean 20-level imbalance of the last 30 s minus its median over the last 30 min (None if no book)."""
    try:
        s = pd.read_csv(os.path.join(OF_SEC, day, f"sec_{key}.csv"), usecols=["t", "bid", "ask", "imb20"])
    except Exception:
        return None
    s = s[(s.bid > 0) & (s.ask > 0) & (s.t <= t_epoch) & (s.t > t_epoch - 1800)].dropna(subset=["imb20"])
    last = s[s.t > t_epoch - 30]
    if len(last) < 10 or len(s) < 300:
        return None
    return round(float(last.imb20.mean() - s.imb20.median()), 3)      # 2026-11-04: assumed bid / ask + slippage per side, option premium points
NAME = {"NIFTY": "Nifty", "BANKNIFTY": "Bank Nifty"}


def _rows(day):
    try:
        return json.load(open(os.path.join(TDIR, f"{day}.json")))
    except Exception:
        return []


def trades_for(day: str, rows: list | None = None, spots: dict | None = None) -> list:
    rows = _rows(day) if rows is None else rows
    out, busy, seen = [], {}, set()
    for r in sorted(rows, key=lambda x: x.get("t_epoch", 0)):
        tb, ec = r.get("tb_delta"), r.get("entry_close")
        if tb is None or not ec or r["side"] * tb <= THR:
            continue
        k, mn = r["key"], r["time"][:5]
        if (k, mn) in seen:
            continue                                       # merged levels touched in the same minute = one event
        seen.add((k, mn))
        if r.get("entry_epoch", 0) < busy.get(k, 0):
            continue                                       # one open trade per index
        busy[k] = r.get("entry_epoch", 0) + HOLD * 60
        sd = r["side"]
        t = {"id": r["id"], "day": day, "key": k, "market": NAME.get(k, k), "time": mn, "level": r["level"], "level_name": r["name"],
             "side": "long" if sd > 0 else "short", "why": f"{'support' if sd > 0 else 'resistance'} {r['name']} {r['level']:,.1f} touched; "
             f"touch minute closed with {'buyers' if sd > 0 else 'sellers'} in control (delta {tb:+.2f})",
             "tb_delta": tb, "entry": ec, "exit_at": (datetime.strptime(mn, "%H:%M") + timedelta(minutes=HOLD + 1)).strftime("%H:%M"),
             "mode": "live" if day >= LIVE_FROM else "replay"}
        if "c30" in r:
            bps = r["c30"]; pts = round(bps / 1e4 * ec, 1)              # c30 is already in the trade's direction
            t.update(status="closed", exit=round(ec * (1 + sd * bps / 1e4), 1), bps=bps, pts=pts,
                     pts_net=round(pts - COST_PTS[k], 1), rs_1lot=round(pts * LOT[k]), rs_1lot_net=round((pts - COST_PTS[k]) * LOT[k]))
        else:
            px = (spots or {}).get(k)
            t.update(status="open", now=px, bps_now=round(sd * (px / ec - 1) * 1e4, 1) if px else None)
        out.append(t)
    return out


def _score(ts):
    c = [t for t in ts if t["status"] == "closed"]
    if not c:
        return None
    n = len(c)
    return {"n": n, "win_pct": round(100 * sum(t["bps"] > 0 for t in c) / n), "avg_bps": round(sum(t["bps"] for t in c) / n, 1),
            "pts": round(sum(t["pts"] for t in c), 1), "pts_net": round(sum(t["pts_net"] for t in c), 1),
            "rs_1lot": sum(t["rs_1lot"] for t in c), "rs_1lot_net": sum(t["rs_1lot_net"] for t in c),
            "verdict": "too early" if n < 30 else ("holding" if sum(t["bps"] for t in c) > 0 else "not holding")}


def api(spots: dict | None = None) -> dict:
    days = sorted(os.path.basename(f)[:-5] for f in glob.glob(os.path.join(TDIR, "*.json")))
    today = datetime.now().strftime("%Y-%m-%d")
    allt = []
    for d in days:
        allt += trades_for(d, spots=spots if d == today else None)
    sc = {}
    for mode in ("live", "replay"):
        for k in ("NIFTY", "BANKNIFTY"):
            s = _score([t for t in allt if t["mode"] == mode and t["key"] == k])
            if s:
                sc[f"{k} {mode}"] = s
    return {"today": [t for t in allt if t["day"] == today], "recent": [t for t in allt if t["status"] == "closed"][-15:][::-1],
            "open": [t for t in allt if t["status"] == "open"], "scorecard": sc, "sessions_live": len([d for d in days if d >= LIVE_FROM]),
            "rule": "Key level touched, and the touch minute closes with buyers in control at support / sellers at resistance (delta beyond 0.2) -> "
                    "enter at that minute's close in the bounce direction, exit 30 min later. No stop. One trade per index at a time. Paper only.",
            "evidence": "Lead from 21-23 Oct: +16.5 bps at 30 min, 11 of 13 positive, about 10 independent events, about 25 splits looked at. A hypothesis, not a tested edge.",
            "cost_note": "Net = after the index-point equivalent of a 0.6-delta next-week option round trip: about 5.5 pts (Nifty) / 15 pts (Bank Nifty)."}


# ---------------------------------------------------------------------------------------------------------------------
# Option-buying trade cards (2026-10-30, Nikhil: "use options buying for now .. trade cards with which strike, SL and trailing SL")
# Vehicle : buy the option with |delta| closest to 0.6 (call for a long, put for a short), nearest expiry with at least 1 day
#           left (no expiry-day options), from the latest option-chain snapshot (~3 min old). 1 lot.
# SL      : on SPOT 1-MINUTE CLOSES (from 30 Oct, after the tick study), 0.10 % beyond the touched level (the bounce has failed), at least 0.05 % from entry. Premium SL shown as an
#           estimate (premium - delta x spot distance).
# Trail   : spot +1R -> SL to entry (breakeven); spot +2R -> SL trails 1R behind the best spot. Still out after 30 min.
# Picked on the 26-28 Oct replay (14 trades - tiny; 0.05 / 0.10 / 0.15 % were looked at, middle one kept, not the best):
#   Nifty +220 pts vs +231 plain 30 min; Bank Nifty +180 vs -174. Not a tested edge; the plain 30-min result is kept beside it.
# Option prices are snapshot last-traded prices, not bid / ask. Costs: brokerage 2 x Rs 20, STT 0.1 % of the sell value,
# exchange 0.035 %, GST 18 %, stamp 0.003 % of the buy value, SEBI.
import math
import time as _time

import pandas as pd

CDIR = os.path.join(ROOT, "data_store", "livedesk", "touchtrades")
OCD = os.path.join(ROOT, "data_store", "flow", "option_chain")
SL_BUF, MIN_RISK, TARGET_DELTA = 0.001, 0.0005, 0.6
_OC: dict = {}
_T: dict = {"t": 0}


def _N(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def _delta(S, K, iv, days, cp):
    if not iv or iv <= 0 or days <= 0:
        return None
    t = days / 365; s = iv / 100
    d1 = (math.log(S / K) + 0.5 * s * s * t) / (s * math.sqrt(t))
    return _N(d1) if cp == "CE" else _N(d1) - 1


def _bs(S, K, iv, days, cp):
    """Black-Scholes price (r = 0). Used to move the entry premium with spot and time, because snapshot LTPs of thin
    strikes go stale (seen on 28 Oct: Bank Nifty 54600 PE showed the same LTP for 10 minutes while spot moved 160 pts)."""
    t = max(days, 1e-4) / 365; s = iv / 100
    d1 = (math.log(S / K) + 0.5 * s * s * t) / (s * math.sqrt(t)); d2 = d1 - s * math.sqrt(t)
    return S * _N(d1) - K * _N(d2) if cp == "CE" else K * _N(-d2) - S * _N(-d1)


def _chain(day):
    p = os.path.join(OCD, f"{day}.csv.gz")
    try:
        mt = os.path.getmtime(p)
    except OSError:
        return None
    c = _OC.get(day)
    if c and c[0] == mt:
        return c[1]
    o = pd.read_csv(p, usecols=["ts", "symbol", "expiry", "strike", "side", "ltp", "volume", "iv", "underlying"])
    o = o[o.expiry.notna()]; o["expiry"] = o.expiry.astype(str); o["ts"] = pd.to_datetime(o.ts)
    _OC.clear(); _OC[day] = (mt, o)
    return o


def _pick(day, key, cp, now):
    o = _chain(day)
    if o is None:
        return None
    x = o[(o.symbol == key) & (o.side == cp) & (o.ts <= now)]
    if not len(x):
        return None
    s0 = x.ts.max(); a = x[x.ts == s0]
    for exp in sorted(a.expiry.unique()):
        days = (pd.Timestamp(exp) + pd.Timedelta(hours=15.5) - s0).total_seconds() / 86400
        if days < MIN_DAYS_LEFT:
            continue                                         # next-week expiry or later (no near-expiry options)
        b = a[(a.expiry == exp) & (a.ltp > 0) & (a.iv > 0) & (a.volume > 0)].copy()
        if len(b) > 4:
            b = b[b.volume >= b.volume.median()]                 # liquid strikes only
        if not len(b):
            continue
        S = float(b.underlying.iloc[0])
        b["dl"] = [_delta(S, k, iv, days, cp) for k, iv in zip(b.strike, b.iv)]
        b = b[b.dl.notna()]
        if not len(b):
            continue
        r = b.iloc[(b.dl.abs() - TARGET_DELTA).abs().argsort().iloc[0]]
        return {"expiry": exp, "strike": float(r.strike), "cp": cp, "delta": round(float(r.dl), 2), "premium": float(r.ltp), "iv": float(r.iv),
                "days_left": round(days, 1), "snap": str(s0)[11:16], "underlying": S}
    return None


def _prem(day, key, exp, strike, cp, now):
    o = _chain(day)
    if o is None:
        return None, None, None
    x = o[(o.symbol == key) & (o.side == cp) & (o.expiry == exp) & (o.strike == strike) & (o.ts <= now) & (o.ltp > 0)]
    if not len(x):
        return None, None, None
    r = x.loc[x.ts.idxmax()]
    return float(r.ltp), str(r.ts)[11:16], float(r.underlying)


def _costs(buy, sell, key=None, lot=0):
    """Round-trip cost of 1 option lot (Rs). 2026-11-04: STT 0.15 % of the sell value (from 1 Apr 2026, was 0.1 %) and an assumed
    spread / slippage of SPREAD_PTS per side (snapshot prices are last-traded, not bid / ask)."""
    exch = 0.00035 * (buy + sell)
    spread = 2 * SPREAD_PTS.get(key, 0) * lot
    return round(40 + 0.0015 * sell + exch + 0.18 * (40 + exch) + 0.00003 * buy + 0.000001 * (buy + sell) + spread, 1)


def _cpath(day):
    return os.path.join(CDIR, f"cards_{day}.json")


def load_cards(day):
    try:
        return json.load(open(_cpath(day)))
    except Exception:
        return []


def _save_cards(day, cards):
    os.makedirs(CDIR, exist_ok=True)
    tmp = _cpath(day) + ".tmp"
    json.dump(cards, open(tmp, "w"), indent=1, default=str)
    os.replace(tmp, _cpath(day))


def tick(t: datetime, spots: dict) -> bool:
    """Called from the Nifty desk poll (after the touch log). Opens a card for each new live signal, manages open cards."""
    if _time.time() - _T["t"] < 15:
        return False
    _T["t"] = _time.time()
    day = t.strftime("%Y-%m-%d")
    if day < LIVE_FROM:
        return False
    cards = load_cards(day); have = {c["id"] for c in cards}; changed = False
    now = pd.Timestamp(t.replace(tzinfo=None))
    for s in trades_for(day, spots=spots):
        if s["id"] in have or s["mode"] != "live":
            continue
        k, sd = s["key"], (1 if s["side"] == "long" else -1)
        e, L = s["entry"], s["level"]
        stop = min(L * (1 - SL_BUF), e * (1 - MIN_RISK)) if sd > 0 else max(L * (1 + SL_BUF), e * (1 + MIN_RISK))
        R = abs(e - stop)
        c = {"id": s["id"], "day": day, "key": k, "market": s["market"], "time": s["time"], "side": s["side"], "level": L, "level_name": s["level_name"],
             "why": s["why"], "spot_entry": e, "spot_sl": round(stop, 1), "spot_sl0": round(stop, 1), "R_pts": round(R, 1), "best": e,
             "exit_by": s["exit_at"], "status": "open", "trail": "initial SL", "lot": LOT[k], "vdone": False}
        o = _pick(day, k, "CE" if sd > 0 else "PE", now)
        if o:
            c["option"] = {**o, "name": f"{NAME[k]} {pd.Timestamp(o['expiry']):%d %b} {o['strike']:.0f} {o['cp']}"}
            # premium = Black-Scholes at the entry spot with the snapshot IV (the snapshot itself is up to ~3 min old)
            c["prem_entry"] = round(_bs(e, o["strike"], o["iv"], o["days_left"], o["cp"]), 1); c["capital"] = round(c["prem_entry"] * LOT[k])
            c["t_entry"] = _time.time()
            c["prem_sl_est"] = round(max(0.05, c["prem_entry"] - abs(o["delta"]) * R), 1)
        else:
            c["option"] = None; c["note"] = "no option-chain snapshot - spot only"
        if day >= COMBO_FROM:
            ln = dom_lean(k, day, _time.time())
            c["dom_lean"] = ln
            c["combo"] = ln is not None and (ln >= DOM_LEAN if sd > 0 else ln <= -DOM_LEAN)
            c["combo_v"] = "v1"
        cards.append(c); changed = True
    for c in cards:
        if c["status"] != "open" and c.get("vdone", True):
            continue
        px = spots.get(c["key"])
        if not px:
            continue
        sd = 1 if c["side"] == "long" else -1; e = c["spot_entry"]
        hm = t.strftime("%H:%M")
        mins = (datetime.strptime(hm, "%H:%M") - datetime.strptime(c["time"], "%H:%M")).seconds / 60 - 1   # minutes since entry
        new_min = c.get("m_last") not in (None, hm)
        close1 = c.get("px_last") if new_min else None          # close of the minute that just finished
        c["m_last"], c["px_last"] = hm, px
        prem = None
        if c.get("option"):
            o = c["option"]; p, snap, und = _prem(c["day"], c["key"], o["expiry"], o["strike"], o["cp"], now)
            if p:
                c["ltp_last"], c["ltp_at"] = p, snap                                   # reference only (can be stale)
            el = (_time.time() - c.get("t_entry", _time.time())) / 86400
            prem = round(_bs(px, o["strike"], o["iv"], o["days_left"] - el, o["cp"]), 1)   # same IV, current spot and time
        def fin(d, why):
            d.update(done=True, why=why, at=hm, spot_pts=round(sd * (px - e), 1))
            if prem is not None:
                b, s_ = c["prem_entry"] * c["lot"], prem * c["lot"]
                d.update(prem=prem, rs_net=round(s_ - b - _costs(b, s_, c["key"], c["lot"])))
        def manage(d, close):
            """SL / trail on 1-minute CLOSES (ticks stopped trades out on noise in the 26-28 Oct tick study)."""
            if close is None or d.get("done"):
                return None
            if sd * (close - d["sl"]) <= 0:
                return "SL" if d["trail"] == "initial SL" else ("breakeven SL" if d["trail"] == "breakeven" else "trailing SL")
            d["best"] = max(d["best"], close) if sd > 0 else min(d["best"], close)
            mv = sd * (d["best"] - e)
            if mv >= 2 * d["R"]:
                nt = d["best"] - sd * d["R"]; d["sl"] = round(max(d["sl"], nt) if sd > 0 else min(d["sl"], nt), 1); d["trail"] = "trailing 1R"
            elif mv >= d["R"] and d["trail"] == "initial SL":
                d["sl"] = round(e, 1); d["trail"] = "breakeven"
            return None
        V = c.setdefault("var", {})                                   # record-only variants, never change the card
        if "wide" not in V:
            L = c["level"]; st = min(L * 0.998, e * (1 - MIN_RISK)) if sd > 0 else max(L * 1.002, e * (1 + MIN_RISK))
            V["wide"] = {"label": "SL 0.20% past the level (1-min closes) + same trail", "sl": round(st, 1), "R": round(abs(e - st), 1), "best": e, "trail": "initial SL"}
            for n in (5, 10, 15):
                V[f"x{n}"] = {"label": f"exit after {n} min"}
            V["plain30"] = {"label": "rule as tested: no SL, exit after 30 min"}
        if "sl15" not in V:                                           # 30 Oct, Nikhil: track 0.15 % too, compare later
            L = c["level"]; st = min(L * 0.9985, e * (1 - MIN_RISK)) if sd > 0 else max(L * 1.0015, e * (1 + MIN_RISK))
            V["sl15"] = {"label": "SL 0.15% past the level (1-min closes) + same trail", "sl": round(st, 1), "R": round(abs(e - st), 1), "best": e, "trail": "initial SL"}
        for vk in ("wide", "sl15"):
            w = manage(V[vk], close1)
            if w: fin(V[vk], w)
        for n in (5, 10, 15):
            if not V[f"x{n}"].get("done") and mins >= n: fin(V[f"x{n}"], f"{n} min")
        end = mins >= 30 or hm >= "15:20"
        for k in ("wide", "sl15", "plain30"):
            if end and not V[k].get("done"): fin(V[k], "30 min")
        c["vdone"] = all(v.get("done") for v in V.values())
        if c["status"] == "open":
            d = {"sl": c["spot_sl"], "R": c["R_pts"], "best": c["best"], "trail": c["trail"]}
            why = manage(d, close1)
            c.update(spot_sl=d["sl"], best=d["best"], trail=d["trail"])
            if not why and (hm >= c["exit_by"] or hm >= "15:20"):
                why = "30 min"
            c["spot_now"] = px
            if prem is not None:
                c["prem_now"] = prem
            if why:
                c.update(status="closed", exit_reason=why, closed=hm, spot_exit=round(px, 1), spot_pts=round(sd * (px - e), 1))
                c["synth_rs_net"] = round((c["spot_pts"] - SYN_PTS[c["key"]]) * c["lot"])        # same exit, synthetic future instead
                if c.get("option") and c.get("prem_now"):
                    b, s_ = c["prem_entry"] * c["lot"], c["prem_now"] * c["lot"]
                    cs = _costs(b, s_, c["key"], c["lot"])
                    c.update(prem_exit=c["prem_now"], rs_gross=round(s_ - b), costs=cs, rs_net=round(s_ - b - cs), ret_pct=round((s_ - b - cs) / b * 100, 1))
        changed = True
    if changed:
        _save_cards(day, cards)
    return changed


def cards_api() -> dict:
    files = sorted(glob.glob(os.path.join(CDIR, "cards_*.json")))
    allc = [c for f in files for c in load_cards(os.path.basename(f)[6:16])]
    cl = [c for c in allc if c["status"] == "closed" and c.get("rs_net") is not None]
    sc = {}
    for k in ("NIFTY", "BANKNIFTY"):
        z = [c for c in cl if c["key"] == k]
        if z:
            sc[k] = {"n": len(z), "win_pct": round(100 * sum(c["rs_net"] > 0 for c in z) / len(z)), "rs_net": sum(c["rs_net"] for c in z),
                     "avg_ret_pct": round(sum(c["ret_pct"] for c in z) / len(z), 1), "max_capital": max(c["capital"] for c in z)}
    vs = {}
    for k in ("NIFTY", "BANKNIFTY"):
        for vn in ("x5", "x10", "x15", "plain30", "sl15", "wide"):
            z = [c["var"][vn]["rs_net"] for c in allc if c["key"] == k and c.get("var", {}).get(vn, {}).get("rs_net") is not None]
            if z:
                vs[f"{k} {vn}"] = {"n": len(z), "rs_net": sum(z), "win_pct": round(100 * sum(x > 0 for x in z) / len(z))}
    for k in ("NIFTY", "BANKNIFTY"):
        z = [c["synth_rs_net"] for c in cl if c["key"] == k and c.get("synth_rs_net") is not None]
        if z and k in sc:
            sc[k]["synthetic"] = {"n": len(z), "rs_net": sum(z), "win_pct": round(100 * sum(x > 0 for x in z) / len(z))}
    today = datetime.now().strftime("%Y-%m-%d")
    return {"variants": vs, "cards": [c for c in allc if c["day"] == today][::-1], "recent": cl[-10:][::-1], "scorecard": sc,
            "price_note": "Option prices are estimates: Black-Scholes with the IV from the option-chain snapshot at entry, moved with spot and time. Real fills differ (bid / ask, IV changes).",
            "rule_option": "Buy the ~0.6-delta option (call for long, put for short), next-week expiry (7+ days left; Bank Nifty: nearest monthly with 7+ days), 1 lot. Costs: real charges (STT 0.15 %) + spread 0.5 pt (Nifty) / 1.5 pt (Bank Nifty) a side. SL on spot 0.10% beyond the level; "
                           "+1R -> SL to entry; +2R -> trail 1R behind the best spot; out after 30 min."}
