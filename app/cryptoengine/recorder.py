"""Capture the live stream to disk as a replay dataset (real granular history).

Each cycle, newly-completed 5-min perp candles (with volume + OI) are appended to
`datasets/live-<date>/<KEY>_perp.csv`, and the current OI/funding/mark context is
snapshotted to `<KEY>_ctx.csv` every `ctx_snapshot_sec`. Optionally every trade is
logged to `trades.jsonl` (`record.raw_ticks: true`). Because the format matches
the replay loader, tomorrow you can:

    python run.py --mode replay --dataset datasets/live-2026-07-20
"""
from __future__ import annotations

import csv
import datetime as dt
import json
import time
from pathlib import Path

from .config import Config, ROOT
from .state import MarketState


class Recorder:
    def __init__(self, cfg: Config, session_date: dt.date | None = None):
        self.cfg = cfg
        self.enabled = bool(cfg.a("record", "enabled", default=True))
        self.ctx_snapshot_sec = int(cfg.a("record", "ctx_snapshot_sec", default=300))
        self.raw_ticks = bool(cfg.a("record", "raw_ticks", default=False))
        d = session_date or dt.datetime.utcnow().date()
        self.dir = ROOT / "datasets" / f"live-{d.isoformat()}"
        self._written_bar: dict[str, dt.datetime] = {}
        self._last_ctx_snap = 0.0
        self._headers_done: set = set()
        self._trades_fh = None
        if self.enabled:
            self.dir.mkdir(parents=True, exist_ok=True)

    def capture(self, state: MarketState):
        if not self.enabled:
            return
        with state.lock:
            for key, st in state.instruments.items():
                self._append_bars(key, st.perp)
        now = time.time()
        if now - self._last_ctx_snap >= self.ctx_snapshot_sec:
            self._last_ctx_snap = now
            self._snapshot_ctx(state)

    def _append_bars(self, key, series):
        completed = series.completed
        if not completed:
            return
        path = self.dir / f"{key}_perp.csv"
        last = self._written_bar.get(key)
        new = [c for c in completed if last is None or c.start > last]
        if not new:
            return
        self._ensure_header(path, ["datetime", "open", "high", "low", "close", "volume", "oi"])
        with path.open("a", newline="") as f:
            w = csv.writer(f)
            for c in new:
                w.writerow([c.start.strftime("%Y-%m-%d %H:%M"),
                            round(c.open, 4), round(c.high, 4), round(c.low, 4),
                            round(c.close, 4), round(c.volume, 6), round(c.oi, 2)])
        self._written_bar[key] = new[-1].start

    def _snapshot_ctx(self, state: MarketState):
        ts = dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M")
        with state.lock:
            for key, st in state.instruments.items():
                path = self.dir / f"{key}_ctx.csv"
                self._ensure_header(path, ["datetime", "mark", "funding", "oi"])
                with path.open("a", newline="") as f:
                    csv.writer(f).writerow([ts, round(st.mark or 0, 4),
                                            st.funding if st.funding is not None else "",
                                            round(st.oi or 0, 2)])

    def _ensure_header(self, path: Path, header):
        if path in self._headers_done:
            return
        if not path.exists():
            with path.open("w", newline="") as f:
                csv.writer(f).writerow(header)
        self._headers_done.add(path)

    def raw_trade(self, coin, ts, price, size, is_buy):
        if not (self.enabled and self.raw_ticks):
            return
        if self._trades_fh is None:
            self._trades_fh = (self.dir / "trades.jsonl").open("a")
        self._trades_fh.write(json.dumps({
            "t": ts.isoformat() if hasattr(ts, "isoformat") else ts,
            "coin": coin, "px": price, "sz": size, "buy": bool(is_buy)}) + "\n")

    def ctx(self, coin, ctx):
        pass  # live ctx already snapshotted on a timer via capture()

    def close(self):
        if self._trades_fh:
            self._trades_fh.close()
