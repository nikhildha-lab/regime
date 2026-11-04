"""Analytics engine: reads MarketState, runs every module, emits a snapshot.

The snapshot is a plain dict (JSON-serializable) consumed by:
  * the dashboard websocket,
  * the alert dispatcher,
  * snapshot.json (the bridge to the indian-trading-skills analyst skill).
"""
from __future__ import annotations

import datetime as dt

from .config import Config
from .state import MarketState
import datetime as _dt

from .analytics import (price_action, volume_profile, market_profile,
                        oi_analysis, imbalance, signals as sig, trade_plan,
                        participation, regime, footprint)
from .analytics import divergence
from .analytics import setup as setup_mod
from .analytics.levels import compute_prior_levels
from . import trade_ticket
from .analytics.depth_book import BOOK as _DEPTH


class Engine:
    def __init__(self, cfg: Config, state: MarketState):
        self.cfg = cfg
        self.state = state
        self.va_pct = float(cfg.a("analytics", "value_area_pct", default=0.70))
        self.k = int(cfg.a("analytics", "swing_lookback", default=2))
        self.orb = int(cfg.a("analytics", "opening_range_minutes", default=15))
        self.min_conf = int(cfg.a("signals", "min_confluence", default=2))
        self.spike = float(cfg.a("signals", "volume_spike_mult", default=2.0))
        self.tpo_period = int(cfg.a("analytics", "tpo_period_minutes", default=30))
        self.rr_min = float(cfg.a("trade_plan", "rr_min", default=1.5))
        self.part_lookback = int(cfg.a("analytics", "participation_lookback", default=3))
        self.part_vol_mult = float(cfg.a("analytics", "participation_vol_mult", default=1.5))
        # regime timeframe: read the stance on a higher TF than entries (0/None = same)
        self.regime_tf = int(cfg.a("analytics", "regime_timeframe", default=0) or 0)
        # footprint / order-flow params
        self.fp_bars = int(cfg.a("footprint", "bars_shown", default=12))
        self.fp_ratio = float(cfg.a("footprint", "imbalance_ratio", default=3.0))
        self.fp_vol_mult = float(cfg.a("footprint", "vol_mult", default=1.5))
        # which prior-period levels to compute (from the fetched daily history)
        want = []
        if bool(cfg.a("levels", "prior_day", default=True)):
            want += ["pdh", "pdl"]
        if bool(cfg.a("levels", "prior_week", default=True)):
            want += ["pwh", "pwl"]
        if bool(cfg.a("levels", "prior_month", default=False)):
            want += ["pmh", "pml"]
        self.level_want = tuple(want)
        self.div_lookback = int(cfg.a("divergence", "health_lookback", default=6))
        # risk profile drives the min actionable grade (one master switch)
        self.risk_profile = str(cfg.a("risk", "profile", default="balanced"))
        self.setup_min_grade = str(cfg.a("risk", "profiles", self.risk_profile,
                                         "min_grade", default="B"))
        self.conf_a = int(cfg.a("setup", "confluence_score_a", default=2))
        self.conf_b = int(cfg.a("setup", "confluence_score_b", default=4))
        self.target_delta = float(cfg.a("setup", "target_delta", default=0.6))
        t1 = cfg.a("setup", "tier1_levels", default=None)
        self.tier1 = tuple(t1) if t1 else setup_mod.DEFAULT_TIER1
        self.chart_bars = int(cfg.a("dashboard", "chart_bars", default=96))

    def compute(self) -> dict:
        try:
            _DEPTH.poll()                      # 20-level book from the Dhan depth recorder
        except Exception:
            pass
        with self.state.lock:
            insts = {k: v for k, v in self.state.instruments.items()}
            snap_instruments = {}
            all_signals = []
            for key, st in insts.items():
                cm = self.cfg.candle_minutes
                spot_c = st.spot.all_candles()
                fut_c = st.fut.all_candles()
                price = st.last_spot or (spot_c[-1].close if spot_c else None)

                pa = price_action.analyze(
                    spot_c, k=self.k, orb_minutes=self.orb,
                    candle_minutes=cm, tick_size=st.ri.cfg.tick_size)
                vp = volume_profile.analyze(
                    fut_c, bin_width=st.ri.cfg.profile_bin,
                    value_area_pct=self.va_pct, last_price=st.last_fut or price)
                mp = market_profile.analyze(
                    spot_c, bin_width=st.ri.cfg.profile_bin, candle_minutes=cm,
                    ib_minutes=self.cfg.ib_minutes, value_area_pct=self.va_pct,
                    tpo_period_minutes=self.tpo_period)
                oi = oi_analysis.analyze(
                    st,
                    pcr_bull_below=float(self.cfg.a("analytics", "pcr_bull_below", default=0.7)),
                    pcr_bear_above=float(self.cfg.a("analytics", "pcr_bear_above", default=1.3)),
                    buildup_pct=float(self.cfg.a("analytics", "oi_buildup_pct", default=5.0)))
                imb = imbalance.analyze(fut_c, spike_mult=self.spike)
                # days-to-expiry (option theta context for the plan + roll warning)
                dte = None
                if st.ri.expiry:
                    dte = (st.ri.expiry - _dt.date.today()).days
                # futures price+OI participation (long/short buildup, covering, unwind)
                part = participation.analyze(
                    fut_c, lookback=self.part_lookback,
                    vol_spike_mult=self.part_vol_mult, dte=dte)
                # regime on a higher timeframe (aggregate candles up) if configured
                if self.regime_tf and self.regime_tf > cm:
                    from .candles import aggregate_candles
                    agg_s = aggregate_candles(spot_c, self.regime_tf)
                    agg_f = aggregate_candles(fut_c, self.regime_tf)
                    pa_r = price_action.analyze(agg_s, k=self.k, orb_minutes=self.orb,
                                                candle_minutes=self.regime_tf,
                                                tick_size=st.ri.cfg.tick_size)
                    vp_r = volume_profile.analyze(agg_f, bin_width=st.ri.cfg.profile_bin,
                                                  value_area_pct=self.va_pct,
                                                  last_price=st.last_fut or price)
                    mp_r = market_profile.analyze(agg_s, bin_width=st.ri.cfg.profile_bin,
                                                  candle_minutes=self.regime_tf,
                                                  ib_minutes=self.cfg.ib_minutes,
                                                  value_area_pct=self.va_pct,
                                                  tpo_period_minutes=max(self.tpo_period, self.regime_tf))
                    reg = regime.classify(pa_r, vp_r, mp_r, price, oi=oi, part=part)
                    reg.tf = self.regime_tf
                else:
                    reg = regime.classify(pa, vp, mp, price, oi=oi, part=part)
                    reg.tf = cm

                # order-flow footprint: where did volume trade & who was aggressor,
                # and is a key level being absorbed? (live confirmation, not backtested)
                fp_tol = float(self.cfg.a("footprint", "level_tol", key,
                                          default=st.ri.cfg.profile_bin * 2))
                # prior-period levels (static, from fetched daily history)
                prior = compute_prior_levels(st.daily, want=self.level_want)
                fp_levels = {
                    "IB-low": mp.ib_low, "IB-high": mp.ib_high,
                    "VAL": vp.val, "VAH": vp.vah, "POC": vp.poc,
                    "put-wall": getattr(oi, "put_wall", None),
                    "call-wall": getattr(oi, "call_wall", None),
                    "swing-low": pa.last_swing_low, "swing-high": pa.last_swing_high,
                    "VWAP": pa.vwap,
                    "PDH": prior.get("pdh"), "PDL": prior.get("pdl"),
                    "PWH": prior.get("pwh"), "PWL": prior.get("pwl"),
                    "PMH": prior.get("pmh"), "PML": prior.get("pml"),
                }
                fp = footprint.analyze(st.footprint, fp_levels,
                                       bars_shown=self.fp_bars,
                                       imbalance_ratio=self.fp_ratio,
                                       tol=fp_tol, vol_mult=self.fp_vol_mult)

                # delta divergence: price swing pivots vs cumulative footprint delta
                sw = [{"t": s.time, "p": s.price, "k": s.kind} for s in pa.swings]
                div = divergence.analyze(
                    sw, st.footprint.delta_series() if st.footprint else [],
                    pa.trend, health_lookback=self.div_lookback)

                sigs = sig.build_signals(key, price, pa, vp, mp, oi, imb,
                                         min_confluence=self.min_conf, part=part)
                # ATR proxy: mean 5-min range over last 14 candles (adaptive stop floor)
                rng = [c.high - c.low for c in spot_c[-14:]]
                atr = (sum(rng) / len(rng)) if rng else None
                # ATR *band* = typical DAILY range (for the option-chain ±1ATR focus).
                # Uses fetched daily history (live); falls back to today's realized range.
                atr_band = None
                dr = [d["h"] - d["l"] for d in (st.daily or [])[-14:]
                      if isinstance(d, dict) and "h" in d and "l" in d]
                if dr:
                    atr_band = sum(dr) / len(dr)
                elif spot_c:
                    atr_band = max(c.high for c in spot_c) - min(c.low for c in spot_c)

                # graded 'A+ reversal' setup (reversal + level tier + divergence +
                # soft regime) and, if actionable, an option trade ticket
                # agreeing volume/OI/structure signal score (Tier-2 confluence)
                _side = fp.read["side"] if fp.read else None
                agree = max([s.score for s in sigs if s.direction == _side],
                            default=0) if _side else 0
                setup_read = setup_mod.grade(fp.read, div, reg, pa, price,
                                             tier1=self.tier1,
                                             min_grade=self.setup_min_grade,
                                             agree_score=agree,
                                             conf_a=self.conf_a, conf_b=self.conf_b)
                ticket = None
                if setup_read:
                    setup_read["t"] = (fp.read or {}).get("t")
                if setup_read and setup_read.get("actionable"):
                    plan_s = trade_plan.make_plan(
                        setup_read["side"], price, pa, vp, mp, oi, imb,
                        tick_size=st.ri.cfg.tick_size, profile_bin=st.ri.cfg.profile_bin,
                        rr_min=self.rr_min, dte=dte, atr=atr)
                    setup_read["entry"] = round(price, 2)
                    setup_read["stop_loss"] = plan_s.get("stop_loss")
                    setup_read["target1"] = plan_s.get("target1")
                    setup_read["rr"] = plan_s.get("rr")
                    ticket = trade_ticket.build(setup_read, price, st, self.cfg,
                                                plan_s, dte)
                sig_dicts = []
                for s in sigs:
                    d = s.to_dict()
                    d["plan"] = trade_plan.make_plan(
                        s.direction, price, pa, vp, mp, oi, imb,
                        tick_size=st.ri.cfg.tick_size, profile_bin=st.ri.cfg.profile_bin,
                        rr_min=self.rr_min, dte=dte, atr=atr)
                    d["with_regime"] = regime.aligns(reg, s.direction, price)
                    d["footprint_confirm"] = fp.confirms(s.direction)
                    d["divergence_confirm"] = div.confirms(s.direction)
                    sig_dicts.append(d)
                all_signals.extend([(s, d) for s, d in zip(sigs, sig_dicts)])

                snap_instruments[key] = {
                    "key": key,
                    "spot": None if price is None else round(price, 2),
                    "fut": round(st.last_fut, 2) if st.last_fut else None,
                    "atr_band": round(atr_band, 1) if atr_band else None,
                    "expiry": st.ri.expiry.isoformat() if st.ri.expiry else None,
                    "candles": [c.as_dict() for c in spot_c[-80:]],
                    "fut_candles": [c.as_dict() for c in fut_c[-80:]],
                    "chart_candles": ([c.as_dict() for c in st.chart_base.all_candles()[-self.chart_bars:]]
                                      if getattr(st, "chart_base", None) is not None else []),
                    "candles5": ([c.as_dict() for c in st.chart5.all_candles()[-self.chart_bars:]]
                                 if getattr(st, "chart5", None) is not None else []),
                    "daily_candles": list(st.daily),
                    "cvd_series": (st.footprint.delta_series()
                                   if st.footprint is not None else []),
                    "depth": _DEPTH.get(key),
                    "price_action": pa.to_dict(),
                    "volume_profile": vp.to_dict(),
                    "market_profile": mp.to_dict(),
                    "oi": oi.to_dict(),
                    "expiries": [e.isoformat() for e in (st.ri.expiries or ([st.ri.expiry] if st.ri.expiry else []))],
                    "near_expiry": st.ri.expiry.isoformat() if st.ri.expiry else None,
                    "chains": {exp_iso: oi_analysis.compact_chain(book, price)
                               for exp_iso, book in st.options_by_expiry.items()},
                    "imbalance": imb.to_dict(),
                    "participation": part.to_dict(),
                    "regime": reg.to_dict(),
                    "footprint": fp.to_dict(),
                    "divergence": div.to_dict(),
                    "prior_levels": prior,
                    "setup": setup_read,
                    "ticket": ticket,
                    "signals": sig_dicts,
                }

            top = sorted(all_signals, key=lambda sd: -sd[0].score)[:8]
            return {
                "generated_at": _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "mode": self.cfg.run_mode,
                "risk_profile": self.risk_profile,
                "config": {"profile": self.risk_profile, "min_grade": self.setup_min_grade,
                           "target_delta": self.target_delta},
                "candle_minutes": self.cfg.candle_minutes,
                "instruments": snap_instruments,
                "top_signals": [d for _, d in top],
            }
