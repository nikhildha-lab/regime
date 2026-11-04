"""Intraday replay: play historical 5-min bars through the live analytics engine.

A "dataset" is a directory containing per-instrument CSVs:

    datasets/<name>/
        NIFTY_spot.csv        datetime,open,high,low,close[,volume,oi]   (index)
        NIFTY_fut.csv         datetime,open,high,low,close,volume[,oi]    (futures)
        NIFTY_options.csv     datetime,strike,leg,oi[,ltp,volume]         (optional)
        BANKNIFTY_spot.csv    ...
        ...

Each bar is expanded into a handful of intra-bar ticks (open→high→low→close)
stamped inside the bar's 5-min window, so candles.py rebuilds the identical
OHLCV and every analytics module runs exactly as it does live. Missing files are
skipped gracefully (e.g. no options file => OI panel stays empty).

Loader is tolerant of column-name variants and common export formats
(TradingView, NSE, Kite). Bars are replayed in timestamp order across all
instruments, at `sim.speed` compression.
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


def build_replay_resolved(cfg: Config, dataset_dir) -> dict:
    """Build ResolvedInstrument map straight from a dataset dir (no broker needed).

    Option strikes are discovered from each <KEY>_options.csv so any real dataset
    maps cleanly; tokens are synthetic sequential ids used only for routing.
    """
    dataset_dir = Path(dataset_dir)
    resolved: dict = {}
    tok = 900000
    for ins in cfg.instruments:
        tok += 1
        spot_token = tok
        tok += 1
        fut_token = tok
        ri = ResolvedInstrument(
            key=ins.key, cfg=ins, spot_token=spot_token, fut_token=fut_token,
            fut_tradingsymbol=f"{ins.fut_name}-REPLAY", expiry=None,
        )
        ri.token_meta[spot_token] = {"role": "spot"}
        ri.token_meta[fut_token] = {"role": "fut"}
        opt_p = dataset_dir / f"{ins.key}_options.csv"
        if opt_p.exists():
            strikes = set()
            for row in load_option_rows(opt_p):
                strikes.add((row["strike"], row["leg"]))
            for strike, leg in sorted(strikes):
                tok += 1
                ri.option_tokens.setdefault(strike, {})[leg] = tok
                ri.token_meta[tok] = {"role": "opt", "leg": leg, "strike": strike}
        resolved[ins.key] = ri
    return resolved

# candidate column names -> canonical
_COLMAP = {
    "datetime": "dt", "date": "dt", "time": "dt", "timestamp": "dt",
    "open": "o", "high": "h", "low": "l", "close": "c",
    "volume": "v", "shares traded": "v", "vol": "v",
    "oi": "oi", "open interest": "oi", "openinterest": "oi",
    "strike": "strike", "strike price": "strike",
    "leg": "leg", "type": "leg", "instrument_type": "leg", "option_type": "leg",
    "ltp": "ltp", "last": "ltp", "price": "ltp",
}

_DT_FORMATS = [
    "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M:%S%z", "%d-%m-%Y %H:%M", "%m/%d/%Y %H:%M",
    "%d-%b-%Y", "%Y-%m-%d",
]


def _parse_dt(s: str) -> dt.datetime | None:
    s = s.strip().strip('"')
    if not s:
        return None
    # epoch seconds?
    if s.isdigit() and len(s) >= 9:
        try:
            return dt.datetime.fromtimestamp(int(s))
        except Exception:
            pass
    for fmt in _DT_FORMATS:
        try:
            d = dt.datetime.strptime(s, fmt)
            return d.replace(tzinfo=None)
        except ValueError:
            continue
    # ISO fallback
    try:
        return dt.datetime.fromisoformat(s.replace("Z", "")).replace(tzinfo=None)
    except Exception:
        return None


def _load_rows(path: Path) -> list[dict]:
    with path.open(newline="") as f:
        sample = f.read(2048)
        f.seek(0)
        delim = ","
        if sample.count("\t") > sample.count(","):
            delim = "\t"
        reader = csv.reader(f, delimiter=delim)
        header = next(reader, None)
        if not header:
            return []
        keys = []
        for h in header:
            k = h.strip().lstrip("﻿").lower()
            keys.append(_COLMAP.get(k, k))
        out = []
        for raw in reader:
            if not raw or all(not c.strip() for c in raw):
                continue
            row = {keys[i]: raw[i] for i in range(min(len(keys), len(raw)))}
            out.append(row)
        return out


def load_bars(path: Path) -> list[dict]:
    """Return sorted list of {dt, o,h,l,c, v, oi} bar dicts."""
    rows = _load_rows(path)
    bars = []
    for r in rows:
        d = _parse_dt(r.get("dt", ""))
        if d is None or "c" not in r:
            continue
        try:
            bars.append({
                "dt": d,
                "o": float(r.get("o", r["c"])),
                "h": float(r.get("h", r["c"])),
                "l": float(r.get("l", r["c"])),
                "c": float(r["c"]),
                "v": float(r.get("v", 0) or 0),
                "oi": float(r["oi"]) if r.get("oi") not in (None, "", "0") else None,
            })
        except (ValueError, KeyError):
            continue
    bars.sort(key=lambda b: b["dt"])
    return bars


def load_option_rows(path: Path) -> list[dict]:
    rows = _load_rows(path)
    out = []
    for r in rows:
        d = _parse_dt(r.get("dt", ""))
        if d is None or "strike" not in r or "leg" not in r:
            continue
        try:
            out.append({
                "dt": d, "strike": float(r["strike"]),
                "leg": str(r["leg"]).upper()[:2],
                "oi": float(r.get("oi", 0) or 0),
                "ltp": float(r.get("ltp", 0) or 0),
                "v": float(r.get("v", 0) or 0),
            })
        except (ValueError, KeyError):
            continue
    out.sort(key=lambda x: x["dt"])
    return out


class ReplayFeed:
    """Plays a dataset directory into MarketState."""

    def __init__(self, cfg: Config, dataset_dir: Path, resolved: dict, state: MarketState):
        self.cfg = cfg
        self.dir = Path(dataset_dir)
        self.resolved = resolved
        self.state = state
        self._stop = threading.Event()
        self.thread: threading.Thread | None = None
        self.events: list[tuple] = []   # (ts, token, ltp, cum_vol, oi)
        self._build_events()

    def _emit_bar_ticks(self, token, bar, cum_vol_ref, has_vol):
        """Return list of (ts, token, ltp, cum_vol, oi) for one bar."""
        base = bar["dt"]
        # 4 ticks inside the bar window: open, high, low, close
        seq = [(0, bar["o"]), (1, bar["h"]), (2, bar["l"]), (3, bar["c"])]
        ticks = []
        for i, (off, px) in enumerate(seq):
            ts = base + dt.timedelta(seconds=off)
            cum = None
            if has_vol:
                if i == len(seq) - 1:            # attribute full bar volume at close
                    cum_vol_ref[0] += bar["v"]
                cum = cum_vol_ref[0]
            ticks.append((ts, token, px, cum, bar["oi"]))
        return ticks

    def _build_events(self):
        events = []
        for key, ri in self.resolved.items():
            spot_p = self.dir / f"{key}_spot.csv"
            fut_p = self.dir / f"{key}_fut.csv"
            opt_p = self.dir / f"{key}_options.csv"

            if spot_p.exists():
                cum = [0.0]
                for bar in load_bars(spot_p):
                    events += self._emit_bar_ticks(ri.spot_token, bar, cum, has_vol=False)
            if fut_p.exists():
                cum = [0.0]
                for bar in load_bars(fut_p):
                    events += self._emit_bar_ticks(ri.fut_token, bar, cum, has_vol=True)
            if opt_p.exists():
                # map (strike,leg) -> token via resolved option_tokens
                for row in load_option_rows(opt_p):
                    legs = ri.option_tokens.get(row["strike"])
                    if not legs:
                        continue
                    tok = legs.get(row["leg"])
                    if not tok:
                        continue
                    events.append((row["dt"], tok, row["ltp"] or 1.0,
                                   row["v"], row["oi"]))
        events.sort(key=lambda e: e[0])
        self.events = events

    def start(self):
        self.thread = threading.Thread(target=self._run, daemon=True, name="replay-feed")
        self.thread.start()

    def _run(self):
        if not self.events:
            print("[replay] no events — check dataset path / CSV format")
            return
        speed = float(self.cfg.a("sim", "speed", default=60))
        prev_ts = self.events[0][0]
        for ts, token, ltp, cum_vol, oi in self.events:
            if self._stop.is_set():
                return
            gap = (ts - prev_ts).total_seconds()
            if gap > 0:
                time.sleep(min(gap / max(1.0, speed), 2.0))
            prev_ts = ts
            self.state.apply_tick(token, ts, ltp, cum_vol, oi)
        print("[replay] dataset finished — dashboard holds final state")

    def stop(self):
        self._stop.set()
