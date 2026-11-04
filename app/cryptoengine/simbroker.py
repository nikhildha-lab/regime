"""Synthetic market generator for closed-feed testing (RUN_MODE=sim).

Produces a plausible 24-hour crypto tape for BTC/ETH/SOL with NO network: a
seeded random walk with intraday drift and a quiet-hours volume lull, emitting
REAL trade prints (each tagged buy/sell so the footprint delta is meaningful),
plus periodic context events (mark, open interest that drifts with price, and a
funding rate that leans with the trend). Deterministic given the seed.

Mirrors the tiny slice of the live data source the tracker consumes, so every
analytics module gets realistic input with the market "closed" (there is no such
thing for crypto — this is purely for offline testing).
"""
from __future__ import annotations

import datetime as dt
import random
from typing import Iterator

from .config import Config


class SimBroker:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.seed = int(cfg.a("sim", "seed", default=42))
        self._rng = random.Random(self.seed)

    def universe(self) -> dict:
        out = {}
        for ins in self.cfg.instruments:
            # sensible size-precision defaults per coin for the sim
            szd = {"BTC": 5, "ETH": 4, "SOL": 2}.get(ins.coin, 3)
            out[ins.coin] = {"szDecimals": szd, "maxLeverage": 50}
        return out

    def generate(self, resolved: dict, start_dt: dt.datetime) -> Iterator[list]:
        """Yield batches of events. Each event is a tuple:
            ("trade", coin, ts, price, size, is_buy)
            ("ctx",   coin, ts, mark, funding, oi, day_vol_usd, prev_day_px)
        One batch ~= one render cadence.
        """
        speed = float(self.cfg.a("sim", "speed", default=30))
        tps = float(self.cfg.a("sim", "ticks_per_sec", default=8))
        cm = self.cfg.candle_minutes

        px, drift, oi, prev_close = {}, {}, {}, {}
        base_size = {}
        for key, ri in resolved.items():
            start = float(self.cfg.a("sim", "start_price", key, default=1000.0))
            px[key] = start
            prev_close[key] = start * (1 - self._rng.uniform(-0.01, 0.02))
            drift[key] = self._rng.uniform(-1, 1)
            oi[key] = self._rng.uniform(5000, 50000)
            # typical per-print size in coin units (bigger notional coins trade smaller size)
            base_size[key] = max(0.001, 2000.0 / start)

        # anchor the sim clock to the start of the UTC day
        anchor = dt.datetime.combine(start_dt.date(), self.cfg.market_open)
        end_clock = anchor + dt.timedelta(hours=24) - dt.timedelta(minutes=1)
        sim_clock = anchor
        step = dt.timedelta(seconds=max(1.0, speed / tps))
        ctx_accum = 0.0

        while sim_clock < end_clock:
            batch = []
            hour = sim_clock.hour + sim_clock.minute / 60.0
            # a quiet stretch overnight UTC (crypto's lower-liquidity window)
            lull = 0.5 if 2 <= hour < 7 else 1.0
            for key, ri in resolved.items():
                d = drift[key]
                drift[key] = d * 0.995 + self._rng.uniform(-0.05, 0.05)
                vol_scale = ri.cfg.profile_bin
                move = (drift[key] * 0.15 + self._rng.gauss(0, 1)) * vol_scale * 0.4
                px[key] = max(0.01, px[key] + move)
                # 1-3 trade prints this step, biased buy/sell by drift
                buy_prob = 0.5 + max(-0.35, min(0.35, drift[key] * 0.3))
                n_tr = max(1, int(self._rng.gauss(2.0, 1.0) * lull))
                for _ in range(n_tr):
                    is_buy = self._rng.random() < buy_prob
                    jitter = self._rng.uniform(-ri.cfg.tick_size, ri.cfg.tick_size)
                    tp = round(px[key] + jitter, 4)
                    sz = round(abs(self._rng.gauss(base_size[key], base_size[key] * 0.6))
                               * lull, 6)
                    batch.append(("trade", ri.coin, sim_clock, tp, sz, is_buy))
                # OI drifts, expands when trending
                oi[key] = max(100.0, oi[key] * (1 + drift[key] * 0.0006)
                              + self._rng.uniform(-40, 60))

            ctx_accum += step.total_seconds()
            if ctx_accum >= 30:      # emit context ~every 30 sim-seconds
                ctx_accum = 0.0
                for key, ri in resolved.items():
                    funding = max(-0.0006, min(0.0006, drift[key] * 0.00004
                                               + self._rng.uniform(-0.00002, 0.00002)))
                    batch.append(("ctx", ri.coin, sim_clock, round(px[key], 4),
                                  round(funding, 8), round(oi[key], 2),
                                  round(px[key] * oi[key], 0), round(prev_close[key], 4)))
            yield batch
            sim_clock += step
