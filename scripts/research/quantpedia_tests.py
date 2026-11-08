"""QUANTPEDIA IDEAS ON OUR DATA (2026-11-08, Nikhil: "Research quantpedia .. any useful strategies" -> "Let's backtest").
Rules are Quantpedia's as published, fixed before looking at our results; only the clock / costs are adapted.

T1 Bitcoin evening hour (quantpedia.com/strategies/intraday-seasonality-in-bitcoin): long BTC at 22:00 UTC, flat at 24:00 UTC, every day.
   Data: Binance BTCUSDT 5-min 2020-01 -> 2026-10 + 1-hour to 7 Nov 2026. Also every other 2-hour start, to see if 22:00 is special.
T2 Bitcoin x-day high (quantpedia.com/revisiting-trend-following-and-mean-reversion-strategies-in-bitcoin): at the daily close (00:00
   UTC), if the close is the highest close of the last x days -> hold long for the next day, else flat (MAX); MIN = same at the x-day low.
   x = 10, 20, 30, 40, 50. BTC (2020+), ETH and SOL (Binance daily). Compared with buy and hold.
T3 Lunch effect (quantpedia.com/lunch-effect-in-the-u-s-stock-market-indices), Indian clock: Nifty / Bank Nifty front-month futures,
   short 11:00-12:00 IST, long 12:00-14:00 IST, flat at 14:00 (literal SPY hours). Plus every 1-hour window of the session.
Costs: crypto 0.10 % per round trip (taker); 0.04 % (maker) shown too. Futures: Nifty 2 pts / Bank Nifty 5 pts per round trip (futures,
charges + slippage); synthetic 4.5 / 12.5 shown too. Results by period halves; nothing re-fitted.
Run: python3 scripts/research/quantpedia_tests.py
"""
from __future__ import annotations

import importlib.util
import os

import numpy as np
import pandas as pd

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
MD = os.path.join(ROOT, "data_store", "market_data")
DER = os.path.join(ROOT, "data_store", "research", "deriv")


def btc_hourly():
    a = pd.read_csv(os.path.join(MD, "history", "binance_BTC_5m.csv.gz"), usecols=["ts", "open", "high", "low", "close"], parse_dates=["ts"])
    h = a.set_index("ts").resample("1h").agg({"open": "first", "close": "last"}).dropna()
    k = pd.read_csv(os.path.join(DER, "kl1h_BTC.csv")); k["ts"] = pd.to_datetime(k.ts, unit="ms")
    k = k.set_index("ts")[["o", "c"]].rename(columns={"o": "open", "c": "close"})
    return pd.concat([h, k[k.index > h.index.max()]]).sort_index()


def stats_daily(r, label, per_year=365):
    r = r.dropna()
    if not len(r):
        return f"{label}: none"
    eq = (1 + r).cumprod(); dd = (eq / eq.cummax() - 1).min()
    yrs = len(r) / per_year; cagr = eq.iloc[-1] ** (1 / yrs) - 1 if yrs > 0 else np.nan
    sh = r.mean() / r.std() * np.sqrt(per_year) if r.std() else np.nan
    return f"{label:46s} CAGR {100*cagr:+6.1f}% | Sharpe {sh:+.2f} | max DD {100*dd:6.1f}% | days {len(r)}"


def t1():
    print("=" * 110, "\nT1  BITCOIN EVENING HOUR: long 22:00 -> 24:00 UTC every day")
    h = btc_hourly(); h["d"] = h.index.normalize(); h["hr"] = h.index.hour
    rows = []
    for st in range(0, 24, 2):
        a = h[h.hr == st].set_index("d").open; b = h[h.hr == st + 1].set_index("d").close
        r = (b / a - 1).dropna(); rows.append((st, r))
    for per, lo, hi in (("2020-22", "2020", "2022-12-31"), ("2023-24", "2023", "2024-12-31"), ("2025-26", "2025", "2026-12-31"), ("ALL", "2020", "2026-12-31")):
        line = []
        for st, r in rows:
            x = r[lo:hi]; line.append(f"{st:02d}h {10000*x.mean():+5.1f}")
        print(f"  {per:8s} mean bps per 2-hour window (gross):  " + " ".join(line))
    r22 = dict(rows)[22]
    for per, lo, hi in (("2020-22", "2020", "2022-12-31"), ("2023-24", "2023", "2024-12-31"), ("2025-26", "2025", "2026-12-31"), ("ALL", "2020", "2026-12-31")):
        x = r22[lo:hi]
        tt = x.mean() / x.std() * np.sqrt(len(x))
        print(f"  22:00 {per:8s} n {len(x):4d} | gross {10000*x.mean():+5.1f} bps (t {tt:+.1f}) | win {100*(x>0).mean():.0f}% | "
              + stats_daily(x - 0.0010, "net 0.10 %").split(" CAGR")[1][:60] + " | net 0.04 %:" + stats_daily(x - 0.0004, "").split(" CAGR")[1][:30])


def t2():
    print("=" * 110, "\nT2  X-DAY HIGH / LOW (daily close, hold 1 day)")
    h = btc_hourly(); btc = h.close.resample("1D").last().dropna()
    d = pd.read_csv(os.path.join(MD, "binance_daily.csv"), parse_dates=["date"])
    S = {"BTC": btc, **{s[:3]: d[d.symbol == s].set_index("date").close for s in ("ETHUSDT", "SOLUSDT")}}
    for coin, c in S.items():
        c = c.sort_index(); ret = c.pct_change().shift(-1)            # next day's return, earned by a position taken at today's close
        print(f"\n  {coin}  {c.index.min():%Y-%m} .. {c.index.max():%Y-%m}")
        print("   ", stats_daily(ret, "buy and hold"))
        for x in (10, 20, 30, 40, 50):
            for kind in ("MAX", "MIN"):
                sig = (c >= c.rolling(x).max()) if kind == "MAX" else (c <= c.rolling(x).min())
                pos = sig.astype(float); trades = pos.diff().abs().fillna(0)
                r = pos * ret - trades * 0.0010 / 2                  # half the round trip on each entry / exit
                parts = []
                for lo, hi in (("2017", "2021-12-31"), ("2022", "2024-08-31"), ("2024-09", "2026-12-31")):
                    rr = r[lo:hi].dropna()
                    if len(rr) > 100:
                        parts.append(f"{lo[:4]}-{hi[:4]} Sh {rr.mean()/rr.std()*np.sqrt(365):+.2f}")
                print("   ", stats_daily(r, f"{kind} x={x} (in market {100*pos.mean():.0f}%, {int(trades.sum())} switches)"), "|", " · ".join(parts))


def t3():
    print("=" * 110, "\nT3  LUNCH EFFECT, Indian clock (front-month futures)")
    p = os.path.join(ROOT, "scripts", "research", "market_score_fit.py")
    sp = importlib.util.spec_from_file_location("msf", p); M = importlib.util.module_from_spec(sp); sp.loader.exec_module(M)
    for sym, cf, cs in (("NIFTY", 2.0, 4.5), ("BANKNIFTY", 5.0, 12.5)):
        b = M.india_15m(sym)
        b["hm"] = b.dt.dt.strftime("%H:%M")
        o = b.pivot_table(index="day", columns="hm", values="o", aggfunc="first"); c = b.pivot_table(index="day", columns="hm", values="c", aggfunc="last")
        def seg(a, z):        # points from the open of the bar starting at a to the close of the bar ending at z
            zb = (pd.Timestamp("2000-01-01 " + z) - pd.Timedelta(minutes=15)).strftime("%H:%M")
            return (c[zb] - o[a]).dropna()
        short = -seg("11:00", "12:00"); long_ = seg("12:00", "14:00")
        tot = (short + long_).dropna()
        days = sorted(tot.index); half = days[len(days) // 2]
        print(f"\n  {sym}: {days[0]} .. {days[-1]} ({len(days)} days)")
        for nm, x, k in (("short 11-12", short, 1), ("long 12-14", long_, 1), ("rule (both legs)", tot, 2)):
            for per, xx in (("1st half", x[x.index < half]), ("2nd half", x[x.index >= half]), ("all", x)):
                print(f"    {nm:17s} {per:8s} gross {xx.mean():+6.1f} pts/day (t {xx.mean()/xx.std()*np.sqrt(len(xx)):+.1f}) | win {100*(xx>0).mean():.0f}% "
                      f"| net futures {xx.mean()-k*cf:+6.1f} | net synthetic {xx.mean()-k*cs:+6.1f}")
        print("    every 1-hour window (gross pts/day, 1st half / 2nd half):")
        for a in ("09:15", "10:15", "11:15", "12:15", "13:15", "14:15"):
            z = (pd.Timestamp("2000-01-01 " + a) + pd.Timedelta(hours=1)).strftime("%H:%M")
            x = seg(a, z)
            print(f"      {a}-{z}  {x[x.index < half].mean():+6.1f} / {x[x.index >= half].mean():+6.1f}  (t all {x.mean()/x.std()*np.sqrt(len(x)):+.1f})")


if __name__ == "__main__":
    t1(); t2(); t3()
