"""Legged iron-condor strategy backtest.

On RANGE-compatible days, after the first-hour Initial Balance is set, this legs
in one credit spread at a time:

  * qualified SHORT signal  -> BEAR CALL spread (short strike at call-wall / VAH,
                               long wing `width` higher)
  * qualified LONG signal   -> BULL PUT spread (short strike at put-wall / VAL,
                               long wing `width` lower)

Each side is defined-risk and managed independently: take profit at 50% of max
credit, structural stop if spot ACCEPTS beyond the short strike (breakout), and
flatten by EOD. Legging in (never all four legs at once) means each short strike
is sold at a tested extreme, the two shorts sit wide apart, and a trending day
only ever triggers one side. Premiums are Black-Scholes modelled; charges are the
real Zerodha option costs.

    python -m src.backtest --datasets datasets/<range> --label real-3mo   # (for signals engine parity)
    python -m src.condor_backtest --datasets datasets/<range> --label condor-3mo
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
from pathlib import Path

from .config import ROOT, load_config
from .candles import floor_to_bucket
from .state import MarketState
from .engine import Engine
from .replay import ReplayFeed, build_replay_resolved
from .blackscholes import bs_price, expiry_for, years_to_expiry
from .compare_vehicles import _leg_charges


def _hhmm_to_min(s):
    h, m = s.split(":")
    return int(h) * 60 + int(m)


class CondorManager:
    def __init__(self, cfg):
        self.cfg = cfg
        c = cfg.a("condor", default={})
        v = cfg.a("vehicles", default={})
        self.min_score = int(c.get("min_score", 3))
        self.width = c.get("width", {})
        self.profit_take = float(c.get("profit_take", 0.5))
        self.sl_buffer = c.get("sl_buffer", {})
        self.min_otm = c.get("min_otm", {})
        self.eligible = set(c.get("eligible_daytypes", ["Balanced", "Normal", "Neutral"]))
        self.skip_ib = bool(c.get("skip_ib", True))
        self.per_inst = v.get("per_instrument", {})
        self.oc = cfg.a("costs", "options", default={})
        self.r = float(v.get("risk_free", 0.065))
        self.steps = {i.key: i.strike_step for i in cfg.instruments}
        self.open = {}                 # (inst, side) -> spread
        self.placed = set()            # (day, inst, side) already legged today
        self.closed = []

        self.open_min = _hhmm_to_min(cfg.a("session", "market_open", default="09:15"))
        self.ib_end_min = self.open_min + int(cfg.a("session", "ib_minutes", default=60))
        self.close_min = _hhmm_to_min(cfg.a("session", "market_close", default="15:30"))

    # ---- pricing helpers -------------------------------------------------
    def _spread_val(self, spot, short_k, wing_k, T, typ, iv):
        return bs_price(spot, short_k, T, self.r, iv, typ) - \
               bs_price(spot, wing_k, T, self.r, iv, typ)

    # ---- main step -------------------------------------------------------
    def step(self, snap, market_dt):
        insts = snap.get("instruments", {})
        # session clock from latest candle
        st = None
        for v in insts.values():
            cs = v.get("candles") or []
            if cs:
                st = max(st, cs[-1]["t"]) if st else cs[-1]["t"]
        cur_min = _hhmm_to_min(st) if st else self.open_min
        eod = cur_min >= self.close_min
        ib_done = cur_min >= self.ib_end_min

        for key, v in insts.items():
            self._manage(key, v, market_dt, eod)
            if eod or not ib_done and self.skip_ib:
                continue
            mp = v.get("market_profile", {})
            if mp.get("day_type") not in self.eligible:
                continue
            spot = v.get("spot")
            if spot is None:
                continue
            oi = v.get("oi", {})
            day = market_dt.date().isoformat() if hasattr(market_dt, "date") else str(market_dt)[:10]
            sigs = v.get("signals", [])
            has_short = any(s["direction"] == "short" and s["score"] >= self.min_score for s in sigs)
            has_long = any(s["direction"] == "long" and s["score"] >= self.min_score for s in sigs)
            if has_short and (key, "call") not in self.open and (day, key, "call") not in self.placed:
                self._open(key, "call", spot, mp, oi, market_dt, day)
            if has_long and (key, "put") not in self.open and (day, key, "put") not in self.placed:
                self._open(key, "put", spot, mp, oi, market_dt, day)

    def _open(self, key, side, spot, mp, oi, market_dt, day):
        step = self.steps.get(key, 50)
        width = self.width.get(key, 300)
        min_otm = self.min_otm.get(key, 20)
        p = self.per_inst.get(key, {"iv": 0.13, "lot": 65, "expiry": "weekly"})
        iv, lot = p["iv"], p["lot"]
        exp_date = expiry_for(market_dt.date(), p.get("expiry", "weekly"))
        T = years_to_expiry(market_dt, exp_date)

        if side == "call":
            cands = [x for x in (oi.get("call_wall"), mp.get("vah"), mp.get("ib_high")) if x]
            boundary = max(cands) if cands else spot + min_otm
            short_k = math.ceil(max(boundary, spot + min_otm) / step) * step
            wing_k = short_k + width
            typ = "CE"
        else:
            cands = [x for x in (oi.get("put_wall"), mp.get("val"), mp.get("ib_low")) if x]
            boundary = min(cands) if cands else spot - min_otm
            short_k = math.floor(min(boundary, spot - min_otm) / step) * step
            wing_k = short_k - width
            typ = "PE"

        short_e = bs_price(spot, short_k, T, self.r, iv, typ)
        wing_e = bs_price(spot, wing_k, T, self.r, iv, typ)
        credit = short_e - wing_e            # per unit
        if credit <= 0.5:                    # not enough premium to bother
            return
        margin = max(1000.0, width * lot - credit * lot)
        self.open[(key, side)] = {
            "inst": key, "side": side, "short_k": short_k, "wing_k": wing_k,
            "typ": typ, "short_e": short_e, "wing_e": wing_e, "credit": credit,
            "lot": lot, "iv": iv, "exp": exp_date, "entry_dt": market_dt.isoformat(),
            "entry_spot": round(spot, 2), "margin": round(margin), "day": day,
        }
        self.placed.add((day, key, side))

    def _manage(self, key, v, market_dt, eod):
        spot = v.get("spot")
        if spot is None:
            return
        for side in ("call", "put"):
            sp = self.open.get((key, side))
            if not sp:
                continue
            T = years_to_expiry(market_dt, sp["exp"])
            cur = self._spread_val(spot, sp["short_k"], sp["wing_k"], T, sp["typ"], sp["iv"])
            reason = None
            if cur <= (1 - self.profit_take) * sp["credit"]:
                reason = "target"                       # captured >=50% of credit
            elif side == "call" and spot >= sp["short_k"] + self.sl_buffer.get(key, 15):
                reason = "stop"                          # accepted above short call
            elif side == "put" and spot <= sp["short_k"] - self.sl_buffer.get(key, 15):
                reason = "stop"
            elif eod:
                reason = "eod"
            if reason:
                self._close(key, side, spot, market_dt, reason)

    def _close(self, key, side, spot, market_dt, reason):
        sp = self.open.pop((key, side))
        T = years_to_expiry(market_dt, sp["exp"])
        short_x = bs_price(spot, sp["short_k"], T, self.r, sp["iv"], sp["typ"])
        wing_x = bs_price(spot, sp["wing_k"], T, self.r, sp["iv"], sp["typ"])
        exit_val = short_x - wing_x
        lot = sp["lot"]
        cost = (_leg_charges("sell", sp["short_e"], short_x, lot, self.oc)
                + _leg_charges("buy", sp["wing_e"], wing_x, lot, self.oc))
        pnl = (sp["credit"] - exit_val) * lot - cost
        sp.update({"exit_spot": round(spot, 2), "exit_reason": reason,
                   "exit_val": round(exit_val, 2), "cost": round(cost),
                   "pnl": round(pnl), "result": "win" if pnl > 0 else "loss"})
        self.closed.append(sp)

    def flatten(self, snap, market_dt):
        for key, v in snap.get("instruments", {}).items():
            for side in ("call", "put"):
                if (key, side) in self.open:
                    self._close(key, side, v.get("spot", self.open[(key, side)]["entry_spot"]),
                                market_dt, "eod")


def run(datasets_root, label):
    cfg = load_config()
    root = Path(datasets_root)
    if not root.is_absolute():
        root = ROOT / root
    days = sorted([p for p in root.iterdir() if p.is_dir() and any(p.glob("*_spot.csv"))])
    if not days:
        raise SystemExit(f"No day-datasets under {root}")

    mgr = CondorManager(cfg)
    per_day = []
    for d in days:
        resolved = build_replay_resolved(cfg, d)
        state = MarketState(resolved, cfg.candle_minutes)
        engine = Engine(cfg, state)
        feed = ReplayFeed(cfg, d, resolved, state)
        before = len(mgr.closed)
        last = None
        for ts, token, ltp, cv, oi in feed.events:
            state.apply_tick(token, ts, ltp, cv, oi)
            b = floor_to_bucket(ts, cfg.candle_minutes)
            if last is None:
                last = b
            elif b > last:
                mgr.step(engine.compute(), last)
                last = b
        snap = engine.compute()
        mgr.step(snap, last)
        mgr.flatten(snap, last)
        day_sp = mgr.closed[before:]
        per_day.append({"day": d.name, "spreads": len(day_sp),
                        "net": round(sum(s["pnl"] for s in day_sp))})

    _report(cfg, label, mgr.closed, per_day, len(days))


def _report(cfg, label, closed, per_day, ndays):
    n = len(closed)
    calls = [s for s in closed if s["side"] == "call"]
    puts = [s for s in closed if s["side"] == "put"]
    wins = [s for s in closed if s["result"] == "win"]
    net = sum(s["pnl"] for s in closed)
    credit = sum(s["credit"] * s["lot"] for s in closed)
    avg_margin = (sum(s["margin"] for s in closed) / n) if n else 0
    # condor completion: days with both a call and a put spread
    by_day = {}
    for s in closed:
        by_day.setdefault(s["day"], set()).add(s["side"])
    both = sum(1 for sides in by_day.values() if {"call", "put"} <= sides)

    def side_stat(rows):
        if not rows:
            return {"n": 0, "win": 0, "net": 0}
        return {"n": len(rows),
                "win": round(100 * sum(1 for s in rows if s["result"] == "win") / len(rows), 1),
                "net": round(sum(s["pnl"] for s in rows))}

    stats = {
        "label": label, "days": ndays, "days_traded": len(by_day),
        "spreads": n, "condor_days": both,
        "net_pnl": round(net), "credit_collected": round(credit),
        "win_rate": round(100 * len(wins) / n, 1) if n else 0,
        "avg_pnl": round(net / n) if n else 0,
        "avg_margin": round(avg_margin),
        "return_pct": round(100 * net / avg_margin, 1) if avg_margin else None,
        "call": side_stat(calls), "put": side_stat(puts),
        "sl_rate": round(100 * sum(1 for s in closed if s["exit_reason"] == "stop") / n, 1) if n else 0,
        "target_rate": round(100 * sum(1 for s in closed if s["exit_reason"] == "target") / n, 1) if n else 0,
    }
    out = ROOT / "backtests" / label
    out.mkdir(parents=True, exist_ok=True)
    (out / "condor_report.json").write_text(json.dumps(
        {"stats": stats, "per_day": per_day}, indent=2))
    # equity curve
    days = sorted(by_day)
    eq, cum = [], 0
    for day in days:
        cum += sum(s["pnl"] for s in closed if s["day"] == day)
        eq.append(round(cum))
    _html(out / "condor_report.html", stats, eq)

    print(f"\n=== LEGGED IRON CONDOR ({label}) ===")
    print(f"  {ndays} days, {stats['days_traded']} traded, {n} spreads "
          f"({stats['condor_days']} full-condor days)")
    print(f"  NET Rs {stats['net_pnl']:,} | win {stats['win_rate']}% | avg Rs {stats['avg_pnl']:,}/spread "
          f"| {stats['return_pct']}% on ~Rs{stats['avg_margin']:,} margin")
    print(f"  call side: {stats['call']}  put side: {stats['put']}")
    print(f"  exits: target {stats['target_rate']}%  stop {stats['sl_rate']}%")
    print(f"  [report] {out/'condor_report.html'}")


def _html(path, s, eq):
    eq = eq or [0]
    lo, hi = min(eq + [0]), max(eq + [0])
    span = (hi - lo) or 1
    W, H, n = 860, 240, len(eq)
    pts = " ".join(f"{8+i*(W-16)/max(1,n-1):.1f},{H-12-(e-lo)/span*(H-24):.1f}"
                   for i, e in enumerate(eq))
    zero = H - 12 - (0 - lo) / span * (H - 24)
    col = "#3fb950" if s["net_pnl"] >= 0 else "#f85149"
    tiles = [("Net P&L", f"Rs {s['net_pnl']:,}"), ("Return", f"{s['return_pct']}%"),
             ("Win rate", f"{s['win_rate']}%"), ("Spreads", s["spreads"]),
             ("Full-condor days", f"{s['condor_days']}/{s['days_traded']}"),
             ("Avg margin", f"Rs {s['avg_margin']:,}"),
             ("Target exits", f"{s['target_rate']}%"), ("Stop exits", f"{s['sl_rate']}%")]
    th = "".join(f"<div class=t><div class=k>{k}</div><div class=v>{val}</div></div>" for k, val in tiles)
    path.write_text(f"""<!DOCTYPE html><html><head><meta charset=utf-8><title>Legged condor · {s['label']}</title><style>
body{{margin:0;background:#0e1117;color:#e6edf3;font:14px -apple-system,Segoe UI,Roboto,sans-serif;padding:22px}}
h1{{font-size:18px;margin:0 0 4px}} .sub{{color:#8b98a9;font-size:12px;margin-bottom:16px}}
.tiles{{display:flex;flex-wrap:wrap;gap:10px;margin-bottom:18px}}
.t{{background:#161b22;border:1px solid #2a3140;border-radius:10px;padding:10px 14px;min-width:120px}}
.t .k{{font-size:10.5px;color:#8b98a9;text-transform:uppercase;letter-spacing:.5px}} .t .v{{font-size:19px;font-weight:750;margin-top:3px}}
.card{{background:#161b22;border:1px solid #2a3140;border-radius:12px;padding:16px;margin-bottom:16px}}
h2{{font-size:12px;color:#8b98a9;text-transform:uppercase;letter-spacing:.6px;margin:0 0 10px}}
table{{border-collapse:collapse;font-size:13px}} td,th{{padding:4px 16px 4px 0;text-align:left}} th{{color:#8b98a9}}
.warn{{color:#d29922;font-size:11.5px;margin-top:8px;line-height:1.5}}</style></head><body>
<h1>Legged iron condor — {s['label']}</h1>
<div class=sub>{s['days']} days · {s['days_traded']} traded · range-day gate + IB-skip · 50% profit / short-strike stop · net of real option costs</div>
<div class=tiles>{th}</div>
<div class=card><h2>Cumulative P&amp;L (Rs, 1 lot)</h2>
<svg width={W} height={H} viewBox="0 0 {W} {H}"><line x1=8 y1={zero:.1f} x2={W-8} y2={zero:.1f} stroke="#2a3140" stroke-dasharray="4 4"/>
<polyline fill=none stroke="{col}" stroke-width=2 points="{pts}"/></svg></div>
<div class=card><h2>By side</h2>
<table><tr><th>Side</th><th>Spreads</th><th>Win rate</th><th>Net P&amp;L</th></tr>
<tr><td>Bear call spread</td><td>{s['call']['n']}</td><td>{s['call']['win']}%</td><td>Rs {s['call']['net']:,}</td></tr>
<tr><td>Bull put spread</td><td>{s['put']['n']}</td><td>{s['put']['win']}%</td><td>Rs {s['put']['net']:,}</td></tr></table></div>
<div class=warn>Legged in one side at a time on range days after the IB; short strikes at OI wall / value edge, wing {s.get('width','')}pts out.
Credit collected Rs {s['credit_collected']:,} gross. Premiums Black-Scholes (ATM-relative), charges = real option costs; no bid/ask or IV
smile. Full-condor days = both sides legged same day. All intraday, flat by EOD.</div></body></html>""")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--datasets", required=True)
    ap.add_argument("--label", default="condor-3mo")
    args = ap.parse_args()
    run(args.datasets, args.label)
