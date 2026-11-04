"""Order-flow footprint (approximated from Kite's snapshot feed).

HONEST DATA NOTE
----------------
A true footprint needs trade-by-trade data with each trade tagged as a buy
(lifted the ask) or a sell (hit the bid). Kite Connect does NOT provide that.
MODE_FULL gives ~1 snapshot/sec: last price, cumulative volume, and a 5-level
bid/ask book. So we APPROXIMATE:

  * volume-at-price   : attribute the cumulative-volume delta between two
                        snapshots to the price bin of the later snapshot.
  * buy / sell split  : QUOTE RULE — price >= best ask => buy aggressor;
                        price <= best bid => sell aggressor; otherwise fall back
                        to the TICK RULE (up-tick = buy, down-tick = sell).
                        (This is the classic Lee-Ready proxy.)

It captures most — not all — of the flow, and the aggressor is inferred, not
exact. Treat it as a live confirmation lens, NOT a backtested signal (the 2-year
history is OHLC candles only, so footprints can't be validated there).

FootprintBook accumulates per-bar clusters live; `analyze()` turns them into the
snapshot payload: per-bar buy/sell-at-price, bar delta & POC, stacked diagonal
imbalances, and a LEVEL-ABSORPTION read (e.g. buyers absorbing at the lower IB
=> long confirmation).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from ..candles import floor_to_bucket


class FootprintBook:
    """Live per-bar volume-at-price, split buy/sell. One per instrument (futures)."""

    def __init__(self, bin_width: float, bucket_minutes: int, max_bars: int = 96):
        self.bw = float(bin_width) or 1.0
        self.bucket_minutes = bucket_minutes
        self.max_bars = max_bars               # ~full 5-min session + buffer
        self.order: list = []                 # bar-start datetimes, ascending
        self.bars: dict = {}                  # bar-start -> bar dict
        self._last_cum: float | None = None
        self._last_price: float | None = None
        self._last_sign: int = 1

    def delta_series(self):
        """[(HH:MM, bar_delta)] ascending — the full-session delta for divergence."""
        return [(k.strftime("%H:%M"), self.bars[k]["buy"] - self.bars[k]["sell"])
                for k in self.order]

    def seed_bar(self, start, o, h, l, c, buy, sell, bins, approx=True):
        """Inject a historical (approximate) bar rebuilt from a 5-min candle."""
        self.bars[start] = {"o": o, "h": h, "l": l, "c": c,
                            "buy": float(buy), "sell": float(sell),
                            "bins": {float(p): [float(b), float(s)]
                                     for p, (b, s) in bins.items()},
                            "approx": approx}
        if start not in self.order:
            self.order.append(start)
        self.order.sort()
        # keep the most-recent live cum baseline untouched; just bound history
        while len(self.order) > self.max_bars:
            old = self.order.pop(0)
            self.bars.pop(old, None)

    def _bin(self, price: float) -> float:
        return math.floor(price / self.bw) * self.bw

    def _sign(self, price: float, bid, ask) -> int:
        # quote rule first (needs a valid book), else tick rule
        if bid is not None and ask is not None and ask >= bid:
            if price >= ask:
                return 1
            if price <= bid:
                return -1
        if self._last_price is not None:
            if price > self._last_price:
                return 1
            if price < self._last_price:
                return -1
        return self._last_sign

    def on_trade(self, ts, price: float, size: float, is_buy: bool):
        """Crypto path: a real trade with a KNOWN aggressor side (no proxy needed).
        `size` is the traded qty for this print; `is_buy` True = taker lifted the ask."""
        vol = max(0.0, float(size or 0.0))
        bkey = floor_to_bucket(ts, self.bucket_minutes)
        bar = self.bars.get(bkey)
        if bar is None:
            bar = {"o": price, "h": price, "l": price, "c": price,
                   "buy": 0.0, "sell": 0.0, "bins": {}, "approx": False}
            self.bars[bkey] = bar
            self.order.append(bkey)
            if len(self.order) > self.max_bars:
                old = self.order.pop(0)
                self.bars.pop(old, None)
        bar["h"] = max(bar["h"], price)
        bar["l"] = min(bar["l"], price)
        bar["c"] = price
        if vol > 0:
            b = self._bin(price)
            cell = bar["bins"].get(b)
            if cell is None:
                cell = [0.0, 0.0]
                bar["bins"][b] = cell
            if is_buy:
                cell[0] += vol
                bar["buy"] += vol
            else:
                cell[1] += vol
                bar["sell"] += vol
        self._last_price = price

    def on_tick(self, ts, price: float, cum_volume, bid=None, ask=None):
        # cumulative-volume delta -> the volume that traded since last snapshot
        vol = 0.0
        if cum_volume is not None:
            if self._last_cum is None:
                self._last_cum = float(cum_volume)
            vol = max(0.0, float(cum_volume) - self._last_cum)
            self._last_cum = float(cum_volume)

        sign = self._sign(price, bid, ask)

        bkey = floor_to_bucket(ts, self.bucket_minutes)
        bar = self.bars.get(bkey)
        if bar is None:
            bar = {"o": price, "h": price, "l": price, "c": price,
                   "buy": 0.0, "sell": 0.0, "bins": {}, "approx": False}
            self.bars[bkey] = bar
            self.order.append(bkey)
            if len(self.order) > self.max_bars:
                old = self.order.pop(0)
                self.bars.pop(old, None)
        bar["h"] = max(bar["h"], price)
        bar["l"] = min(bar["l"], price)
        bar["c"] = price

        if vol > 0:
            b = self._bin(price)
            cell = bar["bins"].get(b)
            if cell is None:
                cell = [0.0, 0.0]           # [buy, sell]
                bar["bins"][b] = cell
            if sign >= 0:
                cell[0] += vol
                bar["buy"] += vol
            else:
                cell[1] += vol
                bar["sell"] += vol

        self._last_price = price
        self._last_sign = sign


@dataclass
class FootprintRead:
    bars: list = field(default_factory=list)     # recent bars, ascending
    read: dict | None = None                     # level-absorption on the latest bar
    reversals: list = field(default_factory=list)  # absorption reversals across bars
    last_delta: float = 0.0
    last_poc: float | None = None

    def to_dict(self):
        return {"bars": self.bars, "read": self.read, "reversals": self.reversals,
                "last_delta": round(self.last_delta),
                "last_poc": None if self.last_poc is None else round(self.last_poc, 1)}

    def confirms(self, direction: str):
        """True if absorption backs this direction, False if it opposes, else None."""
        if not self.read:
            return None
        return self.read["side"] == direction


def _bin_imbalances(bins, bw, ratio, min_vol):
    """Diagonal (Bid×Ask) imbalance: buy@p vs sell@(p-1), sell@p vs buy@(p+1)."""
    buy_imb, sell_imb = [], []
    prices = sorted(bins)
    m = {p: bins[p] for p in prices}
    for p in prices:
        b, s = m[p]
        down = m.get(round(p - bw, 4)) or m.get(p - bw)
        up = m.get(round(p + bw, 4)) or m.get(p + bw)
        sell_below = down[1] if down else 0.0
        buy_above = up[0] if up else 0.0
        if b >= ratio * max(sell_below, min_vol) and b >= min_vol:
            buy_imb.append(p)
        if s >= ratio * max(buy_above, min_vol) and s >= min_vol:
            sell_imb.append(p)
    return buy_imb, sell_imb


def analyze(book: FootprintBook, levels: dict, bars_shown: int = 12,
            imbalance_ratio: float = 3.0, tol: float = 10.0,
            vol_mult: float = 1.5) -> FootprintRead:
    out = FootprintRead()
    if book is None or not book.order:
        return out

    keys = book.order[-bars_shown:]
    # price decimals follow the bin size (2026-10-23): FX / small coins were all rounded to 0.1 and collapsed into one cell
    _bs = f"{book.bw:.10f}".rstrip("0")
    _dp = max(1, len(_bs.split(".")[1]) if "." in _bs else 0)
    _vr = lambda v: round(v) if abs(v) >= 100 else round(v, 2)
    totals = []
    bar_payload = []
    for k in keys:
        bar = book.bars[k]
        total = bar["buy"] + bar["sell"]
        totals.append(total)
        min_vol = max(1.0, total * 0.02)
        buy_imb, sell_imb = _bin_imbalances(bar["bins"], book.bw,
                                            imbalance_ratio, min_vol)
        # bar POC = price bin with the most total volume
        poc = None
        if bar["bins"]:
            poc = max(bar["bins"], key=lambda p: sum(bar["bins"][p]))
        bins_list = [{"p": round(p, _dp),
                      "b": _vr(bar["bins"][p][0]),
                      "s": _vr(bar["bins"][p][1])}
                     for p in sorted(bar["bins"])]
        bar_payload.append({
            "t": k.strftime("%H:%M"),
            "o": round(bar["o"], _dp), "h": round(bar["h"], _dp),
            "l": round(bar["l"], _dp), "c": round(bar["c"], _dp),
            "delta": _vr(bar["buy"] - bar["sell"]),
            "total": _vr(total),
            "poc": None if poc is None else round(poc, _dp),
            "bins": bins_list,
            "buy_imb": [round(p, _dp) for p in buy_imb],
            "sell_imb": [round(p, _dp) for p in sell_imb],
            "approx": bool(bar.get("approx")),
        })
    out.bars = bar_payload

    last = book.bars[keys[-1]]
    out.last_delta = last["buy"] - last["sell"]
    if last["bins"]:
        out.last_poc = max(last["bins"], key=lambda p: sum(last["bins"][p]))

    # signals use the last 24 bars only (2026-10-23): more bars are exported for the 30 min / 4 h charts,
    # but the absorption median and reversal scan must stay exactly as before
    keys = keys[-24:]; totals = totals[-24:]
    med = sorted(totals)[len(totals) // 2] if totals else 0.0

    # ---- absorption-reversal scan across all shown bars ------------------
    # Static levels (prior day/week H/L) are the cleanest anchors here since
    # they don't move; intraday levels are tested against their current value.
    for k in keys:
        bar = book.bars[k]
        rv = _reversal_at_levels(bar, levels, tol, med, vol_mult)
        if rv:
            rv["t"] = k.strftime("%H:%M")
            out.reversals.append(rv)
    # the live badge = a reversal on the most recent bar, if any
    last_t = keys[-1].strftime("%H:%M")
    for rv in out.reversals:
        if rv["t"] == last_t:
            out.read = rv
    return out


def _reversal_at_levels(bar, levels, tol, med, vol_mult):
    """Footprint absorption reversal on one bar: price tested a level and the
    aggressor flipped (net delta + close back through the level)."""
    delta = bar["buy"] - bar["sell"]
    o, h, l, c = bar["o"], bar["h"], bar["l"], bar["c"]
    heavy = (bar["buy"] + bar["sell"]) >= vol_mult * med if med else False
    rng = h - l
    # where in the bar's range did it close? (rejection shape)
    pos = ((c - l) / rng) if rng > 0 else 0.5   # 1 = closed at high, 0 = at low
    cands = []
    for name, lv in levels.items():
        if lv is None:
            continue
        # support reversal (long): wick pierced/tagged the level, closed back
        # above it in the UPPER part of the bar, with net buying (absorption)
        if l <= lv + tol and c > lv and pos >= 0.6 and delta > 0:
            cands.append(("long", name, lv, delta))
        # resistance reversal (short): tagged from below, closed back under it in
        # the LOWER part of the bar, with net selling
        if h >= lv - tol and c < lv and pos <= 0.4 and delta < 0:
            cands.append(("short", name, lv, delta))
    if not cands:
        return None
    side, name, lv, dl = max(cands, key=lambda x: abs(x[3]))
    strength = "strong" if (heavy and med and abs(dl) >= vol_mult * abs(med) * 0.15) \
        else "moderate"
    verb = "buyers absorbing" if side == "long" else "sellers absorbing"
    return {
        "side": side, "level": float(f"{lv:.6g}"), "level_name": name,
        "delta": round(dl), "strength": strength,
        "note": f"{verb} at {name} {round(lv, 1)} (Δ {'+' if dl > 0 else ''}{round(dl)})",
    }
