"""
Live tick recorder - run every trading day (start before 9:15, stops itself at 15:31).

Records NIFTY + BANKNIFTY current-month futures (and NIFTYBEES / BANKBEES).
Each row = one update with price, traded qty since last update, best bid/ask,
and a buy/sell tag:
    traded at/above previous ask -> BUY
    traded at/below previous bid -> SELL
    in between                   -> same as last price move (tick rule)

Files: data/ticks/YYYY-MM-DD.csv     These become real footprint data later.

Run:   python recorder.py
"""

import csv
import time
from datetime import datetime
from pathlib import Path

from dhanhq import MarketFeed

import config as C
from common import HERE, connect, current_future, equity_id

STOP_AT = "15:31"
COLS = ["time", "symbol", "ltp", "qty", "side", "cum_volume", "bid", "bid_qty",
        "ask", "ask_qty", "oi", "total_buy_qty", "total_sell_qty"]


def build_instruments():
    insts, names = [], {}
    for name, m in C.MARKETS.items():
        fid, fsym = current_future(name)
        insts.append((MarketFeed.NSE_FNO, fid, MarketFeed.Full))
        names[fid] = fsym
        bid = equity_id(m["signal_symbol"])
        insts.append((MarketFeed.NSE, bid, MarketFeed.Full))
        names[bid] = m["signal_symbol"]
    return insts, names


def classify(ltp, prev, last_side):
    if prev is None:
        return "NA"
    if prev["ask"] and ltp >= prev["ask"]:
        return "BUY"
    if prev["bid"] and ltp <= prev["bid"]:
        return "SELL"
    if ltp > prev["ltp"]:
        return "BUY"
    if ltp < prev["ltp"]:
        return "SELL"
    return last_side or "NA"


def main():
    ctx, _ = connect()
    insts, names = build_instruments()
    print("Recording:", ", ".join(names.values()))

    out_dir = HERE / C.TICKS_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{datetime.now():%Y-%m-%d}.csv"
    new = not path.exists()
    f = open(path, "a", newline="")
    w = csv.writer(f)
    if new:
        w.writerow(COLS)

    state, last_side, rows = {}, {}, 0
    while datetime.now().strftime("%H:%M") < STOP_AT:
        feed = MarketFeed(ctx, insts, "v2")
        try:
            while datetime.now().strftime("%H:%M") < STOP_AT:
                feed.run_forever()
                t = feed.get_data()
                if not t or t.get("type") != "Full Data":
                    continue
                sid = str(t["security_id"])
                ltp, vol = float(t["LTP"]), int(t.get("volume") or 0)
                d0 = (t.get("depth") or [{}])[0]
                bid, ask = float(d0.get("bid_price") or 0), float(d0.get("ask_price") or 0)
                prev = state.get(sid)
                qty = vol - prev["vol"] if prev else 0
                if prev and qty <= 0:          # quote change only, no trade
                    state[sid].update(bid=bid, ask=ask)
                    continue
                side = classify(ltp, prev, last_side.get(sid))
                last_side[sid] = side
                state[sid] = {"ltp": ltp, "vol": vol, "bid": bid, "ask": ask}
                w.writerow([datetime.now().isoformat(timespec="milliseconds"), names.get(sid, sid),
                            ltp, qty, side, vol, bid, d0.get("bid_quantity"), ask, d0.get("ask_quantity"),
                            t.get("OI"), t.get("total_buy_quantity"), t.get("total_sell_quantity")])
                rows += 1
                if rows % 500 == 0:
                    f.flush()
                    print(f"{datetime.now():%H:%M:%S}  {rows} trades recorded")
        except KeyboardInterrupt:
            break
        except Exception as e:
            print("Feed dropped:", e, "- reconnecting in 5s")
            time.sleep(5)
        finally:
            try:
                feed.close_connection()
            except Exception:
                pass
    f.close()
    print(f"Stopped. {rows} trades saved to {path}")


if __name__ == "__main__":
    main()
