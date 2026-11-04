"""Analytics engine: reads MarketState, runs every module, emits a snapshot.

The snapshot is a plain dict (JSON-serializable) consumed by the dashboard
websocket, the alert dispatcher, and snapshot.json (the LLM-analyst bridge).

Crypto specifics: each coin has ONE perpetual candle series that carries price +
real volume + open interest, so the same series feeds price-action / market
profile (structure), volume profile / imbalance / participation (flow), and the
footprint. The option-chain OI panel is replaced by perp OI + funding +
estimated liquidation magnets (see analytics/oi_analysis.py).
"""
from __future__ import annotations

import datetime as _dt

from .config import Config
from .state import MarketState

from .analytics import (price_action, volume_profile, market_profile,
                        oi_analysis, imbalance, signals as sig, trade_plan,
                        participation, regime, footprint)
from .analytics import divergence
from .analytics import setup as setup_mod
from .analytics.levels import compute_prior_levels
from . import trade_ticket


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
        self.regime_tf = int(cfg.a("analytics", "regime_timeframe", default=0) or 0)
        # funding / liquidation params
        self.fund_long = float(cfg.a("funding", "crowded_long_annual", default=30.0))
        self.fund_short = float(cfg.a("funding", "crowded_short_annual", default=-8.0))
        self.lev_tiers = list(cfg.a("liquidations", "leverage_tiers", default=[10, 25, 50]))
        self.cluster_bin = float(cfg.a("liquidations", "cluster_bin_pct", default=0.4))
        self.oi_buildup = float(cfg.a("analytics", "oi_buildup_pct", default=3.0))
        # footprint params
        self.fp_bars = int(cfg.a("footprint", "bars_shown", default=12))
        self.fp_ratio = float(cfg.a("footprint", "imbalance_ratio", default=3.0))
        self.fp_vol_mult = float(cfg.a("footprint", "vol_mult", default=1.5))
        # prior-period levels to compute
        want = []
        if bool(cfg.a("levels", "prior_day", default=True)):
            want += ["pdh", "pdl"]
        if bool(cfg.a("levels", "prior_week", default=True)):
            want += ["pwh", "pwl"]
        if bool(cfg.a("levels", "prior_month", default=False)):
            want += ["pmh", "pml"]
        self.level_want = tuple(want)
        self.div_lookback = int(cfg.a("divergence", "health_lookback", default=6))
        self.risk_profile = str(cfg.a("risk", "profile", default="balanced"))
        self.setup_min_grade = str(cfg.a("risk", "profiles", self.risk_profile,
                                         "min_grade", default="B"))
        self.conf_a = int(cfg.a("setup", "confluence_score_a", default=2))
        self.conf_b = int(cfg.a("setup", "confluence_score_b", default=4))
        t1 = cfg.a("setup", "tier1_levels", default=None)
        self.tier1 = tuple(t1) if t1 else setup_mod.DEFAULT_TIER1
        self.chart_bars = int(cfg.a("dashboard", "chart_bars", default=96))
        self._cache: dict = {}   # key -> {'bar': bar_start, 'snap': dict} (scale: 50+ coins)

    def compute(self, focus=None) -> dict:
        """focus: iterable of keys recomputed EVERY call. All other instruments do
        full analytics only when their bar rolls (scale to 50+ coins); between
        rolls their cached snapshot gets a cheap price refresh."""
        focus = set(focus or ())
        with self.state.lock:
            insts = {k: v for k, v in self.state.instruments.items()}
            snap_instruments = {}
            all_signals = []
            for key, st in insts.items():
                cm = self.cfg.candle_minutes
                _cur_bar = (st.perp.current.start if st.perp.current is not None
                            else (st.perp.completed[-1].start if st.perp.completed else None))
                _hit = self._cache.get(key)
                if (_hit is not None and key not in focus and _hit["bar"] == _cur_bar):
                    d = _hit["snap"]
                    _px = st.last_price or st.mark
                    if _px:
                        d["spot"] = round(_px, 4)
                        if st.mark:
                            d["mark"] = round(st.mark, 4)
                    snap_instruments[key] = d
                    continue
                # ONE perp series drives structure AND volume/flow.
                # The trading day is anchored to 00:00 UTC (= 05:30 IST): every day
                # at that boundary the intraday analytics + chart RESET, so VWAP,
                # Initial Balance, ORB, volume/market profile and structure are all
                # scoped to the current day only (not the whole run history).
                _all_c = st.perp.all_candles()
                _ref = _all_c[-1].start if _all_c else _dt.datetime.utcnow()
                _day0 = _dt.datetime.combine(_ref.date(), _dt.time(0, 0))
                perp_c = [c for c in _all_c if c.start >= _day0]
                price = st.mark or st.last_price or (perp_c[-1].close if perp_c else None)

                pa = price_action.analyze(
                    perp_c, k=self.k, orb_minutes=self.orb,
                    candle_minutes=cm, tick_size=st.ri.cfg.tick_size)
                vp = volume_profile.analyze(
                    perp_c, bin_width=st.ri.cfg.profile_bin,
                    value_area_pct=self.va_pct, last_price=price)
                mp = market_profile.analyze(
                    perp_c, bin_width=st.ri.cfg.profile_bin, candle_minutes=cm,
                    ib_minutes=self.cfg.ib_minutes, value_area_pct=self.va_pct,
                    tpo_period_minutes=self.tpo_period)
                oi = oi_analysis.analyze(
                    st, price=price,
                    funding_long_ann=self.fund_long, funding_short_ann=self.fund_short,
                    lev_tiers=self.lev_tiers, cluster_bin_pct=self.cluster_bin,
                    oi_buildup_pct=self.oi_buildup)
                imb = imbalance.analyze(perp_c, spike_mult=self.spike)
                # perp price+OI participation (long/short buildup, covering, unwind)
                part = participation.analyze(
                    perp_c, lookback=self.part_lookback,
                    vol_spike_mult=self.part_vol_mult, dte=None)

                if self.regime_tf and self.regime_tf > cm:
                    from .candles import aggregate_candles
                    agg = aggregate_candles(perp_c, self.regime_tf)
                    pa_r = price_action.analyze(agg, k=self.k, orb_minutes=self.orb,
                                                candle_minutes=self.regime_tf,
                                                tick_size=st.ri.cfg.tick_size)
                    vp_r = volume_profile.analyze(agg, bin_width=st.ri.cfg.profile_bin,
                                                  value_area_pct=self.va_pct,
                                                  last_price=price)
                    mp_r = market_profile.analyze(agg, bin_width=st.ri.cfg.profile_bin,
                                                  candle_minutes=self.regime_tf,
                                                  ib_minutes=self.cfg.ib_minutes,
                                                  value_area_pct=self.va_pct,
                                                  tpo_period_minutes=max(self.tpo_period, self.regime_tf))
                    reg = regime.classify(pa_r, vp_r, mp_r, price, oi=oi, part=part)
                    reg.tf = self.regime_tf
                else:
                    reg = regime.classify(pa, vp, mp, price, oi=oi, part=part)
                    reg.tf = cm

                fp_tol = float(self.cfg.a("footprint", "level_tol", key,
                                          default=st.ri.cfg.profile_bin * 2))
                prior = compute_prior_levels(st.daily, want=self.level_want)
                fp_levels = {
                    "IB-low": mp.ib_low, "IB-high": mp.ib_high,
                    "VAL": vp.val, "VAH": vp.vah, "POC": vp.poc,
                    "LiqDn": oi.liq_dn, "LiqUp": oi.liq_up,
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

                sw = [{"t": s.time, "p": s.price, "k": s.kind} for s in pa.swings]
                div = divergence.analyze(
                    sw, st.footprint.delta_series() if st.footprint else [],
                    pa.trend, health_lookback=self.div_lookback)

                sigs = sig.build_signals(key, price, pa, vp, mp, oi, imb,
                                         min_confluence=self.min_conf, part=part)
                rng = [c.high - c.low for c in perp_c[-14:]]
                atr = (sum(rng) / len(rng)) if rng else None
                atr_band = None
                dr = [d["h"] - d["l"] for d in (st.daily or [])[-14:]
                      if isinstance(d, dict) and "h" in d and "l" in d]
                if dr:
                    atr_band = sum(dr) / len(dr)
                elif perp_c:
                    atr_band = max(c.high for c in perp_c) - min(c.low for c in perp_c)

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
                        rr_min=self.rr_min, dte=None, atr=atr)
                    setup_read["entry"] = float(f"{price:.6g}")
                    setup_read["stop_loss"] = plan_s.get("stop_loss")
                    setup_read["target1"] = plan_s.get("target1")
                    setup_read["rr"] = plan_s.get("rr")
                    ticket = trade_ticket.build(setup_read, price, st, self.cfg, plan_s)

                sig_dicts = []
                for s in sigs:
                    d = s.to_dict()
                    d["plan"] = trade_plan.make_plan(
                        s.direction, price, pa, vp, mp, oi, imb,
                        tick_size=st.ri.cfg.tick_size, profile_bin=st.ri.cfg.profile_bin,
                        rr_min=self.rr_min, dte=None, atr=atr)
                    d["with_regime"] = regime.aligns(reg, s.direction, price)
                    d["footprint_confirm"] = fp.confirms(s.direction)
                    d["divergence_confirm"] = div.confirms(s.direction)
                    sig_dicts.append(d)
                all_signals.extend([(s, d) for s, d in zip(sigs, sig_dicts)])

                snap_instruments[key] = {
                    "key": key,
                    "coin": st.ri.coin,
                    "source": getattr(st.ri, "source", "hl"),
                    "spot": None if price is None else round(price, 4),
                    "mark": round(st.mark, 4) if st.mark else None,
                    "atr_band": round(atr_band, 2) if atr_band else None,
                    "day_vol_usd": round(st.day_vol_usd) if st.day_vol_usd else None,
                    "candles": [c.as_dict() for c in perp_c[-120:]],
                    "fut_candles": [c.as_dict() for c in perp_c[-120:]],
                    "chart_candles": [c.as_dict() for c in st.perp.all_candles()[-self.chart_bars:]],
                    "candles5": ([c.as_dict() for c in st.chart5.all_candles()[-self.chart_bars:]]
                                 if st.chart5 is not None else []),
                    "daily_candles": list(st.daily),
                    "cvd_series": (st.footprint.delta_series()
                                   if st.footprint is not None else []),
                    "price_action": pa.to_dict(),
                    "volume_profile": vp.to_dict(),
                    "market_profile": mp.to_dict(),
                    "oi": oi.to_dict(),
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
                self._cache[key] = {"bar": _cur_bar, "snap": snap_instruments[key]}

            # Opportunities strip shows ONLY actionable A/A+ graded setups (not the
            # raw confluence signals). setup.actionable already requires grade >= the
            # configured min_grade (A), so what surfaces here is A or A+ only.
            _grade_rank = {"A+": 2, "A": 1, "B": 0, "C": -1}
            setup_ops = []
            for _k, _v in snap_instruments.items():
                _su = _v.get("setup")
                if not _su or not _su.get("actionable"):
                    continue
                _tk = _v.get("ticket") or {}
                setup_ops.append({
                    "instrument": _k,
                    "day_vol_usd": _v.get("day_vol_usd"),
                    "direction": _su.get("side"),
                    "grade": _su.get("grade"),
                    "score": _su.get("points"),
                    "title": (str(_su.get("grade")) + " setup \u00b7 " + str(_su.get("mode") or "")).strip(" \u00b7"),
                    "confluence": _su.get("reasons", []),
                    "rationale": " \u00b7 ".join(_su.get("reasons", [])),
                    "price": _su.get("entry"),
                    "plan": {
                        "instrument": _tk.get("vehicle") or (str(_v.get("coin", "")) + " perp"),
                        "note": _tk.get("reason"),
                        "entry": _su.get("entry"),
                        "stop_loss": _su.get("stop_loss"),
                        "target1": _su.get("target1"),
                        "target2": None,
                        "rr": _su.get("rr"),
                    },
                    "with_regime": True,
                    "footprint_confirm": True,
                    "divergence_confirm": _su.get("divergence"),
                    "ticket": _tk or None,
                })
            setup_ops.sort(key=lambda x: (-_grade_rank.get(x.get("grade"), -2), -(x.get("score") or 0)))
            return {
                "generated_at": _dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S") + " UTC",
                "mode": self.cfg.run_mode,
                "risk_profile": self.risk_profile,
                "config": {"profile": self.risk_profile, "min_grade": self.setup_min_grade,
                           "min_vol": float(self.cfg.a("paper", "min_day_vol_usd", default=0) or 0)},
                "candle_minutes": self.cfg.candle_minutes,
                "instruments": snap_instruments,
                "top_signals": setup_ops,
            }
