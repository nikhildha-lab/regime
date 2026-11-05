"""Bank Nifty PDF strategies - OPTION BUYING ONLY (2026-11-06, Nikhil: "Let's test all these once again with option buying only").
Same trades as bn_pdf_strategies.py (same entries, chart stops, targets, exits). Vehicle: BUY one ~0.6-delta Bank Nifty MONTHLY option
(CE for longs, PE for shorts), 1 lot = 30. No 2-year option-price history exists, so each leg is priced with Black-Scholes:
  IV = India VIX (previous close) x 1.15 (Bank Nifty normally trades richer than Nifty; ASSUMPTION), held constant through the trade
  (no IV crush / spike modelled); r = 0 (premium at spot; the futures carry is small for a bought option's P&L over minutes-days)
  expiry: nearest BN monthly with >= 3 days left (#12 expiry-afternoon trades use that day's expiry; #13 positional needs >= 15 days and
  rolls are ignored); dates from the NSE futures file (before 2023: last Thursday of the month)
  strike: the 100-point strike whose BS delta is closest to 0.6 (slightly in the money)
  time: entry / exit minutes inside the day count toward expiry (the 6.25-hour session = 1 trading day of decay; weekends count as days)
  costs: touchtrades._costs (Rs 20 / order brokerage, STT 0.15 % on the sell, exchange, GST, stamp, 1.5-pt spread allowance)
Exit spot = entry spot + side x the strategy's index points (exactly the same exits as the synthetic test)."""
import math, os, sys
import numpy as np, pandas as pd
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")); sys.path.insert(0, os.path.join(ROOT, "app"))
import touchtrades as TT
LOT, DOC = 30, os.path.expanduser("~/mnt/Documents")
T = pd.read_csv(os.path.join(ROOT, "data_store/research/bn_pdf/trades.csv"))
import glob
fs = sorted(glob.glob(f"{DOC}/nifty-live-tracker/datasets/2024-07-17_to_2026-07-19/*/BANKNIFTY_spot.csv"))
B = pd.concat([pd.read_csv(f) for f in fs]).drop_duplicates("datetime"); B["day"] = B.datetime.str[:10]; B["hm"] = B.datetime.str[11:16]
PX = B.set_index(["day", "hm"]).close
y = pd.read_csv(os.path.join(ROOT, "data_store/market_data/yahoo_daily_deep.csv"), usecols=["date", "symbol", "close"])
VIX = y[y.symbol == "^INDIAVIX"].drop_duplicates("date").set_index("date").close.sort_index()
BND = y[y.symbol == "^NSEBANK"].drop_duplicates("date").set_index("date").close.sort_index()
F = pd.read_csv(os.path.join(ROOT, "data_store/market_data/nse_index_futures_2023_2026.csv"), usecols=["symbol", "expiry"])
EXP = sorted(pd.to_datetime(F[F.symbol == "BANKNIFTY"].expiry.astype(str).str[:10].unique()))
def last_thu(y_, m_):
    d = pd.Timestamp(y_, m_, 1) + pd.offsets.MonthEnd(0)
    while d.weekday() != 3: d -= pd.Timedelta(days=1)
    return d
OLD = [last_thu(yy, mm) for yy in range(2014, 2023) for mm in range(1, 13)]
ALLEXP = sorted(set(OLD) | set(EXP))
def N(x): return 0.5 * (1 + math.erf(x / math.sqrt(2)))
def bs(S, K, iv, days, cp):
    t = max(days, 1e-4) / 365; s = iv / 100
    d1 = (math.log(S / K) + 0.5 * s * s * t) / (s * math.sqrt(t)); d2 = d1 - s * math.sqrt(t)
    return S * N(d1) - K * N(d2) if cp == "CE" else K * N(-d2) - S * N(-d1)
def delta(S, K, iv, days, cp):
    t = max(days, 1e-4) / 365; s = iv / 100; d1 = (math.log(S / K) + 0.5 * s * s * t) / (s * math.sqrt(t))
    return N(d1) if cp == "CE" else N(d1) - 1
def tstamp(day, hm):
    hm = hm if (isinstance(hm, str) and ":" in hm) else "15:25"
    return pd.Timestamp(f"{day} {hm}")
def days_to(exp, ts):                      # calendar days incl. the session fraction left (expiry at 15:30)
    return max((exp + pd.Timedelta(hours=15.5) - ts).total_seconds() / 86400, 1e-4)
rows = []
for r in T.itertuples():
    if r.s == 13:
        if r.day not in BND.index: continue
        S0 = float(BND[r.day]); t0 = tstamp(r.day, "15:25"); t1 = tstamp(r.xhm, "15:25"); need = 15
    else:
        S0 = PX.get((r.day, r.hm)); t0 = tstamp(r.day, r.hm); t1 = tstamp(r.day, r.xhm); need = 0 if r.s == 12 else 3
        if S0 is None or not np.isfinite(S0): continue
    vd = VIX[VIX.index < r.day]
    if not len(vd): continue
    iv = float(vd.iloc[-1]) * 1.15
    exp = next((e for e in ALLEXP if days_to(e, t0) >= max(need, 1e-3) and e >= t0.normalize()), None)
    if exp is None: continue
    cp = "CE" if r.side > 0 else "PE"; dte0 = days_to(exp, t0)
    ks = [round(S0 / 100) * 100 + k * 100 for k in range(-15, 16)]
    K = min(ks, key=lambda k: abs(abs(delta(S0, k, iv, dte0, cp)) - 0.6))
    p0 = bs(S0, K, iv, dte0, cp); S1 = S0 + r.side * r.pts
    dte1 = days_to(exp, t1) if t1 < exp + pd.Timedelta(hours=15.5) else 1e-4
    p1 = max(bs(S1, K, iv, dte1, cp), max(0.0, (S1 - K) if cp == "CE" else (K - S1)))
    b, s_ = p0 * LOT, p1 * LOT; cost = TT._costs(b, s_, "BANKNIFTY", LOT)
    rows.append(dict(s=r.s, day=r.day, year=r.year, pts=r.pts, K=K, dte=round(dte0, 1), iv=round(iv, 1), prem=round(p0, 1), prem_exit=round(p1, 1),
                     gross=round(s_ - b), cost=round(cost), net=round(s_ - b - cost), ret_pct=round((s_ - b - cost) / b * 100, 1)))
O = pd.DataFrame(rows); O.to_csv(os.path.join(ROOT, "data_store/research/bn_pdf/option_buy_trades.csv"), index=False)
NAMES = {1: "ORB + VWAP", 2: "Opening-range reversal", 3: "2 PM breakout", 4: "5 EMA (Power of Stocks)", 5: "5 EMA + Bollinger",
         6: "9 EMA + pivot + RSI", 7: "VWAP pullback", 9: "Supertrend + VWAP", 10: "20 / 50 EMA", 12: "Monthly expiry afternoon", 13: "Daily Supertrend positional"}
S = pd.read_csv(os.path.join(ROOT, "data_store/research/bn_pdf/summary.csv"))
out = []
for sid, nm in NAMES.items():
    for yr in ("pre", "Y1", "Y2"):
        z = O[(O.s == sid) & (O.year == yr)]
        if not len(z): continue
        syn = S[(S.no == sid) & (S.year == yr)]
        out.append(dict(no=sid, strategy=nm, year=yr, n=len(z), win=round(100 * (z.net > 0).mean()), avg_prem=round(z.prem.mean()),
                        gross_rs=int(z.gross.mean()), cost_rs=int(z.cost.mean()), opt_net_rs=int(z.net.mean()), total=int(z.net.sum()),
                        synth_net_rs=int(syn.plain_rs.iloc[0]) if len(syn) else None))
R = pd.DataFrame(out); R.to_csv(os.path.join(ROOT, "data_store/research/bn_pdf/option_buy_summary.csv"), index=False)
pd.set_option("display.width", 220); print(R.to_string(index=False))
print("\nIV used: median", O.iv.median(), "| premium median", O.prem.median(), "| days to expiry median", O.dte.median())
