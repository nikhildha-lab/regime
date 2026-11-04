"""
Hourly verdict per market (2026-10-25, Nikhil). Context, not a forecast. Indications only - no orders.

Oriented on the 5-minute and 15-minute timeframes (the only intraday direction reads with a small tested effect -
Nifty 5m / 15m, 2-4 bps an hour), placed against the pressure points around the price:
  direction : 5m and 15m trend + their scores (/10) -> up / down / mixed (strong when the average score >= 5)
  pressure  : nearest blocks above and below (Nifty / Bank Nifty: rolling 10-day level map with strong / medium / weak;
              other markets: the dashboard's support / resistance levels, strength from how many levels pile up)
  OI        : Nifty / Bank Nifty only - OI at the nearest block (defended / abandoned / buyers)
Verdicts: Trend continuation up / down · Buy on dips · Sell on rise · Into strong support / resistance (wait) ·
At a block (wait for the block verdict) · Sideways (wait).
Full update once an hour, re-checked every 15 minutes and updated early only when something important changes (Indian markets 09:15-15:30 IST, everything else round the clock). Every reading is logged with
the price so it can be graded later: data_store/livedesk/hourverdict/<date>.jsonl
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
MP_LIVE = os.path.join(ROOT, "data_store", "dashboard", "market_pulse_live.json")
DIR = os.path.join(ROOT, "data_store", "livedesk", "hourverdict")
NEAR = {"NIFTY": 0.3, "BANKNIFTY": 0.35, "ES": 0.3, "NQ": 0.35, "US_STOCKS": 0.3, "BTC": 0.6, "ETH": 0.7, "SOL": 0.8,
        "GOLD": 0.4, "SILVER": 0.6, "COPPER": 0.5, "ALUMINUM": 0.5, "CRUDE": 0.7, "NATGAS": 1.0}
S: dict = {"slot": {}, "last": {}, "mp": (0, None)}


def _mp():
    try:
        mt = os.path.getmtime(MP_LIVE)
    except OSError:
        return {}
    if S["mp"][0] != mt:
        try:
            S["mp"] = (mt, json.load(open(MP_LIVE)))
        except Exception:
            pass
    return S["mp"][1] or {}


def _blocks(key, mk, px):
    """[(lo, hi, strength, name)] around the price."""
    if mk.get("_blocks"):                         # caller supplies its own blocks (Nifty 50 stock view)
        return sorted(mk["_blocks"])
    out = []
    if key in ("NIFTY", "BANKNIFTY"):
        try:
            import levelmap as LM
            for b in LM.get(key, 10).get("blocks") or []:
                out.append((b["lo"], b["hi"], b["strength"], ", ".join((b.get("alerts") or []) + b["parts"][:1])[:60], bool(b.get("alerts"))))
        except Exception:
            pass
    if not out:
        lv = mk.get("levels") or {}
        for side in ("supports", "resistances"):
            for x in lv.get(side) or []:
                p = x.get("price")
                if not p:
                    continue
                st = x.get("strength") or 1
                w = p * 0.0008
                out.append((p - w, p + w, "strong" if st >= 4 else "medium" if st >= 2 else "weak", ", ".join((x.get("names") or [])[:2]),
                            any("week" in n or "month" in n for n in (x.get("names") or []))))
    return sorted(out)


def _oi_note(key, blk):
    if key not in ("NIFTY", "BANKNIFTY") or not blk:
        return None
    try:
        import oiwatch as OW
        bw = OW.blocks_watch(key)
        r = next((r for r in bw.get("blocks", []) if r["lo"] <= blk[1] and r["hi"] >= blk[0]), None)
        return r["read"].split(" - ")[0] if r else None
    except Exception:
        return None


def _spot(key):
    """Latest index spot from today's option-chain snapshot (blocks are spot levels), else the level map's close."""
    try:
        import glob
        import pandas as pd
        f = sorted(glob.glob(os.path.join(ROOT, "data_store", "flow", "option_chain", "*.csv.gz")))[-1]
        o = pd.read_csv(f, usecols=["ts", "symbol", "underlying"])
        o = o[(o.symbol == key) & (o.underlying > 0)]
        if len(o):
            return float(o.underlying.iloc[-1]), str(o.ts.iloc[-1])[:16]
    except Exception:
        pass
    try:
        import levelmap as LM
        g = LM.get(key, 10)
        return float(g["spot"]), g.get("as_of")
    except Exception:
        return None, None


def compute(key, mk):
    rows = {r.get("tf"): r for r in ((mk.get("mtf") or {}).get("rows") or [])}
    r5, r15 = rows.get("5m") or {}, rows.get("15m") or {}
    px = r5.get("close") or (mk.get("levels") or {}).get("close")
    ref, px_at = "futures" if key in ("ES", "NQ", "GOLD", "SILVER", "COPPER", "CRUDE", "NATGAS", "ALUMINUM") else "price", r5.get("bar")
    if key in ("NIFTY", "BANKNIFTY"):
        sp, sp_at = _spot(key)
        if sp:
            px, ref, px_at = sp, "spot", sp_at
    if not px:
        return None
    s5 = (r5.get("scores") or {}).get("overall"); s15 = (r15.get("scores") or {}).get("overall")
    t5, t15 = r5.get("trend"), r15.get("trend")
    avg = ((s5 or 0) + (s15 or 0)) / 2
    if (t5 == t15 == "Up" and avg > -1) or avg >= 3:      # trend words and scores must not disagree
        d = "up"
    elif (t5 == t15 == "Down" and avg < 1) or avg <= -3:
        d = "down"
    else:
        d = "mixed"
    strong = abs(avg) >= 5
    bl = _blocks(key, mk, px)
    near_pct = NEAR.get(key, 0.4)
    inside = next((b for b in bl if b[0] <= px <= b[1] and (b[2] == "strong" if mk.get("_inside_strong") else b[2] != "weak")), None)   # a weak block does not stop a trend
    sup = [b for b in bl if b[1] < px]; res = [b for b in bl if b[0] > px]
    s1 = sup[-1] if sup else None; r1 = res[0] if res else None
    dist = lambda b: abs(((b[0] + b[1]) / 2) / px - 1) * 100
    near_s = s1 and dist(s1) <= near_pct; near_r = r1 and dist(r1) <= near_pct
    dp = 0 if px > 2000 else 1 if px > 200 else 2
    rng = lambda b: f"{b[0]:,.{dp}f}–{b[1]:,.{dp}f}"
    fmt = lambda b: f"{rng(b)} ({b[2]}{' ★' if len(b) > 4 and b[4] else ''})"           # every block mention carries its strength
    strong_s = next((b for b in reversed(sup) if b[2] == "strong"), None)
    strong_r = next((b for b in res if b[2] == "strong"), None)
    if inside:
        v, tone, line = "At a block - wait", "wait", f"Price inside block {fmt(inside)}; let the block verdict decide (reversal or break)"
        focus = inside
    elif d == "down":
        if near_s and s1[2] == "strong":
            v, tone, line, focus = "Into strong support - wait", "wait", f"Falling into strong support {fmt(s1)}; don't sell into it - wait for a break or a reversal", s1
        elif near_r:
            v, tone, line, focus = "Sell on rise", "bear", f"5m / 15m down and price just under resistance {fmt(r1)} - rises into it are the sell zone", r1
        else:
            v, tone = ("Trend continuation ↓" if strong else "Sell on rise"), "bear"
            line = (f"Sell rises toward {fmt(r1)}" if r1 else "Sell rises") + (f"; supports {', '.join(fmt(b) for b in list(reversed(sup))[:2])}" if s1 else "")
            focus = r1
    elif d == "up":
        if near_r and r1[2] == "strong":
            v, tone, line, focus = "Into strong resistance - wait", "wait", f"Rising into strong resistance {fmt(r1)}; don't buy into it - wait for a break or a reversal", r1
        elif near_s:
            v, tone, line, focus = "Buy on dips", "bull", f"5m / 15m up and price just above support {fmt(s1)} - dips into it are the buy zone", s1
        else:
            v, tone = ("Trend continuation ↑" if strong else "Buy on dips"), "bull"
            line = (f"Buy dips toward {fmt(s1)}" if s1 else "Buy dips") + (f"; resistances {', '.join(fmt(b) for b in res[:2])}" if r1 else "")
            focus = s1
    else:
        v, tone, focus = "Sideways - wait", "neutral", None
        line = f"5m and 15m disagree; range {fmt(s1) if s1 else '?'} to {fmt(r1) if r1 else '?'}"
    oi = _oi_note(key, focus)
    P = lambda b: {"range": rng(b), "lo": round(b[0], 4), "hi": round(b[1], 4), "strength": b[2], "star": b[4], "dist_pct": round(dist(b), 2)}
    press = {"above": [P(b) for b in res[:2]], "below": [P(b) for b in list(reversed(sup))[:2]],
             "chart": [dict(P(b), side="R" if b[0] > px else "S" if b[1] < px else "IN") for b in bl if abs(((b[0] + b[1]) / 2) / px - 1) <= 0.03 and b[2] != "weak"]
                      + ([dict(P(inside), side="IN")] if inside and inside[2] == "weak" else [])}   # 2026-11-03: price inside a block = "IN", not support
    return {"key": key, "ref": ref, "price_at": str(px_at or "")[:16], "verdict": v, "tone": tone, "line": line, "oi": oi, "price": round(px, 4 if px < 50 else 2),
            "tf": {"5m": {"trend": t5, "score": s5}, "15m": {"trend": t15, "score": s15}}, "direction": d, "strong": strong,
            "pressure": press, "inside": rng(inside) if inside else None, "strong_support": rng(strong_s) if strong_s else None, "strong_resistance": rng(strong_r) if strong_r else None}


def _open(key, t):
    if key in ("NIFTY", "BANKNIFTY"):
        return t.weekday() < 5 and 9 * 60 + 15 <= t.hour * 60 + t.minute <= 15 * 60 + 30
    return True


def _sig(r):
    return {"verdict": r.get("verdict"), "direction": r.get("direction"), "oi": r.get("oi"), "inside": r.get("inside")}


def _why(old, new):
    """What important thing changed between two readings (None = nothing important)."""
    out = []
    if old["verdict"] != new["verdict"]:
        out.append(f"verdict {old['verdict']} -> {new['verdict']}")
    if old["direction"] != new["direction"]:
        out.append(f"5m/15m direction {old['direction']} -> {new['direction']}")
    if old["inside"] != new["inside"]:
        out.append(f"entered block {new['inside']}" if new["inside"] else f"left block {old['inside']}")
    if old["oi"] != new["oi"] and (old["oi"] or new["oi"]):
        out.append(f"OI at block {old['oi'] or '-'} -> {new['oi'] or '-'}")
    return "; ".join(out) or None


def tick(force=False) -> dict:
    """Full update every 15 minutes (2026-10-25, Nikhil: "Verdict, updated every 15 mins"). Logged each time."""
    t = datetime.now()
    mp = _mp(); mk = mp.get("markets") or {}
    quarter = t.strftime("%Y-%m-%d %H:") + str(t.minute // 15)
    for key in mk:
        is_open = _open(key, t)
        try:                                             # weekend / holiday: last bar older than 3 h -> treat as closed (not logged)
            import pandas as pd
            pa = pd.Timestamp(next((r for r in ((mk[key].get("mtf") or {}).get("rows") or []) if r.get("tf") == "5m"), {}).get("bar"))
            pa = pa.tz_convert("Asia/Kolkata").tz_localize(None) if pa.tzinfo else pa
            if key not in ("NIFTY", "BANKNIFTY") and (pd.Timestamp(t) - pa).total_seconds() > 3 * 3600:
                is_open = False
        except Exception:
            pass
        if not force and S["slot"].get(key) == quarter:
            continue
        if not is_open and key in S["last"] and not force:
            continue
        try:
            r = compute(key, mk[key])
        except Exception as e:
            r = {"key": key, "error": str(e)[:120]}
        S["slot"][key] = quarter
        if not r:
            continue
        prev = S["last"].get(key)
        r["change"] = _why(_sig(prev), _sig(r)) if prev and not prev.get("error") and not r.get("error") else None
        r["kind"] = "15m"
        r["at"] = t.strftime("%Y-%m-%d %H:%M"); r["closed"] = not is_open
        r["next"] = "updated every 15 minutes" + (" (09:15-15:30 IST on trading days)" if key in ("NIFTY", "BANKNIFTY") else "")
        S["last"][key] = r
        if is_open and not r.get("error"):
            os.makedirs(DIR, exist_ok=True)
            with open(os.path.join(DIR, f"{t:%Y-%m-%d}.jsonl"), "a") as f:
                f.write(json.dumps({k: r.get(k) for k in ("key", "at", "kind", "change", "price", "ref", "verdict", "tone", "direction", "strong", "oi", "inside", "tf")}) + "\n")
    return S["last"]


def _tone(r):
    if r.get("tone"):
        return r["tone"]
    v = r.get("verdict") or ""
    return "bull" if ("↑" in v or "Buy" in v) else "bear" if ("↓" in v or "Sell" in v) else "wait"


_REC = {"t": 0, "d": {}}


def record(days=45):
    """Grade logged readings: for every Buy/Sell-type reading, did price move that way 60-240 min later (same day)?"""
    if time.time() - _REC["t"] < 600:
        return _REC["d"]
    import glob
    rows = []
    for f in sorted(glob.glob(os.path.join(DIR, "*.jsonl")))[-days:]:
        for ln in open(f):
            try:
                rows.append(json.loads(ln))
            except Exception:
                pass
    out = {}
    by = {}
    for r in rows:
        if r.get("price"):
            by.setdefault(r["key"], []).append(r)
    for k, xs in by.items():
        xs.sort(key=lambda r: r["at"])
        ts = [datetime.strptime(r["at"], "%Y-%m-%d %H:%M") for r in xs]
        n = hit = 0; tot = 0.0
        for i, r in enumerate(xs):
            sg = {"bull": 1, "bear": -1}.get(_tone(r))
            if not sg:
                continue
            j = next((j for j in range(i + 1, len(xs)) if 60 <= (ts[j] - ts[i]).total_seconds() / 60 <= 240 and xs[j]["at"][:10] == r["at"][:10]), None)
            if j is None:
                continue
            m = (xs[j]["price"] / r["price"] - 1) * 1e4 * sg
            n += 1; hit += m > 0; tot += m
        out[k] = {"n": n, "hit_pct": round(100 * hit / n) if n else None, "avg_bps": round(tot / n, 1) if n else None}
    _REC.update(t=time.time(), d=out)
    return out


def api(key=None):
    last = tick()
    rec = record()
    for k, r in last.items():
        if isinstance(r, dict):
            r["record"] = rec.get(k) or {"n": 0}
    return {"markets": last if not key else {key: last.get(key)}, "note": "Hourly, re-checked every 15 min for important changes, oriented on 5m / 15m. Context, not a forecast - every reading is logged and will be graded."}
