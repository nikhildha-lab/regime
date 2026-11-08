"""F&O FLOW - the whole NSE F&O market on one board (2026-11-08, Nikhil: "create a new tab for this" - idea from
github.com/MrChartist/order-flow, rebuilt here on our own data; none of that project's code is used, its licence is non-commercial).

For every NSE F&O underlying (~215 stocks + 5 indices), read from its FUTURES:
  move       near-month futures price vs its previous close (Dhan quote: last price - net change)
  OI change  open interest of ALL listed months together vs yesterday's total (NSE F&O bhavcopy), so month-end rolls do not
             look like fresh positions
  build-up   price up + OI up = long build-up · price up + OI down = short covering · price down + OI up = short build-up
             · price down + OI down = long unwinding · inside the noise floor (OI 2 %, price 0.3 %) = neutral
  windows    the same reading over the last 15 and 60 minutes, from this module's own minute snapshots
  levels     yesterday's option open interest (near expiry, NSE bhavcopy): support = biggest put OI at or below price,
             resistance = biggest call OI at or above price, zone = strike where the most OI was added; writer lean = put OI
             added minus call OI added
  checks     5 yes / no checks, counted as conviction (0-5): fresh positions with the move · spot agrees · the last hour
             agrees · option writers lean the same way · the sector agrees
  breadth    up / flat / down, the four build-ups and a breadth score ((long build-up + short covering) - (short build-up +
             long unwinding)) / names, logged every minute to breadth_<day>.csv for later testing against Nifty
Data: Dhan market quote (app/dhan_live.quote, one call a minute for every future + spot), NSE F&O bhavcopy (once a day),
sectors from the Nifty 500 list. Outside market hours the board shows the last session from the bhavcopy alone.
Read-only, context only - no trades.
"""
from __future__ import annotations

import csv
import io
import json
import os
import threading
import time
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
OUT = os.path.join(ROOT, "data_store", "livedesk", "fnoflow")
DOCS = os.environ.get("MP_DOCS") or os.path.join(os.path.expanduser("~"), "Documents")
N500 = os.path.join(DOCS, "MASTER_BACKTESTER", "data", "nse_nifty500", "ind_nifty500list.csv")
INDEX_SPOT = {"NIFTY": "nifty", "BANKNIFTY": "banknifty", "FINNIFTY": "finnifty", "MIDCPNIFTY": "nifty midcap select", "NIFTYNXT50": "nifty next 50"}
FLOOR_OI, FLOOR_PX, FLAT = 2.0, 0.3, 0.25            # % - build-up noise floor; |move| below FLAT counts as flat
S: dict = {"t": 0, "busy": False, "err": None, "eod": None, "uni": None, "cache": {}}


def _today():
    return datetime.now().strftime("%Y-%m-%d")


# ------------------------------------------------------------------ universe (Dhan scrip master)
def universe():
    """{sym: {"kind", "futs": [(expiry, id, lot)] nearest first (max 3), "spot": (segment, id)}} - rebuilt once a day."""
    u = S.get("uni")
    if u and u[0] == _today():
        return u[1]
    import dhan_live as DL
    today = _today(); out = {}
    with open(DL.MASTER, newline="") as f:
        for r in csv.DictReader(f):
            if r.get("EXCH_ID") != "NSE" or r.get("SEGMENT") != "D" or r.get("INSTRUMENT") not in ("FUTSTK", "FUTIDX"):
                continue
            sym = (r.get("UNDERLYING_SYMBOL") or "").strip(); exp = (r.get("SM_EXPIRY_DATE") or "")[:10]
            if not sym or exp < today or (r["INSTRUMENT"] == "FUTIDX" and sym not in INDEX_SPOT):
                continue
            d = out.setdefault(sym, {"kind": "index" if r["INSTRUMENT"] == "FUTIDX" else "stock", "futs": []})
            try:
                lot = int(float(r.get("LOT_SIZE") or 0))
            except ValueError:
                lot = None
            d["futs"].append((exp, r["SECURITY_ID"], lot))
    for sym, d in list(out.items()):
        d["futs"] = sorted(d["futs"])[:3]
        sid = DL.ids_index(INDEX_SPOT[sym]) if d["kind"] == "index" else DL.ids_equity(sym)
        d["spot"] = (("IDX_I" if d["kind"] == "index" else "NSE_EQ"), sid) if sid else None
    S["uni"] = (today, out)
    return out


def names_map():
    m = S["cache"].get("names")
    if m is None:
        m = {}
        try:
            for r in csv.DictReader(open(N500, newline="", encoding="utf-8", errors="ignore")):
                m[r["Symbol"].strip()] = r["Company Name"].strip()
        except Exception:
            pass
        m.update({"NIFTY": "Nifty 50", "BANKNIFTY": "Nifty Bank", "FINNIFTY": "Nifty Financial Services", "MIDCPNIFTY": "Nifty Midcap Select", "NIFTYNXT50": "Nifty Next 50"})
        S["cache"]["names"] = m
    return m


def sectors():
    m = S["cache"].get("sectors")
    if m is None:
        m = {}
        try:
            for r in csv.DictReader(open(N500, newline="", encoding="utf-8", errors="ignore")):
                m[r["Symbol"].strip()] = r["Industry"].strip()
        except Exception:
            pass
        S["cache"]["sectors"] = m
    return m


def nifty50():
    """NSE's own Nifty 50 list (downloaded once, kept in fnoflow/nifty50.csv)."""
    p = os.path.join(OUT, "nifty50.csv")
    if not os.path.exists(p) or time.time() - os.path.getmtime(p) > 30 * 86400:
        try:
            import updater as U
            raw = U.http_get("https://nsearchives.nseindia.com/content/indices/ind_nifty50list.csv", U.NSE_HEADERS)
            os.makedirs(OUT, exist_ok=True); open(p, "wb").write(raw)
        except Exception as e:
            S["n50_err"] = str(e)[:100]
    try:
        return {r["Symbol"].strip() for r in csv.DictReader(open(p, newline="", encoding="utf-8", errors="ignore"))}
    except Exception:
        return set()


# ------------------------------------------------------------------ end of day (NSE F&O bhavcopy)
def _bhav(day: date):
    import updater as U
    cp = os.path.join(OUT, f"bhav_{day:%Y-%m-%d}.csv.gz")
    if os.path.exists(cp):
        return pd.read_csv(cp)
    raw = U.http_get(f"{U.NSE}/fo/BhavCopy_NSE_FO_0_0_0_{day:%Y%m%d}_F_0000.csv.zip", U.NSE_HEADERS)
    df = U._unzip_csv(raw)
    keep = ["TradDt", "FinInstrmTp", "TckrSymb", "XpryDt", "StrkPric", "OptnTp", "ClsPric", "PrvsClsgPric", "OpnIntrst", "ChngInOpnIntrst", "UndrlygPric"]
    df = df[[c for c in keep if c in df.columns]]
    os.makedirs(OUT, exist_ok=True); df.to_csv(cp, index=False, compression="gzip")
    return df


def eod():
    """Latest published bhavcopy before today -> per underlying: futures (near close, prev close, total OI, OI change) and
    yesterday's option levels. Cached for the day."""
    e = S.get("eod")
    if e and e[0] == _today():
        return e[1]
    import updater as U
    d = date.today() - timedelta(days=1); df = None; used = None
    for _ in range(10):
        if d.weekday() < 5 or d == date(2026, 11, 8):            # 8 Nov 2026 = Muhurat (Sunday) session
            try:
                df = _bhav(d); used = d; break
            except U.NotFound:
                pass
            except Exception as ex:
                S["err"] = f"bhavcopy {d}: {str(ex)[:100]}"
        d -= timedelta(days=1)
    if df is None:
        return {}
    out = {}
    fut = df[df.FinInstrmTp.isin(["STF", "IDF"])].copy()
    for sym, g in fut.groupby("TckrSymb"):
        if (g.FinInstrmTp == "IDF").any() and sym not in INDEX_SPOT:
            continue                                              # other index futures (NIFTYFPI ...) are not on the board
        g = g.sort_values("XpryDt"); n = g.iloc[0]
        oi = float(g.OpnIntrst.sum()); chg = float(g.ChngInOpnIntrst.sum())
        out[sym] = {"fut_close": float(n.ClsPric), "fut_prev": float(n.PrvsClsgPric), "oi": oi, "oi_prev": oi - chg,
                    "spot_close": float(n.UndrlygPric) if pd.notna(n.UndrlygPric) else None, "expiry": str(n.XpryDt)[:10]}
    opt = df[df.FinInstrmTp.isin(["STO", "IDO"])]
    for sym, g in opt.groupby("TckrSymb"):
        if sym not in out:
            continue
        g = g[g.XpryDt == g.XpryDt.min()]
        px = out[sym]["fut_close"]
        ce, pe = g[g.OptnTp == "CE"], g[g.OptnTp == "PE"]
        sup = pe[pe.StrkPric <= px * 1.005].sort_values("OpnIntrst").tail(2).iloc[::-1]
        res = ce[ce.StrkPric >= px * 0.995].sort_values("OpnIntrst").tail(2).iloc[::-1]
        z = g.groupby("StrkPric").ChngInOpnIntrst.sum()
        lad = g.pivot_table(index="StrkPric", columns="OptnTp", values="OpnIntrst", aggfunc="sum").fillna(0)
        if len(lad):
            k = int(np.searchsorted(lad.index.values, px)); lad = lad.iloc[max(0, k - 7):k + 7]
        out[sym].update(support2=float(sup.StrkPric.iloc[1]) if len(sup) > 1 else None, resistance2=float(res.StrkPric.iloc[1]) if len(res) > 1 else None,
                        ladder=[[float(k_), float(v.get("CE", 0)), float(v.get("PE", 0))] for k_, v in lad.iterrows()],
                        put_add_cr=round(float(pe.ChngInOpnIntrst.sum()) * px / 1e7, 2), call_add_cr=round(float(ce.ChngInOpnIntrst.sum()) * px / 1e7, 2),
                        opt_expiry=str(g.XpryDt.iloc[0])[:10])
        out[sym].update(support=float(sup.StrkPric.iloc[0]) if len(sup) else None, resistance=float(res.StrkPric.iloc[0]) if len(res) else None,
                        zone=float(z.idxmax()) if len(z) and z.max() > 0 else None,
                        lean=float(pe.ChngInOpnIntrst.sum() - ce.ChngInOpnIntrst.sum()),
                        pcr=round(float(pe.OpnIntrst.sum() / ce.OpnIntrst.sum()), 2) if ce.OpnIntrst.sum() else None)
    res = {"day": str(used), "rows": out}
    S["eod"] = (_today(), res)
    return res


# ------------------------------------------------------------------ live snapshots
def _prev(r):
    """Previous close from a Dhan quote row: last - net change, or the day OHLC 'close' (Dhan's previous close) when net change is 0 / missing."""
    lp, nc, pc = r.get("last_price"), r.get("net_change"), (r.get("ohlc") or {}).get("close")
    if lp is not None and nc not in (None, 0):
        return lp - nc
    return pc if pc else None


def _snap():
    import dhan_live as DL
    U = universe(); req = {}
    for d in U.values():
        for _, sid, _ in d["futs"]:
            req.setdefault("NSE_FNO", []).append(sid)
        if d.get("spot"):
            req.setdefault(d["spot"][0], []).append(d["spot"][1])
    Q = DL.quote(req)
    now = datetime.now(); m = now.strftime("%H:%M"); rows = []
    for sym, d in U.items():
        fq = [(Q.get("NSE_FNO") or {}).get(str(sid)) for _, sid, _ in d["futs"]]
        if not fq or not fq[0]:
            continue
        n = fq[0]
        oi = sum(float(x.get("oi") or 0) for x in fq if x)
        sp = (Q.get(d["spot"][0]) or {}).get(str(d["spot"][1])) if d.get("spot") else None
        rows.append([m, sym, n.get("last_price"), _prev(n), oi, sp.get("last_price") if sp else None, _prev(sp) if sp else None])
    if rows:
        p = os.path.join(OUT, f"snap_{now:%Y-%m-%d}.csv"); new = not os.path.exists(p)
        with open(p, "a", newline="") as f:
            w = csv.writer(f)
            if new:
                w.writerow(["m", "sym", "fut", "fut_prev", "oi", "spot", "spot_prev"])
            w.writerows(rows)
    return len(rows)


def _work():
    try:
        os.makedirs(OUT, exist_ok=True)
        eod(); n = _snap(); S["last"] = {"at": datetime.now().strftime("%H:%M:%S"), "names": n}; S["err"] = None
        try:
            b = board("all")
            with open(os.path.join(OUT, f"breadth_{_today()}.csv"), "a", newline="") as f:
                w = csv.writer(f)
                if f.tell() == 0:
                    w.writerow(["m", "names", "up", "flat", "down", "LB", "SC", "SB", "LU", "score", "nifty_move"])
                br = b.get("breadth") or {}
                w.writerow([datetime.now().strftime("%H:%M"), br.get("names"), br.get("up"), br.get("flat"), br.get("down"), br.get("LB"), br.get("SC"),
                            br.get("SB"), br.get("LU"), br.get("score"), (b.get("index") or {}).get("NIFTY", {}).get("move")])
        except Exception as e:
            S["breadth_err"] = str(e)[:120]
    except Exception as e:
        S["err"] = f"{type(e).__name__}: {str(e)[:160]}"
    finally:
        S["busy"] = False


def tick(force=False):
    now = datetime.now(); hm = now.strftime("%H:%M")
    if S["busy"] or (not force and (time.time() - S["t"] < 58 or now.weekday() >= 5 or not ("09:15" <= hm <= "15:31"))):
        return False
    S["t"] = time.time(); S["busy"] = True
    threading.Thread(target=_work, name="fnoflow", daemon=True).start()
    return True


# ------------------------------------------------------------------ the board
def _bu(px, oi):
    if px is None or oi is None or not np.isfinite(px) or not np.isfinite(oi):
        return "NEUTRAL"
    if abs(oi) < FLOOR_OI or abs(px) < FLOOR_PX:
        return "NEUTRAL"
    return "LONG_BUILD" if px > 0 and oi > 0 else "SHORT_BUILD" if px < 0 and oi > 0 else "SHORT_COVER" if px > 0 else "LONG_UNWIND"


def _latest_snap():
    files = sorted(f for f in os.listdir(OUT) if f.startswith("snap_")) if os.path.isdir(OUT) else []
    return os.path.join(OUT, files[-1]) if files else None


def board(univ="all"):
    E = eod(); R = E.get("rows") or {}
    sp = _latest_snap(); live = sp is not None and os.path.basename(sp)[5:15] == _today()
    sec = sectors(); NM = names_map(); n50 = nifty50() if univ == "nifty50" else None
    names = {}
    if live:
        s = pd.read_csv(sp); last = s.groupby("sym").tail(1).set_index("sym")
        mins = sorted(s.m.unique()); m_now = mins[-1]
        def ago(k):
            i = max(0, len(mins) - 1 - k); return s[s.m == mins[i]].set_index("sym")
        a15, a60 = ago(15), ago(60)
        path = s.pivot_table(index="m", columns="sym", values="fut")
        for sym, r in last.iterrows():
            e = R.get(sym, {})
            mv = (r.fut / r.fut_prev - 1) * 100 if r.fut_prev else None
            oic = (r.oi / e["oi_prev"] - 1) * 100 if e.get("oi_prev") else None
            sprev = e.get("spot_close") or r.spot_prev                 # yesterday's underlying close from the bhavcopy (quote rows reset after hours)
            spm = (r.spot / sprev - 1) * 100 if r.spot and sprev else None
            w = {}
            for lab, A in (("15m", a15), ("60m", a60)):
                if sym in A.index and A.at[sym, "fut"] and A.at[sym, "oi"]:
                    pm = (r.fut / A.at[sym, "fut"] - 1) * 100; om = (r.oi / A.at[sym, "oi"] - 1) * 100
                    w[lab] = {"move": round(pm, 2), "oi": round(om, 2), "bu": _bu(pm * 4, om * 2)}       # shorter windows: smaller floors
            ps = path[sym].dropna() if sym in path else pd.Series(dtype=float)
            ps = pd.concat([ps.iloc[::5], ps.iloc[-1:]]) if len(ps) else ps
            ps = ps[~ps.index.duplicated(keep="last")]
            pth = [[mm, round((v / r.fut_prev - 1) * 100, 2)] for mm, v in ps.items()] if r.fut_prev else []
            names[sym] = {"move": mv, "oi_chg": oic, "spot_move": spm, "fut": r.fut, "fut_prev": r.fut_prev, "oi": r.oi, "w": w, "path": pth}
        at = f"{os.path.basename(sp)[5:15]} {m_now}"
        src = "live" if datetime.now().weekday() < 5 and datetime.now().strftime("%H:%M") <= "15:35" else "session snapshots (market closed)"
    else:
        for sym, e in R.items():
            mv = (e["fut_close"] / e["fut_prev"] - 1) * 100 if e.get("fut_prev") else None
            oic = (e["oi"] / e["oi_prev"] - 1) * 100 if e.get("oi_prev") else None
            names[sym] = {"move": mv, "oi_chg": oic, "spot_move": None, "fut": e.get("fut_close"), "fut_prev": e.get("fut_prev"), "oi": e.get("oi"), "w": {}, "path": []}
        at = E.get("day"); src = "last session (NSE end of day)"
    U = S.get("uni")[1] if S.get("uni") else {}
    rows = []
    for sym, n in names.items():
        if n["move"] is None or not np.isfinite(n["move"]):
            continue
        kind = "index" if sym in INDEX_SPOT else "stock"
        if univ == "nifty50" and kind == "stock" and n50 and sym not in n50:
            continue
        e = R.get(sym, {})
        rows.append({"sym": sym, "kind": kind, "sector": "Index" if kind == "index" else sec.get(sym, "Other"), "move": round(n["move"], 2),
                     "oi_chg": None if n["oi_chg"] is None else round(n["oi_chg"], 2), "bu": _bu(n["move"], n["oi_chg"]),
                     "spot_move": None if n["spot_move"] is None else round(n["spot_move"], 2), "fut": n["fut"], "w": n["w"], "path": n["path"],
                     "support": e.get("support"), "resistance": e.get("resistance"), "zone": e.get("zone"), "pcr": e.get("pcr"), "lean": e.get("lean"),
                     "support2": e.get("support2"), "resistance2": e.get("resistance2"), "ladder": e.get("ladder") or [], "put_add_cr": e.get("put_add_cr"),
                     "call_add_cr": e.get("call_add_cr"), "opt_expiry": e.get("opt_expiry"), "name": NM.get(sym, sym), "fut_prev": n.get("fut_prev"),
                     "oi_cr": round(n["oi"] * n["fut"] / 1e7, 1) if n.get("oi") and n.get("fut") else None})
    if not rows:
        return {"at": at, "source": src, "rows": [], "err": S.get("err")}
    D = pd.DataFrame(rows)
    stocks = D[D.kind == "stock"]
    smed = stocks.groupby("sector").move.median()
    for r in rows:
        side = 1 if r["move"] >= FLAT else -1 if r["move"] <= -FLAT else 0
        r["side"] = side
        sm = smed.get(r["sector"]) if r["kind"] == "stock" else None
        h = (r["w"].get("60m") or {}).get("move")
        f2 = lambda v, d=2: "n/a" if v is None else f"{v:+.{d}f}%"
        way = "up" if side > 0 else "down"
        chk = [("fresh", "New positions opened with the move", side != 0 and r["bu"] == ("LONG_BUILD" if side > 0 else "SHORT_BUILD"),
                f"open interest {f2(r['oi_chg'])} with the price {way}" if side else "no clear move"),
               ("spot", "The stock itself moved the same way", side != 0 and r["spot_move"] is not None and r["spot_move"] * side >= 0.2,
                f"spot {f2(r['spot_move'])} since the previous close" if r["spot_move"] is not None else "needs live data"),
               ("hour", "The last hour agrees", side != 0 and h is not None and h * side > 0,
                f"last hour {f2(h)}" if h is not None else "needs an hour of live data"),
               ("writers", "Option writers lean the same way", side != 0 and r["lean"] is not None and r["lean"] * side > 0,
                f"puts {r['put_add_cr']:+.2f} Cr and calls {r['call_add_cr']:+.2f} Cr of OI added last session" if r.get("put_add_cr") is not None else "no option data"),
               ("sector", "Its sector leans the same way", side != 0 and sm is not None and sm * side > 0,
                f"{r['sector']} median {f2(sm)}" if sm is not None else "index")]
        r["checks"] = [{"id": a, "label": b, "pass": bool(c), "note": d} for a, b, c, d in chk]
        r["conviction"] = sum(1 for c in chk if c[2])
    st = [r for r in rows if r["kind"] == "stock"]
    for sd in (1, -1):
        grp = sorted([r for r in st if r["side"] == sd], key=lambda r: -sd * r["move"])
        for i, r in enumerate(grp):
            r["rank"], r["of"] = i + 1, len(grp)
    cnt = lambda k: sum(1 for r in st if r["bu"] == k)
    br = {"names": len(st), "up": sum(1 for r in st if r["side"] > 0), "flat": sum(1 for r in st if r["side"] == 0), "down": sum(1 for r in st if r["side"] < 0),
          "LB": cnt("LONG_BUILD"), "SC": cnt("SHORT_COVER"), "SB": cnt("SHORT_BUILD"), "LU": cnt("LONG_UNWIND"), "NEUTRAL": cnt("NEUTRAL")}
    br["score"] = round((br["LB"] + br["SC"] - br["SB"] - br["LU"]) / max(1, br["names"]), 3)
    secs = []
    for s_, g in stocks.groupby("sector"):
        secs.append({"sector": s_, "median": round(float(g.move.median()), 2), "n": int(len(g)), "up": int((g.move >= FLAT).sum()), "down": int((g.move <= -FLAT).sum()),
                     "bu": pd.Series([_bu(m, o) for m, o in zip(g.move, g.oi_chg)]).value_counts().idxmax()})
    secs.sort(key=lambda x: -x["median"])
    hist = []
    bp = os.path.join(OUT, f"breadth_{at[:10]}.csv")
    if os.path.exists(bp):
        try:
            hist = pd.read_csv(bp)[["m", "score", "up", "down", "nifty_move"]].dropna(subset=["score"]).values.tolist()
        except Exception:
            pass
    idx = {r["sym"]: r for r in rows if r["kind"] == "index"}
    return {"at": at, "source": src, "eod_day": E.get("day"), "universe": univ, "breadth": br, "sectors": secs, "index": idx,
            "rows": sorted([r for r in rows if r["kind"] == "stock"], key=lambda r: -r["move"]), "breadth_hist": hist, "err": S.get("err"),
            "note": ("" if live else "Market closed: the 'stock agrees' and 'last hour' checks need live data, so conviction tops out at 3 of 5. ")
                    + "Futures move vs previous close; OI = all listed months vs yesterday (NSE bhavcopy); levels = yesterday's option OI. "
                    "Context only - descriptive, not a tested signal."}


_CHAIN_T = [0.0]


def detail(sym):
    """One name: today's (or the last session's) 5-min futures candles and its live option chain (nearest expiry). Cached 2 min."""
    sym = sym.upper(); c = S["cache"].get(("detail", sym))
    if c and time.time() - c[0] < 120:
        return c[1]
    import dhan_live as DL
    d = universe().get(sym) or {}; out = {"sym": sym, "candles": [], "chain": [], "err": None}
    if not d.get("futs"):
        return {**out, "err": "unknown symbol"}
    sp = _latest_snap(); day = date.fromisoformat(os.path.basename(sp)[5:15]) if sp else date.today()
    try:
        bars = DL.intraday("NSE_FNO", d["futs"][0][1], "FUTIDX" if d["kind"] == "index" else "FUTSTK", day, day, 5, oi=True)
        out["candles"] = [[b["ts"].strftime("%H:%M"), b["open"], b["high"], b["low"], b["close"], b["oi"]] for b in bars]
        out["day"] = str(day)
    except Exception as e:
        out["err"] = f"candles: {str(e)[:100]}"
    try:
        seg = "IDX_I" if d["kind"] == "index" else "NSE_EQ"
        uid = DL.ids_index(INDEX_SPOT[sym]) if d["kind"] == "index" else DL.ids_equity(sym)
        wait = 3.1 - (time.time() - _CHAIN_T[0])
        if wait > 0:
            time.sleep(wait)
        exps = sorted(DL._post("/optionchain/expirylist", {"UnderlyingScrip": int(uid), "UnderlyingSeg": seg}) or [])
        exps = [x for x in exps if x >= _today()]
        time.sleep(3.1)
        ch = DL._post("/optionchain", {"UnderlyingScrip": int(uid), "UnderlyingSeg": seg, "Expiry": exps[0]}); _CHAIN_T[0] = time.time()
        und = ch.get("last_price"); rows = []
        for k, v in (ch.get("oc") or {}).items():
            ce, pe = v.get("ce") or {}, v.get("pe") or {}
            rows.append([float(k), float(ce.get("oi") or 0), float(pe.get("oi") or 0), float(ce.get("oi") or 0) - float(ce.get("previous_oi") or 0),
                         float(pe.get("oi") or 0) - float(pe.get("previous_oi") or 0)])
        rows.sort()
        if rows and und:
            ks = [r_[0] for r_ in rows]; i = int(np.searchsorted(ks, und)); rows = rows[max(0, i - 7):i + 7]
        out.update(chain=rows, chain_expiry=exps[0], spot=und, chain_at=datetime.now().strftime("%H:%M"))
    except Exception as e:
        out["err"] = (out["err"] or "") + f" chain: {str(e)[:100]}"
    S["cache"][("detail", sym)] = (time.time(), out)
    return out


def api(q=None):
    q = q or {}
    if q.get("snap") == "1":                               # manual snapshot (testing / outside the schedule)
        return {"started": tick(force=True)}
    if q.get("detail"):
        return detail(q["detail"])
    if q.get("raw"):                                        # one name's raw Dhan quote rows (checking)
        import dhan_live as DL
        d = universe().get(q["raw"].upper()) or {}
        req = {"NSE_FNO": [d["futs"][0][1]]} if d.get("futs") else {}
        if d.get("spot"):
            req.setdefault(d["spot"][0], []).append(d["spot"][1])
        return DL.quote(req) if req else {}
    univ = q.get("u") or "all"
    c = S["cache"].get(("board", univ))
    if c and time.time() - c[0] < 30:
        return c[1]
    try:
        res = board(univ)
    except Exception as e:
        res = {"err": f"{type(e).__name__}: {str(e)[:160]}", "rows": []}
    res["engine"] = {"last": S.get("last"), "err": S.get("err"), "busy": S.get("busy")}
    S["cache"][("board", univ)] = (time.time(), res)
    return res
