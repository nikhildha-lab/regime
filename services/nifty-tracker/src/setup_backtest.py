"""Proxy backtest of the graded reversal/setup framework over spot history.

HONEST SCOPE — read before trusting numbers
--------------------------------------------
The 2-year history is SPOT 5-min OHLC only (no volume, no futures, no option OI).
So this backtest uses PROXIES, not the live tick footprint:

  * delta / footprint : close-position-in-range, with each bar's RANGE standing in
                        for volume (the same proxy that rebuilds your morning history).
  * levels            : static Tier-1 (PDH/PDL/PWH/PWL, IB) + TPO POC / swings.
                        No option walls (no OI history); VWAP is volumeless.
  * outcomes          : underlying R-multiples (1R = entry→stop distance).

It validates the LOGIC — does reversal+level+divergence have an edge, and does the
GRADE rank outcomes (A+ > A > B > C) — NOT the true order-flow magnitude edge,
which is genuinely live-only. Treat a positive result as necessary-but-not-
sufficient, and a negative result as a real red flag.

    python -m src.setup_backtest --datasets datasets/2024-07-17_to_2026-07-19 --label setup-2yr
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
from pathlib import Path

from .config import ROOT, load_config
from .state import MarketState
from .engine import Engine
from .paper_trade import PaperBook
from .instruments import resolve_all
from .candles import Candle
from .analytics.footprint import FootprintBook
from .seed import _approx_bins


def _load_day(day_dir: Path, key: str):
    p = day_dir / f"{key}_spot.csv"
    if not p.exists():
        return []
    out = []
    with p.open() as f:
        for row in csv.DictReader(f):
            try:
                start = dt.datetime.strptime(row["datetime"], "%Y-%m-%d %H:%M")
                out.append(Candle(start, float(row["open"]), float(row["high"]),
                                  float(row["low"]), float(row["close"]), 0.0, 0.0))
            except Exception:
                continue
    return out


def _daily_bar(cands, day: str):
    if not cands:
        return None
    return {"iso": day, "d": day[5:], "o": cands[0].open,
            "h": max(c.high for c in cands), "l": min(c.low for c in cands),
            "c": cands[-1].close}


def _proxy_footprint_bar(book: FootprintBook, c: Candle, tick):
    pv = max(c.high - c.low, tick)                 # range as pseudo-volume
    frac = ((c.close - c.low) / (c.high - c.low)) if c.high > c.low else 0.5
    buy, sell = pv * frac, pv * (1 - frac)
    bins = _approx_bins(c.low, c.high, book.bw, buy, sell)
    book.seed_bar(c.start, c.open, c.high, c.low, c.close, buy, sell, bins, approx=True)


def run(datasets_root: str, label: str, profile: str = "aggressive"):
    cfg = load_config()
    cfg.raw.setdefault("risk", {})["profile"] = profile   # C+ so every grade logs
    root = Path(datasets_root)
    if not root.is_absolute():
        root = ROOT / root
    days = sorted(p for p in root.iterdir() if p.is_dir() and any(p.glob("*_spot.csv")))
    if not days:
        raise SystemExit(f"no day-datasets under {root}")

    # build resolved instruments once (sim broker just to name tokens/steps)
    from .simbroker import SimBroker
    resolved = resolve_all(SimBroker(cfg), cfg)
    keys = list(resolved.keys())

    out_dir = ROOT / "backtests" / label
    out_dir.mkdir(parents=True, exist_ok=True)
    paper = PaperBook(cfg, path=out_dir / "trades.jsonl", load=False)
    paper.min_score = 999            # suppress the signal stream; setups only
    if paper.path.exists():
        paper.path.unlink()

    daily_hist = {k: [] for k in keys}
    per_day = []
    for di, day_dir in enumerate(days):
        day = day_dir.name
        day_cands = {k: _load_day(day_dir, k) for k in keys}
        # fresh state each day; carry the accumulated prior-day dailies
        state = MarketState(resolved, cfg.candle_minutes)
        engine = Engine(cfg, state)
        for k in keys:
            st = state.instruments[k]
            st.footprint = FootprintBook(st.ri.cfg.profile_bin, cfg.candle_minutes,
                                         max_bars=96)
            st.daily = list(daily_hist[k])

        before = len(paper.closed)
        nbars = max((len(v) for v in day_cands.values()), default=0)
        for i in range(nbars):
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
            paper.update(snap, market_dt=mdt)
        prices = {k: state.instruments[k].last_spot for k in keys}
        paper.force_flatten(prices, reason="eod")

        # append this day's daily bar for future days' prior-levels
        for k in keys:
            db = _daily_bar(day_cands[k], day)
            if db:
                daily_hist[k].append(db)

        dtr = paper.closed[before:]
        per_day.append({"day": day, "trades": len(dtr),
                        "net_r": round(sum(t.get("r", 0) for t in dtr), 2)})
        if di % 25 == 0 or di == len(days) - 1:
            print(f"[bt] {day} ({di+1}/{len(days)}): total {len(paper.closed)} setups")

    _report(out_dir, label, str(root), paper, per_day, profile)


def _agg(trades):
    n = len(trades)
    if not n:
        return {"n": 0, "win_rate": 0, "net_r": 0, "avg_r": 0, "expectancy_r": 0}
    wins = [t for t in trades if t.get("result") == "win"]
    rs = [t.get("r", 0) for t in trades]
    return {"n": n, "win_rate": round(100 * len(wins) / n, 1),
            "net_r": round(sum(rs), 2), "avg_r": round(sum(rs) / n, 3),
            "expectancy_r": round(sum(rs) / n, 3)}


def _report(out_dir, label, root, paper, per_day, profile):
    trades = [t for t in paper.closed if t.get("source") == "setup"]
    by_grade = {g: _agg([t for t in trades if t.get("grade") == g])
                for g in ["A+", "A", "B", "C"]}
    by_mode = {m: _agg([t for t in trades if t.get("mode") == m])
               for m in ["continuation", "fade", "range"]}
    by_dir = {d: _agg([t for t in trades if t.get("direction") == d])
              for d in ["long", "short"]}
    by_inst = {}
    for k in set(t["instrument"] for t in trades):
        by_inst[k] = _agg([t for t in trades if t["instrument"] == k])

    # month / quarter grouping by entry date
    def _period(t, q=False):
        s = (t.get("entry_dt") or "")[:7]
        if not q or len(s) < 7:
            return s
        y, m = s.split("-")
        return f"{y}-Q{(int(m)-1)//3 + 1}"
    months = sorted(set(_period(t) for t in trades if t.get("entry_dt")))
    quarters = sorted(set(_period(t, True) for t in trades if t.get("entry_dt")))
    by_month = {mm: _agg([t for t in trades if _period(t) == mm]) for mm in months}
    by_quarter = {qq: _agg([t for t in trades if _period(t, True) == qq]) for qq in quarters}

    overall = _agg(trades)
    equity, cum = [], 0.0
    for t in trades:
        cum += t.get("r", 0)
        equity.append(round(cum, 2))
    peak = mdd = 0.0
    for e in equity:
        peak = max(peak, e)
        mdd = min(mdd, e - peak)
    overall["max_drawdown_r"] = round(mdd, 2)
    gp = sum(t["r"] for t in trades if t.get("r", 0) > 0)
    gl = -sum(t["r"] for t in trades if t.get("r", 0) < 0)
    overall["profit_factor"] = round(gp / gl, 2) if gl > 0 else None

    rep = {"label": label, "root": root, "profile": profile, "days": len(per_day),
           "overall": overall, "by_grade": by_grade, "by_mode": by_mode,
           "by_direction": by_dir, "by_instrument": by_inst,
           "by_month": by_month, "by_quarter": by_quarter, "equity": equity}
    (out_dir / "report.json").write_text(json.dumps(rep, indent=2))
    _html(out_dir / "report.html", rep)
    print(f"\n[bt] {label}: {overall['n']} setups over {len(per_day)} days | "
          f"win {overall['win_rate']}% | avg {overall['avg_r']}R | "
          f"net {overall['net_r']}R | maxDD {overall['max_drawdown_r']}R | "
          f"PF {overall['profit_factor']}")
    print("[bt] by grade:")
    for g, a in by_grade.items():
        print(f"     {g:2s}  n={a['n']:4d}  win={a['win_rate']:5}%  "
              f"avgR={a['avg_r']:+.3f}  netR={a['net_r']:+.1f}")
    print(f"[bt] report -> {out_dir/'report.html'}")


def _html(path, rep):
    def tbl(title, d, cols=("n", "win_rate", "avg_r", "net_r")):
        head = "".join(f"<th>{c}</th>" for c in ("key",) + cols)
        rows = ""
        for k, a in d.items():
            rows += "<tr><td>" + str(k) + "</td>" + "".join(
                f"<td>{a.get(c)}</td>" for c in cols) + "</tr>"
        return f"<div class=card><h2>{title}</h2><table><tr>{head}</tr>{rows}</table></div>"
    eq = rep["equity"] or [0]
    n = len(eq)
    lo, hi = min(eq + [0]), max(eq + [0])
    span = (hi - lo) or 1
    W, H = 900, 240
    pts = " ".join(f"{8 + i*(W-16)/max(1, n-1):.1f},{H-12-(e-lo)/span*(H-24):.1f}"
                   for i, e in enumerate(eq))
    zy = H - 12 - (0 - lo) / span * (H - 24)
    ov = rep["overall"]
    tiles = [("Setups", ov["n"]), ("Days", rep["days"]), ("Win rate", f"{ov['win_rate']}%"),
             ("Avg R", ov["avg_r"]), ("Net R", ov["net_r"]),
             ("Max DD R", ov.get("max_drawdown_r")), ("Profit factor", ov.get("profit_factor")),
             ("Profile", rep["profile"])]
    thtml = "".join(f'<div class=t><div class=k>{k}</div><div class=v>{v}</div></div>'
                    for k, v in tiles)
    col = "#3fb950" if ov["net_r"] >= 0 else "#f85149"
    path.write_text(f"""<!DOCTYPE html><html><head><meta charset=utf-8><title>Setup backtest · {rep['label']}</title>
<style>body{{margin:0;background:#0e1117;color:#e6edf3;font:14px -apple-system,Segoe UI,Roboto,sans-serif;padding:22px}}
h1{{font-size:18px;margin:0 0 4px}} .sub{{color:#8b98a9;font-size:12px;margin-bottom:16px}}
.tiles{{display:flex;flex-wrap:wrap;gap:10px;margin-bottom:18px}}
.t{{background:#161b22;border:1px solid #2a3140;border-radius:10px;padding:10px 14px;min-width:104px}}
.t .k{{font-size:10px;color:#8b98a9;text-transform:uppercase;letter-spacing:.5px}} .t .v{{font-size:20px;font-weight:750;margin-top:3px}}
.card{{background:#161b22;border:1px solid #2a3140;border-radius:12px;padding:16px;margin-bottom:16px}}
h2{{font-size:12px;color:#8b98a9;text-transform:uppercase;letter-spacing:.6px;margin:0 0 10px}}
table{{border-collapse:collapse;font-size:12.5px}} td,th{{padding:3px 14px 3px 0;text-align:left}} th{{color:#8b98a9}}
.warn{{color:#d29922;font-size:11.5px;margin-top:8px;line-height:1.5}}</style></head><body>
<h1>Setup backtest — {rep['label']}</h1>
<div class=sub>{rep['days']} days · {rep['root']} · profile {rep['profile']} · underlying R-multiples</div>
<div class=tiles>{thtml}</div>
<div class=card><h2>Equity (cumulative R)</h2><svg width={W} height={H} viewBox="0 0 {W} {H}">
<line x1=8 y1={zy:.1f} x2={W-8} y2={zy:.1f} stroke="#2a3140" stroke-dasharray="4 4"/>
<polyline fill=none stroke="{col}" stroke-width=2 points="{pts}"/></svg></div>
{tbl("By grade — does A+ beat B?", rep['by_grade'])}
{tbl("By mode", rep['by_mode'])}
{tbl("By direction", rep['by_direction'])}
{tbl("By instrument", rep['by_instrument'])}
{tbl("By quarter", rep['by_quarter'])}
<div class=warn>PROXY backtest: spot-only history, so delta/footprint use close-in-range with bar RANGE as pseudo-volume;
no option OI (no walls), no true tick order-flow. Validates the reversal+level+divergence LOGIC and whether the
GRADE ranks outcomes — NOT the live order-flow magnitude edge, which can't be backtested. Outcomes in underlying R.</div>
</body></html>""")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--datasets", required=True)
    ap.add_argument("--label", default="setup-backtest")
    ap.add_argument("--profile", default="aggressive", help="risk profile (min grade); aggressive=all grades")
    args = ap.parse_args()
    run(args.datasets, args.label, profile=args.profile)
