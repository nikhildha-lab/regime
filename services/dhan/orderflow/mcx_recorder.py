"""
MCX order-flow recorder (2026-11-05, Nikhil: "Yes" to recording live MCX order flow). Read-only: never places orders.
A separate process so the index / stock order-flow engine (of_engine.py) is not touched.

Dhan connection: ONE MarketFeed (Full) - the 5th of Dhan's 5 (tracker 1 + of_engine 3). Full packets carry last trade, total
volume and the 5-level book. Front-month futures (nearest expiry at least 3 days away) of:
  GOLDM, SILVERM, CRUDEOILM, NATGASMINI, COPPER, ALUMINIUM, ZINC
Trades come from volume steps between packets; side = at / above the ask -> buy, at / below the bid -> sell, else tick rule.
Every second, per contract: data/mcx/<date>/sec_<UNDERLYING>.csv
    t, bid, ask, mid, last, buy_vol, sell_vol, imb5, bidq5, askq5      (imb5 = (bid qty - ask qty) / total, 5 levels)
Status every 5 s: data/mcx/live.json.  Session 15:40-23:58 IST (evening only since 2026-11-06); outside it the process sleeps.
Run: <dhan venv>/bin/python -u mcx_recorder.py   (Market Pulse's live desk starts it in MCX hours if it is not running)
"""
from __future__ import annotations

import csv
import json
import os
import threading
import time
from datetime import datetime, timedelta

import pandas as pd

from common import HERE, connect, scrip_master

UNDER = ["GOLDM", "SILVERM", "CRUDEOILM", "NATGASMINI", "COPPER", "ALUMINIUM", "ZINC"]
OUT_ROOT = HERE / "data" / "mcx"
# 2026-11-06 (Nikhil: "record mcx in the evening"): 08:58 start failed all day - during NSE hours Dhan closed the MCX socket at once
# (connections in use by the tracker + order-flow engine) and the 10 s retries drew HTTP 429s. Now: evening session only, from 15:40
# (the order-flow engine stops at 15:31), and only once the engine's live.json has gone quiet; retries back off 10 s -> 5 min.
START_AT, STOP_AT = "15:40", "23:58"
OF_LIVE = HERE / "data" / "of" / "live.json"


def _nse_busy():
    """True while the NSE order-flow engine is still writing (it holds Dhan connections)."""
    try:
        return time.time() - os.path.getmtime(OF_LIVE) < 90
    except OSError:
        return False
LOCK = threading.Lock()
ST = {"feed": "starting", "detail": "", "contracts": {}, "last_tick": {}, "started": None}
BOOK, LAST, AGG = {}, {}, {}


def _f(x) -> float:
    try:
        return float(x or 0)
    except (TypeError, ValueError):
        return 0.0


def front_months():
    s = scrip_master()
    f = s[(s["SEM_EXM_EXCH_ID"] == "MCX") & (s["SEM_INSTRUMENT_NAME"] == "FUTCOM") & (s["SM_SYMBOL_NAME"].isin(UNDER))].copy()
    f["expiry"] = pd.to_datetime(f["SEM_EXPIRY_DATE"])
    f = f[f["expiry"] >= pd.Timestamp.now().normalize() + pd.Timedelta(days=3)].sort_values("expiry")
    out = {}
    for u, g in f.groupby("SM_SYMBOL_NAME"):
        r = g.iloc[0]
        out[str(int(r["SEM_SMST_SECURITY_ID"]))] = (u, r["SEM_TRADING_SYMBOL"])
    return out


def feed_loop(ctx, ids):
    from dhanhq import MarketFeed
    vol_prev = {}
    wait = 10
    while datetime.now().strftime("%H:%M") < STOP_AT:
        feed = None
        try:
            feed = MarketFeed(ctx, [(MarketFeed.MCX, i, MarketFeed.Full) for i in ids], "v2")
            ST.update(feed="connected", detail="")
            got = False
            while datetime.now().strftime("%H:%M") < STOP_AT:
                feed.run_forever()
                d = feed.get_data()
                if not d or d.get("type") != "Full Data":
                    continue
                key = str(d.get("security_id"))
                if key not in ids:
                    continue
                u = ids[key][0]
                ltp, vol = _f(d.get("LTP")), int(_f(d.get("volume")))
                dep = d.get("depth") or []
                with LOCK:
                    if dep:                                     # prices / quantities can arrive as text - keep numbers only
                        bids = [(_f(x.get("bid_price")), _f(x.get("bid_quantity"))) for x in dep]
                        asks = [(_f(x.get("ask_price")), _f(x.get("ask_quantity"))) for x in dep]
                        bids = [x for x in bids if x[0] > 0]; asks = [x for x in asks if x[0] > 0]
                        if bids and asks:
                            BOOK[u] = (bids, asks)
                    pv = vol_prev.get(u); vol_prev[u] = vol
                    if pv is not None and vol > pv and ltp > 0:
                        q = vol - pv
                        b = BOOK.get(u)
                        bid = b[0][0][0] if b and b[0] else None
                        ask = b[1][0][0] if b and b[1] else None
                        if ask and ltp >= ask:
                            side = 1
                        elif bid and ltp <= bid:
                            side = -1
                        else:
                            lp = LAST.get(u, (ltp, 1))
                            side = 1 if ltp > lp[0] else -1 if ltp < lp[0] else lp[1]
                        a = AGG.setdefault(u, [0, 0])
                        a[0 if side > 0 else 1] += q
                        LAST[u] = (ltp, side)
                    elif ltp > 0 and u not in LAST:
                        LAST[u] = (ltp, 1)
                    ST["last_tick"][u] = datetime.now().strftime("%H:%M:%S")
                if not got:
                    got = True; wait = 10                       # data flowing again - reset the back-off
        except Exception as e:
            ST.update(feed="dropped", detail=str(e)[:160])
            print(f"{datetime.now():%H:%M:%S} feed drop: {str(e)[:150]} - reconnecting in {wait}s", flush=True)
            time.sleep(wait)
            wait = min(300, wait * 2)
        finally:
            try:
                feed and feed.close_connection()
            except Exception:
                pass


def main():
    while True:
        now = datetime.now()
        if now.weekday() >= 5 or not (START_AT <= now.strftime("%H:%M") < STOP_AT) or _nse_busy():
            ST.update(feed="closed", detail="waiting for the evening session (15:40-23:58, after the NSE engine stops)")
            write_live()
            time.sleep(60)
            continue
        run_day()


def write_live():
    OUT_ROOT.mkdir(parents=True, exist_ok=True)
    tmp = OUT_ROOT / f"live.json.{os.getpid()}.tmp"
    with LOCK:
        snap = {"at": datetime.now().isoformat(timespec="seconds"), **{k: v for k, v in ST.items()},
                "books": {u: {"bid": b[0][0] if b[0] else None, "ask": b[1][0] if b[1] else None} for u, b in BOOK.items()}}
    json.dump(snap, open(tmp, "w"), default=str)
    os.replace(tmp, OUT_ROOT / "live.json")


def run_day():
    ctx, _ = connect()
    ids = front_months()
    ST["contracts"] = {u: sym for u, sym in ids.values()}
    ST["started"] = datetime.now().isoformat(timespec="seconds")
    print("MCX recorder:", ST["contracts"], flush=True)
    day = f"{datetime.now():%Y-%m-%d}"
    out = OUT_ROOT / day
    out.mkdir(parents=True, exist_ok=True)
    th = threading.Thread(target=feed_loop, args=(ctx, ids), daemon=True)
    th.start()
    files, writers = {}, {}
    for u, _ in ids.values():
        p = out / f"sec_{u}.csv"
        new = not p.exists()
        files[u] = open(p, "a", newline="")
        writers[u] = csv.writer(files[u])
        if new:
            writers[u].writerow(["t", "bid", "ask", "mid", "last", "buy_vol", "sell_vol", "imb5", "bidq5", "askq5"])
    n = 0
    while datetime.now().strftime("%H:%M") < STOP_AT and f"{datetime.now():%Y-%m-%d}" == day:
        time.sleep(1 - (time.time() % 1))
        t = int(time.time())
        with LOCK:
            for u, w in writers.items():
                b = BOOK.get(u)
                if not b or not b[0] or not b[1]:
                    continue
                bid, ask = b[0][0][0], b[1][0][0]
                qb, qa = sum(x[1] or 0 for x in b[0]), sum(x[1] or 0 for x in b[1])
                a = AGG.pop(u, [0, 0])
                w.writerow([t, bid, ask, round((bid + ask) / 2, 2) if bid and ask else "", LAST.get(u, ("", 0))[0], a[0], a[1],
                            round((qb - qa) / (qb + qa), 3) if qb + qa else "", qb, qa])
        n += 1
        if n % 5 == 0:
            for f in files.values():
                f.flush()
            write_live()
    for f in files.values():
        f.close()


if __name__ == "__main__":
    main()
