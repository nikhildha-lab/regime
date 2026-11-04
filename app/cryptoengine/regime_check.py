"""Regime validation replay for crypto (2026-10-22): 1 year of real Hyperliquid 15-min bars (with volume).
Records every bar's tracker regime (bars.csv) and every graded setup the paper book would log (trades.jsonl).
Usage: python -m src.regime_check [dataset]"""
from __future__ import annotations
import csv, sys, time
from pathlib import Path
from .config import ROOT, load_config
from .candles import floor_to_bucket
from .state import MarketState
from .engine import Engine
from .replay import ReplayFeed, build_replay_resolved
from .paper_trade import PaperBook


def main(part="0", ds="datasets/hl-1yr-tf15"):
    part = int(part)
    cfg = load_config()
    cfg.raw.setdefault("risk", {})["profile"] = "aggressive"
    cfg.instruments = [i for i in cfg.instruments if i.key in ("BTC", "ETH", "SOL")]
    out = ROOT / "backtests" / "regime-check"; out.mkdir(parents=True, exist_ok=True)
    paper = PaperBook(cfg, path=out / f"trades_{part}.jsonl", load=False)
    paper.path.write_text(""); paper.min_score = 999; paper.min_vol = 0
    f = open(out / f"bars_{part}.csv", "w", newline=""); w = csv.writer(f)
    w.writerow(["ts", "key", "close", "regime", "stance", "bull", "bear", "confidence"])
    t0 = time.time()
    for day_dir in sorted(p for p in (ROOT / ds).iterdir() if p.is_dir()):
        resolved = build_replay_resolved(cfg, day_dir)
        state = MarketState(resolved, cfg.candle_minutes)
        engine = Engine(cfg, state)
        feed = ReplayFeed(cfg, day_dir, resolved, state)
        cm, last = cfg.candle_minutes, None
        evs = feed.events
        mid = evs[len(evs) // 2][2]
        for ev in evs:
            if part == 0 and ev[2] >= mid:
                break
            feed._dispatch(ev)
            b = floor_to_bucket(ev[2], cm)
            if last is None:
                last = b
            elif b > last and part == 1 and last < mid:
                last = b
            elif b > last:
                snap = engine.compute(focus=list(resolved.keys())) if "focus" in engine.compute.__code__.co_varnames else engine.compute()
                for k, v in snap.get("instruments", {}).items():
                    r = v.get("regime") or {}
                    w.writerow([last.isoformat(), k, v.get("spot"), r.get("regime"), r.get("stance"), r.get("bull"), r.get("bear"), r.get("confidence")])
                paper.update(snap, market_dt=last)
                last = b
    f.close()
    print(f"{len(paper.closed)} setups, {round(time.time() - t0)} s")


if __name__ == "__main__":
    main(*sys.argv[1:])
