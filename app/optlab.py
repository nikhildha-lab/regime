"""OPTIONS LAB - the extra F&O sections (2026-11-08, Nikhil: "focus on repo .. yes .. plan very fancy premium visuals").
Ideas taken from MrChartist's Option Hub and FII/DII repos (personal-use licence: methods only, no code copied); all data is our own:
  vol(sym)      IV rank / percentile (last 252 sessions of data_store/research/iv_hist), volatility cone from the underlying's own
                realised moves (10/20/30/60 sessions), smile and term structure from the latest NSE bhavcopy
  ivmap()       every F&O name: IV rank, IV / 20-day realised volatility, skew - for the bubble map
  flow(sym)     intraday, from our 3-minute option-chain snapshots (data_store/flow/option_chain/<day>.csv.gz):
                  prints   per strike and side between two snapshots: volume added, premium = volume x price (the chain's volume is
                           in UNITS like the bhavcopy's TtlTradgVol - checked 30 Oct: equal to it - while its OI is in contracts),
                           side by tick test (price up = buyers lifted, down = sellers hit; no bid/ask in the snapshot),
                           opening / closing from the OI change (|dOI| >= 30 % of the volume)
                  tide     cumulative buyer-minus-seller premium, calls and puts, and net = calls - puts
                  hedge    futures option writers must trade to stay delta-neutral: sum dOI x delta x lot x spot (Rs Cr);
                           new calls -> writers buy futures (+), new puts -> writers sell (-)   (a model, not observed trades)
                  heat     per strike: net buyer premium and OI opened, calls and puts
  gex(sym)      gamma exposure of the latest snapshot, Rs Cr per 1 % move: seller view (writers short everything) and the dealer
                view (long calls / short puts) with its gamma flip; call and put walls
  inst()        FII / DII cash (forward-saved since Oct 2026), streaks and big-day flags; participant OI from fnoflow
  ban()         NSE F&O ban list for the next session
Everything is context, not a tested signal.
"""
from __future__ import annotations

import json
import math
import os
import re
import time
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
IVH = os.path.join(ROOT, "data_store", "research", "iv_hist")
OC = os.path.join(ROOT, "data_store", "flow", "option_chain")
OUT = os.path.join(ROOT, "data_store", "livedesk", "optlab")
MD = os.path.join(ROOT, "data_store", "market_data")
C: dict = {}
LOTS = {"NIFTY": 75, "BANKNIFTY": 35}
N = lambda x: 0.5 * (1 + math.erf(x / math.sqrt(2)))
NPDF = lambda x: math.exp(-0.5 * x * x) / math.sqrt(2 * math.pi)


def _cache(key, ttl, fn):
    c = C.get(key)
    if c and time.time() - c[0] < ttl:
        return c[1]
    try:
        v = fn()
    except Exception as e:
        v = {"err": f"{type(e).__name__}: {str(e)[:160]}"}
    C[key] = (time.time(), v)
    return v


def _lot(sym):
    try:
        import fnoflow as FF
        f = (FF.universe().get(sym) or {}).get("futs") or []
        if f and f[0][2]:
            return int(f[0][2])
    except Exception:
        pass
    return LOTS.get(sym, 1)


# ---------------------------------------------------------------- volatility
def _ivhist():
    fs = sorted(f for f in os.listdir(IVH) if f.startswith("iv_") and f.endswith(".csv.gz")) if os.path.isdir(IVH) else []
    key = (len(fs), fs[-1] if fs else None)
    c = C.get("ivh")
    if c and c[0] == key:
        return c[1]
    H = pd.concat([pd.read_csv(os.path.join(IVH, f)) for f in fs], ignore_index=True) if fs else pd.DataFrame()
    C["ivh"] = (key, H)
    return H


def _ensure_today():
    """Add the latest session's IV file from the cached bhavcopy if the history job has not got it yet (runs once per day)."""
    import fnoflow as FF
    import buresearch as BR
    day = FF.eod().get("day")
    fp = os.path.join(IVH, f"iv_{day}.csv.gz")
    if day and not os.path.exists(fp):
        os.makedirs(IVH, exist_ok=True)
        BR.iv_day(FF._bhav(date.fromisoformat(day)), day).to_csv(fp, index=False, compression="gzip")
    return day


def _rv(px, n):
    r = np.log(px).diff()
    return r.rolling(n).std() * np.sqrt(252) * 100


def vol(sym):
    def go():
        day = _ensure_today(); H = _ivhist(); h = H[H.sym == sym].sort_values("day")
        if not len(h):
            return {"err": f"no IV history for {sym}"}
        last = h.iloc[-1]; y = h.tail(252); s = y.near_iv.dropna(); cur = last.near_iv
        rank = None if s.max() == s.min() or pd.isna(cur) else round((cur - s.min()) / (s.max() - s.min()) * 100, 0)
        pct = None if pd.isna(cur) else round(float((s < cur).mean() * 100), 0)
        px = h.set_index("day").und.astype(float)
        cone = []
        for n in (10, 20, 30, 60):
            rv = _rv(px, n).dropna()
            if len(rv) > 20:
                q = rv.quantile([0, .25, .5, .75, 1]).round(1).tolist()
                cone.append({"n": n, "min": q[0], "p25": q[1], "med": q[2], "p75": q[3], "max": q[4], "now": round(float(rv.iloc[-1]), 1)})
        hist = [[d, None if pd.isna(v) else round(float(v), 2)] for d, v in zip(h.day.tail(252), h.near_iv.tail(252))]
        sm, term = _smile_term(sym, day)
        hv20 = cone[1]["now"] if len(cone) > 1 else None
        return {"sym": sym, "day": last.day, "und": float(last.und), "iv": None if pd.isna(cur) else float(cur), "iv_next": None if pd.isna(last.get("next_iv")) else float(last["next_iv"]),
                "near_exp": last.get("near_exp"), "straddle": None if pd.isna(last.get("near_straddle")) else float(last["near_straddle"]),
                "skew": None if pd.isna(last.get("skew")) else float(last["skew"]), "rank": rank, "pctile": pct, "lo": round(float(s.min()), 1), "hi": round(float(s.max()), 1),
                "hv20": hv20, "iv_hv": round(cur / hv20, 2) if hv20 and not pd.isna(cur) else None, "cone": cone, "hist": hist, "smile": sm, "term": term,
                "days": int(len(y))}
    return _cache(("vol", sym), 600, go)


def _smile_term(sym, day):
    import fnoflow as FF
    b = FF._bhav(date.fromisoformat(day)); g = b[(b.TckrSymb == sym) & b.FinInstrmTp.isin(["STO", "IDO"])].copy()
    if not len(g):
        return [], []
    g["XpryDt"] = g.XpryDt.astype(str).str[:10]; S0 = float(g.UndrlygPric.dropna().iloc[0]); d0 = date.fromisoformat(day)
    exps = [e for e in sorted(g.XpryDt.unique()) if (date.fromisoformat(e) - d0).days >= 1]
    band = 0.06 if sym in LOTS or sym in ("FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50") else 0.12
    smile, term = [], []
    for i, e in enumerate(exps[:8]):
        x = g[g.XpryDt == e]; T = ((date.fromisoformat(e) - d0).days + 0.25) / 365
        ks = sorted(set(x.StrkPric))
        if not ks:
            continue
        k0 = min(ks, key=lambda z: abs(z - S0))
        ce = x[x.OptnTp == "CE"].set_index("StrkPric").ClsPric; pe = x[x.OptnTp == "PE"].set_index("StrkPric").ClsPric
        a = [FF._iv(float(ce.get(k0, 0)), S0, k0, T, "CE"), FF._iv(float(pe.get(k0, 0)), S0, k0, T, "PE")]; a = [v for v in a if v]
        if a:
            term.append({"expiry": e, "dte": (date.fromisoformat(e) - d0).days, "iv": round(sum(a) / len(a) * 100, 2)})
        if i < 3:
            pts = []
            for k in ks:
                if abs(k / S0 - 1) > band:
                    continue
                cp, px = ("PE", pe.get(k)) if k < S0 else ("CE", ce.get(k))
                if px is None or px <= 0.05:
                    continue
                v = FF._iv(float(px), S0, k, T, cp)
                if v and v < 2:
                    pts.append([float(k), round(v * 100, 2)])
            if len(pts) > 4:                                                     # drop stale prints (IV far below / above the curve's median)
                med = float(np.median([p[1] for p in pts])); pts = [p for p in pts if 0.6 * med <= p[1] <= 2.5 * med]
            smile.append({"expiry": e, "dte": (date.fromisoformat(e) - d0).days, "pts": pts})
    if len(term) > 3:                                                            # an expiry far off the others = stale / illiquid closes
        med = float(np.median([t["iv"] for t in term])); term = [t for t in term if 0.6 * med <= t["iv"] <= 1.5 * med]
    return smile, term


def ivmap():
    def go():
        _ensure_today(); H = _ivhist()
        if not len(H):
            return {"rows": []}
        days = sorted(H.day.unique()); last = days[-1]; recent = H[H.day.isin(days[-252:])]
        out = []
        for sym, h in recent.groupby("sym"):
            h = h.sort_values("day")
            if h.day.iloc[-1] != last or h.near_iv.isna().iloc[-1]:
                continue
            s = h.near_iv.dropna(); cur = h.near_iv.iloc[-1]
            rk = (cur - s.min()) / (s.max() - s.min()) * 100 if s.max() > s.min() else None
            rv = _rv(h.set_index("day").und.astype(float), 20).dropna()
            hv = float(rv.iloc[-1]) if len(rv) else None
            out.append({"sym": sym, "iv": round(float(cur), 1), "rank": None if rk is None else round(rk, 0), "hv20": None if not hv else round(hv, 1),
                        "iv_hv": round(cur / hv, 2) if hv else None, "skew": None if pd.isna(h["skew"].iloc[-1]) else float(h["skew"].iloc[-1])})
        return {"day": last, "rows": out}
    return _cache("ivmap", 600, go)


# ---------------------------------------------------------------- intraday flow from chain snapshots
def _snapday(day=None):
    fs = sorted(f for f in os.listdir(OC) if f.endswith(".csv.gz")) if os.path.isdir(OC) else []
    if not fs:
        return None, None
    f = f"{day}.csv.gz" if day and f"{day}.csv.gz" in fs else fs[-1]
    return f[:10], pd.read_csv(os.path.join(OC, f))


def _bsdelta_gamma(S0, K, T, v, cp, r=0.065):
    if T <= 0 or v <= 0 or S0 <= 0 or K <= 0:
        return 0.0, 0.0
    d1 = (math.log(S0 / K) + (r + 0.5 * v * v) * T) / (v * math.sqrt(T))
    return (N(d1) if cp == "CE" else N(d1) - 1), NPDF(d1) / (S0 * v * math.sqrt(T))


def flow(sym, day=None):
    def go():
        d, D = _snapday(day)
        if D is None:
            return {"err": "no option-chain snapshots recorded"}
        D = D[(D.symbol == sym)].dropna(subset=["expiry"])
        if not len(D):
            return {"err": f"no {sym} snapshots on {d}"}
        exp = sorted(D.expiry.unique())[0]; D = D[D.expiry == exp].sort_values("ts"); lot = _lot(sym)
        D["k"] = D.strike.astype(str) + D.side
        ts = sorted(D.ts.unique()); T0 = datetime.fromisoformat(exp + " 15:30:00")
        prints = []; tide = []; cum = {"CE": 0.0, "PE": 0.0}; hedge = 0.0
        prev = None
        for t in ts:
            cur = D[D.ts == t].set_index("k")
            if prev is not None:
                j = cur.join(prev, rsuffix="_p", how="inner")
                j = j[(j.volume - j.volume_p) > 0]
                S0 = float(cur.underlying.iloc[0]); T = max(1 / 365 / 24, (T0 - datetime.fromisoformat(t)).total_seconds() / 86400 / 365)
                for k, r in j.iterrows():
                    dv = float(r.volume - r.volume_p); doi = float(r.oi - r.oi_p); px = float(r.ltp) if r.ltp > 0 else float(r.ltp_p)
                    if px <= 0:
                        continue
                    side = "buy" if r.ltp > r.ltp_p else "sell" if r.ltp < r.ltp_p else "mid"
                    tag = "opening" if doi * lot >= 0.3 * dv else "closing" if doi * lot <= -0.3 * dv else "churn"
                    prem = dv * px
                    if side != "mid":
                        cum[r.side] += prem if side == "buy" else -prem
                    v = (r.iv or 0) / 100 or 0.15
                    dl, _ = _bsdelta_gamma(S0, float(r.strike), T, v, r.side)
                    hedge += doi * dl * lot * S0 / 1e7
                    prints.append({"ts": t[11:16], "strike": float(r.strike), "cp": r.side, "lots": int(round(dv / lot)), "px": round(px, 2), "prem": round(prem / 1e5, 2),
                                   "side": side, "tag": tag, "doi": int(doi)})
                tide.append([t[11:16], round(cum["CE"] / 1e7, 2), round(cum["PE"] / 1e7, 2), round((cum["CE"] - cum["PE"]) / 1e7, 2), round(hedge, 1), round(S0, 2)])
            prev = cur
        P = pd.DataFrame(prints)
        heat = []
        if len(P):
            P["signed"] = np.where(P.side == "buy", P.prem, np.where(P.side == "sell", -P.prem, 0))
            for (k, cp), g in P.groupby(["strike", "cp"]):
                heat.append({"strike": k, "cp": cp, "net": round(float(g.signed.sum()), 1), "opened": int(g[g.tag == "opening"].doi.sum()), "prem": round(float(g.prem.sum()), 1)})
        tape = P.sort_values("prem", ascending=False).head(30).sort_values("ts", ascending=False).to_dict("records") if len(P) else []
        last = D[D.ts == ts[-1]]
        return {"sym": sym, "day": d, "expiry": exp, "lot": lot, "spot": float(last.underlying.iloc[0]), "snapshots": len(ts), "at": ts[-1][11:16],
                "tide": tide, "tape": tape, "heat": heat,
                "totals": {"call_buy": round(float(P[(P.cp == "CE") & (P.side == "buy")].prem.sum()) / 100, 2) if len(P) else 0,
                           "call_sell": round(float(P[(P.cp == "CE") & (P.side == "sell")].prem.sum()) / 100, 2) if len(P) else 0,
                           "put_buy": round(float(P[(P.cp == "PE") & (P.side == "buy")].prem.sum()) / 100, 2) if len(P) else 0,
                           "put_sell": round(float(P[(P.cp == "PE") & (P.side == "sell")].prem.sum()) / 100, 2) if len(P) else 0}}
    return _cache(("flow", sym, day), 60, go)


def gex(sym, day=None):
    def go():
        d, D = _snapday(day)
        if D is None:
            return {"err": "no option-chain snapshots recorded"}
        D = D[(D.symbol == sym)].dropna(subset=["expiry"])
        if not len(D):
            return {"err": f"no {sym} snapshots on {d}"}
        t = D.ts.max(); X = D[(D.ts == t)]; exp = sorted(X.expiry.unique())[0]; X = X[(X.expiry == exp) & (X.oi > 0)]
        S0 = float(X.underlying.iloc[0]); lot = _lot(sym)
        T = max(1 / 365 / 24, (datetime.fromisoformat(exp + " 15:30:00") - datetime.fromisoformat(t)).total_seconds() / 86400 / 365)
        def prof(S):
            rows = {}
            for r in X.itertuples():
                v = (r.iv or 0) / 100
                if v <= 0:
                    continue
                _, gm = _bsdelta_gamma(S, float(r.strike), T, v, r.side)
                g = gm * float(r.oi) * lot * S * S * 0.01 / 1e7                         # Rs Cr per 1 % move
                z = rows.setdefault(float(r.strike), {"strike": float(r.strike), "call": 0.0, "put": 0.0})
                z["call" if r.side == "CE" else "put"] += g
            return rows
        rows = prof(S0)
        dealer = lambda R: sum(z["call"] - z["put"] for z in R.values())
        grid = np.linspace(S0 * 0.96, S0 * 1.04, 33); prof_curve = [[round(float(s), 1), round(dealer(prof(s)), 1)] for s in grid]
        flip = None
        for (s1, g1), (s2, g2) in zip(prof_curve, prof_curve[1:]):
            if g1 == 0 or (g1 < 0) != (g2 < 0):
                f = s1 + (s2 - s1) * (0 - g1) / (g2 - g1) if g2 != g1 else s1
                if flip is None or abs(f - S0) < abs(flip - S0):
                    flip = round(f, 1)
        R = sorted(rows.values(), key=lambda z: z["strike"])
        for z in R:
            z["call"] = round(z["call"], 2); z["put"] = round(z["put"], 2)
        near = [z for z in R if abs(z["strike"] / S0 - 1) <= 0.05]
        tot_dealer = round(dealer(rows), 1); tot_seller = round(-sum(z["call"] + z["put"] for z in R), 1)
        cw = max(R, key=lambda z: z["call"])["strike"] if R else None; pw = max(R, key=lambda z: z["put"])["strike"] if R else None
        return {"sym": sym, "day": d, "at": t[11:16], "expiry": exp, "spot": S0, "rows": near, "dealer_total": tot_dealer, "seller_total": tot_seller,
                "flip": flip, "call_wall": cw, "put_wall": pw, "curve": prof_curve,
                "regime": "positive" if tot_dealer > 0 else "negative"}
    return _cache(("gex", sym, day), 60, go)


# ---------------------------------------------------------------- institutions + ban list
def inst():
    def go():
        import fnoflow as FF
        c = pd.read_csv(os.path.join(MD, "nse_fiidii_cash.csv")).drop_duplicates(["date", "category"], keep="last")
        w = c.pivot(index="date", columns="category", values="net_cr").sort_index().dropna()
        days = [{"day": d, "fii": round(float(r.FII), 0), "dii": round(float(r.DII), 0)} for d, r in w.tail(45).iterrows()]
        f = w.FII.values[::-1]; sd = 1 if f[0] > 0 else -1; streak = 0
        for v in f:
            if (v > 0) == (sd > 0):
                streak += 1
            else:
                break
        last = days[-1] if days else {}
        flags = []
        if last:
            if last["fii"] <= -5000: flags.append("FII heavy selling (more than Rs 5,000 Cr)")
            if last["fii"] >= 3000: flags.append("FII heavy buying (more than Rs 3,000 Cr)")
            if last["dii"] >= 5000: flags.append("DII big absorption (more than Rs 5,000 Cr)")
            if abs(last["fii"] - last["dii"]) >= 8000: flags.append("extreme FII / DII divergence")
        w10 = w.tail(10); absorb = None
        if (w10.FII < 0).any():
            sell = -w10.FII[w10.FII < 0].sum(); absorb = round(float(w10.DII.clip(lower=0).sum() / sell * 100), 0) if sell else None
        return {"cash": days, "streak": {"side": "selling" if sd < 0 else "buying", "days": int(streak)}, "flags": flags, "absorb10": absorb,
                "mtd_fii": round(float(w[w.index.str[:7] == w.index[-1][:7]].FII.sum()), 0), "mtd_dii": round(float(w[w.index.str[:7] == w.index[-1][:7]].DII.sum()), 0),
                "since": str(w.index.min()), "participants": FF._participants()}
    return _cache("inst", 600, go)


def ban():
    def go():
        import updater as U
        os.makedirs(OUT, exist_ok=True); fp = os.path.join(OUT, f"ban_{date.today()}.json")
        if os.path.exists(fp):
            return json.load(open(fp))
        txt = U.http_get(f"{U.NSE}/fo/fo_secban.csv", U.NSE_HEADERS).decode("utf-8", "ignore")
        m = re.search(r"Trade Date\s*([0-9A-Za-z-]+)", txt)
        syms = [x.strip() for x in re.findall(r"^\s*\d+\s*,\s*([A-Z0-9&\-]+)", txt, re.M)]
        res = {"for": m.group(1) if m else None, "syms": syms, "at": datetime.now().strftime("%Y-%m-%d %H:%M")}
        json.dump(res, open(fp, "w"))
        return res
    return _cache("ban", 3600, go)


def ob(syms):
    """Order blocks for a few stocks (same detection as BuildUp v2, app/fnoshort.py): last bullish OB below and bearish OB (supply) above,
    in futures-price terms, from the latest session's compact futures files."""
    key = ("ob", tuple(sorted(syms)))
    def go():
        import fnoshort as FS
        H = os.path.join(ROOT, "data_store", "research", "fno_hist")
        day = sorted(f[4:14] for f in os.listdir(H) if f.startswith("fut_") and f.endswith(".csv.gz"))[-1]
        st = FS._structure(day, list(syms)); out = {}
        for s_, v in st.items():
            ch = v[2] if len(v) > 2 else None
            if ch:
                out[s_] = {"ob": ch.get("ob"), "ob_day": ch.get("ob_day"), "supply": ch.get("supply"), "low60": ch.get("low60"), "late": bool(v[0]), "why": v[1]}
        return {"day": day, "obs": out}
    return _cache(key, 900, go)


_OBS: dict = {}


def api(q):
    if q.get("lab") == "ob":
        return ob([x for x in (q.get("syms") or "").upper().split(",") if x][:40])
    if q.get("lab") == "calarb":              # 2026-11-11: research backtest - index calendar arbitrage with synthetic futures (app/calarb_bt.py)
        import importlib, sys, threading
        import calarb_bt as CA
        CA = importlib.reload(sys.modules["calarb_bt"])
        if q.get("do") == "run" and not _OBS.get("ca_busy"):
            def go3():
                _OBS["ca_busy"] = True
                try:
                    _OBS["ca"] = CA.run()
                except Exception as e:
                    import traceback
                    _OBS["ca"] = {"err": f"{type(e).__name__}: {str(e)[:200]}", "tb": traceback.format_exc()[-600:]}
                finally:
                    _OBS["ca_busy"] = False
            threading.Thread(target=go3, daemon=True, name="calarb").start()
        return {"busy": bool(_OBS.get("ca_busy")), "res": _OBS.get("ca")}
    if q.get("lab") == "obbt":                # 2026-11-11: research backtest - order-block break + 2 ATR trail (app/obbreak_bt.py), runs in a thread
        import importlib, sys, threading
        import obbreak_bt as BT
        BT = importlib.reload(sys.modules["obbreak_bt"])
        if q.get("do") == "run" and not _OBS.get("bt_busy"):
            def go():
                _OBS["bt_busy"] = True
                try:
                    if q.get("k"):
                        BT.ATR_K = float(q["k"])                 # neighbour check (1.5 / 3 ATR)
                    _OBS["bt"] = BT.run(); _OBS["bt"]["atr_k"] = BT.ATR_K; _OBS["bt_look"] = BT.lookahead_check()
                except Exception as e:
                    _OBS["bt"] = {"err": f"{type(e).__name__}: {str(e)[:200]}"}
                finally:
                    _OBS["bt_busy"] = False
            threading.Thread(target=go, daemon=True, name="obbt").start()
        if q.get("do") == "retest" and not _OBS.get("bt_busy"):
            def go2():
                _OBS["bt_busy"] = True
                try:
                    BT.ATR_K = 2.0; _OBS["bt_rt"] = BT.run_retest()
                except Exception as e:
                    _OBS["bt_rt"] = {"err": f"{type(e).__name__}: {str(e)[:200]}"}
                finally:
                    _OBS["bt_busy"] = False
            threading.Thread(target=go2, daemon=True, name="obbt_rt").start()
        return {"busy": bool(_OBS.get("bt_busy")), "res": _OBS.get("bt"), "look": _OBS.get("bt_look"), "retest": _OBS.get("bt_rt")}
    if q.get("lab") == "ob2":                 # 2026-11-11: order-block breaks for Strongest Readings (app/obscan.py), every F&O stock
        import importlib, sys, obscan as OS
        mt = os.path.getmtime(OS.__file__)
        if _OBS.get("mt") is None:
            _OBS["mt"] = mt
        elif _OBS["mt"] != mt:
            OS = importlib.reload(sys.modules["obscan"]); _OBS["mt"] = mt
        return OS.get()
    w = q.get("lab"); sym = (q.get("sym") or "NIFTY").upper(); day = q.get("day")
    if w == "vol":
        return vol(sym)
    if w == "ivmap":
        return ivmap()
    if w == "flow":
        return flow(sym, day)
    if w == "gex":
        return gex(sym, day)
    if w == "inst":
        return inst()
    if w == "ban":
        return ban()
    return {"err": "lab = vol | ivmap | flow | gex | inst | ban"}
