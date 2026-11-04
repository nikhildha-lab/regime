"""4H block break on metals and commodities (2026-11-05, Nikhil: "can we start testing all metals and commodities").
Futures 1h bars (Yahoo: GC=F gold, SI=F silver, HG=F copper, ALI=F aluminium, CL=F WTI crude, NG=F natural gas; ~May 2024 ->).
Signal = the LIVE Gold v1 rule (app/break4h.build_map + the live signal code): a 4H bar enters a block from outside and trades beyond
its far edge; no taker-volume data for futures, so no delta filter; one open trade per market. Entry at that bar's close, stop back
past the near edge (+0.1 band). 1h path after entry, stop checked first. Exits compared (fixed before running):
  FIXED 2d (gold v1) · TARGETS 1/2/3R thirds · TRAIL 1.5 ATR from +1R · H2 half at 1R + trail · H3 half at 2R + trail.
Gross % per trade, net = minus 0.14 % (same cost as crypto; real futures costs are usually lower). Halves = first / second half of trades."""
import os, sys
import numpy as np, pandas as pd
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")); sys.path[:0] = [os.path.join(ROOT, "app"), os.path.dirname(__file__)]
import break4h as BK
from b4h_sim import sim
COST = 0.14
NAMES = {"GC=F": "Gold", "SI=F": "Silver", "HG=F": "Copper", "ALI=F": "Aluminium", "CL=F": "WTI crude", "NG=F": "Natural gas"}


def h1(sym):
    f = "synaptic_crypto_gold.csv" if sym == "GC=F" else "synaptic_intraday_all.csv"
    g = pd.read_csv(os.path.join(ROOT, "data_store/market_data", f))
    g = g[(g.symbol == sym) & (g.interval == "1h")]; g["t"] = pd.to_datetime(g.datetime_utc)
    return g.set_index("t").sort_index()[["open", "high", "low", "close", "volume"]].astype(float)


def fixed(h, l, c, s, e, st):
    for k in range(len(c)):
        if (s == 1 and l[k] <= st) or (s == -1 and h[k] >= st): return s * (st - e)
    return s * (c[-1] - e)


rows = []
for sym in NAMES:
    g = h1(sym)
    b4 = g.resample("4h").agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}).dropna(subset=["close"])
    cnt = g.close.resample("4h").count().reindex(b4.index); b4 = b4[cnt >= 3]
    d1 = g.resample("1D").agg({"high": "max", "low": "min", "close": "last"}).dropna()
    tr = np.maximum(b4.high - b4.low, np.maximum((b4.high - b4.close.shift()).abs(), (b4.low - b4.close.shift()).abs()))
    atr = tr.ewm(alpha=1 / 14, adjust=False).mean()
    Hh, L, Cc = b4.high.values, b4.low.values, b4.close.values
    maps, busy = {}, None
    for j in range(4, len(b4)):
        t = b4.index[j]
        if busy is not None and t < busy: continue
        day = t.normalize()
        if day not in maps: maps[day] = BK.build_map(sym, b4, d1, day)
        for b in maps[day]:
            side = 1 if Cc[j - 1] > b["hi"] and L[j] <= b["hi"] else -1 if Cc[j - 1] < b["lo"] and Hh[j] >= b["lo"] else 0
            if not side: continue
            if any((Cc[k - 1] > b["hi"] and L[k] <= b["hi"]) or (Cc[k - 1] < b["lo"] and Hh[k] >= b["lo"]) for k in range(max(1, j - 3), j)): continue
            far, near = (b["lo"], b["hi"]) if side > 0 else (b["hi"], b["lo"])
            if side * ((L[j] if side > 0 else Hh[j]) - far) >= 0: continue
            e = Cc[j]; stop = near + side * 0.1 * BK.BAND * e
            if side * (stop - e) <= 0: continue
            s = -side; t_in = t + pd.Timedelta(hours=4)
            P = g[(g.index >= t_in) & (g.index < t_in + pd.Timedelta(hours=48))]
            if len(P) < 20: break
            hh, ll, cc = P.high.values, P.low.values, P.close.values; a = float(atr.iloc[j])
            res = {"FIXED 2d": fixed(hh, ll, cc, s, e, stop)}
            for v in ("TARGETS", "TRAIL", "H2", "H3"):
                res[v] = sim(hh, ll, cc, s, e, stop, a, v, 4, len(P))
            for v, x in res.items():
                rows.append(dict(sym=sym, t=t, ver=v, g=x / e * 100, risk=abs(e - stop) / e * 100, strength=b["strength"]))
            busy = t_in + pd.Timedelta(hours=48); break
D = pd.DataFrame(rows)
def stt(z):
    z = z.sort_values("t"); net = z.g - COST; mid = len(z) // 2; eq = net.cumsum(); dd = (eq - eq.cummax()).min()
    return pd.Series(dict(n=len(z), win=round((net > 0).mean() * 100), gross=round(z.g.mean(), 2), net=round(net.mean(), 2),
                          halves=f"{net.iloc[:mid].mean():+.2f} / {net.iloc[mid:].mean():+.2f}", worst_dd=round(dd, 1), total=round(net.sum(), 1)))
pd.set_option("display.width", 220)
print("period", D.t.min(), "->", D.t.max())
T = D.groupby(["sym", "ver"], sort=False).apply(stt, include_groups=False)
T.index = T.index.set_levels([[NAMES.get(x, x) for x in T.index.levels[0]], T.index.levels[1]])
print(T.to_string())
os.makedirs(os.path.join(ROOT, "data_store/research/b4h_commod"), exist_ok=True); D.to_csv(os.path.join(ROOT, "data_store/research/b4h_commod/trades.csv"), index=False)
