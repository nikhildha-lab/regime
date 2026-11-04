"""
DOM setups - paper test (2026-11-03, Nikhil: "Live trackers v2 - proceed" -> new trade cards on the two DOM setups).
Level first, DOM second (claude/dom-strategy-test-plan.md). Paper only - nothing here ever places an order.

Markets : Nifty + Bank Nifty near-month FUTURES (Dhan order-flow engine, per-second file: bid / ask / trades / 20-level book)
          + the 48 liquid F&O stocks on the same 20-level connection (book every 5 s; no trade feed for stocks).
Levels  : fixed for the day the first time they are read (saved in levels_<day>.json, so a restart gives the same day):
            index  - medium / strong blocks from the Level map (shifted to futures by the live basis) + the call / put OI walls
            stocks - yesterday's high / low / close (NSE end-of-day file)
          plus, from the day's own data: the opening range (09:15-09:30, stocks) / initial balance (09:15-10:15, index).
Setups (rules fixed in advance, before any result was seen):
  HOLD   price comes INTO a level zone from away (outside it for 2+ min), does not break through (no more than 2 bands
         past the far edge) and leaves it on the side it came from within 2 min, with the book leaning our way over the
         last 30 s (20-level imbalance at least 0.10 above its own 30-min median, for a long) and the aggressive side absorbed
         (index: market sells >= market buys while price was in the zone; stocks: 20-level bid qty refilled vs the touch).
         -> trade the bounce. Stop 2 bands beyond the far edge of the zone.
  BREAK  price leaves a level zone on the far side and stays beyond it for 30 s, book leaning the break way (0.15+ vs its 30-min median),
         index: market-order delta over the last 60 s >= 15% of volume in the break direction;
         stocks: the defending side's 20-level qty down 30%+ vs 2 min earlier (wall pulled / eaten).
         -> trade the break. Stop back at the near side of the zone (1 band inside).
  Both   target = the next level in the trade direction, at least 2R (else 2R); out after 60 min or at 15:20;
         new trades 09:30-15:00 only; one open trade per market; each level / setup / side fires once a day;
         at most 4 trades per market a day. Fill = mid price; costs taken off at grading.
Costs   : index = synthetic future 1 lot, Nifty 4.5 pts / Bank Nifty 12.5 pts round trip, P&L in Rs (0.6-delta option 5.5 / 15 pts kept for comparison); stocks 0.08% (brokerage + STT + charges + half the spread x2).
Money   : Rs 1,000 risked per trade (Rs = R after costs x 1,000). The "plan account" line also applies the plan's
          daily limits (first 3 trades of the day, stop for the day at -2R).
Judged  : after 30-40 sessions: positive after costs in both halves with 30+ trades (dom-strategy-test-plan.md).
"""
from __future__ import annotations

import json
import os
import time
from collections import deque
from datetime import datetime

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
OUT = os.path.join(ROOT, "data_store", "livedesk", "domcards")
TRADES = os.path.join(OUT, "trades.json")
OFD = os.path.join(os.environ.get("MP_DHAN_DIR") or os.path.join(os.path.expanduser("~"), "Documents", "dhan"), "orderflow", "data")
EOD = os.path.join(ROOT, "data_store", "market_data", "nse_stocks_eod.csv")
INDEX = ("NIFTY", "BANKNIFTY")
# 2026-11-04 (Nikhil trades the index with 0.6-delta next-week options, not futures): index cost = option round trip in index-point
# terms (charges + 0.5 / 1.5 pt spread a side + up to 60 min decay) / (0.6 x lot). Was 6 / 14 pts (futures, too low for 2026 STT).
COST_PTS = {"NIFTY": 5.5, "BANKNIFTY": 15.0}
# 2026-11-04 (Nikhil: "also try synthetic future ... buy ATM call + sell ATM put"): record-only comparison. Delta ~1, decay nets out;
# cost = 2 legs x 2 sides: brokerage 4 x Rs 20, STT 0.15 % of each sold premium, exchange, GST + spread 0.5 pt (Nifty) / 1.5 pt
# (Bank Nifty) per leg per side, in index points / lot. Rough estimate, not measured fills. Main grading stays the 0.6-delta option.
SYN_PTS = {"NIFTY": 4.5, "BANKNIFTY": 12.5}
# 2026-11-04 (Nikhil: "DOM index should also use synthetic futures and share profits in currency"): index trades are now graded
# as a SYNTHETIC FUTURE (buy ATM call + sell ATM put, next-week expiry), 1 lot, P&L in Rs = (points - SYN_PTS) x lot.
# The 0.6-delta option result is kept beside it for comparison (r_opt, pnl_opt = 0.6 x lot x (points - COST_PTS)).
LOT = {"NIFTY": 65, "BANKNIFTY": 30}
COST_STOCK = 0.0008
BAND = {"index": 0.0004, "stock": 0.0010}          # zone padding as a share of price (Nifty ~10 pts, stock 0.1%)
RISK_RS = 1000
P = {"away_s": 120, "hold_s": 120, "imb_hold": 0.10, "imb_break": 0.15, "break_s": 30, "delta_break": 0.15, "drop": 0.30,
     "max_min": 60, "first": "09:30", "last": "15:00", "flat": "15:20", "rr": 2.0, "per_day": 4, "plan_n": 3, "plan_stop": -2.0}
LABEL = "DOM hold / break"
EDGE = "lead - paper test (judge after 30-40 sessions)"

S = globals().get("S") or {"day": None}


def _hm(t: int) -> str:
    return datetime.fromtimestamp(t).strftime("%H:%M")


def _f(x):
    try:
        return float(x) if x not in ("", None) else None
    except ValueError:
        return None


# ------------------------------------------------------------------ levels
def _merge(z):
    z = sorted(z)
    out = []
    for lo, hi, nm in z:
        if out and lo <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], hi), out[-1][2] + " + " + nm)
        else:
            out.append((lo, hi, nm))
    return [{"lo": round(a, 2), "hi": round(b, 2), "name": n} for a, b, n in out]


def _stock_universe():
    try:
        return set((json.load(open(os.path.join(OFD, "of", "live.json"))).get("stocks") or {}).keys())
    except Exception:
        return set()


def _stock_levels(day):
    import csv
    last, uni = {}, _stock_universe()
    try:
        with open(EOD) as f:
            for r in csv.DictReader(f):
                if r["date"] < day and (not uni or r["symbol"] in uni):
                    last[r["symbol"]] = r
    except Exception:
        pass
    out = {}
    for s, r in last.items():
        z = []
        for k, nm in (("high", "yesterday high"), ("low", "yesterday low"), ("close", "yesterday close")):
            p = _f(r.get(k))
            if p:
                b = p * BAND["stock"]
                z.append((p - b, p + b, nm))
        out[s] = {"zones": _merge(z), "from": r["date"]}
    return out


def _index_levels(hot, views):
    out = {}
    for k in INDEX:
        v = views.get(k) or {}
        spot, fut = _f(v.get("spot")), _f(v.get("fut"))
        basis = (fut - spot) if (spot and fut) else 0.0
        z = []
        try:
            for b in hot("levelmap").get(k, 10).get("blocks") or []:
                if b.get("strength") in ("medium", "strong"):
                    z.append((b["lo"] + basis, b["hi"] + basis, f"{b['strength']} block {b['lo']:.0f}-{b['hi']:.0f}"))
        except Exception as e:
            S.setdefault("err", {})["levelmap"] = str(e)[:100]
        px = fut or spot or 0
        bd = px * BAND["index"]
        for side, nm in (("call", "call wall"), ("put", "put wall")):
            w = _f((v.get("walls") or {}).get(side))
            if w:
                z.append((w + basis - bd, w + basis + bd, f"{nm} {w:.0f}"))
        out[k] = {"zones": _merge(z), "basis": round(basis, 1)}
    return out


def levels(hot=None, views=None):
    """Today's fixed levels (computed once, then read from disk)."""
    day = S["day"]
    p = os.path.join(OUT, f"levels_{day}.json")
    if S.get("lv") is not None:
        return S["lv"]
    if os.path.exists(p):
        S["lv"] = json.load(open(p))
        return S["lv"]
    if hot is None or not views or not all((views.get(k) or {}).get("fut") for k in INDEX):
        return None                                        # wait until the tracker has spot + futures for both indices
    lv = {**_stock_levels(day), **_index_levels(hot, views)}
    os.makedirs(OUT, exist_ok=True)
    json.dump({"at": datetime.now().isoformat(timespec="seconds"), **lv}, open(p, "w"), indent=1)
    S["lv"] = json.load(open(p))
    return S["lv"]


def _lv_ts():
    try:
        return int(datetime.fromisoformat(str((S.get("lv") or {}).get("at"))).timestamp())
    except Exception:
        return 0


# ------------------------------------------------------------------ data (incremental file tails)
def _tail(path):
    off = S["off"].get(path, 0)
    try:
        sz = os.path.getsize(path)
    except OSError:
        return []
    if sz < off:
        off = 0
    with open(path, "rb") as f:
        f.seek(off)
        raw = f.read()
    cut = raw.rfind(b"\n")
    if cut < 0:
        return []
    S["off"][path] = off + cut + 1
    lines = raw[:cut].decode(errors="ignore").split("\n")
    return [l for l in lines if l]


def _rows():
    """New rows as (key, t, mid, imb20, buy, sell, bid_qty20, ask_qty20), oldest first."""
    day = S["day"]
    out = []
    for k in INDEX:
        p = os.path.join(OFD, "of", day, f"sec_{k}.csv")
        for l in _tail(p):
            c = l.split(",")
            if c[0] == "t" or len(c) < 15:
                continue
            mid = _f(c[4])
            if not mid:
                continue
            out.append((k, int(c[0]), mid, _f(c[11]) or 0.0, _f(c[6]) or 0.0, _f(c[7]) or 0.0, _f(c[13]) or 0.0, _f(c[14]) or 0.0))
    p = os.path.join(OFD, "stocks_depth", f"{day}.csv")
    for l in _tail(p):
        c = l.split(",")
        if c[0] == "time" or len(c) < 7:
            continue
        b, a = _f(c[2]), _f(c[3])
        if not b or not a or b <= 0 or a <= b * 0.999 or (a - b) / b > 0.005:
            continue                                   # empty / crossed / very wide book (pre-open, auction, bad tick)
        try:
            t = int(datetime.fromisoformat(c[0]).timestamp())
        except ValueError:
            continue
        out.append((c[1], t, (b + a) / 2, _f(c[6]) or 0.0, 0.0, 0.0, _f(c[4]) or 0.0, _f(c[5]) or 0.0))
    out.sort(key=lambda r: r[1])
    return out


# ------------------------------------------------------------------ engine
def _cost_r(k, entry, risk):
    c = SYN_PTS[k] if k in INDEX else entry * COST_STOCK          # index: synthetic future (main) since 4 Nov
    return c / risk if risk else 0.0


def _index_money(tr, g):
    """Index trade in rupees, 1 lot: synthetic future (main) and the 0.6-delta option (comparison)."""
    k = tr["key"]; pts = g * tr["risk"]
    tr.update(vehicle="synthetic future (ATM call + short ATM put), 1 lot", lot=LOT[k], pts_gross=round(pts, 1),
              pts_net=round(pts - SYN_PTS[k], 1), cost_r=round(SYN_PTS[k] / tr["risk"], 3),
              r=round(g - SYN_PTS[k] / tr["risk"], 2), pnl=round((pts - SYN_PTS[k]) * LOT[k]), r_synth=round(g - SYN_PTS[k] / tr["risk"], 2),
              r_opt=round(g - COST_PTS[k] / tr["risk"], 2), pnl_opt=round(0.6 * LOT[k] * (pts - COST_PTS[k])))


def _close(tr, px, t, why):
    sd = 1 if tr["side"] == "long" else -1
    g = (px - tr["entry"]) * sd / tr["risk"]
    tr.update(status="closed", exit=round(px, 2), closed=f"{S['day']} {_hm(t)}", exit_reason=why,
              r_gross=round(g, 2), r=round(g - tr["cost_r"], 2), pnl=round((g - tr["cost_r"]) * RISK_RS))
    if tr["key"] in INDEX and tr.get("risk"):
        _index_money(tr, g)


def _open(k, setup, side, entry, stop, zone, zones, t, why):
    sd = 1 if side == "long" else -1
    risk = abs(entry - stop)
    if risk <= 0:
        return
    nxt = sorted([z["lo"] if sd > 0 else z["hi"] for z in zones if (z["lo"] > entry if sd > 0 else z["hi"] < entry)], key=lambda p: abs(p - entry))
    tgt = next((p for p in nxt if abs(p - entry) >= P["rr"] * risk), None)
    tnote = "next level" if tgt is not None else f"{P['rr']:.0f}R (no level far enough)"
    if tgt is None:
        tgt = entry + sd * P["rr"] * risk
    today = [x for x in S["trades"] if x["day"] == S["day"]]
    closed_r = sum(x["r"] for x in today if x["status"] == "closed" and x.get("plan"))
    plan = sum(1 for x in today if x.get("plan")) < P["plan_n"] and closed_r > P["plan_stop"]
    S["trades"].append({"id": f"{k}-{S['day']}-{_hm(t)}-{setup}", "day": S["day"], "key": k, "setup": setup, "side": side,
                        "opened": f"{S['day']} {_hm(t)}", "t0": t, "entry": round(entry, 2), "stop": round(stop, 2), "target": round(tgt, 2),
                        "target_note": tnote, "risk": risk, "cost_r": round(_cost_r(k, entry, risk), 3), "level": zone["name"],
                        "zone": [zone["lo"], zone["hi"]], "why": why, "plan": plan, "status": "open",
                        "kind": "futures" if k in INDEX else "stock",
                        # 2026-11-03 audit (look-ahead): index levels (blocks, OI walls, basis) are fixed when first read. If that
                        # happened after this trade's time (a restart mid-day), the trade used levels it could not have known.
                        "late_levels": bool(k in INDEX and t < S.get("lv_ts", 0))})
    S["changed"] = True


def _flat_all(t):
    """2026-11-03 audit: at 15:20 close every open trade at its market's last good price. A stock whose book goes empty or
    very wide near the close (pre-close auction rows are skipped) used to stay open overnight."""
    for x in S["trades"]:
        if x["status"] == "open" and x["day"] == S["day"]:
            h = (S["inst"].get(x["key"]) or {}).get("h")
            if h:
                _close(x, h[-1][1], t, "15:20 flat")
                S["changed"] = True


def _step(k, t, mid, imb, buy, sell, bq, aq):
    I = S["inst"].setdefault(k, {"h": deque(), "lv": {}, "or": [None, None], "n": 0, "ih": deque(), "base": None, "base_t": 0})
    h = I["h"]
    h.append((t, mid, imb, buy, sell, bq, aq))
    while h and h[0][0] < t - 300:
        h.popleft()
    ih = I["ih"]                                   # the book's own normal lean (median of the last 30 min): Nifty's book is
    ih.append((t, imb))                            # usually ask-heavy (median -0.22), so the lean is measured against it
    while ih and ih[0][0] < t - 1800:
        ih.popleft()
    if t - I["base_t"] >= 60 and len(ih) >= 20:
        v = sorted(x[1] for x in ih)
        I["base"], I["base_t"] = v[len(v) // 2], t
    hm = _hm(t)
    if hm >= P["flat"] and not S.get("flat_done"):
        S["flat_done"] = True
        _flat_all(t)
    # opening range (stocks 09:15-09:30) / initial balance (index 09:15-10:15) -> extra levels once complete
    orr_end = "10:15" if k in INDEX else "09:30"
    if "09:15" <= hm < orr_end:
        I["or"] = [max(I["or"][0] or mid, mid), min(I["or"][1] or mid, mid)]
    lvk = (S["lv"] or {}).get(k) or {}
    zones = list(lvk.get("zones") or [])
    if hm >= orr_end and I["or"][0] and not I.get("or_done"):
        bd = mid * BAND["index" if k in INDEX else "stock"]
        nm = "IB" if k in INDEX else "opening range"
        I["extra"] = _merge([(I["or"][0] - bd, I["or"][0] + bd, f"{nm} high"), (I["or"][1] - bd, I["or"][1] + bd, f"{nm} low")])
        I["or_done"] = True
    zones = sorted(zones + I.get("extra", []), key=lambda z: z["lo"])
    # manage the open trade
    op = next((x for x in S["trades"] if x["key"] == k and x["status"] == "open"), None)
    if op:
        sd = 1 if op["side"] == "long" else -1
        if (mid - op["stop"]) * sd <= 0:
            _close(op, mid, t, "stop")                       # filled at the price seen (worse than the stop on a gap)
        elif (mid - op["target"]) * sd >= 0:
            _close(op, op["target"], t, "target")
        elif t - op["t0"] >= P["max_min"] * 60 or hm >= P["flat"]:
            _close(op, mid, t, "time" if hm < P["flat"] else "15:20 flat")
        if op["status"] == "closed":
            S["changed"] = True
        return
    can = P["first"] <= hm < P["last"] and I["n"] < P["per_day"]
    b = mid * BAND["index" if k in INDEX else "stock"]
    can = can and I["base"] is not None
    if can:
        w30 = [x[2] for x in h if x[0] >= t - 30]
        imb30 = (sum(w30) / len(w30) if w30 else 0.0) - I["base"]          # lean vs this book's normal
    for z in zones:
        zid = f"{z['lo']}-{z['hi']}"
        st = I["lv"].setdefault(zid, {"side": None, "since": t, "touch": None, "fired": set(), "out": None, "was": None})
        lo, hi = z["lo"], z["hi"]
        pos = "above" if mid > hi else "below" if mid < lo else "in"
        if pos != st["side"]:
            prev = st["side"]
            if pos == "in" and prev in ("above", "below") and t - st["since"] >= P["away_s"]:
                st["touch"] = {"from": prev, "t": t, "buy": 0.0, "sell": 0.0, "bq": bq, "aq": aq, "ext": mid}
            if pos in ("above", "below") and prev is not None:
                st["out"] = {"side": pos, "t": t, "from": st["was"]}
            if pos in ("above", "below"):
                st["was"] = pos
            st["side"], st["since"] = pos, t
        tc = st["touch"]
        if tc:
            tc["buy"] += buy; tc["sell"] += sell
            tc["ext"] = min(tc["ext"], mid) if tc["from"] == "above" else max(tc["ext"], mid)
            if t - tc["t"] > P["hold_s"]:
                st["touch"] = None
        if not can:
            continue
        # HOLD: came in from above (support) / below (resistance), leaves on the same side, did not go through
        if tc and pos == tc["from"]:
            long_ = tc["from"] == "above"
            sd = 1 if long_ else -1
            far = lo if long_ else hi
            held = (tc["ext"] - (far - sd * 2 * b)) * sd > 0
            flow = (imb30 * sd >= P["imb_hold"])
            if k in INDEX:
                absorbed = (tc["sell"] >= tc["buy"]) if long_ else (tc["buy"] >= tc["sell"])
                ab_txt = f"market sells {tc['sell']:.0f} vs buys {tc['buy']:.0f} in the zone"
            else:
                absorbed = (bq >= tc["bq"]) if long_ else (aq >= tc["aq"])
                ab_txt = f"{'bid' if long_ else 'ask'} qty {tc['bq' if long_ else 'aq']:.0f} -> {bq if long_ else aq:.0f}"
            key = ("hold", "long" if long_ else "short")
            if held and flow and absorbed and key not in st["fired"]:
                st["fired"].add(key); I["n"] += 1; st["touch"] = None
                _open(k, "hold", key[1], mid, far - sd * 2 * b, z, zones, t,
                      f"held {z['name']} · book lean {imb30:+.2f} vs normal · {ab_txt}")
                return
        # BREAK: out on the far side, beyond for 30 s
        o = st["out"]
        if o and o.get("from") and o["from"] != o["side"] and pos == o["side"] and P["break_s"] <= t - o["t"] < P["break_s"] + 20:
            long_ = pos == "above"
            sd = 1 if long_ else -1
            edge = hi if long_ else lo
            stay = all((x[1] - edge) * sd > 0 for x in h if x[0] >= o["t"])
            w60 = [x for x in h if x[0] >= t - 60]
            vol = sum(x[3] + x[4] for x in w60); dl = sum(x[3] - x[4] for x in w60)
            if k in INDEX:
                push = vol > 0 and dl / vol * sd >= P["delta_break"]
                p_txt = f"delta {dl / vol * 100 if vol else 0:+.0f}% of volume (60 s)"
            else:
                old = [x for x in h if t - 150 <= x[0] <= t - 90]
                if old:
                    q0 = old[0][6] if long_ else old[0][5]
                    q1 = aq if long_ else bq
                    push = q0 > 0 and (q0 - q1) / q0 >= P["drop"]
                    p_txt = f"{'ask' if long_ else 'bid'} qty {q0:.0f} -> {q1:.0f}"
                else:
                    push, p_txt = False, ""
            key = ("break", "long" if long_ else "short")
            if stay and push and imb30 * sd >= P["imb_break"] and key not in st["fired"]:
                st["fired"].add(key); I["n"] += 1
                _open(k, "break", key[1], mid, (lo if long_ else hi) - sd * b, z, zones, t,
                      f"broke {z['name']} · book lean {imb30:+.2f} vs normal · {p_txt}")
                return


# ------------------------------------------------------------------ store / tick / api
def _load():
    try:
        return json.load(open(TRADES))
    except Exception:
        return []


def _save():
    os.makedirs(OUT, exist_ok=True)
    tmp = TRADES + ".tmp"
    json.dump(S["trades"], open(tmp, "w"), indent=1, default=str)
    os.replace(tmp, TRADES)
    S["changed"] = False


def _new_day(day):
    S.clear()
    S.update(day=day, off={}, inst={}, lv=None, changed=False, err={})
    # today is rebuilt from the day's files (same result after a restart); earlier days come from the store
    S["trades"] = [x for x in _load() if x["day"] != day]


def tick(hot=None, views=None):
    """Called from the Live Desk loop (every ~2 s). Weekdays 09:15-15:35 only."""
    now = datetime.now()
    if now.weekday() >= 5 or not ("09:15" <= now.strftime("%H:%M") <= "15:35"):
        return
    day = now.strftime("%Y-%m-%d")
    if S.get("day") != day or S.get("ver") != 3:
        _new_day(day)
        S["ver"] = 3                                   # v2 (3 Nov): skip empty / wide stock books -> today is rebuilt once
    if levels(hot, views) is None:
        return
    S["lv_ts"] = S.get("lv_ts") or _lv_ts()
    t0 = time.time()
    for r in _rows():
        _step(*r)
    S["took_s"] = round(time.time() - t0, 3)
    S["at"] = now.isoformat(timespec="seconds")
    if S.get("changed"):
        _save()


def _rec(rows):
    n = len(rows)
    if not n:
        return {"n": 0}
    return {"n": n, "win_pct": round(100 * sum(x["r"] > 0 for x in rows) / n), "avg_r_gross": round(sum(x["r_gross"] for x in rows) / n, 2),
            "avg_r": round(sum(x["r"] for x in rows) / n, 2), "total_r": round(sum(x["r"] for x in rows), 2), "rs": round(sum(x["pnl"] for x in rows))}


def trades():
    """Live list while the engine runs today; otherwise the saved record (the file is the source of truth)."""
    now = datetime.now()
    live = S.get("day") == now.strftime("%Y-%m-%d") and now.weekday() < 5 and "09:15" <= now.strftime("%H:%M") <= "15:35"
    return S.get("trades") if live else _load()


def api() -> dict:
    T = trades() or []
    cl = [x for x in T if x["status"] == "closed" and not x.get("late_levels")]     # look-ahead trades kept but not counted
    day = datetime.now().strftime("%Y-%m-%d")
    days = sorted({x["day"] for x in T})
    by = {}
    for x in cl:
        by.setdefault(f"{x['setup']} · {'index' if x['key'] in INDEX else 'stocks'}", []).append(x)
    lv = S.get("lv") or {}
    return {"open": [x for x in T if x["status"] == "open"], "today": [x for x in T if x["day"] == day][::-1],
            "record": {"all": _rec(cl), "plan": _rec([x for x in cl if x.get("plan")]), "by": {k: _rec(v) for k, v in sorted(by.items())},
                       "sessions": len(days), "since": days[0] if days else None,
                       "index_vehicle": {"option_0.6d": _rec([{**x, "r": x["r_opt"], "pnl": x["pnl_opt"]} for x in cl if x["key"] in INDEX and x.get("r_opt") is not None]),
                                         "synthetic_future": _rec([x for x in cl if x["key"] in INDEX]),
                                         "note": "Index trades, 1 lot, in rupees. Main = synthetic future (buy ATM call + sell ATM put). Comparison = 0.6-delta next-week option. Costs are estimates."}},
            "levels": {k: len((v or {}).get("zones") or []) for k, v in lv.items() if isinstance(v, dict) and k in (S.get("inst") or {})},
            "at": S.get("at"), "took_s": S.get("took_s"), "err": S.get("err"),
            "rules": "HOLD = into a level from away, leaves the same side within 2 min, book leaning our way, aggressive side absorbed. "
                     "BREAK = out the far side and beyond for 30 s, book leaning the break way, index delta >= 15% / stock wall pulled 30%. "
                     "Target next level (>= 2R), stop beyond the level, out after 60 min or 15:20, entries 09:30-15:00. "
                     "Index = synthetic future (buy ATM call + sell ATM put, next-week expiry), 1 lot (Nifty 65, Bank Nifty 30), P&L in Rs after about 4.5 pts (Nifty) / 12.5 pts (Bank Nifty) round-trip cost; 0.6-delta option shown for comparison. Stocks: Rs 1,000 risk a trade, 0.08% cost.",
            "label": LABEL, "edge": EDGE, "late_levels_n": sum(1 for x in T if x.get("late_levels")),
            "note": "Paper only - never orders. Index trades opened before that day's levels were first read (restart mid-day) are kept but not counted (look-ahead). Judge after 30-40 sessions: positive after costs in both halves, 30+ trades."}
