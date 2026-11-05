"""BANK NIFTY - what really happens at level touches (2026-11-06, Nikhil: "Understand the historical data patterns for bank nifty what happens
at reversal and what really happens .. can we make it profitable this strategy .. keep iterating"). Paper research only.
Data: Bank Nifty spot 5-min, 18 Jul 2024 - 17 Jul 2026 (nifty-live-tracker/datasets/2024-07-17_to_2026-07-19), 496 sessions.
Design FIRST year (Jul 2024 - Jun 2025), TEST second year (Jul 2025 - Jul 2026). Everything below fixed before running.
  levels : PDH / PDL / PDC (previous session), IBH / IBL (09:15-10:15, used from 10:15), round 500s
  touch  : a 5-min bar reaches a level approached from the other side (previous close beyond it); first touch per level per day
           + re-touch after 30 min; bars 09:30-14:30. fade side = support -> long, resistance -> short
  what next 30 min (6 bars, from the touch bar close): fav = best move in the fade direction, adv = worst against, end = close-to-close
  context: daymove = (touch close / day open - 1) in the FADE direction (+ = fading with the day's move, - = fading against it),
           gap, time bucket, level type, approach = 30-min move into the level (ATR units, 14-day daily ATR), touch no.
  trade  : fade at the touch bar close; stop 0.10 % past the level (like touch-minute; also 0.20 %); +1R -> breakeven; +2R trail 1R;
           out after 30 min; managed on 5-min closes; cost 12.5 pts (synthetic future); Rs = 30 x pts net.
"""
import glob, os
import numpy as np, pandas as pd
DOC = os.path.expanduser("~/mnt/Documents")
fs = sorted(glob.glob(f"{DOC}/nifty-live-tracker/datasets/2024-07-17_to_2026-07-19/*/BANKNIFTY_spot.csv"))
B = pd.concat([pd.read_csv(f) for f in fs]); B["dt"] = pd.to_datetime(B.datetime); B = B.drop_duplicates("dt").sort_values("dt")
B["day"] = B.dt.dt.strftime("%Y-%m-%d"); B["hm"] = B.dt.dt.strftime("%H:%M")
days = sorted(B.day.unique()); G = {d: g.reset_index(drop=True) for d, g in B.groupby("day")}
D = B.groupby("day").agg(o=("open", "first"), h=("high", "max"), l=("low", "min"), c=("close", "last"))
tr = pd.concat([D.h - D.l, (D.h - D.c.shift()).abs(), (D.l - D.c.shift()).abs()], axis=1).max(axis=1); D["atr"] = tr.rolling(14).mean().shift()
COST, LOT = 12.5, 30
def sim(g, i, sd, e, stop, n=6):
    R = abs(e - stop); sl, best, trail = stop, e, 0
    for k in range(i + 1, min(len(g), i + 1 + n)):
        c = g.close[k]
        if sd * (c - sl) <= 0: return sd * (c - e), ["SL", "BE", "TR"][trail]
        best = max(best, c) if sd > 0 else min(best, c); mv = sd * (best - e)
        if mv >= 2 * R: nt = best - sd * R; sl = max(sl, nt) if sd > 0 else min(sl, nt); trail = 2
        elif mv >= R and trail == 0: sl = e; trail = 1
    k = min(len(g) - 1, i + n); return sd * (g.close[k] - e), "30m"
rows = []
for j in range(15, len(days)):
    d, p = days[j], days[j - 1]; g, gp = G[d], G[p]; atr = D.atr[d]
    if not np.isfinite(atr) or len(g) < 60: continue
    lv = {"PDH": gp.high.max(), "PDL": gp.low.min(), "PDC": gp.close.iloc[-1]}
    ib = g[g.hm < "10:15"]; ibh, ibl = ib.high.max(), ib.low.min()
    lo, hi = g.low.min(), g.high.max(); r = 500
    rounds = {f"R{int(x)}": float(x) for x in np.arange(np.floor(lo / r) * r, hi + r, r)}
    op = g.open[0]; gap = op / gp.close.iloc[-1] - 1; last = {}
    for i in range(1, len(g) - 1):
        b, q = g.iloc[i], g.iloc[i - 1]
        if not ("09:30" <= b.hm <= "14:30"): continue
        L = dict(lv); L.update(rounds)
        if b.hm >= "10:15": L.update({"IBH": ibh, "IBL": ibl})
        for name, x in L.items():
            side = 1 if (q.close > x and b.low <= x) else -1 if (q.close < x and b.high >= x) else 0
            if not side: continue
            if name in last and i - last[name] < 6: continue
            n_touch = sum(1 for _ in [0]) if name not in last else 2
            last[name] = i
            e = b.close
            fut = g.iloc[i + 1:i + 7]
            if len(fut) < 6: continue
            fav = side * ((fut.high.max() if side > 0 else fut.low.min()) - e); adv = side * ((fut.low.min() if side > 0 else fut.high.max()) - e)
            st10 = min(x * 0.999, e * 0.9995) if side > 0 else max(x * 1.001, e * 1.0005)
            st20 = min(x * 0.998, e * 0.9995) if side > 0 else max(x * 1.002, e * 1.0005)
            p10, w10 = sim(g, i, side, e, st10); p20, w20 = sim(g, i, side, e, st20)
            k30 = max(0, i - 6)
            rows.append(dict(day=d, hm=b.hm, level=name[:1] if name.startswith("R") else name, side=side, touch=n_touch,
                             daymove=side * (e / op - 1) * 100, gap=gap * 100, gapdir=np.sign(gap) * side,
                             approach=side * -(e - g.close[k30]) / atr,  # + = price came INTO the level fast (against the fade)
                             rng_atr=(g.high[:i + 1].max() - g.low[:i + 1].min()) / atr,
                             fav=fav / atr, adv=adv / atr, end=side * (fut.close.iloc[-1] - e) / atr,
                             p10=p10, w10=w10, rs10=(p10 - COST) * LOT, p20=p20, w20=w20, rs20=(p20 - COST) * LOT, atr=atr, e=e))
            break
T = pd.DataFrame(rows); T["year"] = np.where(T.day < "2025-07-01", "Y1", "Y2")
os.makedirs("data_store/research/bn_level_study", exist_ok=True); T.to_csv("data_store/research/bn_level_study/touches.csv", index=False)
T["outcome"] = np.where(T.fav >= 0.25 + 0 * T.adv, np.where(T.adv <= -0.25, "both", "reversal"), np.where(T.adv <= -0.25, "break", "chop"))
print("touches", len(T), "days", T.day.nunique(), "Y1", (T.year == "Y1").sum(), "Y2", (T.year == "Y2").sum())
print("\nWHAT HAPPENS in 30 min (move >= 0.25 daily ATR):"); print(pd.crosstab(T.year, T.outcome, normalize="index").round(2))
def tab(col, bins=None, labels=None):
    x = pd.cut(T[col], bins, labels=labels) if bins else T[col]
    t = T.groupby([x, "year"], observed=True).agg(n=("rs10", "size"), end_atr=("end", "mean"), win=("p10", lambda s: (s > COST).mean()), rs_trade=("rs10", "mean"), rs20_trade=("rs20", "mean")).round(3)
    print(f"\n--- by {col} ---"); print(t.unstack("year").to_string())
tab("daymove", [-9, -1.0, -0.5, -0.2, 0.2, 0.5, 1.0, 9], ["<-1% against", "-1..-0.5", "-0.5..-0.2", "flat", "+0.2..0.5", "+0.5..1", ">+1% with"])
tab("gapdir", [-2, -0.5, 0.5, 2], ["fade vs gap", "no gap", "fade with gap"])
tab("hm", ["09:29", "10:00", "11:00", "12:30", "14:31"], ["09:30-10", "10-11", "11-12:30", "12:30-14:30"]) if False else None
T["tb"] = pd.cut(T.hm.str[:2].astype(int) * 60 + T.hm.str[3:].astype(int), [569, 600, 660, 750, 871], labels=["09:30-10", "10-11", "11-12:30", "12:30-14:30"])
tab("tb"); tab("level"); tab("approach", [-9, -0.15, 0.15, 0.3, 9], ["drifted away", "slow", "fast in", "very fast in"]); tab("rng_atr", [0, 0.5, 0.8, 1.1, 9], ["<0.5 ATR", "0.5-0.8", "0.8-1.1", ">1.1 exhausted"])
print("\nALL:", T.groupby("year").agg(n=("rs10", "size"), rs10=("rs10", "mean"), rs20=("rs20", "mean"), win=("p10", lambda s: (s > COST).mean())).round(1).to_string())
