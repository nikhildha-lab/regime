"""
ORDER-FLOW PATTERNS: identify, show, and auto paper-trade (2026-11-11, Nikhil: "enrich order flow, identify pattern, and
auto deploy trades ... plan first" -> plan claude/orderflow-patterns-plan-2026-11-11.md, then "go").

One engine for the chart marks, the Patterns card, the paper trades and later the backtests, so what is tested is what trades.
Everything runs on CLOSED bars only (no look-ahead); a trade enters at a bar's close, never inside a bar.

Markets and bars (read-only, no new feed calls):
  * NIFTY / BANKNIFTY futures - the Dhan order-flow engine's 1-min footprint (~/Documents/dhan/orderflow/data/of/<day>/footprint_<SYM>.jsonl,
    trades classified against the 20-level book), merged into 5-min bars for patterns; 1-min bars for the opening-range model.
  * BTC / ETH / SOL, GOLD - the stored real 15-min footprint bars (data_store/livedesk/footprint/<desk>/<key>/<UTC day>.json, Hyperliquid
    trades with the exchange's aggressor side). Only 15-min footprint exists for these, so they run on 15-min bars.

Patterns (feed + chart): stacked imbalances, absorption (heavy delta against, no progress), exhaustion, finished / unfinished auction,
trapped traders, delta divergence (with an "at a value edge" flag). Iceberg needs the book and is not built yet.
Context: VWAP, session profile (VPOC / VAH / VAL / HVN / LVN), yesterday's value, naked VPOCs, unfinished extremes, opening range, day type.

Entry models (paper trades, RECORD ONLY until each passes MASTER_BACKTESTER): C1 VWAP break-retest-absorb, C2 LVN rejection,
C3 failed auction, C4 opening range 6-min (India only - needs 1-min bars), C5 stacked-imbalance zone defend, C6 divergence at the value edge.
Every model spells out stop, target, time stop and size. Limits: one open trade per market across the models, at most 3 a market a day,
stop for the day at -2R per market; India entries 09:21-15:00, flat at 15:20; NSE holidays are simply days with no bars.
Sizes: crypto / gold 100 USDT x 5 = 500 USDT notional, 0.14 % round trip; Nifty / Bank Nifty 2 lots synthetic future, 18 / 45 pts round trip.

PAPER ONLY. NOTHING HERE PLACES AN ORDER. Not financial advice.
"""
from __future__ import annotations

import json
import math
import os
import time
from datetime import datetime, timedelta, timezone

HOME = os.path.expanduser("~")
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
OF_DIR = os.path.join(HOME, "Documents", "dhan", "orderflow", "data", "of")
FP_DIR = os.path.join(ROOT, "data_store", "livedesk", "footprint")
OUT = os.path.join(ROOT, "data_store", "livedesk", "ofpatterns")
IST = timezone(timedelta(hours=5, minutes=30))

MARKETS = {   # key -> (desk, source, pattern timeframes in minutes). 2026-11-11 (Nikhil: "15M + 30M"): each timeframe is its own
    # pattern feed and its own paper book (rule id = model + "_" + minutes, e.g. ofp_stack_15). India keeps 5M too.
    "NIFTY": ("nifty", "of1m", (5, 15, 30)), "BANKNIFTY": ("nifty", "of1m", (5, 15, 30)),
    "BTC": ("crypto", "fp15", (15, 30)), "ETH": ("crypto", "fp15", (15, 30)), "SOL": ("crypto", "fp15", (15, 30)), "GOLD": ("metals", "fp15", (15, 30))}
# 4H is a filter, not a pattern feed: a model trades only with the 4H trend (Regime Board 4H score >= +2 = up, <= -2 = down,
# between = both sides allowed). The score is snapshotted every pass into h4.json, and a trade uses the snapshot taken at or
# before its entry, so a later 4H change never rewrites an earlier trade.
H4_ROW = {"NIFTY": "Nifty 50", "BANKNIFTY": "Bank Nifty", "BTC": "crypto|BTC", "ETH": "crypto|ETH", "SOL": "crypto|SOL", "GOLD": "metals|Gold"}
H4_EDGE = 2.0
RB_LATEST = os.path.join(ROOT, "data_store", "livedesk", "regimeboard", "latest.json")
TIME_BARS = {   # time stop in bars per model and timeframe (about the same clock time on every timeframe, floor 4 bars)
    "base": {5: 12, 15: 8, 30: 6}, "lvn": {5: 18, 15: 12, 30: 8}, "div": {5: 24, 15: 16, 30: 10}}
NAME = {"NIFTY": "Nifty fut", "BANKNIFTY": "Bank Nifty fut", "BTC": "BTC", "ETH": "ETH", "SOL": "SOL", "GOLD": "Gold"}
COST_PTS = {"NIFTY": 18.0, "BANKNIFTY": 45.0}          # round trip, futures (real-orderflow-test.md)
LOTS_FALLBACK = {"NIFTY": 65, "BANKNIFTY": 30}         # only if the Dhan scrip master has no lot size
LOTS_N = 2                                             # Indian lots: always even, minimum 2
CRYPTO_NOTIONAL, CRYPTO_COST = 500.0, 0.0014           # 100 USDT x 5; 0.14 % round trip

MODELS = {
    "ofp_vwap":  "VWAP Break · Retest · Absorb",
    "ofp_lvn":   "LVN Rejection",
    "ofp_fail":  "Failed Auction",
    "ofp_or":    "Opening Range 6-Min",
    "ofp_stack": "Stacked Imbalance Zone Defend",
    "ofp_div":   "Delta Divergence at Value Edge",
}
PATTERNS = {"stack": "Stacked Imbalances", "absorb": "Absorption", "exhaust": "Exhaustion", "unfinished": "Unfinished Auction",
            "finished": "Finished Auction", "trapped": "Trapped Traders", "div": "Delta Divergence"}
IMB_RATIO = 3.0
MAX_TRADES_DAY, DAY_STOP_R = 3, -2.0
S: dict = globals().get("S") or {"t": 0.0, "last": None, "err": None}


# ============================================================ bars
def _rows(levels_or_bins, fmt):
    out = {}
    for x in levels_or_bins or []:
        try:
            if fmt == "lv":                    # engine writes [price, sell, buy]
                p, s_, b = float(x[0]), float(x[1] or 0), float(x[2] or 0)
            else:
                p, b, s_ = float(x["p"]), float(x.get("b") or 0), float(x.get("s") or 0)
        except Exception:
            continue
        o = out.setdefault(p, [0.0, 0.0]); o[0] += b; o[1] += s_
    return out


def _mk(d, hm, o, h, l, c, rows, step):
    buy = sum(v[0] for v in rows.values()); sell = sum(v[1] for v in rows.values())
    return {"d": d, "t": hm, "o": o, "h": h, "l": l, "c": c, "buy": buy, "sell": sell, "delta": buy - sell, "vol": buy + sell,
            "rows": sorted((p, v[0], v[1]) for p, v in rows.items()), "step": step}


def _step_of(prices):
    ps = sorted(set(prices)); ds = [round(b - a, 6) for a, b in zip(ps, ps[1:]) if b > a]
    return min(ds) if ds else 1.0


def india_1m(sym, days=8, of_dir=None):
    """Closed 1-min bars, 09:15-15:29 IST, last `days` sessions with data."""
    of_dir = of_dir or OF_DIR
    try:
        dates = sorted(d for d in os.listdir(of_dir) if len(d) == 10 and d[4] == "-")[-days:]
    except Exception:
        return []
    out = []
    for d in dates:
        f = os.path.join(of_dir, d, f"footprint_{sym}.jsonl")
        try:
            lines = open(f).read().splitlines()
        except Exception:
            continue
        for ln in lines:
            try:
                b = json.loads(ln)
            except Exception:
                continue
            hm = str(b.get("time") or "")
            if b.get("o") is None or not ("09:15" <= hm < "15:30") or not (b.get("buy") or b.get("sell")):
                continue
            rows = _rows(b.get("levels"), "lv")
            if not rows:
                continue
            out.append(_mk(d, hm, b["o"], b["h"], b["l"], b["c"], rows, 0))
    st = _step_of([p for b in out[-400:] for p, _, _ in b["rows"]]) if out else 1.0
    for b in out:
        b["step"] = st
    return out


def merge(bars, tf, start_min=0, base=1):
    """Merge closed 1-min bars into tf-minute bars (India buckets start 09:15). A bucket is kept only when complete or followed by later data."""
    groups = {}
    for b in bars:
        hh, mm = map(int, b["t"].split(":")); m = hh * 60 + mm
        k = (b["d"], (m - start_min) // tf)
        groups.setdefault(k, []).append(b)
    out = []
    keys = sorted(groups)
    for i, k in enumerate(keys):
        g = groups[k]
        last_bucket = i == len(keys) - 1
        if last_bucket and len(g) < tf // base:
            continue                                   # still forming
        rows = {}
        for b in g:
            for p, bb, ss in b["rows"]:
                o = rows.setdefault(p, [0.0, 0.0]); o[0] += bb; o[1] += ss
        m0 = start_min + k[1] * tf
        if start_min and m0 + tf > 15 * 60 + 30:
            continue                                   # India: a bucket running past the 15:30 close is not a full bar
        out.append(_mk(k[0], f"{m0 // 60:02d}:{m0 % 60:02d}", g[0]["o"], max(x["h"] for x in g), min(x["l"] for x in g), g[-1]["c"], rows, g[0]["step"]))
    return out


def stored_15(desk, key, days=6, fp_dir=None):
    """Stored real 15-min footprint bars (UTC days) - completed bars only (the writer drops the forming bar)."""
    folder = os.path.join(fp_dir or FP_DIR, desk, key)
    try:
        files = sorted(f for f in os.listdir(folder) if f.endswith(".json"))[-days:]
    except Exception:
        return []
    out = []
    for f in files:
        try:
            day = json.load(open(os.path.join(folder, f))) or {}
        except Exception:
            continue
        for t in sorted(day):
            b = day[t]
            if b.get("approx") or b.get("o") is None:
                continue
            rows = _rows(b.get("bins"), "bins")
            if rows:
                out.append(_mk(b.get("d") or f[:-5], t, b["o"], b["h"], b["l"], b["c"], rows, 0))
    st = _step_of([p for b in out[-300:] for p, _, _ in b["rows"]]) if out else 1.0
    for b in out:
        b["step"] = st
    return out


# ============================================================ helpers
def _med(xs):
    xs = sorted(x for x in xs if x is not None)
    return xs[len(xs) // 2] if xs else 0.0


def _pct(xs, q):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(q * len(xs)))] if xs else 0.0


def diag_imb(rows, ratio=IMB_RATIO):
    """Diagonal imbalance (the card's read): buy at p vs sell one row below; sell at p vs buy one row above. Rows ascending."""
    if not rows:
        return set(), set()
    mv = max(1e-9, 0.3 * _med([b + s for _, b, s in rows]))
    bi, si = set(), set()
    for i, (p, b, s) in enumerate(rows):
        below = rows[i - 1][2] if i > 0 else 0.0
        above = rows[i + 1][1] if i + 1 < len(rows) else 0.0
        if b >= mv and b >= ratio * max(below, mv * 0.34):
            bi.add(i)
        if s >= mv and s >= ratio * max(above, mv * 0.34):
            si.add(i)
    return bi, si


def stacks(idx_set, n=3):
    """Runs of >= n consecutive row indexes."""
    out, run = [], []
    for i in sorted(idx_set):
        if run and i == run[-1] + 1:
            run.append(i)
        else:
            if len(run) >= n:
                out.append(run)
            run = [i]
    if len(run) >= n:
        out.append(run)
    return out


def profile(bars):
    vol = {}
    for b in bars:
        for p, bb, ss in b["rows"]:
            vol[p] = vol.get(p, 0.0) + bb + ss
    if not vol:
        return None
    ps = sorted(vol); tot = sum(vol.values())
    poc_i = max(range(len(ps)), key=lambda i: vol[ps[i]])
    lo = hi = poc_i; acc = vol[ps[poc_i]]
    while acc < 0.7 * tot and (lo > 0 or hi < len(ps) - 1):
        up = vol[ps[hi + 1]] if hi < len(ps) - 1 else -1
        dn = vol[ps[lo - 1]] if lo > 0 else -1
        if up >= dn:
            hi += 1; acc += up
        else:
            lo -= 1; acc += dn
    # HVN / LVN on a 3-row smoothed profile; an LVN must sit between two HVNs and be <= 40 % of the smaller one
    sm = [(vol[ps[max(0, i - 1)]] + vol[ps[i]] + vol[ps[min(len(ps) - 1, i + 1)]]) / 3 for i in range(len(ps))]
    pk = [i for i in range(1, len(ps) - 1) if sm[i] >= sm[i - 1] and sm[i] > sm[i + 1] and sm[i] >= 0.5 * max(sm)]
    lvn = []
    for a, b in zip(pk, pk[1:]):
        j = min(range(a, b + 1), key=lambda i: sm[i])
        if sm[j] <= 0.4 * min(sm[a], sm[b]):
            lvn.append({"p": ps[j], "hvn_lo": ps[a], "hvn_hi": ps[b]})
    return {"poc": ps[poc_i], "val": ps[lo], "vah": ps[hi], "hvn": [ps[i] for i in pk], "lvn": lvn, "vol": tot}


def vwap_series(bars):
    pv = vv = 0.0; out = []
    for b in bars:
        for p, bb, ss in b["rows"]:
            pv += p * (bb + ss); vv += bb + ss
        out.append(pv / vv if vv else b["c"])
    return out


def sessions(bars):
    s = {}
    for b in bars:
        s.setdefault(b["d"], []).append(b)
    return [s[d] for d in sorted(s)]


# ============================================================ patterns on closed bars
def detect(bars, ctx, mkt, tf):
    """Pattern events for one session's bars (bars = today's closed bars; ctx = context incl. history medians)."""
    ev = []
    hist = ctx["hist"]
    for i, b in enumerate(bars):
        rows = b["rows"]
        if len(rows) < 3:
            continue
        look = (hist + bars[:i])[-20:]
        md = _med([abs(x["delta"]) for x in look]) or 1.0
        mv = _med([x["vol"] for x in look]) or 1.0
        mrow = _med([bb + ss for _, bb, ss in rows]) or 1.0
        bi, si = diag_imb(rows)
        hit = lambda k, side, price, note, extra=None: ev.append({"mkt": mkt, "tf": tf, "d": b["d"], "t": b["t"], "pattern": k, "side": side,
                                                              "price": price, "note": note, "ev": dict({"delta": round(b["delta"]), "vol": round(b["vol"])}, **(extra or {}))})
        for run in stacks(bi):
            hit("stack", "long", rows[run[0]][0], f"{len(run)} stacked buy imbalances {rows[run[0]][0]:g}-{rows[run[-1]][0]:g}", {"rows": len(run), "lo": rows[run[0]][0], "hi": rows[run[-1]][0]})
        for run in stacks(si):
            hit("stack", "short", rows[run[-1]][0], f"{len(run)} stacked sell imbalances {rows[run[0]][0]:g}-{rows[run[-1]][0]:g}", {"rows": len(run), "lo": rows[run[0]][0], "hi": rows[run[-1]][0]})
        prev3 = (hist + bars[:i])[-3:]
        if len(prev3) == 3:
            lo3, hi3 = min(x["l"] for x in prev3), max(x["h"] for x in prev3)
            rng = max(b["h"] - b["l"], b["step"])
            # absorption: heavy delta against, no progress, close away from the push
            if b["delta"] <= -2 * md and b["vol"] >= 1.5 * mv and b["l"] >= lo3 - b["step"] and (b["c"] - b["l"]) / rng >= 0.5:
                hit("absorb", "long", b["l"], f"selling absorbed: delta {b['delta']:+.0f}, low held near {lo3:g}")
            if b["delta"] >= 2 * md and b["vol"] >= 1.5 * mv and b["h"] <= hi3 + b["step"] and (b["h"] - b["c"]) / rng >= 0.5:
                hit("absorb", "short", b["h"], f"buying absorbed: delta {b['delta']:+.0f}, high held near {hi3:g}")
            # exhaustion at a new extreme
            tv = [r[1] + r[2] for r in rows[-3:]][::-1]; bv = [r[1] + r[2] for r in rows[:3]]
            if b["h"] > hi3 and len(rows) >= 4 and tv[0] < tv[1] < tv[2] and tv[0] <= 0.25 * mrow:
                hit("exhaust", "short", b["h"], "volume thins into the new high")
            if b["l"] < lo3 and len(rows) >= 4 and bv[0] < bv[1] < bv[2] and bv[0] <= 0.25 * mrow:
                hit("exhaust", "long", b["l"], "volume thins into the new low")
        # finished / unfinished auction at the extremes (card: zero on one side = finished; both sides trading = unfinished)
        top, bot = rows[-1], rows[0]
        p3 = (hist + bars[:i])[-3:]
        new_hi = len(p3) == 3 and b["h"] > max(x["h"] for x in p3)
        new_lo = len(p3) == 3 and b["l"] < min(x["l"] for x in p3)
        if len(rows) < 4 or not new_hi:
            pass
        elif min(top[1], top[2]) >= 0.25 * mrow:
            hit("unfinished", "long", top[0], f"unfinished high {top[2]:.0f} x {top[1]:.0f} - a target until revisited", {"level": top[0]})
        elif min(top[1], top[2]) <= max(1.0, 0.02 * (top[1] + top[2])):
            hit("finished", "short", top[0], f"finished high {top[2]:.0f} x {top[1]:.0f}")
        if len(rows) < 4 or not new_lo:
            pass
        elif min(bot[1], bot[2]) >= 0.25 * mrow:
            hit("unfinished", "short", bot[0], f"unfinished low {bot[2]:.0f} x {bot[1]:.0f} - a target until revisited", {"level": bot[0]})
        elif min(bot[1], bot[2]) <= max(1.0, 0.02 * (bot[1] + bot[2])):
            hit("finished", "long", bot[0], f"finished low {bot[2]:.0f} x {bot[1]:.0f}")
        # trapped traders: imbalance near the extreme, then a close beyond all of it (this bar or the next closed bar)
        nxt = bars[i + 1] if i + 1 < len(bars) else None
        t3 = rows[-3:]; b3 = rows[:3]
        if any(k >= len(rows) - 3 for k in bi):
            zl = t3[0][0]
            if b["c"] < zl or (nxt and nxt["c"] < zl):
                hit("trapped", "short", zl, "trapped buyers: buy imbalance at the high, then a close below it")
        if any(k < 3 for k in si):
            zh = b3[-1][0]
            if b["c"] > zh or (nxt and nxt["c"] > zh):
                hit("trapped", "long", zh, "trapped sellers: sell imbalance at the low, then a close above it")
    # delta divergence on swing pivots (2 bars each side), confirmed 2 bars later; flag "at value edge"
    cvd, c = [], 0.0
    for b in bars:
        c += b["delta"]; cvd.append(c)
    pl = [i for i in range(2, len(bars) - 2) if bars[i]["l"] < min(bars[i - 1]["l"], bars[i - 2]["l"], bars[i + 1]["l"], bars[i + 2]["l"])]
    ph = [i for i in range(2, len(bars) - 2) if bars[i]["h"] > max(bars[i - 1]["h"], bars[i - 2]["h"], bars[i + 1]["h"], bars[i + 2]["h"])]
    pv = ctx.get("prior") or {}
    band = 2 * (bars[0]["step"] if bars else 1)
    for a, b2 in zip(pl, pl[1:]):
        if b2 - a <= 24 and bars[b2]["l"] < bars[a]["l"] and cvd[b2] > cvd[a]:
            k = bars[b2 + 2]; edge = pv.get("val") is not None and abs(bars[b2]["l"] - pv["val"]) <= band * 2
            ev.append({"mkt": mkt, "tf": tf, "d": k["d"], "t": k["t"], "pattern": "div", "side": "long", "price": bars[b2]["l"],
                       "note": "lower low, less selling (bull divergence)" + (" · at yesterday's VAL" if edge else ""), "ev": {"edge": edge, "pivot_t": bars[b2]["t"]}})
    for a, b2 in zip(ph, ph[1:]):
        if b2 - a <= 24 and bars[b2]["h"] > bars[a]["h"] and cvd[b2] < cvd[a]:
            k = bars[b2 + 2]; edge = pv.get("vah") is not None and abs(bars[b2]["h"] - pv["vah"]) <= band * 2
            ev.append({"mkt": mkt, "tf": tf, "d": k["d"], "t": k["t"], "pattern": "div", "side": "short", "price": bars[b2]["h"],
                       "note": "higher high, less buying (bear divergence)" + (" · at yesterday's VAH" if edge else ""), "ev": {"edge": edge, "pivot_t": bars[b2]["t"]}})
    return ev


# ============================================================ context
def context(all_bars, mkt):
    S_ = sessions(all_bars)
    if not S_:
        return None
    today = S_[-1]
    prior_bars = S_[-2] if len(S_) > 1 else []
    prior = profile(prior_bars) if prior_bars else None
    # naked VPOCs: earlier sessions' VPOCs no later bar has traded through
    naked = []
    for k in range(max(0, len(S_) - 11), len(S_) - 1):
        pf = profile(S_[k])
        if not pf:
            continue
        later = [b for s in S_[k + 1:] for b in s]
        if not any(b["l"] <= pf["poc"] <= b["h"] for b in later):
            naked.append({"p": pf["poc"], "d": S_[k][0]["d"]})
    hist = [b for s in S_[:-1] for b in s][-60:]
    return {"today": today, "prior": prior, "prior_day": prior_bars[0]["d"] if prior_bars else None, "naked": naked, "hist": hist,
            "prior_bars": prior_bars}


def day_type(today, prior, first_hour_bars):
    if not today or not prior:
        return {"type": "Undecided", "why": "needs yesterday's value area"}
    vw = profile(today)
    after = today[first_hour_bars:]
    out_up = sum(1 for b in after if b["c"] > prior["vah"]); out_dn = sum(1 for b in after if b["c"] < prior["val"])
    n = len(after)
    visits, inside = 0, False
    poc_run = None
    for i, b in enumerate(today):
        pf = profile(today[:i + 1]); p = pf["poc"] if pf else b["c"]
        on = b["l"] <= p <= b["h"]
        if on and not inside and i >= first_hour_bars:
            visits += 1
        inside = on
    last = today[-1]["c"]
    in_value = prior["val"] <= last <= prior["vah"]
    ib = today[:first_hour_bars]
    ib_ext = None
    if len(ib) == first_hour_bars and len(today) > first_hour_bars:
        ibr = max(b["h"] for b in ib) - min(b["l"] for b in ib)
        dr = max(b["h"] for b in today) - min(b["l"] for b in today)
        ib_ext = round(dr / ibr, 2) if ibr > 0 else None
    side = "up" if out_up >= out_dn else "down"
    share = (max(out_up, out_dn) / n) if n else 0
    if n >= 3 and share >= 0.6 and not in_value:
        typ, what = "Trend " + ("Up" if side == "up" else "Down"), "Trade with the move on pullbacks, or stay flat. Never fade it."
    elif in_value and visits >= 3:
        typ, what = "Rotation", "Fade the edges of value back toward VPOC."
    else:
        typ, what = "Undecided", "Wait: not clearly trending or rotating yet."
    return {"type": typ, "what": what, "in_prior_value": in_value, "outside_share": round(share, 2), "outside_side": side,
            "vpoc_visits": visits, "ib_ext": ib_ext, "vpoc": vw["poc"] if vw else None}


# ============================================================ trades
def _lot(key):
    if key in S.get("lots", {}):
        return S["lots"][key]
    S.setdefault("lots", {})[key] = _lot_fetch(key)
    return S["lots"][key]


def _lot_fetch(key):
    try:
        import dhan_live as DL
        for c in DL.futures_contracts():
            if c["underlying"] == key and c.get("lot_size"):
                return int(c["lot_size"])
    except Exception:
        pass
    return LOTS_FALLBACK.get(key, 1)


def _money(key, side, entry, exit_, risk_pts, part=1.0):
    sd = 1 if side == "long" else -1
    if key in COST_PTS:
        cost = COST_PTS[key]; pts = sd * (exit_ - entry)
        lot = _lot(key)
        return {"pnl": round((pts - cost) * lot * LOTS_N * part, 0), "r": round((pts - cost) / risk_pts * part, 3) if risk_pts else None, "ccy": "₹"}
    ret = sd * (exit_ / entry - 1)
    return {"pnl": round((ret - CRYPTO_COST) * CRYPTO_NOTIONAL * part, 2), "r": round((ret * entry - CRYPTO_COST * entry) / risk_pts * part, 3) if risk_pts else None,
            "ccy": "$"}


def simulate(bars, k, side, entry, stop, targets, max_bars, key, flat_at=None, invalid=None):
    """Walk closed bars after index k. Stop first if a bar touches both (conservative). targets = [(price, share)]; after the first
    partial target the stop moves to entry. Returns the trade's exit fields, or the open state."""
    sd = 1 if side == "long" else -1
    risk = abs(entry - stop)
    left, cur_stop, legs = 1.0, stop, []
    tg = list(targets)
    jend = k
    for j in range(k + 1, len(bars)):
        jend = j
        b = bars[j]
        if b["d"] != bars[k]["d"] and key in COST_PTS:
            break
        hit_stop = (b["l"] <= cur_stop) if sd > 0 else (b["h"] >= cur_stop)
        if hit_stop:
            legs.append((cur_stop, left, "stop" if cur_stop == stop else "stop at entry")); left = 0; break
        while tg and ((b["h"] >= tg[0][0]) if sd > 0 else (b["l"] <= tg[0][0])):
            p, share = tg.pop(0); share = min(share, left)
            legs.append((p, share, "target")); left -= share
            if left > 1e-9:
                cur_stop = entry
            if left <= 1e-9:
                break
        if left <= 1e-9:
            break
        if invalid and invalid(j):
            legs.append((b["c"], left, "invalidated")); left = 0; break
        if flat_at and b["t"] >= flat_at:
            legs.append((b["c"], left, f"flat {flat_at}")); left = 0; break
        if j - k >= max_bars:
            legs.append((b["c"], left, f"time stop {max_bars} bars")); left = 0; break
    if left > 1e-9:
        last = bars[-1]["c"]
        return {"status": "open", "now": last, "legs": legs, **{("open_" + kk): v for kk, v in _money(key, side, entry, last, risk).items()}}
    pnl = r = 0.0; ccy = None
    for p, share, why in legs:
        m = _money(key, side, entry, p, risk, share); pnl += m["pnl"]; r += (m["r"] or 0); ccy = m["ccy"]
    px_ = sum(p * share for p, share, _ in legs)
    jx = min(len(bars) - 1, k + 1 + len(legs))
    return {"status": "closed", "closed_t": bars[jend]["t"], "closed_d": bars[jend]["d"], "exit": round(px_, 4), "exit_reason": " + ".join(sorted({w for _, _, w in legs})), "pnl": round(pnl, 2), "r": round(r, 3), "ccy": ccy,
            "legs": legs}


def _targets_ladder(side, entry, risk, levels, min_r=1.5):
    """Nearest structural level beyond entry that is at least min_r away; else 2R."""
    sd = 1 if side == "long" else -1
    cands = sorted(p for p in levels if p is not None and sd * (p - entry) >= min_r * risk)
    if sd < 0:
        cands = sorted(cands, reverse=True)
    if cands:
        return cands[0], "level"
    return entry + sd * 2 * risk, "2R"


def _hm(m):
    return f"{m // 60:02d}:{m % 60:02d}"


def _min(hm):
    hh, mm = map(int, hm.split(":")); return hh * 60 + mm


def h4_at(ctx, key, d, hm, tf):
    """4H score in force when the bar starting d hm (bar time zone) closed."""
    snaps = ctx.get("h4") or []
    if not snaps:
        return None
    end = datetime.strptime(f"{d} {hm}", "%Y-%m-%d %H:%M") + timedelta(minutes=tf)
    stamp = (end if key in COST_PTS else end + timedelta(hours=5, minutes=30)).strftime("%Y-%m-%d %H:%M")
    v = None
    for t, sc in snaps:
        if t <= stamp:
            v = sc
        else:
            break
    return v if v is not None else snaps[0][1]           # before the first snapshot: the first reading


def run_models(key, tf, today, ctx, ev, india_1m_today=None):
    """All six models over today's closed bars. Returns (setups, trades). Deterministic: the same bars give the same trades."""
    step = today[0]["step"] if today else 1.0
    band = 2 * step
    prior = ctx.get("prior") or {}
    vw = vwap_series(today)
    india = key in COST_PTS
    look = ctx["hist"]
    md = lambda i: _med([abs(x["delta"]) for x in (look + today[:i])[-20:]]) or 1.0
    setups, cands = [], []
    lv_all = [prior.get("vah"), prior.get("val"), prior.get("poc")] + [n["p"] for n in ctx.get("naked", [])] + \
             [e["ev"].get("level") for e in ev if e["pattern"] == "unfinished"]
    entry_ok = (lambda b: "09:21" <= _hm(_min(b["t"]) + tf) <= "15:00") if india else (lambda b: True)   # judged on the bar's close
    flat = _hm(15 * 60 + 15 - tf) if india else None        # the bar that closes 15:15 is the last: flat by 15:20
    MB = TIME_BARS["base"][tf]

    def cand(model, i, side, entry, stop, tgts, max_bars, why, invalid=None, m1=None):
        risk = abs(entry - stop)
        h4 = h4_at(ctx, key, today[i]["d"], m1[0] if m1 else today[i]["t"], 1 if m1 else tf)
        if h4 is not None and ((side == "long" and h4 <= -H4_EDGE) or (side == "short" and h4 >= H4_EDGE)):
            setups.append({"model": model, "t": today[i]["t"], "side": side, "state": "skipped", "why": why + f" · against the 4H trend ({h4:+.1f})"})
            return
        if india and 0 < risk < COST_PTS[key]:
            setups.append({"model": model, "t": today[i]["t"], "side": side, "state": "skipped", "why": why + f" · stop {risk:.0f} pts is inside the {COST_PTS[key]:.0f}-pt cost"})
            return
        if not india and 0 < risk < CRYPTO_COST * entry:
            setups.append({"model": model, "t": today[i]["t"], "side": side, "state": "skipped", "why": why + " · stop is inside the 0.14 % cost"})
            return
        if risk <= 0 or not entry_ok(today[i]):
            setups.append({"model": model, "t": today[i]["t"], "side": side, "state": "skipped", "why": why + (" · outside entry hours" if risk > 0 else " · zero risk")})
            return
        c = {"model": model, "i": i, "side": side, "entry": entry, "stop": stop, "targets": tgts, "max_bars": max_bars, "why": why, "invalid": invalid}
        if m1:
            c["m1"] = m1
        cands.append(c)
        return True

    # ---- C1 VWAP break - retest - absorb
    for i in range(1, len(today)):
        b, pb = today[i], today[i - 1]
        for side, sd in (("long", 1), ("short", -1)):
            if not (sd * (pb["c"] - vw[i - 1]) <= 0 < sd * (b["c"] - vw[i])):
                continue
            dz = [abs(x["delta"]) for x in (look + today[:i])[-20:]]
            if not (sd * b["delta"] > 0 and abs(b["delta"]) >= _pct(dz, 0.7)):
                continue
            st = {"model": "ofp_vwap", "t": b["t"], "side": side, "steps": ["Break ✓"], "state": "armed"}
            for j in range(i + 1, min(i + 7, len(today))):
                r = today[j]
                touch = (r["l"] <= vw[j] + band) if sd > 0 else (r["h"] >= vw[j] - band)
                if not touch:
                    continue
                if sd * (r["c"] - vw[j]) <= 0:
                    st.update(state="skipped", why="retest closed through VWAP"); break
                if not (sd * r["delta"] <= -md(j)):
                    st.update(state="skipped", why="no absorption on the retest"); st["steps"].append("Retest ✓"); break
                st["steps"] += ["Retest ✓", "Absorbed ✓"]
                if j + 1 < len(today):
                    k = today[j + 1]
                    if sd * (k["c"] - k["o"]) > 0 and sd * (k["c"] - r["c"]) > 0:
                        st["steps"].append("Entry ✓"); st["state"] = "triggered"
                        stop = (r["l"] - step) if sd > 0 else (r["h"] + step)
                        tp, how = _targets_ladder(side, k["c"], abs(k["c"] - stop), lv_all + [max(x["h"] for x in today[:j + 2]) if sd > 0 else min(x["l"] for x in today[:j + 2])])
                        cand("ofp_vwap", j + 1, side, k["c"], stop, [(tp, 1.0)], MB, f"VWAP break {b['t']}, absorbed retest {r['t']} (delta {r['delta']:+.0f}) · target {how}")
                    else:
                        st.update(state="skipped", why="next bar did not close away from VWAP")
                break
            setups.append(st)

    # ---- C2 LVN rejection (yesterday's LVNs between two HVNs)
    for lv in (prior.get("lvn") or []):
        zlo, zhi = lv["p"] - band, lv["p"] + band
        for i in range(len(today) - 2):
            p, s_, r = today[i], today[i + 1], today[i + 2]
            for side, sd, came in (("short", -1, "below"), ("long", 1, "above")):
                push = (p["o"] < zlo and p["h"] >= zlo) if came == "below" else (p["o"] > zhi and p["l"] <= zhi)
                if not push:
                    continue
                stall = s_["vol"] <= 0.6 * p["vol"] and (abs(s_["delta"]) <= 0.1 * s_["vol"] or (s_["delta"] * p["delta"] < 0))
                bi, si = diag_imb(r["rows"])
                rej = (r["c"] < zlo and r["delta"] < 0 and len(si) >= 2) if came == "below" else (r["c"] > zhi and r["delta"] > 0 and len(bi) >= 2)
                st = {"model": "ofp_lvn", "t": p["t"], "side": side, "steps": ["Push In ✓"] + (["Stall ✓"] if stall else []), "state": "armed", "level": lv["p"]}
                if stall and rej:
                    st["steps"].append("Rejected ✓"); st["state"] = "triggered"
                    stop = (zhi + step) if sd < 0 else (zlo - step)
                    tgt = lv["hvn_lo"] if sd < 0 else lv["hvn_hi"]
                    if sd * (tgt - r["c"]) >= 1.5 * abs(r["c"] - stop):
                        cand("ofp_lvn", i + 2, side, r["c"], stop, [(tgt, 1.0)], TIME_BARS["lvn"][tf], f"LVN {lv['p']:g} rejected from {came} · target HVN {tgt:g}")
                    else:
                        st.update(state="skipped", why="HVN target under 1.5R")
                elif stall:
                    st.update(state="skipped", why="no rejection close")
                else:
                    continue
                setups.append(st)

    # ---- C3 failed auction above yesterday's VAH / below VAL
    if prior:
        for side, sd, edge in (("short", -1, prior["vah"]), ("long", 1, prior["val"])):
            i0 = next((i for i, b in enumerate(today) if (b["h"] > edge if sd < 0 else b["l"] < edge)), None)
            if i0 is None:
                continue
            st = {"model": "ofp_fail", "t": today[i0]["t"], "side": side, "steps": ["Outside Value ✓"], "state": "armed"}
            absorbed = back = None
            for j in range(i0, len(today)):
                b = today[j]
                if absorbed is None and (sd < 0 and b["delta"] >= md(j) and b["c"] <= b["o"] or sd > 0 and b["delta"] <= -md(j) and b["c"] >= b["o"]):
                    absorbed = j; st["steps"].append("No Progress ✓")
                if absorbed is not None and back is None and (b["c"] < edge if sd < 0 else b["c"] > edge):
                    back = j; st["steps"].append("Back In Value ✓"); continue
                if back is not None and j - back <= 6 and (b["h"] >= edge - band if sd < 0 else b["l"] <= edge + band) and (b["c"] < edge if sd < 0 else b["c"] > edge):
                    ext = max(x["h"] for x in today[i0:j + 1]) if sd < 0 else min(x["l"] for x in today[i0:j + 1])
                    stop = ext + step if sd < 0 else ext - step
                    t1, t2 = prior["poc"], (prior["val"] if sd < 0 else prior["vah"])
                    risk = abs(b["c"] - stop)
                    if sd * (t1 - b["c"]) >= 1.0 * risk:
                        st["steps"].append("Retest Failed ✓"); st["state"] = "triggered"
                        inv = (lambda jj, e=edge, s=sd: jj >= 1 and all((today[q]["c"] > e) if s < 0 else (today[q]["c"] < e) for q in (jj - 1, jj)))
                        cand("ofp_fail", j, side, b["c"], stop, [(t1, 0.5), (t2, 0.5)], 999, f"failed auction {'above VAH' if sd < 0 else 'below VAL'} {edge:g} · T1 VPOC {t1:g}, T2 {t2:g}", inv)
                    else:
                        st.update(state="skipped", why="VPOC under 1R away")
                    break
                if back is not None and j - back > 6:
                    st.update(state="skipped", why="no retest within 6 bars"); break
            setups.append(st)

    # ---- C5 stacked imbalance zone defend
    for i in range(len(today) - 2):
        b = today[i]
        bi, si = diag_imb(b["rows"])
        for side, sd, runs in (("long", 1, stacks(bi)), ("short", -1, stacks(si))):
            if not runs or sd * (b["c"] - b["o"]) <= 0:
                continue
            run = max(runs, key=len)
            zlo, zhi = b["rows"][run[0]][0], b["rows"][run[-1]][0]
            st = {"model": "ofp_stack", "t": b["t"], "side": side, "steps": ["Impulse ✓"], "state": "armed", "zone": [zlo, zhi]}
            for j in range(i + 1, min(i + 7, len(today) - 1)):
                r = today[j]
                if (r["c"] < zlo) if sd > 0 else (r["c"] > zhi):
                    st.update(state="skipped", why="zone broken"); break
                into = (r["l"] <= zhi) if sd > 0 else (r["h"] >= zlo)
                if into and sd * r["delta"] < 0:
                    st["steps"].append("Pullback ✓")
                    k = today[j + 1]
                    if sd * (k["c"] - k["o"]) > 0 and sd * (k["c"] - r["c"]) > 0:
                        st["steps"].append("Entry ✓"); st["state"] = "triggered"
                        stop = (zlo - step) if sd > 0 else (zhi + step)
                        ext = b["h"] if sd > 0 else b["l"]
                        risk = abs(k["c"] - stop)
                        tp = ext if sd * (ext - k["c"]) >= 1.5 * risk else k["c"] + sd * 2 * risk
                        cand("ofp_stack", j + 1, side, k["c"], stop, [(tp, 1.0)], MB, f"{len(run)} stacked {'buy' if sd > 0 else 'sell'} imbalances {zlo:g}-{zhi:g} defended")
                    else:
                        st.update(state="skipped", why="next bar did not close with the impulse")
                    break
            setups.append(st)

    # ---- C6 delta divergence at the value edge
    for e in ev:
        if e["pattern"] != "div" or not e["ev"].get("edge"):
            continue
        i = next((q for q, b in enumerate(today) if b["t"] == e["t"]), None)
        if i is None:
            continue
        b = today[i]; sd = 1 if e["side"] == "long" else -1
        st = {"model": "ofp_div", "t": b["t"], "side": e["side"], "steps": ["Divergence At Edge ✓"], "state": "armed"}
        if sd * (b["c"] - b["o"]) > 0 and prior:
            stop = e["price"] - sd * step
            t1, t2 = prior["poc"], (prior["vah"] if sd > 0 else prior["val"])
            risk = abs(b["c"] - stop)
            tg = [(t1, 0.5), (t2, 0.5)] if sd * (t1 - b["c"]) >= risk else [(b["c"] + sd * 2 * risk, 1.0)]
            st["steps"].append("Flip Close ✓"); st["state"] = "triggered"
            cand("ofp_div", i, e["side"], b["c"], stop, tg, TIME_BARS["div"][tf], e["note"])
        else:
            st.update(state="skipped", why="the confirming bar did not close the right way")
        setups.append(st)

    # ---- C4 opening range 6-min (India, 1-min bars)
    if india and india_1m_today and tf == 5:                # the 1-min opening-range model runs once, beside the 5M book
        m1 = india_1m_today
        rng = [b for b in m1 if "09:15" <= b["t"] <= "09:20"]
        prev_d = ctx.get("prior_1m_absdelta") or []
        p90 = _pct(prev_d, 0.9) if prev_d else None
        if len(rng) == 6 and p90:
            rh, rl = max(b["h"] for b in rng), min(b["l"] for b in rng)
            done = set()
            for i in range(6, len(m1) - 1):
                b = m1[i]
                if b["t"] > "10:30":
                    break
                for side, sd, edge in (("long", 1, rh), ("short", -1, rl)):
                    if (side, "x") in done:
                        continue
                    broke = (b["c"] > edge) if sd > 0 else (b["c"] < edge)
                    fake = (b["h"] > edge and b["c"] <= edge) if sd > 0 else (b["l"] < edge and b["c"] >= edge)
                    if fake and sd * b["delta"] >= p90:                     # reversal: aggressive break, no reward
                        k = m1[i + 1]
                        if sd * (k["c"] - k["o"]) < 0:
                            stop = (b["h"] + step) if sd > 0 else (b["l"] - step)
                            tgt = rl if sd > 0 else rh
                            risk = abs(k["c"] - stop)
                            done.add((side, "x"))
                            if abs(tgt - k["c"]) >= risk:
                                cand("ofp_or", _bar_idx_5m(today, k["t"]), "short" if sd > 0 else "long", k["c"], stop, [(tgt, 1.0)], 12,
                                     f"opening range {rl:g}-{rh:g}: aggressive break {b['t']} (delta {b['delta']:+.0f}) with no reward → reversal", None, (k["t"], i + 1))
                    elif broke:
                        for j in range(i + 1, min(i + 16, len(m1) - 1)):
                            r = m1[j]
                            if ((r["l"] <= edge + band) if sd > 0 else (r["h"] >= edge - band)) and sd * (r["c"] - edge) > 0 and sd * r["delta"] < 0:
                                k = m1[j + 1]
                                if sd * (k["c"] - k["o"]) > 0:
                                    stop = (r["l"] - step) if sd > 0 else (r["h"] + step)
                                    risk = abs(k["c"] - stop)
                                    cand("ofp_or", _bar_idx_5m(today, k["t"]), side, k["c"], stop, [(k["c"] + sd * 2 * risk, 1.0)], 12,
                                         f"opening range {rl:g}-{rh:g}: break, retest {r['t']} with selling absorbed, bullish close" if sd > 0 else
                                         f"opening range {rl:g}-{rh:g}: break, retest {r['t']} with buying absorbed, bearish close", None, (k["t"], j + 1))
                                break
                        done.add((side, "x"))
            setups.append({"model": "ofp_or", "t": "09:20", "side": "both", "state": "triggered" if any(c["model"] == "ofp_or" for c in cands) else ("done" if done else "armed"),
                           "steps": [f"Range {rl:g}-{rh:g} ✓"]})

    # ---- risk gate: in time order, one open trade per market, 3 a day, stop at -2R
    trades, open_until, n_day, r_day = [], -1, 0, 0.0
    for c in sorted(cands, key=lambda x: (x.get("m1", (today[x["i"]]["t"],))[0] if x.get("m1") else today[x["i"]]["t"])):
        ti = c["m1"][0] if c.get("m1") else today[c["i"]]["t"]
        if n_day >= MAX_TRADES_DAY or r_day <= DAY_STOP_R:
            continue
        if open_until is None or (open_until != -1 and ti < open_until):
            continue
        if c.get("m1"):
            res = simulate(india_1m_today, c["m1"][1], c["side"], c["entry"], c["stop"], c["targets"], 60, key, "15:19", None)
        else:
            res = simulate(today, c["i"], c["side"], c["entry"], c["stop"], c["targets"], c["max_bars"], key, flat, c["invalid"])
        tr = {"id": f"{key}|{tf}|{today[0]['d']}|{ti}|{c['model']}|{c['side']}", "rule": f"{c['model']}_{tf}", "model": c["model"], "book_tf": tf, "key": key, "side": c["side"], "d": today[0]["d"],
              "opened_t": ti, "entry": round(c["entry"], 4), "stop": round(c["stop"], 4), "targets": [[round(p, 4), s_] for p, s_ in c["targets"]],
              "why": c["why"], "tf": 1 if c.get("m1") else tf, "h4": h4_at(ctx, key, today[0]["d"], ti, 1 if c.get("m1") else tf), **res}
        tr["opened_ist"] = _ist(key, tr["d"], ti)
        if res.get("closed_t"):
            tr["closed_ist"] = _ist(key, res["closed_d"], res["closed_t"])
        trades.append(tr); n_day += 1
        if res["status"] == "open":
            open_until = None
        else:
            r_day += res.get("r") or 0
            open_until = res["closed_t"]
    return setups, trades


def _ist(key, d, hm):
    """India bars are already IST; crypto / gold stored bars are UTC."""
    if key in COST_PTS:
        return f"{d} {hm}"
    try:
        x = datetime.strptime(f"{d} {hm}", "%Y-%m-%d %H:%M") + timedelta(hours=5, minutes=30)
        return x.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return f"{d} {hm}"


def _bar_idx_5m(today, hm):
    idx = 0
    for q, b in enumerate(today):
        if b["t"] <= hm:
            idx = q
    return idx


def _exit_time(bars, k, nlegs, res):
    """Rough exit time for the one-at-a-time rule: re-walk to the bar where the trade ended."""
    sd_stop = res.get("legs") or []
    # the simulate loop ends on a bar; recover it by re-simulating index positions is costly - use the time of the bar where the exit price was reached
    for j in range(k + 1, len(bars)):
        b = bars[j]
        if any((b["l"] <= p <= b["h"]) or why.startswith(("time", "flat", "invalid")) for p, _, why in sd_stop):
            return b["t"]
    return bars[-1]["t"]


# ============================================================ forward outcomes (Scoreboard)
def outcomes(bars_all, events, n=(3, 6, 12)):
    idx = {(b["d"], b["t"]): i for i, b in enumerate(bars_all)}
    for e in events:
        i = idx.get((e["d"], e["t"]))
        if i is None:
            continue
        sd = 1 if e["side"] == "long" else -1
        c0 = bars_all[i]["c"]
        e["fwd"] = {str(k): (round(sd * (bars_all[i + k]["c"] / c0 - 1) * 1e4, 1) if i + k < len(bars_all) and bars_all[i + k]["d"] == e["d"] else None) for k in n}
    return events


def baseline(bars_all, k=6):
    up = n = 0
    for i in range(len(bars_all) - k):
        if bars_all[i + k]["d"] != bars_all[i]["d"]:
            continue
        n += 1; up += bars_all[i + k]["c"] > bars_all[i]["c"]
    return round(up / n, 3) if n else None


# ============================================================ runner
def build_market(key, tf=None, of_dir=None, fp_dir=None, h4=None, _cache=None):
    desk, src, tfs = MARKETS[key]
    tf = tf or tfs[0]
    cache = _cache if _cache is not None else {}
    m1 = []
    if src == "of1m":
        if "m1" not in cache:
            cache["m1"] = india_1m(key, of_dir=of_dir)
        m1 = cache["m1"]
        bars = merge(m1, tf, 9 * 60 + 15)
    else:
        if "b15" not in cache:
            cache["b15"] = stored_15(desk, key, fp_dir=fp_dir)
        bars = cache["b15"] if tf == 15 else merge(cache["b15"], tf, 0, base=15)
    if not bars:
        return {"key": key, "tf": tf, "err": "no bars"}
    ctx = context(bars, key)
    ctx["h4"] = h4 if h4 is not None else _h4_snaps().get(key, [])
    today = ctx["today"]
    d0 = today[0]["d"]
    m1_today = [b for b in m1 if b["d"] == d0]
    if src == "of1m":
        m1_prev = [b for b in m1 if b["d"] == ctx.get("prior_day")]
        ctx["prior_1m_absdelta"] = [abs(b["delta"]) for b in m1_prev]
    ev = detect(today, ctx, key, tf)
    setups, trades = run_models(key, tf, today, ctx, ev, m1_today)
    fh = max(1, 60 // tf)
    dt = day_type(today, ctx["prior"], fh)
    vw = vwap_series(today)[-1]
    tp = profile(today)
    last = today[-1]["c"]
    pr = ctx["prior"] or {}
    lv = [("VWAP", vw), ("Today VPOC", tp["poc"] if tp else None), ("Today VAH", tp["vah"] if tp else None), ("Today VAL", tp["val"] if tp else None),
          ("Yesterday VAH", pr.get("vah")), ("Yesterday VPOC", pr.get("poc")), ("Yesterday VAL", pr.get("val"))]
    lv += [("LVN (Yesterday)", x["p"]) for x in (pr.get("lvn") or [])][:3]
    lv += [(f"Naked VPOC {n['d'][5:]}", n["p"]) for n in ctx["naked"]][:4]
    unf = [e for e in ev if e["pattern"] == "unfinished" and not any(b["l"] <= e["price"] <= b["h"] for b in today if b["t"] > e["t"])]
    lv += [("Unfinished " + ("High" if e["side"] == "long" else "Low"), e["price"]) for e in unf][-3:]
    if src == "of1m":
        rng = [b for b in m1_today if "09:15" <= b["t"] <= "09:20"]
        if len(rng) == 6:
            lv += [("Opening Range High", max(b["h"] for b in rng)), ("Opening Range Low", min(b["l"] for b in rng))]
    levels = sorted([{"name": n, "p": round(p, 4), "dist_pct": round((p / last - 1) * 100, 3)} for n, p in lv if p is not None], key=lambda x: abs(x["dist_pct"]))
    all_hist = [b for b in bars]
    outcomes(all_hist, ev)
    h4 = (ctx["h4"][-1][1] if ctx["h4"] else None)
    return {"key": key, "name": NAME[key], "tf": tf, "tfs": list(tfs), "h4": h4, "h4_word": None if h4 is None else ("Up" if h4 >= H4_EDGE else "Down" if h4 <= -H4_EDGE else "Neutral"), "day": d0, "last": last, "bar_t": today[-1]["t"], "n_bars": len(today),
            "day_type": dt, "levels": levels, "events": ev, "setups": setups, "trades": trades, "base6": baseline(all_hist)}


def _h4_path():
    return os.path.join(OUT, "h4.json")


def _h4_snaps():
    try:
        return json.load(open(_h4_path()))
    except Exception:
        return {}


def _h4_record():
    """Append the Regime Board's current 4H score per market when it changes (or every 15 min). Keeps the last 2,000 a market."""
    try:
        rows = (json.load(open(RB_LATEST)) or {}).get("rows") or {}
    except Exception:
        return
    snaps = _h4_snaps()
    now = datetime.now(IST).strftime("%Y-%m-%d %H:%M")
    ch = False
    for k, rid in H4_ROW.items():
        sc = ((rows.get(rid) or {}).get("tf") or {}).get("4h")
        if sc is None:
            continue
        L = snaps.setdefault(k, [])
        if L and L[-1][1] == sc and (datetime.strptime(now, "%Y-%m-%d %H:%M") - datetime.strptime(L[-1][0], "%Y-%m-%d %H:%M")).total_seconds() < 900:
            continue
        if L and L[-1][0] == now:
            L[-1][1] = sc
        else:
            L.append([now, sc])
        snaps[k] = L[-2000:]; ch = True
    if ch:
        os.makedirs(OUT, exist_ok=True)
        tmp = _h4_path() + ".tmp"
        json.dump(snaps, open(tmp, "w")); os.replace(tmp, _h4_path())


def _log_path():
    return os.path.join(OUT, "trades.json")


def _merge_log(trades):
    """Keep every trade ever made (id -> trade). Today's are recomputed from bars each pass; older days are frozen."""
    p = _log_path()
    try:
        log = json.load(open(p))
    except Exception:
        log = {}
    for t in trades:
        old = log.get(t["id"])
        if old and old.get("status") == "closed" and t.get("status") == "closed":
            continue                                     # a closed trade never changes
        log[t["id"]] = t
    os.makedirs(OUT, exist_ok=True)
    tmp = p + f".{os.getpid()}.tmp"
    json.dump(log, open(tmp, "w"), default=str); os.replace(tmp, p)
    return log


def tick(force=False):
    """From the live-desk loop (via regimeboard.tick): at most every 20 s."""
    if not force and time.time() - S["t"] < 20:
        return False
    S["t"] = time.time()
    try:
        _h4_record()
    except Exception as e:
        S["err"] = "4H: " + str(e)[:140]
    snaps = _h4_snaps()
    out, all_tr = {}, []
    for k, (_, _, tfs) in MARKETS.items():
        cache = {}
        for tf in tfs:
            try:
                m = build_market(k, tf, h4=snaps.get(k, []), _cache=cache)
                out[f"{k}|{tf}"] = m
                all_tr += m.get("trades") or []
            except Exception as e:
                out[f"{k}|{tf}"] = {"key": k, "tf": tf, "err": f"{type(e).__name__}: {e}"[:160]}
    try:
        _merge_log(all_tr)
    except Exception as e:
        S["err"] = str(e)[:160]
    S["last"] = {"at": datetime.now(IST).isoformat(timespec="seconds"), "markets": out}
    try:
        os.makedirs(OUT, exist_ok=True)
        tmp = os.path.join(OUT, "latest.json.tmp")
        json.dump(S["last"], open(tmp, "w"), default=str); os.replace(tmp, os.path.join(OUT, "latest.json"))
    except Exception:
        pass
    return True


def trades():
    """Every logged trade. Trades logged before 15M / 30M existed carry the bare model id: they get their timeframe added here."""
    try:
        out = list(json.load(open(_log_path())).values())
    except Exception:
        return []
    for t in out:
        if t.get("rule") in MODELS:
            t["model"] = t["rule"]
            t["book_tf"] = 5 if t["key"] in COST_PTS else 15
            t["rule"] = f"{t['rule']}_{t['book_tf']}"
    return out


def rule_ids():
    """Every model x timeframe paper rule: id -> label."""
    out = {}
    for m, lab in MODELS.items():
        for tf in sorted({x for _, _, tfs in MARKETS.values() for x in tfs}):
            if m == "ofp_or" and tf != 5:
                continue
            out[f"{m}_{tf}"] = f"{lab} · {tf}M"
    return out


def scoreboard(markets):
    sb = {}
    for k, m in markets.items():
        for e in m.get("events") or []:
            z = sb.setdefault(e["pattern"], {"pattern": e["pattern"], "label": PATTERNS.get(e["pattern"], e["pattern"]), "n": 0, "n6": 0, "hit6": 0, "bps6": 0.0})
            z["n"] += 1
            f = (e.get("fwd") or {}).get("6")
            if f is not None:
                z["n6"] += 1; z["hit6"] += f > 0; z["bps6"] += f
    for z in sb.values():
        z["hit_pct"] = round(100 * z["hit6"] / z["n6"]) if z["n6"] else None
        z["avg_bps"] = round(z["bps6"] / z["n6"], 1) if z["n6"] else None
    tr = trades()
    mb = {}
    ids = rule_ids()
    for t in tr:
        z = mb.setdefault(t["rule"], {"rule": t["rule"], "label": ids.get(t["rule"], t["rule"]), "n": 0, "closed": 0, "wins": 0, "r": 0.0, "open": 0})
        z["n"] += 1
        if t.get("status") == "closed":
            z["closed"] += 1; z["wins"] += (t.get("r") or 0) > 0; z["r"] = round(z["r"] + (t.get("r") or 0), 2)
        else:
            z["open"] += 1
    return {"patterns": sorted(sb.values(), key=lambda z: -z["n"]), "models": [dict(mb.get(k, {"rule": k, "label": v, "n": 0, "closed": 0, "wins": 0, "r": 0.0, "open": 0}), status="Record Only")
                                                                          for k, v in ids.items()]}


def api(key=None, tf=None):
    if not S.get("last") or time.time() - S["t"] > 60:
        tick(force=True)
    L = S.get("last") or {}
    ms = L.get("markets") or {}
    out = {"at": L.get("at"), "models": MODELS, "patterns": PATTERNS, "score": scoreboard(ms),
           "note": "Paper only - never orders. Every model is Record Only until it passes MASTER_BACKTESTER. Each timeframe is its own feed and paper book: "
                   "India 5M, 15M and 30M (1-min for the opening range), crypto and Gold 15M and 30M (built from the stored 15-min footprint - no finer footprint is kept for them). "
                   "4H is a filter: a model trades only with the Regime Board's 4H trend (score >= +2 up, <= -2 down, between = both sides). Iceberg detection is not built yet (needs the order book).",
           "err": S.get("err")}
    if key:
        tfs = MARKETS.get(key, (None, None, ()))[2]
        tf = int(tf) if tf and int(tf) in tfs else (tfs[0] if tfs else None)
        out["market"] = ms.get(f"{key}|{tf}")
    else:
        out["markets"] = {k: {kk: v for kk, v in m.items() if kk not in ("events",)} | {"events": (m.get("events") or [])[-40:]} for k, m in ms.items()}
    return out
