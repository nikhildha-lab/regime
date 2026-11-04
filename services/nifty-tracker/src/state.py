"""In-memory market state shared between the feed and the analytics/dashboard.

One MarketState holds everything for all tracked instruments. The feed writes
ticks in; the analytics engine and dashboard read snapshots out. A single lock
guards mutation so the websocket callback thread and the analytics thread don't
race.
"""
from __future__ import annotations

import datetime as dt
import threading
from dataclasses import dataclass, field

from .candles import CandleSeries
from .instruments import ResolvedInstrument
from .analytics.footprint import FootprintBook


@dataclass
class OptionQuote:
    strike: float
    leg: str            # CE / PE
    ltp: float = 0.0
    ltp_open: float | None = None  # first premium seen today (for premium+OI read)
    oi: float = 0.0
    oi_open: float | None = None   # first OI seen today (for buildup %)
    volume: float = 0.0


@dataclass
class InstrumentState:
    ri: ResolvedInstrument
    spot: CandleSeries
    fut: CandleSeries
    chart_base: CandleSeries | None = None      # display-only, multi-day, base TF
    chart5: CandleSeries | None = None          # display-only, multi-day, 5-min
    options: dict[tuple[float, str], OptionQuote] = field(default_factory=dict)   # nearest expiry
    options_by_expiry: dict = field(default_factory=dict)   # exp_iso -> {(strike,leg): OptionQuote}
    last_spot: float = 0.0
    last_fut: float = 0.0
    daily: list = field(default_factory=list)   # recent daily candles (for 1D/1W chart view)
    footprint: FootprintBook | None = None      # live order-flow footprint (futures)

    def atm_strike(self) -> float | None:
        strikes = sorted({k[0] for k in self.options})
        if not strikes or not self.last_spot:
            return None
        return min(strikes, key=lambda s: abs(s - self.last_spot))


class MarketState:
    def __init__(self, resolved: dict[str, ResolvedInstrument], candle_minutes: int):
        self.lock = threading.RLock()
        self.candle_minutes = candle_minutes
        self.instruments: dict[str, InstrumentState] = {}
        self.token_index: dict[int, tuple[str, str, tuple | None]] = {}
        self.session_date = dt.date.today()

        for key, ri in resolved.items():
            st = InstrumentState(ri=ri,
                                 spot=CandleSeries(candle_minutes),
                                 fut=CandleSeries(candle_minutes),
                                 chart_base=CandleSeries(candle_minutes),
                                 chart5=CandleSeries(5),
                                 footprint=FootprintBook(ri.cfg.profile_bin,
                                                         candle_minutes))
            # build an OptionQuote book per streamed expiry (fallback: nearest only)
            obe = ri.option_tokens_by_expiry or (
                {ri.expiry: ri.option_tokens} if ri.option_tokens else {})
            for exp, by_strike in obe.items():
                exp_iso = exp.isoformat() if hasattr(exp, "isoformat") else str(exp)
                book: dict = {}
                for strike, legs in by_strike.items():
                    for leg, tok in legs.items():
                        book[(strike, leg)] = OptionQuote(strike=strike, leg=leg)
                        self.token_index[tok] = (key, "opt", (strike, leg, exp_iso))
                st.options_by_expiry[exp_iso] = book
            near_iso = (ri.expiry.isoformat() if ri.expiry
                        else (next(iter(st.options_by_expiry), None)))
            st.options = st.options_by_expiry.get(near_iso, {})
            self.instruments[key] = st
            self.token_index[ri.spot_token] = (key, "spot", None)
            self.token_index[ri.fut_token] = (key, "fut", None)

    def apply_tick(self, token: int, ts: dt.datetime, ltp: float,
                   cum_volume: float | None = None, oi: float | None = None,
                   bid: float | None = None, ask: float | None = None):
        loc = self.token_index.get(token)
        if not loc:
            return
        key, role, sub = loc
        with self.lock:
            st = self.instruments[key]
            if role == "spot":
                st.spot.on_tick(ts, ltp)
                if st.chart_base is not None:
                    st.chart_base.on_tick(ts, ltp)
                if st.chart5 is not None:
                    st.chart5.on_tick(ts, ltp)
                st.last_spot = ltp
            elif role == "fut":
                st.fut.on_tick(ts, ltp, cum_volume=cum_volume, oi=oi)
                st.last_fut = ltp
                if st.footprint is not None:
                    st.footprint.on_tick(ts, ltp, cum_volume, bid=bid, ask=ask)
            elif role == "opt":
                strike, leg, exp_iso = sub
                book = st.options_by_expiry.get(exp_iso)
                q = book.get((strike, leg)) if book else None
                if q is not None:
                    if q.ltp_open is None and ltp > 0:
                        q.ltp_open = ltp
                    q.ltp = ltp
                    if oi is not None:
                        if q.oi_open is None:
                            q.oi_open = oi
                        q.oi = oi
                    if cum_volume is not None:
                        q.volume = cum_volume
