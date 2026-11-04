"""
Order-flow calculations (2026-10-17). Pure Python, no network - so it can be tested on recorded / synthetic data.
Read-only analytics: never places orders.

Per instrument, fed by:
  on_book(bids, asks, t, deep=False)   bids / asks = [(price, qty, orders), ...] best first. deep=True for 200-level.
  on_trade(price, qty, t)              trade side is ESTIMATED (Dhan does not say who bought / sold):
                                       at / above best ask = buy, at / below best bid = sell, in between = tick rule.
  second(t)  -> (row dict, [events])   once per second
  minute(t)  -> (footprint candle, heatmap rows)   once per minute

Events (all labelled "context" until our signal check has ~60 sessions):
  wall_new      large resting order (>= WALL_K x median level size) near price, alive >= 10 s
  wall_pulled   a wall disappeared WITHOUT price trading to it  (possible spoof / changed mind)
  wall_consumed a wall disappeared because price traded through it
  absorption    heavy aggressive buying (selling) at a price while the price did not move through it within 60 s
                (sellers / buyers absorbed it)
  iceberg       traded volume at the best level >= ICE_K x the largest size ever shown there, level still holding
  big_trade     a single print >= the session's 99.5th percentile size (min 20 prints seen)
"""
from __future__ import annotations

import bisect
import math
from collections import defaultdict, deque

WALL_K = 5.0          # wall = level qty >= WALL_K x median level qty (top levels)
WALL_NEAR = 0.004     # only levels within 0.4% of mid count as walls
WALL_MIN_AGE = 10     # seconds alive before "wall_new"
PULL_MIN_AGE = 5      # seconds alive before a disappearance counts
ABS_WINDOW = 60       # seconds
ABS_K = 4.0           # aggressive volume at a price >= ABS_K x median per-price minute volume
ICE_K = 3.0           # traded at a level >= ICE_K x the largest size ever shown there
ICE_MIN_AGE = 20      # level must have been at the best price this long
ICE_MIN_X = 10        # and traded volume >= ICE_MIN_X x median trade size
BIG_X = 5             # big print = above the session 99.5th percentile AND >= BIG_X x median trade size
BIG_PCT = 0.995


def _median(xs):
    xs = sorted(x for x in xs if x > 0)
    if not xs:
        return 0.0
    n = len(xs)
    return float(xs[n // 2]) if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2.0


class Instrument:
    def __init__(self, sym: str, bucket: float):
        self.sym, self.bucket = sym, bucket
        self.bids, self.asks = [], []          # 20-level (or 5-level fallback)
        self.deep_bids, self.deep_asks = [], []  # 200-level when available
        self.last_trade_px = None
        self.last_side = 0
        self.cvd = 0.0
        self.sec_buy = self.sec_sell = 0.0
        self.min_trades = []                   # (px, qty, side) this minute
        self.min_open = self.min_high = self.min_low = self.min_close = None
        self.walls = {}                        # (side, price) -> dict
        self.abs_watch = {}                    # (side, price) -> {"t0", "vol"}
        self.abs_count = defaultdict(int)      # (side, price) -> times absorbed today
        self.abs_last = {}                     # (side, price) -> last event time
        self.level_vol_hist = deque(maxlen=30)  # per-minute median per-price volume
        self.ice = {}                          # (side, price) -> {"traded", "max_shown"}
        self.sizes = []                        # sorted trade sizes (for big-trade threshold)
        self.events_today = 0
        self.last_t = None

    # ------------------------------------------------------------------ inputs
    def best(self):
        bb = self.bids[0][0] if self.bids else None
        ba = self.asks[0][0] if self.asks else None
        return bb, ba

    def mid(self):
        bb, ba = self.best()
        if bb and ba:
            return (bb + ba) / 2.0
        return self.last_trade_px

    def on_book(self, bids, asks, t, deep=False):
        bids = [b for b in bids if b[0] and b[1] is not None]
        asks = [a for a in asks if a[0] and a[1] is not None]
        bids.sort(key=lambda x: -x[0])
        asks.sort(key=lambda x: x[0])
        if deep:
            self.deep_bids, self.deep_asks = bids, asks
        else:
            self.bids, self.asks = bids, asks
        # iceberg bookkeeping: largest size shown at the current best levels
        if deep:
            return
        for side, lv in ((1, bids), (-1, asks)):
            if lv:
                k = (side, round(lv[0][0], 2))
                cur = self._top.get(side) if hasattr(self, "_top") else None
                if cur != k:
                    self._top = getattr(self, "_top", {})
                    self._top[side] = k
                    self.ice[k] = {"traded": 0.0, "max_shown": 0.0, "t0": t}
                d = self.ice[k]
                d["max_shown"] = max(d["max_shown"], float(lv[0][1]))

    def on_trade(self, px, qty, t):
        if not px or not qty or qty <= 0:
            return
        bb, ba = self.best()
        if ba and px >= ba:
            side = 1
        elif bb and px <= bb:
            side = -1
        elif self.last_trade_px is not None and px != self.last_trade_px:
            side = 1 if px > self.last_trade_px else -1
        else:
            side = self.last_side or 1
        self.last_side, self.last_trade_px = side, px
        if side > 0:
            self.sec_buy += qty
        else:
            self.sec_sell += qty
        self.cvd += side * qty
        self.min_trades.append((px, qty, side))
        if self.min_open is None:
            self.min_open = px
        self.min_high = px if self.min_high is None else max(self.min_high, px)
        self.min_low = px if self.min_low is None else min(self.min_low, px)
        self.min_close = px
        bisect.insort(self.sizes, qty)
        if len(self.sizes) > 50000:
            self.sizes = self.sizes[::2]
        # absorption: aggressive volume at this price (buys lift the ask, sells hit the bid)
        k = (side, round(px, 2))
        w = self.abs_watch.setdefault(k, {"t0": t, "vol": 0.0})
        w["vol"] += qty
        # iceberg: volume traded at the best level on the passive side
        ik = (-side, round(px, 2))
        if ik in self.ice:
            self.ice[ik]["traded"] += qty
        self._pending_big = getattr(self, "_pending_big", [])
        if len(self.sizes) >= 20:
            thr = self.sizes[min(len(self.sizes) - 1, int(BIG_PCT * len(self.sizes)))]
            med = self.sizes[len(self.sizes) // 2]
            if qty > thr and qty >= BIG_X * med:
                self._pending_big.append((t, px, qty, side))

    # ------------------------------------------------------------------ per second
    def _imb(self, bids, asks, n=None):
        b = sum(x[1] for x in (bids[:n] if n else bids))
        a = sum(x[1] for x in (asks[:n] if n else asks))
        return (b - a) / (b + a) if (a + b) else 0.0, b, a

    def second(self, t):
        ev = []
        bb, ba = self.best()
        mid = self.mid()
        i5, b5, a5 = self._imb(self.bids, self.asks, 5)
        i20, b20, a20 = self._imb(self.bids, self.asks, 20)
        deep = bool(self.deep_bids and self.deep_asks)
        idp = self._imb(self.deep_bids, self.deep_asks)[0] if deep else None
        book_b = self.deep_bids if deep else self.bids
        book_a = self.deep_asks if deep else self.asks
        # ---- walls
        if mid:
            med = _median([x[1] for x in (book_b[:40] + book_a[:40])])
            seen = set()
            if med > 0:
                for side, lv in ((1, book_b), (-1, book_a)):
                    for px, q, _o in lv:
                        if abs(px - mid) / mid > WALL_NEAR:
                            break
                        if q >= WALL_K * med:
                            k = (side, round(px, 2))
                            seen.add(k)
                            w = self.walls.get(k)
                            if w is None:
                                self.walls[k] = {"t0": t, "last": t, "qty": q, "max": q, "announced": False}
                            else:
                                w.update(last=t, qty=q, max=max(w["max"], q))
                                if not w["announced"] and t - w["t0"] >= WALL_MIN_AGE:
                                    w["announced"] = True
                                    ev.append(self._ev(t, "wall_new", side, px, q, f"{'bid' if side > 0 else 'ask'} wall {q:.0f} ({q / med:.0f}x normal) {abs(px - mid):.1f} pts away"))
            for k in list(self.walls):
                if k in seen:
                    continue
                w = self.walls.pop(k)
                side, px = k
                if t - w["t0"] < PULL_MIN_AGE:
                    continue
                traded_to = (self.min_low is not None and self.min_low <= px) if side > 0 else (self.min_high is not None and self.min_high >= px)
                last = self.last_trade_px
                if last is not None:
                    traded_to = traded_to or (last <= px if side > 0 else last >= px)
                kind = "wall_consumed" if traded_to else "wall_pulled"
                ev.append(self._ev(t, kind, side, px, w["max"],
                                   f"{'bid' if side > 0 else 'ask'} wall {w['max']:.0f} at {px} {'traded through' if traded_to else 'pulled before price got there'} after {t - w['t0']:.0f}s"))
        # ---- absorption
        base = _median(self.level_vol_hist) if self.level_vol_hist else 0
        for k in list(self.abs_watch):
            w = self.abs_watch[k]
            if t - w["t0"] < ABS_WINDOW:
                continue
            side, px = k
            vol = w.pop("vol")
            del self.abs_watch[k]
            if base <= 0 or vol < ABS_K * base:
                continue
            held = (self.min_high is None or self.last_trade_px is None) or \
                   ((self.last_trade_px <= px + self.bucket) if side > 0 else (self.last_trade_px >= px - self.bucket))
            if not held:
                continue
            if t - self.abs_last.get(k, -1e9) < 300:
                continue
            self.abs_last[k] = t
            self.abs_count[k] += 1
            who = "sellers absorbed buying" if side > 0 else "buyers absorbed selling"
            ev.append(self._ev(t, "absorption", -side, px, vol,
                               f"{who} at {px}: {vol:.0f} aggressive in {ABS_WINDOW}s ({vol / base:.0f}x normal), price held · {self.abs_count[k]}x today"))
        # ---- icebergs (at current best levels)
        for side, lv in ((1, self.bids), (-1, self.asks)):
            if not lv:
                continue
            k = (side, round(lv[0][0], 2))
            d = self.ice.get(k)
            med = self.sizes[len(self.sizes) // 2] if self.sizes else 0
            if d and d["max_shown"] > 0 and d["traded"] >= ICE_K * d["max_shown"] and not d.get("flag") \
                    and t - d["t0"] >= ICE_MIN_AGE and med and d["traded"] >= ICE_MIN_X * med:
                d["flag"] = True
                ev.append(self._ev(t, "iceberg", side, lv[0][0], d["traded"],
                                   f"possible iceberg {'bid' if side > 0 else 'ask'} at {lv[0][0]}: {d['traded']:.0f} traded vs max {d['max_shown']:.0f} shown"))
        if len(self.ice) > 2000:
            self.ice = {k: v for k, v in self.ice.items() if t - v["t0"] < 1800}
        # ---- big prints
        merged = {}
        for (tt, px, q, s) in getattr(self, "_pending_big", []):        # one event per side per burst (10 s)
            lb = getattr(self, "_last_big", {}).get(s)
            if lb and tt - lb[0] <= 10 and abs(px - lb[1]) <= self.bucket:
                lb[2] += q
                continue
            m = merged.setdefault(s, [tt, px, 0.0, 0])
            m[2] += q
            m[3] += 1
        self._last_big = getattr(self, "_last_big", {})
        for s, (tt, px, q, n) in merged.items():
            self._last_big[s] = [tt, px, q]
            ev.append(self._ev(tt, "big_trade", s, px, q, f"big {'buy' if s > 0 else 'sell'} prints {q:.0f} at ~{px}"
                               f"{f' ({n} prints)' if n > 1 else ''} (estimated side)"))
        self._pending_big = []
        delta = self.sec_buy - self.sec_sell
        row = {"t": t, "sym": self.sym, "bid": bb, "ask": ba, "mid": round(mid, 2) if mid else None,
               "last": self.last_trade_px, "buy_vol": self.sec_buy, "sell_vol": self.sec_sell, "delta": delta,
               "cvd": self.cvd, "imb5": round(i5, 3), "imb20": round(i20, 3),
               "imb_deep": round(idp, 3) if idp is not None else None, "bid20": b20, "ask20": a20,
               "walls_bid": sum(1 for (s, _p) in self.walls if s > 0), "walls_ask": sum(1 for (s, _p) in self.walls if s < 0)}
        self.sec_buy = self.sec_sell = 0.0
        self.last_t = t
        self.events_today += len(ev)
        return row, ev

    def _ev(self, t, kind, side, px, qty, text):
        return {"t": t, "sym": self.sym, "kind": kind, "side": "bid" if side > 0 else "ask", "bias": side,
                "price": round(px, 2), "qty": round(float(qty), 1), "text": text, "label": "context"}

    # ------------------------------------------------------------------ per minute
    def peek(self, t):
        """The minute still forming (2026-10-23): same shape as minute() but nothing is reset - for live charts."""
        fp = defaultdict(lambda: [0.0, 0.0])
        for px, q, s in self.min_trades:
            b = math.floor(px / self.bucket) * self.bucket
            fp[round(b, 2)][1 if s > 0 else 0] += q
        levels = sorted(fp.items())
        if not levels or self.min_open is None:
            return None
        buy = sum(v[1] for _, v in levels); sell = sum(v[0] for _, v in levels)
        return {"t": t, "sym": self.sym, "o": self.min_open, "h": self.min_high, "l": self.min_low, "c": self.min_close,
                "buy": buy, "sell": sell, "delta": buy - sell, "cvd": self.cvd, "forming": True,
                "levels": [[p, round(s_, 1), round(b_, 1)] for p, (s_, b_) in levels]}

    def minute(self, t):
        fp = defaultdict(lambda: [0.0, 0.0])         # bucket -> [sell(bid) vol, buy(ask) vol]
        for px, q, s in self.min_trades:
            b = math.floor(px / self.bucket) * self.bucket
            fp[round(b, 2)][1 if s > 0 else 0] += q
        levels = sorted(fp.items())
        vols = [a + b for _, (a, b) in levels]
        if vols:
            self.level_vol_hist.append(_median(vols))
        poc = max(levels, key=lambda x: x[1][0] + x[1][1])[0] if levels else None
        buy = sum(v[1] for _, v in levels)
        sell = sum(v[0] for _, v in levels)
        # stacked imbalance: 3+ consecutive buckets where buy >= 3x sell (or the reverse)
        stack_b = stack_s = run_b = run_s = 0
        for _, (s_, b_) in levels:
            run_b = run_b + 1 if b_ >= 3 * max(s_, 1) else 0
            run_s = run_s + 1 if s_ >= 3 * max(b_, 1) else 0
            stack_b, stack_s = max(stack_b, run_b), max(stack_s, run_s)
        candle = {"t": t, "sym": self.sym, "o": self.min_open, "h": self.min_high, "l": self.min_low, "c": self.min_close,
                  "buy": buy, "sell": sell, "delta": buy - sell, "cvd": self.cvd, "poc": poc,
                  "stacked_buy": stack_b, "stacked_sell": stack_s,
                  "levels": [[p, round(s_, 1), round(b_, 1)] for p, (s_, b_) in levels]}
        # heatmap slice: resting liquidity by bucket within +-1% of mid (deep book if available)
        mid = self.mid()
        heat = []
        if mid:
            agg = defaultdict(lambda: [0.0, 0.0])
            for side, lv in ((0, self.deep_bids or self.bids), (1, self.deep_asks or self.asks)):
                for px, q, _o in lv:
                    if abs(px - mid) / mid <= 0.01:
                        agg[round(math.floor(px / self.bucket) * self.bucket, 2)][side] += q
            heat = [[t, self.sym, p, round(b, 1), round(a, 1)] for p, (b, a) in sorted(agg.items())]
        self.min_trades = []
        self.min_open = self.min_high = self.min_low = self.min_close = None
        return candle, heat
