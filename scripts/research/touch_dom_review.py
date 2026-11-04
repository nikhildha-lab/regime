"""Proper review of the Touch + DOM idea (2026-11-04). Reads data_store/research/touch_dom/trades.csv (touch_dom_combo.py).
Questions fixed in advance:
  Q1 does the DOM book lean add information at level touches?  fade result when lean agrees vs is against, ALL touches (more samples)
  Q2 same question inside touches where the touch-minute delta already agrees
  Q3 is it robust?  thresholds 0 / 0.05 / 0.10 / 0.15, both halves of the days, each index, card exits and plain 30-min exit
  Q4 is it luck?  shuffle the lean labels within each day 5,000 times; p = share of shuffles with agree-minus-against >= the real one
Gross in index points first; net = synthetic future (Nifty 4.5, Bank Nifty 12.5 pts). Paper research only."""
import numpy as np, pandas as pd
T = pd.read_csv("data_store/research/touch_dom/trades.csv").dropna(subset=["lean"])
T["sl"] = T.lean * T.side                     # lean in the trade direction (+ = book agrees with the fade)
T["ta"] = T.td * T.side                       # touch-minute delta in the trade direction
COST = {"NIFTY": 4.5, "BANKNIFTY": 12.5}
days = sorted(T.day.unique()); half = days[len(days) // 2]
rng = np.random.default_rng(7)


def line(x, col="pts"):
    if not len(x): return "n   0"
    net = x[col] - x.sym.map(COST)
    return f"n {len(x):3d} ({x.day.nunique()} days) · gross {x[col].mean():+6.1f} · net {net.mean():+6.1f} · win(net) {100*(net>0).mean():3.0f}%"


def shuffle_p(S, thr, col="pts"):
    real = S[S.sl >= thr][col].mean() - S[S.sl <= -thr][col].mean()
    if np.isnan(real): return None, None
    cnt = 0
    for _ in range(5000):
        sh = S.groupby("day").sl.transform(lambda v: rng.permutation(v.values))
        d = S[sh >= thr][col].mean() - S[sh <= -thr][col].mean()
        cnt += d >= real
    return round(real, 1), round(cnt / 5000, 3)


out = [f"Touches with a book reading: {len(T)} on {T.day.nunique()} days ({days[0]} .. {days[-1]}), halves split at {half}"]
for col, lab in (("pts", "card exits (stop / breakeven / trail / 30 min)"), ("p30", "plain 30-min exit, no stop")):
    out.append(f"\n===== {lab} =====")
    for sym in ("NIFTY", "BANKNIFTY", None):
        S = T if sym is None else T[T.sym == sym]
        out.append(f"\n-- {sym or 'BOTH'} --  all touches: {line(S, col)}")
        for thr in (0.0, 0.05, 0.10, 0.15):
            ag, againt = S[S.sl >= thr if thr else S.sl > 0], S[S.sl <= -thr if thr else S.sl < 0]
            diff, p = shuffle_p(S, thr if thr else 1e-9, col)
            out.append(f"  lean {thr:.2f}  AGREE   {line(ag, col)}")
            out.append(f"             AGAINST {line(againt, col)}   agree-minus-against {diff} pts, luck p {p}")
            for h, m in (("1st half", S.day < half), ("2nd half", S.day >= half)):
                a2, b2 = ag[ag.day.isin(S[m].day)], againt[againt.day.isin(S[m].day)]
                out.append(f"     {h}: agree {a2[col].mean():+6.1f} (n {len(a2)}) vs against {b2[col].mean():+6.1f} (n {len(b2)})")
        TD = S[S.ta > 0.2]
        out.append(f"  inside touch-delta-agree (n {len(TD)}): lean>=0.10 {line(TD[TD.sl >= 0.10], col)} | lean<=-0.10 {line(TD[TD.sl <= -0.10], col)} | rest {line(TD[TD.sl.abs() < 0.10], col)}")
        out.append(f"  touch delta alone: agree>0.2 {line(S[S.ta > 0.2], col)} | against<-0.2 {line(S[S.ta < -0.2], col)}")
print("\n".join(out))
