"""DOM INDEX v3 BACKTEST (2026-11-09, Nikhil: "NOW BACK TEST") - MASTER_BACKTESTER gauntlet on the futures-only version.

Strategy (app/domcards.py, LEVELS = "fut", unchanged): Nifty / Bank Nifty near-month futures. Levels = medium / strong blocks of the
futures-only Level Map as of the PREVIOUS session (levelmap.build_fut) + the day's IB high / low after 10:15. HOLD: fresh touch (away
120 s+), not 2 bands through, rejected within 120 s, 30-s book lean >= 0.10 vs its 30-min median, absorption; stop 2 bands beyond the far
edge. BREAK: through the far edge, beyond for 30 s, lean >= 0.15, 60-s delta >= 15 % of volume; stop 1 band inside. Target = next level
>= 2R else 2R; out after 60 min / 15:20; 09:30-15:00 entries; one open trade per index; each level / setup / side once a day; 4 a day.
Fill = futures mid. Costs = synthetic future round trip 4.5 / 12.5 pts (domcards.SYN_PTS); futures 16.5 / 40 pts shown as stress.

Data: the Dhan order-flow engine's per-second files (bid / ask / mid, 20-level lean, market buy / sell) - they exist only since
2026-10-20, so the test is 14 sessions. No longer history is possible for this strategy (it needs the live book).
No look-ahead: levels from the previous session's bars; the book / tape only up to the current second; the engine's own code runs.
Gauntlet: 70 / 30 split by session; 2x / 3x costs; +1 / +2 pt slippage a side; best 3 trades removed; neighbouring settings
(lean, hold window, stop width, R:R, delta) one at a time with a count of how many were tried; by setup / side / index.
Run: TZ=Asia/Kolkata python3 scripts/research/dom_index_v3_backtest.py   -> data_store/research/dom_index_v3_bt/{trades_base.csv, grid.csv, report.txt}
"""
from __future__ import annotations

import copy
import io
import json
import os
import sys
from contextlib import redirect_stdout

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import dom_index_replay as R   # noqa: E402
D = R.D
D.IX_START = "2000-01-01"     # research: the live-record flush must not block past days
MODE = os.environ.get("DOM_MODE", "spot" if getattr(D, "PRICE", "fut") == "spot" else "fut")   # DOM_MODE=fut re-runs v3
D.PRICE = MODE
OUTD = os.path.join(R.ROOT, "data_store", "research", "dom_index_v3_bt" if MODE == "fut" else "dom_index_v4_spot_bt")
LOT = D.LOT; SYN = D.SYN_PTS; FUT = {"NIFTY": 16.5, "BANKNIFTY": 40.0}
P0 = copy.deepcopy(D.P); BAND0 = dict(D.BAND)


def load_days():
    out = []
    for day in R.days():
        rows = sorted(R._sec(day, "NIFTY") + R._sec(day, "BANKNIFTY"), key=lambda r: r[1])
        if len(rows) < 2000:
            continue
        if MODE == "spot":
            rows = R.spot_rows(day, rows)
            if not rows:
                continue
            lv = R.rebuilt_levels_spot(day, rows)
        else:
            p = os.path.join(D.OUT, f"levels_{day}.json"); L = json.load(open(p)) if os.path.exists(p) else {}
            if all(isinstance(L.get(k), dict) and L[k].get("prices") == "futures" for k in D.INDEX):
                lv = {k: L[k] for k in D.INDEX}
            else:
                lv = R.rebuilt_levels_fut(day)
        out.append((day, lv, rows))
    return out


def run(days, P=None, band=None, stop_bands=None):
    D.P.clear(); D.P.update(copy.deepcopy(P0)); D.P.update(P or {})
    D.BAND.clear(); D.BAND.update(BAND0); D.BAND.update(band or {})
    tr = []
    for day, lv, rows in days:
        tr += [x for x in R.replay(day, lv, rows) if x["status"] == "closed"]
    T = pd.DataFrame(tr)
    if T.empty:
        return T
    T["lot"] = T.key.map(LOT); T["pts"] = T.r_gross * T.risk
    T["pnl_syn"] = (T.pts - T.key.map(SYN)) * T.lot
    T["pnl_fut"] = (T.pts - T.key.map(FUT)) * T.lot
    T["r_syn"] = T.pnl_syn / (T.risk * T.lot)
    return T


def stats(T, col="pnl_syn"):
    if T is None or len(T) == 0:
        return {"n": 0}
    s = T[col]; w = s[s > 0].sum(); l = -s[s < 0].sum(); eq = s.cumsum()
    r = T[col] / (T.risk * T.lot)                       # R after the costs in this column
    return {"n": int(len(T)), "win%": round(100 * (s > 0).mean()), "avgR": round(float(r.mean()), 2), "PF": round(w / l, 2) if l else float("inf"),
            "rs": int(s.sum()), "maxDD": int((eq - eq.cummax()).min())}


def fmt(d):
    return " · ".join(f"{k} {v}" for k, v in d.items())


PK = os.path.join(OUTD, "days.pkl")


def days_cached():
    import pickle
    if os.path.exists(PK):
        return pickle.load(open(PK, "rb"))
    d = load_days(); pickle.dump(d, open(PK, "wb")); return d


def chunk(arg):
    """One piece per call (the device shell has a 3-minute limit): load | base | knob:<name> | band | report"""
    import pickle
    os.makedirs(OUTD, exist_ok=True)
    if arg == "load":
        d = days_cached(); print("cached", len(d), "sessions"); return
    days = days_cached(); names = [d for d, _, _ in days]; cut = names[int(len(names) * 0.7)]
    gp = os.path.join(OUTD, "grid_parts.json"); G = json.load(open(gp)) if os.path.exists(gp) else []
    def rec(knob, v, T2):
        st = stats(T2); tr_, te_ = (stats(T2[T2.day < cut]), stats(T2[T2.day >= cut])) if len(T2) else ({}, {})
        G.append({"knob": knob, "value": v, **st, "train_rs": tr_.get("rs"), "test_rs": te_.get("rs"), "train_avgR": tr_.get("avgR"), "test_avgR": te_.get("avgR"),
                  "n_break": int((T2.setup == "break").sum()) if len(T2) else 0, "break_rs": int(T2[T2.setup == "break"].pnl_syn.sum()) if len(T2) else 0})
        print(f"   {knob:12s} {v:<8} {fmt(st):70s} train {tr_.get('rs')} / test {te_.get('rs')}")
    if arg == "base":
        base = run(days); base.to_csv(os.path.join(OUTD, "trades_base.csv"), index=False); rec("base", "*", base)
    elif arg.startswith("knob:"):
        k = arg[5:]; vals = dict(KNOBS)[k]
        for v in vals:
            if v == P0[k]:
                continue
            rec(k, v, run(days, {k: v}))
    elif arg == "band":
        for bm in (0.0002, 0.0008):
            rec("band", bm, run(days, None, {"index": bm}))
    elif arg == "report":
        report(days, cut, G); return
    json.dump(G, open(gp, "w"), indent=1)


KNOBS = [("imb_hold", [0.05, 0.10, 0.15, 0.20]), ("imb_break", [0.10, 0.15, 0.20]), ("hold_s", [60, 120, 180, 240]), ("away_s", [60, 120, 300]),
         ("delta_break", [0.10, 0.15, 0.25]), ("rr", [1.5, 2.0, 3.0]), ("max_min", [30, 60, 90])]


def report(days, cut, G):
    names = [d for d, _, _ in days]
    base = pd.read_csv(os.path.join(OUTD, "trades_base.csv"))
    out = io.StringIO()
    with redirect_stdout(out):
        print(f"DOM INDEX {'v4 SPOT' if MODE == 'spot' else 'v3 FUTURES'} BACKTEST · {len(days)} sessions {names[0]} .. {names[-1]} · synthetic future 1 lot after costs (Rs) · "
              f"{'price path = spot (1-min bar opens), levels = spot Level Map + option walls, book from futures' if MODE == 'spot' else 'fills at the futures mid'}\n")
        print("1. BASE RULE          ", fmt(stats(base)))
        print("   train (first 70%)  ", fmt(stats(base[base.day < cut])), f"  [< {cut}]")
        print("   test  (last 30%)   ", fmt(stats(base[base.day >= cut])))
        print("   by setup           ", {k: fmt(stats(g)) for k, g in base.groupby("setup")})
        print("   by side            ", {k: fmt(stats(g)) for k, g in base.groupby("side")})
        print("   by index           ", {k: fmt(stats(g)) for k, g in base.groupby("key")})
        print("   by exit            ", {k: f"n {len(g)} avgR {g.r_syn.mean():+.2f}" for k, g in base.groupby("exit_reason")})
        print("   by level type      ", {k: fmt(stats(g)) for k, g in base.groupby(base.level.str.extract(r"^(IB|strong|medium)", expand=False).fillna("other"))})
        print("   per day            ", {d: int(v) for d, v in base.groupby("day").pnl_syn.sum().items()})
        print("\n2. STRESS (base rule)")
        for lab, T2 in (("futures costs 16.5 / 40 pts", base.assign(pnl_syn=base.pnl_fut)),
                        ("2x synthetic costs", base.assign(pnl_syn=(base.pts - 2 * base.key.map(SYN)) * base.lot)),
                        ("3x synthetic costs", base.assign(pnl_syn=(base.pts - 3 * base.key.map(SYN)) * base.lot)),
                        ("+1 pt slippage a side", base.assign(pnl_syn=(base.pts - base.key.map(SYN) - 2) * base.lot)),
                        ("+2 pts slippage a side", base.assign(pnl_syn=(base.pts - base.key.map(SYN) - 4) * base.lot)),
                        ("best 3 trades removed", base.sort_values("pnl_syn").iloc[:-3]),
                        ("plan account (first 3 a day, -2R stop)", plan(base))):
            print(f"   {lab:40s}", fmt(stats(T2)))
        print("\n3. NEIGHBOURING SETTINGS (one knob at a time; live settings: " + ", ".join(f"{k} {P0[k]}" for k, _ in KNOBS) + f", band {BAND0['index']})")
        GD = pd.DataFrame(G)
        GD = GD.fillna({"rs": 0, "maxDD": 0, "break_rs": 0, "n_break": 0, "avgR": 0})
        for r in GD.to_dict("records"):
            print(f"   {r['knob']:12s} {str(r['value']):<8} n {r['n']:3} win {r.get('win%')}% avgR {r.get('avgR', 0):+.2f} PF {r.get('PF')} Rs {int(r['rs'] or 0):+,} maxDD {int(r.get('maxDD') or 0):,}"
                  f"  train {r['train_rs']} / test {r['test_rs']}  | break n {r['n_break']} Rs {int(r['break_rs']):+,}")
        n_tried = len(GD); n_pos = int((GD.rs > 0).sum()); n_both = int(((GD.train_rs.fillna(-1) > 0) & (GD.test_rs.fillna(-1) > 0)).sum())
        print(f"\n   grid honesty: {n_tried} settings tried · {n_pos} positive overall · {n_both} positive in BOTH train and test "
              f"(with ~{len(base)} trades over 14 sessions, a few positives are expected by chance; nothing here is out-of-sample)")
        GD.to_csv(os.path.join(OUTD, "grid.csv"), index=False)
    txt = out.getvalue(); print(txt); open(os.path.join(OUTD, "report.txt"), "w").write(txt)


def main():
    os.makedirs(OUTD, exist_ok=True)
    days = load_days()
    names = [d for d, _, _ in days]
    out = io.StringIO()
    with redirect_stdout(out):
        print(f"DOM INDEX v3 BACKTEST · {len(days)} sessions {names[0]} .. {names[-1]} · synthetic future 1 lot after costs (Rs)\n")
        base = run(days)
        base.to_csv(os.path.join(OUTD, "trades_base.csv"), index=False)
        cut = names[int(len(names) * 0.7)]
        print("1. BASE RULE          ", fmt(stats(base)))
        print("   train (first 70%)  ", fmt(stats(base[base.day < cut])), f"  [< {cut}]")
        print("   test  (last 30%)   ", fmt(stats(base[base.day >= cut])))
        print("   by setup           ", {k: fmt(stats(g)) for k, g in base.groupby("setup")})
        print("   by side            ", {k: fmt(stats(g)) for k, g in base.groupby("side")})
        print("   by index           ", {k: fmt(stats(g)) for k, g in base.groupby("key")})
        print("   by exit            ", {k: f"n {len(g)} avgR {g.r_syn.mean():+.2f}" for k, g in base.groupby("exit_reason")})
        print("   by level type      ", {k: fmt(stats(g)) for k, g in base.groupby(base.level.str.extract(r"^(IB|strong|medium)", expand=False).fillna("other"))})
        print("   per day            ", {d: int(v) for d, v in base.groupby("day").pnl_syn.sum().items()})
        print("\n2. STRESS (base rule)")
        for lab, T2 in (("futures costs 16.5 / 40 pts", base.assign(pnl_syn=base.pnl_fut)),
                        ("2x synthetic costs", base.assign(pnl_syn=(base.pts - 2 * base.key.map(SYN)) * base.lot)),
                        ("3x synthetic costs", base.assign(pnl_syn=(base.pts - 3 * base.key.map(SYN)) * base.lot)),
                        ("+1 pt slippage a side", base.assign(pnl_syn=(base.pts - base.key.map(SYN) - 2) * base.lot)),
                        ("+2 pts slippage a side", base.assign(pnl_syn=(base.pts - base.key.map(SYN) - 4) * base.lot)),
                        ("best 3 trades removed", base.sort_values("pnl_syn").iloc[:-3]),
                        ("plan account (first 3 a day, -2R stop)", plan(base))):
            print(f"   {lab:40s}", fmt(stats(T2)))
        print("\n3. NEIGHBOURING SETTINGS (one knob at a time; * = the live setting)")
        grid = []
        knobs = [("imb_hold", [0.05, 0.10, 0.15, 0.20]), ("imb_break", [0.10, 0.15, 0.20]), ("hold_s", [60, 120, 180, 240]), ("away_s", [60, 120, 300]),
                 ("delta_break", [0.10, 0.15, 0.25]), ("rr", [1.5, 2.0, 3.0]), ("max_min", [30, 60, 90])]
        for k, vals in knobs:
            for v in vals:
                T2 = run(days, {k: v}); st = stats(T2); tr_, te_ = stats(T2[T2.day < cut]) if len(T2) else {}, stats(T2[T2.day >= cut]) if len(T2) else {}
                grid.append({"knob": k, "value": v, **st, "train_rs": tr_.get("rs"), "test_rs": te_.get("rs"), "train_avgR": tr_.get("avgR"), "test_avgR": te_.get("avgR")})
                print(f"   {k:12s} {v:<6}{'*' if v == P0[k] else ' '} {fmt(st):70s} train {tr_.get('rs')} / test {te_.get('rs')}")
        for bm in (0.0002, 0.0004, 0.0008):
            T2 = run(days, None, {"index": bm}); st = stats(T2); tr_, te_ = stats(T2[T2.day < cut]) if len(T2) else {}, stats(T2[T2.day >= cut]) if len(T2) else {}
            grid.append({"knob": "band", "value": bm, **st, "train_rs": tr_.get("rs"), "test_rs": te_.get("rs"), "train_avgR": tr_.get("avgR"), "test_avgR": te_.get("avgR")})
            print(f"   {'band':12s} {bm:<6}{'*' if bm == BAND0['index'] else ' '} {fmt(st):70s} train {tr_.get('rs')} / test {te_.get('rs')}")
        G = pd.DataFrame(grid); G.to_csv(os.path.join(OUTD, "grid.csv"), index=False)
        n_tried = len(G); n_pos = int((G.rs > 0).sum()); n_both = int(((G.train_rs.fillna(-1) > 0) & (G.test_rs.fillna(-1) > 0)).sum())
        print(f"\n   grid honesty: {n_tried} settings tried · {n_pos} positive overall · {n_both} positive in BOTH train and test "
              f"(with {len(base)} trades and 14 sessions, a few positives are expected by chance)")
        print("\n4. BREAK ONLY (every setting above, HOLD trades dropped) - the one piece that keeps showing up positive")
        for k, vals in (("imb_break", [0.10, 0.15, 0.20]), ("delta_break", [0.10, 0.15, 0.25])):
            for v in vals:
                T2 = run(days, {k: v}); T2 = T2[T2.setup == "break"]
                print(f"   {k:12s} {v:<6} {fmt(stats(T2))}")
        D.P.clear(); D.P.update(P0); D.BAND.clear(); D.BAND.update(BAND0)
    txt = out.getvalue(); print(txt)
    open(os.path.join(OUTD, "report.txt"), "w").write(txt)


def plan(T):
    pl = []
    for day, g in T.sort_values("opened").groupby("day"):
        took = []
        for _, x in g.iterrows():
            rc = sum(p.r_syn for p in took if str(p.closed) <= x.opened)
            if len(took) < 3 and rc > -2:
                took.append(x)
        pl += took
    return pd.DataFrame(pl)


if __name__ == "__main__":
    if len(sys.argv) > 1:
        chunk(sys.argv[1])
    else:
        main()
