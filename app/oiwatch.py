"""
Live option-writer monitor (2026-10-24). Indications only.

From the option-chain snapshots (~every 3 min, data_store/flow/option_chain/<today>.csv.gz), for each strike within
+-2 % of spot on the nearest expiry: OI change over the last 15 and 30 minutes, puts and calls.
Flags (writers are inferred: OI up while that option's price falls = writing; OI down = covering / leaving):
  put writers ADDING below spot        -> support being built / defended
  put writers LEAVING a strike         -> support being abandoned (23 Oct: 22600 / 22700 puts -40 % before the break)
  call writers ADDING above spot       -> resistance being built / defended (23 Oct: 22800 calls 104k -> 246k at the top)
  call writers LEAVING                 -> short covering fuel (23 Oct 13:15-13:45 before the rally)
  NEW WALL: a strike that entered today's top-3 OI on its side
Logged every new snapshot to data_store/livedesk/oiwatch/<date>.jsonl for testing (untested as a signal).
"""
from __future__ import annotations

import json
import os
from datetime import datetime

import pandas as pd

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
OCD = os.path.join(ROOT, "data_store", "flow", "option_chain")
LOG = os.path.join(ROOT, "data_store", "livedesk", "oiwatch")
_C: dict = {}
MIN_PCT, MIN_ABS = 8.0, {"NIFTY": 15000, "BANKNIFTY": 3000}


def _load(day):
    p = os.path.join(OCD, f"{day}.csv.gz")
    if not os.path.exists(p):
        return None
    mt = os.path.getmtime(p)
    c = _C.get(p)
    if c and c[0] == mt:
        return c[1]
    o = pd.read_csv(p, usecols=["ts", "symbol", "expiry", "strike", "side", "oi", "ltp", "volume", "underlying"])
    o = o[o.expiry.notna()]
    o["ts"] = pd.to_datetime(o.ts)
    _C[p] = (mt, o)
    return o


def watch(key: str = "NIFTY", day: str | None = None, at: str | None = None) -> dict:
    day = day or datetime.now().strftime("%Y-%m-%d")
    o = _load(day)
    if o is None:
        files = sorted(f[:10] for f in os.listdir(OCD) if f.endswith(".csv.gz")) if os.path.isdir(OCD) else []
        if not files:
            return {"key": key, "error": "no option-chain snapshots"}
        day = files[-1]; o = _load(day)
    o = o[o.symbol == key]
    if o.empty:
        return {"key": key, "day": day, "error": "no rows"}
    exp = sorted(o.expiry.unique())[0]
    o = o[o.expiry == exp]
    o = o[o.ts <= pd.Timestamp(f"{day} 15:00")]                # writer moves after 15:00 are end-of-day noise - stop at 3 pm
    if at:
        o = o[o.ts <= pd.Timestamp(f"{day} {min(at, '15:00')}")]
    times = sorted(o.ts.unique())
    if len(times) < 2:
        return {"key": key, "day": day, "error": "need two snapshots"}
    t1 = times[-1]
    def snap(t):
        s = o[o.ts == t]
        return s.pivot_table(index="strike", columns="side", values="oi", aggfunc="sum"), s.pivot_table(index="strike", columns="side", values="ltp", aggfunc="last"), float(s.underlying.iloc[0])
    def before(mins):
        c = [t for t in times if t <= t1 - pd.Timedelta(minutes=mins)]
        return c[-1] if c else times[0]
    oi1, px1, spot = snap(t1)
    oi15, px15, _ = snap(before(15)); oi30, px30, s30 = snap(before(30)); oi0, _, _ = snap(times[0])
    near = [k for k in oi1.index if abs(k / spot - 1) <= 0.02]
    rows, flags = [], []
    for k in near:
        r = {"strike": float(k)}
        for side in ("PE", "CE"):
            cur = float(oi1.get(side, pd.Series()).get(k, 0) or 0)
            a15 = float(oi15.get(side, pd.Series()).get(k, 0) or 0); a30 = float(oi30.get(side, pd.Series()).get(k, 0) or 0)
            p_now = float(px1.get(side, pd.Series()).get(k, 0) or 0); p30 = float(px30.get(side, pd.Series()).get(k, 0) or 0)
            r[side] = {"oi": round(cur), "chg15": round(cur - a15), "chg30": round(cur - a30),
                       "pct30": round(100 * (cur / a30 - 1), 1) if a30 else None, "prem_chg30": round(p_now - p30, 2)}
        rows.append(r)
        for side, below_ok in (("PE", k < spot), ("CE", k > spot)):
            x = r[side]
            if not x["pct30"] or abs(x["chg30"]) < MIN_ABS[key] or abs(x["pct30"]) < MIN_PCT:
                continue
            who = "put" if side == "PE" else "call"
            if x["chg30"] > 0 and below_ok and x["prem_chg30"] <= 0:
                flags.append({"strike": k, "side": side, "kind": "adding", "text": f"{who} writers ADDING at {int(k)}: OI {x['pct30']:+.0f}% in 30 min ({x['chg30']/1000:+.0f}k)",
                              "means": "support being built / defended" if side == "PE" else "resistance being built / defended"})
            elif x["chg30"] < 0:
                flags.append({"strike": k, "side": side, "kind": "leaving", "text": f"{who} writers LEAVING {int(k)}: OI {x['pct30']:+.0f}% in 30 min ({x['chg30']/1000:+.0f}k)",
                              "means": "support being abandoned" if side == "PE" else "resistance being abandoned / short covering"})
    for side in ("PE", "CE"):
        top_now = set(oi1.get(side, pd.Series()).nlargest(3).index); top_open = set(oi0.get(side, pd.Series()).nlargest(3).index)
        for k in top_now - top_open:
            if abs(k / spot - 1) <= 0.03:
                flags.append({"strike": float(k), "side": side, "kind": "new_wall", "text": f"NEW {'put' if side == 'PE' else 'call'} wall at {int(k)} (now top-3 OI, was not at the open)",
                              "means": "new support level forming" if side == "PE" else "new resistance level forming"})
    flags.sort(key=lambda f: (f["kind"] != "new_wall", -abs(next((r[f["side"]]["chg30"] for r in rows if r["strike"] == f["strike"]), 0))))
    out = {"key": key, "day": day, "expiry": exp, "at": pd.Timestamp(t1).strftime("%H:%M"), "spot": round(spot, 1), "spot_30m_ago": round(s30, 1),
           "flags": flags[:8], "strikes": sorted(rows, key=lambda r: -r["strike"]),
           "note": "Stops at 15:00 (end-of-day noise after). Writers inferred: OI up with premium down = writing. Untested as a signal - being logged."}
    return out


def log_tick(keys=("NIFTY", "BANKNIFTY")):
    """Append one line per new snapshot (called from the live poll)."""
    now = datetime.now()
    if not ("09:15" <= now.strftime("%H:%M") <= "15:00"):
        return
    day = now.strftime("%Y-%m-%d")
    for k in keys:
        try:
            w = watch(k, day)
        except Exception:
            continue
        if w.get("error") or w.get("day") != day:
            continue
        last = _C.get(("logged", k))
        if last == w["at"]:
            continue
        _C[("logged", k)] = w["at"]
        os.makedirs(LOG, exist_ok=True)
        with open(os.path.join(LOG, f"{day}.jsonl"), "a") as f:
            try:
                bw = blocks_watch(k, day)
                bl = [{x: r[x] for x in ("range", "strength", "side", "read", "tone")} | {"def30": r["defenders"]["chg_m30"], "att30": r["attackers"]["chg_m30"]}
                      for r in bw.get("blocks", [])]
            except Exception:
                bl = []
            f.write(json.dumps({"key": k, "at": w["at"], "spot": w["spot"], "flags": w["flags"], "blocks": bl,
                                "strikes": [{"s": r["strike"], "pe": r["PE"]["oi"], "ce": r["CE"]["oi"]} for r in w["strikes"]]}) + "\n")


# ------------------------------------------------------------------ OI at the blocks (2026-10-24)
def blocks_watch(key: str = "NIFTY", day: str | None = None, at: str | None = None) -> dict:
    """For each strong / medium block within 1.5 % of spot: option OI at the strikes inside the block (+-1 strike step),
    puts for a support block, calls for a resistance block, change over 15 / 30 min and since the open.
    Writers adding = block being defended (reversal more likely); leaving = block being given up (break more likely).
    The other side is shown too (call writing at a support block = sellers pressing). Untested as a signal - logged."""
    try:
        import levelmap as LM
        lm = LM.get(key, 10)
    except Exception as e:
        return {"key": key, "error": f"level map: {e}"[:120]}
    day = day or datetime.now().strftime("%Y-%m-%d")
    o = _load(day)
    if o is None:
        files = sorted(f[:10] for f in os.listdir(OCD) if f.endswith(".csv.gz")) if os.path.isdir(OCD) else []
        if not files:
            return {"key": key, "error": "no option-chain snapshots"}
        day = files[-1]; o = _load(day)
    o = o[(o.symbol == key)]
    if o.empty:
        return {"key": key, "error": "no rows"}
    o = o[o.expiry == sorted(o.expiry.unique())[0]]
    o = o[o.ts <= pd.Timestamp(f"{day} 15:00")]
    if at:
        o = o[o.ts <= pd.Timestamp(f"{day} {min(at, '15:00')}")]
    times = sorted(o.ts.unique())
    if len(times) < 2:
        return {"key": key, "day": day, "error": "need two snapshots"}
    t1 = times[-1]
    def before(m):
        c = [t for t in times if t <= t1 - pd.Timedelta(minutes=m)]
        return c[-1] if c else times[0]
    snaps = {"now": t1, "m15": before(15), "m30": before(30), "open": times[0]}
    piv = {k: o[o.ts == t].pivot_table(index="strike", columns="side", values="oi", aggfunc="sum") for k, t in snaps.items()}
    pxp = {k: o[o.ts == t].pivot_table(index="strike", columns="side", values="ltp", aggfunc="last") for k, t in snaps.items()}
    spot = float(o[o.ts == t1].underlying.iloc[0])
    step = 50 if key == "NIFTY" else 100
    rows = []
    for b in lm.get("blocks") or []:
        if b["strength"] == "weak" or abs(b["mid"] / spot - 1) > 0.015:
            continue
        side = "support" if b["hi"] < spot else "resistance" if b["lo"] > spot else "at price"
        ks = [k for k in piv["now"].index if b["lo"] - step <= k <= b["hi"] + step]
        if not ks:
            continue
        def tot(snap, sd):
            p = piv[snap]
            return float(p.get(sd, pd.Series()).reindex(ks).fillna(0).sum()) if sd in p else 0.0
        own = "PE" if side != "resistance" else "CE"; other = "CE" if own == "PE" else "PE"
        r = {"range": f"{b['lo']:,.0f}–{b['hi']:,.0f}", "lo": b["lo"], "hi": b["hi"], "strength": b["strength"], "side": side,
             "alerts": b.get("alerts") or [], "strikes": [int(k) for k in ks], "dist_pct": round(100 * (b["mid"] / spot - 1), 2)}
        def prem(snap, sd):
            p = pxp[snap]
            return float(p.get(sd, pd.Series()).reindex(ks).fillna(0).mean()) if sd in p else 0.0
        for lab, sd in (("defenders", own), ("attackers", other)):
            now = tot("now", sd)
            pc = prem("now", sd) - prem("m30", sd)
            r[lab] = {"type": "put" if sd == "PE" else "call", "oi": round(now),
                      **{f"chg_{m}": round(now - tot(m, sd)) for m in ("m15", "m30", "open")},
                      **{f"pct_{m}": (round(100 * (now / tot(m, sd) - 1), 1) if tot(m, sd) else None) for m in ("m30", "open")},
                      "prem_chg30": round(pc, 2)}
        for sd in ("PE", "CE"):                               # both sides, so a caller can pick defenders by the approach side
            now = tot("now", sd); pc = prem("now", sd) - prem("m30", sd); o30 = tot("m30", sd)
            r[sd] = {"oi": round(now), "chg_m15": round(now - tot("m15", sd)), "chg_m30": round(now - o30), "chg_open": round(now - tot("open", sd)),
                     "pct_m30": round(100 * (now / o30 - 1), 1) if o30 else None, "prem_chg30": round(pc, 2)}
        d, a = r["defenders"], r["attackers"]
        thr = MIN_ABS[key]
        if (d["pct_m30"] or 0) >= 5 and d["chg_m30"] >= thr and d["prem_chg30"] > 0:
            r["read"], r["tone"] = f"BUYERS - {d['type']} buying here (OI and premium up): break more likely", "break"
        elif (d["pct_m30"] or 0) >= 5 and d["chg_m30"] >= thr and not ((a["pct_m30"] or 0) >= 10 and a["chg_m30"] >= thr):
            r["read"], r["tone"] = "DEFENDED - writers adding: reversal more likely here", "hold"
        elif (d["pct_m30"] or 0) <= -5 and d["chg_m30"] <= -thr:
            r["read"], r["tone"] = "ABANDONED - writers leaving: break more likely", "break"
        elif (a["pct_m30"] or 0) >= 10 and a["chg_m30"] >= thr:
            r["read"], r["tone"] = f"PRESSURE - {a['type']} writers adding against it: watch for a break", "break"
        else:
            r["read"], r["tone"] = "No clear change", "flat"
        rows.append(r)
    rows.sort(key=lambda r: -r["hi"])
    return {"key": key, "day": day, "at": pd.Timestamp(t1).strftime("%H:%M"), "spot": round(spot, 1), "blocks": rows,
            "note": "Defenders = put writers at support / call writers at resistance. OI up with premium down = writing; OI up with premium up = buying. Untested as a signal - every snapshot is logged."}


def oi_read(key: str, own: dict, other: dict, own_type: str, other_type: str):
    """(tone, text) for a block from the defenders' (own) and attackers' (other) OI stats."""
    thr = MIN_ABS[key]
    if (own.get("pct_m30") or 0) >= 5 and own["chg_m30"] >= thr and own["prem_chg30"] > 0:
        return "break", f"{own_type} buying here (OI and premium up)"
    if (own.get("pct_m30") or 0) >= 5 and own["chg_m30"] >= thr and not ((other.get("pct_m30") or 0) >= 10 and other["chg_m30"] >= thr):
        return "hold", f"{own_type} writers adding"
    if (own.get("pct_m30") or 0) <= -5 and own["chg_m30"] <= -thr:
        return "break", f"{own_type} writers leaving"
    if (other.get("pct_m30") or 0) >= 10 and other["chg_m30"] >= thr:
        return "break", f"{other_type} writers adding against it"
    return "flat", "no clear OI change"
