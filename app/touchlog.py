"""
Level-touch log (2026-10-24). Research recorder - no signals, no orders.

Every time Nifty / Bank Nifty spot reaches a key level (09:30-14:30 IST) we save what was happening right then:
  level + name (yday POC/VAH/VAL, PDH/PDL, prior week / month, OI walls, max pain, MP supports / resistances), side
  (support test = price came from above, resistance test = from below), touch number today;
  footprint at the touch from the Dhan order-flow engine (real trades, futures): delta of the last 1 / 3 / 5 minutes,
  15-min CVD change and price change (divergence), volume and delta at the bar extremes (absorption proxy),
  5-min trend (EMA20 vs EMA60), book imbalance (5 / 20 / deep levels), engine events in the last 5 min
  (absorption, iceberg, wall consumed / pulled, big trade);
  FII / Pro / Client positions from the evening before, the setup read and guard at that moment.
Then the outcome: fade return 15 / 30 / 60 min later (bps, long from support / short from resistance), and the worst /
best move inside 30 min. After ~30-40 sessions: scripts/research/touch_review.py tests level + positions + footprint together.
Files: data_store/livedesk/touches/YYYY-MM-DD.json
Why: levels alone (2023-26, 3,978 touches) and levels + FII positions show no edge - see data_store/research/level_fii.
"""
from __future__ import annotations

import json
import os
from datetime import datetime

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
DIR = os.path.join(ROOT, "data_store", "livedesk", "touches")
HOME = os.path.expanduser("~")
OF = os.path.join(os.environ.get("MP_DHAN_DIR") or os.path.join(HOME, "Documents", "dhan"), "orderflow", "data", "of")
TOL = 0.0001          # within 0.01 % counts as reaching the level
COOL = 15 * 60        # same level counts again only after 15 min
S: dict = {"day": None, "rows": [], "last": {}, "seen": {}}


def _path(day):
    return os.path.join(DIR, f"{day}.json")


def load(day):
    try:
        return json.load(open(_path(day)))
    except Exception:
        return []


def _save():
    os.makedirs(DIR, exist_ok=True)
    tmp = _path(S["day"]) + ".tmp"
    with open(tmp, "w") as f:
        json.dump(S["rows"], f, indent=1, default=str)
    os.replace(tmp, _path(S["day"]))


def _ema(xs, n):
    k, e = 2 / (n + 1), None
    for x in xs:
        e = x if e is None else e + k * (x - e)
    return e


def flow_at(bars: list, day: str, side: int) -> dict:
    b = [x for x in bars if x.get("d") == day and not x.get("approx")]
    if len(b) < 5:
        return {}
    def dr(n):
        z = b[-n:]; tot = sum(x.get("total") or 0 for x in z)
        return round(sum(x.get("delta") or 0 for x in z) / tot, 3) if tot else None
    z15 = b[-15:]
    cvd15 = sum(x.get("delta") or 0 for x in z15)
    px15 = z15[-1]["c"] - z15[0]["o"]
    # volume / delta at the extreme of the last 3 bars (support test: the lows; resistance: the highs)
    ext_v = ext_d = 0.0
    for x in b[-3:]:
        bins = x.get("bins") or []
        if not bins:
            continue
        e = min(bins, key=lambda q: q["p"]) if side > 0 else max(bins, key=lambda q: q["p"])
        ext_v += e["b"] + e["s"]; ext_d += e["b"] - e["s"]
    tot3 = sum(x.get("total") or 0 for x in b[-3:]) or 1
    allb = [x for x in bars if x.get("d", "") <= day][-400:]          # trend needs the previous sessions too
    c5 = [x["c"] for i, x in enumerate(allb) if i % 5 == 4]
    tr = None
    if len(c5) >= 20:
        tr = 1 if _ema(c5, 20) > _ema(c5, 60) else -1
    return {"d1": dr(1), "d3": dr(3), "d5": dr(5), "cvd15": round(cvd15), "px15": round(px15, 1),
            "diverge15": bool(cvd15 and px15 and (cvd15 > 0) != (px15 > 0)),
            "ext_vol_share": round(ext_v / tot3, 3), "ext_delta": round(ext_d), "trend5": tr, "fut": b[-1]["c"]}


def book_events(key: str, level_fut: float, t_epoch: float) -> dict:
    out = {}
    try:
        lj = json.load(open(os.path.join(OF, "live.json")))
        now = ((lj.get("instruments") or {}).get(key) or {}).get("now") or {}
        out.update({k: now.get(k) for k in ("imb5", "imb20", "imb_deep", "cvd")})
    except Exception:
        pass
    ev = {}
    try:
        f = os.path.join(OF, datetime.now().strftime("%Y-%m-%d"), "events.jsonl")
        with open(f, "rb") as fh:
            fh.seek(max(0, os.path.getsize(f) - 400_000)); tail = fh.read().decode(errors="ignore").splitlines()[1:]
        for line in tail:
            try:
                e = json.loads(line)
            except Exception:
                continue
            if e.get("sym") != key or t_epoch - e.get("t", 0) > 300:
                continue
            k = e.get("kind")
            if k in ("wall_new",):
                continue
            near = level_fut and e.get("price") and abs(e["price"] / level_fut - 1) < 0.0015
            slot = ev.setdefault(k, {"n": 0, "near": 0, "bias": 0})
            slot["n"] += 1; slot["near"] += int(bool(near)); slot["bias"] += int(e.get("bias") or 0)
    except Exception:
        pass
    out["events5m"] = ev
    return out


def _block(pool, L):
    """Support / resistance as a block: every level within +-0.1 % bands that overlap the touched level's band."""
    zs = sorted((p * 0.999, p * 1.001, n) for p, n in pool)
    blocks = []
    for lo, hi, n in zs:
        if blocks and lo <= blocks[-1][1]:
            blocks[-1] = [blocks[-1][0], max(hi, blocks[-1][1]), blocks[-1][2] + [n]]
        else:
            blocks.append([lo, hi, [n]])
    for lo, hi, ns in blocks:
        if lo <= L <= hi:
            return lo, hi, ns
    return L * 0.999, L * 1.001, []


def _opt_oi(day, key, side, mid, t0, t1):
    """% change in put OI (support) / call OI (resistance) at strikes within 0.4 % of the block between two snapshots."""
    import pandas as pd
    p = os.path.join(ROOT, "data_store", "flow", "option_chain", f"{day}.csv.gz")
    o = pd.read_csv(p, usecols=["ts", "symbol", "strike", "side", "oi"])
    o = o[(o.symbol == key) & (o.side == ("PE" if side > 0 else "CE")) & ((o.strike / mid - 1).abs() < 0.004)]
    o["ts"] = pd.to_datetime(o.ts)
    s0, s1 = o[o.ts <= t0].ts.max(), o[o.ts <= t1].ts.max()
    if pd.isna(s0) or pd.isna(s1) or s1 <= s0:
        return None
    a, b = o[o.ts == s0].oi.sum(), o[o.ts == s1].oi.sum()
    return round((b / a - 1) * 100, 2) if a else None


def _zone_track(r, t, px, el, day, bars_fn):
    z, sd = r["zone"], r["side"]
    far, near = (z["lo"], z["hi"]) if sd > 0 else (z["hi"], z["lo"])
    r["lo_px"], r["hi_px"] = min(r["lo_px"], px), max(r["hi_px"], px)
    mk = int(el)
    if mk <= 60 and len(r["path"]) <= mk:
        r["path"].append(round(px, 1))                                 # one price per minute for 60 min
    if el <= 60:
        if sd * (px - far) < 0: r["pierced"] = True
        if sd * (px - near) > 0 and el > 1: r["out_near"] = True
    if "early" not in r and el >= 5:                                   # decision point: 5 minutes into the block
        e = {"pierced5": bool(r.get("pierced"))}
        try:
            hm = r["time"][:5]
            bs = [x for x in bars_fn(r["key"]) if x.get("d") == day and x.get("t", "") >= hm][:5]
            tot = sum(x.get("total") or 0 for x in bs)
            e["def_delta"] = round(sd * sum(x.get("delta") or 0 for x in bs) / tot, 3) if tot else None   # + = defenders won
        except Exception:
            pass
        try:
            t0 = datetime.fromtimestamp(r["t_epoch"] - 60); t1 = datetime.fromtimestamp(r["t_epoch"] + 8 * 60)
            e["opt_oi_chg"] = _opt_oi(day, r["key"], sd, (z["lo"] + z["hi"]) / 2, t0, t1) if el >= 9 else None
        except Exception:
            e["opt_oi_chg"] = None
        if el >= 9 or e.get("opt_oi_chg") is not None:
            e["px"] = px; r["early"] = e
    if "zone_outcome" not in r and el >= 60:
        c5 = r["path"][4::5]
        beyond = [sd * (c - far) < 0 for c in c5]
        accept = any(beyond[a] and beyond[a + 1] for a in range(len(beyond) - 1))
        r["zone_outcome"] = ("BREAK" if accept and sd * (px - far) < 0 else "SWEEP" if r.get("pierced") and not accept and r.get("out_near")
                             else "HOLD" if not r.get("pierced") and r.get("out_near") else "CHOP")
        if r.get("early", {}).get("px"):
            r["thru60_from_early"] = round(-sd * (px / r["early"]["px"] - 1) * 1e4, 1)


def tick(t: datetime, spots: dict, views: dict, S_mod, P_mod, bars_fn) -> bool:
    day = t.strftime("%Y-%m-%d")
    if S["day"] != day:
        S.update(day=day, rows=load(day), last={}, seen={})
    m = t.hour * 60 + t.minute
    changed = False
    ep = t.timestamp()              # live: IST wall clock on the Mac = real epoch
    # 1) outcomes for open touches
    for r in S["rows"]:
        if r.get("done"):
            continue
        px = spots.get(r["key"])
        if not px:
            continue
        el = (ep - r["t_epoch"]) / 60
        # confirmation lead (23 Oct look): the touch minute's own delta, and outcomes from the minute's close
        if "tb_delta" not in r and t.strftime("%H:%M") > r["time"][:5]:
            try:
                bar = next((x for x in reversed(bars_fn(r["key"])) if x.get("d") == day and x.get("t") == r["time"][:5]), None)
            except Exception:
                bar = None
            r["tb_delta"] = round(bar["delta"] / bar["total"], 3) if bar and bar.get("total") else None
            r["entry_close"] = round(px, 1); r["entry_epoch"] = ep; changed = True
        if r.get("entry_close"):
            ec = (ep - r["entry_epoch"]) / 60
            for n in (15, 30, 60):
                if f"c{n}" not in r and ec >= n:
                    r[f"c{n}"] = round(r["side"] * (px / r["entry_close"] - 1) * 1e4, 1); changed = True
        if r.get("zone"):
            _zone_track(r, t, px, el, day, bars_fn)
        f = r["side"] * (px / r["level"] - 1) * 1e4
        r["mae30"] = round(min(r.get("mae30", 0), f), 1) if el <= 30 else r.get("mae30")
        r["mfe30"] = round(max(r.get("mfe30", 0), f), 1) if el <= 30 else r.get("mfe30")
        for n in (15, 30, 60):
            if f"f{n}" not in r and el >= n:
                r[f"f{n}"] = round(f, 1); changed = True
        if ("f60" in r and "c60" in r and "zone_outcome" in r) or m >= 15 * 60 + 29:
            r["done"] = True; changed = True
    # 2) new touches
    if 9 * 60 + 30 <= m <= 14 * 60 + 30:
        for key in S_mod.KEYS:
            px, prev = spots.get(key), S["last"].get(key)
            S["last"][key] = px or prev
            if not (px and prev) or px == prev:
                continue
            try:
                pool = S_mod.level_pool(key, px, views.get(key) or {})
            except Exception:
                continue
            for L, name in pool:
                up, dn = L * (1 + TOL), L * (1 - TOL)
                side = 1 if prev > up >= px else -1 if prev < dn <= px else 0      # +1 support test (from above), -1 resistance test
                if not side:
                    continue
                lid = f"{key}|{name.split(' + ')[0]}|{round(L)}"
                zlo, zhi, znames = _block(pool, L)
                if ep - S["seen"].get(lid, 0) < COOL:
                    continue
                S["seen"][lid] = ep
                n_today = sum(1 for r in S["rows"] if r["lid"] == lid) + 1
                bars = bars_fn(key)
                fl = flow_at(bars, day, side)
                basis = (fl.get("fut") - px) if fl.get("fut") else 0
                ctx = {}
                try:
                    pos = P_mod._cached("positions", 600, P_mod._positions)
                    for row in pos.get("rows", []):
                        if row["who"] in ("FII", "Pro", "Client"):
                            ctx[row["who"]] = {k: row[k] for k in ("fut_long_pct", "fut_net_1d", "fut_net_5d", "opt_net_1d", "puts_written_1d", "calls_written_1d")}
                    ctx["guard"] = P_mod.guard(key, t)
                    ctx["vol15"] = P_mod.vol_label(key)[0]
                except Exception as e:
                    ctx["error"] = str(e)[:80]
                try:
                    srow = next((r for r in reversed(S_mod.load(day)) if r["market"] == key), None)
                    ctx["setup"] = {"bias": srow.get("bias"), "score": srow.get("score"), "status": srow.get("status")} if srow else None
                except Exception:
                    pass
                S["rows"].append({"id": f"{key}-{t.strftime('%H%M%S')}-{len(S['rows'])}", "key": key, "lid": lid, "time": t.strftime("%H:%M:%S"),
                                  "t_epoch": ep, "level": round(L, 1), "name": name, "side": side, "touch_n": n_today,
                                  "spot": round(px, 1), "basis": round(basis, 1), "flow": fl,
                                  "zone": {"lo": round(zlo, 1), "hi": round(zhi, 1), "levels": znames}, "path": [], "lo_px": px, "hi_px": px,
                                  "book": book_events(key, L + basis, ep), "ctx": ctx})
                changed = True
    if changed:
        _save()
    return changed


def summary(day: str | None = None) -> dict:
    day = day or datetime.now().strftime("%Y-%m-%d")
    rows = S["rows"] if S["day"] == day else load(day)
    days = sorted(f[:-5] for f in os.listdir(DIR) if f.endswith(".json")) if os.path.isdir(DIR) else []
    total = sum(len(load(d)) for d in days[-60:])
    return {"day": day, "today": len(rows), "sessions": len(days), "total": total, "target_sessions": 35,
            "rows": [{k: r.get(k) for k in ("key", "time", "level", "name", "side", "touch_n", "f15", "f30", "f60")} for r in rows[-30:]]}
