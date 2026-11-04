"""Month-over-month & quarter-over-quarter robustness report.

Prices every backtested trade across the three intraday vehicles (option buy,
synthetic future, smart hybrid) and breaks the smart-hybrid P&L down by month and
quarter, so you can see whether the edge is CONSISTENT across regimes or just a
couple of lucky months. Also does a simple walk-forward split (first half vs
second half) and reports consistency metrics.

    python -m src.period_report --label real-2yr
    python -m src.period_report --trades paper/paper_trades.jsonl --label forward
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from .config import ROOT, load_config
from .compare_vehicles import price_trade


def _quarter(ym):            # "2026-07" -> "2026-Q3"
    y, m = ym.split("-")
    return f"{y}-Q{(int(m) - 1) // 3 + 1}"


def _agg(rows, key):
    pnl = [r[key] for r in rows]
    n = len(pnl)
    wins = sum(1 for x in pnl if x > 0)
    return {"n": n, "net": round(sum(pnl)),
            "win": round(100 * wins / n, 1) if n else 0}


def _load_priced(label, trades_path, instrument=None):
    cfg = load_config()
    cfg_v = dict(cfg.a("vehicles", default={}))
    cfg_v["hybrid"] = cfg.a("hybrid", default={})
    cfg_c = cfg.a("costs", default={})
    r = float(cfg_v.get("risk_free", 0.065))
    steps = {i.key: i.strike_step for i in cfg.instruments}
    tp = Path(trades_path) if trades_path else ROOT / "backtests" / label / "trades.jsonl"
    if not tp.exists():
        raise SystemExit(f"No trades at {tp}")
    trades = [json.loads(l) for l in tp.read_text().splitlines() if l.strip()]
    if instrument:
        trades = [t for t in trades if t.get("instrument") == instrument]
    skip = set(cfg_v["hybrid"].get("skip_daytypes", []))
    priced = []
    for t in trades:
        p = price_trade(t, cfg_v, cfg_c, steps.get(t["instrument"], 50), r)
        if not p:
            continue
        p["month"] = p["day"][:7]
        p["skip"] = p.get("day_type") in skip
        priced.append(p)
    return priced


def report(label, trades_path=None, instrument=None):
    priced = _load_priced(label, trades_path, instrument)
    hyb = [p for p in priced if not p["skip"]]         # smart hybrid actually trades these
    months = sorted({p["month"] for p in priced})

    # per-month rows (hybrid primary; buy/synth for reference)
    mrows = []
    for m in months:
        allm = [p for p in priced if p["month"] == m]
        hm = [p for p in hyb if p["month"] == m]
        mrows.append({"period": m,
                      "hybrid": _agg(hm, "hybrid"),
                      "buy": _agg(allm, "buy"),
                      "synth": _agg(allm, "synth"),
                      "skipped": len(allm) - len(hm)})
    # per-quarter
    quarters = sorted({_quarter(m) for m in months})
    qrows = []
    for q in quarters:
        allq = [p for p in priced if _quarter(p["month"]) == q]
        hq = [p for p in hyb if _quarter(p["month"]) == q]
        qrows.append({"period": q, "hybrid": _agg(hq, "hybrid"),
                      "buy": _agg(allq, "buy"), "synth": _agg(allq, "synth")})

    # consistency + walk-forward on the hybrid
    m_net = [mr["hybrid"]["net"] for mr in mrows]
    prof_months = sum(1 for x in m_net if x > 0)
    worst = min(m_net) if m_net else 0
    best = max(m_net) if m_net else 0
    streak = cur = 0
    for x in m_net:
        cur = cur + 1 if x < 0 else 0
        streak = max(streak, cur)
    half = len(months) // 2 or 1
    fh = [p for p in hyb if p["month"] in set(months[:half])]
    sh = [p for p in hyb if p["month"] in set(months[half:])]

    summary = {
        "label": f"{label} · {instrument}" if instrument else label,
        "trades": len(priced), "hybrid_trades": len(hyb),
        "months": len(months), "quarters": len(quarters),
        "hybrid_total": round(sum(p["hybrid"] for p in hyb)),
        "profitable_months": prof_months, "profitable_pct": round(100 * prof_months / len(months), 0) if months else 0,
        "best_month": best, "worst_month": worst, "max_losing_streak": streak,
        "walkfwd_first": _agg(fh, "hybrid"), "walkfwd_second": _agg(sh, "hybrid"),
    }
    out = ROOT / "backtests" / (f"{label}-{instrument}" if instrument else label)
    out.mkdir(parents=True, exist_ok=True)
    (out / "period_report.json").write_text(json.dumps(
        {"summary": summary, "months": mrows, "quarters": qrows}, indent=2))
    _html(out / "period_report.html", summary, mrows, qrows)

    print(f"\n=== PERIOD ANALYSIS ({summary['label']}) — option buy ===")
    print(f"  {summary['months']} months | total Rs {summary['hybrid_total']:,} | "
          f"profitable months {prof_months}/{len(months)} ({summary['profitable_pct']:.0f}%)")
    print(f"  best month Rs {best:,} | worst Rs {worst:,} | max losing streak {streak}")
    print(f"  walk-forward: 1st half Rs {summary['walkfwd_first']['net']:,} "
          f"({summary['walkfwd_first']['win']}% win) | 2nd half Rs {summary['walkfwd_second']['net']:,} "
          f"({summary['walkfwd_second']['win']}% win)")
    print("  Quarter-over-quarter:")
    for q in qrows:
        print(f"    {q['period']}: hybrid Rs {q['hybrid']['net']:>9,} ({q['hybrid']['n']} tr, {q['hybrid']['win']}% win)"
              f" | buy Rs {q['buy']['net']:>9,} | synth Rs {q['synth']['net']:>9,}")
    print(f"  [report] {out/'period_report.html'}")


def _bar_svg(mrows, W=880, H=200):
    vals = [mr["hybrid"]["net"] for mr in mrows] or [0]
    hi, lo = max(vals + [0]), min(vals + [0])
    span = (hi - lo) or 1
    n = len(vals)
    bw = (W - 20) / max(1, n)
    zero = 10 + (H - 20) * (hi / span)
    bars = ""
    for i, v in enumerate(vals):
        x = 10 + i * bw
        h = (abs(v) / span) * (H - 20)
        y = zero - h if v >= 0 else zero
        col = "#3fb950" if v >= 0 else "#f85149"
        bars += f'<rect x="{x:.1f}" y="{y:.1f}" width="{bw*0.8:.1f}" height="{max(1,h):.1f}" fill="{col}"/>'
    return (f'<svg width={W} height={H} viewBox="0 0 {W} {H}">'
            f'<line x1=6 y1={zero:.1f} x2={W-6} y2={zero:.1f} stroke="#2a3140"/>{bars}</svg>')


def _rows_html(rows):
    out = ""
    for r in rows:
        h, b, s = r["hybrid"], r["buy"], r["synth"]
        hc = "#3fb950" if h["net"] >= 0 else "#f85149"
        out += (f"<tr><td>{r['period']}</td>"
                f"<td>{h['n']}</td><td style='color:{hc};font-weight:650'>Rs {h['net']:,}</td>"
                f"<td>{h['win']}%</td>"
                f"<td class=mut>Rs {b['net']:,}</td><td class=mut>Rs {s['net']:,}</td></tr>")
    return out


def _html(path, s, mrows, qrows):
    wf1, wf2 = s["walkfwd_first"], s["walkfwd_second"]
    tiles = [("Total (hybrid)", f"Rs {s['hybrid_total']:,}"),
             ("Months", s["months"]),
             ("Profitable months", f"{s['profitable_months']}/{s['months']} ({s['profitable_pct']:.0f}%)"),
             ("Best month", f"Rs {s['best_month']:,}"), ("Worst month", f"Rs {s['worst_month']:,}"),
             ("Max losing streak", f"{s['max_losing_streak']} mo"),
             ("Walk-fwd 1st half", f"Rs {wf1['net']:,}"), ("Walk-fwd 2nd half", f"Rs {wf2['net']:,}")]
    th = "".join(f"<div class=t><div class=k>{k}</div><div class=v>{v}</div></div>" for k, v in tiles)
    path.write_text(f"""<!DOCTYPE html><html><head><meta charset=utf-8><title>Period analysis · {s['label']}</title><style>
body{{margin:0;background:#0e1117;color:#e6edf3;font:14px -apple-system,Segoe UI,Roboto,sans-serif;padding:22px}}
h1{{font-size:18px;margin:0 0 4px}} .sub{{color:#8b98a9;font-size:12px;margin-bottom:16px}}
.tiles{{display:flex;flex-wrap:wrap;gap:10px;margin-bottom:18px}}
.t{{background:#161b22;border:1px solid #2a3140;border-radius:10px;padding:10px 14px;min-width:120px}}
.t .k{{font-size:10px;color:#8b98a9;text-transform:uppercase;letter-spacing:.5px}} .t .v{{font-size:18px;font-weight:750;margin-top:3px}}
.card{{background:#161b22;border:1px solid #2a3140;border-radius:12px;padding:16px;margin-bottom:16px}}
h2{{font-size:12px;color:#8b98a9;text-transform:uppercase;letter-spacing:.6px;margin:0 0 10px}}
table{{border-collapse:collapse;width:100%;font-size:13px}} td,th{{padding:6px 12px;text-align:left;border-bottom:1px solid #222834}}
th{{color:#8b98a9;font-size:11px;text-transform:uppercase}} .mut{{color:#8b98a9}}
.warn{{color:#d29922;font-size:11.5px;margin-top:8px;line-height:1.5}}</style></head><body>
<h1>Period analysis — {s['label']}</h1>
<div class=sub>Smart hybrid, month-over-month &amp; quarter-over-quarter · {s['trades']} signals, {s['hybrid_trades']} traded · net of real option costs</div>
<div class=tiles>{th}</div>
<div class=card><h2>Monthly P&amp;L (hybrid, Rs 1 lot)</h2>{_bar_svg(mrows)}</div>
<div class=card><h2>Quarter over quarter</h2>
<table><tr><th>Quarter</th><th>Trades</th><th>Hybrid net</th><th>Win</th><th>Opt-buy net</th><th>Synth net</th></tr>
{_rows_html(qrows)}</table></div>
<div class=card><h2>Month over month</h2>
<table><tr><th>Month</th><th>Trades</th><th>Hybrid net</th><th>Win</th><th>Opt-buy net</th><th>Synth net</th></tr>
{_rows_html(mrows)}</table></div>
<div class=warn>Walk-forward = first-half months vs second-half (a held-out check). Consistent sign/size across quarters and a
positive 2nd half are what "sure" looks like; one or two months carrying the total is a red flag. Vehicle P&amp;L is
Black-Scholes-modelled off spot; older months lack the volume/OI signal layer (not retrievable). Not investment advice.</div>
</body></html>""")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", default="real-3mo")
    ap.add_argument("--trades", default=None, help="explicit trades.jsonl (e.g. a live paper file)")
    ap.add_argument("--instrument", default=None, help="filter to NIFTY or BANKNIFTY")
    args = ap.parse_args()
    report(args.label, args.trades, args.instrument)
