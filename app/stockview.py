"""
Nifty 50 stock view for the Indian market tab (2026-10-25, Nikhil: "add all Nifty 50 data, select from a drop-down").
Indications only - no orders. A simple, transparent engine (not the Market Pulse regime engine), untested.

Per stock (on demand, cached):
  bars      : Dhan daily (~400 days) + 5-minute (last ~12 days; 15m / 1h built from 5m). Fallback daily = nse_stocks_eod.csv.
  timeframes: 5m 15m 1h daily weekly monthly. Score /10 from 4 checks (close vs EMA fast, EMA fast vs slow, EMA fast rising,
              close vs 20 bars ago), each +-1 -> x2.5. Up >= +5, Down <= -5, else Flat. Weekly / monthly = completed bars.
  regime    : 20-day realised volatility percentile (1y) + daily trend -> High Volatility / Uptrend / Downtrend / Range / Quiet.
  blocks    : density of last week / month high-low (star), previous day high / low / close, 10-day 5-min volume nodes,
              30-min swing highs / lows (recent weigh more) and option OI walls (top put / call strikes, fresh writing).
              Merged inside a band of 0.25% of price. strong = score >= 6 with OI or star; medium >= 3.5; else weak.
  verdicts  : intraday (15m + 1h), weekly (daily + weekly), monthly (weekly + monthly) + the 5m/15m hourly-style verdict.
  OI        : Dhan option chain, nearest expiry: PCR, max pain, put / call walls, fresh writing since yesterday, OI at blocks.
"""
from __future__ import annotations

import json
import os
import time
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
N50_FILE = os.path.join(ROOT, "data_store", "india", "nifty50.json")
EOD = os.path.join(ROOT, "data_store", "market_data", "nse_stocks_eod.csv")
N50_URL = "https://nsearchives.nseindia.com/content/indices/ind_nifty50list.csv"
# fallback only if NSE cannot be reached (may be out of date - the NSE list replaces it automatically)
N50_FALLBACK = ["ADANIENT", "ADANIPORTS", "APOLLOHOSP", "ASIANPAINT", "AXISBANK", "BAJAJ-AUTO", "BAJFINANCE", "BAJAJFINSV", "BEL",
                "BHARTIARTL", "CIPLA", "COALINDIA", "DRREDDY", "EICHERMOT", "ETERNAL", "GRASIM", "HCLTECH", "HDFCBANK", "HDFCLIFE",
                "HINDALCO", "HINDUNILVR", "ICICIBANK", "INDIGO", "INFY", "ITC", "JIOFIN", "JSWSTEEL", "KOTAKBANK", "LT", "M&M",
                "MARUTI", "MAXHEALTH", "NESTLEIND", "NTPC", "ONGC", "POWERGRID", "RELIANCE", "SBILIFE", "SBIN", "SHRIRAMFIN",
                "SUNPHARMA", "TATACONSUM", "TMPV", "TATASTEEL", "TCS", "TECHM", "TITAN", "TRENT", "ULTRACEMCO", "WIPRO"]
C: dict = {}


def _hours(t=None):
    t = t or datetime.now()
    return t.weekday() < 5 and 9 * 60 + 15 <= t.hour * 60 + t.minute <= 15 * 60 + 35


def _cached(key, ttl, fn):
    x = C.get(key)
    if x and time.time() - x[0] < ttl:
        return x[1]
    v = fn()
    C[key] = (time.time(), v)
    return v


# ------------------------------------------------------------------ universe
def nifty50() -> dict:
    """{'stocks': [...], 'source': 'NSE <date>' | 'fallback list'} - refreshed from NSE once a week."""
    try:
        j = json.load(open(N50_FILE))
        if (datetime.now() - datetime.fromisoformat(j["fetched"])).days < 7 and len(j.get("stocks", [])) >= 45:
            return j
    except Exception:
        j = None
    try:
        import updater as U
        s = U._CSESSION
        r = s.get(N50_URL, timeout=20, headers={"Referer": "https://www.nseindia.com/"})
        if r.status_code == 200 and "Symbol" in r.text:
            import io
            d = pd.read_csv(io.StringIO(r.text))
            out = {"fetched": datetime.now().isoformat(timespec="seconds"), "source": f"NSE list {date.today()}",
                   "stocks": sorted(d["Symbol"].astype(str).str.strip().tolist()),
                   "industry": dict(zip(d["Symbol"].astype(str).str.strip(), d.get("Industry", pd.Series([""] * len(d))).astype(str)))}
            if len(out["stocks"]) >= 45:
                os.makedirs(os.path.dirname(N50_FILE), exist_ok=True)
                json.dump(out, open(N50_FILE, "w"))
                return out
    except Exception:
        pass
    return j or {"fetched": None, "source": "fallback list (NSE not reachable - may be out of date)", "stocks": sorted(N50_FALLBACK), "industry": {}}


def universe() -> dict:
    u = nifty50()
    try:
        q = json.load(open(os.path.join(ROOT, "data_store", "india", "live_quotes.json"))).get("quotes", {})
    except Exception:
        q = {}
    rows = []
    for s in u["stocks"]:
        x = q.get(s) or {}
        rows.append({"sym": s, "ltp": x.get("ltp"), "industry": (u.get("industry") or {}).get(s, "")})
    tracked = []
    try:                                     # stocks the live Nifty tracker carries (order flow + trade cards)
        import re
        cfg = open(os.path.join(os.environ.get("MP_NIFTY_TRACKER_DIR") or os.path.join(os.path.expanduser("~"), "Documents", "nifty-live-tracker"), "config.yaml")).read()
        tracked = [k for k in re.findall(r"key:\s*([A-Z0-9&-]+)", cfg) if k not in ("NIFTY", "BANKNIFTY")]
    except Exception:
        pass
    for r in rows:
        r["tracked"] = r["sym"] in tracked
    return {"source": u["source"], "stocks": rows, "tracked": tracked}


# ------------------------------------------------------------------ data
def _sid(sym):
    import dhan_live as DL
    return DL.ids_equity(sym)


def _daily(sym) -> pd.DataFrame:
    def get():
        try:
            import dhan_live as DL
            sid = _sid(sym)
            if sid:
                b = DL.daily("NSE_EQ", sid, "EQUITY", date.today() - timedelta(days=420), date.today())
                if len(b) > 60:
                    d = pd.DataFrame(b)
                    d["date"] = pd.to_datetime([x.date() for x in d.ts])
                    return d.drop_duplicates("date", keep="last").set_index("date")[["open", "high", "low", "close", "volume"]], "Dhan"
        except Exception:
            pass
        e = pd.read_csv(EOD)
        e = e[e.symbol == sym].copy()
        e["date"] = pd.to_datetime(e.date)
        return e.set_index("date").sort_index()[["open", "high", "low", "close", "volume"]], "NSE end-of-day file"
    return _cached(("d", sym), 300 if _hours() else 3 * 3600, get)


def _intra(sym) -> pd.DataFrame:
    def get():
        try:
            import dhan_live as DL
            sid = _sid(sym)
            b = DL.intraday("NSE_EQ", sid, "EQUITY", date.today() - timedelta(days=16), date.today(), 5, oi=False) if sid else []
            if b:
                d = pd.DataFrame(b)
                d["ts"] = pd.to_datetime([x.replace(tzinfo=None) for x in d.ts])
                return d.set_index("ts")[["open", "high", "low", "close", "volume"]]
        except Exception as e:
            C["intra_err"] = str(e)[:120]
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    return _cached(("i", sym), 120 if _hours() else 3 * 3600, get)


def _chain(sym):
    def get():
        try:
            import dhan_live as DL
            import optionchain as OC
            sid = _sid(sym)
            if not sid:
                return None
            exps = sorted(DL._post("/optionchain/expirylist", {"UnderlyingScrip": int(sid), "UnderlyingSeg": "NSE_EQ"}) or [])
            if not exps:
                return None
            # expiry week: OI is rolling out of the near month, so read the next expiry from 3 days before expiry
            e = exps[0]
            if len(exps) > 1 and (pd.Timestamp(exps[0]) - pd.Timestamp(date.today())).days <= 3:
                e = exps[1]
            time.sleep(3)
            d = OC.parse_dhan(DL._post("/optionchain", {"UnderlyingScrip": int(sid), "UnderlyingSeg": "NSE_EQ", "Expiry": e}), sym,
                              datetime.now().strftime("%Y-%m-%d %H:%M"), e)
            d = d[d.oi > 0] if len(d) else None
            if d is not None:
                d.attrs["rolled"] = e != exps[0]
            return d
        except Exception as ex:
            C["chain_err"] = str(ex)[:120]
            return None
    return _cached(("c", sym), 180 if _hours() else 3 * 3600, get)


# ------------------------------------------------------------------ analysis
def _rs(d, rule):
    return d.resample(rule, origin="start_day", offset="9h15min", label="left", closed="left").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}).dropna(subset=["close"])


def _tf(d: pd.DataFrame, name: str) -> dict:
    if d is None or len(d) < 8:
        return {"tf": name, "trend": None, "score": None, "close": None, "bar": None}
    c = d.close
    f, s = (20, 50) if len(c) >= 60 else (6, 12)
    ef, es = c.ewm(span=f, adjust=False).mean(), c.ewm(span=s, adjust=False).mean()
    look = min(20, len(c) - 1)
    chk = [np.sign(c.iloc[-1] - ef.iloc[-1]), np.sign(ef.iloc[-1] - es.iloc[-1]), np.sign(ef.iloc[-1] - ef.iloc[-4]),
           np.sign(c.iloc[-1] - c.iloc[-1 - look])]
    sc = round(float(sum(chk)) * 2.5, 1)
    return {"tf": name, "trend": "Up" if sc >= 5 else "Down" if sc <= -5 else "Flat", "score": sc,
            "close": round(float(c.iloc[-1]), 2), "bar": str(d.index[-1])[:16]}


def _blocks(spot, dl, i5, ch):
    band = spot * 0.0025
    items = []                                   # (price, weight, label, kind)
    today = pd.Timestamp(date.today())
    done = dl[dl.index < today] if _hours() else dl
    if len(done) >= 2:
        p = done.iloc[-1]
        items += [(p.high, 2, "prev day high", "price"), (p.low, 2, "prev day low", "price")]   # close left out: it sits on price
    w = done.resample("W-FRI").agg({"high": "max", "low": "min"}).dropna()
    if len(w) >= 2:
        lw = w.iloc[-2] if w.index[-1] >= done.index[-1] and done.index[-1].weekday() < 4 else w.iloc[-1]
        items += [(lw.high, 3, "last week high", "alert"), (lw.low, 3, "last week low", "alert")]
    m = done.resample("ME").agg({"high": "max", "low": "min"}).dropna()
    if len(m) >= 2:
        lm = m.iloc[-2]
        items += [(lm.high, 3, "last month high", "alert"), (lm.low, 3, "last month low", "alert")]
    if len(i5):
        z = i5[i5.index >= i5.index[-1].normalize() - pd.Timedelta(days=14)]
        tp = (z.high + z.low + z.close) / 3
        bins = (tp / band).round() * band
        vp = z.volume.groupby(bins).sum().sort_values(ascending=False)
        for k, (px, v) in enumerate(vp.head(4).items()):
            items.append((px, 3 - 0.5 * k, "10-day volume node", "price"))
        m30 = _rs(z, "30min")
        hi, lo = m30.high.values, m30.low.values
        n = len(m30)
        for j in range(2, n - 2):
            rec = 1 + (j / n)
            if hi[j] == max(hi[j - 2:j + 3]):
                items.append((hi[j], rec, "30-min swing high", "price"))
            if lo[j] == min(lo[j - 2:j + 3]):
                items.append((lo[j], rec, "30-min swing low", "price"))
    if ch is not None and len(ch):
        tot = ch.oi.sum() or 1
        for side, lab in (("PE", "put OI"), ("CE", "call OI")):
            s = ch[ch.side == side].sort_values("oi", ascending=False)
            for _, r in s.head(3).iterrows():
                items.append((r.strike, 6 * r.oi / tot * 4 + 1, f"{lab} {r.strike:g}", "oi"))
            fw = s[s.chg_oi > 0].sort_values("chg_oi", ascending=False).head(2)
            for _, r in fw.iterrows():
                items.append((r.strike, 1.5, f"fresh {lab.split()[0]} writing {r.strike:g}", "oi"))
    items = [x for x in items if spot * 0.93 <= x[0] <= spot * 1.07]
    items.sort()
    groups = []
    for it in items:
        if groups and it[0] - groups[-1][-1][0] <= band and it[0] - groups[-1][0][0] <= 2 * band:   # no long chains
            groups[-1].append(it)
        else:
            groups.append([it])
    out = []
    for g in groups:
        sc = sum(x[1] for x in g)
        kinds = {x[3] for x in g}
        lo_, hi_ = min(x[0] for x in g), max(x[0] for x in g)
        if hi_ - lo_ < band * 0.6:
            mid = (lo_ + hi_) / 2
            lo_, hi_ = mid - band * 0.3, mid + band * 0.3
        st = "strong" if sc >= 6 and ("oi" in kinds or "alert" in kinds) else "medium" if sc >= 3.5 else "weak"
        star = "alert" in kinds
        out.append({"lo": round(lo_, 2), "hi": round(hi_, 2), "score": round(sc, 1), "strength": st, "star": star,
                    "side": "R" if lo_ > spot else "S" if hi_ < spot else ("R" if (lo_ + hi_) / 2 > spot else "S"),
                    "why": ", ".join(dict.fromkeys(x[2] for x in sorted(g, key=lambda x: -x[1])))[:120]})
    return out


def _ref_levels(spot, dl, i5) -> list:
    """2026-11-04 (Nikhil): dashed reference lines for the block chart - 5-day value area (rolling volume profile, 70 %),
    initial balance (09:15-10:15) of the latest session, previous day / week / month high and low."""
    out = []
    today = pd.Timestamp(date.today())
    done = dl[dl.index < today] if _hours() else dl
    if len(done) >= 2:
        p = done.iloc[-1]
        out += [("PDH", "previous day high", p.high), ("PDL", "previous day low", p.low)]
    w = done.resample("W-FRI").agg({"high": "max", "low": "min"}).dropna()
    if len(w) >= 2:
        lw = w.iloc[-2] if w.index[-1] >= done.index[-1] and done.index[-1].weekday() < 4 else w.iloc[-1]
        out += [("PWH", "previous week high", lw.high), ("PWL", "previous week low", lw.low)]
    m = done.resample("ME").agg({"high": "max", "low": "min"}).dropna()
    if len(m) >= 2:
        out += [("PMH", "previous month high", m.iloc[-2].high), ("PML", "previous month low", m.iloc[-2].low)]
    if len(i5):
        days = sorted(set(i5.index.date))
        z = i5[pd.Index(i5.index.date).isin(days[-5:])]
        step = spot * 0.001
        tp = ((z.high + z.low + z.close) / 3 / step).round() * step
        v = z.volume.groupby(tp.values).sum()
        if v.sum() > 0:
            o = v.sort_values(ascending=False)
            va = o[(o.cumsum() / o.sum()) <= 0.7].index
            if len(va):
                out += [("VAH", "5-day value area high (rolling volume profile)", float(max(va))), ("VAL", "5-day value area low (rolling volume profile)", float(min(va))),
                        ("POC", "5-day point of control (price with the most volume)", float(o.index[0]))]
        last = i5[i5.index.date == days[-1]]
        if len(last) and last.volume.sum() > 0:                 # 2026-11-04: VWAP of the latest session
            out.append(("VWAP", f"VWAP of the {days[-1]:%m-%d} session", float(((last.high + last.low + last.close) / 3 * last.volume).sum() / last.volume.sum())))
        ib = last[last.index.strftime("%H:%M") < "10:15"]
        if len(ib) >= 10 and (not _hours() or datetime.now().strftime("%H:%M") >= "10:15" or days[-1] != date.today()):
            out += [("IBH", f"initial balance high {days[-1]:%m-%d} (09:15-10:15)", float(ib.high.max())), ("IBL", f"initial balance low {days[-1]:%m-%d} (09:15-10:15)", float(ib.low.min()))]
    return [{"short": a, "name": n, "px": round(float(x), 2), "dist_pct": round(100 * (float(x) / spot - 1), 2)} for a, n, x in out if x == x and x]


def _fmt(b, px):
    d = 0 if px > 2000 else 1 if px > 200 else 2
    return f"{b['lo']:,.{d}f}–{b['hi']:,.{d}f}"


def _verdict(a, b, sup, res, spot, n=3, line=""):
    sc = ((a or {}).get("score") or 0) + ((b or {}).get("score") or 0)
    ta, tb = (a or {}).get("trend"), (b or {}).get("trend")
    word = "Bullish" if sc >= 7.5 or (ta == tb == "Up") else "Bearish" if sc <= -7.5 or (ta == tb == "Down") else "Mixed" if {ta, tb} == {"Up", "Down"} else "Sideways"
    L = lambda xs: [{"range": _fmt(x, spot), "strength": x["strength"], "star": x["star"]} for x in xs[:n]]
    out = {"word": word, "line": line, "res": L(res), "sup": L(sup)}
    txt = lambda xs: ", ".join(f"{x['range']} {x['strength']}{' ★' if x['star'] else ''}" for x in xs)
    out["text"] = " · ".join(t for t in [line, ("resistance " + txt(out["res"])) if out["res"] else "", ("support " + txt(out["sup"])) if out["sup"] else ""] if t)
    return out


def _oi(ch, spot, blocks):
    if ch is None or not len(ch):
        return None
    pe, ce = ch[ch.side == "PE"], ch[ch.side == "CE"]
    strikes = sorted(ch.strike.unique())
    pain = min(strikes, key=lambda k: float(((k - ce.strike).clip(lower=0) * ce.oi).sum() + ((pe.strike - k).clip(lower=0) * pe.oi).sum()))
    step = float(np.median(np.diff(strikes))) if len(strikes) > 2 else spot * 0.01
    at = []
    for b in blocks:
        if b["strength"] == "weak" or abs((b["lo"] + b["hi"]) / 2 / spot - 1) > 0.04:
            continue
        z = ch[(ch.strike >= b["lo"] - step / 2) & (ch.strike <= b["hi"] + step / 2)]
        if not len(z):
            continue
        p, c = z[z.side == "PE"].chg_oi.sum(), z[z.side == "CE"].chg_oi.sum()
        po, co = max(z[z.side == "PE"].oi.sum(), 1), max(z[z.side == "CE"].oi.sum(), 1)
        own, other, own_oi, oth_oi = (p, c, po, co) if b["side"] == "S" else (c, p, co, po)
        big = lambda x, base: abs(x) >= 0.05 * base                 # ignore changes under 5% of the OI already there
        read = ("DEFENDED", "hold") if own > 0 and big(own, own_oi) and own > abs(other) else \
               ("ABANDONED", "break") if own < 0 and big(own, own_oi) else \
               ("PRESSURE", "break") if other > 0 and big(other, oth_oi) and other > abs(own) else ("no clear change", "flat")
        at.append({"range": _fmt(b, spot), "strength": b["strength"], "side": "support" if b["side"] == "S" else "resistance",
                   "put_chg": int(p), "call_chg": int(c), "read": read[0], "tone": read[1]})
    at.sort(key=lambda r: -float(r["range"].split("–")[0].replace(",", "")))
    top = lambda s, col: [f"{x:g}" for x in s.sort_values(col, ascending=False).head(3).strike]
    return {"expiry": str(ch.expiry.iloc[0]), "pcr": round(pe.oi.sum() / max(ce.oi.sum(), 1), 2), "max_pain": pain,
            "puts": top(pe, "oi"), "calls": top(ce, "oi"),
            "note": "Expiry week: reading the next month, where OI is building at every strike - block reads are weak this week." if ch.attrs.get("rolled") else None,
            "put_writing": top(pe[pe.chg_oi > 0], "chg_oi"), "call_writing": top(ce[ce.chg_oi > 0], "chg_oi"), "at_blocks": at,
            "as_of": str(ch.ts.iloc[0])}


def analyze(sym: str) -> dict:
    sym = sym.upper().strip()
    if sym not in nifty50()["stocks"]:
        return {"sym": sym, "error": "not in the Nifty 50 list"}
    dl, src = _daily(sym)
    if dl is None or len(dl) < 30:
        return {"sym": sym, "error": "no daily history"}
    i5 = _intra(sym)
    if len(i5) and i5.index[-1].date() > dl.index[-1].date():           # today's partial day from 5-min bars
        t = i5[i5.index.date == i5.index[-1].date()]
        dl = pd.concat([dl, pd.DataFrame({"open": [t.open.iloc[0]], "high": [t.high.max()], "low": [t.low.min()],
                                          "close": [t.close.iloc[-1]], "volume": [t.volume.sum()]}, index=[pd.Timestamp(t.index[-1].date())])])
    live = _hours() and len(i5) and i5.index[-1].date() == date.today()
    spot = float(i5.close.iloc[-1]) if live else float(dl.close.iloc[-1])           # closed: official close (daily bar)
    px_at = str(i5.index[-1])[:16] if live else f"{dl.index[-1].date()} close"
    ch = _chain(sym)
    wk = dl.resample("W-FRI").agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}).dropna()
    if len(wk) and wk.index[-1] > pd.Timestamp(date.today()):                                   # completed weeks only
        wk = wk.iloc[:-1]
    mo = dl.resample("ME").agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}).dropna()
    if len(mo) and mo.index[-1] >= pd.Timestamp(date.today()):
        mo = mo.iloc[:-1]
    tfs = [_tf(i5, "5m"), _tf(_rs(i5, "15min") if len(i5) else None, "15m"), _tf(_rs(i5, "60min") if len(i5) else None, "1h"),
           _tf(dl, "daily"), _tf(wk, "weekly"), _tf(mo, "monthly")]
    T = {x["tf"]: x for x in tfs}
    # regime (simple)
    r = np.log(dl.close).diff()
    vol20 = r.rolling(20).std() * np.sqrt(252) * 100
    pct = float((vol20.iloc[-250:] <= vol20.iloc[-1]).mean() * 100) if vol20.notna().sum() > 60 else None
    tr = pd.concat([dl.high - dl.low, (dl.high - dl.close.shift()).abs(), (dl.low - dl.close.shift()).abs()], axis=1).max(axis=1)
    atr = float(tr.rolling(14).mean().iloc[-1])
    dt = T["daily"]["trend"]
    regime = "High Volatility" if pct is not None and pct >= 80 else "Uptrend" if dt == "Up" else "Downtrend" if dt == "Down" else \
             "Quiet" if pct is not None and pct <= 20 else "Range"
    prev = float(dl.close.iloc[-2]) if dl.index[-1].date() == date.today() or not _hours() else float(dl.close.iloc[-1])
    if not _hours() and len(dl) >= 2:
        prev = float(dl.close.iloc[-2])
    blocks = _blocks(spot, dl, i5, ch)
    sup = sorted([b for b in blocks if b["hi"] < spot], key=lambda b: -b["hi"])
    res = sorted([b for b in blocks if b["lo"] > spot], key=lambda b: b["lo"])
    # 5m / 15m hourly-style verdict (same rules as the index card)
    hv = None
    try:
        import hourverdict as H
        mk = {"mtf": {"rows": [{"tf": x["tf"], "trend": x["trend"], "scores": {"overall": x["score"]}, "close": spot, "bar": x["bar"]} for x in tfs]},
              "_blocks": [(b["lo"], b["hi"], b["strength"], b["why"].split(", ")[0], b["star"]) for b in blocks]}
    except Exception as e:
        mk = None
    keep = []                                    # tidy: up to 4 strong/medium and 2 weak each side, weak only within 3%
    for side in (sup, res):
        sm = [b for b in side if b["strength"] != "weak"][:4]
        wk_ = [b for b in side if b["strength"] == "weak" and abs((b["lo"] + b["hi"]) / 2 / spot - 1) <= 0.03][:2]
        keep += sm + wk_
    keep += [b for b in blocks if b["lo"] <= spot <= b["hi"]]
    ksup = [b for b in sup if b in keep]; kres = [b for b in res if b in keep]
    near = lambda xs, pct, ok: [b for b in xs if abs((b["lo"] + b["hi"]) / 2 / spot - 1) * 100 <= pct and ok(b)]
    verdicts = {"intraday": _verdict(T["15m"], T["1h"], near(ksup, 1.5, lambda b: True), near(kres, 1.5, lambda b: True), spot),
                "weekly": _verdict(T["daily"], T["weekly"], near(ksup, 4, lambda b: b["strength"] != "weak"), near(kres, 4, lambda b: b["strength"] != "weak"), spot),
                "monthly": _verdict(T["weekly"], T["monthly"], near(sup, 8, lambda b: b["strength"] == "strong" or b["star"]),
                                    near(res, 8, lambda b: b["strength"] == "strong" or b["star"]), spot)}
    try:
        mk["_blocks"] = [(b["lo"], b["hi"], b["strength"], b["why"].split(", ")[0], b["star"]) for b in keep]
        mk["_inside_strong"] = True                 # stocks: only a strong block stops the trend read (closing-area nodes are common)
        hv = H.compute(sym, mk)
    except Exception as e:
        hv = {"error": str(e)[:100]}
    grp = {st: sorted([dict(b, range=_fmt(b, spot)) for b in keep if b["strength"] == st], key=lambda b: b["lo"]) for st in ("strong", "medium", "weak")}
    return {"sym": sym, "spot": round(spot, 2), "price_at": px_at, "prev_close": round(prev, 2), "chg_pct": round((spot / prev - 1) * 100, 2),
            "day": {"open": float(dl.open.iloc[-1]), "high": float(dl.high.iloc[-1]), "low": float(dl.low.iloc[-1])} if dl.index[-1].date() == (i5.index[-1].date() if len(i5) else dl.index[-1].date()) else None,
            "regime": regime, "vol_pct": round(pct) if pct is not None else None, "atr": round(atr, 2), "atr_pct": round(atr / spot * 100, 2),
            "timeframes": tfs, "blocks": grp, "levels": _ref_levels(spot, dl, i5), "verdicts": verdicts, "hourly": hv, "oi": _oi(ch, spot, keep),
            "intraday_ok": bool(len(i5)), "daily_src": src, "notes": [x for x in (C.get("intra_err"), C.get("chain_err")) if x][:2],
            "built": datetime.now().strftime("%Y-%m-%d %H:%M"), "market_open": _hours()}
