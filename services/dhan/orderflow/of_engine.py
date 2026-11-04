"""
Order-flow engine (2026-10-17). Read-only: never places orders.

Dhan connections (Dhan allows 5; the Nifty Live Tracker uses 1, this engine 3):
  1. MarketFeed (Full)  NIFTY + BANKNIFTY current futures -> trades (LTP / volume) + 5-level book fallback
  2. FullDepth 20-level NIFTY + BANKNIFTY futures        -> book for imbalance, walls, icebergs
  3. FullDepth 200-level NIFTY futures                   -> deep book for walls and the liquidity heatmap
     (Bank Nifty 200-level / ATM +-5 options come later, once the futures signals look sensible)

Every second: imbalance (5 / 20 / deep), walls, pulled / consumed walls, absorption, icebergs, big prints, delta, CVD.
Every minute: footprint candle + heatmap slice.  At 15:31: stops and writes the day summary (of_eod.py).
Files: data/of/<date>/ sec_<SYM>.csv · events.jsonl · footprint_<SYM>.jsonl · heatmap_<SYM>.csv.gz · trades_<SYM>.csv.gz
       data/of/live.json (latest state, rewritten every 2 s - what the MCP tools and the Intraday persona read)
Also keeps writing data/depth/<date>_summary.csv (same columns as depth_recorder.py) so older readers keep working.
Run: python of_engine.py        (Start-Tracker-LIVE starts it; the Market Pulse health monitor restarts it if it stops)
"""
from __future__ import annotations

import csv
import gzip
import json
import os
import sys
import threading
import time
import traceback
from collections import deque
from datetime import datetime

from common import HERE, connect, current_future
from of_calc import Instrument

STOP_AT, START_AT = "15:31", "09:07"
BUCKET = {"NIFTY": 2.0, "BANKNIFTY": 5.0}
DEEP_200 = ["NIFTY"]
OUT_ROOT = HERE / "data" / "of"
LIVE = OUT_ROOT / "live.json"
_LADDER_HIST = {}     # 2026-11-03: DOM ladder for Market Pulse - per instrument, recent books to show the 10-second change per price


def _ladder(n, I, now, bids=None, asks=None):
    """Top 20 bids / asks as [price, qty, orders, qty change vs ~10 s ago, wall flag]. Wall = qty >= 3x the median level."""
    bids, asks = (list(I.bids[:20]), list(I.asks[:20])) if I is not None else (list(bids or [])[:20], list(asks or [])[:20])
    if not bids or not asks:
        return None
    cur = {("b", p): q for p, q, _ in bids}
    cur.update({("a", p): q for p, q, _ in asks})
    h = _LADDER_HIST.setdefault(n, deque(maxlen=40))
    h.append((now, cur))
    old = next((m for t, m in reversed(h) if now - t >= 10), None)
    qs = sorted(q for _, q, _ in bids + asks)
    med = qs[len(qs) // 2] or 1
    row = lambda side, p, q, o: [p, q, o, None if old is None else q - old.get((side, p), 0), q >= 3 * med]
    return {"t": now, "median_qty": med, "bids": [row("b", p, q, o) for p, q, o in bids], "asks": [row("a", p, q, o) for p, q, o in asks]}
LOCK = threading.Lock()
STATUS = {"feeds": {}, "started": None, "errors": deque(maxlen=20)}


def log(msg):
    line = f"{datetime.now():%H:%M:%S} {msg}"
    print(line, flush=True)
    STATUS["errors"].append(line) if "drop" in msg or "error" in msg.lower() else None


def feed_state(name, state, detail=""):
    STATUS["feeds"][name] = {"state": state, "detail": detail[:160], "at": f"{datetime.now():%H:%M:%S}"}


def run_full_feed(ctx, ids, inst):
    from dhanhq import MarketFeed
    vol_prev = {}
    while datetime.now().strftime("%H:%M") < STOP_AT:
        feed = None
        try:
            feed = MarketFeed(ctx, [(MarketFeed.NSE_FNO, i, MarketFeed.Full) for i in ids], "v2")
            feed_state("trades (Full)", "connected")
            while datetime.now().strftime("%H:%M") < STOP_AT:
                feed.run_forever()
                d = feed.get_data()
                if not d or d.get("type") != "Full Data":
                    continue
                sym = ids[str(d["security_id"])]
                t = time.time()
                ltp, vol = float(d.get("LTP") or 0), int(d.get("volume") or 0)
                dep = d.get("depth") or []
                with LOCK:
                    I = inst[sym]
                    if dep and not I.bids:          # 5-level fallback until the 20-level book arrives
                        I.on_book([(x.get("bid_price"), x.get("bid_quantity"), x.get("bid_orders")) for x in dep],
                                  [(x.get("ask_price"), x.get("ask_quantity"), x.get("ask_orders")) for x in dep], t)
                    pv = vol_prev.get(sym)
                    vol_prev[sym] = vol
                    if pv is not None and vol > pv and ltp > 0:
                        q = vol - pv
                        I.on_trade(ltp, q, t)
                        TRADES.setdefault(sym, []).append((round(t, 3), ltp, q, I.last_side))
        except Exception as e:
            feed_state("trades (Full)", "dropped", str(e))
            log(f"trades feed drop: {str(e)[:150]} - reconnecting in 5s")
            time.sleep(5)
        finally:
            try:
                feed and feed.close_connection()
            except Exception:
                pass


def run_depth(ctx, ids, inst, level, stock_ids=None):
    from dhanhq import FullDepth
    name = f"depth {level}"
    stock_ids = stock_ids or {}
    keys = [(FullDepth.NSE_FNO, i) for i in ids] + [(FullDepth.NSE, i) for i in stock_ids]
    while datetime.now().strftime("%H:%M") < STOP_AT:
        fd = None
        book = {}
        try:
            fd = FullDepth(ctx, keys, level)
            fd.run_forever()
            feed_state(name, "connected")
            while datetime.now().strftime("%H:%M") < STOP_AT:
                data = fd.loop.run_until_complete(fd.ws.recv())
                while data:
                    u = fd.process_data(data)
                    if not u:
                        break
                    data = u.pop("remaining_data", None)
                    sid, seg = str(u["security_id"]), u.get("exchange_segment")
                    book.setdefault((seg, sid), {})[u["type"]] = [(x["price"], x["quantity"], x["orders"]) for x in u["depth"]]
                    b = book[(seg, sid)]
                    if "Bid" in b and "Ask" in b and seg == FullDepth.NSE and sid in stock_ids:
                        STOCK_BOOK[stock_ids[sid]] = (b["Bid"], b["Ask"], time.time())
                    elif "Bid" in b and "Ask" in b and sid in ids:
                        with LOCK:
                            inst[ids[sid]].on_book(b["Bid"], b["Ask"], time.time(), deep=(level == 200))
                if fd.on_close:
                    raise RuntimeError("server closed the feed (806 = subscribe to Data APIs, 807 = token expired)")
        except Exception as e:
            feed_state(name, "dropped", str(e))
            log(f"{name} drop: {str(e)[:150]} - reconnecting in 5s")
            time.sleep(5)
        finally:
            try:
                fd and fd.ws and fd.close_connection()
            except Exception:
                pass


TRADES: dict = {}
# 2026-11-03 (Nikhil: DOM test on stocks): the most liquid F&O stocks ride on the SAME 20-level depth connection as the
# index futures (Dhan allows up to 50 instruments per depth connection; no extra connection, so no 429s). Book only, no trades.
STOCKS = ["RELIANCE", "HDFCBANK", "ICICIBANK", "INFY", "TCS", "SBIN", "AXISBANK", "KOTAKBANK", "LT", "BHARTIARTL", "ITC",
          "BAJFINANCE", "M&M", "MARUTI", "TMPV", "HINDUNILVR", "SUNPHARMA", "TITAN", "ULTRACEMCO", "ADANIENT", "ADANIPORTS",
          "NTPC", "POWERGRID", "ONGC", "COALINDIA", "TATASTEEL", "JSWSTEEL", "HINDALCO", "BAJAJFINSV", "BAJAJ-AUTO",
          "EICHERMOT", "HCLTECH", "WIPRO", "TECHM", "ASIANPAINT", "DRREDDY", "CIPLA", "APOLLOHOSP", "INDUSINDBK", "BEL",
          "HAL", "TRENT", "SHRIRAMFIN", "VEDL", "DLF", "BANKBARODA", "TATAPOWER", "ETERNAL"]
STOCK_BOOK: dict = {}      # sym -> (bids, asks, t)


def main():
    ctx, _ = connect()
    ids, names = {}, {}
    for name in ("NIFTY", "BANKNIFTY"):
        fid, fsym = current_future(name)
        ids[str(fid)] = name
        names[name] = fsym
    inst = {n: Instrument(n, BUCKET[n]) for n in names}
    from common import equity_id
    stock_ids = {}
    for sym in STOCKS[:48]:
        try:
            stock_ids[equity_id(sym)] = sym
        except (SystemExit, Exception):
            pass
    print("Stock books (20 levels):", len(stock_ids), "stocks", flush=True)
    print("Order-flow engine for", names, flush=True)
    while datetime.now().strftime("%H:%M") < START_AT:
        time.sleep(5)
    day = f"{datetime.now():%Y-%m-%d}"
    out = OUT_ROOT / day
    out.mkdir(parents=True, exist_ok=True)
    STATUS["started"] = f"{datetime.now():%Y-%m-%d %H:%M:%S}"
    deep_ids = {k: v for k, v in ids.items() if v in DEEP_200}
    threads = [threading.Thread(target=run_full_feed, args=(ctx, ids, inst), daemon=True),
               threading.Thread(target=run_depth, args=(ctx, ids, inst, 20, stock_ids), daemon=True)]
    for k, v in deep_ids.items():
        threads.append(threading.Thread(target=run_depth, args=(ctx, {k: v}, inst, 200), daemon=True))
    for th in threads:
        th.start()
    sec_files, sec_w = {}, {}
    ev_f = open(out / "events.jsonl", "a")
    fp_f = {n: open(out / f"footprint_{n}.jsonl", "a") for n in inst}
    heat_f = {n: gzip.open(out / f"heatmap_{n}.csv.gz", "at", newline="") for n in inst}
    trade_f = {n: gzip.open(out / f"trades_{n}.csv.gz", "at", newline="") for n in inst}
    depth_dir = HERE / "data" / "depth"
    depth_dir.mkdir(parents=True, exist_ok=True)
    comp_p = depth_dir / f"{day}_summary.csv"
    comp_new = not comp_p.exists()
    comp_f = open(comp_p, "a", newline="")
    comp_w = csv.writer(comp_f)
    if comp_new:
        comp_w.writerow(["time", "symbol", "bid", "ask", "bid_qty5", "ask_qty5", "bid_qty20", "ask_qty20", "imbalance20",
                         "bid_orders20", "ask_orders20", "bid_wall_px", "bid_wall_qty", "ask_wall_px", "ask_wall_qty", "levels_bid", "levels_ask"])
    sd_dir = HERE / "data" / "stocks_depth"
    sd_dir.mkdir(parents=True, exist_ok=True)
    sd_p = sd_dir / f"{day}.csv"
    sd_new = not sd_p.exists()
    sd_f = open(sd_p, "a", newline="")
    sd_w = csv.writer(sd_f)
    if sd_new:
        sd_w.writerow(["time", "symbol", "bid", "ask", "bid_qty20", "ask_qty20", "imbalance20", "bid_orders20", "ask_orders20",
                       "bid_wall_px", "bid_wall_qty", "ask_wall_px", "ask_wall_qty"])
    last_sd = 0
    recent_ev = deque(maxlen=200)
    recent_fp = {n: deque(maxlen=60) for n in inst}
    last_rows = {}
    last_min = int(time.time() // 60)
    last_live = 0
    while datetime.now().strftime("%H:%M") < STOP_AT:
        t0 = time.time()
        now = int(t0)
        try:
            with LOCK:
                for n, I in inst.items():
                    row, ev = I.second(now)
                    last_rows[n] = row
                    if n not in sec_w:
                        p = out / f"sec_{n}.csv"
                        new = not p.exists()
                        sec_files[n] = open(p, "a", newline="")
                        sec_w[n] = csv.DictWriter(sec_files[n], fieldnames=list(row.keys()))
                        if new:
                            sec_w[n].writeheader()
                    sec_w[n].writerow(row)
                    for e in ev:
                        e["time"] = datetime.fromtimestamp(e["t"]).strftime("%H:%M:%S")
                        ev_f.write(json.dumps(e) + "\n")
                        recent_ev.append(e)
                    bw = max(I.bids[:20], key=lambda x: x[1]) if I.bids else (None, None, None)
                    aw = max(I.asks[:20], key=lambda x: x[1]) if I.asks else (None, None, None)
                    comp_w.writerow([datetime.fromtimestamp(now).isoformat(timespec="seconds"), n, row["bid"], row["ask"],
                                     sum(x[1] for x in I.bids[:5]), sum(x[1] for x in I.asks[:5]), row["bid20"], row["ask20"],
                                     row["imb20"], sum((x[2] or 0) for x in I.bids[:20]), sum((x[2] or 0) for x in I.asks[:20]),
                                     bw[0], bw[1], aw[0], aw[1], len(I.bids), len(I.asks)])
                    for tr in TRADES.pop(n, []):
                        trade_f[n].write(",".join(map(str, tr)) + "\n")
                    if now // 60 != last_min:
                        candle, heat = I.minute(now)
                        candle["time"] = datetime.fromtimestamp(now).strftime("%H:%M")
                        fp_f[n].write(json.dumps(candle) + "\n")
                        recent_fp[n].append(candle)
                        csv.writer(heat_f[n]).writerows(heat)
                if now // 60 != last_min:
                    last_min = now // 60
                    for f in list(sec_files.values()) + [ev_f, comp_f] + list(fp_f.values()):
                        f.flush()
            if t0 - last_sd >= 5 and STOCK_BOOK:          # stock books every 5 s (the DOM test on stocks)
                last_sd = t0
                for sym, (bb, aa, ts) in list(STOCK_BOOK.items()):
                    if now - ts > 30 or not bb or not aa:
                        continue
                    b20, a20 = bb[:20], aa[:20]
                    qb, qa = sum(x[1] for x in b20), sum(x[1] for x in a20)
                    bw, aw = max(b20, key=lambda x: x[1]), max(a20, key=lambda x: x[1])
                    sd_w.writerow([datetime.fromtimestamp(now).isoformat(timespec="seconds"), sym, b20[0][0], a20[0][0], qb, qa,
                                   round((qb - qa) / (qb + qa), 3) if qb + qa else None, sum((x[2] or 0) for x in b20),
                                   sum((x[2] or 0) for x in a20), bw[0], bw[1], aw[0], aw[1]])
                sd_f.flush()
            if t0 - last_live >= 2:
                last_live = t0
                snap = {"at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "date": day, "contracts": names,
                        "feeds": STATUS["feeds"], "started": STATUS["started"], "label": "context (untested until ~60 sessions)",
                        "instruments": {n: {"now": last_rows.get(n), "cvd": inst[n].cvd, "ladder": _ladder(n, inst[n], now),
                                            "walls": [{"side": "bid" if s > 0 else "ask", "price": p, "qty": w["qty"],
                                                       "age_s": int(now - w["t0"])} for (s, p), w in sorted(inst[n].walls.items(), key=lambda kv: -kv[1]["qty"])[:8]],
                                            "absorption_counts": {f"{'sell-side' if s > 0 else 'buy-side'} {p}": c for (s, p), c in inst[n].abs_count.items()},
                                            "footprint_last": list(recent_fp[n])[-15:],
                                            "footprint_now": dict(inst[n].peek(now) or {}, time=datetime.fromtimestamp(now).strftime("%H:%M")) if inst[n].peek(now) else None} for n in inst},
                        "events": list(recent_ev)[-60:],
                        "stocks": {sym: _ladder("S:" + sym, None, now, bb, aa) for sym, (bb, aa, ts) in list(STOCK_BOOK.items()) if now - ts <= 30}}
                tmp = str(LIVE) + ".tmp"
                with open(tmp, "w") as f:
                    json.dump(snap, f, default=str)
                os.replace(tmp, LIVE)
        except Exception:
            log("engine error: " + traceback.format_exc().splitlines()[-1])
        time.sleep(max(0.05, 1.0 - (time.time() - t0)))
    for f in list(sec_files.values()) + [ev_f, comp_f] + list(fp_f.values()) + list(heat_f.values()) + list(trade_f.values()):
        try:
            f.close()
        except Exception:
            pass
    print("Session over - writing the day summary ...", flush=True)
    try:
        import of_eod
        of_eod.run(day)
    except Exception as e:
        print("day summary failed:", e, flush=True)


if __name__ == "__main__":
    sys.exit(main())
