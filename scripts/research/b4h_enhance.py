"""4H break - can BTC / ETH get better? (2026-11-05, Nikhil: "enhance the logic for BTC and ETH wherever possible").
Same v3 trades as b4h_exit_v4 / b4h_hybrid (research BREAK trades, BTC 24 h trend agrees, ATR not wild, 5-min path, stop first, 2 days).
Base = the live rule: ETH trail 1.5 ATR; BTC 3 targets (and the H2 record). Ideas FIXED BEFORE RUNNING (one at a time on top of base):
  delta10 / delta15 : taker delta in the break direction >= 10 % / 15 % (now 5 %)
  strong / weak block: block score in the top / bottom third (per coin)
  weekday only       : skip Saturday / Sunday UTC entries
  calm only          : ATR rank in the bottom third (skip 'normal')
  trend1             : BTC 24 h move >= 1 % in the trade direction (now > 0)
  hour buckets       : entry 4H bar hour UTC (report only - not a filter candidate unless both halves agree by a wide margin)
  exits ETH          : trail 1.0 / 2.0 / 2.5 / 3.0 ATR; hold 3 days
  exits BTC          : stop room 0 / 0.25 / 1.0 ATR (now 0.5); H2; hold 3 days
A change is worth adding only if it beats base in BOTH halves after costs and keeps >= 50 % of the trades."""
import os, sys, pickle
import numpy as np, pandas as pd
sys.path.insert(0, os.path.dirname(__file__))
from b4h_sim import sim
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
OUT = os.path.join(ROOT, "data_store/research/blocks_xmkt/"); COST = 0.14
btc_c = pickle.load(open(OUT + "exitstudy_BTC.pkl", "rb"))["base"].close.resample("4h").last().dropna()
rows = []
for SYM in ("ETH", "BTC"):
    d = pickle.load(open(OUT + f"exitstudy_{SYM}.pkl", "rb")); E, B = d["E"], d["base"]
    b4 = B.resample("4h").agg({"high": "max", "low": "min", "close": "last"}).dropna()
    tr = np.maximum(b4.high - b4.low, np.maximum((b4.high - b4.close.shift()).abs(), (b4.low - b4.close.shift()).abs()))
    atrp = tr.ewm(alpha=1 / 14, adjust=False).mean() / b4.close
    rank = atrp.rolling(1080, min_periods=540).apply(lambda x: (x[:-1] < x[-1]).mean(), raw=True)
    T = B.index.values; H, L, C = B.high.values, B.low.values, B.close.values
    for _, r in E[E.def_delta < -0.05].iterrows():
        i0 = np.searchsorted(T, np.datetime64(r.t_entry))
        if i0 + 864 > len(T): continue
        rk = rank[rank.index < r.t_entry]
        if not len(rk) or np.isnan(rk.iloc[-1]) or rk.iloc[-1] > 2 / 3: continue
        bb = btc_c[btc_c.index <= r.t_entry]; b24 = bb.iloc[-1] / bb.iloc[-7] - 1
        s, e, a = int(r.side), float(r.e), float(r.atr)
        if b24 * s <= 0: continue
        h, l, c = H[i0:i0 + 864], L[i0:i0 + 864], C[i0:i0 + 864]
        base_st = float(r.stop0) - (s * 0.5 * a if SYM == "BTC" else 0.0)
        res = {}
        if SYM == "ETH":
            res["base"] = sim(h, l, c, s, e, base_st, a, "TRAIL", 48, 576)
        else:
            res["base"] = sim(h, l, c, s, e, base_st, a, "TARGETS", 48, 576)
            res["H2"] = sim(h, l, c, s, e, base_st, a, "H2", 48, 576)
            res["hold 3d"] = sim(h, l, c, s, e, base_st, a, "TARGETS", 48, 864)
            for rm in (0.0, 0.25, 1.0):
                st2 = float(r.stop0) - s * rm * a
                if s * (e - st2) > 0: res[f"room {rm}"] = sim(h, l, c, s, e, st2, a, "TARGETS", 48, 576)
        if SYM == "ETH":
            res["hold 3d"] = sim(h, l, c, s, e, base_st, a, "TRAIL", 48, 864)
        rows.append(dict(sym=SYM, t=pd.Timestamp(r.t_entry), half="2023-24" if r.day < pd.Timestamp("2025-01-01") else "2025-26",
                         delta=-float(r.def_delta), score=float(r.strength_score), rank=float(rk.iloc[-1]), b24=b24 * s,
                         wkd=pd.Timestamp(r.t_entry).weekday() >= 5, hour=(pd.Timestamp(r.t_entry) - pd.Timedelta(hours=4)).hour,
                         e=e, a=a, s=s, stop=base_st, i0=i0, **{f"x_{k}": (v / e * 100 if v is not None else np.nan) for k, v in res.items()}))
D = pd.DataFrame(rows)
# ETH trail sizes need a variable multiplier: re-simulate with a local copy of the trail
def trail_k(h, l, c, s, e, st, a, k, n):
    R = abs(e - st); cur, best, on = st, e, False
    for j in range(n):
        if (s == 1 and l[j] <= cur) or (s == -1 and h[j] >= cur): return s * (cur - e)
        if (j + 1) % 48 == 0:
            best = max(best, c[j]) if s == 1 else min(best, c[j])
            if s * (best - e) >= R: on = True
            if on: nt = best - s * k * a; cur = max(cur, nt) if s == 1 else min(cur, nt)
    return s * (c[n - 1] - e)
dE = pickle.load(open(OUT + "exitstudy_ETH.pkl", "rb"))["base"]; HE, LE, CE = dE.high.values, dE.low.values, dE.close.values
for k in (1.0, 2.0, 2.5, 3.0):
    D.loc[D.sym == "ETH", f"x_trail {k}"] = [trail_k(HE[r.i0:r.i0 + 576], LE[r.i0:r.i0 + 576], CE[r.i0:r.i0 + 576], r.s, r.e, r.stop, r.a, k, 576) / r.e * 100
                                            for r in D[D.sym == "ETH"].itertuples()]
D.to_csv(os.path.join(ROOT, "data_store/research/b4h_enhance_trades.csv"), index=False)
def line(z, col="x_base", base=None):
    n = z[col] - COST; h = z.groupby("half")[col].apply(lambda x: (x - COST).mean())
    return f"n {len(z):3d} · net {n.mean():+5.2f}% · halves {h.get('2023-24', np.nan):+5.2f} / {h.get('2025-26', np.nan):+5.2f} · win {100*(n>0).mean():3.0f}% · total {n.sum():+6.1f}%"
for sym in ("ETH", "BTC"):
    S = D[D.sym == sym]
    qs = S.score.quantile([1 / 3, 2 / 3]).values
    print(f"\n===== {sym} =====\n  BASE (live)            {line(S)}")
    for lab, m in (("delta >= 10%", S.delta >= 0.10), ("delta >= 15%", S.delta >= 0.15), ("strong block (top 1/3)", S.score >= qs[1]),
                   ("weak block (bottom 1/3)", S.score <= qs[0]), ("weekday only", ~S.wkd), ("calm ATR only", S["rank"] <= 1 / 3),
                   ("BTC 24h >= 1% with trade", S.b24 >= 0.01)):
        print(f"  filter {lab:24s} {line(S[m])}")
    for col in [c for c in S.columns if c.startswith("x_") and c != "x_base"]:
        print(f"  exit   {col[2:]:24s} {line(S, col)}")
    print("  by entry hour (UTC):", {h: round((z.x_base - COST).mean(), 2) for h, z in S.groupby("hour")}, {h: len(z) for h, z in S.groupby("hour")})


# ---- combined candidates (written after the one-at-a-time table; only changes that passed: both halves better, >= 50 % of trades)
#   ETH v4: weekday only + BTC 24 h >= 1 % with the trade + hold 3 days (trail 1.5 ATR unchanged)
#   BTC v4: BTC 24 h >= 1 % with the trade + stop room 1.0 ATR + H2 exit (half at 1R, rest trails)
print("\n===== combined v4 candidates =====")
E2 = D[D.sym == "ETH"]
m = (~E2.wkd) & (E2.b24 >= 0.01)
print("  ETH v4 (weekday + trend 1% + hold 3d)", line(E2[m], "x_hold 3d"), "| base", line(E2))
B2 = D[D.sym == "BTC"].copy()
dB = pickle.load(open(OUT + "exitstudy_BTC.pkl", "rb"))["base"]; HB, LB, CB = dB.high.values, dB.low.values, dB.close.values
Eb = pickle.load(open(OUT + "exitstudy_BTC.pkl", "rb"))["E"].set_index("t_entry")
v = []
for r in B2.itertuples():
    st2 = float(Eb.loc[r.t, "stop0"] if not isinstance(Eb.loc[r.t, "stop0"], pd.Series) else Eb.loc[r.t, "stop0"].iloc[0]) - r.s * 1.0 * r.a
    v.append(sim(HB[r.i0:r.i0 + 576], LB[r.i0:r.i0 + 576], CB[r.i0:r.i0 + 576], r.s, r.e, st2, r.a, "H2", 48, 576) / r.e * 100)
B2["x_v4"] = v
m = B2.b24 >= 0.01
print("  BTC v4 (trend 1% + room 1.0 + H2)     ", line(B2[m], "x_v4"), "| base", line(B2))
print("  BTC room 1.0 + H2 (no trend filter)   ", line(B2, "x_v4"))
