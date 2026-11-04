"""Synthetic broker + tick generator for closed-market testing (RUN_MODE=sim).

Mimics the tiny slice of the Kite API the tracker uses:
  * .instruments("NSE" | "NFO")  -> instrument dump rows
  * generate_ticks(...)          -> a stream of tick dicts shaped like KiteTicker

The price path is a seeded random walk with intraday drift + a lunchtime lull,
plus a plausible option-chain whose OI shifts as spot moves, so every analytics
module has realistic-looking input. Deterministic given the seed.
"""
from __future__ import annotations

import datetime as dt
import math
import random
from typing import Iterator

from .config import Config


class SimBroker:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.seed = int(cfg.a("sim", "seed", default=42))
        self._rng = random.Random(self.seed)
        self._token = 100000
        self._nse_rows: list[dict] = []
        self._nfo_rows: list[dict] = []
        self._build_universe()

    # ---- instrument dump -------------------------------------------------
    def _next_token(self) -> int:
        self._token += 1
        return self._token

    def _build_universe(self):
        today = dt.date.today()
        # nearest Thursday + two more weekly expiries (so the dropdown is testable)
        exp1 = today + dt.timedelta(days=(3 - today.weekday()) % 7 or 7)
        expiry = exp1
        self._sim_expiries = [exp1, exp1 + dt.timedelta(days=7),
                              exp1 + dt.timedelta(days=14)]
        for ins in self.cfg.instruments:
            self._nse_rows.append({
                "instrument_token": self._next_token(),
                "tradingsymbol": ins.index_symbol,
                "name": ins.index_symbol,
                "segment": "INDICES",
                "instrument_type": "EQ",
                "exchange": "NSE",
                "strike": 0, "expiry": "",
            })
            self._nfo_rows.append({
                "instrument_token": self._next_token(),
                "tradingsymbol": f"{ins.fut_name}{expiry:%y%b}FUT".upper(),
                "name": ins.fut_name,
                "segment": "NFO-FUT",
                "instrument_type": "FUT",
                "exchange": "NFO",
                "strike": 0, "expiry": expiry.isoformat(),
            })
            base = float(self.cfg.a("sim", "start_price", ins.key, default=20000.0))
            atm = round(base / ins.strike_step) * ins.strike_step
            for exp in self._sim_expiries:
                for i in range(-ins.strikes_each_side, ins.strikes_each_side + 1):
                    strike = atm + i * ins.strike_step
                    for leg in ("CE", "PE"):
                        self._nfo_rows.append({
                            "instrument_token": self._next_token(),
                            "tradingsymbol": f"{ins.opt_name}{exp:%y%m%d}{int(strike)}{leg}".upper(),
                            "name": ins.opt_name,
                            "segment": "NFO-OPT",
                            "instrument_type": leg,
                            "exchange": "NFO",
                            "strike": strike,
                            "expiry": exp.isoformat(),
                        })

    def instruments(self, exchange: str) -> list[dict]:
        return list(self._nse_rows if exchange == "NSE" else self._nfo_rows)

    # ---- tick generation -------------------------------------------------
    def generate_ticks(self, resolved: dict, start_dt: dt.datetime) -> Iterator[list[dict]]:
        """Yield batches of ticks. One batch ~= one real render cadence."""
        open_t = self.cfg.market_open
        close_t = self.cfg.market_close
        speed = float(self.cfg.a("sim", "speed", default=40))
        tps = float(self.cfg.a("sim", "ticks_per_sec", default=8))

        # per-instrument state
        px = {}
        cum_vol = {}
        drift = {}
        for key, ri in resolved.items():
            px[key] = {
                "spot": float(self.cfg.a("sim", "start_price", key, default=20000.0)),
            }
            px[key]["fut"] = px[key]["spot"] * 1.0008  # small basis
            cum_vol[key] = 0.0
            drift[key] = self._rng.uniform(-1, 1)

        # option OI baseline (all streamed expiries)
        oi_state: dict = {}
        for key, ri in resolved.items():
            obe = ri.option_tokens_by_expiry or {ri.expiry: ri.option_tokens}
            for by_strike in obe.values():
                for strike, legs in by_strike.items():
                    for leg, tok in legs.items():
                        oi_state[tok] = self._rng.uniform(20000, 120000)

        sim_clock = dt.datetime.combine(dt.date.today(), open_t)
        end_clock = dt.datetime.combine(dt.date.today(), close_t)
        step = dt.timedelta(seconds=max(1.0, speed / tps))

        while sim_clock < end_clock:
            batch: list[dict] = []
            minutes_in = (sim_clock - dt.datetime.combine(sim_clock.date(), open_t)).seconds / 60
            lull = 0.45 if 150 < minutes_in < 240 else 1.0  # midday volume lull
            for key, ri in resolved.items():
                d = drift[key]
                # random walk with mild mean-reversion of drift
                drift[key] = d * 0.995 + self._rng.uniform(-0.05, 0.05)
                vol_scale = ri.cfg.profile_bin
                move = (drift[key] * 0.15 + self._rng.gauss(0, 1)) * vol_scale * 0.25
                px[key]["spot"] = max(1.0, px[key]["spot"] + move)
                px[key]["fut"] = px[key]["spot"] * (1.0 + self._rng.uniform(0.0004, 0.0012))
                qty = max(0, int(self._rng.gauss(900, 400) * lull))
                cum_vol[key] += qty

                batch.append(_tick(ri.spot_token, px[key]["spot"], sim_clock))
                # synthesize a 1-tick-wide book around the futures price, and bias
                # the trade to bid/ask by drift so the footprint delta is meaningful
                tsz = ri.cfg.tick_size or 0.05
                futpx = px[key]["fut"]
                bid = round(futpx - tsz, 2)
                ask = round(futpx + tsz, 2)
                # aggressor: with the drift, trade prints at ask (buy) or bid (sell)
                buy_prob = 0.5 + max(-0.35, min(0.35, drift[key] * 0.3))
                traded = ask if self._rng.random() < buy_prob else bid
                batch.append(_tick(ri.fut_token, traded, sim_clock,
                                   volume=int(cum_vol[key]),
                                   oi=1_200_000 + int(cum_vol[key] * 0.3),
                                   bid=bid, ask=ask))
                # options: OI drifts with spot; puts build below, calls above.
                # farther expiries carry a bit more time value.
                spot = px[key]["spot"]
                obe = ri.option_tokens_by_expiry or {ri.expiry: ri.option_tokens}
                for ei, (exp, by_strike) in enumerate(sorted(obe.items(), key=lambda kv: str(kv[0]))):
                    tv = 40 + ei * 35     # extra time value for later expiries
                    for strike, legs in by_strike.items():
                        for leg, tok in legs.items():
                            pull = 1.0 + (0.002 if (leg == "PE" and strike < spot)
                                          or (leg == "CE" and strike > spot) else -0.001)
                            oi_state[tok] = max(0.0, oi_state[tok] * pull
                                                + self._rng.uniform(-300, 400))
                            intrinsic = max(0.0, (spot - strike) if leg == "CE" else (strike - spot))
                            ltp = round(intrinsic + self._rng.uniform(5, tv), 2)
                            batch.append(_tick(tok, ltp, sim_clock,
                                               volume=int(oi_state[tok] * 0.05),
                                               oi=int(oi_state[tok])))
            yield batch
            sim_clock += step


def _tick(token: int, ltp: float, ts: dt.datetime,
          volume: int | None = None, oi: int | None = None,
          bid: float | None = None, ask: float | None = None) -> dict:
    t = {
        "instrument_token": token,
        "last_price": round(ltp, 2),
        "exchange_timestamp": ts,
    }
    if volume is not None:
        t["volume_traded"] = volume
    if oi is not None:
        t["oi"] = oi
    if bid is not None and ask is not None:
        t["depth"] = {"buy": [{"price": bid, "quantity": 0, "orders": 0}],
                      "sell": [{"price": ask, "quantity": 0, "orders": 0}]}
    return t
