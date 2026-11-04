"""4H break v3 on the forward-test coins - 90-day check (2026-11-05, Nikhil: "run backtest again for all possible recorded trades").
Uses the LIVE code's map (app/break4h.build_map) and the live signal rule on Binance futures 1-min data (data_store/research/alts1m,
~90 days): 4H bar enters a block from outside and trades beyond its far edge, taker volume with the break (net >= 5 %), BTC 24 h
trend agrees, ATR not 'wild' (rank vs the bars available - only ~90 days, so the 180-day rank is approximated). Exit = the coins'
live exit: block stop, trail 1.5 x ATR behind the best 4H close once +1R, out after 2 days; 5-min path, stop checked first.
Gross % per trade, net = minus 0.14 %. 90 days is short: a sanity check, not a test."""
import os, sys
import numpy as np, pandas as pd
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")); sys.path[:0] = [os.path.join(ROOT, "app"), os.path.dirname(__file__)]
import break4h as BK
from b4h_exit_v4 import sim
A1 = os.path.join(ROOT, "data_store/research/alts1m")


def load(sym):
    d = pd.read_csv(os.path.join(A1, f"{sym}USDT_1m.csv.gz")); d["t"] = pd.to_datetime(d.t, unit="ms"); d = d.set_index("t").sort_index()
    d = d.rename(columns={"o": "open", "h": "high", "l": "low", "c": "close", "v": "volume"})
    return d


btc = load("BTC").close.resample("4h").last().dropna()
rows = []
for sym in [x for x in BK.ALTS if os.path.exists(os.path.join(A1, f"{x}USDT_1m.csv.gz"))]:
    m1 = load(sym)
    b4 = m1.resample("4h").agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum", "tbv": "sum"}).dropna()
    b4["delta"] = (2 * b4.tbv - b4.volume) / b4.volume.replace(0, np.nan)
    d1 = m1.resample("1D").agg({"high": "max", "low": "min", "close": "last"}).dropna()
    m5 = m1.resample("5min").agg({"high": "max", "low": "min", "close": "last"}).dropna()
    tr = np.maximum(b4.high - b4.low, np.maximum((b4.high - b4.close.shift()).abs(), (b4.low - b4.close.shift()).abs()))
    atr = tr.ewm(alpha=1 / 14, adjust=False).mean(); atrp = atr / b4.close
    O, H, L, C = b4.open.values, b4.high.values, b4.low.values, b4.close.values
    maps, busy = {}, None
    for j in range(4, len(b4) - 1):
        t = b4.index[j]
        if busy is not None and t < busy: continue
        day = t.normalize()
        if day not in maps: maps[day] = BK.build_map(sym, b4, d1, day)
        for b in maps[day]:
            side = 1 if C[j - 1] > b["hi"] and L[j] <= b["hi"] else -1 if C[j - 1] < b["lo"] and H[j] >= b["lo"] else 0
            if not side: continue
            if any((C[k - 1] > b["hi"] and L[k] <= b["hi"]) or (C[k - 1] < b["lo"] and H[k] >= b["lo"]) for k in range(max(1, j - 3), j)): continue
            far, near = (b["lo"], b["hi"]) if side > 0 else (b["hi"], b["lo"])
            if side * ((L[j] if side > 0 else H[j]) - far) >= 0: continue
            dl = b4.delta.iloc[j]
            if not (pd.notna(dl) and dl * side < -0.05): continue
            e = C[j]; stop = near + side * 0.1 * BK.BAND * e; s = -side
            if s * (e - stop) <= 0: continue
            bb = btc[btc.index <= t]
            if len(bb) < 7 or (bb.iloc[-1] / bb.iloc[-7] - 1) * s <= 0:
                rows.append(dict(sym=sym, t=t, kept=False, why="against BTC trend", g=np.nan)); break
            hist = atrp.iloc[max(0, j - 1079):j]
            rk = (hist < atrp.iloc[j]).mean() if len(hist) >= 120 else np.nan
            t_in = t + pd.Timedelta(hours=4); P = m5[(m5.index >= t_in) & (m5.index < t_in + pd.Timedelta(hours=48))]
            if len(P) < 400: break
            g = sim(P.high.values, P.low.values, P.close.values, s, e, stop, float(atr.iloc[j]), "trail", 48, len(P)) / e * 100
            wild = not np.isnan(rk) and rk > 2 / 3
            rows.append(dict(sym=sym, t=t, kept=not wild, why="wild ATR" if wild else "", g=g, risk=abs(e - stop) / e * 100))
            if not wild: busy = t_in + pd.Timedelta(hours=48)
            break
D = pd.DataFrame(rows)
K = D[D.kept]
print("period", K.t.min(), "->", K.t.max())
for sym, z in K.groupby("sym"):
    n = z.g - 0.14
    print(f"  {sym:6s} n {len(z):3d} · win {100*(n>0).mean():3.0f}% · gross {z.g.mean():+5.2f}% · net {n.mean():+5.2f}% · total net {n.sum():+6.1f}%")
n = K.g - 0.14
print(f"  ALL    n {len(K):3d} · win {100*(n>0).mean():3.0f}% · gross {K.g.mean():+5.2f}% · net {n.mean():+5.2f}% · total {n.sum():+6.1f}%")
S = D[~D.kept & D.g.notna()]
if len(S): print(f"  skipped (wild ATR) n {len(S)} · net {(S.g - 0.14).mean():+5.2f}%")
os.makedirs(os.path.join(ROOT, "data_store/research/b4h_alts"), exist_ok=True); D.to_csv(os.path.join(ROOT, "data_store/research/b4h_alts/trades.csv"), index=False)
