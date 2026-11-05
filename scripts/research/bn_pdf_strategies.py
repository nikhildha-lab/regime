"""Bank Nifty option-buying strategies from Nikhil's PDF (compiled 6 Nov 2026) - backtest with a SYNTHETIC FUTURE + HEDGE (2026-11-06,
Nikhil: "Can you backtest these with synthetic future with hedge"). Paper research only. Rules written from the PDF BEFORE running.
Data: Bank Nifty spot 5-min 18 Jul 2024 - 17 Jul 2026 (nifty-live-tracker datasets); daily ^NSEBANK (Yahoo) for #13; BN monthly expiry
dates from the NSE futures file for #12. Y1 = Jul 2024 - Jun 2025, Y2 = Jul 2025 - Jul 2026.
Index data has NO volume -> 'VWAP' = the day's running average of the typical price (TWAP proxy); #10's volume filter is skipped.
Vehicles (1 lot = 30): plain synthetic (buy ATM CE + sell ATM PE, mirror for shorts) = index points - 12.5 pts;
  hedged 0.2 = 0.8 x points - 16 pts; hedged 0.3 = 0.7 x points - 16.5 pts (the bought ~0.2/0.3-delta option on the sold side lowers the
  net delta and adds ~3.5-4 pts of costs; intraday its loss-cap is rarely reached, so it is modelled as delta + cost). #13 (held for days)
  also pays ~6 pts a day of hedge decay (estimate) and a roll cost every 20 sessions.
Fills: stop-entries fill at the trigger (or the bar open if it gaps past); stops / targets on 5-min highs / lows, stop first if both in one
bar (conservative); intraday trades out at the 15:15 bar close; entries 09:30-15:00 unless the rule says otherwise.
Not testable here: #8 (option-chart VWAP - needs option prices), #11 (OI - needs 2 years of chain), #14 (a vehicle, not a signal)."""
import glob, os
import numpy as np, pandas as pd
DOC = os.path.expanduser("~/mnt/Documents"); ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
LOT = 30
fs = sorted(glob.glob(f"{DOC}/nifty-live-tracker/datasets/2024-07-17_to_2026-07-19/*/BANKNIFTY_spot.csv"))
B = pd.concat([pd.read_csv(f) for f in fs]); B["tt"] = pd.to_datetime(B.datetime); B = B.drop_duplicates("tt").sort_values("tt").reset_index(drop=True)
B["day"] = B.tt.dt.strftime("%Y-%m-%d"); B["hm"] = B.tt.dt.strftime("%H:%M")
B = B[B.groupby("day").close.transform("size") >= 60].reset_index(drop=True)

def ema(s, n): return s.ewm(span=n, adjust=False).mean()
def rsi(c, n=14):
    d = c.diff(); u = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean(); v = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + u / v)
def supertrend(h, l, c, n=10, m=3.0):
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1); atr = tr.ewm(alpha=1 / n, adjust=False).mean()
    hl2 = (h + l) / 2; ub, lb = (hl2 + m * atr).values, (hl2 - m * atr).values; C = c.values
    fu, fl, dirn = ub.copy(), lb.copy(), np.ones(len(c))
    for i in range(1, len(c)):
        fu[i] = ub[i] if (ub[i] < fu[i - 1] or C[i - 1] > fu[i - 1]) else fu[i - 1]
        fl[i] = lb[i] if (lb[i] > fl[i - 1] or C[i - 1] < fl[i - 1]) else fl[i - 1]
        dirn[i] = 1 if C[i] > fu[i - 1] else -1 if C[i] < fl[i - 1] else dirn[i - 1]
    line = np.where(dirn > 0, fl, fu)
    return pd.Series(dirn, index=c.index), pd.Series(line, index=c.index)
def resample(df, mins):
    g = df.set_index("tt").groupby("day")
    out = g.apply(lambda x: x[["open", "high", "low", "close"]].resample(f"{mins}min", origin="start_day", offset="9h15min").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last"}).dropna()).reset_index()
    out["hm"] = out.tt.dt.strftime("%H:%M"); return out

# indicators on 5-min (continuous across days) + day context
B["tp"] = (B.high + B.low + B.close) / 3; B["vwap"] = B.groupby("day").tp.cumsum() / (B.groupby("day").cumcount() + 1)
B["e5"], B["e9"], B["e20"], B["e50"] = ema(B.close, 5), ema(B.close, 9), ema(B.close, 20), ema(B.close, 50)
B["rsi"] = rsi(B.close); B["rsima"] = B.rsi.rolling(9).mean()
m20 = B.close.rolling(20).mean(); sd20 = B.close.rolling(20).std(); B["bbu"], B["bbl"] = m20 + 1.5 * sd20, m20 - 1.5 * sd20
B["st"], B["stl"] = supertrend(B.high, B.low, B.close)
D = B.groupby("day").agg(h=("high", "max"), l=("low", "min"), c=("close", "last")); D["pp"] = ((D.h + D.l + D.c) / 3).shift()
B = B.merge(D[["pp"]], left_on="day", right_index=True, how="left")
M15 = resample(B, 15); M15["e5"] = ema(M15.close, 5); m = M15.close.rolling(20).mean(); s = M15.close.rolling(20).std(); M15["bbl"] = m - 1.5 * s
DAYS = {d: g.reset_index(drop=True) for d, g in B.groupby("day")}; D15 = {d: g.reset_index(drop=True) for d, g in M15.groupby("day")}
TRADES = []

def walk(g, i0, side, entry, stop, target=None, exit_fn=None, last_hm="15:15"):
    """Manage from bar i0 (the bar AFTER entry). exit_fn(row) -> True to exit at that bar's close."""
    for k in range(i0, len(g)):
        r = g.iloc[k]
        if side > 0 and r.low <= stop: return min(stop, r.open) - entry if r.open < stop else stop - entry, "stop", r.hm
        if side < 0 and r.high >= stop: return entry - (max(stop, r.open) if r.open > stop else stop), "stop", r.hm
        if target is not None:
            if side > 0 and r.high >= target: return target - entry, "target", r.hm
            if side < 0 and r.low <= target: return entry - target, "target", r.hm
        if r.hm >= last_hm: return side * (r.close - entry), "time", r.hm
        if exit_fn is not None and exit_fn(r): return side * (r.close - entry), "rule exit", r.hm
    r = g.iloc[-1]; return side * (r.close - entry), "time", r.hm

def add(sid, day, hm, side, res):
    pts, why, xhm = res; TRADES.append(dict(s=sid, day=day, hm=hm, side=side, pts=pts, why=why, xhm=xhm))

for day, g in DAYS.items():
    if g.hm.iloc[0] != "09:15" or len(g) < 70: continue
    orh, orl = g[g.hm < "09:30"].high.max(), g[g.hm < "09:30"].low.min(); rng = orh - orl
    # S1 ORB + VWAP: first 5-min close beyond the 09:15-09:30 range with VWAP agreeing; stop other end; target 1.5 x range
    for i in range(len(g)):
        r = g.iloc[i]
        if not ("09:30" <= r.hm <= "15:00"): continue
        if r.close > orh and r.close > r.vwap: add(1, day, r.hm, 1, walk(g, i + 1, 1, r.close, orl, r.close + 1.5 * rng)); break
        if r.close < orl and r.close < r.vwap: add(1, day, r.hm, -1, walk(g, i + 1, -1, r.close, orh, r.close - 1.5 * rng)); break
    # S2 Opening-range reversal (15-min): break out, then a 15-min close back inside -> fade; stop beyond the extreme since the break; target VWAP or other side
    q = D15.get(day)
    if q is not None and len(q) > 4:
        brk, ext = 0, None
        for j in range(1, len(q)):
            r = q.iloc[j]
            if r.hm > "14:45": break
            if not brk:
                if r.high > orh: brk, ext = 1, r.high
                elif r.low < orl: brk, ext = -1, r.low
                if not brk: continue
            ext = max(ext, r.high) if brk > 0 else min(ext, r.low)
            back = (brk > 0 and r.close < orh) or (brk < 0 and r.close > orl)
            if back:
                side = -brk; e = r.close; stop = ext + brk * 0.0002 * e
                g5 = g[g.tt >= r.tt + pd.Timedelta(minutes=15)].reset_index(drop=True)
                vw = g[g.tt < r.tt + pd.Timedelta(minutes=15)].vwap.iloc[-1]
                far = orl if side < 0 else orh
                tgt = vw if (side < 0 and far < vw < e) or (side > 0 and e < vw < far) else far
                if side * (tgt - e) > 0 and len(g5): add(2, day, r.hm, side, walk(g5, 0, side, e, stop, tgt))
                break
    # S3 2 PM breakout: day high / low at 14:00; stop-entry on the break; stop = other side or 0.5 % (the closer); out 15:20
    pre = g[g.hm < "14:00"]; H, L = pre.high.max(), pre.low.min()
    for i in range(len(g)):
        r = g.iloc[i]
        if r.hm < "14:00" or r.hm > "15:10": continue
        if r.high > H:
            e = max(H, r.open); st = max(L, e * 0.995); add(3, day, r.hm, 1, walk(g, i + 1, 1, e, st)); break
        if r.low < L:
            e = min(L, r.open); st = min(H, e * 1.005); add(3, day, r.hm, -1, walk(g, i + 1, -1, e, st)); break
    # S4 / S5 Power of Stocks 5 EMA (CE on 15-min, PE on 5-min); S5 adds the Bollinger Band (20, 1.5) condition
    for sid, bb in ((4, False), (5, True)):
        if q is not None:                                   # CE side on 15-min
            alert = None; busy = "00:00"
            for j in range(1, len(q)):
                r = q.iloc[j]
                if r.hm >= "15:00": break
                if alert is not None and r.high > alert.high and r.hm >= busy:
                    e = max(alert.high, r.open); st = alert.low; R = e - st
                    if R > 0:
                        g5 = g[g.tt >= r.tt].reset_index(drop=True)
                        res = walk(g5, 0, 1, e, st, e + 3 * R); add(sid, day, r.hm, 1, res); busy = res[2]
                    alert = None; continue
                cond = r.high < r.e5 and (not bb or r.high < r.bbl)
                if cond: alert = r
        alert = None; busy = "00:00"                         # PE side on 5-min
        for i in range(1, len(g)):
            r = g.iloc[i]
            if r.hm >= "15:00": break
            if alert is not None and r.low < alert.low and r.hm >= busy:
                e = min(alert.low, r.open); st = alert.high; R = st - e
                if R > 0:
                    res = walk(g, i, -1, e, st, e - 3 * R); add(sid, day, r.hm, -1, res); busy = res[2]
                alert = None; continue
            cond = r.low > r.e5 and (not bb or r.low > r.bbu)
            if cond: alert = r
    # S6 9 EMA + pivot + RSI: close crosses above both (RSI > its 9-bar mean) -> long; stop previous candle low; target 50 pts
    busy = "00:00"
    for i in range(2, len(g)):
        r, p = g.iloc[i], g.iloc[i - 1]
        if not ("09:30" <= r.hm <= "15:00") or r.hm < busy or not np.isfinite(r.pp): continue
        up = r.close > max(r.e9, r.pp) and p.close <= max(p.e9, r.pp) and r.rsi > r.rsima
        dn = r.close < min(r.e9, r.pp) and p.close >= min(p.e9, r.pp) and r.rsi < r.rsima
        if up and r.close - p.low > 0: res = walk(g, i + 1, 1, r.close, p.low, r.close + 50); add(6, day, r.hm, 1, res); busy = res[2]
        elif dn and p.high - r.close > 0: res = walk(g, i + 1, -1, r.close, p.high, r.close - 50); add(6, day, r.hm, -1, res); busy = res[2]
    # S7 VWAP pullback: 6 bars on one side, a bar touches VWAP and closes back on that side -> trade; stop 50 pts past VWAP; exit on a close across VWAP
    busy = "00:00"
    for i in range(7, len(g)):
        r = g.iloc[i]; w = g.iloc[i - 6:i]
        if not ("09:45" <= r.hm <= "15:00") or r.hm < busy: continue
        if (w.close > w.vwap).all() and r.low <= r.vwap < r.close:
            res = walk(g, i + 1, 1, r.close, r.vwap - 50, None, lambda x: x.close < x.vwap); add(7, day, r.hm, 1, res); busy = res[2]
        elif (w.close < w.vwap).all() and r.high >= r.vwap > r.close:
            res = walk(g, i + 1, -1, r.close, r.vwap + 50, None, lambda x: x.close > x.vwap); add(7, day, r.hm, -1, res); busy = res[2]
    # S9 Supertrend (10, 3) + VWAP: flip with VWAP agreeing; exit on the opposite flip (stop = Supertrend line at entry)
    busy = "00:00"
    for i in range(1, len(g)):
        r, p = g.iloc[i], g.iloc[i - 1]
        if not ("09:30" <= r.hm <= "15:00") or r.hm < busy: continue
        if r.st > 0 and p.st < 0 and r.close > r.vwap:
            res = walk(g, i + 1, 1, r.close, r.stl, None, lambda x: x.st < 0); add(9, day, r.hm, 1, res); busy = res[2]
        elif r.st < 0 and p.st > 0 and r.close < r.vwap:
            res = walk(g, i + 1, -1, r.close, r.stl, None, lambda x: x.st > 0); add(9, day, r.hm, -1, res); busy = res[2]
    # S10 20 / 50 EMA: close crosses to above both -> long, exit on a close back below the 20 EMA (mirror for shorts); stop = last 6-bar swing
    busy = "00:00"
    for i in range(7, len(g)):
        r, p = g.iloc[i], g.iloc[i - 1]
        if not ("09:30" <= r.hm <= "15:00") or r.hm < busy: continue
        if r.close > max(r.e20, r.e50) and p.close <= max(p.e20, p.e50):
            res = walk(g, i + 1, 1, r.close, g.iloc[i - 6:i + 1].low.min(), None, lambda x: x.close < x.e20); add(10, day, r.hm, 1, res); busy = res[2]
        elif r.close < min(r.e20, r.e50) and p.close >= min(p.e20, p.e50):
            res = walk(g, i + 1, -1, r.close, g.iloc[i - 6:i + 1].high.max(), None, lambda x: x.close > x.e20); add(10, day, r.hm, -1, res); busy = res[2]

# S12 monthly expiry afternoon: 14:00-14:30 range on BN monthly expiry days; 5-min close beyond -> trade; stop other side; target 1 x range; out 15:20
F = pd.read_csv(os.path.join(ROOT, "data_store/market_data/nse_index_futures_2023_2026.csv"), usecols=["symbol", "expiry"])
EXP = set(F[F.symbol == "BANKNIFTY"].expiry.astype(str).str[:10])
for day, g in DAYS.items():
    if day not in EXP: continue
    w = g[(g.hm >= "14:00") & (g.hm < "14:30")]
    if len(w) < 5: continue
    H, L = w.high.max(), w.low.min(); R = H - L
    for i in range(len(g)):
        r = g.iloc[i]
        if r.hm < "14:30" or r.hm > "15:10": continue
        if r.close > H: add(12, day, r.hm, 1, walk(g, i + 1, 1, r.close, L, r.close + R)); break
        if r.close < L: add(12, day, r.hm, -1, walk(g, i + 1, -1, r.close, H, r.close - R)); break
T = pd.DataFrame(TRADES); T["hold_days"] = 0.0

# S13 daily Supertrend (10, 3) positional on ^NSEBANK daily (2015 -> 2026 for depth; split reported for the same Y1 / Y2 window too)
y = pd.read_csv(os.path.join(ROOT, "data_store/market_data/yahoo_daily_deep.csv")); y = y[y.symbol == "^NSEBANK"].drop_duplicates("date").sort_values("date")
y = y[y.date >= "2015-01-01"].reset_index(drop=True); st_, _ = supertrend(y.high, y.low, y.close)
pos = 0; e0 = d0 = None; rows13 = []
for i in range(1, len(y)):
    if st_[i] != st_[i - 1]:
        if pos: rows13.append(dict(s=13, day=str(y.date[d0])[:10], hm="close", side=pos, pts=pos * (y.close[i] - e0), why="flip", xhm=str(y.date[i])[:10], hold_days=float(i - d0)))
        pos = int(st_[i]); e0 = y.close[i]; d0 = i
T = pd.concat([T, pd.DataFrame(rows13)], ignore_index=True)

NAMES = {1: "ORB + VWAP", 2: "Opening-range reversal", 3: "2 PM breakout", 4: "5 EMA (Power of Stocks)", 5: "5 EMA + Bollinger",
         6: "9 EMA + pivot + RSI", 7: "VWAP pullback", 9: "Supertrend + VWAP", 10: "20 / 50 EMA", 12: "Monthly expiry afternoon", 13: "Daily Supertrend positional"}
T["year"] = np.where(T.day < "2025-07-01", "Y1", "Y2"); T.loc[(T.s == 13) & (T.day < "2024-07-18"), "year"] = "pre"
roll = (T.hold_days // 20) * 12.5; decay = T.hold_days * 6
T["plain"] = (T.pts - 12.5 - roll) * LOT; T["h20"] = (0.8 * T.pts - 16 - roll - decay) * LOT; T["h30"] = (0.7 * T.pts - 16.5 - roll - decay) * LOT
os.makedirs(os.path.join(ROOT, "data_store/research/bn_pdf"), exist_ok=True); T.to_csv(os.path.join(ROOT, "data_store/research/bn_pdf/trades.csv"), index=False)
out = []
for sid, nm in NAMES.items():
    Z = T[T.s == sid]
    for yr in (("Y1", "Y2") if sid != 13 else ("pre", "Y1", "Y2")):
        z = Z[Z.year == yr]
        if not len(z): continue
        out.append(dict(no=sid, strategy=nm, year=yr, n=len(z), win=round(100 * (z.pts > 12.5).mean()), avg_pts=round(z.pts.mean(), 1),
                        plain_rs=int(z.plain.mean()), h20_rs=int(z.h20.mean()), h30_rs=int(z.h30.mean()), plain_total=int(z.plain.sum()), h30_total=int(z.h30.sum())))
R = pd.DataFrame(out); R.to_csv(os.path.join(ROOT, "data_store/research/bn_pdf/summary.csv"), index=False)
pd.set_option("display.width", 220); print(R.to_string(index=False))
