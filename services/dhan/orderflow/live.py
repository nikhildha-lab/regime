"""
Paper trading engine - runs the 4 setups on LIVE Dhan ticks with REAL order flow.

  * Levels (POC / VAH / VAL / prev high-low / IB) come from the NIFTY & BANKNIFTY
    futures 1-min bars (yesterday from Dhan, today built live from ticks).
  * Delta is REAL: every trade is tagged buy/sell from the live bid/ask.
  * Signals use the same rules as the backtest (setups.py).
  * Trades are PAPER ONLY - nothing is sent to the exchange. Fills at the next tick,
    exit at stop / target / 15:15, same limits as the backtest.
  * Everything is logged to data/paper/YYYY-MM-DD_signals.csv and _trades.csv.

Run on a trading day (start any time before or during market hours):
    python live.py
Replay a recorded day instead of the live feed (for testing):
    python live.py --replay data/ticks/2026-10-15.csv
"""

import csv
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

import config as C
import engine
import setups
from common import HERE, connect, current_future

PAPER_DIR = HERE / "data" / "paper"
STOP_AT = "15:31"
START_AT = "09:12"


# ---------------- tick source ----------------
def live_ticks(ctx, insts):
    from dhanhq import MarketFeed
    while datetime.now().strftime("%H:%M") < STOP_AT:
        feed = MarketFeed(ctx, insts, "v2")
        try:
            while datetime.now().strftime("%H:%M") < STOP_AT:
                feed.run_forever()
                t = feed.get_data()
                if t and t.get("type") == "Full Data":
                    d0 = (t.get("depth") or [{}])[0]
                    yield (datetime.now(), str(t["security_id"]), float(t["LTP"]), int(t.get("volume") or 0),
                           float(d0.get("bid_price") or 0), float(d0.get("ask_price") or 0))
        except KeyboardInterrupt:
            return
        except Exception as e:
            print("Feed dropped:", e, "- reconnecting in 10s")
            time.sleep(10)
        finally:
            try:
                feed.close_connection()
            except Exception:
                pass


def replay_ticks(path, names):
    sym_to_id = {v: k for k, v in names.items()}
    with open(path) as f:
        for r in csv.DictReader(f):
            sid = sym_to_id.get(r["symbol"])
            if sid is None:
                continue
            yield (datetime.fromisoformat(r["time"]), sid, float(r["ltp"]), int(float(r["cum_volume"])),
                   float(r["bid"] or 0), float(r["ask"] or 0))


# ---------------- one index ----------------
class Tracker:
    def __init__(self, name, m, dhan, sid, sym):
        self.name, self.m, self.sym = name, m, sym
        self.prev, self.last_side = None, "NA"
        self.bars = []          # finished 1-min bars (dicts)
        self.cur = None         # bar being built
        self.open_trade = None
        self.trades, self.seen = [], set()
        self.lv = self.yesterday_levels(dhan, sid)
        print(f"{name}: {sym} | yesterday POC {self.lv['p_poc']:.1f}  VAH {self.lv['p_vah']:.1f}  VAL {self.lv['p_val']:.1f}")

    def yesterday_levels(self, dhan, sid):
        end = datetime.now().date()
        r = dhan.intraday_minute_data(sid, "NSE_FNO", "FUTIDX", f"{end - timedelta(days=7)} 09:15:00", f"{end} 09:14:00", 1)
        if r.get("status") != "success" or not r["data"].get("timestamp"):
            sys.exit(f"{self.name}: could not load yesterday's bars: {r.get('remarks')}")
        d = r["data"]
        df = pd.DataFrame({k: d[k] for k in ("open", "high", "low", "close", "volume")})
        df["ts"] = pd.to_datetime(pd.Series(d["timestamp"]), unit="s", utc=True).dt.tz_convert("Asia/Kolkata").dt.tz_localize(None)
        df["date"] = df["ts"].dt.date
        df = df[df["date"] < end]
        if df.empty:
            sys.exit(f"{self.name}: no bars for a previous day (holiday run?)")
        g = df[df["date"] == df["date"].max()]
        poc, vah, val = engine.profile(g, g["close"].iloc[0] * C.PROFILE_BIN_PCT)
        return {"p_poc": poc, "p_vah": vah, "p_val": val, "p_high": g["high"].max(),
                "p_low": g["low"].min(), "p_close": g["close"].iloc[-1]}

    # --- ticks -> 1-min bars with real delta ---
    def on_tick(self, ts, ltp, vol, bid, ask):
        prev = self.prev
        qty = vol - prev["vol"] if prev else 0
        if prev is None or qty > 0:
            side = "NA"
            if prev:
                if ask and ltp >= prev["ask"]:
                    side = "BUY"
                elif bid and ltp <= prev["bid"]:
                    side = "SELL"
                elif ltp != prev["ltp"]:
                    side = "BUY" if ltp > prev["ltp"] else "SELL"
                else:
                    side = self.last_side
                self.last_side = side
            self.prev = {"ltp": ltp, "vol": vol, "bid": bid, "ask": ask}
            if prev:
                self.add_to_bar(ts, ltp, qty, side)
        else:
            self.prev.update(bid=bid, ask=ask)
        return self.check_exit(ts, ltp)

    def add_to_bar(self, ts, px, qty, side):
        minute = ts.replace(second=0, microsecond=0)
        if self.cur is None or self.cur["ts"] != minute:
            if self.cur is not None:
                self.bars.append(self.cur)
            self.cur = {"ts": minute, "open": px, "high": px, "low": px, "close": px, "volume": 0, "buy_vol": 0, "sell_vol": 0}
        b = self.cur
        b["high"], b["low"], b["close"] = max(b["high"], px), min(b["low"], px), px
        b["volume"] += qty
        if side == "BUY":
            b["buy_vol"] += qty
        elif side == "SELL":
            b["sell_vol"] += qty

    # --- run setups on the bars so far ---
    def new_signals(self, now):
        if len(self.bars) < 2:
            return []
        m1 = pd.DataFrame(self.bars)
        m1["date"] = m1["ts"].dt.date
        m1 = engine.add_delta(m1)
        d = m1["date"].iloc[-1]
        ib = m1[m1["ts"].dt.strftime("%H:%M") < C.IB_END]
        lv = pd.DataFrame([{**self.lv, "date": d, "open": m1["open"].iloc[0],
                            "ib_high": ib["high"].max(), "ib_low": ib["low"].min()}]).set_index("date")
        bars = engine.signal_bars(m1)
        bars = bars[bars["ready"] <= now]          # only completed 5-min bars
        out = []
        for fn in setups.ALL_SETUPS.values():
            for s in fn(bars, lv):
                key = (s["setup"], s["signal_ts"], s["dir"])
                if key not in self.seen:
                    self.seen.add(key)
                    out.append(s)
        return out

    # --- paper trades ---
    def maybe_enter(self, sig, ts, px):
        if self.open_trade or sum(t["date"] == sig["date"] for t in self.trades) >= C.MAX_TRADES_PER_DAY:
            return None
        if not (C.FIRST_ENTRY <= ts.strftime("%H:%M") <= C.LAST_ENTRY):
            return None
        d = sig["dir"]
        if (sig["target"] - px) * d <= 0 or (px - sig["stop"]) * d <= 0:
            return None
        self.open_trade = {"index": self.name, "setup": sig["setup"], "date": sig["date"], "dir": d,
                           "entry_ts": ts, "entry": px, "stop": sig["stop"], "target": sig["target"]}
        return self.open_trade

    def check_exit(self, ts, px):
        t = self.open_trade
        if not t:
            return None
        d, reason = t["dir"], None
        if (px - t["stop"]) * d <= 0:
            reason = "STOP"
        elif (px - t["target"]) * d >= 0:
            reason = "TARGET"
        elif ts.strftime("%H:%M") >= C.FORCE_EXIT:
            reason = "TIME"
        if not reason:
            return None
        lot = self.m["lot"]
        pts = round((px - t["entry"]) * d, 2)
        t.update(exit_ts=ts, exit=px, reason=reason, points=pts, pnl_1lot_fut=round(pts * lot - 2 * C.OPT_BROKERAGE))
        self.trades.append(t)
        self.open_trade = None
        return t


# ---------------- main ----------------
def main():
    replay = sys.argv[2] if len(sys.argv) > 2 and sys.argv[1] == "--replay" else None
    ctx, dhan = connect()
    PAPER_DIR.mkdir(parents=True, exist_ok=True)
    day = datetime.now().strftime("%Y-%m-%d") if not replay else Path(replay).stem
    sig_f = open(PAPER_DIR / f"{day}_signals.csv", "a", newline="")
    tr_f = open(PAPER_DIR / f"{day}_trades.csv", "a", newline="")
    sig_w, tr_w = csv.writer(sig_f), csv.writer(tr_f)
    if sig_f.tell() == 0:
        sig_w.writerow(["time", "index", "setup", "dir", "price", "stop", "target", "taken"])
    if tr_f.tell() == 0:
        tr_w.writerow(["index", "setup", "dir", "entry_ts", "entry", "exit_ts", "exit", "reason", "points", "pnl_1lot_fut"])

    from dhanhq import MarketFeed
    trackers, insts, names = {}, [], {}
    for name, m in C.MARKETS.items():
        sid, sym = current_future(name)
        trackers[sid] = Tracker(name, m, dhan, sid, sym)
        insts.append((MarketFeed.NSE_FNO, sid, MarketFeed.Full))
        names[sid] = sym

    if not replay and datetime.now().strftime("%H:%M") < START_AT:
        print(f"Waiting for market open ({START_AT})...")
        while datetime.now().strftime("%H:%M") < START_AT:
            time.sleep(20)
    print("PAPER TRADING (no real orders). Ctrl+C to stop.\n")

    source = replay_ticks(replay, names) if replay else live_ticks(ctx, insts)
    last_check = {}
    for ts, sid, ltp, vol, bid, ask in source:
        tr = trackers.get(sid)
        if not tr:
            continue
        closed = tr.on_tick(ts, ltp, vol, bid, ask)
        if closed:
            tr_w.writerow([closed[k] for k in ("index", "setup", "dir", "entry_ts", "entry", "exit_ts", "exit", "reason", "points", "pnl_1lot_fut")])
            tr_f.flush()
            print(f"{ts:%H:%M:%S} {tr.name} EXIT {closed['reason']} @ {ltp:.1f}  {closed['points']:+.1f} pts  "
                  f"(1 lot fut: Rs {closed['pnl_1lot_fut']:+,.0f})")
        # check for new signals once per completed minute
        minute = ts.replace(second=0, microsecond=0)
        if last_check.get(sid) != minute:
            last_check[sid] = minute
            for s in tr.new_signals(ts):
                t = tr.maybe_enter(s, ts, ltp)
                sig_w.writerow([ts.isoformat(timespec="seconds"), tr.name, s["setup"], s["dir"], ltp, round(s["stop"], 1), round(s["target"], 1), bool(t)])
                sig_f.flush()
                word = "LONG" if s["dir"] > 0 else "SHORT"
                print(f"{ts:%H:%M:%S} {tr.name} {s['setup']} {word} @ {ltp:.1f}  stop {s['stop']:.1f}  target {s['target']:.1f}  "
                      f"{'-> PAPER ENTRY' if t else '(skipped: limit/already in trade)'}")

    # summary
    print("\n---- Day summary (paper) ----")
    for tr in trackers.values():
        if tr.open_trade:
            print(f"{tr.name}: trade still open at end: {tr.open_trade['setup']} {tr.open_trade['dir']}")
        p = sum(t["pnl_1lot_fut"] for t in tr.trades)
        print(f"{tr.name}: {len(tr.trades)} trades, {sum(t['points'] for t in tr.trades):+.1f} pts, Rs {p:+,.0f} on 1 lot futures")
    sig_f.close(), tr_f.close()
    print(f"Logs: {PAPER_DIR}")


if __name__ == "__main__":
    main()
