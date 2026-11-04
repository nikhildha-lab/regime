"""STRONG BLOCK TOUCH v2 - PAPER ONLY (2026-11-04, Nikhil: "let's optimize it to strong levels ... don't change old logic, add new").
A NEW rule beside the touch-minute, DOM and Touch + DOM records (none of those is changed). Never places orders.

Rule (fixed before the history run, scripts/research/strong_block_touch.py; history 21 Oct - 4 Nov: 8 trades, -Rs 2,002 - too few to judge):
  map     : app/levelmap.build(key, asof = previous session) - STRONG blocks only, fixed for the day (levels_<day>.json),
            shifted from spot to futures by the basis (futures mid - option-chain spot, median of the snapshots from 09:20)
  touch   : a closed 1-min futures bar reaches a strong block's near edge from outside (prev close above -> support, below ->
            resistance); re-touch of the same block only after 15 min; bars 09:30-14:30; one open trade per index
  confirm : touch-minute delta / volume > +0.2 at support, < -0.2 at resistance
  trade   : enter at the touch bar close; stop on a 1-min close 0.05 % beyond the block's FAR edge; target 2R; +1R -> breakeven;
            out after 60 min or at 15:20 (all on 1-min closes)
  money   : synthetic future (buy ATM call + sell ATM put, next-week expiry), 1 lot: Nifty 65 / Bank Nifty 30,
            P&L in rupees after 4.5 / 12.5 pts round trip
Data: the Dhan order-flow engine's per-second file (~/Documents/dhan/orderflow/data/of/<day>/sec_<SYM>.csv) and the option chain.
Judge after 30-40 sessions: positive after costs in both halves. Nothing changes without Nikhil's approval.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime

import pandas as pd

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
OUT = os.path.join(ROOT, "data_store", "livedesk", "strongblock")
OCD = os.path.join(ROOT, "data_store", "flow", "option_chain")
OF = os.path.join(os.environ.get("MP_DHAN_DIR") or os.path.join(os.path.expanduser("~"), "Documents", "dhan"), "orderflow", "data", "of")
INDEX = ("NIFTY", "BANKNIFTY")
LOT = {"NIFTY": 65, "BANKNIFTY": 30}
SYN_PTS = {"NIFTY": 4.5, "BANKNIFTY": 12.5}
TD, FAR_BUF, MIN_R, RETOUCH_S, MAX_MIN = 0.2, 0.0005, 0.0005, 900, 60
VERSION = "v2"
S: dict = {"t": 0}


def _path(name):
    return os.path.join(OUT, name)


def _load(day):
    try:
        return json.load(open(_path(f"trades_{day}.json")))
    except Exception:
        return {"day": day, "trades": [], "last_touch": {}, "done_upto": {}}


def _save(day, st):
    os.makedirs(OUT, exist_ok=True)
    tmp = _path(f"trades_{day}.json") + f".{os.getpid()}.tmp"
    json.dump(st, open(tmp, "w"), indent=1, default=str)
    os.replace(tmp, _path(f"trades_{day}.json"))


def _bars(day, sym):
    """Closed 1-min futures bars from the per-second file: OHLC of the mid, delta = market buys - sells, volume."""
    s = pd.read_csv(os.path.join(OF, day, f"sec_{sym}.csv"), usecols=["t", "bid", "ask", "mid", "buy_vol", "sell_vol"])
    s = s[(s.bid > 0) & (s.ask > 0)].copy()
    s["time"] = pd.to_datetime(s.t, unit="s", utc=True).dt.tz_convert("Asia/Kolkata").dt.strftime("%H:%M")
    s = s[(s.time >= "09:15") & (s.time < "15:30")]
    now_m = datetime.now().strftime("%H:%M")
    s = s[s.time < now_m]                                   # closed minutes only
    g = s.groupby("time")
    return pd.DataFrame({"o": g.mid.first(), "h": g.mid.max(), "l": g.mid.min(), "c": g.mid.last(),
                         "delta": g.buy_vol.sum() - g.sell_vol.sum(), "vol": g.buy_vol.sum() + g.sell_vol.sum()}).reset_index()


def _levels(day, st):
    """Strong blocks for the day, fixed the first time they are read (spot); basis is filled once option-chain snapshots exist."""
    lv = st.get("levels") or {}
    if all(k in lv for k in INDEX):
        return lv
    import levelmap as LM
    prev = sorted(f[:10] for f in os.listdir(OCD) if f.endswith(".csv.gz") and f[:10] < day)
    if not prev:
        return lv
    for k in INDEX:
        if k in lv:
            continue
        m = LM.build(k, asof=prev[-1])
        lv[k] = {"asof": prev[-1], "zones": [{"lo": z["lo"], "hi": z["hi"], "parts": z.get("parts")} for z in m["blocks"] if z.get("strength") == "strong"]}
    st["levels"] = lv
    return lv


def _basis(day, sym, B):
    try:
        o = pd.read_csv(os.path.join(OCD, f"{day}.csv.gz"), usecols=["ts", "symbol", "underlying"])
    except Exception:
        return None
    o = o[o.symbol == sym]; o["time"] = o.ts.str[11:16]
    o = o[(o.time >= "09:20") & (o.time <= "09:40")].groupby("time").underlying.median()
    j = B.set_index("time").c.reindex(o.index).dropna()
    return round(float((j - o.reindex(j.index)).median()), 1) if len(j) else None


def _close(tr, px, hm, why):
    sd = 1 if tr["side"] == "long" else -1
    pts = round(sd * (px - tr["entry"]), 1)
    tr.update(status="closed", exit=round(px, 1), closed=f"{tr['day']} {hm}", exit_reason=why, pts_gross=pts,
              pts_net=round(pts - SYN_PTS[tr["key"]], 1), r=round(pts / tr["risk"], 2) if tr["risk"] else None,
              pnl=round((pts - SYN_PTS[tr["key"]]) * LOT[tr["key"]]))


def _step(day, st, k, B):
    lv = st["levels"][k]
    if lv.get("basis") is None:
        lv["basis"] = _basis(day, k, B)
        if lv["basis"] is None:
            return
    bs = lv["basis"]
    Z = [(z["lo"] + bs, z["hi"] + bs) for z in lv["zones"]]
    upto = st["done_upto"].get(k, "09:15")
    for i in range(1, len(B)):
        b, p = B.iloc[i], B.iloc[i - 1]
        if b.time <= upto:
            continue
        st["done_upto"][k] = b.time
        op = next((t for t in st["trades"] if t["key"] == k and t["status"] == "open"), None)
        if op:                                               # manage on the closed bar: stop, target, breakeven, time
            sd = 1 if op["side"] == "long" else -1
            op["bars"] = op.get("bars", 0) + 1; op["now"] = round(b.c, 1)
            if sd * (b.c - op["sl"]) <= 0:
                _close(op, b.c, b.time, "stop" if op["sl"] == op["stop"] else "breakeven")
            elif sd * (b.c - op["target"]) >= 0:
                _close(op, b.c, b.time, "target")
            elif b.time >= "15:20" or op["bars"] >= MAX_MIN:
                _close(op, b.c, b.time, "60 min" if op["bars"] >= MAX_MIN else "15:20")
            elif sd * (b.c - op["entry"]) >= op["risk"] and sd * (op["sl"] - op["entry"]) < 0:
                op["sl"] = op["entry"]; op["trail"] = "breakeven"
            continue                                         # one open trade per index
        if not ("09:30" <= b.time <= "14:30"):
            continue
        for lo, hi in Z:
            side = 1 if (p.c > hi and b.l <= hi) else -1 if (p.c < lo and b.h >= lo) else 0
            if not side:
                continue
            zk = f"{lo:.1f}-{hi:.1f}"; tb = int(datetime.strptime(f"{day} {b.time}", "%Y-%m-%d %H:%M").timestamp())
            if zk in st["last_touch"] and tb - st["last_touch"][zk] < RETOUCH_S:
                continue
            st["last_touch"][zk] = tb
            td = (b.delta / b.vol if b.vol else 0) * side
            e = float(b.c); stop = lo * (1 - FAR_BUF) if side > 0 else hi * (1 + FAR_BUF); R = side * (e - stop)
            if td > TD and R > e * MIN_R:
                st["trades"].append({"id": f"{k}-{day}-{b.time}", "rule": "strongblock", "version": VERSION, "day": day, "key": k,
                                     "side": "long" if side > 0 else "short", "opened": f"{day} {b.time}", "entry": round(e, 1),
                                     "stop": round(stop, 1), "sl": round(stop, 1), "target": round(e + side * 2 * R, 1), "risk": round(R, 1),
                                     "zone": [round(lo, 1), round(hi, 1)], "basis": bs, "touch_delta": round(td, 2), "status": "open",
                                     "trail": "initial", "lot": LOT[k], "vehicle": "synthetic future (ATM call + short ATM put), 1 lot",
                                     "why": f"strong block {lo:,.0f}-{hi:,.0f} (futures) touched from {'above' if side > 0 else 'below'}; "
                                            f"touch minute {'buyers' if side > 0 else 'sellers'} in control (delta {td:+.2f})"})
            break


def tick(force: bool = False) -> bool:
    """Called from the live-desk loop. Runs at most every 20 s, 09:15-15:35 IST on weekdays."""
    now = datetime.now()
    if not force and (time.time() - S["t"] < 20 or now.weekday() >= 5 or not ("09:16" <= now.strftime("%H:%M") <= "15:35")):
        return False
    S["t"] = time.time()
    day = now.strftime("%Y-%m-%d")
    st = _load(day)
    try:
        _levels(day, st)
    except Exception as e:
        st["err"] = f"levels: {str(e)[:120]}"
    for k in INDEX:
        if k not in (st.get("levels") or {}):
            continue
        try:
            B = _bars(day, k)
        except Exception as e:
            st[f"err_{k}"] = str(e)[:120]; continue
        if len(B) < 2:
            continue
        _step(day, st, k, B)
    st["at"] = now.isoformat(timespec="seconds")
    _save(day, st)
    return True


def trades(days: int = 30) -> list:
    files = sorted(f for f in os.listdir(OUT) if f.startswith("trades_")) if os.path.isdir(OUT) else []
    out = []
    for f in files[-days:]:
        try:
            out += json.load(open(_path(f))).get("trades", [])
        except Exception:
            pass
    return out


def api() -> dict:
    T = trades()
    cl = [t for t in T if t["status"] == "closed"]
    n = len(cl)
    rec = {"n": n} if not n else {"n": n, "win_pct": round(100 * sum(t["pnl"] > 0 for t in cl) / n), "rs": sum(t["pnl"] for t in cl),
                                  "avg_pts_gross": round(sum(t["pts_gross"] for t in cl) / n, 1), "avg_pts_net": round(sum(t["pts_net"] for t in cl) / n, 1)}
    return {"version": VERSION, "open": [t for t in T if t["status"] == "open"], "closed": cl[-20:][::-1], "record": rec,
            "note": "Paper only. Strong blocks (evening-before map) + touch-minute confirm; synthetic future 1 lot, rupees after costs."}
