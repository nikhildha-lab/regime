"""
20-level order book recorder (NIFTY + BANKNIFTY current-month futures). Added 2026-10-15.
Runs alongside recorder.py (does not touch it). Start before 9:15, stops itself at 15:31.

Every second, for each future, saves:
  data/depth/YYYY-MM-DD_summary.csv   best bid/ask, total bid vs ask qty over 5 and 20 levels, imbalance,
                                      orders, the biggest bid / ask "wall" (price and qty)
  data/depth/YYYY-MM-DD_levels.csv.gz all 20 bid + 20 ask levels (price, qty, orders) for later study
Read-only market data: never places orders.

Run:   python depth_recorder.py
"""

import csv
import gzip
import time
from datetime import datetime

from dhanhq import FullDepth

from common import HERE, connect, current_future

STOP_AT = "15:31"
START_AT = "09:14"
SUMMARY = ["time", "symbol", "bid", "ask", "bid_qty5", "ask_qty5", "bid_qty20", "ask_qty20", "imbalance20",
           "bid_orders20", "ask_orders20", "bid_wall_px", "bid_wall_qty", "ask_wall_px", "ask_wall_qty", "levels_bid", "levels_ask"]


def summarise(bids, asks):
    bids = sorted([b for b in bids if b["price"] > 0], key=lambda x: -x["price"])
    asks = sorted([a for a in asks if a["price"] > 0], key=lambda x: x["price"])
    if not bids or not asks:
        return None
    q = lambda xs, n: sum(x["quantity"] for x in xs[:n])
    b20, a20 = q(bids, 20), q(asks, 20)
    bw, aw = max(bids, key=lambda x: x["quantity"]), max(asks, key=lambda x: x["quantity"])
    return [bids[0]["price"], asks[0]["price"], q(bids, 5), q(asks, 5), b20, a20,
            round((b20 - a20) / (b20 + a20), 4) if b20 + a20 else 0,
            sum(x["orders"] for x in bids[:20]), sum(x["orders"] for x in asks[:20]),
            bw["price"], bw["quantity"], aw["price"], aw["quantity"], len(bids), len(asks)], bids, asks


def main():
    ctx, _ = connect()
    insts, names = [], {}
    for name in ("NIFTY", "BANKNIFTY"):
        fid, fsym = current_future(name)
        insts.append((FullDepth.NSE_FNO, fid))
        names[int(fid)] = fsym
    print("20-level depth for:", ", ".join(names.values()))
    while datetime.now().strftime("%H:%M") < START_AT:
        time.sleep(5)

    out = HERE / "data" / "depth"
    out.mkdir(parents=True, exist_ok=True)
    day = f"{datetime.now():%Y-%m-%d}"
    sp, lp = out / f"{day}_summary.csv", out / f"{day}_levels.csv.gz"
    new_s, new_l = not sp.exists(), not lp.exists()
    fs, fl = open(sp, "a", newline=""), gzip.open(lp, "at", newline="")
    ws, wl = csv.writer(fs), csv.writer(fl)
    if new_s:
        ws.writerow(SUMMARY)
    if new_l:
        wl.writerow(["time", "symbol", "side", "level", "price", "qty", "orders"])

    book, last_saved, rows = {}, {}, 0
    while datetime.now().strftime("%H:%M") < STOP_AT:
        fd = None
        try:
            fd = FullDepth(ctx, insts, 20)
            fd.run_forever()                                  # connect + subscribe
            while datetime.now().strftime("%H:%M") < STOP_AT:
                data = fd.loop.run_until_complete(fd.ws.recv())
                while data:
                    u = fd.process_data(data)
                    if not u:
                        break
                    data = u.pop("remaining_data", None)
                    sid = int(u["security_id"])
                    book.setdefault(sid, {})[u["type"]] = u["depth"]
                if fd.on_close:
                    raise RuntimeError("server closed the depth feed (see message above)")
                now = datetime.now()
                sec = now.strftime("%H:%M:%S")
                for sid, bk in book.items():
                    if "Bid" not in bk or "Ask" not in bk or last_saved.get(sid) == sec:
                        continue
                    r = summarise(bk["Bid"], bk["Ask"])
                    if not r:
                        continue
                    s, bids, asks = r
                    t = now.isoformat(timespec="seconds")
                    ws.writerow([t, names.get(sid, sid)] + s)
                    for side, lv in (("B", bids), ("A", asks)):
                        for i, x in enumerate(lv[:20], 1):
                            wl.writerow([t, names.get(sid, sid), side, i, x["price"], x["quantity"], x["orders"]])
                    last_saved[sid] = sec
                    rows += 1
                    if rows % 600 == 0:
                        fs.flush()
                        print(f"{now:%H:%M:%S}  {rows} depth snapshots saved")
        except KeyboardInterrupt:
            break
        except Exception as e:
            print("Depth feed dropped:", str(e)[:200], "- reconnecting in 5s")
            time.sleep(5)
        finally:
            try:
                if fd is not None and fd.ws is not None:
                    fd.close_connection()
            except Exception:
                pass
    fs.close()
    fl.close()
    print(f"Stopped. {rows} snapshots saved to {sp}")


if __name__ == "__main__":
    main()
