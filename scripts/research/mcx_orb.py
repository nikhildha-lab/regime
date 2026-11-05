"""MCX opening-range breakout - history check (2026-11-05, Nikhil: "start apply strategies on metals and commodities" -> MCX via Dhan).
Paper research only. Rules fixed BEFORE running (the Stocks in Play ORB rule, adapted to MCX's two busy moments):
  series   : per underlying, each day the contract with the most volume that day (Dhan 1-min, nearest 3 live contracts)
  windows  : MORNING  opening range 09:00-09:15 IST, entries 09:15-11:00, all flat 16:55
             US OPEN  opening range = first 15 min after 09:30 New York time (19:00 IST summer / 20:00 IST winter),
                      entries for the next 2 hours, all flat 23:20 IST
  in play  : opening-range volume >= 2 x its own average of the previous 20 sessions (same window)
  entry    : first 5-min close beyond the range, either side (direction = that side); one trade per window per day
  stop     : other side of the range or 1 x ATR(14, 5-min), whichever is closer
  exits    : half off at 1.5R, stop to entry; the rest out on a 5-min close back through the day VWAP (from 09:00); flat time
  costs    : Dhan Rs 20 / order x 2 + 18 % GST, CTT 0.01 % on the sell side, exchange ~0.0021 % a side (+GST), stamp 0.002 % buy,
             SEBI fee, + 1 tick slippage each side; per 1 lot (multipliers below - CHECK against MCX contract specs)
"""
import glob, os
import numpy as np, pandas as pd
from zoneinfo import ZoneInfo
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
D = os.path.join(ROOT, "data_store", "research", "mcx_intraday")
MULT = {"GOLDM": 10, "SILVERM": 5, "CRUDEOILM": 10, "NATGASMINI": 250, "COPPER": 2500, "ALUMINIUM": 5000, "ZINC": 5000}
TICK = {"GOLDM": 1.0, "SILVERM": 1.0, "CRUDEOILM": 1.0, "NATGASMINI": 0.1, "COPPER": 0.05, "ALUMINIUM": 0.05, "ZINC": 0.05}
NY, IST = ZoneInfo("America/New_York"), ZoneInfo("Asia/Kolkata")

def series(u):
    parts = []
    for f in glob.glob(os.path.join(D, f"{u}_*_1m.csv.gz")):
        d = pd.read_csv(f, parse_dates=["ts"]); d["exp"] = os.path.basename(f).split("_")[1]; parts.append(d)
    a = pd.concat(parts); a["day"] = a.ts.dt.strftime("%Y-%m-%d")
    best = a.groupby(["day", "exp"]).volume.sum().reset_index().sort_values("volume").groupby("day").tail(1)
    a = a.merge(best[["day", "exp"]], on=["day", "exp"]).sort_values("ts").set_index("ts")
    return a

def five(a):
    b = a.groupby("day", group_keys=False).apply(lambda x: x[["open", "high", "low", "close", "volume"]].resample("5min").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}).dropna())
    b["day"] = b.index.strftime("%Y-%m-%d"); b["hm"] = b.index.strftime("%H:%M")
    tp = (b.high + b.low + b.close) / 3
    b["vwap"] = (tp * b.volume).groupby(b.day).cumsum() / b.volume.groupby(b.day).cumsum().replace(0, np.nan)
    tr = pd.concat([b.high - b.low, (b.high - b.close.shift()).abs(), (b.low - b.close.shift()).abs()], axis=1).max(axis=1)
    b["atr"] = tr.rolling(14).mean()
    return b

def cost_pct(u, px):
    v = px * MULT[u]
    rs = 40 * 1.18 + v * (0.0001 + 2 * 0.000021 * 1.18 + 0.00002 + 2 * 0.000001)
    return 100 * (rs / v + 2 * TICK[u] / px)

def windows(day):
    d = pd.Timestamp(day)
    ny = pd.Timestamp(f"{day} 09:30", tz=NY).tz_convert(IST).tz_localize(None)
    return {"MORNING": (d + pd.Timedelta("09:00:00"), d + pd.Timedelta("09:15:00"), d + pd.Timedelta("11:00:00"), d + pd.Timedelta("16:55:00")),
            "US OPEN": (ny, ny + pd.Timedelta(minutes=15), ny + pd.Timedelta(minutes=135), d + pd.Timedelta("23:20:00"))}

def run():
    rows = []
    for u in MULT:
        try:
            a = series(u)
        except ValueError:
            continue
        b = five(a); days = sorted(b.day.unique()); orv = {}
        for day in days:
            W = windows(day)
            for wn, (o0, o1, e1, flat) in W.items():
                o = a[(a.index >= o0) & (a.index < o1) & (a.day == day)]
                if len(o) < 10: continue
                v = o.volume.sum(); hist = orv.get(wn, []); orv.setdefault(wn, []).append(v)
                if len(hist) < 10: continue
                rv = v / np.mean(hist[-20:])
                if rv < 2: continue
                hi, lo = o.high.max(), o.low.min()
                bb = b[(b.day == day) & (b.index >= o1)]
                w = bb[bb.index < e1]
                hit = w[(w.close > hi) | (w.close < lo)]
                if not len(hit): continue
                t0 = hit.index[0]; x = bb.loc[t0]; s = 1 if x.close > hi else -1; e = x.close
                st_or = lo if s > 0 else hi; st_atr = e - s * x.atr if np.isfinite(x.atr) else st_or
                stop = st_or if abs(e - st_or) < abs(e - st_atr) else st_atr
                R = abs(e - stop)
                if R <= 0: continue
                left, pnl, cur, why = 1.0, 0.0, stop, None
                for t_, y in bb[bb.index > t0].iterrows():
                    if t_ >= flat:
                        pnl += left * s * (y.close - e); why = "flat"; break
                    if (s > 0 and y.low <= cur) or (s < 0 and y.high >= cur):
                        pnl += left * s * (cur - e); why = "stop" if left == 1 else "breakeven"; break
                    if left == 1 and ((s > 0 and y.high >= e + 1.5 * R) or (s < 0 and y.low <= e - 1.5 * R)):
                        pnl += 0.5 * 1.5 * R; left = 0.5; cur = e
                    if left == 0.5 and s * (y.close - y.vwap) < 0:
                        pnl += left * s * (y.close - e); why = "VWAP"; break
                else:
                    if len(bb): pnl += left * s * (bb.close.iloc[-1] - e); why = "end of data"
                g = pnl / e * 100; c = cost_pct(u, e)
                rows.append(dict(u=u, win=wn, day=day, t=str(t0), side=s, rv=round(rv, 2), entry=e, risk_pct=round(R / e * 100, 2),
                                 g=round(g, 3), cost=round(c, 3), net=round(g - c, 3), rs_lot=round((g - c) / 100 * e * MULT[u]), exit=why))
    return pd.DataFrame(rows)

if __name__ == "__main__":
    T = run(); os.makedirs(os.path.join(ROOT, "data_store/research/mcx_orb"), exist_ok=True)
    T.to_csv(os.path.join(ROOT, "data_store/research/mcx_orb/trades.csv"), index=False)
    days = sorted(T.day.unique()); half = days[len(days) // 2]
    def st(Z): return pd.Series({"n": len(Z), "win%": round(100 * (Z.net > 0).mean()), "gross%": round(Z.g.mean(), 3), "net%": round(Z.net.mean(), 3),
                                 "rs_lot": int(Z.rs_lot.sum()), "h1_net%": round(Z[Z.day < half].net.mean(), 3), "h2_net%": round(Z[Z.day >= half].net.mean(), 3)})
    print("days", days[0], "->", days[-1], "split", half)
    print(T.groupby("win").apply(st).to_string()); print(T.groupby(["win", "u"]).apply(st).to_string())
    print("ALL", st(T).to_dict()); print(T.groupby("u").cost.mean().round(3).to_dict())
