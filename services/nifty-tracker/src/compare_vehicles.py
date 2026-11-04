"""Compare how the SAME qualified signals perform across trading vehicles.

For every paper trade produced by the backtest, we compute gross P&L (brokerage
ignored, per your request) under four vehicles:

  1. FUTURE           long/short future; P&L = spot move x lot; margin = future_margin
  2. OPTION BUY       buy ATM CE (long) / PE (short); P&L = premium change x lot;
                      capital = premium outlay
  3. OPTION SELL      sell ATM PE (long view) / CE (short view); P&L = premium
                      collected - buyback; margin = optsell_margin
  4. HYBRID           whichever vehicle the trade plan recommended for that setup
                      (futures on trend/BOS days, option-sell into OI walls)

Option premiums are modelled with Black-Scholes off the entry/exit spot + time
(see blackscholes.py) since expired option history isn't retrievable. Reports
total P&L, return on deployed capital, and win-rate per vehicle, plus per-day
equity, to an HTML file.

    python -m src.backtest --datasets datasets/<range> --label real-3mo --min-score 4
    python -m src.compare_vehicles --label real-3mo
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path

from .config import ROOT, load_config
from .blackscholes import bs_price, expiry_for, years_to_expiry


def _parse_dt(s):
    if not s:
        return None
    try:
        return dt.datetime.fromisoformat(s)
    except Exception:
        return None


def _leg_cost(buy_leg, sell_leg, c):
    """Round-trip charges given buy-leg and sell-leg notionals (Rs)."""
    brok = 2 * c["brokerage_per_order"]
    txn = c["exch_txn"] * (buy_leg + sell_leg)
    sebi = c["sebi"] * (buy_leg + sell_leg)
    stt = c["stt_sell"] * sell_leg
    gst = c["gst"] * (brok + txn + sebi)
    stamp = c["stamp_buy"] * buy_leg
    return brok + txn + sebi + stt + gst + stamp


def _fut_cost(entry, exit_, lot, long, c):
    buy_leg = (entry if long else exit_) * lot
    sell_leg = (exit_ if long else entry) * lot
    return _leg_cost(buy_leg, sell_leg, c)


def _opt_cost(e_prem, x_prem, lot, side, c):
    # buy: buy@entry, sell@exit; sell: sell@entry, buy@exit
    if side == "buy":
        buy_leg, sell_leg = e_prem * lot, x_prem * lot
    else:
        sell_leg, buy_leg = e_prem * lot, x_prem * lot
    return _leg_cost(buy_leg, sell_leg, c)


def _leg_charges(open_action, open_prem, close_prem, lot, c):
    """Round-trip charges for ONE option leg opened+closed intraday."""
    no, nc = open_prem * lot, close_prem * lot
    close_action = "sell" if open_action == "buy" else "buy"
    brok = 2 * c["brokerage_per_order"]
    txn = c["exch_txn"] * (no + nc)
    sebi = c["sebi"] * (no + nc)
    stt = (c["stt_sell"] * no if open_action == "sell" else 0) + \
          (c["stt_sell"] * nc if close_action == "sell" else 0)
    stamp = (c["stamp_buy"] * no if open_action == "buy" else 0) + \
            (c["stamp_buy"] * nc if close_action == "buy" else 0)
    gst = c["gst"] * (brok + txn + sebi)
    return brok + txn + sebi + stt + stamp + gst


def price_trade(tr, cfg_v, cfg_c, strike_step, r):
    """Return dict of per-vehicle NET P&L (Rs, after real costs) + capital + cost."""
    inst = tr["instrument"]
    p = cfg_v["per_instrument"].get(inst, {"lot": 65, "iv": 0.13, "expiry": "weekly"})
    lot = p["lot"] * cfg_v.get("lots", 1)
    iv = p["iv"]
    long = tr["direction"] == "long"
    sign = 1 if long else -1
    entry, exit_ = tr["entry"], tr.get("exit")
    if exit_ is None:
        return None
    edt = _parse_dt(tr.get("entry_dt")) or dt.datetime(2000, 1, 1, 9, 30)
    xdt = _parse_dt(tr.get("exit_dt")) or edt
    exp_date = expiry_for(edt.date(), p.get("expiry", "weekly"))
    Te = years_to_expiry(edt, exp_date)
    Tx = years_to_expiry(xdt, exp_date)
    strike = round(entry / strike_step) * strike_step

    fc, oc = cfg_c["futures"], cfg_c["options"]

    def bsp(S, K, T, typ):
        return bs_price(S, K, T, r, iv, typ)

    # 1) FUTURE (net of cost)
    fut_cost = _fut_cost(entry, exit_, lot, long, fc)
    fut = (exit_ - entry) * sign * lot - fut_cost

    # 2) OPTION BUY (CE if long, PE if short)
    btyp = "CE" if long else "PE"
    e_prem = bs_price(entry, strike, Te, r, iv, btyp)
    x_prem = bs_price(exit_, strike, Tx, r, iv, btyp)
    buy_cost = _opt_cost(e_prem, x_prem, lot, "buy", oc)
    buy = (x_prem - e_prem) * lot - buy_cost
    buy_cap = max(1.0, e_prem * lot)

    # 3) OPTION SELL as a CREDIT SPREAD (defined risk, low margin)
    #    long view  -> bull put spread: sell ATM PE, buy OTM PE (width lower)
    #    short view -> bear call spread: sell ATM CE, buy OTM CE (width higher)
    sw = cfg_v.get("sell_spread", {}).get("width", {}).get(inst, 300)
    sfloor = cfg_v.get("sell_spread", {}).get("min_margin", {}).get(inst, 6000)
    styp = "PE" if long else "CE"
    wing = (strike - sw) if long else (strike + sw)
    s_e, s_x = bsp(entry, strike, Te, styp), bsp(exit_, strike, Tx, styp)   # short leg
    w_e, w_x = bsp(entry, wing, Te, styp), bsp(exit_, wing, Tx, styp)       # long wing
    sell_cost = (_leg_charges("sell", s_e, s_x, lot, oc)
                 + _leg_charges("buy", w_e, w_x, lot, oc))
    sell = ((s_e - s_x) + (w_x - w_e)) * lot - sell_cost
    net_credit = (s_e - w_e) * lot
    sell_margin = max(sfloor, sw * lot - net_credit)

    # 5) SYNTHETIC FUTURE (buy ATM CE + sell ATM PE + deep-OTM hedge)
    syn = cfg_v.get("synthetic", {})
    hpts = syn.get("hedge_points", {}).get(inst, 600)
    syn_margin = syn.get("margin", {}).get(inst, 40000)

    if long:
        # +CE(K), -PE(K), +PE(K-hedge)
        ce_e, ce_x = bsp(entry, strike, Te, "CE"), bsp(exit_, strike, Tx, "CE")
        pe_e, pe_x = bsp(entry, strike, Te, "PE"), bsp(exit_, strike, Tx, "PE")
        hk = strike - hpts
        h_e, h_x = bsp(entry, hk, Te, "PE"), bsp(exit_, hk, Tx, "PE")
        pnl = ((ce_x - ce_e) + (pe_e - pe_x) + (h_x - h_e)) * lot
        cst = (_leg_charges("buy", ce_e, ce_x, lot, oc)
               + _leg_charges("sell", pe_e, pe_x, lot, oc)
               + _leg_charges("buy", h_e, h_x, lot, oc))
    else:
        # -CE(K), +PE(K), +CE(K+hedge)
        ce_e, ce_x = bsp(entry, strike, Te, "CE"), bsp(exit_, strike, Tx, "CE")
        pe_e, pe_x = bsp(entry, strike, Te, "PE"), bsp(exit_, strike, Tx, "PE")
        hk = strike + hpts
        h_e, h_x = bsp(entry, hk, Te, "CE"), bsp(exit_, hk, Tx, "CE")
        pnl = ((ce_e - ce_x) + (pe_x - pe_e) + (h_x - h_e)) * lot
        cst = (_leg_charges("sell", ce_e, ce_x, lot, oc)
               + _leg_charges("buy", pe_e, pe_x, lot, oc)
               + _leg_charges("buy", h_e, h_x, lot, oc))
    synth = pnl - cst

    # 4) SMART HYBRID — route between OPTION BUY (default) and SYNTHETIC (trend, full delta)
    hy = cfg_v.get("hybrid", {}) if isinstance(cfg_v.get("hybrid"), dict) else {}
    use_synth = _route_synth(tr, hy)
    if use_synth:
        hybrid, hcap, hcost = synth, syn_margin, cst
    else:
        hybrid, hcap, hcost = buy, buy_cap, buy_cost

    return {"future": fut, "fut_cost": fut_cost,
            "buy": buy, "buy_cap": buy_cap, "buy_cost": buy_cost,
            "sell": sell, "sell_cost": sell_cost, "sell_margin": sell_margin,
            "synth": synth, "synth_cost": cst, "synth_margin": syn_margin,
            "hybrid": hybrid, "hybrid_cap": hcap, "hybrid_cost": hcost,
            "hybrid_pick": "synthetic" if use_synth else "buy",
            "day_type": tr.get("day_type"),
            "instrument": inst, "day": edt.date().isoformat()}


def _route_synth(tr, hy):
    """True -> synthetic future; False -> option buy. Data-driven: synthetic on
    the day-types where full delta-1 wins (Trend / Neutral)."""
    return tr.get("day_type") in set(hy.get("synth_daytypes", ["Trend", "Neutral"]))


def _skip_trade(tr, hy):
    """Skip day-types that lose on both vehicles (default Normal-Variation)."""
    return tr.get("day_type") in set(hy.get("skip_daytypes", []))


def summarize(rows, key, capital, cost_key):
    pnl = [r[key] for r in rows]
    total = sum(pnl)
    n = len(pnl)
    wins = sum(1 for x in pnl if x > 0)
    avg = total / n if n else 0
    cost = sum(r[cost_key] for r in rows)
    return {
        "trades": n, "total_pnl": round(total),
        "win_rate": round(100 * wins / n, 1) if n else 0,
        "avg_pnl": round(avg),
        "return_pct": round(100 * total / capital, 1) if capital else None,
        "capital": round(capital),
        "total_cost": round(cost), "avg_cost": round(cost / n) if n else 0,
    }


def compare(label: str, instrument=None) -> dict:
    cfg = load_config()
    cfg_v = dict(cfg.a("vehicles", default={}))
    cfg_v["hybrid"] = cfg.a("hybrid", default={})
    cfg_c = cfg.a("costs", default={})
    r = float(cfg_v.get("risk_free", 0.065))
    steps = {i.key: i.strike_step for i in cfg.instruments}

    tp = ROOT / "backtests" / label / "trades.jsonl"
    if not tp.exists():
        raise SystemExit(f"No trades at {tp}. Run the backtest with --label {label} first.")
    trades = [json.loads(l) for l in tp.read_text().splitlines() if l.strip()]
    if instrument:
        trades = [t for t in trades if t.get("instrument") == instrument]
        if not trades:
            raise SystemExit(f"No {instrument} trades in {label}")

    priced = [price_trade(t, cfg_v, cfg_c, steps.get(t["instrument"], 50), r) for t in trades]
    priced = [p for p in priced if p]

    skip = set(cfg_v["hybrid"].get("skip_daytypes", []))
    hyb_rows = [p for p in priced if p.get("day_type") not in skip]   # smart hybrid trades these

    avg_buy_cap = sum(p["buy_cap"] for p in priced) / len(priced) if priced else 1
    avg_hyb_cap = sum(p["hybrid_cap"] for p in hyb_rows) / len(hyb_rows) if hyb_rows else 1
    avg_syn_margin = sum(p["synth_margin"] for p in priced) / len(priced) if priced else 40000
    n_synth = sum(1 for p in hyb_rows if p["hybrid_pick"] == "synthetic")

    # Only the two clean vehicles + the smart router between them
    vehicles = {
        "Option BUY (CE/PE)": summarize(priced, "buy", avg_buy_cap, "buy_cost"),
        "Synthetic future (3-leg)": summarize(priced, "synth", avg_syn_margin, "synth_cost"),
        "Smart hybrid (routed)": summarize(hyb_rows, "hybrid", avg_hyb_cap, "hybrid_cost"),
    }
    days = sorted({p["day"] for p in priced})
    curves = {}
    for key, rows in (("buy", priced), ("synth", priced), ("hybrid", hyb_rows)):
        cum, series = 0, []
        for d in days:
            cum += sum(p[key] for p in rows if p["day"] == d)
            series.append(round(cum))
        curves[key] = series

    report = {"label": label, "trades": len(priced), "vehicles": vehicles,
              "days": days, "curves": curves, "n_synth": n_synth,
              "n_buy": len(hyb_rows) - n_synth, "n_skipped": len(priced) - len(hyb_rows),
              "assumptions": {"iv": {k: v["iv"] for k, v in cfg_v["per_instrument"].items()},
                              "lots": {k: v["lot"] for k, v in cfg_v["per_instrument"].items()},
                              "avg_synth_margin": round(avg_syn_margin),
                              "avg_option_buy_cost": round(avg_buy_cap),
                              "synth_days": cfg_v["hybrid"].get("synth_daytypes", ["Trend", "Neutral"]),
                              "skip_days": cfg_v["hybrid"].get("skip_daytypes", [])}}
    report["label"] = f"{label} · {instrument}" if instrument else label
    out = ROOT / "backtests" / (f"{label}-{instrument}" if instrument else label)
    out.mkdir(parents=True, exist_ok=True)
    (out / "vehicle_comparison.json").write_text(json.dumps(report, indent=2))
    _html(out / "vehicle_comparison.html", report)
    print(f"\n=== INTRADAY VEHICLES ({report['label']}, {len(priced)} trades, NET of real costs) ===")
    print(f"    smart hybrid: {len(hyb_rows)} traded (synth {n_synth} / buy {len(hyb_rows)-n_synth}), "
          f"{len(priced)-len(hyb_rows)} skipped (Normal-Variation)")
    for name, s in vehicles.items():
        print(f"  {name:26s} NET Rs {s['total_pnl']:>9,} | {s['return_pct']:>7}% on "
              f"Rs{s['capital']:>7,} | win {s['win_rate']}% | cost Rs{s['avg_cost']:>4,}/tr")
    print(f"[report] {out/'vehicle_comparison.html'}")
    return report


def _html(path, rep):
    v = rep["vehicles"]
    order = list(v.keys())
    best = max(order, key=lambda k: v[k]["total_pnl"])
    rows = ""
    for k in order:
        s = v[k]
        col = "#3fb950" if s["total_pnl"] >= 0 else "#f85149"
        star = " ★" if k == best else ""
        rows += (f"<tr><td><b>{k}{star}</b></td>"
                 f"<td>{s['trades']}</td>"
                 f"<td style='color:{col};font-weight:700'>Rs {s['total_pnl']:,}</td>"
                 f"<td style='color:{col}'>{s['return_pct']}%</td>"
                 f"<td>{s['win_rate']}%</td>"
                 f"<td style='color:#f85149'>Rs {s['avg_cost']:,}</td>"
                 f"<td>Rs {s['capital']:,}</td></tr>")
    # equity curves
    cur = rep["curves"]
    allv = [x for s in cur.values() for x in s] or [0]
    lo, hi = min(allv + [0]), max(allv + [0])
    span = (hi - lo) or 1
    n = len(rep["days"]) or 1
    W, H = 860, 260
    colors = {"future": "#58a6ff", "synth": "#f778ba", "buy": "#3fb950",
              "sell": "#d29922", "hybrid": "#bc8cff"}
    names = {"future": "Future", "synth": "Synthetic", "buy": "Opt Buy",
             "sell": "Opt Sell", "hybrid": "Hybrid"}
    polys, legend = "", ""
    for key, series in cur.items():
        pts = " ".join(f"{8+i*(W-16)/max(1,n-1):.1f},{H-14-(e-lo)/span*(H-28):.1f}"
                       for i, e in enumerate(series))
        polys += f'<polyline fill=none stroke="{colors[key]}" stroke-width=2 points="{pts}"/>'
        legend += (f'<span style="color:{colors[key]}">&#9632;</span> {names[key]} '
                   f'(Rs {series[-1]:,})&nbsp;&nbsp;')
    zero_y = H - 14 - (0 - lo) / span * (H - 28)
    a = rep["assumptions"]
    path.write_text(f"""<!DOCTYPE html><html><head><meta charset=utf-8>
<title>Vehicle comparison · {rep['label']}</title><style>
body{{margin:0;background:#0e1117;color:#e6edf3;font:14px -apple-system,Segoe UI,Roboto,sans-serif;padding:22px}}
h1{{font-size:18px;margin:0 0 4px}} .sub{{color:#8b98a9;font-size:12px;margin-bottom:16px}}
table{{border-collapse:collapse;width:100%;margin-bottom:18px;font-size:13.5px}}
td,th{{padding:8px 12px;text-align:left;border-bottom:1px solid #222834}}
th{{color:#8b98a9;font-size:11px;text-transform:uppercase;letter-spacing:.5px}}
.card{{background:#161b22;border:1px solid #2a3140;border-radius:12px;padding:16px;margin-bottom:16px}}
h2{{font-size:12px;color:#8b98a9;text-transform:uppercase;letter-spacing:.6px;margin:0 0 10px}}
.legend{{font-size:12px;margin-top:8px}} .warn{{color:#d29922;font-size:11.5px;margin-top:6px;line-height:1.5}}
</style></head><body>
<h1>Intraday vehicles — {rep['label']}</h1>
<div class=sub>{rep['trades']} qualified trades · option-buy vs synthetic future vs smart router · <b>NET of real Zerodha option costs</b> · 1 lot ·
router chose synthetic on {rep.get('n_synth',0)}, option-buy on {rep.get('n_buy',0)}</div>
<div class=card><h2>Results per vehicle (net of costs)</h2>
<table><tr><th>Vehicle</th><th>Trades</th><th>Net P&amp;L</th><th>Return on capital</th><th>Win rate</th><th>Cost / trade</th><th>Capital base</th></tr>
{rows}</table>
<div class=warn>★ = highest total P&amp;L. <b>Smart hybrid</b> = SYNTHETIC future on {a.get('synth_days',[])} days, OPTION BUY otherwise,
and it SKIPS {a.get('skip_days',[])} days (they lose on both vehicles). "Return on capital" uses: synthetic
~Rs {a.get('avg_synth_margin',40000):,}, option-buy = avg premium outlay Rs {a['avg_option_buy_cost']:,}. Rules fit on this 3-month sample &mdash; validate out-of-sample.</div></div>
<div class=card><h2>Cumulative P&amp;L by vehicle (Rs, 1 lot)</h2>
<svg width={W} height={H} viewBox="0 0 {W} {H}">
<line x1=8 y1={zero_y:.1f} x2={W-8} y2={zero_y:.1f} stroke="#2a3140" stroke-dasharray="4 4"/>
{polys}</svg><div class=legend>{legend}</div></div>
<div class=warn>NET of real Zerodha option costs (Apr-2026): STT 0.15% on premium sell, NSE txn, SEBI, 18% GST, stamp.
Option-buy ~Rs 80/round-trip, synthetic ~Rs 215 (3 legs). Premiums Black-Scholes (ATM, IV {a['iv']}) off the spot
move — no bid/ask, IV smile, or vol-crush, so first-order. All intraday, flat by EOD.</div>
</body></html>""")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", default="real-3mo")
    ap.add_argument("--instrument", default=None, help="filter to NIFTY or BANKNIFTY")
    args = ap.parse_args()
    compare(args.label, args.instrument)
