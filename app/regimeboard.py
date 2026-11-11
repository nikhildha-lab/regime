"""REGIME BOARD - one regime for every market we can price (2026-11-10, Nikhil: "need regime calculation of all
Indian indices ... then crypto ... metals ... US"). Phase 1: the Indian indices, from Dhan.

Groups (2026-11-10 evening, Nikhil: "crypto, metals, commodities and US market is missing ... where is the regime
score"): the board now carries every group the All Markets card carries, in the same order and with the same group
names - Indian market, Crypto, Metals, Commodities, US market - plus a score column.

    India         Dhan daily bars (15 indices, each with its top 10 constituents nested)
    Crypto        Binance daily klines, the 30 largest USDT pairs by 24 h turnover
    Metals        Yahoo futures (Binance has no silver, copper or aluminium - only PAXG gold)
    Commodities   Yahoo futures
    US            Yahoo indices

Score: one number, -10 to +10, built from the same daily bars as the regime, so every row is comparable:
the average of (price vs its 20-day mean), (20-day mean vs 50-day mean) and (20-day change), each divided by the
market's own daily volatility and capped at 2 sigma, then scaled. It is a description of the trend, not a signal.

Engine: the simple, transparent one described in app/stockview.py - 20-day realised volatility as a percentile of
its own last year, plus the daily trend:

    High Volatility   vol percentile >= 80
    Uptrend           close > 20-day mean > 50-day mean
    Downtrend         close < 20-day mean < 50-day mean
    Quiet             vol percentile <= 20
    Range             anything else

The full Market Pulse regime engine is NOT used here: it only exists for the few markets the pipeline tracks, and a
board has to compare like with like. Where the full engine has a verdict, the page shows it as a second chip.

Data: Dhan daily bars (app/dhan_live.daily). Daily bars change once a day, so each symbol is computed once per
session and cached in data_store/livedesk/regimeboard/.

Nothing here is a signal. It describes conditions.
"""
from __future__ import annotations

import json
import math
import os
import threading
import time
from datetime import date, datetime, timedelta

import pandas as pd

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
OUT = os.path.join(ROOT, "data_store", "livedesk", "regimeboard")
LATEST = os.path.join(OUT, "latest.json")

# display name -> the name Dhan's master file knows (lower case lookup happens in dhan_live.ids_index)
INDIA = [
    ("Nifty 50", "NIFTY 50"),
    ("Bank Nifty", "NIFTY BANK"),
    ("Fin Nifty", "NIFTY FIN SERVICE"),
    ("Nifty Next 50", "NIFTY NEXT 50"),
    ("Nifty Midcap 100", "NIFTY MIDCAP 100"),
    ("Nifty Smallcap 100", "NIFTY SMLCAP 100"),
    ("Nifty IT", "NIFTY IT"),
    ("Nifty Pharma", "NIFTY PHARMA"),
    ("Nifty Auto", "NIFTY AUTO"),
    ("Nifty FMCG", "NIFTY FMCG"),
    ("Nifty Metal", "NIFTY METAL"),
    ("Nifty Energy", "NIFTY ENERGY"),
    ("Nifty Realty", "NIFTY REALTY"),
    ("Nifty PSU Bank", "NIFTY PSU BANK"),
    ("India VIX", "INDIA VIX"),
]

# Yahoo tickers. Metals and commodities are the front-month futures; the dashboard prices them the same way.
METALS = [("Gold", "GC=F"), ("Silver", "SI=F"), ("Copper", "HG=F"), ("Platinum", "PL=F"), ("Palladium", "PA=F"),
          ("Aluminium", "ALI=F")]
COMMOD = [("Crude (WTI)", "CL=F"), ("Brent crude", "BZ=F"), ("Natural gas", "NG=F")]
US = [("S&P 500", "^GSPC"), ("Nasdaq 100", "^NDX"), ("Dow Jones", "^DJI"), ("Russell 2000", "^RUT"),
      ("S&P Midcap 400", "^SP400")]
# The 30 majors, plus the two the desk already follows (ZEC, DASH). A named list on purpose: Binance's turnover
# ranking is full of tokenised shares (NVDAB, SOXLB, MUB) and pegged tokens, which are not coins and would crowd
# out the real ones. Any name Binance does not quote in USDT is simply skipped.
INTRA_BUDGET = 150      # seconds for the 5-minute and 4-hour pass; whatever is not reached keeps its last value
CRYPTO = ["BTC", "ETH", "BNB", "SOL", "XRP", "DOGE", "ADA", "TRX", "AVAX", "LINK", "DOT", "POL", "LTC", "BCH",
          "NEAR", "UNI", "ICP", "APT", "ETC", "XLM", "HBAR", "FIL", "ARB", "OP", "INJ", "SUI", "AAVE", "TIA",
          "RENDER", "SEI", "ZEC", "DASH"]
BINANCE = "https://api.binance.com/api/v3"

DAYS = 420          # a year of sessions plus room for the 50-day mean
VOL_HI, VOL_LO = 80, 20
TOP_N = 10          # constituents shown under each index
MEM_DIR = os.path.join(ROOT, "data_store", "india", "index_members")
# Constituents come from NSE's published index CSVs - the same archive file and the same session app/stockview.py
# uses for the Nifty 50 list, which works on this Mac. (NSE's /api/equity-stockIndices endpoint returned nothing
# here, so it is only tried as a second choice and the reason is recorded on the row.)
NSE_MEMBERS = "https://www.nseindia.com/api/equity-stockIndices?index="
CSV_URL = "https://nsearchives.nseindia.com/content/indices/ind_%slist.csv"
CSV_SLUG = {"NIFTY 50": "nifty50", "NIFTY BANK": "niftybank", "NIFTY NEXT 50": "niftynext50",
            "NIFTY MIDCAP 100": "niftymidcap100", "NIFTY SMLCAP 100": "niftysmallcap100",
            # NSE's own file name for Fin Nifty is not the obvious one; the code tries each and keeps what answers
            "NIFTY FIN SERVICE": ["niftyfinance", "niftyfinancialservices", "niftyfinservice", "niftyfin"], "NIFTY IT": "niftyit", "NIFTY PHARMA": "niftypharma",
            "NIFTY AUTO": "niftyauto", "NIFTY FMCG": "niftyfmcg", "NIFTY METAL": "niftymetal",
            "NIFTY ENERGY": "niftyenergy", "NIFTY REALTY": "niftyrealty", "NIFTY PSU BANK": "niftypsubank"}
EOD = os.path.join(ROOT, "data_store", "market_data", "nse_stocks_eod.csv")
NOTE: dict = {}        # index name -> how its list was built, shown on the row
S: dict = {"t": 0}


def _p(day):
    return os.path.join(OUT, f"day_{day}.json")


def _load(day):
    try:
        return json.load(open(_p(day)))
    except Exception:
        return {"day": day, "rows": {}, "missing": {}}


def _save(day, st):
    os.makedirs(OUT, exist_ok=True)
    for path in (_p(day), LATEST):
        tmp = path + f".{os.getpid()}.tmp"
        json.dump(st, open(tmp, "w"), indent=1, default=str)
        os.replace(tmp, path)


# ---- the full engine, the one Nifty has always used (src/common/regime_engine/exposure_regime.py) ----
# Same five states, same thresholds, same order (first match wins). The only input this board cannot feed it is
# implied volatility, which exists for Nifty alone; every other gate runs on the daily bars already fetched.
ER_LINE, ADX_LINE, HV_LINE, COMP_VOL, COMP_ER, DD_LINE = 0.30, 22.5, 80, 30, 0.15, -0.10
EXPOSURE = {"Bull Trend": 1.0, "Compression": 0.9, "Range": 0.6, "High Volatility": 0.2, "Bear Trend": 0.15}
# how close an input has to be to its line before the label is called shaky (regime-read-plan.md)
NEAR = {"vol_pct": 5, "er_20": 0.03, "adx": 2, "ret_20d": 0.01, "px_vs_ema200": 0.01, "dd_60d": 0.02}


def _adx(h, l, c, n=14):
    up, dn = h.diff(), -l.diff()
    pdm = up.where((up > dn) & (up > 0), 0.0)
    ndm = dn.where((dn > up) & (dn > 0), 0.0)
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / n, adjust=False).mean()
    pdi = 100 * pdm.ewm(alpha=1 / n, adjust=False).mean() / atr.replace(0, float("nan"))
    ndi = 100 * ndm.ewm(alpha=1 / n, adjust=False).mean() / atr.replace(0, float("nan"))
    dx = 100 * (pdi - ndi).abs() / (pdi + ndi).replace(0, float("nan"))
    return dx.ewm(alpha=1 / n, adjust=False).mean()


def _er_series(c, n=20):
    return (c - c.shift(n)).abs() / c.diff().abs().rolling(n).sum().replace(0, float("nan"))


def _label_series(f: pd.DataFrame) -> pd.Series:
    hv = (f.vol_pct > HV_LINE) | ((f.vol_pct > 60) & (f.dd_60d < DD_LINE))
    trend = (f.er_20 > ER_LINE) & (f.adx > ADX_LINE)
    out = pd.Series("Range", index=f.index, dtype=object)
    out[(f.vol_pct < COMP_VOL) & (f.er_20 < COMP_ER)] = "Compression"
    out[trend & (f.ret_20d < 0) & (f.px_vs_ema200 < 0)] = "Bear Trend"
    out[trend & (f.ret_20d > 0) & (f.px_vs_ema200 > 0)] = "Bull Trend"
    out[hv] = "High Volatility"
    return out


def _zdir(x: pd.Series, win: int) -> float:
    """one feature's direction, -1 .. +1: 2*Phi(z) - 1 against its own last year (score_v2.py's method)"""
    m, sd = x.rolling(win, min_periods=max(20, win // 4)).mean(), x.rolling(win, min_periods=max(20, win // 4)).std()
    z = ((x - m) / sd.replace(0, float("nan"))).clip(-4, 4)
    v = z.iloc[-1]
    if pd.isna(v):
        return float("nan")
    return 2 * (0.5 * (1 + math.erf(float(v) / math.sqrt(2)))) - 1


def full_regime(df: pd.DataFrame) -> dict:
    """The full engine plus everything needed to show WHY: each gate's reading against its line, how close the
    nearest line is, how long the label has held, and the weighted direction across the timeframes this board has."""
    need = [k for k in ("high", "low", "close") if k in df.columns]
    if "close" not in need or len(df) < 220:
        return {}
    c = pd.to_numeric(df["close"], errors="coerce").dropna()
    h = pd.to_numeric(df["high"], errors="coerce").reindex(c.index).fillna(c) if "high" in df else c
    l = pd.to_numeric(df["low"], errors="coerce").reindex(c.index).fillna(c) if "low" in df else c
    lr = (c / c.shift()).apply(lambda v: math.log(v) if v and v > 0 else float("nan"))
    f = pd.DataFrame(index=c.index)
    f["vol_20d"] = lr.rolling(20).std() * (252 ** 0.5)
    f["vol_pct"] = f.vol_20d.rolling(252, min_periods=120).apply(lambda a: (a <= a[-1]).mean() * 100, raw=True)
    f["er_20"] = _er_series(c)
    f["adx"] = _adx(h, l, c)
    f["ret_20d"] = c / c.shift(20) - 1
    f["px_vs_ema200"] = c / c.ewm(span=200, adjust=False).mean() - 1
    f["dd_60d"] = c / c.rolling(60, min_periods=20).max() - 1
    lab = _label_series(f)
    last, row = lab.iloc[-1], f.iloc[-1]
    if pd.isna(row.vol_pct) or pd.isna(row.adx):
        return {}
    held = 1
    for i in range(len(lab) - 2, -1, -1):
        if lab.iloc[i] != last:
            break
        held += 1
    gates = [("Volatility percentile", "vol_pct", float(row.vol_pct), HV_LINE, "above"),
             ("Efficiency ratio", "er_20", float(row.er_20), ER_LINE, "above"),
             ("ADX", "adx", float(row.adx), ADX_LINE, "above"),
             ("20-day return", "ret_20d", float(row.ret_20d) * 100, 0.0, "above"),
             ("Price vs 200-day", "px_vs_ema200", float(row.px_vs_ema200) * 100, 0.0, "above"),
             ("60-day drawdown", "dd_60d", float(row.dd_60d) * 100, DD_LINE * 100, "below")]
    near = [g[0] for g in gates
            if abs(g[2] - g[3]) <= NEAR[g[1]] * (100 if g[1] in ("ret_20d", "px_vs_ema200", "dd_60d") else 1)]
    # direction and confidence, the weighted way (score_v2.py). Only the legs this board can build from daily bars:
    # the 5m / 15m / 75m legs need tick data it does not fetch, so the daily and weekly weights are renormalised.
    legs = {}
    try:
        d1 = [_zdir(c / c.ewm(span=50, adjust=False).mean(), 252), _zdir(c / c.ewm(span=200, adjust=False).mean(), 252),
              _zdir(c / c.shift(20) - 1, 252)]
        d1 = [x for x in d1 if not pd.isna(x)]
        if d1:
            legs["1D"] = (sum(d1) / len(d1) * 100, 0.35)
        w = c.resample("W").last().dropna()
        if len(w) > 60:
            d2 = [_zdir(w / w.shift(26) - 1, 52), _zdir(w / w.rolling(40).mean(), 52)]
            d2 = [x for x in d2 if not pd.isna(x)]
            if d2:
                legs["1W"] = (sum(d2) / len(d2) * 100, 0.20)
    except Exception:
        pass
    direction = conf = agree = None
    if legs:
        tot = sum(wt for _, wt in legs.values())
        direction = round(sum(v * wt for v, wt in legs.values()) / tot, 1)
        sgn = (1 if direction >= 5 else -1 if direction <= -5 else 0)
        agree = round(100 * (sum(wt for v, wt in legs.values() if sgn and (v > 0) == (sgn > 0)) / tot if sgn else 0))
        conf = round(min(100.0, abs(direction) * 1.5) * agree / 100)
    # 2026-11-11 (Nikhil): trend and volatility are two different facts, so they get two labels instead of one.
    # Same gates, same thresholds - only the collapsing into a single word is dropped.
    trend_on = (row.er_20 > ER_LINE) and (row.adx > ADX_LINE)
    t_state = ("Up" if trend_on and row.ret_20d > 0 and row.px_vs_ema200 > 0 else
               "Down" if trend_on and row.ret_20d < 0 and row.px_vs_ema200 < 0 else "Flat")
    vp = float(row.vol_pct)
    v_state = "Hot" if vp >= HV_LINE else "Busy" if vp >= 60 else "Normal" if vp >= COMP_VOL else "Calm"
    v_dir = "steady"
    try:
        then = float(f.vol_20d.iloc[-11])
        now = float(row.vol_20d)
        if then and now / then > 1.10:
            v_dir = "rising"
        elif then and now / then < 0.90:
            v_dir = "easing"
    except Exception:
        pass
    # 2026-11-11 (Nikhil, UI review item 8): the same two labels one bar earlier, so the board can tag a market whose
    # regime changed since the previous session. Uses only bars already closed before the latest one - no look-ahead.
    p_trend = p_vol = None
    try:
        pr = f.iloc[-2]
        if not (pd.isna(pr.vol_pct) or pd.isna(pr.adx)):
            p_on = (pr.er_20 > ER_LINE) and (pr.adx > ADX_LINE)
            p_trend = ("Up" if p_on and pr.ret_20d > 0 and pr.px_vs_ema200 > 0 else
                       "Down" if p_on and pr.ret_20d < 0 and pr.px_vs_ema200 < 0 else "Flat")
            pv = float(pr.vol_pct)
            p_vol = "Hot" if pv >= HV_LINE else "Busy" if pv >= 60 else "Normal" if pv >= COMP_VOL else "Calm"
    except Exception:
        pass
    # 2026-11-11 (Nikhil, UI review): how many sessions the current trend label has held, on the same rules, bar by bar
    trend_days = None
    try:
        on_s = (f.er_20 > ER_LINE) & (f.adx > ADX_LINE)
        up_s = on_s & (f.ret_20d > 0) & (f.px_vs_ema200 > 0)
        dn_s = on_s & (f.ret_20d < 0) & (f.px_vs_ema200 < 0)
        ts_s = pd.Series("Flat", index=f.index).mask(up_s, "Up").mask(dn_s, "Down")
        ok = f.vol_pct.notna() & f.adx.notna()
        ts_s = ts_s[ok]
        if len(ts_s) and ts_s.iloc[-1] == t_state:
            trend_days = 1
            for i in range(len(ts_s) - 2, -1, -1):
                if ts_s.iloc[i] != t_state:
                    break
                trend_days += 1
    except Exception:
        trend_days = None
    return {"char": last, "exposure": EXPOSURE.get(last), "held_days": held,
            "trend_state": t_state, "vol_state": v_state, "vol_dir": v_dir,
            "prev_trend": p_trend, "prev_vol": p_vol, "trend_days": trend_days,
            "changed": bool(p_trend and (p_trend != t_state or p_vol != v_state)),
            "near_line": near, "steady": not near,
            "gates": [{"name": n, "key": k, "value": round(v, 2), "line": ln, "want": w} for n, k, v, ln, w in gates],
            "direction": direction, "confidence": conf, "agree_pct": agree,
            "legs": {k: round(v, 1) for k, (v, _) in legs.items()},
            "word": None if agree is None else ("aligned" if agree >= 75 else "mostly aligned" if agree >= 55 else "conflicting")}


# ---- the same score on every timeframe (2026-11-10, Nikhil: "regime score for 5m, 4h, daily, weekly, monthly") ----
# One formula, each timeframe measured in ITS OWN bars and its own volatility, so the five numbers are comparable.
def _bar_score(c: pd.Series, short: bool = False):
    """short=True is the monthly leg: a year of daily bars is only ~20 months, far too few for 20- and 50-bar
    means, so the monthly score uses 6- and 12-month means and the last 6 months' change. Stated on the card."""
    c = pd.to_numeric(c, errors="coerce").dropna()
    a, b, n = (6, 12, 18) if short else (20, 50, 60)
    if len(c) < n:
        return None
    px, m20, m50 = float(c.iloc[-1]), float(c.tail(a).mean()), float(c.tail(b).mean())
    sd = float(c.pct_change().tail(n).std() * 100)
    if not sd or sd != sd:
        return None
    chg20 = (px / float(c.iloc[-a - 1]) - 1) * 100
    parts = [(px / m20 - 1) * 100 / (sd * math.sqrt(a)),
             (m20 / m50 - 1) * 100 / (sd * math.sqrt(b - a + 10)),
             chg20 / (sd * math.sqrt(a))]
    z = sum(min(max(x, -2.0), 2.0) for x in parts) / len(parts)
    return round(max(-10.0, min(10.0, z * 5)), 1)


def _swing_scores(df: pd.DataFrame) -> dict:
    """daily / weekly / monthly, all from the daily bars already fetched."""
    c = pd.to_numeric(df["close"], errors="coerce").dropna()
    out = {"d": _bar_score(c)}
    try:
        out["w"] = _bar_score(c.resample("W").last().dropna())
    except Exception:
        out["w"] = None
    out["m"] = None
    for rule in ("ME", "M"):
        try:
            out["m"] = _bar_score(c.resample(rule).last().dropna(), short=True)
            break
        except Exception:
            continue
    return out


def _intraday_scores(group: str, sym: str) -> dict:
    """5-minute and 4-hour, from each market's own feed. Top-level rows only - the constituents would be hundreds
    of extra calls for a number that changes faster than the page is read."""
    out = {"5m": None, "4h": None}
    try:
        if group == "crypto":
            import requests
            for key, iv in (("5m", "5m"), ("4h", "4h")):
                r = requests.get(f"{BINANCE}/klines", params={"symbol": sym, "interval": iv, "limit": 300}, timeout=20)
                r.raise_for_status()
                out[key] = _bar_score(pd.Series([float(k[4]) for k in r.json()]))
        elif group == "india":
            import dhan_live as DL
            sid = DL.ids_index(sym)
            if sid:
                end = date.today()
                m5 = DL.intraday("IDX_I", sid, "INDEX", end - timedelta(days=5), end, 5, oi=False)
                out["5m"] = _bar_score(pd.Series([b["close"] for b in m5]))
                time.sleep(0.4)
                h1 = DL.intraday("IDX_I", sid, "INDEX", end - timedelta(days=90), end, 60, oi=False)
                d = pd.DataFrame(h1)
                if len(d):
                    d["ts"] = pd.to_datetime(d.ts)
                    out["4h"] = _bar_score(d.set_index("ts")["close"].resample("4h").last().dropna())
        else:
            import updater as U
            df5, _ = U.yahoo_chart(sym, "5m", "5d")
            out["5m"] = _bar_score(df5["close"])
            df1, _ = U.yahoo_chart(sym, "1h", "3mo")
            d = df1.copy()
            d["ts"] = pd.to_datetime(d["ts"])
            out["4h"] = _bar_score(d.set_index("ts")["close"].resample("4h").last().dropna())
    except Exception:
        pass
    return out


# ---- 5M and 4H for the stocks under each index (2026-11-11, Nikhil: "yes", the lighter version) ----
# Runs after the headline markets, on its own time budget, so their 5M / 4H are never delayed. One Dhan call per stock
# for 5M each pass; 4H once an hour per stock (a 4-hour bar barely moves in 15 minutes) from 80 days of hourly bars,
# which fits in one call. Outside market hours it runs once after the close, then waits for the next session.
# Oldest-first, so a pass cut short by the budget carries on where it stopped next time.
STOCK_BUDGET = 180
STOCK_4H_EVERY = 3600


def _last_close(now: datetime) -> datetime:
    d = now.date()
    close = datetime.combine(d, datetime.min.time()).replace(hour=15, minute=30)
    if now.weekday() < 5 and now >= close:
        return close
    d -= timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return datetime.combine(d, datetime.min.time()).replace(hour=15, minute=30)


def _stock_intraday(st: dict, rows: dict) -> bool:
    now = datetime.now()
    hm = now.strftime("%H:%M")
    live = now.weekday() < 5 and "09:15" <= hm <= "15:30"
    last = st.get("stock_intra_at")
    if not live and last and last >= _last_close(now).isoformat(timespec="seconds"):
        return False
    import dhan_live as DL
    syms = []
    for r in rows.values():
        for k in r.get("children") or []:
            if k.get("sym") and k["sym"] not in syms:
                syms.append(k["sym"])
    a5 = st.setdefault("stock_5m_at", {})
    a4 = st.setdefault("stock_4h_at", {})
    res = st.setdefault("stock_tf", {})
    syms.sort(key=lambda x: a5.get(x, 0))
    t0, end, finished = time.time(), date.today(), True
    for sym in syms:
        if time.time() - t0 > STOCK_BUDGET:
            finished = False
            break
        try:
            sid = DL.ids_equity(sym)
            if sid:
                out = res.setdefault(sym, {})
                m5 = DL.intraday("NSE_EQ", sid, "EQUITY", end - timedelta(days=5), end, 5, oi=False)
                out["5m"] = _bar_score(pd.Series([b["close"] for b in m5])) if m5 else None
                if time.time() - a4.get(sym, 0) > STOCK_4H_EVERY:
                    d = pd.DataFrame(DL.intraday("NSE_EQ", sid, "EQUITY", end - timedelta(days=80), end, 60, oi=False))
                    if len(d):
                        d["ts"] = pd.to_datetime(d.ts)
                        out["4h"] = _bar_score(d.set_index("ts")["close"].resample("4h").last().dropna())
                    a4[sym] = time.time()
        except Exception:
            pass                                   # one bad symbol never holds up the queue
        a5[sym] = time.time()
    for r in rows.values():
        for k in r.get("children") or []:
            v = res.get(k.get("sym"))
            if v:
                k.setdefault("tf", {}).update({x: v.get(x) for x in ("5m", "4h")})
    for sym, k in (st.get("stocks") or {}).items():
        v = res.get(sym)
        if v and isinstance(k, dict):
            k.setdefault("tf", {}).update({x: v.get(x) for x in ("5m", "4h")})
    if finished:
        st["stock_intra_at"] = now.isoformat(timespec="seconds")
    return True


def _score(px, m20, m50, chg20, vol_ann):
    """-10 .. +10. Three trend readings, each divided by this market's own daily volatility and capped at 2 sigma."""
    sd = max(float(vol_ann) / (252 ** 0.5), 1e-6)          # daily move, per cent
    parts = [(px / m20 - 1) * 100 / (sd * math.sqrt(20)),
             (m20 / m50 - 1) * 100 / (sd * math.sqrt(30))]
    if chg20 is not None:
        parts.append(chg20 / (sd * math.sqrt(20)))
    z = sum(min(max(x, -2.0), 2.0) for x in parts) / len(parts)
    return round(max(-10.0, min(10.0, z * 5)), 1)


def regime_of(df: pd.DataFrame) -> dict:
    """df: daily bars with a 'close' column, oldest first. Returns the row the board shows."""
    c = pd.to_numeric(df["close"], errors="coerce").dropna()
    if len(c) < 60:
        return {"regime": None, "note": "not enough history"}
    ret = c.pct_change()
    vol = ret.rolling(20).std() * (252 ** 0.5) * 100
    v = vol.dropna()
    if v.empty:
        return {"regime": None, "note": "not enough history"}
    last_vol = float(v.iloc[-1])
    window = v.tail(252)
    pct = round(float((window <= last_vol).mean() * 100))
    m20, m50 = float(c.tail(20).mean()), float(c.tail(50).mean())
    px = float(c.iloc[-1])
    up, dn = px > m20 > m50, px < m20 < m50
    regime = ("High Volatility" if pct >= VOL_HI else
              "Uptrend" if up else
              "Downtrend" if dn else
              "Quiet" if pct <= VOL_LO else "Range")
    chg = lambda n: round((px / float(c.iloc[-n - 1]) - 1) * 100, 2) if len(c) > n else None
    c20 = chg(20)
    try:
        full = full_regime(df)
    except Exception:
        full = {}
    try:
        full["tf"] = _swing_scores(df)
    except Exception:
        pass
    return {**full, "regime": regime, "trend": "Up" if up else "Down" if dn else "Flat",
            "vol_pct": pct, "vol": round(last_vol, 1), "px": round(px, 2),
            "score": _score(px, m20, m50, c20, last_vol),
            "chg_1d": chg(1), "chg_20d": chg(20), "chg_60d": chg(60),
            "bars": int(len(c)), "as_of": str(df.index[-1])[:10] if len(df.index) else None}


def _traded_value() -> dict:
    """symbol -> last session's traded value (close x volume) from the EOD file the desk already keeps."""
    try:
        d = pd.read_csv(EOD, usecols=["date", "symbol", "close", "volume"])
        d = d[d["date"] == d["date"].max()]
        return {str(r.symbol).strip(): float(r.close) * float(r.volume) for r in d.itertuples()}
    except Exception:
        return {}


def _members_csv(index_name: str) -> list:
    """Symbols from NSE's published index CSV. Returns [] when the archive has no such file."""
    slugs = CSV_SLUG.get(index_name)
    if not slugs:
        return []
    if isinstance(slugs, str):
        slugs = [slugs]
    import updater as U
    last = ""
    for slug in slugs:
        r = U._CSESSION.get(CSV_URL % slug, timeout=20, headers={"Referer": "https://www.nseindia.com/"})
        if r.status_code == 200 and "Symbol" in r.text:
            import io as _io
            d = pd.read_csv(_io.StringIO(r.text))
            return [str(x).strip() for x in d["Symbol"].astype(str).tolist() if str(x).strip()]
        last = f"HTTP {r.status_code}"
    raise LookupError(last or "no file")


def _members_live(index_name: str) -> list:
    """[{sym, value}] from NSE's live feed, when it answers. Traded value comes straight from it."""
    import urllib.parse
    import updater as U
    r = U._CSESSION.get(NSE_MEMBERS + urllib.parse.quote(index_name), timeout=20,
                        headers={"Referer": "https://www.nseindia.com/market-data/live-equity-market"})
    if r.status_code != 200:
        raise LookupError(f"HTTP {r.status_code}")
    rows = []
    for d in (r.json().get("data") or []):
        sym = (d.get("symbol") or "").strip()
        if not sym or sym.upper() == index_name.upper() or sym.startswith("NIFTY"):
            continue                                   # the first row is the index itself
        try:
            val = float(str(d.get("totalTradedValue") or 0).replace(",", ""))
        except ValueError:
            val = 0.0
        rows.append({"sym": sym, "value": val})
    return rows


def _members(index_name: str) -> list:
    """[{sym, value}] for an index, ranked by traded value, cached for the day.
    'Top 10' means the ten largest by the last session's TRADED VALUE, not by index weight: NSE does not publish
    weights in either feed, and traded value is the honest thing we can measure. The page says so.
    """
    os.makedirs(MEM_DIR, exist_ok=True)
    slug = "".join(ch if ch.isalnum() else "_" for ch in index_name.lower())
    path = os.path.join(MEM_DIR, f"{slug}.json")
    today = datetime.now().strftime("%Y-%m-%d")
    try:
        j = json.load(open(path))
        if j.get("day") == today and j.get("rows"):
            NOTE[index_name] = j.get("note") or ""
            return j["rows"]
    except Exception:
        j = None
    rows, note, why = [], "", []
    try:                                               # the CSV archive works on this Mac; try it first
        syms = _members_csv(index_name)
        if syms:
            tv = _traded_value()
            rows = [{"sym": x, "value": tv.get(x, 0.0)} for x in syms]
            note = ("ranked by the last session's traded value" if tv
                    else "NSE list order - no traded value available")
    except Exception as e:
        why.append(f"csv {type(e).__name__}")
    if not rows:
        try:
            rows = _members_live(index_name)
            note = "ranked by today's traded value (NSE live feed)"
        except Exception as e:
            why.append(f"live {type(e).__name__}: {e}"[:60])
    rows.sort(key=lambda x: -x["value"])
    NOTE[index_name] = note or ("list not available: " + " / ".join(why))
    if rows:
        json.dump({"day": today, "index": index_name, "note": NOTE[index_name], "rows": rows}, open(path, "w"), indent=1)
    elif j and j.get("rows"):
        return j["rows"]                               # keep yesterday's list rather than show nothing
    return rows


def _stock_bars(sym: str) -> pd.DataFrame:
    import dhan_live as DL
    sid = DL.ids_equity(sym)
    if not sid:
        raise LookupError("no Dhan security id")
    rows = DL.daily("NSE_EQ", sid, "EQUITY", date.today() - timedelta(days=int(DAYS * 1.6)), date.today())
    if not rows:
        raise LookupError("no daily bars")
    d = pd.DataFrame(rows)
    d["ts"] = pd.to_datetime(d.ts.astype(str).str[:10])
    return d.set_index("ts").sort_index().tail(DAYS)


def _india_bars(dhan_name: str) -> pd.DataFrame:
    import dhan_live as DL
    sid = DL.ids_index(dhan_name)
    if not sid:
        raise LookupError("no Dhan security id")
    rows = DL.daily("IDX_I", sid, "INDEX", date.today() - timedelta(days=int(DAYS * 1.6)), date.today())
    if not rows:
        raise LookupError("no daily bars")
    d = pd.DataFrame(rows)
    d["ts"] = pd.to_datetime(d.ts.astype(str).str[:10])
    return d.set_index("ts").sort_index().tail(DAYS)


def _yahoo_bars(sym: str) -> pd.DataFrame:
    """Daily bars from Yahoo (metals, commodities, US indices)."""
    import updater as U
    df, _ = U.yahoo_chart(sym, "1d", "2y")
    if df is None or df.empty:
        raise LookupError("no daily bars")
    d = df.copy()
    d["ts"] = pd.to_datetime(d["ts"]).dt.tz_localize(None)
    return d.set_index("ts").sort_index().tail(DAYS)


def _binance_bars(pair: str) -> pd.DataFrame:
    """Daily klines from Binance (crypto)."""
    import requests
    r = requests.get(f"{BINANCE}/klines", params={"symbol": pair, "interval": "1d", "limit": DAYS}, timeout=20)
    r.raise_for_status()
    k = r.json()
    if not k:
        raise LookupError("no daily bars")
    d = pd.DataFrame(k, columns=["ot", "open", "high", "low", "close", "volume", "ct", "qv", "n", "bb", "bq", "ig"])
    d["ts"] = pd.to_datetime(d["ot"], unit="ms")
    for col in ("open", "high", "low", "close", "volume"):
        d[col] = pd.to_numeric(d[col], errors="coerce")
    return d.set_index("ts").sort_index()[["open", "high", "low", "close", "volume"]]


def _crypto_list() -> list:
    """[(label, pair)] - the named majors, in Binance 24 h turnover order, keeping only pairs Binance actually quotes."""
    import requests
    r = requests.get(f"{BINANCE}/ticker/24hr", timeout=25)
    r.raise_for_status()
    live = {}
    for t in r.json():
        try:
            live[t.get("symbol") or ""] = float(t.get("quoteVolume") or 0)
        except ValueError:
            pass
    out = [(b, b + "USDT", live.get(b + "USDT", 0.0)) for b in CRYPTO if (b + "USDT") in live]
    out.sort(key=lambda x: -x[2])
    return [(b, s) for b, s, _ in out]


GROUPS = [("india", "Indian market"), ("crypto", "Crypto"), ("metals", "Metals"),
          ("global", "Commodities"), ("us", "US market")]


def _simple_group(st, key, items, loader):
    """One pass over a flat group (crypto / metals / commodities / US). Cached per day like the Indian rows."""
    rows, missing = st["rows"], st["missing"]
    order = []
    for label, sym in items:
        rid = f"{key}|{label}"
        order.append(rid)
        if rid in rows:
            continue
        try:
            r = regime_of(loader(sym))
            r.update(name=label, sym=sym, group=key, src=("binance" if key == "crypto" else "yahoo"))
            rows[rid] = r
            missing.pop(rid, None)
        except Exception as e:
            missing[rid] = f"{type(e).__name__}: {e}"[:120]
        time.sleep(0.25)
    st.setdefault("order", {})[key] = order


def _build():
    day = datetime.now().strftime("%Y-%m-%d")
    st = _load(day)
    rows, missing = st.get("rows") or {}, st.get("missing") or {}
    stocks = st.get("stocks") or {}            # one regime per stock per day, shared across indices
    for label, dhan_name in INDIA:
        if label not in rows:
            try:
                r = regime_of(_india_bars(dhan_name))
                r.update(name=label, sym=dhan_name, group="india", src="dhan")
                rows[label] = r
                missing.pop(label, None)
            except Exception as e:
                missing[label] = f"{type(e).__name__}: {e}"[:120]
            time.sleep(0.6)                    # Dhan rate limit - the first build fills slowly on purpose
        if label == "India VIX" or not rows.get(label):
            continue
        if rows[label].get("children"):        # already nested today
            continue
        kids = []
        try:
            for m in _members(dhan_name)[:TOP_N]:
                sym = m["sym"]
                if sym not in stocks:
                    try:
                        k = regime_of(_stock_bars(sym))
                    except Exception as e:
                        k = {"regime": None, "note": f"{type(e).__name__}"[:40]}
                    k.update(name=sym, sym=sym, group="india", src="dhan")
                    stocks[sym] = k
                    time.sleep(0.6)
                kids.append(dict(stocks[sym], traded_value=round(m["value"])))
        except Exception as e:
            rows[label]["children_error"] = f"{type(e).__name__}: {e}"[:100]
        if NOTE.get(dhan_name):
            rows[label]["members_note"] = NOTE[dhan_name]
        if kids:
            rows[label]["children"] = kids
            counts = {}
            for k in kids:
                if k.get("regime"):
                    counts[k["regime"]] = counts.get(k["regime"], 0) + 1
            rows[label]["breadth"] = counts
    # the other groups - flat for now; nesting (crypto, metals, US constituents) comes next
    try:
        cl = st.get("crypto_list") or _crypto_list()
        st["crypto_list"] = cl
    except Exception as e:
        cl = st.get("crypto_list") or []
        missing["crypto|list"] = f"{type(e).__name__}: {e}"[:120]
    st["rows"], st["missing"] = rows, missing
    _simple_group(st, "crypto", cl, _binance_bars)
    _simple_group(st, "metals", METALS, _yahoo_bars)
    _simple_group(st, "global", COMMOD, _yahoo_bars)
    _simple_group(st, "us", US, _yahoo_bars)
    rows, missing = st["rows"], st["missing"]
    st["stocks"] = stocks
    st.update(day=day, rows=rows, missing=missing, at=datetime.now().isoformat(timespec="seconds"),
              children_note=f"top {TOP_N} by the last session's traded value on NSE - not by index weight",
              engine="simple: 20-day realised vol percentile + daily trend (app/stockview.py rule)",
              score_note="-10 to +10: price vs 20-day mean, 20-day mean vs 50-day mean and the 20-day change, "
                         "each divided by this market's own daily volatility and capped at 2 sigma")
    _save(day, st)                       # the board is usable before the slow part below
    # The two intraday numbers are recomputed every pass - unlike the daily bars, they move during the session.
    # They run last, on a time budget, so a slow feed delays only these two columns and never the board itself.
    t0 = time.time()
    for key, _name in GROUPS:
        ids = [l for l, _ in INDIA] if key == "india" else (st.get("order", {}).get(key) or [])
        for rid in ids:
            if time.time() - t0 > INTRA_BUDGET:
                break
            r = rows.get(rid)
            if not r or not r.get("sym"):
                continue
            try:
                r.setdefault("tf", {}).update(_intraday_scores(key, r["sym"]))
            except Exception:
                pass
    st.update(rows=rows, at=datetime.now().isoformat(timespec="seconds"))
    _save(day, st)
    try:                                   # then the stocks, on their own budget (the "Updated" time stays the markets')
        if _stock_intraday(st, rows):
            st["rows"] = rows
            _save(day, st)
    except Exception as e:
        st.setdefault("missing", {})["stocks|5m-4h"] = f"{type(e).__name__}: {e}"[:120]
        _save(day, st)


def _work():
    try:
        _build()
    except Exception as e:
        S["error"] = f"{type(e).__name__}: {e}"[:160]
    finally:
        S["busy"] = False


def _mod(name: str):
    """An app module reloaded when its file changes (same as _boardlive, any name)."""
    import importlib
    import sys
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), name + ".py")
    mt = os.path.getmtime(p)
    m = sys.modules.get(name)
    if m is None:
        m = importlib.import_module(name)
    elif getattr(m, "_MT", None) != mt:
        m = importlib.reload(m)
    m._MT = mt
    return m


_OFP = {"busy": False, "t": 0.0, "err": None}


def _ofp_work():
    try:
        _mod("ofpatterns").tick()
        _OFP["err"] = None
        try:                                 # 2026-11-11: 12-month Binance footprint rebuild (app/ofhistory.py) - starts once, resumes after a restart
            _mod("ofhistory").start()
        except Exception as e:
            _OFP["hist_err"] = f"{type(e).__name__}: {e}"[:160]
        try:                                 # 2026-11-11: the standing Order Flow Research study (app/ofstudy.py), every 6 hours
            _mod("ofstudy").start()
        except Exception as e:
            _OFP["study_err"] = f"{type(e).__name__}: {e}"[:160]
    except Exception as e:
        _OFP["err"] = f"{type(e).__name__}: {e}"[:160]
    finally:
        _OFP["busy"] = False


_STF = {"busy": False}


def _stf_work():
    try:                                     # 2026-11-11: ST + Fisher + ADX Nifty option buying, live paper rule (app/stfisher.py) - ticks every 30 s
        _mod("stfisher").tick()
    except Exception as e:
        _OFP["stf_err"] = f"{type(e).__name__}: {e}"[:160]
    finally:
        _STF["busy"] = False


def tick(force=False) -> bool:
    """from the live-desk loop: every 15 min in Indian hours, hourly otherwise"""
    if not _STF["busy"]:
        _STF["busy"] = True
        threading.Thread(target=_stf_work, name="stfisher", daemon=True).start()
    # 2026-11-11: the order-flow pattern engine (app/ofpatterns.py) rides on this loop - every 20 s, in its own thread,
    # so paper trades are recorded without a dashboard restart. Paper only.
    if not _OFP["busy"] and time.time() - _OFP["t"] >= 20:
        _OFP["t"], _OFP["busy"] = time.time(), True
        threading.Thread(target=_ofp_work, name="ofpatterns", daemon=True).start()
    now = datetime.now()
    hm = now.strftime("%H:%M")
    gap = 900 if ("09:00" <= hm <= "16:00" and now.weekday() < 5) else 3600
    if S.get("busy") or (not force and time.time() - S["t"] < gap):
        return False
    S["t"] = time.time()
    S["busy"] = True
    threading.Thread(target=_work, name="regimeboard", daemon=True).start()
    return True


def api() -> dict:
    try:
        st = json.load(open(LATEST))
    except Exception:
        return {"groups": [], "note": "the board is still being built - it fills over the first few minutes",
                "error": S.get("error")}
    R, order = st.get("rows") or {}, st.get("order") or {}
    groups = []
    for key, name in GROUPS:
        if key == "india":
            rows = [R[l] for l, _ in INDIA if l in R]
        else:
            rows = [R[rid] for rid in (order.get(key) or []) if rid in R]
        groups.append({"key": key, "name": name, "rows": rows})
    return {"at": st.get("at"), "day": st.get("day"), "engine": st.get("engine"),
            "children_note": st.get("children_note"), "score_note": st.get("score_note"),
            "groups": groups, "missing": st.get("missing") or {}, "error": S.get("error")}


# ---- one instrument's own page (2026-11-10 evening: every board row is a link) ----
_D: dict = {}          # (group, sym, kind) -> (day, payload); daily bars change once a day


def _bars_for(group: str, sym: str, kind: str = ""):
    if group == "india":
        return _stock_bars(sym) if kind == "stock" else _india_bars(sym)
    if group == "crypto":
        return _binance_bars(sym)
    return _yahoo_bars(sym)


def _boardlive():
    """app/boardlive.py, reloaded when its file changes (the live-price feeds for the board)."""
    import importlib
    import sys
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "boardlive.py")
    mt = os.path.getmtime(p)
    m = sys.modules.get("boardlive")
    if m is None:
        m = importlib.import_module("boardlive")
    elif getattr(m, "_MT", None) != mt:
        m = importlib.reload(m)
    m._MT = mt
    return m


SMX_NOW = {"NIFTY": "Nifty 50", "BANKNIFTY": "Bank Nifty", "BTC": "crypto|BTC", "ETH": "crypto|ETH", "SOL": "crypto|SOL",
           "GOLD": "metals|Gold", "US": "us|S&P 500"}


def _smx_v2(g: str) -> dict:
    """The v2 matrix for one group plus each market's trend / volatility now (this board's own labels, so the page and
    the study use one definition). {"missing": True} until the study has been run once."""
    f = os.path.join(ROOT, "data_store", "research", "strategy_regime_3y", "matrix_v2.json")
    try:
        d = json.load(open(f))
    except Exception:
        return {"missing": True, "group": g, "run": "cd ~/Documents/synaptic-main/Backtrader_App && python3 scripts/research/strategy_regime_v2.py"}
    now = {}
    try:
        rows = (json.load(open(LATEST)) or {}).get("rows") or {}
        for k, rid in SMX_NOW.items():
            r = rows.get(rid) or {}
            if r.get("trend_state"):
                now[k] = {"trend": r["trend_state"], "vol": r.get("vol_state"), "dir": r.get("vol_dir"), "as_of": r.get("as_of")}
    except Exception:
        pass
    out = {**(d.get(g) or {"rows": []}), "group": g, "meta": d.get("_meta") or {}, "now": now,
           "built": datetime.fromtimestamp(os.path.getmtime(f)).isoformat(timespec="minutes"), "v2": True}
    return out


def detail(group: str, sym: str, name: str = "", kind: str = "") -> dict:
    """The row, plus a year of closes with the two means, for the generic regime page."""
    # 2026-11-11 (Nikhil: "LTP should refresh every second, live feed wherever possible"): the board page asks for
    # live prices through this existing route (group="_live"), so no dashboard restart is needed for a new route.
    if group == "_live":
        return _boardlive().snap()
    if group == "_smx":                      # 2026-11-11: Strategy x Regime Matrix on the two-axis regime (scripts/research/strategy_regime_v2.py)
        return _smx_v2(sym or "india")
    if group == "_ofvid":                    # 2026-11-11: order-flow pattern explainer videos (data_store/research/ofvideos/<key>.mp4) as base64
        import base64
        key = (name or sym or "").strip()
        if key not in ("stack", "absorb", "exhaust", "unfinished", "finished", "trapped", "div"):
            return {"err": "unknown video"}
        f = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data_store", "research", "ofvideos", f"{key}.mp4")
        if not os.path.exists(f):
            return {"err": "video not found"}
        return {"key": key, "mime": "video/mp4", "b64": base64.b64encode(open(f, "rb").read()).decode()}
    if group == "_stf":                      # 2026-11-11: ST + Fisher + ADX paper rule state (app/stfisher.py); sym="tick" forces a tick
        F = _mod("stfisher")
        if sym == "tick":
            F.tick(force=True)
        if sym == "probe":
            return F.probe()
        return {**F.api(), "stf_err": _OFP.get("stf_err")}
    if group == "_ofstudy":                  # 2026-11-11: Order Flow Research results; sym="run" starts a run now
        T = _mod("ofstudy")
        if sym == "run":
            T.start(now=True)
        out = T.api()
        out["engine_errs"] = {k: v for k, v in _OFP.items() if k.endswith("_err") and v}
        return out
    if group == "_ofhist":                   # 2026-11-11: rebuild status / start / stop
        H = _mod("ofhistory")
        return H.stop() if sym == "stop" else H.start() if sym == "start" else H.status()
    if group == "_ofp":                      # order-flow patterns card (2026-11-11); sym = one market or "" for all
        out = _mod("ofpatterns").api(sym or None, name or None)      # name = timeframe in minutes
        out["runner_err"] = _OFP.get("err")
        return out
    day = datetime.now().strftime("%Y-%m-%d")
    key = (group, sym, kind)
    hit = _D.get(key)
    if hit and hit[0] == day:
        return hit[1]
    df = _bars_for(group, sym, kind)
    row = regime_of(df)
    row.update(name=name or sym, sym=sym, group=group, kind=kind or ("index" if group != "crypto" else "coin"))
    c = pd.to_numeric(df["close"], errors="coerce").dropna()
    m20, m50 = c.rolling(20).mean(), c.rolling(50).mean()
    idx = list(c.index)[-260:]
    series = [{"t": str(i)[:10], "c": round(float(c[i]), 6),
               "m20": None if pd.isna(m20[i]) else round(float(m20[i]), 6),
               "m50": None if pd.isna(m50[i]) else round(float(m50[i]), 6)} for i in idx]
    kids = []
    if group == "india" and kind != "stock":
        try:
            st = json.load(open(LATEST))
            for lab, dn in INDIA:
                if dn == sym or lab == (name or ""):
                    kids = (st.get("rows", {}).get(lab) or {}).get("children") or []
                    break
        except Exception:
            kids = []
    out = {"row": row, "series": series, "children": kids,
           "engine": "simple: 20-day realised vol percentile + daily trend (app/stockview.py rule)",
           "thresholds": {"high_vol_pct": VOL_HI, "quiet_pct": VOL_LO}}
    _D[key] = (day, out)
    return out
