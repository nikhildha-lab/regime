"""Indian stocks intraday (MIS 5x) + TradingView order-flow ideas (2026-11-05). Paper research only. Rules FIXED BEFORE RUNNING.
Data: 1-min OHLCV for 48 F&O stocks, 8 Jul - 4 Nov 2026 (Dhan, data_store/research/stocks_intraday), built into 5-min bars.
No bid/ask tape for stocks, so 'delta' = candle-colour volume (green = buy, red = sell) - the same approximation the TradingView
scripts use. Costs: stocks 0.12 % round trip (charges ~0.08 % + slippage ~0.04 %); crypto 0.10 %. Money: Rs 1 lakh position
(Rs 20,000 margin x 5). One trade at a time per stock; all trades flat by 15:10.
S1 STOCKS IN PLAY ORB: opening range 09:15-09:30; in play = opening-range volume >= 2 x its own 20-day average AND gap >= 0.5 %;
   entry = first 5-min close beyond the range in the gap direction, 09:30-11:30; stop = other side of the range or 1 x ATR(14, 5m),
   whichever is closer; half off at 1.5R and stop to entry; rest exits on a 5-min close back through VWAP; max 3 a day (highest volume).
S2 STANDARDIZED ORDERFLOW (our version of AlgoAlpha's description): SO = sum of signed volume over 10 bars / (stdev of signed volume
   over 50 bars x sqrt 10). (a) momentum: long when SO crosses above +1 with price above VWAP, short mirror; (b) reversal: short when SO
   falls back below +2.5 after being above, long mirror. Stop 1 x ATR, out after 6 bars (30 min) or 15:10.
S3 ULTIMATE ORDER FLOW (our version of the description): 50-bar volume profile in 20 rows; HVN = row volume >= 1.3 x mean row;
   large order = bar volume >= 2.5 x its 20-bar average; LONG = price within 0.2 % of an HVN, large GREEN bar, cumulative delta above
   its 20-bar average; SHORT mirror; 10-bar cooldown; stop 1 x ATR beyond entry, target 2R, out after 12 bars or 15:10.
All are tested on stocks and on BTC / ETH / SOL 5-min (Binance, ~90 days, same rules, 24 h, no 15:10 exit, crypto cost)."""
import os, glob
import numpy as np, pandas as pd
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
SD = os.path.join(ROOT, "data_store/research/stocks_intraday"); A1 = os.path.join(ROOT, "data_store/research/alts1m")
POS = 100000


def five(d1):
    d1 = d1.set_index("ts").sort_index()
    b = d1.resample("5min", origin="start_day", offset="9h15min", label="left", closed="left").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}).dropna()
    return b


def add_feats(b, session=True):
    b = b.copy(); b["day"] = b.index.normalize(); b["hm"] = b.index.strftime("%H:%M")
    tp = (b.high + b.low + b.close) / 3
    if session:
        b["vwap"] = (tp * b.volume).groupby(b.day).cumsum() / b.volume.groupby(b.day).cumsum()
    else:
        b["vwap"] = (tp * b.volume).rolling(288).sum() / b.volume.rolling(288).sum()
    tr = np.maximum(b.high - b.low, np.maximum((b.high - b.close.shift()).abs(), (b.low - b.close.shift()).abs()))
    b["atr"] = tr.rolling(14).mean()
    b["sv"] = np.sign(b.close - b.open) * b.volume
    b["so"] = b.sv.rolling(10).sum() / (b.sv.rolling(50).std() * np.sqrt(10))
    b["cvd"] = b.sv.cumsum(); b["cvd_ma"] = b.cvd.rolling(20).mean(); b["vma"] = b.volume.rolling(20).mean()
    return b


def run_exit(b, i, s, e, stop, tgt, max_bars, session, mode="simple"):
    """Walk forward from bar i+1. mode simple: stop / target / time. mode s1: half at 1.5R + stop to entry, rest out on VWAP cross."""
    R = abs(e - stop); left, pnl, cur = 1.0, 0.0, stop; H, L, C, V, HM = b.high.values, b.low.values, b.close.values, b.vwap.values, b.hm.values
    day = b.day.values[i]; D = b.day.values
    for k in range(i + 1, min(i + 1 + max_bars, len(b))):
        if session and (D[k] != day or HM[k] >= "15:10"):
            return pnl + left * s * (C[k - 1] - e) if D[k] != day else pnl + left * s * (C[k] - e)
        if (s > 0 and L[k] <= cur) or (s < 0 and H[k] >= cur):
            return pnl + left * s * (cur - e)
        if mode == "s1":
            if left == 1.0 and ((s > 0 and H[k] >= e + 1.5 * R) or (s < 0 and L[k] <= e - 1.5 * R)):
                pnl += 0.5 * 1.5 * R; left = 0.5; cur = e
            if left == 0.5 and s * (C[k] - V[k]) < 0:
                return pnl + left * s * (C[k] - e)
        elif tgt is not None and ((s > 0 and H[k] >= tgt) or (s < 0 and L[k] <= tgt)):
            return s * (tgt - e)
    k = min(i + max_bars, len(b) - 1)
    return pnl + left * s * (C[k] - e)


def s2_s3(b, session, mkt):
    out = []
    so = b.so.values; C = b.close.values; VW = b.vwap.values; A = b.atr.values; HM = b.hm.values; D = b.day.values
    busy = {"S2a": -1, "S2b": -1, "S3": -1}
    for i in range(60, len(b) - 1):
        if session and not ("09:30" <= HM[i] <= "14:50"):
            continue
        if not np.isfinite(A[i]) or A[i] <= 0:
            continue
        sig = {}
        if so[i - 1] < 1 <= so[i] and C[i] > VW[i]: sig["S2a"] = 1
        if so[i - 1] > -1 >= so[i] and C[i] < VW[i]: sig["S2a"] = -1
        if so[i - 1] > 2.5 >= so[i]: sig["S2b"] = -1
        if so[i - 1] < -2.5 <= so[i]: sig["S2b"] = 1
        if b.volume.values[i] >= 2.5 * b.vma.values[i]:
            w = b.iloc[i - 49:i + 1]; lo, hi = w.low.min(), w.high.max()
            if hi > lo:
                edges = np.linspace(lo, hi, 21); idx = np.clip(np.digitize((w.high + w.low + w.close) / 3, edges) - 1, 0, 19)
                prof = np.bincount(idx, weights=w.volume, minlength=20); mids = (edges[:-1] + edges[1:]) / 2
                hvn = mids[prof >= 1.3 * prof.mean()]
                near = len(hvn) and np.min(np.abs(hvn / C[i] - 1)) <= 0.002
                green = C[i] > b.open.values[i]; cvd_up = b.cvd.values[i] > b.cvd_ma.values[i]
                if near and green and cvd_up: sig["S3"] = 1
                if near and (not green) and (not cvd_up): sig["S3"] = -1
        for nm, s in sig.items():
            if i <= busy[nm]: continue
            e = C[i]; stop = e - s * A[i]
            if nm == "S3":
                g = run_exit(b, i, s, e, stop, e + s * 2 * A[i], 12, session)
                busy[nm] = i + 10
            else:
                g = run_exit(b, i, s, e, stop, None, 6, session)
                busy[nm] = i + 6
            out.append(dict(mkt=mkt, rule=nm, t=b.index[i], g=g / e * 100))
    return out


def s1(b, mkt, daily):
    out = []
    b = b.copy(); days = sorted(b.day.unique())
    orv = b[b.hm < "09:30"].groupby("day").volume.sum(); orh = b[b.hm < "09:30"].groupby("day").high.max(); orl = b[b.hm < "09:30"].groupby("day").low.min()
    for j, dday in enumerate(days):
        if j < 20: continue
        avg = orv.reindex(days[j - 20:j]).mean(); prevc = b[b.day == days[j - 1]].close.iloc[-1]
        today = b[b.day == dday]
        if not len(today) or avg <= 0: continue
        rv = orv.get(dday, 0) / avg; gap = today.open.iloc[0] / prevc - 1
        if rv < 2 or abs(gap) < 0.005: continue
        s = 1 if gap > 0 else -1; hi, lo = orh[dday], orl[dday]
        for i in np.where((b.day.values == dday) & (b.hm.values >= "09:30") & (b.hm.values <= "11:30"))[0]:
            c = b.close.values[i]
            if (s > 0 and c > hi) or (s < 0 and c < lo):
                a = b.atr.values[i]; st_or = lo if s > 0 else hi; st_atr = c - s * a
                stop = st_or if abs(c - st_or) < abs(c - st_atr) else st_atr
                if s * (c - stop) <= 0: break
                g = run_exit(b, i, s, c, stop, None, 80, True, "s1")
                out.append(dict(mkt=mkt, rule="S1", t=b.index[i], g=g / c * 100, rv=rv, day=dday))
                break
    return out


rows, s1rows = [], []
for f in sorted(glob.glob(os.path.join(SD, "*_1m.csv.gz"))):
    sym = os.path.basename(f)[:-10]
    d = pd.read_csv(f, parse_dates=["ts"]); b = add_feats(five(d))
    rows += s2_s3(b, True, sym); s1rows += s1(b, sym, None)
for sym in ("BTC", "ETH", "SOL"):
    d = pd.read_csv(os.path.join(A1, f"{sym}USDT_1m.csv.gz")); d["ts"] = pd.to_datetime(d.t, unit="ms")
    d = d.rename(columns={"o": "open", "h": "high", "l": "low", "c": "close", "v": "volume"})
    b = d.set_index("ts")[["open", "high", "low", "close", "volume"]].resample("5min").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}).dropna()
    rows += [dict(r, mkt="CRYPTO:" + sym) for r in s2_s3(add_feats(b, session=False), False, sym)]
S1 = pd.DataFrame(s1rows)
if len(S1):                                           # max 3 a day, highest opening-range volume first
    S1 = S1.sort_values(["day", "rv"], ascending=[True, False]).groupby("day").head(3)
D = pd.concat([pd.DataFrame(rows), S1.drop(columns=["rv", "day"], errors="ignore")], ignore_index=True)
D["crypto"] = D.mkt.str.startswith("CRYPTO"); D["cost"] = np.where(D.crypto, 0.10, 0.12); D["net"] = D.g - D.cost
mid = D.t.sort_values().iloc[len(D) // 2]
def line(z):
    h = z.t.median()
    return (f"n {len(z):5d} · win {100*(z.net>0).mean():3.0f}% · gross {z.g.mean():+.3f}% · net {z.net.mean():+.3f}% · halves "
            f"{z[z.t < h].net.mean():+.3f} / {z[z.t >= h].net.mean():+.3f} · Rs/lot-equiv {z.net.sum()/100*POS:+,.0f}")
print("period stocks:", D[~D.crypto].t.min(), "->", D[~D.crypto].t.max())
for (cr, rule), z in D.groupby(["crypto", "rule"]):
    print(f"  {'CRYPTO' if cr else 'STOCKS'} {rule:4s} {line(z)}")
os.makedirs(os.path.join(ROOT, "data_store/research/stock_intraday"), exist_ok=True); D.to_csv(os.path.join(ROOT, "data_store/research/stock_intraday/trades.csv"), index=False)
