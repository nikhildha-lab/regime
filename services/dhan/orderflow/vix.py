"""
VIX analysis - does India VIX explain when our trades win and lose?

Downloads daily India VIX from Dhan (once, cached in data/INDIAVIX_daily.csv),
joins it to trades.csv, and shows P&L by:
  - VIX level bucket   (low / normal / high / very high)
  - VIX trend          (rising or falling over the last 5 days)
  - month              (monthly P&L next to monthly average VIX)

Run after backtest.py:   python vix.py        -> vix_report.html
"""

from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

import config as C
from common import HERE, connect

VIX_ID = "21"                      # India VIX in Dhan's list (NSE index)
VIX_FILE = HERE / C.DATA_DIR / "INDIAVIX_daily.csv"
BUCKETS = [(0, 12, "low (<12)"), (12, 15, "normal (12-15)"), (15, 20, "high (15-20)"), (20, 99, "very high (20+)")]
MODES = ["BEES_MIS", "DEBIT_SPREAD", "CREDIT_SPREAD", "FAR_HEDGE", "NAKED_SELL"]


def get_vix(need_until):
    if VIX_FILE.exists():
        v = pd.read_csv(VIX_FILE, parse_dates=["date"])
        if v["date"].max().date() >= need_until - timedelta(days=3):
            return v
    _, dhan = connect()
    end = date.today()
    start = end - timedelta(days=365 * C.HISTORY_YEARS + 30)
    print("Downloading India VIX daily history...")
    r = dhan.historical_daily_data(VIX_ID, "IDX_I", "INDEX", str(start), str(end))
    if r.get("status") != "success":
        raise SystemExit(f"VIX download failed: {r.get('remarks')}")
    d = r["data"]
    v = pd.DataFrame({"date": pd.to_datetime(d["timestamp"], unit="s", utc=True).tz_convert("Asia/Kolkata").tz_localize(None).normalize(),
                      "open": d["open"], "high": d["high"], "low": d["low"], "close": d["close"]})
    v = v.drop_duplicates("date").sort_values("date")
    VIX_FILE.parent.mkdir(parents=True, exist_ok=True)
    v.to_csv(VIX_FILE, index=False)
    print(f"  {len(v)} days saved")
    return v


def bucket(x):
    for lo, hi, name in BUCKETS:
        if lo <= x < hi:
            return name
    return "?"


def pf(p):
    w, l = p[p > 0].sum(), -p[p < 0].sum()
    return w / l if l else np.inf


def table(df, by):
    g = df.groupby([by, "mode"])["pnl"]
    out = pd.DataFrame({"trades": g.size(), "net": g.sum(), "avg": g.mean(), "win": g.apply(lambda p: (p > 0).mean()), "pf": g.apply(pf)})
    return out.reset_index()


def main():
    t = pd.read_csv(HERE / "trades.csv", parse_dates=["entry_ts"])
    t = t[~t["setup"].str.startswith("ALL_COMBINED") & t["mode"].isin(MODES)].copy()
    t["date"] = t["entry_ts"].dt.normalize()
    v = get_vix(t["date"].max().date())
    # use YESTERDAY's close (known at the open), plus 5-day change
    v = v.sort_values("date")
    v["vix"] = v["close"].shift(1)
    v["vix_chg5"] = v["close"].shift(1) - v["close"].shift(6)
    t = t.merge(v[["date", "vix", "vix_chg5"]], on="date", how="left").dropna(subset=["vix"])
    t["level"] = t["vix"].apply(bucket)
    t["trend"] = np.where(t["vix_chg5"] > 0, "VIX rising (5d)", "VIX falling (5d)")
    t["month"] = t["date"].dt.to_period("M").astype(str)

    by_level = table(t, "level")
    by_trend = table(t, "trend")
    monthly = t.groupby(["month", "mode"])["pnl"].sum().unstack("mode").fillna(0)
    mvix = v.set_index(v["date"].dt.to_period("M").astype(str))["close"].groupby(level=0).mean().round(1)
    monthly.insert(0, "avg VIX", mvix.reindex(monthly.index))
    corr = {m: float(round(np.corrcoef(monthly["avg VIX"], monthly[m])[0, 1], 2)) for m in MODES if m in monthly}

    # ---- print ----
    print("\nP&L by VIX level (yesterday's close):")
    for m in MODES:
        d = by_level[by_level["mode"] == m]
        print(f"  {m}")
        for _, r in d.iterrows():
            print(f"    {r['level']:18s} trades {int(r['trades']):4d}  win {r['win']*100:5.1f}%  PF {r['pf']:.2f}  net Rs {r['net']:>10,.0f}")
    print("\nP&L by VIX trend:")
    for m in MODES:
        d = by_trend[by_trend["mode"] == m]
        for _, r in d.iterrows():
            print(f"  {m:14s} {r['trend']:16s} trades {int(r['trades']):4d}  PF {r['pf']:.2f}  net Rs {r['net']:>10,.0f}")
    print("\nCorrelation of monthly P&L with monthly VIX:", corr)

    # ---- html ----
    css = """
:root{--bg:#fbfaf8;--fg:#1d1d1b;--muted:#6b6a66;--card:#fff;--line:#e6e3dd;--up:#1f7a4d;--down:#b3372f;--chip:#f1eee8}
@media (prefers-color-scheme:dark){:root{--bg:#161615;--fg:#ecebe7;--muted:#9b9993;--card:#1f1f1d;--line:#33322f;--up:#4cc38a;--down:#ef6b5f;--chip:#2a2927}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
main{max-width:1100px;margin:0 auto;padding:28px 16px 60px}h1{font-size:24px;margin:0 0 4px}h2{font-size:18px;margin:34px 0 8px}
.note{background:var(--chip);border-radius:8px;padding:10px 14px;margin:14px 0}.wrap{overflow-x:auto;background:var(--card);border:1px solid var(--line);border-radius:10px}
table{border-collapse:collapse;width:100%}th,td{padding:7px 10px;border-bottom:1px solid var(--line);text-align:right;white-space:nowrap}
th{font-size:12px;color:var(--muted)}th.l,td.l{text-align:left}tr:last-child td{border-bottom:0}.up{color:var(--up)}.down{color:var(--down)}
"""
    cls = lambda x: "up" if x > 0 else "down"
    f1 = lambda x: "inf" if x == np.inf else f"{x:.2f}"
    h = [f"<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
         f"<title>VIX vs Results</title><style>{css}</style></head><body><main><h1>India VIX vs trade results</h1>"
         f"<div class='note'>VIX used is <b>yesterday's close</b> (known before the trade). Trades: {len(t)} across {t['date'].nunique()} days. "
         "Buckets: low &lt;12, normal 12-15, high 15-20, very high 20+.</div>"]
    for title, d, col in (("By VIX level", by_level, "level"), ("By VIX trend (last 5 days)", by_trend, "trend")):
        h.append(f"<h2>{title}</h2><div class='wrap'><table><tr><th class='l'>Trade as</th><th class='l'>VIX</th><th>Trades</th><th>Win %</th><th>PF</th><th>Net Rs</th><th>Avg / trade</th></tr>")
        for m in MODES:
            for _, r in d[d["mode"] == m].iterrows():
                h.append(f"<tr><td class='l'>{m}</td><td class='l'>{r[col]}</td><td>{int(r['trades'])}</td><td>{r['win']*100:.0f}%</td>"
                         f"<td>{f1(r['pf'])}</td><td class='{cls(r['net'])}'>{r['net']:,.0f}</td><td class='{cls(r['avg'])}'>{r['avg']:,.0f}</td></tr>")
        h.append("</table></div>")
    h.append("<h2>Month by month</h2><div class='note'>Correlation of monthly P&amp;L with monthly VIX: " +
             ", ".join(f"{k} <b>{v:+.2f}</b>" for k, v in corr.items()) +
             ". (+1 = wins when VIX is high, -1 = wins when VIX is low, 0 = no relationship)</div>")
    h.append("<div class='wrap'><table><tr><th class='l'>Month</th><th>Avg VIX</th>" + "".join(f"<th>{m}</th>" for m in MODES if m in monthly) + "</tr>")
    for mo, r in monthly.iterrows():
        h.append(f"<tr><td class='l'>{mo}</td><td>{r['avg VIX']}</td>" + "".join(f"<td class='{cls(r[m])}'>{r[m]:,.0f}</td>" for m in MODES if m in monthly) + "</tr>")
    h.append("</table></div></main></body></html>")
    Path(HERE / "vix_report.html").write_text("\n".join(h))
    print(f"\nReport: {HERE / 'vix_report.html'}")


if __name__ == "__main__":
    main()
