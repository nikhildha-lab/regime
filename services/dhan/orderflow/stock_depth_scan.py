"""
Order-book scan for liquid Indian stocks (2026-10-23, Nikhil). Read-only market data: never places orders.
One Dhan connection (the 5th: tracker 1 + of_engine 3 + this 1): 20-level FullDepth for up to 50 NSE cash stocks.
Every second per stock -> data/stocks_depth/YYYY-MM-DD_summary.csv (same columns as the index depth summary).
Every 5 s -> data/stocks_depth/live.json: book lean (20 levels, now / 60-s average / 60-s change), biggest bid and ask walls
(price, size, x the median level size), wall events in the last 10 min (a wall >= 5x median appears / is pulled), mid price.
At the stop (15:31) -> data/stocks_depth/signal_stats.csv: per stock and day, does a strong lean or a wall event lead the next
5 / 15 minutes? (the same test as the index study, so an edge has to show up day after day before anyone uses it).
Started automatically by Market Pulse in NSE hours. Run by hand:  ~/Documents/dhan/venv/bin/python stock_depth_scan.py
"""
import csv, json, time, statistics
from collections import deque, defaultdict
from datetime import datetime
import pandas as pd
from dhanhq import FullDepth
from common import HERE, connect

STOP_AT, START_AT = "15:31", "09:14"
UNIVERSE = ["RELIANCE", "HDFCBANK", "ICICIBANK", "INFY", "TCS", "SBIN", "AXISBANK", "KOTAKBANK", "LT", "BHARTIARTL", "ITC",
            "HINDUNILVR", "BAJFINANCE", "BAJAJFINSV", "MARUTI", "M&M", "SUNPHARMA", "TITAN", "ADANIENT", "ADANIPORTS", "NTPC",
            "POWERGRID", "TATASTEEL", "JSWSTEEL", "HCLTECH", "WIPRO", "TECHM", "ULTRACEMCO", "ASIANPAINT", "NESTLEIND",
            "ONGC", "COALINDIA", "HINDALCO", "INDUSINDBK", "BAJAJ-AUTO", "EICHERMOT", "HEROMOTOCO", "DRREDDY", "CIPLA",
            "BEL", "HAL", "TRENT", "ZOMATO", "ETERNAL", "TATAMOTORS", "TMPV", "DLF", "VEDL", "BPCL", "SHRIRAMFIN"]
OUT = HERE / "data" / "stocks_depth"
SUMMARY = ["time", "symbol", "bid", "ask", "bid_qty5", "ask_qty5", "bid_qty20", "ask_qty20", "imbalance20",
           "bid_orders20", "ask_orders20", "bid_wall_px", "bid_wall_qty", "ask_wall_px", "ask_wall_qty", "levels_bid", "levels_ask"]


def universe():
    s = pd.read_csv(HERE / "data" / "scrip_master.csv", low_memory=False)
    e = s[(s.SEM_EXM_EXCH_ID == "NSE") & (s.SEM_SEGMENT == "E") & (s.SEM_SERIES == "EQ")]
    ids = {}
    for sym in UNIVERSE:
        r = e[e.SEM_TRADING_SYMBOL == sym]
        if len(r):
            ids[int(r.iloc[0].SEM_SMST_SECURITY_ID)] = sym
    return dict(list(ids.items())[:50])


def summarise(bids, asks):
    bids = sorted([b for b in bids if b["price"] > 0], key=lambda x: -x["price"])
    asks = sorted([a for a in asks if a["price"] > 0], key=lambda x: x["price"])
    if not bids or not asks:
        return None
    q = lambda xs, n: sum(x["quantity"] for x in xs[:n])
    b20, a20 = q(bids, 20), q(asks, 20)
    bw, aw = max(bids, key=lambda x: x["quantity"]), max(asks, key=lambda x: x["quantity"])
    med = statistics.median([x["quantity"] for x in bids[:20] + asks[:20]]) or 1
    row = [bids[0]["price"], asks[0]["price"], q(bids, 5), q(asks, 5), b20, a20, round((b20 - a20) / (b20 + a20), 4) if b20 + a20 else 0,
           sum(x["orders"] for x in bids[:20]), sum(x["orders"] for x in asks[:20]), bw["price"], bw["quantity"], aw["price"], aw["quantity"],
           len(bids), len(asks)]
    return row, med


def main():
    ctx, _ = connect()
    names = universe()
    insts = [(FullDepth.NSE, sid) for sid in names]
    print(f"order-book scan: {len(names)} stocks")
    while datetime.now().strftime("%H:%M") < START_AT:
        time.sleep(5)
    OUT.mkdir(parents=True, exist_ok=True)
    day = f"{datetime.now():%Y-%m-%d}"
    sp = OUT / f"{day}_summary.csv"; new = not sp.exists()
    fs = open(sp, "a", newline=""); ws = csv.writer(fs)
    if new:
        ws.writerow(SUMMARY)
    book, last_saved = {}, {}
    hist = defaultdict(lambda: deque(maxlen=900))          # (t, mid, imb20) per stock, last 15 min
    walls = defaultdict(dict); events = deque(maxlen=400); last_live = 0
    while datetime.now().strftime("%H:%M") < STOP_AT:
        fd = None
        try:
            fd = FullDepth(ctx, insts, 20)
            fd.run_forever()
            while datetime.now().strftime("%H:%M") < STOP_AT:
                data = fd.loop.run_until_complete(fd.ws.recv())
                while data:
                    u = fd.process_data(data)
                    if not u:
                        break
                    data = u.pop("remaining_data", None)
                    book.setdefault(int(u["security_id"]), {})[u["type"]] = u["depth"]
                if fd.on_close:
                    raise RuntimeError("server closed the depth feed")
                now = datetime.now(); sec = now.strftime("%H:%M:%S"); ts = now.timestamp()
                for sid, bk in book.items():
                    if "Bid" not in bk or "Ask" not in bk or last_saved.get(sid) == sec:
                        continue
                    r = summarise(bk["Bid"], bk["Ask"])
                    if not r:
                        continue
                    row, med = r; sym = names.get(sid, str(sid))
                    ws.writerow([now.isoformat(timespec="seconds"), sym] + row); last_saved[sid] = sec
                    mid = (row[0] + row[1]) / 2; hist[sym].append((ts, mid, row[6]))
                    # wall events: a level >= 5x the median level size appears / disappears (price within 0.5%)
                    for side, px, qty in (("bid", row[9], row[10]), ("ask", row[11], row[12])):
                        big = qty >= 5 * med and abs(px / mid - 1) <= 0.005
                        prev = walls[sym].get(side)
                        if big and (not prev or prev[0] != px):
                            events.append({"t": sec, "ts": ts, "sym": sym, "kind": "wall_new", "side": side, "px": px, "x": round(qty / med, 1), "mid": mid})
                            walls[sym][side] = (px, qty)
                        elif not big and prev:
                            gone = (side == "bid" and mid > prev[0]) or (side == "ask" and mid < prev[0])
                            events.append({"t": sec, "ts": ts, "sym": sym, "kind": "wall_pulled" if gone else "wall_consumed", "side": side, "px": prev[0], "mid": mid})
                            walls[sym].pop(side, None)
                    bk["_med"], bk["_row"] = med, row
                if ts - last_live >= 5:
                    write_live(names, book, hist, events, now); last_live = ts
                    fs.flush()
        except KeyboardInterrupt:
            break
        except Exception as e:
            print("depth feed dropped:", str(e)[:200], "- reconnecting in 5s"); time.sleep(5)
        finally:
            try:
                if fd is not None and fd.ws is not None:
                    fd.close_connection()
            except Exception:
                pass
    fs.close()
    eod(sp, day)


def write_live(names, book, hist, events, now):
    rows = []
    for sid, sym in names.items():
        bk = book.get(sid) or {}; row = bk.get("_row"); h = hist.get(sym)
        if not row or not h:
            continue
        ts_now = h[-1][0]; last60 = [x for x in h if x[0] >= ts_now - 60]; old = [x for x in h if x[0] <= ts_now - 60]
        avg60 = sum(x[2] for x in last60) / len(last60)
        chg60 = row[6] - old[-1][2] if old else None
        ret15 = (h[-1][1] / h[0][1] - 1) * 100 if h[0][1] else None
        med = bk.get("_med") or 1; mid = (row[0] + row[1]) / 2
        ev = [e for e in events if e["sym"] == sym and e["ts"] >= ts_now - 600]
        rows.append({"sym": sym, "mid": round(mid, 2), "spread_bps": round((row[1] - row[0]) / mid * 1e4, 1), "imb20": row[6],
                     "imb20_avg60": round(avg60, 3), "imb20_chg60": None if chg60 is None else round(chg60, 3), "ret_15m_pct": None if ret15 is None else round(ret15, 2),
                     "bid_wall": {"px": row[9], "x": round(row[10] / med, 1), "dist_bps": round((mid - row[9]) / mid * 1e4, 1)},
                     "ask_wall": {"px": row[11], "x": round(row[12] / med, 1), "dist_bps": round((row[11] - mid) / mid * 1e4, 1)},
                     "events_10m": [{k: e[k] for k in ("t", "kind", "side", "px")} for e in ev][-6:],
                     "lean": "bids heavy" if avg60 >= 0.3 else "asks heavy" if avg60 <= -0.3 else "balanced"})
    rows.sort(key=lambda r: -abs(r["imb20_avg60"]))
    tmp = OUT / "live.json.tmp"
    json.dump({"at": now.strftime("%Y-%m-%d %H:%M:%S"), "n": len(rows), "label": "context only - untested until ~20 sessions", "stocks": rows}, open(tmp, "w"))
    tmp.replace(OUT / "live.json")


def eod(sp, day):
    """Per stock: does a strong 60-s lean (|avg| >= 0.3) lead the next 5 / 15 minutes? (bps of mid, one signal per 5 min)"""
    try:
        d = pd.read_csv(sp, parse_dates=["time"]); d = d[d.bid.notna() & d.ask.notna()]
        out = []
        for sym, g in d.groupby("symbol"):
            g = g.set_index("time").sort_index(); g = g[~g.index.duplicated()].resample("1s").last().ffill(limit=5).dropna(subset=["bid"])
            mid = (g.bid + g.ask) / 2; avg = g.imbalance20.rolling(60, min_periods=30).mean(); spr = ((g.ask - g.bid) / mid * 1e4).median()
            for h in (300, 900):
                f = (mid.shift(-h) / mid - 1) * 1e4
                sig = avg.where(avg.abs() >= 0.3).dropna(); sig = sig[~sig.index.floor("5min").duplicated()]
                if len(sig) < 5:
                    continue
                pnl = (sig.apply(lambda v: 1 if v > 0 else -1) * f.reindex(sig.index)).dropna()
                out.append({"date": day, "sym": sym, "horizon_min": h // 60, "n": len(pnl), "avg_bps": round(pnl.mean(), 2),
                            "hit": round((pnl > 0).mean(), 3), "median_spread_bps": round(spr, 2)})
        p = OUT / "signal_stats.csv"; new = not p.exists()
        pd.DataFrame(out).to_csv(p, mode="a", header=new, index=False)
        print(f"day stats written for {len(set(o['sym'] for o in out))} stocks")
    except Exception as e:
        print("eod stats failed:", e)


if __name__ == "__main__":
    main()
