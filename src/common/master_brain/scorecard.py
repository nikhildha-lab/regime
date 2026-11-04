"""
Strategy x Regime scorecard.

For every strategy trade log we know about, tag each trade with the DESCRIPTIVE regime that
was known at its entry (point-in-time: last daily bar whose close is before the entry, and
last CLOSED 4H bar), then compute per-cell stats.

A cell is "trusted" only with >= MIN_TRADES trades (governance: no decisions on thin data).
PnL is taken as each strategy's own backtest logs it (net of costs where the log has it);
units differ between strategies, so compare cells WITHIN a strategy, not across strategies.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Callable, List, Optional

import numpy as np
import pandas as pd

from src.common.regime_engine import data as D
from src.common.regime_engine.engine import RegimeEngine, describe_state

MIN_TRADES = 30
IST = D.IST


@dataclass
class TradeLog:
    strategy: str
    instrument: str
    timeframe: str
    paths: List[str]
    loader: Callable[[pd.DataFrame], pd.DataFrame]   # -> columns entry_ts (naive IST), side (LONG/SHORT), pnl
    pnl_note: str


def _ts(date, time=None):
    return pd.to_datetime(date.astype(str) + (" " + time.astype(str) if time is not None else ""))


def _div(side):
    return lambda d: pd.DataFrame({"entry_ts": _ts(d["date"], d["entry_time"]), "side": side, "pnl": d["pnl"]})


def _ndtt(d):
    return pd.DataFrame({"entry_ts": _ts(d["entry_date"], d["entry_time"]), "side": d["position_type"].str.upper(), "pnl": d["pnl"]})


def _ndtt_opt(d):
    return pd.DataFrame({"entry_ts": pd.to_datetime(d["entry_time"]), "side": d["direction"].str.upper(), "pnl": d["net_pnl"]})


def _manish(d):
    return pd.DataFrame({"entry_ts": pd.to_datetime(d["entry_time"]),
                         "side": np.where(d["option_type"].str.upper() == "CE", "LONG", "SHORT"), "pnl": d["pnl"]})


def _eod(side_col):
    # EOD stock strategies: entry on entry_date; use 09:15 so the regime is the PREVIOUS day's close
    return lambda d: pd.DataFrame({"entry_ts": pd.to_datetime(d["entry_date"]) + pd.Timedelta(hours=9, minutes=15),
                                   "side": d[side_col].str.upper(), "pnl": d["pnl_net"]})


S = "src/strategies"
TRADE_LOGS = [
    TradeLog("bullish_divergence", "NIFTY ATM CE (buy)", "5m", [f"{S}/bullish_divergence/backtest/reports/trades/trades_20260826_180057.csv"], _div("LONG"), "pnl as logged"),
    TradeLog("bearish_divergence", "NIFTY ATM PE (buy)", "5m", [f"{S}/bearish_divergence/backtest/reports/trades/trades_20260826_175308.csv"], _div("SHORT"), "pnl as logged"),
    TradeLog("manish_options (OI momentum)", "NIFTY weekly options (buy)", "5m",
             [f"{S}/manish_options/core/Backtest/reports/trades_20260511_182502.csv", f"{S}/manish_options/core/Backtest/reports/trades_20260511_173258.csv"], _manish, "pnl as logged"),
    TradeLog("nd_tt_v4 futures, daily (ungated)", "NIFTY futures", "1D signal", ["backtest/nd_tt_v4_v1_regime/daily/nd_tt_v4_v1_regime_off_149trades.csv"], _ndtt, "net of commission"),
    TradeLog("nd_tt_v4 futures, 4H (ungated)", "NIFTY futures", "4H signal", ["backtest/nd_tt_v4_v1_regime/4h/nd_tt_v4_v1_regime_off_220trades.csv"], _ndtt, "net of commission"),
    TradeLog("nd_tt_v4_options_v1, daily (ungated)", "NIFTY 3-leg synthetic", "1D signal", [f"{S}/nd_tt_v4_options_v1/backtest/reports/trades/trades_20260930_154906_regime_off_day.csv"], _ndtt_opt, "net_pnl"),
    TradeLog("nd_tt_v4_options_v1, 4H (ungated)", "NIFTY 3-leg synthetic", "4H signal", [f"{S}/nd_tt_v4_options_v1/backtest/reports/trades/trades_20260930_154738_regime_off_240minute.csv"], _ndtt_opt, "net_pnl"),
    TradeLog("macd_only (stocks, EOD)", "NSE stocks", "1D", ["backtest/orb/macd_only/trades_orb_aligned.csv"], _eod("side"), "pnl_net"),
    TradeLog("macd_divergence (stocks, EOD)", "NSE stocks", "1D", ["backtest/orb/macd_divergence/trades_orb_aligned_eod.csv"], _eod("_side"), "pnl_net"),
]


def state_series(eng: RegimeEngine, profile: str) -> pd.DataFrame:
    fr = eng.feature_frame(profile)
    names = [describe_state(t, v if isinstance(v, str) else None) for t, v in zip(fr["group_TREND"], fr["vol_state"])]
    return pd.DataFrame({"available_at": pd.DatetimeIndex(fr["available_at"]).tz_convert(IST),
                         "trend_state": [n[0] for n in names], "regime_name": [n[1] for n in names],
                         "panel_trend": fr["panel_trend"].values}).sort_values("available_at")


def tag_trades(trades: pd.DataFrame, states: dict) -> pd.DataFrame:
    t = trades.copy()
    t["entry_ts"] = pd.to_datetime(t["entry_ts"]).dt.tz_localize(IST)
    t = t.sort_values("entry_ts").reset_index(drop=True)
    for prof, st in states.items():
        m = pd.merge_asof(t[["entry_ts"]], st, left_on="entry_ts", right_on="available_at",
                          direction="backward", allow_exact_matches=True, tolerance=pd.Timedelta(days=5))
        t[f"regime_{prof}"] = m["regime_name"].values
        t[f"trend_{prof}"] = m["trend_state"].values
        t[f"panel_{prof}"] = m["panel_trend"].values
    return t


def cell_stats(pnl: pd.Series) -> dict:
    pnl = pnl.astype(float)
    n = len(pnl)
    wins, losses = pnl[pnl > 0], pnl[pnl <= 0]
    sd = pnl.std(ddof=1) if n > 1 else np.nan
    return {
        "n": n,
        "win_rate_pct": round(100 * len(wins) / n, 1) if n else None,
        "total_pnl": round(pnl.sum(), 0),
        "avg_pnl": round(pnl.mean(), 1) if n else None,
        "profit_factor": round(wins.sum() / -losses.sum(), 2) if losses.sum() < 0 else None,
        "t_stat": round(pnl.mean() / (sd / np.sqrt(n)), 2) if n > 2 and sd > 0 else None,
        "trusted": n >= MIN_TRADES,
    }


def load_all(root: str) -> pd.DataFrame:
    frames = []
    for tl in TRADE_LOGS:
        parts = []
        for p in tl.paths:
            fp = os.path.join(root, p)
            if os.path.exists(fp):
                parts.append(tl.loader(pd.read_csv(fp)))
        if not parts:
            continue
        df = pd.concat(parts, ignore_index=True).dropna(subset=["entry_ts", "pnl"])
        df["strategy"], df["instrument"], df["timeframe"], df["pnl_note"] = tl.strategy, tl.instrument, tl.timeframe, tl.pnl_note
        frames.append(df)
    return pd.concat(frames, ignore_index=True)


def build_scorecard(root: str, eng: Optional[RegimeEngine] = None):
    eng = eng or RegimeEngine()
    states = {p: state_series(eng, p) for p in ("daily", "4h", "1h")}
    trades = load_all(root)
    tagged = pd.concat([tag_trades(g, states).assign(strategy=s) for s, g in trades.groupby("strategy")], ignore_index=True)
    rows = []
    for strat, g in tagged.groupby("strategy"):
        base = cell_stats(g["pnl"])
        rows.append({"strategy": strat, "dimension": "ALL", "cell": "ALL", **base})
        for dim in ("regime_daily", "trend_daily", "regime_4h", "trend_4h", "panel_daily", "panel_4h", "panel_1h"):
            for cell, gg in g.groupby(dim):
                rows.append({"strategy": strat, "dimension": dim, "cell": cell, **cell_stats(gg["pnl"])})
        # with-trend vs counter-trend (daily trend at entry)
        aligned = np.where(g["trend_daily"] == "Sideways", "Sideways",
                           np.where(((g["side"] == "LONG") & (g["trend_daily"] == "Uptrend")) |
                                    ((g["side"] == "SHORT") & (g["trend_daily"] == "Downtrend")), "With trend", "Against trend"))
        for cell, gg in g.groupby(aligned):
            rows.append({"strategy": strat, "dimension": "alignment_daily", "cell": cell, **cell_stats(gg["pnl"])})
        # trade direction vs the 1H indicator panel at entry (panel_1h exists from late 2023)
        pal = np.where(g["panel_1h"].isna(), None, np.where(g["panel_1h"] == "Flat", "Panel flat",
                       np.where(((g["side"] == "LONG") & (g["panel_1h"] == "Up")) | ((g["side"] == "SHORT") & (g["panel_1h"] == "Down")),
                                "With 1H panel", "Against 1H panel")))
        for cell, gg in g.groupby(pd.Series(pal, index=g.index).dropna()):
            rows.append({"strategy": strat, "dimension": "alignment_panel_1h", "cell": cell, **cell_stats(gg["pnl"])})
    card = pd.DataFrame(rows)
    meta = trades.groupby("strategy").agg(instrument=("instrument", "first"), timeframe=("timeframe", "first"),
                                          first=("entry_ts", "min"), last=("entry_ts", "max"), pnl_note=("pnl_note", "first"))
    return card, tagged, meta


# ---------------------------------------------------------------------------
# Option-selling strategies from the MASTER_BACKTESTER session (ungated raw runs, real NSE chains)
# ---------------------------------------------------------------------------
OPTION_LOG_DIR = "data_store/master_backtester/options_trades"


def build_option_scorecard(root: str, eng: Optional[RegimeEngine] = None):
    """Tag every ungated option trade with our regime + India VIX bucket (previous close) at entry (3:15 PM)."""
    import glob
    eng = eng or RegimeEngine()
    states = {p: state_series(eng, p) for p in ("daily", "4h", "1h")}
    vix = D.load_yahoo_daily("^INDIAVIX")["close"] if D.has_market_data() else None
    tagged = []
    for f in sorted(glob.glob(os.path.join(root, OPTION_LOG_DIR, "trades_*.csv"))):
        sym, strat = os.path.basename(f)[7:-4].split("_", 1)
        if strat.endswith(("_h3", "_h5", "_m2")):          # sensitivity variants
            continue
        raw = pd.read_csv(f)
        t = pd.DataFrame({"entry_ts": pd.to_datetime(raw["entry"]) + pd.Timedelta(hours=15, minutes=15),
                          "side": "NEUTRAL", "pnl": raw["pnl"]})
        tg = tag_trades(t, states)
        tg["strategy"] = f"{strat} {sym}"
        if vix is not None:
            d = pd.DataFrame({"d": tg["entry_ts"].dt.tz_localize(None).dt.normalize()})
            v = pd.merge_asof(d, vix.rename("v").rename_axis("vd").reset_index(), left_on="d", right_on="vd",
                              allow_exact_matches=False)["v"].values
            tg["vix_prev_close"] = v
            tg["vix_bucket"] = pd.cut(v, [0, 12, 14.5, 100], labels=["VIX<12", "12-14.5", "VIX>=14.5"], right=False).astype(str)
        tg["range_regime"] = np.where(tg["regime_daily"].isin(["Calm range", "Choppy range"]), "Sideways (calm/choppy)", "Trending (up/down)")
        tagged.append(tg)
    T = pd.concat(tagged, ignore_index=True)
    rows = []
    for s, g in T.groupby("strategy"):
        rows.append({"strategy": s, "dimension": "ALL", "cell": "ALL", **cell_stats(g["pnl"])})
        for dim in ("range_regime", "regime_daily", "vix_bucket", "panel_daily", "panel_1h"):
            if dim in g:
                for c, gg in g.groupby(dim):
                    rows.append({"strategy": s, "dimension": dim, "cell": c, **cell_stats(gg["pnl"])})
    return pd.DataFrame(rows), T
