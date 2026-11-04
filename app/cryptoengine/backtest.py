"""Batch backtester — run the full engine + paper logic over many days.

Feeds each day's dataset through MarketState bar-by-bar (decisions on 5-min close,
no dashboard, no sleep), letting the paper engine open/close qualified signals
exactly as it would live. Aggregates win-rate, avg-R, expectancy, gross R, max
drawdown, profit factor and an equity curve, then writes a JSON + HTML report.

    python tools/fetch_hl_history.py --days 30        # build real HL day-datasets
    python -m src.backtest --datasets datasets/hl-30d --label real-30d
    open backtests/real-30d/report.html

Coverage note: HL candle history gives price + real volume, so price action,
volume/market profile, imbalance/CVD and structure all backtest faithfully. Live
funding & OI aren't in candle history (they're live-captured), so funding/liq
factors are validated forward by the live paper engine, not in the historical run
— the same honest split as the NSE tracker's live-only option OI.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from .config import ROOT, load_config
from .candles import floor_to_bucket
from .state import MarketState
from .engine import Engine
from .replay import ReplayFeed, build_replay_resolved
from .paper_trade import PaperBook


def _day_dirs(root: Path) -> list[Path]:
    return [p for p in sorted(root.iterdir())
            if p.is_dir() and any(p.glob("*_perp.csv"))]


def run_day(cfg, day_dir: Path, paper: PaperBook) -> dict:
    resolved = build_replay_resolved(cfg, day_dir)
    state = MarketState(resolved, cfg.candle_minutes)
    engine = Engine(cfg, state)
    feed = ReplayFeed(cfg, day_dir, resolved, state)     # builds events; not started
    events = feed.events
    if not events:
        return {"day": day_dir.name, "trades": 0}

    before = len(paper.closed)
    cm = cfg.candle_minutes
    last_bucket = None
    for ev in events:
        feed._dispatch(ev)
        bucket = floor_to_bucket(ev[2], cm)
        if last_bucket is None:
            last_bucket = bucket
        elif bucket > last_bucket:
            snap = engine.compute()
            paper.update(snap, market_dt=last_bucket)
            last_bucket = bucket
    snap = engine.compute()
    paper.update(snap, market_dt=last_bucket)
    prices = {k: v.get("spot") for k, v in snap.get("instruments", {}).items()}
    paper.force_flatten(prices, reason="eod")

    day_trades = paper.closed[before:]
    net_r = round(sum(t.get("r", 0) for t in day_trades), 2)
    wins = sum(1 for t in day_trades if t.get("result") == "win")
    return {"day": day_dir.name, "trades": len(day_trades), "wins": wins, "net_r": net_r}


def _drawdown(equity: list[float]) -> float:
    peak = mdd = 0.0
    for e in equity:
        peak = max(peak, e)
        mdd = min(mdd, e - peak)
    return round(mdd, 2)


def _profit_factor(trades):
    gp = sum(t["r"] for t in trades if t.get("r", 0) > 0)
    gl = -sum(t["r"] for t in trades if t.get("r", 0) < 0)
    return round(gp / gl, 2) if gl > 0 else None


def _by_dir(trades):
    out = {}
    for t in trades:
        b = out.setdefault(t["direction"], {"n": 0, "w": 0, "r": 0.0})
        b["n"] += 1
        b["w"] += 1 if t.get("result") == "win" else 0
        b["r"] = round(b["r"] + t.get("r", 0), 2)
    for b in out.values():
        b["win_rate"] = round(100 * b["w"] / b["n"], 1) if b["n"] else 0
    return out


def backtest(datasets_root: str, label: str = "backtest", min_score=None) -> dict:
    cfg = load_config()
    root = Path(datasets_root)
    if not root.is_absolute():
        root = ROOT / root
    days = _day_dirs(root)
    if not days:
        raise SystemExit(f"No day-datasets (with *_perp.csv) found under {root}")

    out_dir = ROOT / "backtests" / label
    out_dir.mkdir(parents=True, exist_ok=True)
    paper = PaperBook(cfg, path=out_dir / "trades.jsonl", load=False)
    paper.eod_flatten = True                 # bounded dataset: flat at each day end
    if min_score is not None:
        paper.min_score = int(min_score)
    if paper.path.exists():
        paper.path.unlink()

    per_day = []
    for d in days:
        per_day.append(run_day(cfg, d, paper))
        print(f"[bt] {per_day[-1]['day']}: {per_day[-1].get('trades',0)} trades, "
              f"net {per_day[-1].get('net_r',0)}R")

    trades = paper.closed
    st = paper.stats()
    equity, cum = [], 0.0
    for t in trades:
        cum += t.get("r", 0)
        equity.append(round(cum, 2))
    st["max_drawdown_r"] = _drawdown(equity)
    st["days"] = len(days)
    st["profit_factor"] = _profit_factor(trades)

    report = {"label": label, "datasets_root": str(root), "days": len(days),
              "stats": st, "equity": equity, "per_day": per_day,
              "by_direction": _by_dir(trades)}
    (out_dir / "report.json").write_text(json.dumps(report, indent=2))
    _write_html(out_dir / "report.html", report)
    print(f"\n[bt] {label}: {st['closed']} trades over {len(days)} days | "
          f"win {st['win_rate']}% | avg {st['avg_r']}R | expectancy {st['expectancy_r']}R | "
          f"gross {st['gross_r']}R | maxDD {st['max_drawdown_r']}R | PF {st['profit_factor']}")
    print(f"[bt] report -> {out_dir/'report.html'}")
    return report


def _write_html(path: Path, rep: dict):
    st = rep["stats"]
    eq = rep["equity"] or [0]
    n = len(eq)
    lo, hi = min(eq + [0]), max(eq + [0])
    span = (hi - lo) or 1
    W, H = 900, 260
    pts = " ".join(
        f"{8 + i * (W - 16) / max(1, n - 1):.1f},"
        f"{H - 12 - (e - lo) / span * (H - 24):.1f}" for i, e in enumerate(eq))
    zero_y = H - 12 - (0 - lo) / span * (H - 24)
    tiles = [("Trades", st["closed"]), ("Days", st.get("days")),
             ("Win rate", f"{st['win_rate']}%"), ("Avg R", st["avg_r"]),
             ("Expectancy R", st["expectancy_r"]), ("Gross R", st["gross_r"]),
             ("Max DD R", st.get("max_drawdown_r")), ("Profit factor", st.get("profit_factor"))]
    tile_html = "".join(f'<div class=t><div class=k>{k}</div><div class=v>{v}</div></div>'
                        for k, v in tiles)
    bd = rep.get("by_direction", {})
    bd_rows = "".join(
        f"<tr><td>{d}</td><td>{v['n']}</td><td>{v['win_rate']}%</td><td>{v['r']}</td></tr>"
        for d, v in bd.items())
    color = "#3fb950" if st.get("gross_r", 0) >= 0 else "#f85149"
    path.write_text(f"""<!DOCTYPE html><html><head><meta charset=utf-8>
<title>Backtest · {rep['label']}</title><style>
body{{margin:0;background:#0e1117;color:#e6edf3;font:14px -apple-system,Segoe UI,Roboto,sans-serif;padding:22px}}
h1{{font-size:18px;margin:0 0 4px}} .sub{{color:#8b98a9;font-size:12px;margin-bottom:16px}}
.tiles{{display:flex;flex-wrap:wrap;gap:10px;margin-bottom:18px}}
.t{{background:#161b22;border:1px solid #2a3140;border-radius:10px;padding:10px 14px;min-width:110px}}
.t .k{{font-size:10.5px;color:#8b98a9;text-transform:uppercase;letter-spacing:.5px}}
.t .v{{font-size:20px;font-weight:750;margin-top:3px}}
.card{{background:#161b22;border:1px solid #2a3140;border-radius:12px;padding:16px;margin-bottom:16px}}
h2{{font-size:12px;color:#8b98a9;text-transform:uppercase;letter-spacing:.6px;margin:0 0 10px}}
table{{border-collapse:collapse;font-size:13px}} td,th{{padding:3px 12px 3px 0;text-align:left}} th{{color:#8b98a9}}
.warn{{color:#d29922;font-size:11.5px;margin-top:8px}}
</style></head><body>
<h1>Crypto backtest — {rep['label']}</h1>
<div class=sub>{rep['days']} days · {rep['datasets_root']} · equity in R-multiples (1R = one unit of risk)</div>
<div class=tiles>{tile_html}</div>
<div class=card><h2>Equity curve (cumulative R)</h2>
<svg width={W} height={H} viewBox="0 0 {W} {H}">
<line x1=8 y1={zero_y:.1f} x2={W-8} y2={zero_y:.1f} stroke="#2a3140" stroke-dasharray="4 4"/>
<polyline fill=none stroke="{color}" stroke-width=2 points="{pts}"/>
</svg></div>
<div class=card><h2>By direction</h2>
<table><tr><th>Dir</th><th>Trades</th><th>Win rate</th><th>Net R</th></tr>{bd_rows}</table></div>
<div class=warn>Paper fills at signal price; outcomes in R on the underlying. Validates the directional
edge, not exact leveraged P&amp;L. Funding &amp; OI factors are live-only and not in this historical run.
Synthetic datasets demonstrate the machinery only — not a tradeable edge.</div>
</body></html>""")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--datasets", required=True)
    ap.add_argument("--label", default="backtest")
    ap.add_argument("--min-score", type=int, default=None)
    args = ap.parse_args()
    backtest(args.datasets, args.label, min_score=args.min_score)
