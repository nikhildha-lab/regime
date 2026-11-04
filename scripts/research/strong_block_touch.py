"""STRONG-BLOCK TOUCH (2026-11-04, Nikhil: "let's optimize it to strong levels"). Paper research only, never orders.
Rule fixed BEFORE running:
  map     : app/levelmap.build(key, asof = previous session) - the block map as it looked the evening before (no look-ahead),
            shifted from spot to futures by the day's basis (futures mid - option-chain spot, median 09:20-09:40)
  touch   : a 1-min bar reaches a block's near edge from outside (prev close above a block -> support test, below -> resistance);
            re-touch only after 15 min; 09:30-14:30; one open trade per index
  confirm : touch-minute delta / volume > +0.2 at support, < -0.2 at resistance (the touch-minute rule)
  trade   : enter at the touch bar close; stop on a 1-min close 0.05 % beyond the block's FAR edge; target 2R; +1R -> breakeven;
            out after 60 min or 15:20 (all on 1-min closes)
  money   : synthetic future (buy ATM call + sell ATM put), 1 lot, Nifty 65 / Bank Nifty 30, cost 4.5 / 12.5 pts round trip
Reported for strong blocks (the rule) and, for comparison only, medium / weak blocks and strong blocks without the confirm.
"""
import os, sys
import numpy as np, pandas as pd
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path[:0] = [os.path.join(ROOT, "app"), os.path.dirname(__file__)]
import levelmap as LM
import touch_dom_combo as TDC

OCD = os.path.join(ROOT, "data_store", "flow", "option_chain")
LOT, COST = TDC.LOT, TDC.COST


def basis(day, sym, B):
    o = pd.read_csv(os.path.join(OCD, f"{day}.csv.gz"), usecols=["ts", "symbol", "underlying"])
    o = o[o.symbol == sym]; o["time"] = o.ts.str[11:16]
    o = o[(o.time >= "09:20") & (o.time <= "09:40")].groupby("time").underlying.median()
    j = B.set_index("time").c.reindex(o.index).dropna()
    return float((j - o.reindex(j.index)).median()) if len(j) else None


def run():
    days = sorted(d for d in os.listdir(TDC.OF) if len(d) == 10 and d[4] == "-")
    ocd = sorted(f[:10] for f in os.listdir(OCD) if f.endswith(".csv.gz"))
    rows, info = [], []
    for sym in ("NIFTY", "BANKNIFTY"):
        for di, day in enumerate(days):
            prev = [d for d in ocd if d < day]
            if not prev or day not in ocd:
                continue
            try:
                B = TDC.bars(day, sym)
            except Exception:
                continue
            if B.vol.sum() == 0 or len(B) < 60:
                continue
            bs = basis(day, sym, B)
            if bs is None:
                continue
            m = LM.build(sym, asof=prev[-1])
            Z = [(z["lo"] + bs, z["hi"] + bs, z["strength"]) for z in m["blocks"]]
            info.append((day, sym, prev[-1], round(bs, 1), sum(z[2] == "strong" for z in Z), sum(z[2] == "medium" for z in Z)))
            last, busy = {}, -1
            for i in range(1, len(B) - 1):
                b, p = B.iloc[i], B.iloc[i - 1]
                if not ("09:30" <= b.time <= "14:30") or i <= busy:
                    continue
                for lo, hi, st in Z:
                    side = 1 if (p.c > hi and b.l <= hi) else -1 if (p.c < lo and b.h >= lo) else 0
                    if not side:
                        continue
                    key = (lo, hi)
                    if key in last and b.t - last[key] < 900:
                        continue
                    last[key] = b.t
                    td = (b.delta / b.vol if b.vol else 0) * side
                    e = b.c
                    stop = lo * (1 - 0.0005) if side > 0 else hi * (1 + 0.0005)
                    R = side * (e - stop)
                    if R <= e * 0.0005:                       # already through the far edge on the touch bar
                        continue
                    tgt = e + side * 2 * R; sl, why, ex, j = stop, "60 min", e, i
                    for j in range(i + 1, min(i + 61, len(B))):
                        f = B.iloc[j]
                        if f.time > "15:20":
                            break
                        ex = f.c
                        if side * (f.c - sl) <= 0:
                            why = "stop" if sl == stop else "breakeven"; break
                        if side * (f.c - tgt) >= 0:
                            why = "target"; break
                        if side * (f.c - e) >= R:
                            sl = e if side * (sl - e) < 0 else sl
                    f30 = B.iloc[i + 1:i + 31]
                    pts = side * (ex - e)
                    rows.append({"day": day, "sym": sym, "time": b.time, "strength": st, "side": side, "zone": f"{lo:.0f}-{hi:.0f}",
                                 "td": round(td, 2), "confirm": td > 0.2, "risk": round(R, 1), "pts": round(pts, 1),
                                 "p30": round(side * (f30.c.iloc[-1] - e), 1) if len(f30) else 0.0, "exit": why,
                                 "rs": round((pts - COST[sym]) * LOT[sym])})
                    if td > 0.2 and st == "strong":
                        busy = j                               # one open rule trade per index
                    break
    return pd.DataFrame(rows), info


if __name__ == "__main__":
    T, info = run()
    os.makedirs(os.path.join(ROOT, "data_store/research/strong_block"), exist_ok=True)
    T.to_csv(os.path.join(ROOT, "data_store/research/strong_block/touches.csv"), index=False)
    print("days / maps (day, index, map as of, basis, strong, medium):"); [print("  ", x) for x in info]
    days = sorted(T.day.unique()); half = days[len(days) // 2]
    def L(x, col="pts"):
        if not len(x): return "n   0"
        n = x[col] - x.sym.map(COST)
        return (f"n {len(x):3d} ({x.day.nunique()}d) · gross {x[col].mean():+6.1f} pts · net {n.mean():+6.1f} · win {100*(n>0).mean():3.0f}% · "
                f"Rs/lot {(n*x.sym.map(LOT)).sum():+8,.0f} · halves {(n*x.sym.map(LOT))[x.day < half].sum():+7,.0f} / {(n*x.sym.map(LOT))[x.day >= half].sum():+7,.0f}")
    for sym in ("NIFTY", "BANKNIFTY"):
        S = T[T.sym == sym]
        print(f"\n{sym}  (halves split at {half})")
        for lab, m in (("RULE strong + confirm", (S.strength == "strong") & S.confirm), ("strong, no confirm", S.strength == "strong"),
                       ("medium + confirm", (S.strength == "medium") & S.confirm), ("weak + confirm", (S.strength == "weak") & S.confirm),
                       ("all blocks, no confirm", S.index == S.index)):
            print(f"  {lab:24s} card {L(S[m])}")
            print(f"  {'':24s} 30m  {L(S[m], 'p30')}")
    R = T[(T.strength == "strong") & T.confirm]
    print("\nRULE trades:"); print(R[["day", "sym", "time", "zone", "side", "td", "risk", "pts", "rs", "exit"]].to_string(index=False))
    print("\nstrong-block touches: exits", T[T.strength == "strong"].exit.value_counts().to_dict())
