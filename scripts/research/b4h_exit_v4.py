"""4H break - profit-booking fixes (2026-11-05, Nikhil: "I need rules for trailing logic else targets ... how will I book profits").
Variants fixed BEFORE running; the live rules are not changed (record-only versions if approved).
  Coins (ETH, SOL; v3 conditions: BTC 24 h trend agrees, ATR not wild; research BREAK trades, 5-min path, stop checked first):
    NOW        block stop, trail 1.5 x ATR behind the best 4H close once a 4H close is >= +1R, out after 2 days
    BE+TRAIL   same, plus: once +1R the stop is never below entry (breakeven floor), trail only raises it
    TARGETS    3 targets 1R / 2R / 3R (thirds), stop to breakeven after T1, to T1 after T2, rest out at 2 days   (BTC's rule)
  Gold (v1 signal, rebuilt with app/break4h.py build_map + the live signal code on GC=F 1h bars; 1h path, stop checked first):
    NOW        block stop, out after 2 days (12 x 4H)
    TARGETS    as above (thirds at 1R / 2R / 3R, breakeven after T1, T1 after T2)
    BE+TRAIL   as coins BE+TRAIL
Gross % per trade first, net = minus 0.14 % round trip. Halves: crypto 2023-24 / 2025-26; gold first / second half of its trades.
"""
import os, sys, pickle
import numpy as np, pandas as pd
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")); sys.path.insert(0, os.path.join(ROOT, "app"))
OUT = os.path.join(ROOT, "data_store/research/blocks_xmkt/")
COST = 0.14


def sim(h, l, c, s, e, st, a, mode, bar_per_4h, n):
    """h/l/c = path arrays after entry; returns gross price move per unit (pnl in price)."""
    R = abs(e - st); tg = [e + s * k * R for k in (1, 2, 3)] if mode == "targets" else []
    left, pnl, cur, best, on, hit = 1.0, 0.0, st, e, False, 0
    n = min(n, len(c))
    for k in range(n):
        if (s == 1 and l[k] <= cur) or (s == -1 and h[k] >= cur):
            return pnl + left * s * (cur - e)
        while hit < len(tg) and ((s == 1 and h[k] >= tg[hit]) or (s == -1 and l[k] <= tg[hit])):
            pnl += (1 / 3) * s * (tg[hit] - e); left -= 1 / 3; hit += 1; cur = e if hit == 1 else tg[hit - 2]
        if left <= 1e-9:
            return pnl
        if mode in ("trail", "betrail") and (k + 1) % bar_per_4h == 0:
            best = max(best, c[k]) if s == 1 else min(best, c[k])
            if s * (best - e) >= R:
                on = True
            if on:
                nt = best - s * 1.5 * a
                cur = max(cur, nt) if s == 1 else min(cur, nt)
                if mode == "betrail":
                    cur = max(cur, e) if s == 1 else min(cur, e)
    return pnl + left * s * (c[n - 1] - e)


rows = []
# ---------- coins
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
        st = float(r.stop0) - (s * 0.5 * a if SYM == "BTC" else 0.0)     # BTC v3: block stop + 0.5 ATR room
        if s * (e - st) <= 0: continue
        h, l, c = H[i0:i0 + 576], L[i0:i0 + 576], C[i0:i0 + 576]
        half = "2023-24" if r.day < pd.Timestamp("2025-01-01") else "2025-26"
        vers = (("NOW targets", "targets"), ("trail 1.5 ATR", "trail")) if SYM == "BTC" else (("NOW trail", "trail"), ("BE+TRAIL", "betrail"), ("TARGETS", "targets"))
        for nm, mode in vers:
            g = sim(h, l, c, s, e, st, a, mode, 48, 576) / e * 100
            rows.append(dict(mkt=SYM, half=half, ver=nm, g=g, risk=abs(e - st) / e * 100, day=r.day))
# ---------- gold (live signal code on history)
import break4h as BK
g = pd.read_csv(os.path.join(ROOT, "data_store/market_data/synaptic_crypto_gold.csv"))
g = g[(g.symbol == "GC=F") & (g.interval == "1h")]; g["t"] = pd.to_datetime(g.datetime_utc)
g = g.set_index("t").sort_index()[["open", "high", "low", "close", "volume"]].astype(float)
b4 = g.resample("4h").agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}).dropna(subset=["close"])
cnt = g.close.resample("4h").count().reindex(b4.index); b4 = b4[cnt >= 3]
d1 = g.resample("1D").agg({"high": "max", "low": "min", "close": "last"}).dropna()
tr = np.maximum(b4.high - b4.low, np.maximum((b4.high - b4.close.shift()).abs(), (b4.low - b4.close.shift()).abs()))
atr = tr.ewm(alpha=1 / 14, adjust=False).mean()
O, Hh, L, Cc = b4.open.values, b4.high.values, b4.low.values, b4.close.values
maps = {}; ngold = 0; busy_until = None
for j in range(4, len(b4)):
    t = b4.index[j]
    if busy_until is not None and t < busy_until: continue          # one open trade at a time (as live)
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
        s = -side; t_in = t + pd.Timedelta(hours=4)
        P = g[(g.index >= t_in) & (g.index < t_in + pd.Timedelta(hours=48))]
        if len(P) < 30: break
        for nm, mode in (("NOW fixed 2d", "fixed"), ("TARGETS", "targets"), ("BE+TRAIL", "betrail")):
            gg = sim(P.high.values, P.low.values, P.close.values, s, e, stop, float(atr.iloc[j]), mode, 4, len(P)) / e * 100
            rows.append(dict(mkt="GOLD", half=None, ver=nm, g=gg, risk=abs(e - stop) / e * 100, day=t))
        ngold += 1; busy_until = t_in + pd.Timedelta(hours=48)
        break
D = pd.DataFrame(rows)
gd = D[D.mkt == "GOLD"].day.sort_values().unique()
if len(gd): D.loc[D.mkt == "GOLD", "half"] = np.where(D[D.mkt == "GOLD"].day < gd[len(gd) // 2], "1st half", "2nd half")
def stt(z):
    net = z.g - COST; eq = net.cumsum(); dd = (eq - eq.cummax()).min()
    return pd.Series(dict(n=len(z), win=(net > 0).mean() * 100, gross=z.g.mean(), net=net.mean(), net_R=(net / z.risk).mean(), worst_dd=dd, total_net=net.sum()))
pd.set_option("display.width", 220)
print("gold trades rebuilt:", ngold, gd[0] if len(gd) else "", "->", gd[-1] if len(gd) else "")
print(D.groupby(["mkt", "ver", "half"]).apply(stt, include_groups=False).round(2).to_string())
print("\nALL YEARS"); print(D.groupby(["mkt", "ver"]).apply(stt, include_groups=False).round(2).to_string())
os.makedirs(os.path.join(ROOT, "data_store/research/b4h_exit_v4"), exist_ok=True); D.to_csv(os.path.join(ROOT, "data_store/research/b4h_exit_v4/trades.csv"), index=False)
