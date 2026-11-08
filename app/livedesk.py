"""
Live Desk (2026-10-20) - live views on top of the live trackers.

Reads the Nifty tracker (http://127.0.0.1:8899/api/snapshot), the metals tracker (:8897) and the crypto engine
that runs inside Market Pulse (app/crypto_live.py, since 2026-11-01) every 2 s: footprint / order-flow views,
Block story + Block watch, 4H break, touch-minute, momentum basket, DOM ladder. It does NOT change the trackers' rules.
2026-11-03 (Nikhil): tracker setup cards (+ their grader, log book and review) dropped permanently.
Indications only - nothing here places orders.
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
DIR = os.path.join(ROOT, "data_store", "livedesk")
CFG_PATH = os.path.join(DIR, "config.json")
HOME = os.path.expanduser("~")
OF_LIVE = os.path.join(os.environ.get("MP_DHAN_DIR") or os.path.join(HOME, "Documents", "dhan"), "orderflow", "data", "of", "live.json")
IST = timezone(timedelta(hours=5, minutes=30))

DEFAULT_CFG = {
    "poll_seconds": 2,                       # live prices / footprint every 2 s (the trackers themselves refresh every 1.5 s)
    "crypto_coins": ["BTC", "ETH", "SOL", "BNB", "XRP", "UNI", "ZEC", "LINK", "DASH"],   # 2026-11-03: + drop-down coins (analysis only)
}
DESKS = {
    "nifty": {"url": "http://127.0.0.1:8899/api/snapshot", "port": 8899, "name": "Nifty & Bank Nifty"},
    "crypto": {"inproc": True, "name": "Crypto"},      # 2026-11-01: engine runs inside Market Pulse (app/crypto_live.py)
    # 2026-10-23: clones of the crypto tracker on Hyperliquid's HIP-3 'xyz' perps (Documents/metals-tracker)
    "metals": {"url": "http://127.0.0.1:8897/api/snapshot?full=1", "port": 8897, "name": "Gold & Silver"},
}
CRYPTO_LIKE = ("crypto", "metals")          # 24/7 Hyperliquid-style desks: UTC stamps, 4 h time exit, cards carry over midnight
_LOCK = threading.RLock()
_STATE = {"desks": {}, "started": None}


def cfg() -> dict:
    c = dict(DEFAULT_CFG)
    try:
        c.update(json.load(open(CFG_PATH)))
    except Exception:
        pass
    return c


def now_ist() -> datetime:
    return datetime.now(IST)


def _hm(s: str) -> int:
    h, m = s.split(":")
    return int(h) * 60 + int(m)


def _crypto_engine():
    """The crypto engine module (normal import, not hot-reloaded, so its feeds keep running). Starts it if it is down."""
    import crypto_live as CL
    if not CL._E["running"] and not _STATE.get("crypto_starting"):
        _STATE["crypto_starting"] = True
        def go():
            try:
                _STATE["crypto_engine"] = CL.start().get("detail")
            except Exception as e:
                _STATE["crypto_engine"] = f"start failed: {e}"[:160]
            finally:
                _STATE["crypto_starting"] = False
        threading.Thread(target=go, daemon=True, name="crypto-engine-start").start()
    return CL


def _get(url: str, timeout=8):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode())


def setup_name(s: dict) -> str:
    mode = (s or {}).get("mode") or ""
    reasons = " ".join((s or {}).get("reasons") or []) if isinstance((s or {}).get("reasons"), list) else str((s or {}).get("reasons") or "")
    if "absorption" in reasons.lower() and mode == "range":
        return "Absorption reversal"
    if mode == "range":
        return "Range fade"
    if mode == "continuation":
        return "Continuation"
    return mode.title() or "Setup"


def _num(x):
    try:
        return None if x is None else float(x)
    except Exception:
        return None


def flow_feed() -> dict:
    """Real buy / sell flow for Nifty comes from the Dhan order-flow engine (trades feed)."""
    try:
        d = json.load(open(OF_LIVE))
        age = time.time() - os.path.getmtime(OF_LIVE)
        st = ((d.get("feeds") or {}).get("trades (Full)") or {}).get("state")
        return {"ok": st == "connected" and age < 60, "state": st, "age_s": round(age)}
    except Exception as e:
        return {"ok": False, "state": "missing", "error": str(e)[:60]}


# ---------- footprint timeframes (2026-10-23): 15 min bars from the tracker, merged into 30 min and 4 h ----------
TFS = (15, 30, 240)


FP_DIR = os.path.join(DIR, "footprint")      # real (not estimated) completed 15-min footprint bars, one file per market per day
_FPH: dict = {}


def _bar_dates(desk: str, bars: list) -> list:
    """Bars carry only HH:MM. Date them backwards from now (UTC for crypto-style desks, IST for Nifty)."""
    now = datetime.now(timezone.utc) if desk in CRYPTO_LIKE else now_ist()
    d = now.date(); nm = now.hour * 60 + now.minute
    out, prev = [None] * len(bars), None
    for i in range(len(bars) - 1, -1, -1):
        try:
            hh, mm = map(int, str(bars[i].get("t"))[:5].split(":")); m = hh * 60 + mm
        except Exception:
            out[i] = d.isoformat(); continue
        if prev is None and m > nm + 15:
            d = d - timedelta(days=1)
        elif prev is not None and m > prev:
            d = d - timedelta(days=1)
        prev = m; out[i] = d.isoformat()
    return out


def _fph(desk: str, key: str) -> dict:
    k = (desk, key)
    if k not in _FPH:
        h = {}
        folder = os.path.join(FP_DIR, desk, key)
        try:
            for f in sorted(os.listdir(folder))[-21:]:
                if f.endswith(".json"):
                    for t, b in (json.load(open(os.path.join(folder, f))) or {}).items():
                        h[(f[:-5], t)] = b
        except Exception:
            pass
        _FPH[k] = h
    return _FPH[k]


def fp_store(desk: str, key: str, bars: list):
    """Keep every completed, real footprint bar on disk so a tracker restart does not wipe real order flow (2026-10-23)."""
    bars = [b for b in (bars or []) if isinstance(b, dict) and b.get("t")]
    if len(bars) < 2:
        return
    h, ds, changed = _fph(desk, key), _bar_dates(desk, bars), set()
    for b, d in zip(bars[:-1], ds[:-1]):                  # last bar is still forming
        if b.get("approx") or not b.get("total"):
            continue
        if (d, b["t"]) not in h:
            h[(d, b["t"])] = dict(b, d=d); changed.add(d)
    for d in changed:
        folder = os.path.join(FP_DIR, desk, key); os.makedirs(folder, exist_ok=True)
        day = {t: b for (dd, t), b in h.items() if dd == d}
        tmp = os.path.join(folder, d + ".json.tmp")
        json.dump(day, open(tmp, "w"), separators=(",", ":")); os.replace(tmp, os.path.join(folder, d + ".json"))


def fp_hist(desk: str, key: str, bars: list) -> list:
    """Stored real bars + the tracker's current bars, dated and in time order. A stored real bar beats an estimated one."""
    bars = [b for b in (bars or []) if isinstance(b, dict) and b.get("t")]
    m = dict(_fph(desk, key))
    for b, d in zip(bars, _bar_dates(desk, bars)):
        old = m.get((d, b["t"]))
        if old is None or not b.get("approx") or old.get("approx"):
            m[(d, b["t"])] = dict(b, d=d)
    return [m[k] for k in sorted(m)]


OF_DIR = os.path.join(os.environ.get("MP_DHAN_DIR") or os.path.join(HOME, "Documents", "dhan"), "orderflow", "data", "of")
_OFC: dict = {}


def of_min_bars(sym: str, days: int = 20) -> list:
    """Nifty / Bank Nifty 1-min footprint from the Dhan order-flow engine (trades classified against the 20-level book:
    at/above ask = buy, at/below bid = sell, else tick rule). Checked 2026-10-23 against the tracker's own snapshot
    classification: same volume, but its delta tracks price far better (15-min corr 0.59 vs 0.20) - so it is the source."""
    out = []
    try:
        dates = sorted(d for d in os.listdir(OF_DIR) if len(d) == 10 and d[4] == "-")[-days:]
    except Exception:
        return out
    for d in dates:
        f = os.path.join(OF_DIR, d, f"footprint_{sym}.jsonl")
        try:
            mt = os.path.getmtime(f)
        except Exception:
            continue
        ck = _OFC.get(f)
        if not ck or ck[0] != mt:
            bars = []
            for line in open(f):
                try:
                    b = json.loads(line)
                except Exception:
                    continue
                if b.get("o") is None or not (b.get("buy") or b.get("sell")) or not ("09:15" <= str(b.get("time")) < "15:30"):
                    continue
                bars.append({"t": b.get("time"), "d": d, "o": b["o"], "h": b["h"], "l": b["l"], "c": b["c"],
                             "delta": (b.get("buy") or 0) - (b.get("sell") or 0), "total": (b.get("buy") or 0) + (b.get("sell") or 0),
                             "bins": [{"p": lv[0], "b": lv[2], "s": lv[1]} for lv in (b.get("levels") or [])], "approx": False})   # engine writes [price, sell, buy]
            _OFC[f] = ck = (mt, bars)
        out += ck[1]
    return out


_VP5: dict = {}


def vp5(desk: str, key: str, raw: dict, days: int = 5) -> dict:
    """2026-11-06 (Nikhil: "volume profile (rolling last 5 days) on the left side"): traded volume per price over the last `days` sessions
    (Nifty / Bank Nifty: the order-flow engine's 1-min futures footprint; other markets: the stored + live 15-min footprint bars).
    {"days": [...], "bins": [[price, volume], ...], "poc": price}. Cached 60 s per market."""
    ck = (desk, key)
    h = _VP5.get(ck)
    if h and time.time() - h[0] < 60:
        return h[1]
    if desk == "nifty" and key in ("NIFTY", "BANKNIFTY"):
        bars = of_min_bars(key, days=days + 2)
    else:
        bars = fp_hist(desk, key, (raw.get("footprint") or {}).get("bars") or raw.get("fp") or [])
    ds = sorted({b.get("d") for b in bars if b.get("d")})[-days:]
    vol: dict = {}
    for b in bars:
        if b.get("d") not in ds:
            continue
        for c in b.get("bins") or []:
            try:
                p = float(c["p"]); vol[p] = vol.get(p, 0.0) + float(c.get("b") or 0) + float(c.get("s") or 0)
            except Exception:
                continue
    out = {"days": ds, "bins": sorted([[p, round(v, 1)] for p, v in vol.items()]), "poc": max(vol, key=vol.get) if vol else None}
    _VP5[ck] = (time.time(), out)
    return out


_FLV: dict = {}


def fut_levels(key: str) -> dict:
    """2026-11-06 (Nikhil: "lets keep every thing future"): futures-native levels for the Nifty desk chart, from the order-flow engine's full
    1-min footprint (no spot -> futures guessing): previous session high / low, today's IB (09:15-10:15, once 10:15 has passed), VWAP from
    every traded lot today, and the live basis = median over the last ~10 option-chain snapshots of (futures 1-min close - chain spot) at the
    same minute (the tracker's own spot / futures quotes lag each other by up to ~20 pts). Cached 30 s."""
    c = _FLV.get(key)
    if c and time.time() - c[0] < 30:
        return c[1]
    out = {}
    try:
        B = of_min_bars(key, days=6)
        days = sorted({b["d"] for b in B})
        if days:
            T = [b for b in B if b["d"] == days[-1]]
            if len(days) > 1:
                P = [b for b in B if b["d"] == days[-2]]
                out.update(pdh=max(b["h"] for b in P), pdl=min(b["l"] for b in P), prev_day=days[-2])
            ib = [b for b in T if str(b["t"]) < "10:15"]
            if ib and any(str(b["t"]) >= "10:15" for b in T):
                out.update(ibh=max(b["h"] for b in ib), ibl=min(b["l"] for b in ib))
            pv = vv = 0.0
            for b in T:
                for x in b.get("bins") or []:
                    q = (x.get("b") or 0) + (x.get("s") or 0); pv += q * x["p"]; vv += q
            if vv > 0:
                out["vwap"] = round(pv / vv, 2)
            try:
                import pandas as pd
                o = pd.read_csv(os.path.join(ROOT, "data_store", "flow", "option_chain", f"{days[-1]}.csv.gz"), usecols=["ts", "symbol", "underlying"])
                o = o[o.symbol == key].groupby("ts").underlying.first()
                cl = {str(b["t"])[:5]: b["c"] for b in T}
                pairs = [(cl[t[11:16]] - u) for t, u in o.items() if t[11:16] in cl][-10:]
                if len(pairs) >= 3:
                    pairs.sort(); out["basis"] = round(pairs[len(pairs) // 2], 1); out["basis_n"] = len(pairs)
            except Exception:
                pass
    except Exception as e:
        out["err"] = str(e)[:120]
    _FLV[key] = (time.time(), out)
    return out


def _fp_source(desk: str, key: str, bars: list):
    """(bars, source) for the footprint chart: Dhan order-flow engine 1-min bars for NIFTY / BANKNIFTY when today has data,
    else the tracker's 15-min bars (+ stored real history)."""
    if desk == "nifty" and key in ("NIFTY", "BANKNIFTY"):
        ob = of_min_bars(key)
        if ob:
            try:                       # the minute still forming (engine live.json, every 2 s) so the chart moves live
                lj = json.load(open(OF_LIVE))
                fn = ((lj.get("instruments") or {}).get(key) or {}).get("footprint_now")
                if fn and lj.get("date") == ob[-1]["d"] and time.time() - os.path.getmtime(OF_LIVE) < 15 and str(fn.get("time")) > str(ob[-1]["t"]) \
                        and "09:15" <= str(fn.get("time")) < "15:30":
                    ob = ob + [{"t": fn["time"], "d": lj["date"], "o": fn["o"], "h": fn["h"], "l": fn["l"], "c": fn["c"],
                                "delta": (fn.get("buy") or 0) - (fn.get("sell") or 0), "total": (fn.get("buy") or 0) + (fn.get("sell") or 0),
                                "bins": [{"p": lv[0], "b": lv[2], "s": lv[1]} for lv in (fn.get("levels") or [])], "approx": False}]
            except Exception:
                pass
            return ob, "of1m"
    return fp_hist(desk, key, bars), "tracker15"


def fp_tf(bars: list, tf: int, desk: str, src: int = 15) -> list:
    """Merge 15-min footprint bars into tf-minute bars. Crypto / metals / FX buckets are UTC clock buckets (00:00, 04:00 ...);
    Nifty buckets start at the 09:15 open (09:15, 09:45 ... / 09:15, 13:15). 'n' = how many 15-min bars are inside,
    'partial' = the bucket is not complete (still forming, or history starts inside it)."""
    bars = [b for b in (bars or []) if isinstance(b, dict) and b.get("t")]
    off = 555 if desk == "nifty" else 0
    groups, day, prev = [], 0, None
    for b in bars:
        try:
            hh, mm = map(int, str(b["t"])[:5].split(":"))
        except Exception:
            continue
        m = hh * 60 + mm
        if prev is not None and m < prev:
            day += 1
        prev = m
        k = (b.get("d") or day, (m - off) // tf)
        if not groups or groups[-1][0] != k:
            groups.append((k, []))
        groups[-1][1].append(b)
    ps = sorted({float(c["p"]) for b in bars for c in (b.get("bins") or []) if c.get("p") is not None})
    bw = min((ps[i + 1] - ps[i] for i in range(len(ps) - 1) if ps[i + 1] - ps[i] > 0), default=1.0)
    out = []
    for gi, ((dd, bk), bs) in enumerate(groups):
        cells = {}
        for b in bs:
            for c in b.get("bins") or []:
                o = cells.setdefault(float(c["p"]), [0.0, 0.0]); o[0] += float(c.get("b") or 0); o[1] += float(c.get("s") or 0)
        total = sum(_num(b.get("total")) or 0 for b in bs)
        min_vol = max(1.0, total * 0.02) if total >= 50 else total * 0.02
        bi, si = [], []
        for pz, (bv, sv) in cells.items():
            dn = cells.get(round(pz - bw, 8)) or next((v for q, v in cells.items() if abs(q - (pz - bw)) < bw * 1e-3), None)
            up = cells.get(round(pz + bw, 8)) or next((v for q, v in cells.items() if abs(q - (pz + bw)) < bw * 1e-3), None)
            if bv >= 3.0 * max(dn[1] if dn else 0.0, min_vol) and bv >= min_vol: bi.append(pz)
            if sv >= 3.0 * max(up[0] if up else 0.0, min_vol) and sv >= min_vol: si.append(pz)
        start = (bk * tf + off) % 1440
        need = tf // src if desk != "nifty" else min(tf // src, max(1, (930 - (bk * tf + off)) // src))
        out.append({"t": f"{start // 60:02d}:{start % 60:02d}", "o": bs[0].get("o"), "c": bs[-1].get("c"),
                    "h": max(_num(b.get("h")) or -1e18 for b in bs), "l": min(_num(b.get("l")) or 1e18 for b in bs),
                    "delta": sum(_num(b.get("delta")) or 0 for b in bs), "total": total,
                    "poc": max(cells, key=lambda q: sum(cells[q])) if cells else None,
                    "bins": [{"p": q, "b": v[0], "s": v[1]} for q, v in sorted(cells.items())],
                    "buy_imb": sorted(bi), "sell_imb": sorted(si), "n": len(bs), "partial": len(bs) < need * 0.9,
                    "approx": any(b.get("approx") for b in bs), "d": bs[0].get("d") or dd,
                    "real_delta": sum(_num(b.get("delta")) or 0 for b in bs if not b.get("approx"))})
    # one CVD for the whole desk (2026-10-23): running total of REAL bar deltas, reset each session
    # (00:00 UTC for crypto / metals / FX, 09:15 IST for Nifty). Estimated bars (rebuilt from candles) add nothing.
    run, sess = 0.0, None
    for b in out:
        if b["d"] != sess:
            sess, run = b["d"], 0.0
        run += b["real_delta"]
        b["cvd"] = run
    # 2026-11-06 (Nikhil: all candles for the day): every bar of the latest session (was the last 24 only)
    n_last = sum(1 for b in out if out and b["d"] == out[-1]["d"])
    return out[-max(24, n_last):]


def tf_flow(bars_tf: list, spot) -> dict | None:
    """Order-flow facts on one timeframe: current bar and last completed bar delta (as a share of volume), 3-bar delta,
    price vs the current bar POC, stacked imbalances, and whether the bars are estimates rebuilt from candles."""
    if not bars_tf:
        return None
    r = lambda b: round(b["delta"] / b["total"], 3) if b.get("total") else None
    cur = bars_tf[-1]; prev = bars_tf[-2] if len(bars_tf) > 1 else None
    l3 = bars_tf[-3:]; t3 = sum(b.get("total") or 0 for b in l3)
    poc = cur.get("poc")
    return {"cur_delta": r(cur), "prev_delta": r(prev) if prev else None,
            "delta_3bars": round(sum(b.get("delta") or 0 for b in l3) / t3, 3) if t3 else None,
            "vs_poc": None if poc is None or spot is None else (1 if spot > poc else -1 if spot < poc else 0),
            "stacked_buy": len(cur.get("buy_imb") or []), "stacked_sell": len(cur.get("sell_imb") or []),
            "bars_in_cur": cur.get("n"), "estimated": bool(cur.get("approx") or (prev or {}).get("approx")),
            "cvd_session": cur.get("cvd")}


def view_of(desk: str, key: str, inst: dict) -> dict:
    """Slim live picture for the Live tab's own chart: footprint bars, levels, OI walls, CVD, OI change."""
    vp, mp, pa = inst.get("volume_profile") or {}, inst.get("market_profile") or {}, inst.get("price_action") or {}
    oi, fp, imb, reg = inst.get("oi") or {}, inst.get("footprint") or {}, inst.get("imbalance") or {}, inst.get("regime") or {}
    bars = fp.get("bars") or []
    if isinstance(bars, dict):
        bars = list(bars.values())
    spot = _num(inst.get("spot"))
    chain = []
    for r in (oi.get("chain") or []):
        if isinstance(r, dict) and r.get("strike") is not None:
            chain.append({k: r.get(k) for k in ("strike", "ce_oi", "pe_oi", "ce_chg", "pe_chg", "atm")})
    if spot and chain:
        chain = sorted(chain, key=lambda r: abs(float(r["strike"]) - spot))[:11]
        chain.sort(key=lambda r: float(r["strike"]))
    s = inst.get("setup") or {}
    return {"desk": desk, "key": key, "spot": spot, "fut": inst.get("fut"),
            **dict(zip(("fp", "fp_src"), _fp_source(desk, key, bars))),
            "fut_lv": fut_levels(key) if desk == "nifty" and key in ("NIFTY", "BANKNIFTY") else None,   # 2026-11-06: futures-native levels + basis   # 2026-11-03 audit: unused "cvd" series (it held per-bar delta, not CVD) dropped
            # support / resistance (2026-10-23): the tracker's swing levels + previous day / week high-low
            "sr": {"support": [x for x in (pa.get("support") or []) if x is not None][:4],
                   "resistance": [x for x in (pa.get("resistance") or []) if x is not None][:4],
                   "PDH": (inst.get("prior_levels") or {}).get("pdh"), "PDL": (inst.get("prior_levels") or {}).get("pdl"),
                   "PWH": (inst.get("prior_levels") or {}).get("pwh"), "PWL": (inst.get("prior_levels") or {}).get("pwl")},
            "levels": {"POC": vp.get("poc"), "VAH": vp.get("vah"), "VAL": vp.get("val"), "VWAP": pa.get("vwap"),
                       "IB high": mp.get("ib_high"), "IB low": mp.get("ib_low")},
            "walls": {"call": oi.get("call_wall"), "put": oi.get("put_wall")},
            "depth": {k: (inst.get("depth") or {}).get(k) for k in ("imb20", "imb20_1m", "imb20_5m", "bid_wall_px", "bid_wall_qty", "ask_wall_px", "ask_wall_qty", "stale")} if inst.get("depth") else None,
            "oi": {k: oi.get(k) for k in ("pcr", "max_pain", "options_bias", "bias_detail", "oi_chg_pct", "funding_annual_pct", "net_bias", "liq_up", "liq_dn") if oi.get(k) is not None},
            "chain": chain, "day_type": mp.get("day_type"), "regime": reg.get("regime"), "stance": reg.get("stance"),
            "delta_state": imb.get("delta_state"), "cum_delta": imb.get("cum_delta"), "absorption": imb.get("absorption"),
            "setup": {k: s.get(k) for k in ("state", "grade", "side", "mode", "level_name", "reasons", "entry", "stop_loss", "target1")} if s else None,
            # 2026-10-31: same chart marks as the crypto tracker dashboard - absorption reversals at levels (strong / moderate)
            # and the swing delta divergence (exhaustion: new price extreme without new delta extreme)
            "rev": [{k: r.get(k) for k in ("t", "side", "level", "level_name", "delta", "strength", "note")} for r in (fp.get("reversals") or []) if isinstance(r, dict)][-24:],
            "div": ((inst.get("divergence") or {}).get("swing") or None)}




def _instruments(desk: str, snap: dict, C: dict) -> dict:
    ins = snap.get("instruments") or {}
    if desk == "crypto":
        return {k: v for k, v in ins.items() if k in C["crypto_coins"]}
    return ins


def poll_once():
    C = cfg()
    t = now_ist()
    with _LOCK:
        for desk, D in DESKS.items():
            stt = {"at": t.isoformat(timespec="seconds"), "ok": False}
            try:
                snap = _crypto_engine().snapshot() if D.get("inproc") else _get(D["url"])
                stt.update(ok=True, generated=snap.get("generated_at"), mode=snap.get("mode"))
            except Exception as e:
                stt["error"] = str(e)[:80]
                _STATE["desks"][desk] = stt
                if desk in TRACKER_DIRS and C.get("autostart_clones", True):      # keep the Gold & Silver tracker running
                    n = _STATE.setdefault("down", {}).get(desk, 0) + 1
                    _STATE["down"][desk] = n
                    if n % 30 == 3:                                               # ~6 s after it goes down, then once a minute
                        try:
                            stt["autostart"] = start_tracker(desk).get("detail")
                        except Exception as ex:
                            stt["autostart"] = str(ex)[:80]
                continue
            _STATE.setdefault("down", {})[desk] = 0
            _STATE.setdefault("spots", {})[desk] = {k: _num(v.get("spot")) for k, v in (snap.get("instruments") or {}).items()}
            if desk == "nifty":
                try:
                    pcr_record(snap, t)
                except Exception as e:
                    _STATE["pcr_error"] = str(e)[:120]
            stt["flow"] = flow_feed() if desk == "nifty" else {"ok": True}
            watch, views = {}, {}
            for key, inst in _instruments(desk, snap, C).items():
                try:
                    views[key] = view_of(desk, key, inst)
                    try:
                        fp_store(desk, key, (inst.get("footprint") or {}).get("bars") or [])
                    except Exception as e:
                        _STATE["fp_store_error"] = str(e)[:120]
                except Exception as e:
                    views[key] = {"error": str(e)[:80]}
                px = _num(inst.get("spot"))
                s = inst.get("setup") or {}
                watch[key] = {"spot": px, "state": s.get("state"), "grade": s.get("grade"), "side": s.get("side"),
                              "setup": setup_name(s) if s else None, "level": s.get("level_name") or s.get("level"),
                              "regime": (inst.get("regime") or {}).get("regime"),
                              "cvd": (inst.get("imbalance") or {}).get("cum_delta"),
                              "call_wall": (inst.get("oi") or {}).get("call_wall"), "put_wall": (inst.get("oi") or {}).get("put_wall")}
            # 2026-11-03 (Nikhil): tracker setup cards dropped permanently - no cards are opened, graded or alerted.
            try:                                   # 4H block-break paper trades (ETH / BTC / GOLD), throttled inside
                B4 = _hot("break4h")
                B4.tick()
            except Exception as e:
                _STATE["break4h_error"] = str(e)[:160]
            try:                                   # 2026-11-06 (Nikhil): one CSV of every paper trade, all rules / tabs, every 5 min (app/tradelog.py);
                _hot("tradelog").tick(_hot, {})    # 2026-11-08: runs for every desk now (throttled to 5 min inside), so weekends / evenings update too
            except Exception as e:
                _STATE["tradelog_error"] = str(e)[:160]
            try:                                   # 2026-11-08 (Nikhil): F&O Flow minute snapshots (own hours / weekday check inside)
                _hot("fnoflow").tick()
            except Exception as e:
                _STATE["fnoflow_error"] = str(e)[:160]
            try:                                   # 2026-11-08 (Nikhil): Stock Volume Break v1 paper rule (app/stockvb.py) - own hours / weekday check inside
                _hot("stockvb").tick()
            except Exception as e:
                _STATE["stockvb_error"] = str(e)[:160]
            try:                                   # 2026-11-07 (Nikhil): crypto rise / fall score every 5 min (fetch + score + log + check)
                _hot("cryptoscore").tick()
            except Exception as e:
                _STATE["cryptoscore_error"] = str(e)[:160]
            try:                                   # 2026-11-07 (Nikhil): keep saving Binance OI / long-short / taker data hourly (app/derivhist.py)
                _hot("derivhist").tick()
            except Exception as e:
                _STATE["derivhist_error"] = str(e)[:160]
            try:                                   # 2026-11-05: keep the MCX order-flow recorder up in MCX hours (app/mcxwatch.py)
                _STATE["mcx_recorder"] = _hot("mcxwatch").tick()
            except Exception as e:
                _STATE["mcx_recorder"] = f"error: {str(e)[:120]}"
            try:                                   # hourly 5m/15m verdict per market (once per hour slot, logged)
                _hot("hourverdict").tick()
            except Exception as e:
                _STATE["hourverdict_error"] = str(e)[:160]
            if desk == "nifty":
                try:
                    setups_tick(t, views)
                except Exception as e:
                    _STATE["setups_error"] = str(e)[:160]
                try:                               # 2026-11-03: DOM hold / break paper test (app/domcards.py), 09:15-15:35
                    _hot("domcards").tick(_hot, views)
                except Exception as e:
                    _STATE["domcards_error"] = str(e)[:160]
                # 2026-11-06 (Nikhil: "retire and fully remove"): Strong Block Touch v2 retired - no longer ticked (code + data moved to _archive/2026-11-03/backups/2026-11-06_retire)
                try:                               # 2026-11-06: Overnight hold paper rule (app/overnight.py): signal 15:13, exit next day ~09:16
                    _hot("overnight").tick()
                except Exception as e:
                    _STATE["overnight_error"] = str(e)[:160]
                try:                               # 2026-11-06: Nifty day / night vol cycle paper rules (app/volcycle.py): day 10:30-15:25, night 15:25-next 09:45
                    _hot("volcycle").tick()
                except Exception as e:
                    _STATE["volcycle_error"] = str(e)[:160]
                # 2026-11-08: the trade-log CSV tick moved below (it ran only while the Nifty tracker was up, so it froze after hours / weekends)
                try:                               # 2026-11-05: Stocks in Play ORB paper rule (app/stockorb.py), MIS 5x, new - nothing else changed
                    _hot("stockorb").tick()
                except Exception as e:
                    _STATE["stockorb_error"] = str(e)[:160]
            if desk == "crypto" and time.time() - _STATE.get("edge_t", 0) > 300:   # Edge board job: momentum paper basket (Block story removed 2026-11-03)
                _STATE["edge_t"] = time.time()
                try:
                    _hot("xsecpaper").tick()
                except Exception as e:
                    _STATE["xsec_error"] = str(e)[:120]
            try:                                   # chart annotations: log setup changes (2026-10-25)
                _hot("chartmarks").record(desk, watch, t)
            except Exception as e:
                _STATE["chartmarks_error"] = str(e)[:120]
            stt["watch"] = watch
            _STATE.setdefault("views", {})[desk] = views
            _STATE["desks"][desk] = stt


# ---------------- PCR tracker (2026-10-23): one row per index every 5 min in NSE hours, one CSV per day ----------------
PCR_DIR = os.path.join(DIR, "pcr")
PCR_COLS = ["time", "key", "spot", "pcr_near", "pcr_all", "call_oi_near", "put_oi_near", "d_call", "d_put",
            "pcr_chg_5m", "pcr_chg_day", "spot_chg_5m", "max_pain", "call_wall", "put_wall", "window_ok", "read"]


def _pcr_path(day: str) -> str:
    return os.path.join(PCR_DIR, f"{day}.csv")


def _pcr_rows(day: str) -> list:
    import csv
    try:
        with open(_pcr_path(day)) as f:
            return list(csv.DictReader(f))
    except Exception:
        return []


def _pcr_read(dp, dpx):
    """Plain-language read of the last 5 min. Context only - our 2-year test found no forecasting edge in OI flow alone."""
    if dp is None or dpx is None or (abs(dp) < 0.005):
        return "flat"
    if dp > 0 and dpx >= 0: return "puts added on rise - bulls supported"
    if dp > 0 and dpx < 0: return "puts added on fall - support building / hedging"
    if dp < 0 and dpx > 0: return "calls added on rise - resistance building"
    return "calls added on fall - bears in control"


def pcr_record(snap: dict, t: datetime):
    if t.weekday() >= 5 or not (_hm("09:15") <= t.hour * 60 + t.minute <= _hm("15:30")):
        return
    bucket = t.replace(minute=t.minute - t.minute % 5, second=0, microsecond=0).strftime("%H:%M")
    last = _STATE.setdefault("pcr_last", {})
    day = t.strftime("%Y-%m-%d")
    rows_out = []
    for key in ("NIFTY", "BANKNIFTY"):
        if last.get(key) == (day, bucket):
            continue
        inst = (snap.get("instruments") or {}).get(key) or {}
        oi = inst.get("oi") or {}; chain = oi.get("chain") or []; spot = _num(inst.get("spot"))
        if not chain or not spot:
            continue
        near = sorted(chain, key=lambda r: abs((r.get("strike") or 0) - spot))[:21]          # ATM +-10 strikes
        c_n = sum(r.get("ce_oi") or 0 for r in near); p_n = sum(r.get("pe_oi") or 0 for r in near)
        c_a = sum(r.get("ce_oi") or 0 for r in chain); p_a = sum(r.get("pe_oi") or 0 for r in chain)
        ks = [r.get("strike") for r in chain if r.get("strike")]
        prev = [r for r in _pcr_rows(day) if r["key"] == key]
        pn = round(p_n / c_n, 3) if c_n else None
        f = lambda r, k: float(r[k]) if r.get(k) not in (None, "") else None
        p5 = f(prev[-1], "pcr_near") if prev else None; s5 = f(prev[-1], "spot") if prev else None
        p0 = f(prev[0], "pcr_near") if prev else None
        dp = round(pn - p5, 3) if pn is not None and p5 is not None else None
        dpx = round(spot - s5, 2) if s5 else None
        rows_out.append({"time": bucket, "key": key, "spot": spot, "pcr_near": pn, "pcr_all": round(p_a / c_a, 3) if c_a else None,
                         "call_oi_near": int(c_n), "put_oi_near": int(p_n),
                         "d_call": int(c_n - f(prev[-1], "call_oi_near")) if prev else None,
                         "d_put": int(p_n - f(prev[-1], "put_oi_near")) if prev else None,
                         "pcr_chg_5m": dp, "pcr_chg_day": round(pn - p0, 3) if pn is not None and p0 is not None else None,
                         "spot_chg_5m": dpx, "max_pain": oi.get("max_pain"), "call_wall": oi.get("call_wall"), "put_wall": oi.get("put_wall"),
                         "window_ok": bool(ks and min(ks) <= spot <= max(ks)), "read": _pcr_read(dp, dpx)})
        last[key] = (day, bucket)
    if rows_out:
        import csv
        os.makedirs(PCR_DIR, exist_ok=True)
        new = not os.path.exists(_pcr_path(day))
        with open(_pcr_path(day), "a", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=PCR_COLS)
            if new: w.writeheader()
            for r in rows_out: w.writerow(r)


# ---------- trade setups (2026-10-24): app/setups.py, reloaded when the file changes ----------
_SETUPS = {"mt": None, "mod": None}


def _setups_mod():
    import importlib
    here = os.path.dirname(os.path.abspath(__file__))
    mt = (os.path.getmtime(os.path.join(here, "setups.py")), os.path.getmtime(os.path.join(here, "prep.py")))
    if _SETUPS["mod"] is None or _SETUPS["mt"] != mt:
        import prep as P
        if _SETUPS["mod"] is not None:
            opens = dict(P.OPEN); P = importlib.reload(P); P.OPEN.update(opens)     # keep today's session opens
        import setups as S
        _SETUPS["mod"] = importlib.reload(S); _SETUPS["mt"] = mt
    return _SETUPS["mod"]


_HOT: dict = {}
def _bs_hv(desk, key):
    """Blocks for Block watch / Block story: the Verdict's blocks; for a Nifty 50 stock, the stock view's (cached 3 min)."""
    if desk == "nifty" and key not in ("NIFTY", "BANKNIFTY"):
        x = _STATE.setdefault("_stk_hv", {}).get(key)
        if x and time.time() - x[0] < 180:
            return x[1]
        try:
            h = (_hot("stockview").analyze(key) or {}).get("hourly")
        except Exception:
            h = None
        _STATE["_stk_hv"][key] = (time.time(), h)
        return h
    return (_hot("hourverdict").S.get("last") or {}).get(key)


def _hot(name: str):
    """Import a helper module and reload it when its file changes (no Market Pulse restart needed)."""
    import importlib, sys
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), name + ".py")
    mt = os.path.getmtime(p)
    m = sys.modules.get(name)
    if m is None:
        m = importlib.import_module(name)
    elif _HOT.get(name) != mt:
        m = importlib.reload(m)
    _HOT[name] = mt
    return m

def setups_tick(t: datetime, views: dict):
    S = _setups_mod()
    spots = {k: _num((views.get(k) or {}).get("spot")) for k in S.KEYS}
    # 2026-11-03 (Nikhil): the 6-vote "Trade setups" are off the dashboard -> no longer computed. The overnight prep still
    # needs the day's real open price, which S.tick used to record.
    for k in S.KEYS:
        try:
            if spots.get(k):
                S.PREP.note_open(k, t, spots[k])
        except Exception as e:
            _STATE["prep_open_error"] = str(e)[:120]
    try:                                   # option-writer monitor log (one line per new option-chain snapshot)
        OW = _hot("oiwatch")
        OW.log_tick()
    except Exception as e:
        _STATE["oiwatch_error"] = str(e)[:200]
    try:                                   # research recorder: every key-level touch with footprint + positions
        TL = _hot("touchlog")
        TL.tick(t, spots, views, S, S.PREP, of_min_bars)
    except Exception as e:
        _STATE["touchlog_error"] = str(e)[:200]
    try:                                   # 2026-10-30: option-buying paper cards for touch-minute confirmation (Nikhil)
        _hot("touchtrades").tick(t, spots)
    except Exception as e:
        _STATE["touchtrades_error"] = str(e)[:200]


def loop():
    """Polls forever. Hot-reloads this file when it changes, so code fixes apply without restarting Market Pulse."""
    import importlib
    import sys
    _STATE["started"] = now_ist().isoformat(timespec="seconds")
    me, mt = sys.modules[__name__], os.path.getmtime(__file__)
    while True:
        try:
            if os.path.getmtime(__file__) != mt:
                mt = os.path.getmtime(__file__)
                st = me._STATE
                me = importlib.reload(me)
                me._STATE.update(st)
                me._STATE["reloaded"] = now_ist().isoformat(timespec="seconds")
            me.poll_once()
        except Exception as e:
            me._STATE["error"] = str(e)[:200]
        time.sleep(me.cfg()["poll_seconds"])


def start():
    threading.Thread(target=loop, daemon=True, name="livedesk").start()


_BNK = {}
BN_CVD = {"BTC", "ETH", "SOL", "BNB", "XRP", "UNI", "ZEC", "LINK", "DASH"}       # coins traded on Binance in the crypto engine


def _bn_cvd(key: str, tf: int, bars: list) -> list:
    """2026-11-03 (Nikhil: "check CVD numbers"): per-bar delta and the session CVD (reset 00:00 UTC) taken from Binance's own
    taker buy / sell split for the same bars - exact, and it survives engine restarts (bars rebuilt from candles after a
    restart had no real delta, so the running CVD missed part of the day). Footprint cells are unchanged."""
    if not bars or key not in BN_CVD:
        return bars
    iv = {15: "15m", 30: "30m", 240: "4h"}.get(tf, "15m")
    x = _BNK.get((key, iv))
    if not x or time.time() - x[0] > 20:
        try:
            d0 = min(b.get("d") or "" for b in bars) or datetime.utcnow().strftime("%Y-%m-%d")
            st = int(datetime.strptime(d0, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000)
            k = _get(f"https://fapi.binance.com/fapi/v1/klines?symbol={key}USDT&interval={iv}&startTime={st}&limit=1000", timeout=6)
        except Exception:
            return bars
        m, run, day = {}, 0.0, None
        for r in k:
            t = datetime.fromtimestamp(int(r[0]) / 1000, tz=timezone.utc)
            dd = t.strftime("%Y-%m-%d")
            if dd != day:
                day, run = dd, 0.0
            dl = 2 * float(r[9]) - float(r[5])
            run += dl
            m[(dd, t.strftime("%H:%M"))] = (dl, run, float(r[5]))
        x = (time.time(), m); _BNK[(key, iv)] = x
    m = x[1]
    for b in bars:
        v = m.get((b.get("d"), b.get("t")))
        if v:
            b["delta"], b["real_delta"], b["cvd"], b["cvd_src"] = round(v[0], 2), round(v[0], 2), round(v[1], 2), "binance"
    return bars


def view(desk: str, tf: int = 15, want=None) -> dict:
    """want = the market(s) the page shows (2026-11-03 audit): only those get footprint bars; the others send price and
    regime for the drop-down (the crypto view was 224 KB every 2 s for 9 coins when one is on screen)."""
    tf = tf if tf in TFS else 15
    if desk == "crypto" and want:      # 2026-11-04 (Nikhil): the coin on screen is recomputed every tick (was only BTC/ETH/SOL; UNI etc.
        try:                           # froze at the first seconds of each new bar while the price line kept moving)
            import crypto_live as CL
            known = set(((_STATE.get("views") or {}).get("crypto") or {}).keys())
            ks = {k for k in want if k in known}
            if ks:
                CL._E["focus"]["keys"] = ks
        except Exception as e:
            _STATE["crypto_focus_error"] = str(e)[:120]
    ins = {}
    for k, v in ((_STATE.get("views") or {}).get(desk) or {}).items():
        if want and k not in want:
            ins[k] = {x: v.get(x) for x in ("desk", "key", "spot", "fut", "regime", "stance", "day_type")}
            continue
        try:
            ins[k] = dict(v, fp=fp_tf(v.get("fp") or [], tf, desk, 1 if v.get("fp_src") == "of1m" else 15))
            if desk == "crypto" and k in BN_CVD:
                ins[k]["fp"] = _bn_cvd(k, tf, ins[k]["fp"])
                if ins[k]["fp"] and ins[k]["fp"][-1].get("cvd_src") == "binance":
                    ins[k]["cum_delta"] = round(ins[k]["fp"][-1]["cvd"])
        except Exception:
            ins[k] = dict(v, fp=(v.get("fp") or [])[-24:])
    if desk == "nifty":                # 2026-10-25: tracker off (evening / weekend) -> last session's real order flow from the Dhan engine
        for k in ("NIFTY", "BANKNIFTY"):
            if ins.get(k, {}).get("fp"):
                continue
            try:
                ob = of_min_bars(k, days=12 if tf >= 60 else 3)
                if ob:
                    last = ob[-1]["d"]
                    if tf < 60:                    # 2026-11-04: 4h needs ~6 sessions to fill the chart's 12 candles
                        ob = [b for b in ob if b["d"] == last]
                    ins[k] = {"desk": desk, "key": k, "spot": ob[-1]["c"], "fp": fp_tf(ob, tf, desk, 1), "fp_src": "of1m", "cvd": [],
                              "sr": {}, "levels": {}, "walls": {}, "chain": [], "oi": {}, "last_session": last}
            except Exception:
                pass
    for k, v in ins.items():           # 2026-11-06 (Nikhil): rolling 5-day volume profile for the chart's left side
        if isinstance(v, dict) and v.get("fp"):
            try:
                v["vp5"] = vp5(desk, k, ((_STATE.get("views") or {}).get(desk) or {}).get(k) or {})
            except Exception as e:
                _STATE["vp5_error"] = str(e)[:120]
            if desk == "nifty" and k in ("NIFTY", "BANKNIFTY"):      # 2026-11-07 (Nikhil): big walls on the order-flow chart
                try:
                    v["bigwalls"] = _hot("bigwalls").api(k)
                except Exception as e:
                    _STATE["bigwalls_error"] = str(e)[:120]
    try:                               # 2026-10-25: OI tag / burst per bar, OI line, logged setup events (chartmarks)
        CM = _hot("chartmarks")
        for k, v in ins.items():
            if isinstance(v, dict) and v.get("fp"):
                v["fp"] = [dict(b) for b in v["fp"]]
                v["oi_line"] = CM.annotate(desk, k, v["fp"], tf)
                v["events"] = CM.events(desk, k)
    except Exception as e:
        _STATE["chartmarks_error"] = str(e)[:120]
    return {"desk": desk, "tf": tf, "at": (_STATE["desks"].get(desk) or {}).get("at"), "ok": (_STATE["desks"].get(desk) or {}).get("ok"),
            "instruments": ins}


def status(desk: str | None = None) -> dict:
    """Is each live tracker feed up? (2026-11-03: tracker setup cards dropped - no cards here any more.)"""
    out = {"day": now_ist().strftime("%Y-%m-%d"), "desks": {}}
    for d in DESKS:
        if desk and d != desk:
            continue
        out["desks"][d] = dict(_STATE["desks"].get(d) or {"ok": False, "error": "not polled yet"})
    return out


# ---------- start a clone tracker from Market Pulse (2026-10-23) ----------
# The Gold & Silver tracker runs from Documents/metals-tracker. Their .command files
# copied in by Claude are not executable on the Mac, so Market Pulse starts the supervisor with bash (same as the .command does).
TRACKER_DIRS = {"metals": "metals-tracker"}


def start_tracker(desk: str, restart: bool = False) -> dict:
    import subprocess
    name = TRACKER_DIRS.get(desk)
    if desk == "crypto":                  # 2026-11-01: in-process engine (app/crypto_live.py)
        import crypto_live as CL
        return CL.restart() if restart else (CL.start() if not CL._E["running"] else {"ok": True, "detail": "already running"})
    if not name:
        return {"ok": False, "detail": "unknown tracker"}
    d = os.environ.get("MP_METALS_DIR") if name == "metals-tracker" and os.environ.get("MP_METALS_DIR") else os.path.abspath(os.path.join(ROOT, "..", "..", name))
    if not os.path.exists(os.path.join(d, "run_forever.sh")):
        return {"ok": False, "detail": f"{name} not found"}
    if restart:
        subprocess.run(["pkill", "-f", f"{name}/run_forever.sh"]); subprocess.run(["pkill", "-f", f"run_{name.split('-')[0]}.py --mode live"])
        time.sleep(1)
    try:
        if subprocess.run(["pgrep", "-f", f"{name}/run_forever.sh"], capture_output=True).stdout.strip():
            return {"ok": True, "detail": "already running"}
    except Exception:
        pass
    subprocess.Popen(["/bin/bash", os.path.join(d, "run_forever.sh")], cwd=d, stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    return {"ok": True, "detail": f"{DESKS[desk]['name']} tracker starting - live in about 1-2 minutes"}


def handle_get(path: str, q: dict):
    """All GET /api/livedesk/* routes live here so they hot-reload with this file."""
    if path == "/api/livedesk/diag":                   # 2026-11-03 audit: errors kept by the loops, threads, memory (read-only)
        import collections, resource, sys as _sys
        th = collections.Counter(t.name.rstrip("0123456789-") for t in threading.enumerate())
        return {"errors": {k: v for k, v in _STATE.items() if k.endswith("_error") or k == "error"},
                "threads": dict(th.most_common()), "n_threads": threading.active_count(),
                "rss_mb": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (1024 * 1024 if _sys.platform == "darwin" else 1024)),
                "crypto": __import__("crypto_live").status(), "started": _STATE.get("started"), "reloaded": _STATE.get("reloaded")}
    if path == "/api/livedesk/domcards":               # 2026-11-03: DOM hold / break paper trades + record
        return _hot("domcards").api()
    if path == "/api/livedesk/dom":                    # 2026-11-03: DOM ladder (Dhan order-flow engine, 20 levels, every 2 s)
        try:
            L = json.load(open(OF_LIVE))
        except Exception as e:
            return {"ok": False, "error": f"order-flow engine not running ({str(e)[:60]})"}
        key = q.get("key", "NIFTY")
        I = (L.get("instruments") or {}).get(key) or {}
        S = (L.get("stocks") or {}).get(key)
        if not I and S:                                 # 2026-11-03: liquid F&O stocks (cash book, same depth connection)
            qb, qa = sum(r[1] for r in S["bids"]), sum(r[1] for r in S["asks"])
            return {"ok": True, "key": key, "at": L.get("at"), "contract": f"{key} (cash, NSE)", "ladder": S,
                    "last": None, "bid": S["bids"][0][0], "ask": S["asks"][0][0],
                    "imb20": round((qb - qa) / (qb + qa), 2) if qb + qa else None, "feeds": L.get("feeds"), "error": None}
        now = I.get("now") or {}
        return {"ok": bool(I.get("ladder")), "key": key, "at": L.get("at"), "contract": (L.get("contracts") or {}).get(key),
                "ladder": I.get("ladder"), "last": now.get("last"), "bid": now.get("bid"), "ask": now.get("ask"),
                "imb20": now.get("imb20"), "cvd": I.get("cvd"), "walls": I.get("walls"),
                "absorption": I.get("absorption_counts"), "feeds": L.get("feeds"),
                "error": None if I.get("ladder") else "no order book yet (market opens 09:15; the engine needs a minute to connect)"}
    if path == "/api/livedesk/run_tool" and q.get("name") == "restart_of_engine":  # 2026-11-03: restart the Dhan order-flow engine (loads code changes)
        import subprocess
        d = os.path.join(os.environ.get("MP_DHAN_DIR") or os.path.join(HOME, "Documents", "dhan"), "orderflow")
        subprocess.run(["pkill", "-f", "of_engine.py"])
        time.sleep(2)
        log = open(os.path.join(d, "of_engine.log"), "a")
        p = subprocess.Popen((["caffeinate", "-i"] if __import__("shutil").which("caffeinate") else []) + [os.path.join(os.environ.get("MP_DHAN_DIR") or os.path.join(HOME, "Documents", "dhan"), "venv", "bin", "python"), "-u", "of_engine.py"],
                             cwd=d, stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
        return {"ok": True, "pid": p.pid, "detail": "order-flow engine restarting - book back in about a minute"}
    if path == "/api/livedesk/run_tool" and q.get("name") == "tabnews_now":
        import importlib
        import news as _nw
        importlib.reload(_nw)
        T = _hot("tabnews")
        r = T.run(force=True)
        return {t: {"kept": len(v.get("items", [])), "scanned": v.get("scanned"), "errors": v.get("errors") or v.get("error")} for t, v in (r or {}).get("tabs", {}).items()}
    if path == "/api/livedesk/fnoflow":                # 2026-11-08 (Nikhil): F&O Flow tab - every F&O name from its futures (app/fnoflow.py)
        return _hot("fnoflow").api(q)
    if path == "/api/livedesk/bigwalls":               # 2026-11-07 (Nikhil): big resting orders in the Nifty / Bank Nifty book (app/bigwalls.py)
        return _hot("bigwalls").api(q.get("sym") or "NIFTY")
    if path == "/api/livedesk/marketscore":            # 2026-11-07 (Nikhil): same score for Indian futures, metals, commodities (app/marketscore.py)
        return _hot("marketscore").api(q.get("view") or "india")
    if path == "/api/livedesk/cryptoscore":            # 2026-11-07 (Nikhil): SOL / BTC / ETH rise-fall score, 15m / 1h / 4h (app/cryptoscore.py)
        return _hot("cryptoscore").api()
    if path == "/api/livedesk/run_tool" and q.get("name") == "fetch_deriv":         # 2026-11-07: Binance OI / long-short / funding / CVD history (research)
        return _hot("derivhist").start([c for c in (q.get("coins") or "").split(",") if c] or None)
    if path == "/api/livedesk/run_tool" and q.get("name") == "deriv_status":
        return _hot("derivhist").status()
    if path == "/api/livedesk/run_tool" and q.get("name") == "fetch_mcx_hist":      # 2026-11-05: one-time MCX 1-min history (research)
        return _hot("mcxhist").start(int(q.get("days", 100)))
    if path == "/api/livedesk/run_tool" and q.get("name") == "mcx_hist_status":
        import json as _j
        try:
            return _j.load(open(os.path.join(ROOT, "data_store", "research", "mcx_intraday", "_status.json")))
        except Exception as e:
            return {"state": "not started", "error": str(e)[:80]}
    if path == "/api/livedesk/run_tool" and q.get("name") == "fetch_stock_hist":    # 2026-11-05: one-time stock 1-min history (research)
        return _hot("stockhist").start(int(q.get("days", 120)))
    if path == "/api/livedesk/run_tool" and q.get("name") == "stock_hist_status":
        import json as _j
        try:
            return _j.load(open(os.path.join(ROOT, "data_store", "research", "stocks_intraday", "_status.json")))
        except Exception as e:
            return {"state": "not started", "error": str(e)[:80]}
    if path == "/api/livedesk/run_tool" and q.get("name") == "news_now":           # 2026-11-03: refresh headlines now (and time each step)
        import importlib, traceback
        t0 = time.time()
        try:
            import news as NW
            importlib.reload(NW)
            items, errs = NW.fetch_feeds(); t1 = time.time()
            r = NW.run_all()
            return {"ok": True, "feeds_s": round(t1 - t0, 1), "total_s": round(time.time() - t0, 1), "feed_items": len(items), "feed_errors": errs[:6], "result": r}
        except Exception:
            return {"ok": False, "s": round(time.time() - t0, 1), "error": traceback.format_exc().splitlines()[-3:]}
    if path == "/api/livedesk/run_tool" and q.get("name") == "restart_pulse":     # same as Desktop 'Restart Market Pulse' (2026-10-24)
        import signal
        threading.Timer(1.5, lambda: os.kill(os.getpid(), signal.SIGTERM)).start()
        return {"ok": True, "detail": "Market Pulse restarting - the Start Trading Desk window brings it back within a minute"}
    if path == "/api/livedesk/start_tracker":
        return start_tracker(q.get("desk", ""), restart=q.get("restart") == "1")
    if path == "/api/livedesk/edgeboard":
        return _hot("edgeboard").api()
    if path == "/api/livedesk/tabnews":                   # 2026-11-04: swing catalysts per market tab (app/tabnews.py)
        return _hot("tabnews").api(q.get("tab"))
    if path == "/api/livedesk/reflevels":                 # 2026-11-04: VAH / VAL, IB, PDH / PDL ... for crypto, metals, energy (app/reflevels.py)
        return _hot("reflevels").get(q.get("key", "BTC"))
    if path == "/api/livedesk/narrative":                 # 2026-11-04: weekly global narrative (app/narrative.py)
        return _hot("narrative").api()
    if path == "/api/livedesk/blockwatch":
        desk, key = q.get("desk", "crypto"), q.get("key", "SOL")
        try:
            vw = ((_STATE.get("views") or {}).get(desk) or {}).get(key) or (view(desk, 15).get("instruments") or {}).get(key)
            return _hot("blockwatch").get(desk, key, vw, _bs_hv(desk, key))
        except Exception as e:
            return {"key": key, "error": str(e)[:160]}
    if path == "/api/livedesk/n50":
        return _hot("stockview").universe()
    if path == "/api/livedesk/stockpulse":
        try:
            return _hot("stockpulse").build(q.get("sym", "RELIANCE"))
        except Exception as e:
            return {"error": str(e)[:200]}
    if path == "/api/livedesk/stock":
        return _hot("stockview").analyze(q.get("sym", "RELIANCE"))
    if path == "/api/livedesk/hourverdict":
        return _hot("hourverdict").api(q.get("key"))
    if path == "/api/livedesk/break4h":
        B4 = _hot("break4h")
        return B4.api()
    if path == "/api/livedesk/oiblocks":
        OW = _hot("oiwatch")
        out = OW.blocks_watch(q.get("key", "NIFTY"), q.get("day"), q.get("at"))
        try:                                   # 2026-11-03: option snapshots stop ~15:00; place blocks against the latest index price
            hv = (_hot("hourverdict").S.get("last") or {}).get(q.get("key", "NIFTY")) or {}
            px = _num(hv.get("price"))
            if px and isinstance(out, dict) and out.get("blocks") is not None:
                out["oi_spot"], out["spot"], out["spot_at"] = out.get("spot"), px, hv.get("price_at")
                for b in out["blocks"]:
                    b["side"] = "support" if b["hi"] < px else "resistance" if b["lo"] > px else "price inside"
                    b["dist_pct"] = round(100 * ((b["lo"] + b["hi"]) / 2 / px - 1), 2)
        except Exception:
            pass
        return out
    if path == "/api/livedesk/levelmap":
        LM = _hot("levelmap")
        return LM.get(q.get("key", "NIFTY"), int(q.get("days", 10)))
    if path == "/api/livedesk/stratmatrix":            # 2026-10-31: strategy x regime matrix (scripts/research/strategy_regime_3y.py --matrix)
        f = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data_store", "research", "strategy_regime_3y", "matrix.json")
        try:
            d = json.load(open(f)); g = q.get("group", "india")
            out = {**(d.get(g) or {"rows": []}), "group": g, "built": datetime.fromtimestamp(os.path.getmtime(f)).isoformat(timespec="minutes")}
            if g == "crypto":                          # implied-vol (Deribit DVOL, BTC) percentile now -> size for strategies using it
                import pandas as _pd
                dv = _pd.read_csv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data_store", "market_data", "crypto_dvol.csv"))
                b = dv[dv["currency"] == "BTC"].drop_duplicates("date", keep="last").sort_values("date")["close"].astype(float).tail(253)
                if len(b) > 120:
                    pct = float((b.iloc[:-1] < b.iloc[-1]).mean() * 100)
                    out["iv_now"] = {"what": "BTC DVOL", "value": round(float(b.iloc[-1]), 1), "pct": round(pct),
                                     "size": 1.5 if pct < 33 else 0.5 if pct > 67 else 1.0,
                                     "date": str(dv[dv["currency"] == "BTC"]["date"].max())}
            return out
        except Exception as e:
            return {"error": str(e)[:160], "rows": []}
    if path == "/api/livedesk/uspaper":       # 2026-11-07 (Nikhil): US paper trades - desk cards + dashboard form (app/uspaper.py)
        try:
            return _hot("uspaper").api(q)
        except Exception as e:
            return {"ok": False, "error": str(e)[:160], "rows": []}
    if path == "/api/livedesk/papertrades":   # 2026-10-30: paper trades, all rules in one list (Nikhil); Block story dropped 2026-11-03
        px = {}
        for desk in ("crypto", "nifty", "metals"):
            for k, v in (((_STATE.get("views") or {}).get(desk) or {}).items()):
                n = _num((v or {}).get("spot")) or _num((v or {}).get("price")) or _num((v or {}).get("ltp"))
                if n: px[k] = n
        keys = [k for k in (q.get("keys") or "").split(",") if k] or None
        grp = (q.get("group") or "").strip().lower() or None      # 2026-11-04: tab filter for the "All paper trades" card
        return _hot("papertrades").api(_hot, px, keys, grp)
    if path == "/api/livedesk/tradecharts":   # 2026-11-06 (Nikhil): paper trades with their own price charts (app/tradecharts.py)
        px = {}
        for desk in ("crypto", "nifty", "metals"):
            for k, v in (((_STATE.get("views") or {}).get(desk) or {}).items()):
                n = _num((v or {}).get("spot")) or _num((v or {}).get("price")) or _num((v or {}).get("ltp"))
                if n: px[k] = n
        grp = (q.get("group") or "").strip().lower() or None
        days = int(q.get("days") or 1) if str(q.get("days") or "1").isdigit() else 1
        return _hot("tradecharts").api(_hot, px, grp, max(1, min(days, 10)))
    if path == "/api/livedesk/prep":
        return _setups_mod().PREP.prep()
    if path == "/api/livedesk/view":
        return view(q.get("desk", "nifty"), int(q.get("tf") or 15) if str(q.get("tf") or "15").isdigit() else 15,
                    set(x for x in (q.get("keys") or "").split(",") if x) or None)
    return status(q.get("desk"))
