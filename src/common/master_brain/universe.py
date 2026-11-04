"""
The full strategy universe in one place:

  1. MASTER_BACKTESTER legs: every trade log in its results/ folder (optimized/improved variant per market)
     + its leg registry (66 validated legs: 'core' / 'bench').
  2. Backtrader_App NIFTY strategies with trade logs (scorecard.TRADE_LOGS) and those without (paper only).
  3. Option sellers (option_rules.py) - handled with BACKTESTER's VIX/event rules.
  4. Old Synaptic engine crypto bots (Axiom, Pyxis, Ratio, Sideways) - verdict from the Oct-2026 4-coin backtest.

Every leg is judged against the regime of ITS OWN market (markets.py), with the same "v2-strict" rule
as the NIFTY recommender: only a trusted cell (>= 30 trades) with |t| >= 2 can override the leg's stance.

Caveat: BACKTESTER's 'optimized' trade logs are in-sample after parameter optimization; its registry
holds the walk-forward / multi-year verdicts, which is why registry status leads and regime only vetoes.
"""
from __future__ import annotations

import glob
import os

import numpy as np
import pandas as pd

from . import markets as M
from .scorecard import cell_stats, MIN_TRADES

BUNDLE = "data_store/master_backtester/bundle"
OPTION_STRATS = {"condor_clock", "fly_rescue", "straddle_recenter", "volcano_calendar", "delta_hedged",
                 "ratio_flip", "owl_bundle", "odte_ladder", "regime_spreads", "strangle_shield", "split_condor",
                 "reverse_calendar_m1", "yearly_covered"}

# registry 'market' text -> market whose regime governs the leg
REGISTRY_MARKET = [
    ("NSE stocks", "NIFTY"), ("NIFTY", "NIFTY"), ("BANKNIFTY", "BANKNIFTY"),
    ("ETH (pair BTC)", "ETH"), ("BTC (pair ETH)", "BTC"), ("BTC+ETH", "BTC"), ("BTC", "BTC"), ("ETH", "ETH"), ("SOL", "SOL"),
    ("SI+GC", "GOLD"), ("GC micro", "GOLD"), ("GOLD", "GOLD"), ("GLD/SLV", "GOLD"), ("SILVER", "SILVER"),
    ("COPPER", "COPPER"), ("CL micro", "CRUDE"), ("ES/NQ", "ES"), ("ES micro", "ES"), ("NQ micro", "NQ"),
    ("US stocks", "US_STOCKS"), ("SPDR", "US_STOCKS"), ("ENSEMBLE", "US_STOCKS"), ("COMBINED25", "US_STOCKS"),
]

SYNAPTIC_BOTS = [
    ("Axiom (MACD/RSI/ADX momentum, 15m)", "BTC", "Do not run", "Oct-2026 backtest on AAVE/BNB/COMP/SNX: -Rs4,118 on 16,608 trades, PF 0.85"),
    ("Pyxis (SMA 20/50 cross, 1h)", "BTC", "Do not run", "Oct-2026 backtest: -Rs186 on 514 trades, PF 0.76"),
    ("Ratio (stat-arb ranking, 1d)", "BTC", "Research only", "Oct-2026 backtest: +Rs22 on 65 trades, PF 1.29 - too few trades"),
    ("Sideways (BB + RSI mean reversion)", "BTC", "Research only", "Needs the HMM brain; not backtested standalone"),
]


def registry_market(text: str):
    for key, m in REGISTRY_MARKET:
        if key in str(text):
            return m
    return None


def load_registry(root: str) -> pd.DataFrame:
    p = os.path.join(root, BUNDLE, "engine/leg_registry.csv")
    r = pd.read_csv(p)
    r["regime_market"] = r["market"].map(registry_market)
    r.loc[r["market_class"] == "Options", "regime_market"] = r.loc[r["market_class"] == "Options", "market"].map(
        lambda x: "BANKNIFTY" if "BANKNIFTY" in x else "NIFTY")
    return r


def _pick_file(files):
    for tag in ("_optimized", "_improved", ""):
        for f in files:
            if f.endswith(f"{tag}.csv") and ("_baseline" not in f):
                return f
    return None


def load_backtester_trades(root: str) -> pd.DataFrame:
    rows = []
    for d in sorted(glob.glob(os.path.join(root, BUNDLE, "results/*"))):
        strat = os.path.basename(d)
        if strat in OPTION_STRATS:
            continue
        by_mkt = {}
        for f in glob.glob(os.path.join(d, "trades_*.csv")):
            mkt = os.path.basename(f)[7:-4].split("_")[0]
            by_mkt.setdefault(mkt, []).append(f)
        for mkt, files in by_mkt.items():
            f = _pick_file(files)
            if f is None or mkt not in M.MARKETS:
                continue
            t = pd.read_csv(f)
            if "net_pnl" not in t:
                continue
            et = t["entry_time"] if "entry_time" in t and t["entry_time"].astype(str).str.len().min() >= 10 else t.get("entry_date")
            df = pd.DataFrame({"entry_ts": pd.to_datetime(et.astype(str).str[:19], errors="coerce"),
                               "side": t.get("side", t.get("direction", "")).astype(str).str.upper(),
                               "pnl": t["net_pnl"].astype(float)})
            df["strategy"], df["market"], df["variant"] = strat, mkt, os.path.basename(f)[7:-4]
            rows.append(df.dropna(subset=["entry_ts"]))
    return pd.concat(rows, ignore_index=True)


def build_universe_scorecard(root: str):
    trades = load_backtester_trades(root)
    parts = []
    for mkt, g in trades.groupby("market"):
        tag = M.tag_entries(mkt, g["entry_ts"].reset_index(drop=True))
        g = g.reset_index(drop=True)
        parts.append(pd.concat([g, tag], axis=1))
    T = pd.concat(parts, ignore_index=True)
    rows = []
    for (s, m), g in T.groupby(["strategy", "market"]):
        rows.append({"strategy": s, "market": m, "dimension": "ALL", "cell": "ALL", **cell_stats(g["pnl"])})
        for dim in ("regime_name", "trend_state", "panel_trend"):
            for c, gg in g.groupby(dim):
                rows.append({"strategy": s, "market": m, "dimension": dim, "cell": c, **cell_stats(gg["pnl"])})
    return pd.DataFrame(rows), T


def _cell(card, s, m, dim, cell):
    r = card[(card.strategy == s) & (card.market == m) & (card.dimension == dim) & (card.cell == cell)]
    return None if r.empty else r.iloc[0]


EXACT_LOG_MARKETS = {"NIFTY", "BANKNIFTY", "BTC", "ETH", "SOL", "GOLD"}   # markets with BACKTESTER results/ trade logs


def judge_leg(card, strat, mkt, regime: dict, registry_status):
    """v2-strict decision for one strategy x market leg."""
    base = _cell(card, strat, mkt, "ALL", "ALL")
    if registry_status == "core":
        stance = "Run (core)"
    elif registry_status == "bench":
        stance = "Paper (bench)"
    else:
        stance = "Research only"
    why = [f"BACKTESTER registry: {registry_status or 'not in registry'}"]
    ev = None
    if base is not None and base["n"] >= MIN_TRADES:
        why.append(f"{int(base['n'])} trades, PF {base['profit_factor']}")
        if base["avg_pnl"] <= 0 and stance != "Research only":
            stance = "Paper only (negative record)"
        for dim, key in (("regime_name", "regime_name"), ("trend_state", "trend_state")):
            r = _cell(card, strat, mkt, dim, regime.get(key))
            if r is not None and r["n"] >= MIN_TRADES:
                ev = r
                t = r["t_stat"] if pd.notna(r["t_stat"]) else 0
                why.append(f"in '{regime.get(key)}': {int(r['n'])} trades, PF {r['profit_factor']}, t {t:+.1f}")
                if t <= -2:
                    stance = "Pause (regime veto)"
                break
    elif base is not None:
        why.append(f"only {int(base['n'])} trades in log")
    return stance, "; ".join(why), ev


def recommend_universe(root: str, as_of, card=None):
    card = card if card is not None else build_universe_scorecard(root)[0]
    reg = load_registry(root)
    regimes = {m: M.regime_now(m, as_of) for m in M.MARKETS}
    out, seen = [], set()
    for _, r in reg.iterrows():
        if r["market_class"] == "Options":
            continue                                     # option legs: option_rules.py
        mkt = r["regime_market"]
        key = (r["strategy"], mkt)
        seen.add(key)
        rg = regimes.get(mkt, {"regime_name": None})
        exact = str(r["market"]).split(" ")[0] in EXACT_LOG_MARKETS and str(r["market"]).split(" ")[0] == mkt
        if mkt and exact:
            stance, why, _ = judge_leg(card, r["strategy"], mkt, rg, r["status"])
        else:   # leg ran on data we hold no trade log for (US futures, US stocks, micro contracts...)
            stance = "Run (core)" if r["status"] == "core" else "Paper (bench)"
            why = (f"BACKTESTER registry: {r['status']}; no trade log here, so the regime is context only"
                   + ("" if mkt else "; no market mapped"))
        out.append({"source": "MASTER_BACKTESTER registry", "strategy": r["strategy"], "leg": f"{r['market']} {r['tf']}",
                    "market": mkt, "market_class": r["market_class"], "registry": f"{r['status']} / {r['tier']} / PF {r['pf']} / n {r['n']}",
                    "regime": rg.get("regime_name"), "action": stance, "reason": why})
    for (s, m), _ in card[card.dimension == "ALL"].groupby(["strategy", "market"]):
        if (s, m) in seen:
            continue
        rg = regimes.get(m, {})
        stance, why, _ = judge_leg(card, s, m, rg, None)
        out.append({"source": "MASTER_BACKTESTER results (not in registry)", "strategy": s, "leg": f"{m} (results log)",
                    "market": m, "market_class": "", "registry": "not in registry", "regime": rg.get("regime_name"),
                    "action": stance, "reason": why})
    for name, mkt, act, note in SYNAPTIC_BOTS:
        out.append({"source": "Synaptic engine (old crypto bots)", "strategy": name, "leg": "crypto alts", "market": mkt,
                    "market_class": "Crypto", "registry": "-", "regime": regimes[mkt].get("regime_name"), "action": act, "reason": note})
    return pd.DataFrame(out), regimes
