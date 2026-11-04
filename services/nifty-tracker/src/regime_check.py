"""Regime validation replay (2026-10-22). Chunked so each run fits in a few minutes.

Replays the tracker engine bar by bar over the 2-year spot history (same proxies as
setup_backtest: no volume / OI history), and records:
  bars.csv   : every bar - day, time, instrument, close, tracker regime, stance, bull, bear
  trades.jsonl: every graded setup the paper book would log, with its regime fields
Usage: python -m src.regime_check START END   (day indexes, END exclusive)
"""
from __future__ import annotations

import csv
import sys
from pathlib import Path

from .config import ROOT, load_config
from .state import MarketState
from .engine import Engine
from .paper_trade import PaperBook
from .instruments import resolve_all
from .analytics.footprint import FootprintBook
from .setup_backtest import _load_day, _daily_bar, _proxy_footprint_bar

DATA = ROOT / "datasets" / "2024-07-17_to_2026-07-19"
OUT = ROOT / "backtests" / "regime-check"


def main(a: int, b: int):
    cfg = load_config()
    cfg.raw.setdefault("risk", {})["profile"] = "aggressive"
    days = sorted(p for p in DATA.iterdir() if p.is_dir() and any(p.glob("*_spot.csv")))
    from .simbroker import SimBroker
    resolved = resolve_all(SimBroker(cfg), cfg)
    keys = list(resolved.keys())
    OUT.mkdir(parents=True, exist_ok=True)
    paper = PaperBook(cfg, path=OUT / f"trades_{a:04d}.jsonl", load=False)
    paper.min_score = 999
    paper.path.write_text("")
    # prior-day dailies for the chunk start
    daily_hist = {k: [] for k in keys}
    for d in days[max(0, a - 30):a]:
        for k in keys:
            db = _daily_bar(_load_day(d, k), d.name)
            if db:
                daily_hist[k].append(db)
    f = open(OUT / f"bars_{a:04d}.csv", "w", newline="")
    w = csv.writer(f)
    w.writerow(["day", "t", "key", "close", "regime", "stance", "bull", "bear", "confidence"])
    for d in days[a:b]:
        day_cands = {k: _load_day(d, k) for k in keys}
        state = MarketState(resolved, cfg.candle_minutes)
        engine = Engine(cfg, state)
        for k in keys:
            st = state.instruments[k]
            st.footprint = FootprintBook(st.ri.cfg.profile_bin, cfg.candle_minutes, max_bars=96)
            st.daily = list(daily_hist[k])
        n = max((len(v) for v in day_cands.values()), default=0)
        for i in range(n):
            mdt = None
            for k in keys:
                cs = day_cands[k]
                if i < len(cs):
                    c = cs[i]
                    st = state.instruments[k]
                    st.spot.completed.append(c)
                    st.last_spot = c.close
                    _proxy_footprint_bar(st.footprint, c, st.ri.cfg.tick_size)
                    mdt = c.start
            snap = engine.compute()
            for k, v in snap.get("instruments", {}).items():
                r = v.get("regime") or {}
                w.writerow([d.name, mdt.strftime("%H:%M") if mdt else "", k, v.get("spot"), r.get("regime"), r.get("stance"),
                            r.get("bull"), r.get("bear"), r.get("confidence")])
            paper.update(snap, market_dt=mdt)
        paper.force_flatten({k: state.instruments[k].last_spot for k in keys}, reason="eod")
        for k in keys:
            db = _daily_bar(day_cands[k], d.name)
            if db:
                daily_hist[k].append(db)
    f.close()
    print(f"days {a}-{b}: {len(paper.closed)} setups")


if __name__ == "__main__":
    main(int(sys.argv[1]), int(sys.argv[2]))
