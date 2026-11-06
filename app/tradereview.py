"""TRADE REVIEW FOR EVERY PAPER RULE (2026-11-06, Nikhil: "same columns for All Paper Trades ... record these observations for previous
trades too ... for all strategies and trades across all tabs in a single CSV file").

Read-only. Gives each closed paper trade the same three notes as the DOM table - what went right, what went wrong, where to improve -
from the trade's own price path. Ideas only: nothing here changes a rule.

Price path used:
    Nifty / Bank Nifty   : order-flow engine 1-sec futures mid (domcards._path)
    NSE stocks (tracked) : 20-level depth mid (domcards._path)
    crypto / metals / FX / commodities : the stored 15-min footprint bars (high / low / close), data_store/livedesk/footprint
Moves are measured from the path price AT ENTRY (not the entry price), so a spot-scale trade on a futures path is not shifted by the
basis. 1R = the trade's first stop distance. Results for finished trades are kept in data_store/livedesk/reviews/paper_reviews.json.
"""
from __future__ import annotations

import bisect
import json
import os
import time
from datetime import datetime, timedelta, timezone

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
OUT = os.path.join(ROOT, "data_store", "livedesk", "reviews")
FPD = os.path.join(ROOT, "data_store", "livedesk", "footprint")
IST = timezone(timedelta(hours=5, minutes=30))
POST_MIN = 30                     # how long after the exit we look ("what price did next")
VERSION = "v1"
_C: dict = {"rows": None, "dirty": False, "saved": 0}
_FP: dict = {}


def _DOM():
    import domcards as D
    return D


def _ep(s):
    """'2026-11-06 09:48' (IST) -> epoch."""
    try:
        return datetime.fromisoformat(str(s)[:16]).replace(tzinfo=IST).timestamp()
    except ValueError:
        return None


# ---------------------------------------------------------------- price paths
def _fp_desk(key):
    for d in ("crypto", "metals", "fx"):
        if os.path.isdir(os.path.join(FPD, d, key)):
            return d
    return None


def _fp_bars(key, t0, t1):
    """15-min footprint bars (UTC files) overlapping [t0, t1] -> list of (start_epoch, o, h, l, c)."""
    d = _fp_desk(key)
    if not d:
        return []
    out = []
    a = datetime.fromtimestamp(t0, timezone.utc).date() - timedelta(days=1)
    b = datetime.fromtimestamp(t1, timezone.utc).date()
    while a <= b:
        f = os.path.join(FPD, d, key, f"{a}.json")
        try:
            mt = os.path.getmtime(f)
            ck = _FP.get(f)
            if not ck or ck[0] != mt:
                _FP[f] = ck = (mt, json.load(open(f)) or {})
            for hm, x in ck[1].items():
                try:
                    st = datetime.fromisoformat(f"{a} {hm}").replace(tzinfo=timezone.utc).timestamp()
                    out.append((st, float(x["o"]), float(x["h"]), float(x["l"]), float(x["c"])))
                except Exception:
                    continue
        except Exception:
            pass
        a += timedelta(days=1)
    out.sort()
    return [x for x in out if x[0] + 900 > t0 and x[0] <= t1]


def _path(key, day, t0, t1):
    """(kind, rows). kind 'tick': rows = [(t, price)] ; kind 'bar': rows = [(t, o, h, l, c)]."""
    D = _DOM()
    try:
        ts, px = D._path(day, key)
    except Exception:
        ts, px = [], []
    if ts:
        i, j = bisect.bisect_left(ts, t0 - 120), bisect.bisect_right(ts, t1)
        return "tick", list(zip(ts[i:j], px[i:j])), (px[0] if px else None)
    bars = _fp_bars(key, t0 - 86400, t1)
    if bars:
        day0 = datetime.fromtimestamp(t0, timezone.utc).replace(hour=0, minute=0, second=0).timestamp()
        opn = next((b[1] for b in bars if b[0] >= day0), bars[0][1])
        return "bar", [b for b in bars if b[0] + 900 > t0 - 120], opn
    return None, [], None


def _moves(kind, rows, t0, t1, sd, R):
    """MFE / MAE in R inside [t0, t1], best move in R in the POST_MIN after t1, all from the path price at entry."""
    if not rows:
        return None
    if kind == "tick":
        p0 = next((p for t, p in rows if t >= t0), None)
        ins = [sd * (p - p0) / R for t, p in rows if t0 <= t <= t1] if p0 else []
        post = [sd * (p - p0) / R for t, p in rows if t1 < t <= t1 + POST_MIN * 60] if p0 else []
    else:
        b0 = next((b for b in rows if b[0] <= t0 < b[0] + 900), rows[0])
        p0 = b0[4] if b0[0] + 900 <= t0 + 60 else b0[1]
        inb = [b for b in rows if b[0] + 900 > t0 and b[0] <= t1]
        ins = [sd * (x - p0) / R for b in inb for x in (b[2], b[3])]
        post = [sd * (x - p0) / R for b in rows if t1 < b[0] <= t1 + max(POST_MIN, 60) * 60 for x in (b[2], b[3])]
    if not ins:
        return None
    return {"p0": p0, "mfe": max(ins), "mae": min(ins), "post_best": max(post) if post else None}


# ---------------------------------------------------------------- the notes
def review(t, R=None):
    """t = a papertrades row (rule, key, side, opened, closed, entry, stop, target(s), exit, exit_reason, pnl, r).
    R = first stop distance in price points (falls back to |entry - stop|). Returns dict or None (open / no data)."""
    if t.get("status") != "closed":
        return None
    t0, t1 = _ep(t.get("opened")), _ep(t.get("closed"))
    if not t0 or not t1:
        return None
    t1 += 59
    sd = 1 if t.get("side") == "long" else -1
    e = t.get("entry")
    if not R and t.get("stop") is not None and e is not None and abs(e - t["stop"]) > 0:
        R = abs(e - t["stop"])
    day = str(t["opened"])[:10]
    kind, rows, opn = _path(t["key"], day, t0, t1)
    rr = t.get("r")
    why = str(t.get("exit_reason") or "").lower()
    stop_hit = "stop" in why or why in ("sl", "sl hit") or why.startswith("sl")
    tgt_hit = "target" in why or why[:2] in ("t1", "t2", "t3") or "tp" == why[:2]
    pnl = t.get("pnl")
    right, wrong, imp = [], [], []
    out = {"mfe": None, "mae": None, "post_best": None, "day_move_pct": None, "path": kind or "none"}
    if not R or not rows:
        # no stop (overnight) or no stored path: notes from the result only
        if t.get("rule", "").startswith("ovn"):
            if pnl is not None and pnl > 0:
                right.append("The strong close carried into the next open, as the pattern says")
            else:
                wrong.append("The next open did not follow the close (gap went the other way or faded)")
                imp.append("Record the overnight news / US close before the 15:13 signal and test it as a filter")
        elif pnl is not None:
            (right if pnl > 0 else wrong).append(f"{'Won' if pnl > 0 else 'Lost'} {abs(rr):.2f}R" if rr is not None else ("Profit" if pnl > 0 else "Loss"))
        if not rows and not t.get("rule", "").startswith("ovn"):
            wrong.append("no stored price path for this market / day - review from the result only")
        out.update(right="; ".join(right), wrong="; ".join(wrong), improve=" · ".join(dict.fromkeys(imp)) or "-", final=True)
        return out
    m = _moves(kind, rows, t0, t1, sd, R)
    if not m:
        out.update(right="", wrong="no price path inside the trade window", improve="-", final=True)
        return out
    mfe, mae, pb = m["mfe"], m["mae"], m["post_best"]
    tg = t.get("targets") or ([t["target"]] if t.get("target") else None)
    tr_r = abs(tg[-1] - e) / R if tg and e is not None else 2.0
    mins = max(1, int((t1 - 59 - t0) // 60))
    if tgt_hit:
        right.append(f"Reached the target ({why}) in {mins} min")
        if pb is not None and pb > tr_r + 1:
            right.append(f"price kept going to +{pb:.1f}R after the exit"); imp.append("Book part at the target and trail the rest")
    elif stop_hit and (pnl or 0) > 0:
        right.append(f"Trailing / moved stop locked a gain ({why}, best +{mfe:.1f}R)")
        if mfe - (rr or 0) >= 1:
            wrong.append(f"gave back {mfe - (rr or 0):.1f}R from the best point"); imp.append("Test a tighter trail once past +2R")
    elif stop_hit:
        if mfe >= 1:
            wrong.append(f"Was +{mfe:.1f}R, then came all the way back to the stop"); imp.append("Move the stop to entry at +1R (breakeven)")
        elif mfe >= 0.3:
            wrong.append(f"Only a small move our way (+{mfe:.1f}R), then failed")
        else:
            wrong.append(f"Never moved our way (best +{mfe:.1f}R) - the signal did not follow through")
            imp.append("Wait for a second confirmation (a close beyond the level) before entering")
        if pb is not None and pb >= max(1.0, tr_r if tg else 1.5):
            wrong.append(f"after the stop, price went +{pb:.1f}R our way within {POST_MIN} min"); imp.append("Stop was too tight here - test a wider stop")
        right.append(f"Loss capped by the stop ({rr:+.2f}R)" if rr is not None else "Loss capped by the stop")
    else:
        if pnl is not None and pnl > 0:
            right.append(f"Closed in profit on the {why or 'time'} exit (best +{mfe:.1f}R, worst {mae:.1f}R)")
        else:
            wrong.append(f"{why or 'time'} exit without a gain (best +{mfe:.1f}R, worst {mae:.1f}R)")
        if mfe >= 1 and (rr if rr is not None else 0) < 0.3:
            wrong.append(f"was +{mfe:.1f}R during the trade and gave it back"); imp.append("Take profit at +1R or trail - the gain was given back")
        elif mfe < 0.5 and (pnl or 0) <= 0:
            imp.append("Move was too small - test a stricter entry filter")
    if opn and m["p0"]:
        dm = sd * (m["p0"] / opn - 1) * 100
        out["day_move_pct"] = round(dm, 2)
        lim = 1.5 if _fp_desk(t["key"]) == "crypto" else 0.5
        if dm <= -lim:
            wrong.append(f"traded against the day's move ({dm:+.1f}% from the open)"); imp.append(f"Skip trades against a strong day move (> {lim}%)")
    if kind == "tick" and str(t["opened"])[11:16] < "09:45":
        (wrong if stop_hit else right).append("taken in the first 30 min (noisy open)")
    out.update(right="; ".join(right), wrong="; ".join(wrong), improve=" · ".join(dict.fromkeys(imp)) or "-",
               mfe=round(mfe, 2), mae=round(mae, 2), post_best=None if pb is None else round(pb, 2),
               final=time.time() > t1 + POST_MIN * 60 + 120)
    return out


# ---------------------------------------------------------------- cache
def _cp():
    return os.path.join(OUT, "paper_reviews.json")


def _cache():
    if _C["rows"] is None:
        try:
            _C["rows"] = json.load(open(_cp()))
        except Exception:
            _C["rows"] = {}
    return _C["rows"]


def flush(force=False):
    if not _C["dirty"] or (not force and time.time() - _C["saved"] < 10):
        return
    os.makedirs(OUT, exist_ok=True)
    tmp = _cp() + f".{os.getpid()}.tmp"
    json.dump(_C["rows"], open(tmp, "w"), default=str)
    os.replace(tmp, _cp())
    _C["dirty"] = False; _C["saved"] = time.time()


def tid(t):
    return f"{t.get('rule')}|{t.get('key')}|{t.get('opened')}|{t.get('side')}"


def cached(t, R=None):
    if t.get("status") != "closed":
        return None
    C = _cache(); k = tid(t); h = C.get(k)
    if h and h.get("_v") == VERSION and (h.get("final") or time.time() - h.get("_t", 0) < 60):
        return h
    try:
        v = review(t, R)
    except Exception as e:
        v = {"right": "", "wrong": "", "improve": "-", "err": f"{type(e).__name__}: {str(e)[:60]}", "final": False}
    if v is None:
        return None
    v["_v"] = VERSION; v["_t"] = time.time()
    C[k] = v; _C["dirty"] = True
    flush()
    return v
