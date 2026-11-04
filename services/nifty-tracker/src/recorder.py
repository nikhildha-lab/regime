"""Capture the live stream to disk as a replay dataset (real granular history).

Two layers of granularity:

1. Bars + OI snapshots (default, compact): each cycle, newly-completed 5-min
   candles for spot & futures are appended to
   `datasets/live-<date>/<KEY>_{spot,fut}.csv`, and the full ATM option-chain OI
   is snapshotted to `<KEY>_options.csv` every `oi_snapshot_sec`. This yields a
   real, replayable dataset for the day — including live-captured option OI.

2. Raw ticks (optional, `record.raw_ticks: true`): every websocket tick is
   appended to `datasets/live-<date>/ticks.jsonl` for maximum granularity /
   custom reconstruction. Can grow large; off by default.

Because live-recorded datasets use the exact replay CSV format, tomorrow you can:
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
        self.oi_snapshot_sec = int(cfg.a("record", "oi_snapshot_sec", default=300))
        self.raw_ticks = bool(cfg.a("record", "raw_ticks", default=False))
        d = session_date or dt.date.today()
        self.dir = ROOT / "datasets" / f"live-{d.isoformat()}"
        self._written_bar: dict[tuple, dt.datetime] = {}   # (key,role)->last bar start
        self._last_oi_snap = 0.0
        self._headers_done: set[Path] = set()
        self._ticks_fh = None
        if self.enabled:
            self.dir.mkdir(parents=True, exist_ok=True)

    # ---- bar + OI capture (call each engine cycle) -----------------------
    def capture(self, state: MarketState):
        if not self.enabled:
            return
        with state.lock:
            for key, st in state.instruments.items():
                self._append_bars(key, "spot", st.spot, with_oi=False)
                self._append_bars(key, "fut", st.fut, with_oi=True)
        now = time.time()
        if now - self._last_oi_snap >= self.oi_snapshot_sec:
            self._last_oi_snap = now
            self._snapshot_oi(state)

    def _append_bars(self, key, role, series, with_oi):
        completed = series.completed  # only closed candles
        if not completed:
            return
        path = self.dir / f"{key}_{role}.csv"
        header = ["datetime", "open", "high", "low", "close"] + \
                 (["volume", "oi"] if with_oi else [])
        last = self._written_bar.get((key, role))
        new = [c for c in completed if last is None or c.start > last]
        if not new:
            return
        self._ensure_header(path, header)
        with path.open("a", newline="") as f:
            w = csv.writer(f)
            for c in new:
                row = [c.start.strftime("%Y-%m-%d %H:%M"),
                       round(c.open, 2), round(c.high, 2), round(c.low, 2), round(c.close, 2)]
                if with_oi:
                    row += [int(c.volume), int(c.oi)]
                w.writerow(row)
        self._written_bar[(key, role)] = new[-1].start

    def _snapshot_oi(self, state: MarketState):
        ts = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
        with state.lock:
            for key, st in state.instruments.items():
                if not st.options:
                    continue
                path = self.dir / f"{key}_options.csv"
                self._ensure_header(path, ["datetime", "strike", "leg", "oi", "ltp"])
                with path.open("a", newline="") as f:
                    w = csv.writer(f)
                    for (strike, leg), q in sorted(st.options.items()):
                        w.writerow([ts, int(strike), leg, int(q.oi), round(q.ltp, 2)])

    def _ensure_header(self, path: Path, header):
        if path in self._headers_done:
            return
        if not path.exists():
            with path.open("w", newline="") as f:
                csv.writer(f).writerow(header)
        self._headers_done.add(path)

    # ---- raw tick capture (call from feed on each tick) ------------------
    def raw_tick(self, token, ts, ltp, cum_vol, oi, bid=None, ask=None):
        if not (self.enabled and self.raw_ticks):
            return
        if self._ticks_fh is None:
            self._ticks_fh = (self.dir / "ticks.jsonl").open("a")
        rec = {"t": ts.isoformat() if hasattr(ts, "isoformat") else ts,
               "tok": token, "ltp": ltp, "cv": cum_vol, "oi": oi}
        if bid is not None:
            rec["bid"] = bid
        if ask is not None:
            rec["ask"] = ask
        self._ticks_fh.write(json.dumps(rec) + "\n")

    def close(self):
        if self._ticks_fh:
            self._ticks_fh.close()
