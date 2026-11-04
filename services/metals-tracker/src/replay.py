"""Intraday replay: play historical 5-min bars through the live analytics engine.

A "dataset" is a directory of per-coin CSVs:

    datasets/<name>/
        BTC_perp.csv    datetime,open,high,low,close,volume[,oi]
        BTC_ctx.csv     datetime,mark,funding,oi                 (optional)
        ETH_perp.csv    ...

Each bar is expanded into a few intra-bar trade prints (open→high→low→close,
volume split by close position so the footprint delta is meaningful), stamped
inside the bar's window, so candles.py rebuilds the identical OHLCV and every
analytics module runs exactly as it does live. A per-coin ctx file (funding/OI)
is optional — without it the funding panel stays empty and OI comes from the
perp file's `oi` column if present. Loader tolerates common column-name variants.
"""
from __future__ import annotations

import csv
import datetime as dt
import threading
import time
from pathlib import Path

from .config import Config
from .instruments import ResolvedInstrument
from .state import MarketState

_COLMAP = {
    "datetime": "dt", "date": "dt", "time": "dt", "timestamp": "dt",
    "open": "o", "high": "h", "low": "l", "close": "c",
    "volume": "v", "vol": "v", "oi": "oi", "open interest": "oi",
    "mark": "mark", "markpx": "mark", "funding": "funding", "fundingrate": "funding",
}

_DT_FORMATS = [
    "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M:%S%z", "%d-%m-%Y %H:%M", "%m/%d/%Y %H:%M", "%Y-%m-%d",
]


def build_replay_resolved(cfg: Config, dataset_dir) -> dict:
    resolved: dict = {}
    for ins in cfg.instruments:
        resolved[ins.key] = ResolvedInstrument(
            key=ins.key, cfg=ins, coin=ins.coin,
            sz_decimals={"BTC": 5, "ETH": 4, "SOL": 2}.get(ins.coin, 3),
            max_leverage=50)
    return resolved


def _parse_dt(s: str):
    s = (s or "").strip().strip('"')
    if not s:
        return None
    if s.isdigit() and len(s) >= 12:              # epoch ms
        try:
            return dt.datetime.utcfromtimestamp(int(s) / 1000.0)
        except Exception:
            pass
    if s.isdigit() and len(s) >= 9:               # epoch s
        try:
            return dt.datetime.utcfromtimestamp(int(s))
        except Exception:
            pass
    for fmt in _DT_FORMATS:
        try:
            return dt.datetime.strptime(s, fmt).replace(tzinfo=None)
        except ValueError:
            continue
    try:
        return dt.datetime.fromisoformat(s.replace("Z", "")).replace(tzinfo=None)
    except Exception:
        return None


def _load_rows(path: Path) -> list[dict]:
    with path.open(newline="") as f:
        sample = f.read(2048)
        f.seek(0)
        delim = "\t" if sample.count("\t") > sample.count(",") else ","
        reader = csv.reader(f, delimiter=delim)
        header = next(reader, None)
        if not header:
            return []
        keys = [_COLMAP.get(h.strip().lstrip("﻿").lower(),
                            h.strip().lower()) for h in header]
        out = []
        for raw in reader:
            if not raw or all(not c.strip() for c in raw):
                continue
            out.append({keys[i]: raw[i] for i in range(min(len(keys), len(raw)))})
        return out


def load_bars(path: Path) -> list[dict]:
    bars = []
    for r in _load_rows(path):
        d = _parse_dt(r.get("dt", ""))
        if d is None or "c" not in r:
            continue
        try:
            bars.append({
                "dt": d,
                "o": float(r.get("o", r["c"])), "h": float(r.get("h", r["c"])),
                "l": float(r.get("l", r["c"])), "c": float(r["c"]),
                "v": float(r.get("v", 0) or 0),
                "oi": float(r["oi"]) if r.get("oi") not in (None, "", "0") else None,
            })
        except (ValueError, KeyError):
            continue
    bars.sort(key=lambda b: b["dt"])
    return bars


def load_ctx(path: Path) -> list[dict]:
    out = []
    for r in _load_rows(path):
        d = _parse_dt(r.get("dt", ""))
        if d is None:
            continue
        try:
            out.append({"dt": d,
                        "mark": float(r["mark"]) if r.get("mark") else None,
                        "funding": float(r["funding"]) if r.get("funding") else None,
                        "oi": float(r["oi"]) if r.get("oi") else None})
        except (ValueError, KeyError):
            continue
    out.sort(key=lambda x: x["dt"])
    return out


class ReplayFeed:
    def __init__(self, cfg: Config, dataset_dir, resolved: dict, state: MarketState):
        self.cfg = cfg
        self.dir = Path(dataset_dir)
        self.resolved = resolved
        self.state = state
        self._stop = threading.Event()
        self.thread: threading.Thread | None = None
        self.events: list[tuple] = []
        self._build_events()

    def _bar_trades(self, coin, bar):
        base = bar["dt"]
        rng = bar["h"] - bar["l"]
        frac_up = ((bar["c"] - bar["l"]) / rng) if rng > 0 else 0.5
        seq = [(0, bar["o"]), (1, bar["h"]), (2, bar["l"]), (3, bar["c"])]
        out = []
        vol_each = bar["v"] / len(seq) if bar["v"] else 0.0
        for i, (off, px) in enumerate(seq):
            is_buy = (px >= bar["o"]) if i else frac_up >= 0.5
            out.append(("trade", coin, base + dt.timedelta(seconds=off),
                        px, vol_each, is_buy))
        return out

    def _build_events(self):
        events = []
        for key, ri in self.resolved.items():
            perp_p = self.dir / f"{key}_perp.csv"
            ctx_p = self.dir / f"{key}_ctx.csv"
            if ctx_p.exists():
                for c in load_ctx(ctx_p):
                    events.append(("ctx", ri.coin, c["dt"], c.get("mark"),
                                   c.get("funding"), c.get("oi")))
            if perp_p.exists():
                for bar in load_bars(perp_p):
                    if bar.get("oi") is not None:
                        events.append(("ctx", ri.coin, bar["dt"] - dt.timedelta(seconds=1),
                                       None, None, bar["oi"]))
                    events += self._bar_trades(ri.coin, bar)
        events.sort(key=lambda e: e[2])
        self.events = events

    def start(self):
        self.thread = threading.Thread(target=self._run, daemon=True, name="replay-feed")
        self.thread.start()

    def _dispatch(self, ev):
        if ev[0] == "trade":
            _, coin, ts, px, sz, is_buy = ev
            self.state.apply_trade(coin, ts, px, sz, is_buy)
        else:
            _, coin, ts, mark, funding, oi = ev
            self.state.apply_ctx(coin, mark=mark, funding=funding, oi=oi, ts=ts)

    def _run(self):
        if not self.events:
            print("[replay] no events — check dataset path / CSV format")
            return
        speed = float(self.cfg.a("sim", "speed", default=60))
        prev = self.events[0][2]
        for ev in self.events:
            if self._stop.is_set():
                return
            gap = (ev[2] - prev).total_seconds()
            if gap > 0:
                time.sleep(min(gap / max(1.0, speed), 2.0))
            prev = ev[2]
            self._dispatch(ev)
        print("[replay] dataset finished — dashboard holds final state")

    def stop(self):
        self._stop.set()
