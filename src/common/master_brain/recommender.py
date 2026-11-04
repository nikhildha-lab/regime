"""
Recommender v1 (rule set "v2-strict"): Run / Pause / Paper-only per strategy, for a given moment.

Inputs : the regime engine's MTF call at `as_of` + the Strategy x Regime scorecard.
Rules (deterministic, fixed BEFORE the walk-forward test; the Desk Narrator may only EXPLAIN them):
  1. Strategy level (n >= 30): average P&L <= 0 -> no live running: Pause if t <= -1, else Paper only.
     (Tightened after the first walk-forward: "never run a strategy with negative expectancy".)
  2. Regime level: in the most specific trusted cell (daily regime_name, else daily trend_state, n >= 30)
       t <= -2  -> Pause in this regime;   t >= +2 -> Run (strong regime evidence)
       otherwise follow the strategy-level stance (Run if not paused in step 1).
  3. Size stays 1.0: sizing by regime confidence is NOT validated yet, so it is off.
  4. Short-volatility strategies are paused on expiry days (event rule).
  5. No backtest log -> "Paper only".
  6. STATUS: ADVISORY. Walk-forward (scripts/master_brain_walkforward.py, 2023-11..2026-03) showed regime
     switching did not beat running every strategy (v1 rules: -Rs6.4L, v2-strict: -Rs2.4L vs run-all).
     Gate 3 of the plan is NOT passed - recommendations are for paper trading and review only.
  Nothing here places an order. Moving any strategy to live money needs a human click.
"""
from __future__ import annotations

from typing import Optional

import pandas as pd

from .scorecard import MIN_TRADES
from . import option_rules as OR

SHORT_VOL = {"nifty_straddle_925", "iron_butterfly_opt", "weirdor_opt"}

# India strategies in src/strategies with no usable trade log in the repo yet (strategy catalog, 2026-10-04)
NO_EVIDENCE_INDIA = [
    "nifty_straddle_925 (Backtrader_App)", "iron_butterfly_opt (Backtrader_App)", "weirdor_opt", "bull_call_spread", "bull_put_spread",
    "bear_put_spread", "bear_call_spread", "niftybees", "macd_intraday_v2", "macd_future",
    "rabbit_intraday", "rabbit_20sma_swing", "rabbit_5sma_reversal", "rabbit_liquidity_spike_v2_3", "apex_flux",
]


def _cell(card: pd.DataFrame, strat: str, dim: str, cell: Optional[str]):
    if cell is None:
        return None
    r = card[(card.strategy == strat) & (card.dimension == dim) & (card.cell == cell)]
    return None if r.empty else r.iloc[0]


def _t(row):
    return row["t_stat"] if pd.notna(row["t_stat"]) else 0.0


def recommend(call: dict, card: pd.DataFrame) -> dict:
    comb = call["combined"]
    regime, trend, conf = comb["regime_name"], comb["trend_state"], comb["state_confidence"]
    cal = call["calendar"]
    recs = []
    for strat in sorted(card.strategy.unique()):
        base = _cell(card, strat, "ALL", "ALL")
        if base is None or base["n"] < MIN_TRADES:
            recs.append({"strategy": strat, "action": "Paper only", "size": 0.0, "evidence_level": None,
                         "reason": "fewer than 30 backtest trades"})
            continue
        stance_ok = base["avg_pnl"] > 0
        action = "Run" if stance_ok else ("Pause" if _t(base) <= -1.0 else "Paper only")
        level, used = "ALL", base
        for dim, cell in (("regime_daily", regime), ("trend_daily", trend)):
            r = _cell(card, strat, dim, cell)
            if r is not None and r["n"] >= MIN_TRADES:
                level, used = dim, r
                if _t(r) <= -2.0:
                    action = "Pause"
                elif _t(r) >= 2.0:
                    action = "Run"
                break
        if strat in SHORT_VOL and (cal["is_weekly_expiry"] or cal["is_monthly_expiry"]):
            action = "Pause"
        where = {"regime_daily": f"the '{regime}' regime", "trend_daily": f"daily {trend}", "ALL": "all regimes"}[level]
        why = (f"{used['n']} past trades in {where}: avg P&L {used['avg_pnl']:+,.0f}, "
               f"PF {used['profit_factor'] if pd.notna(used['profit_factor']) else 0:.2f}, t {_t(used):+.1f}")
        if not stance_ok and action != "Run":
            why += f" | overall record negative ({base['n']} trades, avg {base['avg_pnl']:+,.0f})"
        recs.append({"strategy": strat, "action": action, "size": 1.0 if action == "Run" else 0.0,
                     "evidence_level": level,
                     "evidence": {k: (None if pd.isna(used[k]) else used[k]) for k in ("n", "win_rate_pct", "avg_pnl", "profit_factor", "t_stat")},
                     "reason": why})
    for strat in NO_EVIDENCE_INDIA:
        recs.append({"strategy": strat, "action": "Paper only", "size": 0.0, "evidence_level": None,
                     "reason": "no backtest trade log in the repo yet; backtest it to enter the scorecard"})
    vix = (call.get("india_vix") or {}).get("value")
    day = pd.Timestamp(call["as_of"]).tz_localize(None) if pd.Timestamp(call["as_of"]).tzinfo else pd.Timestamp(call["as_of"])
    options = [OR.evaluate(n, vix, cal, comb, day) for n in list(OR.OPTION_STRATEGIES) + OR.REJECTED]
    return {"as_of": call["as_of"], "regime": comb, "calendar": cal, "india_vix": call.get("india_vix"),
            "option_strategies": options,
            "weight_set_version": call["weight_set_version"], "rule_set": "v2-strict",
            "status": "ADVISORY - walk-forward did not beat run-all; paper trading and review only",
            "recommendations": recs,
            "note": "Recommendations only. No orders are placed; live money needs human approval."}
