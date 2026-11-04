"""4H break - targets AND trailing stop together (2026-11-05, Nikhil: "SOL with targets or trailing ... experiment if we have both").
Same trades / data / cost as b4h_exit_v4.py (coins: research BREAK trades, v3 conditions, 5-min path; Gold: rebuilt with the live code,
1h path). Stop checked first in every bar. Variants fixed BEFORE running:
  TRAIL     (coins now)  block stop; once a 4H close is +1R, trail 1.5 ATR behind the best 4H close; out after 2 days
  TARGETS   (BTC now)    thirds at 1R / 2R / 3R; stop -> breakeven after T1, -> T1 after T2; rest out after 2 days
  H1  thirds at 1R and 2R; stop -> breakeven after T1, -> T1 after T2; the LAST THIRD trails 1.5 ATR (never below T1); 2 days
  H2  HALF at 1R, stop -> breakeven; the other half trails 1.5 ATR (never below entry); 2 days
  H3  HALF at 2R; the other half trails 1.5 ATR from +1R (the current trail); the whole stop never below entry after +1R; 2 days
Gross % per trade first, net = minus 0.14 %."""
import os, sys, pickle
import numpy as np, pandas as pd
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")); sys.path.insert(0, os.path.join(ROOT, "app"))
OUT = os.path.join(ROOT, "data_store/research/blocks_xmkt/")
COST = 0.14
PLANS = {"TARGETS": ([(1, 1 / 3), (2, 1 / 3), (3, 1 / 3)], None), "H1": ([(1, 1 / 3), (2, 1 / 3)], "t1"),
         "H2": ([(1, 0.5)], "be"), "H3": ([(2, 0.5)], "be_after_1R")}


def sim(h, l, c, s, e, st, a, ver, bpb, n):
    R = abs(e - st); n = min(n, len(c)); cur, best, on, left, pnl = st, e, False, 1.0, 0.0
    if ver == "TRAIL":
        tg, floor = [], None
    else:
        tg, floor = PLANS[ver]
    hit = 0
    for k in range(n):
        if (s == 1 and l[k] <= cur) or (s == -1 and h[k] >= cur):
            return pnl + left * s * (cur - e)
        while hit < len(tg) and ((s == 1 and h[k] >= e + s * tg[hit][0] * R) or (s == -1 and l[k] <= e + s * tg[hit][0] * R)):
            px = e + s * tg[hit][0] * R; pnl += tg[hit][1] * s * (px - e); left -= tg[hit][1]; hit += 1
            if ver == "TARGETS" or ver == "H1":
                cur = e if hit == 1 else e + s * tg[hit - 2][0] * R
            elif ver == "H2":
                cur = e if s * (e - cur) > 0 else cur
        if left <= 1e-9:
            return pnl
        trailing = ver in ("TRAIL", "H3") or (ver == "H1" and hit >= 2) or (ver == "H2" and hit >= 1)
        if (k + 1) % bpb == 0:
            best = max(best, c[k]) if s == 1 else min(best, c[k])
            if ver in ("TRAIL", "H3") and s * (best - e) >= R: on = True
            if ver in ("H1", "H2") and trailing: on = True
            if on and trailing:
                nt = best - s * 1.5 * a
                cur = max(cur, nt) if s == 1 else min(cur, nt)
            if ver == "H3" and on:
                cur = max(cur, e) if s == 1 else min(cur, e)
            if ver == "H1" and hit >= 2:
                t1 = e + s * R; cur = max(cur, t1) if s == 1 else min(cur, t1)
    return pnl + left * s * (c[n - 1] - e)


VERS = ("TRAIL", "TARGETS", "H1", "H2", "H3")
rows = []
btc_c = pickle.load(open(OUT + "exitstudy_BTC.pkl", "rb"))["base"].close.resample("4h").last().dropna()
for SYM in ("ETH", "SOL", "BTC"):
    d = pickle.load(open(OUT + f"exitstudy_{SYM}.pkl", "rb")); E, B = d["E"], d["base"]
    b4 = B.resample("4h").agg({"high": "max", "low": "min", "close": "last"}).dropna()
    tr = np.maximum(b4.high - b4.low, np.maximum((b4.high - b4.close.shift()).abs(), (b4.low - b4.close.shift()).abs()))
    atrp = tr.ewm(alpha=1 / 14, adjust=False).mean() / b4.close
    rank = atrp.rolling(1080, min_periods=540).apply(lambda x: (x[:-1] < x[-1]).mean(), raw=True)
    T = B.index.values; H, L, C = B.high.values, B.low.values, B.close.values
    for _, r in E[E.def_delta < -0.05].iterrows():
        i0 = np.searchsorted(T, np.datetime64(r.t_entry))
        if i0 + 576 > len(T): continue
        rk = rank[rank.index < r.t_entry]
        if not len(rk) or np.isnan(rk.iloc[-1]) or rk.iloc[-1] > 2 / 3: continue
        bb = btc_c[btc_c.index <= r.t_entry]; b24 = bb.iloc[-1] / bb.iloc[-7] - 1
        s, e, a = int(r.side), float(r.e), float(r.atr)
        if b24 * s <= 0: continue
        st = float(r.stop0) - (s * 0.5 * a if SYM == "BTC" else 0.0)
        if s * (e - st) <= 0: continue
        half = "2023-24" if r.day < pd.Timestamp("2025-01-01") else "2025-26"
        for v in VERS:
            g = sim(H[i0:i0 + 576], L[i0:i0 + 576], C[i0:i0 + 576], s, e, st, a, v, 48, 576) / e * 100
            rows.append(dict(mkt=SYM, half=half, ver=v, g=g))
# gold - reuse the rebuilt trades from b4h_exit_v4 logic
import break4h as BK
g = pd.read_csv(os.path.join(ROOT, "data_store/market_data/synaptic_crypto_gold.csv"))
g = g[(g.symbol == "GC=F") & (g.interval == "1h")]; g["t"] = pd.to_datetime(g.datetime_utc)
g = g.set_index("t").sort_index()[["open", "high", "low", "close", "volume"]].astype(float)
b4 = g.resample("4h").agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}).dropna(subset=["close"])
cnt = g.close.resample("4h").count().reindex(b4.index); b4 = b4[cnt >= 3]
d1 = g.resample("1D").agg({"high": "max", "low": "min", "close": "last"}).dropna()
tr = np.maximum(b4.high - b4.low, np.maximum((b4.high - b4.close.shift()).abs(), (b4.low - b4.close.shift()).abs()))
atr = tr.ewm(alpha=1 / 14, adjust=False).mean()
Hh, L, Cc = b4.high.values, b4.low.values, b4.close.values
maps, busy, gt = {}, None, []
for j in range(4, len(b4)):
    t = b4.index[j]
    if busy is not None and t < busy: continue
    day = t.normalize()
    if day not in maps: maps[day] = BK.build_map("GOLD", b4, d1, day)
    for b in maps[day]:
        side = 1 if Cc[j - 1] > b["hi"] and L[j] <= b["hi"] else -1 if Cc[j - 1] < b["lo"] and Hh[j] >= b["lo"] else 0
        if not side: continue
        if any((Cc[k - 1] > b["hi"] and L[k] <= b["hi"]) or (Cc[k - 1] < b["lo"] and Hh[k] >= b["lo"]) for k in range(max(1, j - 3), j)): continue
        far, near = (b["lo"], b["hi"]) if side > 0 else (b["hi"], b["lo"])
        if side * ((L[j] if side > 0 else Hh[j]) - far) >= 0: continue
        e = Cc[j]; stop = near + side * 0.1 * BK.BAND * e
        if side * (stop - e) <= 0: continue
        t_in = t + pd.Timedelta(hours=4); P = g[(g.index >= t_in) & (g.index < t_in + pd.Timedelta(hours=48))]
        if len(P) < 30: break
        gt.append(t)
        for v in VERS:
            gg = sim(P.high.values, P.low.values, P.close.values, -side, e, stop, float(atr.iloc[j]), v, 4, len(P)) / e * 100
            rows.append(dict(mkt="GOLD", half=None, ver=v, g=gg, t=t))
        busy = t_in + pd.Timedelta(hours=48); break
D = pd.DataFrame(rows)
if gt:
    mid = sorted(gt)[len(gt) // 2]; m = D.mkt == "GOLD"; D.loc[m, "half"] = np.where(D.loc[m, "t"] < mid, "1st half", "2nd half")
def stt(z):
    net = z.g - COST; eq = net.cumsum(); dd = (eq - eq.cummax()).min()
    h = z.groupby("half").g.apply(lambda x: round((x - COST).mean(), 2)).to_dict()
    return pd.Series(dict(n=len(z), win=round((net > 0).mean() * 100), gross=round(z.g.mean(), 2), net=round(net.mean(), 2),
                          halves=" / ".join(f"{v:+.2f}" for v in h.values()), worst_dd=round(dd, 1), total=round(net.sum(), 1)))
pd.set_option("display.width", 220)
print(D.groupby(["mkt", "ver"], sort=False).apply(stt, include_groups=False).to_string())
os.makedirs(os.path.join(ROOT, "data_store/research/b4h_hybrid"), exist_ok=True); D.to_csv(os.path.join(ROOT, "data_store/research/b4h_hybrid/trades.csv"), index=False)
