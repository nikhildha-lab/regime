"""Live 20-level order book for NIFTY / BANKNIFTY futures.

Source: the Dhan depth recorder in ~/Documents/dhan/orderflow (depth_recorder.py),
which writes one summary row per second per future to
    data/depth/YYYY-MM-DD_summary.csv
(bid/ask, total bid vs ask qty over 5 and 20 levels, imbalance, order counts,
biggest bid / ask wall). We tail that file incrementally - no second Dhan
connection, read-only. If the recorder is not running, get() returns None.
"""
from __future__ import annotations

import csv
import datetime as dt
from collections import deque
from pathlib import Path

DEPTH_DIR = Path.home() / "Documents" / "dhan" / "orderflow" / "data" / "depth"
STALE_S = 30


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


class DepthBook:
    def __init__(self, depth_dir: Path = DEPTH_DIR):
        self.dir = Path(depth_dir)
        self.path = None
        self.pos = 0
        self.header = None
        self.last: dict[str, dict] = {}
        self.hist: dict[str, deque] = {}

    @staticmethod
    def _key(sym) -> str | None:
        s = str(sym or "").upper()
        if s.startswith("BANKNIFTY"):
            return "BANKNIFTY"
        if s.startswith("NIFTY"):
            return "NIFTY"
        return None

    def poll(self, today: dt.date | None = None):
        p = self.dir / f"{(today or dt.date.today()):%Y-%m-%d}_summary.csv"
        if p != self.path:
            self.path, self.pos, self.header = p, 0, None
            self.last.clear(); self.hist.clear()
        if not p.exists():
            return
        try:
            with open(p, "rb") as f:
                f.seek(self.pos)
                chunk = f.read()
        except OSError:
            return
        end = chunk.rfind(b"\n")
        if end < 0:
            return
        self.pos += end + 1                               # only consume complete lines
        for row in csv.reader(chunk[:end + 1].decode("utf-8", "ignore").splitlines()):
            if not row:
                continue
            if self.header is None or row[0] == "time":
                self.header = row
                continue
            d = dict(zip(self.header, row))
            k = self._key(d.get("symbol"))
            if not k:
                continue
            try:
                t = dt.datetime.fromisoformat(d["time"])
            except (KeyError, ValueError):
                continue
            rec = {"t": t, **{c: _f(d.get(c)) for c in (
                "bid", "ask", "bid_qty5", "ask_qty5", "bid_qty20", "ask_qty20", "imbalance20",
                "bid_orders20", "ask_orders20", "bid_wall_px", "bid_wall_qty",
                "ask_wall_px", "ask_wall_qty")}}
            self.last[k] = rec
            self.hist.setdefault(k, deque(maxlen=900)).append((t, rec["imbalance20"]))

    def get(self, key: str, now: dt.datetime | None = None) -> dict | None:
        r = self.last.get(key)
        if not r:
            return None
        now = now or dt.datetime.now()

        def avg(sec):
            xs = [v for t, v in self.hist.get(key, ()) if v is not None
                  and (now - t).total_seconds() <= sec]
            return round(sum(xs) / len(xs), 4) if xs else None
        b5, a5 = r.get("bid_qty5") or 0, r.get("ask_qty5") or 0
        age = (now - r["t"]).total_seconds()
        return {
            "time": r["t"].strftime("%H:%M:%S"), "age_s": round(age, 1),
            "stale": age > STALE_S,
            "bid": r["bid"], "ask": r["ask"],
            "imb20": r["imbalance20"], "imb20_1m": avg(60), "imb20_5m": avg(300),
            "imb5": round((b5 - a5) / (b5 + a5), 4) if (b5 + a5) else None,
            "bid_qty20": r["bid_qty20"], "ask_qty20": r["ask_qty20"],
            "bid_orders20": r["bid_orders20"], "ask_orders20": r["ask_orders20"],
            "bid_wall_px": r["bid_wall_px"], "bid_wall_qty": r["bid_wall_qty"],
            "ask_wall_px": r["ask_wall_px"], "ask_wall_qty": r["ask_wall_qty"],
        }


BOOK = DepthBook()
