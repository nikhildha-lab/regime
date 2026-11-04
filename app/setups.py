"""
Trade setups (2026-10-24, v1: Nifty / Bank Nifty intraday). Indications only - never places orders; Nikhil decides.

Every 5 minutes in NSE hours (09:30-15:00 for new setups) each index gets one read built from six votes:
  trend      5-min + 15-min score from the dashboard's timeframe engine          (context - descriptive)
  impact     overall impact score /10 (trend 40 / flows 30 / positioning 30)     (context - no 5-day edge in the 2026-10-21 test)
  options OI last 15 min of the NSE option chain: call / put build-up, covering   (untested as a signal)
  futures OI near-month futures: long / short build-up, short covering, unwinding (OI states: no edge in the 2-year test)
  PCR        change in the near-expiry put/call ratio since the open             (untested)
  order flow 15-min delta of the last 3 bars, Dhan order-flow engine (real trades) (filter: +0.10R in the real-trade crypto test)
Score = sum of votes. >= +3 -> BUY DIPS, <= -3 -> SELL RISES, otherwise STAND ASIDE.
Levels come from today's VWAP / value area / initial balance, the daily supports and resistances, previous day high / low
and the option walls. Entry = the nearest level on the dip (rise) side, stop beyond the next level (0.15-0.6 % of price),
targets = the next two levels the other way. A setup needs reward:risk >= 1.2 to be "ready".

Life of a setup: watching -> active (price reached the entry zone) -> target 2 / stop / breakeven after target 1 / time exit
15:20, or cancelled when the read changes before entry, or expired at 15:00 without entry.
Results are graded in index points and R, also net of the desk's option cost (4 pts Nifty / 10 pts Bank Nifty).
Files: data_store/livedesk/setups/YYYY-MM-DD.json
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
DIR = os.path.join(ROOT, "data_store", "livedesk", "setups")
OC_LIVE = os.path.join(ROOT, "data_store", "flow", "oc_live.json")
MP_LIVE = os.path.join(ROOT, "data_store", "dashboard", "market_pulse_live.json")
OC_DIR = os.path.join(ROOT, "data_store", "flow", "option_chain")
KEYS = ("NIFTY", "BANKNIFTY")
COST_FUT = {"NIFTY": 18.0, "BANKNIFTY": 45.0}          # futures round trip in points (STT 0.05% from Apr 2026)
BREAK_KEYS = ("BANKNIFTY",)                             # break trade: tested +3.1 / +5.8 bps net (2024-26); Nifty lost
BRK: dict = {"day": None}
STEP = {"NIFTY": 50, "BANKNIFTY": 100}
COST = {"NIFTY": 4.0, "BANKNIFTY": 10.0}
TESTED = {"trend": "context", "impact": "context - no 5-day edge in test", "options_oi": "untested",
          "futures_oi": "no edge alone (2-year test)", "pcr": "untested", "flow": "filter: +0.10R (real-trade test)"}
_CACHE: dict = {}
import prep as PREP                      # app/ is on sys.path (livedesk reloads both on change)


def _json(path):
    try:
        mt = os.path.getmtime(path)
    except OSError:
        return {}
    c = _CACHE.get(path)
    if c and c[0] == mt:
        return c[1]
    try:
        d = json.load(open(path))
    except Exception:
        return c[1] if c else {}
    _CACHE[path] = (mt, d)
    return d


def _path(day: str) -> str:
    return os.path.join(DIR, f"{day}.json")


def load(day: str) -> list:
    try:
        return json.load(open(_path(day)))
    except Exception:
        return []


def save(day: str, rows: list):
    os.makedirs(DIR, exist_ok=True)
    tmp = _path(day) + ".tmp"
    with open(tmp, "w") as f:
        json.dump(rows, f, indent=1, default=str)
    os.replace(tmp, _path(day))


# ------------------------------------------------------------------ the read
def votes(key: str, spot: float, view: dict, pcr_rows: list, flow: dict | None) -> list:
    """[(name, vote -1/0/+1, plain-language reason)]"""
    out = []
    mk = (_json(MP_LIVE).get("markets") or {}).get(key) or {}
    tf = {r.get("tf"): (r.get("scores") or {}).get("overall") for r in ((mk.get("mtf") or {}).get("rows") or []) if r.get("available")}
    s = [v for v in (tf.get("5m"), tf.get("15m")) if v is not None]
    tr = sum(s) / len(s) if s else None
    out.append(("trend", 0 if tr is None or abs(tr) < 1 else (1 if tr > 0 else -1),
                "5-15 min trend " + ("n/a" if tr is None else f"{tr:+.1f}/10")))
    ov = (((mk.get("impact") or {}).get("overall")) or {}).get("score")
    out.append(("impact", 0 if ov is None or abs(ov) < 2.5 else (1 if ov > 0 else -1), "impact score " + ("n/a" if ov is None else f"{ov:+.1f}/10")))
    oc = ((_json(OC_LIVE).get("symbols") or {}).get(key)) or {}
    w15 = next((w for w in (oc.get("windows") or []) if w.get("window") == "last 15 min"), None)
    b = (w15 or {}).get("bias")
    mix = ", ".join(f"{k} {v}%" for k, v in sorted(((w15 or {}).get("mix") or {}).items(), key=lambda kv: -kv[1])[:2] if v)
    out.append(("options_oi", 0 if b is None or abs(b) < 40 else (1 if b > 0 else -1), f"options OI 15 min: {mix or 'n/a'}"))
    fut = next((f for f in (oc.get("futures") or []) if f.get("minutes") == 15), None)
    st = (fut or {}).get("state") or ""
    fv = 1 if st in ("long build-up", "short covering") else -1 if st in ("short build-up", "long unwinding") else 0
    out.append(("futures_oi", fv, f"futures 15 min: {st or 'n/a'}"))
    p0 = next((r for r in pcr_rows if r.get("pcr_near") not in (None, "")), None)
    p1 = next((r for r in reversed(pcr_rows) if r.get("pcr_near") not in (None, "")), None)
    dp = None if not (p0 and p1) else float(p1["pcr_near"]) - float(p0["pcr_near"])
    out.append(("pcr", 0 if dp is None or abs(dp) < 0.05 else (1 if dp > 0 else -1),
                "PCR " + ("n/a" if dp is None else f"{float(p1['pcr_near']):.2f} ({dp:+.2f} since open)")))
    d3 = (flow or {}).get("delta_3bars")
    out.append(("flow", 0 if d3 is None or abs(d3) < 0.05 else (1 if d3 > 0 else -1),
                "order flow 45 min " + ("n/a" if d3 is None else f"{d3:+.2f} of volume")))
    return out


def level_pool(key: str, spot: float, view: dict) -> list:
    pool = []
    add = lambda p, n: pool.append((float(p), n)) if p not in (None, "") else None
    L = view.get("levels") or {}                  # the live view names them POC / VAH / VAL / VWAP / IB high / IB low
    for k, v in L.items():
        if isinstance(v, (int, float)):
            add(v, {"poc": "POC", "vah": "VAH", "val": "VAL", "vwap": "VWAP", "ib_high": "IB high", "ib_low": "IB low"}.get(k, k))
    sr = view.get("sr") or {}
    for p in sr.get("support") or []:
        add(p, "support")
    for p in sr.get("resistance") or []:
        add(p, "resistance")
    for k, n in (("PDH", "prev day high"), ("PDL", "prev day low"), ("PWH", "prev week high"), ("PWL", "prev week low")):
        add(sr.get(k), n)
    oc = ((_json(OC_LIVE).get("symbols") or {}).get(key)) or {}
    add(oc.get("call_wall"), "call wall")
    add(oc.get("put_wall"), "put wall")
    add(oc.get("max_pain"), "max pain")
    mk = (_json(MP_LIVE).get("markets") or {}).get(key) or {}
    lv = mk.get("levels") or {}
    for x in (lv.get("supports") or [])[:4]:
        add(x.get("price"), (x.get("names") or ["support"])[0])
    for x in (lv.get("resistances") or [])[:4]:
        add(x.get("price"), (x.get("names") or ["resistance"])[0])
    ls = (lv.get("intraday") or {}).get("last_session") or {}
    for k, n in (("poc", "yday POC"), ("vah", "yday VAH"), ("val", "yday VAL")):
        add(ls.get(k), n)
    pool = [(p, n) for p, n in pool if p > 0 and abs(p / spot - 1) < 0.03]
    pool.sort()
    merged = []                                   # levels within 0.05 % of each other count as one
    for p, n in pool:
        if merged and abs(p / merged[-1][0] - 1) < 0.0005:
            merged[-1] = (merged[-1][0], merged[-1][1] if n in merged[-1][1] else merged[-1][1] + " + " + n)
        else:
            merged.append((p, n))
    return merged


def oi_strikes(key: str) -> list:
    """Big option positions from the latest option-chain snapshot (2 nearest expiries): top-3 put OI below / call OI
    above spot, top-2 OI additions each side (fresh writing), top-2 volume strikes. [(strike, name, kind)]"""
    try:
        files = sorted(f for f in os.listdir(OC_DIR) if f.endswith(".csv.gz"))
    except OSError:
        return []
    if not files:
        return []
    f = os.path.join(OC_DIR, files[-1]); mt = os.path.getmtime(f)
    c = _CACHE.get(("oi", key))
    if c and c[0] == mt and time.time() - c[2] < 120:
        return c[1]
    out = []
    try:
        import pandas as pd
        o = pd.read_csv(f, usecols=["ts", "symbol", "expiry", "strike", "side", "oi", "chg_oi", "volume", "underlying"])
        o = o[(o.symbol == key) & o.expiry.notna() & (o.oi > 0)]
        o = o[o.ts == o.ts.max()]
        exps = sorted(o.expiry.unique())[:2]; o = o[o.expiry.isin(exps)]
        u = float(o.underlying.iloc[-1])
        a = o.groupby(["strike", "side"]).agg(oi=("oi", "sum"), chg=("chg_oi", "sum"), vol=("volume", "sum")).reset_index()
        puts = a[(a.side == "PE") & (a.strike < u) & (a.strike > u * 0.97)]; calls = a[(a.side == "CE") & (a.strike > u) & (a.strike < u * 1.03)]
        out += [(r.strike, f"put OI {int(r.strike)}", "oi") for r in puts.nlargest(3, "oi").itertuples()]
        out += [(r.strike, f"call OI {int(r.strike)}", "oi") for r in calls.nlargest(3, "oi").itertuples()]
        out += [(r.strike, f"put writing {int(r.strike)}", "oi_add") for r in puts.nlargest(2, "chg").itertuples() if r.chg > 0]
        out += [(r.strike, f"call writing {int(r.strike)}", "oi_add") for r in calls.nlargest(2, "chg").itertuples() if r.chg > 0]
        out += [(r.strike, f"option volume {int(r.strike)}", "opt_vol") for r in pd.concat([puts, calls]).nlargest(2, "vol").itertuples()]
    except Exception:
        out = c[1] if c else []
    _CACHE[("oi", key)] = (mt, out, time.time())
    return out


def blocks(key: str, spot: float, view: dict) -> list:
    """Support / resistance as BLOCKS: every price level and big-OI strike +-0.1 %, overlapping bands merged.
    Strength from the 2024-26 test (break within 1 h): price level + big-OI strike = strong (4-6 %), 3+ levels or
    fresh writing / big option volume = medium (5-9 %), one price level or one OI strike alone = weak (13-19 %)."""
    items = [(p, n, "oi" if n in ("call wall", "put wall", "max pain") else "price") for p, n in level_pool(key, spot, view)]
    items += [(p, n, k) for p, n, k in oi_strikes(key) if abs(p / spot - 1) < 0.03]
    zs = sorted((p * 0.999, p * 1.001, n, k) for p, n, k in items)
    out = []
    for lo, hi, n, k in zs:
        if out and lo <= out[-1]["hi"]:
            b = out[-1]; b["hi"] = max(hi, b["hi"]); b["names"].append(n); b["kinds"].add(k)
        else:
            out.append({"lo": lo, "hi": hi, "names": [n], "kinds": {k}})
    rank = {"weak": 0, "medium": 1, "strong": 2}
    try:                                   # the rolling 10-day level map can upgrade a block (weekly / monthly highs-lows etc.)
        import levelmap as LM
        mp = LM.get(key, 10).get("blocks") or []
    except Exception:
        mp = []
    for b in out:
        k, n = b["kinds"], len(b["names"])
        b["strength"] = "strong" if ("price" in k and "oi" in k) else "medium" if (n >= 3 or "oi_add" in k or "opt_vol" in k) else "weak"
        for m_ in mp:
            if m_["lo"] <= b["hi"] and m_["hi"] >= b["lo"]:
                if rank[m_["strength"]] > rank[b["strength"]]:
                    b["strength"] = m_["strength"]
                b["names"] += [f"10-day map: {x}" for x in (m_.get("alerts") or [])[:2]]
        b["lo"], b["hi"] = round(b["lo"], 1), round(b["hi"], 1); b["kinds"] = sorted(k)
    return out


def block_at(bl: list, px: float):
    return next((b for b in bl if b["lo"] <= px <= b["hi"]), None)


def plan(key: str, spot: float, side: int, pool: list) -> dict | None:
    """side +1 buy dips, -1 sell rises. Entry at the nearest level on the dip / rise side."""
    buf, lo_r, hi_r = spot * 0.001, spot * 0.0015, spot * 0.006
    below = [x for x in pool if x[0] < spot - spot * 0.0005]
    above = [x for x in pool if x[0] > spot + spot * 0.0005]
    if side > 0:
        if not below or not above:
            return None
        e = below[-1]
        nxt = [x for x in below if x[0] < e[0] - lo_r]
        stop = (nxt[-1][0] - buf) if nxt else e[0] - hi_r
        stop = min(max(stop, e[0] - hi_r), e[0] - lo_r)
        tg = [x for x in above if x[0] > e[0] + lo_r][:2]
    else:
        if not above or not below:
            return None
        e = above[0]
        nxt = [x for x in above if x[0] > e[0] + lo_r]
        stop = (nxt[0][0] + buf) if nxt else e[0] + hi_r
        stop = max(min(stop, e[0] + hi_r), e[0] + lo_r)
        tg = [x for x in reversed(below) if x[0] < e[0] - lo_r][:2]
    if not tg:
        return None
    entry = e[0]
    risk = abs(entry - stop)
    rr = abs(tg[0][0] - entry) / risk if risk else 0
    return {"entry": round(entry, 1), "zone": [round(entry - buf, 1), round(entry + buf, 1)], "entry_level": e[1],
            "stop": round(stop, 1), "t1": round(tg[0][0], 1), "t1_level": tg[0][1],
            "t2": round(tg[1][0], 1) if len(tg) > 1 else None, "t2_level": tg[1][1] if len(tg) > 1 else None,
            "risk_pts": round(risk, 1), "rr": round(rr, 2)}


def vehicle(key: str, side: int, score: int, spot: float, vix: float | None) -> str:
    st = STEP[key]
    atm = round(spot / st) * st
    leg, far = ("CE", atm + 2 * st) if side > 0 else ("PE", atm - 2 * st)
    caution = vix is not None and vix >= 18
    if abs(score) >= 5 and not caution:
        v = f"Futures, or buy ATM {atm} {leg}"
    else:
        v = f"{'Bull call' if side > 0 else 'Bear put'} spread: buy {atm} {leg}, sell {far} {leg}"
    return v + (" · India VIX high: half size" if caution else "")


def read(key: str, spot: float, view: dict, pcr_rows: list, flow: dict | None, vix: float | None) -> dict:
    vs = votes(key, spot, view, pcr_rows, flow)
    score = sum(v for _, v, _ in vs)
    side = 1 if score >= 3 else -1 if score <= -3 else 0
    out = {"score": score, "side": side, "bias": "Buy dips" if side > 0 else "Sell rises" if side < 0 else "Stand aside",
           "votes": [{"k": k, "v": v, "why": w, "tested": TESTED[k]} for k, v, w in vs]}
    if side:
        p = plan(key, spot, side, level_pool(key, spot, view))
        if p:
            out.update(p)
            try:
                b = block_at(blocks(key, spot, view), p["entry"])
                if b:
                    out["entry_block"] = {"strength": b["strength"], "lo": b["lo"], "hi": b["hi"], "names": b["names"][:5]}
                    if b["strength"] == "weak":
                        out["note"] = "weak block (one level): these broke 13-19% of the time vs 4-6% for strong blocks"
            except Exception:
                pass
            out["ready"] = p["rr"] >= 1.2
            out["vehicle"] = vehicle(key, side, score, spot, vix)
        else:
            out["ready"] = False
            out["note"] = "no clean level to lean on"
    return out


# ------------------------------------------------------------------ lifecycle
def _hm(t: datetime) -> int:
    return t.hour * 60 + t.minute


def tick(t: datetime, spots: dict, views: dict, pcr_rows_fn, flows: dict, vix: float | None, force_read: bool = False) -> bool:
    """Called every poll (~2 s) with IST time. New reads every 5 min; price checks every call. Returns True if saved."""
    day = t.strftime("%Y-%m-%d")
    rows = load(day)
    changed = False
    m = _hm(t)
    for key in KEYS:
        px = spots.get(key)
        if not px:
            continue
        cur = next((r for r in reversed(rows) if r["market"] == key and r["status"] in ("watching", "active") and r.get("kind") != "break"), None)
        try:
            PREP.note_open(key, t, px)
            gd = PREP.guard(key, t)
        except Exception as e:
            gd = {"stand_aside": None, "no_entry": None, "half": [], "notes": [f"guard error: {e}"[:80]]}
        if key in BREAK_KEYS:
            try:
                changed |= break_tick(t, key, px, rows, views.get(key) or {}, gd)
            except Exception as e:
                BRK["error"] = str(e)[:120]
        # 1) price checks
        if cur and cur["status"] == "watching":
            if gd["no_entry"] or gd["stand_aside"]:
                pass                                                # wait: no entries in a blocked window
            elif (cur["side"] > 0 and px <= cur["zone"][1]) or (cur["side"] < 0 and px >= cur["zone"][0]):
                cur.update(status="active", entered_at=t.strftime("%H:%M"), entry_px=round(px, 1), best=0.0, worst=0.0)
                changed = True
            elif m >= 15 * 60:
                cur.update(status="expired", closed_at=t.strftime("%H:%M"), result="no entry by 15:00")
                changed = True
        if cur and cur["status"] == "active":
            sd, e = cur["side"], cur["entry_px"]
            risk = abs(e - cur["stop"]) or 1
            r = sd * (px - e) / risk
            cur["best"], cur["worst"] = round(max(cur.get("best", 0), r), 2), round(min(cur.get("worst", 0), r), 2)
            stop_now = cur.get("stop_now", cur["stop"])
            exit_ = None
            if sd * (px - stop_now) <= 0:
                exit_ = ("breakeven after target 1" if cur.get("t1_hit") else "stop", stop_now)
            elif cur.get("t2") and sd * (px - cur["t2"]) >= 0:
                exit_ = ("target 2", cur["t2"])
            elif m >= 15 * 60 + 20:
                exit_ = ("time 15:20", px)
            if not exit_ and not cur.get("t1_hit") and sd * (px - cur["t1"]) >= 0:
                cur["t1_hit"] = t.strftime("%H:%M"); cur["stop_now"] = e
                if not cur.get("t2"):
                    exit_ = ("target 1", cur["t1"])
                changed = True
            if exit_:
                pts = sd * (exit_[1] - e)
                if cur.get("t1_hit") and exit_[0] != "target 1":
                    pts = (sd * (cur["t1"] - e) + pts) / 2        # half booked at target 1, half on the runner
                cur.update(status="closed", closed_at=t.strftime("%H:%M"), exit_reason=exit_[0], exit_px=round(exit_[1], 1),
                           points=round(pts, 1), r=round(pts / risk, 2), r_net=round((pts - COST[key]) / risk, 2))
                changed = True
        # 2) new read every 5 minutes (09:30-15:00)
        if not (9 * 60 + 30 <= m < 15 * 60):
            continue
        slot = t.replace(minute=(t.minute // 5) * 5, second=0, microsecond=0).strftime("%H:%M")
        last = next((r for r in reversed(rows) if r["market"] == key and r.get("kind") != "break"), None)
        if last and last.get("slot") == slot and not force_read:
            continue
        if cur and cur["status"] == "active":
            cur["slot"] = slot
            continue
        rd = read(key, px, views.get(key) or {}, pcr_rows_fn(key), flows.get(key), vix)
        rd["guard"] = gd
        if gd["stand_aside"] and rd["side"]:
            rd.update(bias="Stand aside", side=0, ready=False, note="blocked: " + gd["stand_aside"])
        elif rd["side"] and gd["half"]:
            rd["size"] = "half"
            if rd.get("vehicle"):
                rd["vehicle"] += " · half size (" + ", ".join(gd["half"]) + ")"
        if cur and cur["status"] == "watching":
            if rd["side"] != cur["side"] or not rd.get("ready"):
                cur.update(status="cancelled", closed_at=t.strftime("%H:%M"), result="read changed before entry")
            else:                                                   # same bias: refresh levels while waiting
                cur.update({k: rd[k] for k in ("score", "votes", "guard", "size", "entry", "zone", "entry_level", "stop", "t1", "t1_level", "t2", "t2_level", "risk_pts", "rr", "vehicle") if k in rd},
                           slot=slot, updated=t.strftime("%H:%M"), spot=round(px, 1))
                changed = True
                continue
        new = dict(rd, id=f"{day}-{key}-{slot}", market=key, horizon="intraday", slot=slot, created=t.strftime("%H:%M"),
                   updated=t.strftime("%H:%M"), spot=round(px, 1),
                   status="watching" if rd["side"] and rd.get("ready") else "no trade")
        if new["status"] == "no trade" and last and last.get("status") == "no trade" and last.get("bias") == new["bias"]:
            last.update(slot=slot, updated=t.strftime("%H:%M"), votes=new["votes"], score=new["score"], spot=new["spot"])
        else:
            rows.append(new)
        changed = True
    if changed:
        save(day, rows)
    return changed


def break_tick(t: datetime, key: str, px: float, rows: list, view: dict, gd: dict) -> bool:
    """Bank Nifty break trade (paper). A visit starts when price enters a block from outside. If price trades beyond
    the far edge within the first 10 min, enter WITH the break at minute 10: support broke -> short, resistance broke
    -> long. Stop = back inside past the near edge + 0.05 %. Exit after 60 min or 15:20. Graded after futures cost."""
    day, m, ep = t.strftime("%Y-%m-%d"), t.hour * 60 + t.minute, t.timestamp()
    if BRK.get("day") != day:
        BRK.clear(); BRK["day"] = day
    st = BRK.setdefault(key, {"prev": None, "visit": None, "bl": None, "bl_t": 0})
    changed = False
    # manage an open break trade
    for r in rows:
        if r.get("kind") == "break" and r["market"] == key and r["status"] == "active":
            sd, e, stop = r["side"], r["entry_px"], r["stop"]
            risk = abs(e - stop) or 1
            ex = None
            if sd * (px - stop) <= 0: ex = ("stop", stop)
            elif ep >= r["exit_epoch"]: ex = ("time 60 min", px)
            elif m >= 15 * 60 + 20: ex = ("time 15:20", px)
            if ex:
                pts = sd * (ex[1] - e)
                r.update(status="closed", closed_at=t.strftime("%H:%M"), exit_reason=ex[0], exit_px=round(ex[1], 1), points=round(pts, 1),
                         r=round(pts / risk, 2), r_net=round((pts - COST_FUT[key]) / risk, 2))
                changed = True
    if ep - st["bl_t"] > 60 or not st["bl"]:
        st["bl"] = blocks(key, px, view); st["bl_t"] = ep
    prev, st["prev"] = st["prev"], px
    if not prev:
        return changed
    v = st["visit"]
    if v:
        far, near = (v["lo"], v["hi"]) if v["side"] > 0 else (v["hi"], v["lo"])
        el = (ep - v["start"]) / 60
        if v["side"] * (px - far) < 0:
            v["pierced"] = True
        if not v["pierced"] and v["side"] * (px - near) > 0 and el > 1:
            st["visit"] = None                                            # left on the near side: block held for now
        elif el >= 10:
            st["visit"] = None
            busy = any(r.get("kind") == "break" and r["market"] == key and r["status"] == "active" for r in rows)
            stop = near * (1 + 0.0005 * v["side"])
            if v["pierced"] and not busy and not gd.get("stand_aside") and not gd.get("no_entry") and v["start_m"] <= 14 * 60 + 30 \
                    and v["side"] * (stop - px) > 0:
                sd = -v["side"]
                st_ = STEP[key]; atm = round(px / st_) * st_
                rows.append({"id": f"{day}-{key}-BRK-{t.strftime('%H%M')}", "market": key, "kind": "break", "horizon": "intraday",
                             "bias": "Sell - support block broke" if sd < 0 else "Buy - resistance block broke", "score": None, "side": sd,
                             "status": "active", "created": v["start_hm"], "updated": t.strftime("%H:%M"), "entered_at": t.strftime("%H:%M"),
                             "spot": round(px, 1), "entry": round(px, 1), "entry_px": round(px, 1), "stop": round(stop, 1),
                             "risk_pts": round(abs(px - stop), 1), "exit_epoch": ep + 3600, "exit_by": datetime.fromtimestamp(ep + 3600).strftime("%H:%M"),
                             "entry_level": " + ".join(v["names"][:4]), "entry_block": {"strength": v["strength"], "lo": v["lo"], "hi": v["hi"], "names": v["names"][:5]},
                             "vehicle": (f"Futures {'short' if sd < 0 else 'long'}, or buy ATM {atm} {'PE' if sd < 0 else 'CE'}")
                                        + (" · half size (" + ", ".join(gd["half"]) + ")" if gd.get("half") else ""),
                             "note": "Paper only. Tested 2024-26: Bank Nifty +3.1 / +5.8 bps a trade after futures costs, ~45% winners, ~4 a month.",
                             "guard": gd})
                changed = True
        return changed
    if not (9 * 60 + 30 <= m <= 14 * 60 + 30):
        return changed
    for b in st["bl"] or []:
        side = 1 if prev > b["hi"] >= px else -1 if prev < b["lo"] <= px else 0
        if side:
            st["visit"] = {"side": side, "lo": b["lo"], "hi": b["hi"], "names": b["names"], "strength": b["strength"], "start": ep,
                           "start_m": m, "start_hm": t.strftime("%H:%M"), "pierced": side * (px - (b["lo"] if side > 0 else b["hi"])) < 0}
            break
    return changed


def scorecard(days: int = 30) -> dict:
    t = datetime.now()
    rows = []
    for i in range(days):
        rows += [r for r in load((t - timedelta(days=i)).strftime("%Y-%m-%d")) if r.get("status") == "closed"]
    def agg(keyf):
        g = {}
        for r in rows:
            g.setdefault(keyf(r), []).append(r)
        return [{"group": k, "n": len(v), "win_pct": round(100 * sum(1 for x in v if (x.get("r_net") or 0) > 0) / len(v)),
                 "avg_r_net": round(sum(x.get("r_net") or 0 for x in v) / len(v), 2), "points": round(sum(x.get("points") or 0 for x in v), 1),
                 "verdict": "too early" if len(v) < 30 else "holding" if sum(x.get("r_net") or 0 for x in v) > 0 else "not holding"}
                for k, v in sorted(g.items())]
    return {"closed": len(rows), "by_market": agg(lambda r: r["market"]), "by_bias": agg(lambda r: r["bias"]),
            "by_kind": agg(lambda r: "break trade" if r.get("kind") == "break" else "dip / rise setup"),
            "by_score": agg(lambda r: "break trade" if r.get("kind") == "break" else f"score {abs(r.get('score') or 0)}")}
