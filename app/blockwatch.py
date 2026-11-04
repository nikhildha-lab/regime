"""
Block watch - 1-minute zoom at the nearest block (2026-10-25, Nikhil: "capture moves like SOL at 101.6-101.9").
Descriptive only. Tested 2026-10-25 (scripts/research/block_rejection.py, block_oi_push.py):
  * fading double tests / rejections at prior-day/week zones on 5-min bars, 2023-26, SOL/BTC/ETH: gross 0..+3 bps,
    net -7..-10 bps after 0.10% cost, real zones = fake zones -> no edge;
  * OI during the push into a zone (29 days, 1,564 touches) did not separate breaks from rejections (break 45% vs 48%).
So this panel describes the fight at the block and logs every status change for grading - it is not a trade signal.

Zone   : tracker levels (prior day/week high-low, value area, POC, swing S/R, VWAP, IB) + the Verdict's blocks, merged within
         0.15%; the nearest zone within 0.35% (crypto) / 0.25% (Nifty) of price.
Tests  : 1-minute bars touching the zone from outside. push = buy-sell delta of the 3 bars into the touch; OI over the push.
Rules  : wick (closed back out) · exhaustion (push < 25% of the first test's) · equal / lower high · OI not rising · sellers after.
Status : approaching -> testing -> rejection forming (2+ rules, back out) -> rejected (close past the neckline)
         or breaking (close beyond the far edge) -> broke out (2 closes beyond).
"""
from __future__ import annotations

import json
import os
import time
import urllib.request
from datetime import datetime

import pandas as pd

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
LOG = os.path.join(ROOT, "data_store", "livedesk", "blockwatch")
BIN = {k: k + "USDT" for k in ("BTC", "ETH", "SOL", "BNB", "XRP", "UNI", "ZEC", "LINK", "DASH")}   # 2026-11-03: + drop-down coins
HL = {"GOLD": "xyz:GOLD", "SILVER": "xyz:SILVER", "COPPER": "xyz:COPPER", "PAXG": "PAXG", "CRUDE": "xyz:CL", "NATGAS": "xyz:NATGAS"}   # + commodities 2026-11-03      # metals perps on Hyperliquid (2026-10-25)
HL_URL = "https://api.hyperliquid.xyz/info"
WIN = {"crypto": 0.0035, "nifty": 0.0025, "metals": 0.003}
C: dict = {"oi": {}, "last": {}}


def _get(url):
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "synaptic"}), timeout=10) as r:
        return json.loads(r.read().decode())


def _post(body):
    req = urllib.request.Request(HL_URL, data=json.dumps(body).encode(), headers={"Content-Type": "application/json", "User-Agent": "synaptic"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode())


def _clv_delta(o, h, l, c, v):
    """No trade-side split in this feed: estimate buy-sell from where the bar closed in its range (marked est.)."""
    return v * (((c - l) - (h - c)) / (h - l)) if h > l else 0.0


def stock_fut(sym):
    """NSE near-month stock future (security id) from the Dhan scrip master; rolls to the next month 1 day before expiry."""
    x = C.get(("sf", sym))
    if x:
        return x
    import csv
    from datetime import date, timedelta
    import dhan_live as DL
    rows = []
    with open(DL.MASTER, newline="") as f:
        for r in csv.DictReader(f):
            if r.get("EXCH_ID") == "NSE" and r.get("INSTRUMENT") == "FUTSTK" and (r.get("UNDERLYING_SYMBOL") or "").strip() == sym:
                rows.append((r.get("SM_EXPIRY_DATE", "")[:10], r["SECURITY_ID"]))
    lim = str(date.today() + timedelta(days=1))
    rows = sorted(r for r in rows if r[0] > lim)
    C[("sf", sym)] = rows[0] if rows else None
    return C[("sf", sym)]


def _stock_bars(sym):
    x = C.get(("stk", sym))
    if x and time.time() - x[0] < 45:
        return x[1]
    from datetime import date, timedelta
    import dhan_live as DL
    sf = stock_fut(sym)
    if not sf:
        return []
    b = DL.intraday("NSE_FNO", sf[1], "FUTSTK", date.today() - timedelta(days=4), date.today(), 1, oi=True)
    out = [{"ts": pd.Timestamp(r["ts"].replace(tzinfo=None)), "o": r["open"], "h": r["high"], "l": r["low"], "c": r["close"], "vol": r["volume"],
            "oi": r["oi"], "delta": _clv_delta(r["open"], r["high"], r["low"], r["close"], r["volume"]), "est": True} for r in b]
    if out:
        last = out[-1]["ts"].normalize(); out = [r for r in out if r["ts"].normalize() == last]
    C[("stk", sym)] = (time.time(), out)
    return out


def _bars(desk, key):
    if desk == "metals" and key in HL:
        x = C.get(("hl", key))
        if x and time.time() - x[0] < 20:
            return x[1]
        now = int(time.time() * 1000)
        j = _post({"type": "candleSnapshot", "req": {"coin": HL[key], "interval": "1m", "startTime": now - 80 * 60000, "endTime": now}})
        b = [{"ts": pd.Timestamp(int(r["t"]), unit="ms"), "o": float(r["o"]), "h": float(r["h"]), "l": float(r["l"]), "c": float(r["c"]), "vol": float(r["v"]),
              "delta": _clv_delta(float(r["o"]), float(r["h"]), float(r["l"]), float(r["c"]), float(r["v"])), "est": True} for r in j]
        C[("hl", key)] = (time.time(), b)
        return b
    if desk == "nifty" and key not in ("NIFTY", "BANKNIFTY"):
        return _stock_bars(key)
    if desk == "crypto" and key in BIN:
        x = C.get(("k", key))
        if x and time.time() - x[0] < 20:
            return x[1]
        j = _get(f"https://fapi.binance.com/fapi/v1/klines?symbol={BIN[key]}&interval=1m&limit=75")
        b = [{"ts": pd.Timestamp(int(r[0]), unit="ms"), "o": float(r[1]), "h": float(r[2]), "l": float(r[3]), "c": float(r[4]),
              "delta": 2 * float(r[9]) - float(r[5]), "vol": float(r[5])} for r in j]
        C[("k", key)] = (time.time(), b)
        return b
    if desk == "nifty" and key in ("NIFTY", "BANKNIFTY"):
        import livedesk as LD
        ob = LD.of_min_bars(key, days=2)
        if not ob:
            return []
        last = ob[-1]["d"]
        return [{"ts": pd.Timestamp(f"{b['d']} {b['t']}"), "o": b["o"], "h": b["h"], "l": b["l"], "c": b["c"], "delta": b["delta"], "vol": b["total"]}
                for b in ob if b["d"] == last][-75:]
    return []


def _oi(desk, key):
    """pd.Series of OI (index = time the value is known). Crypto: Binance 5-min history + live snapshots every ~20 s."""
    try:
        if desk == "crypto" and key in BIN:
            ring = C["oi"].setdefault(key, [])
            if not ring or time.time() - ring[-1][2] > 15:
                j = _get(f"https://fapi.binance.com/fapi/v1/openInterest?symbol={BIN[key]}")
                ring.append((pd.Timestamp(int(j["time"]), unit="ms"), float(j["openInterest"]), time.time()))
                del ring[:-800]
            x = C.get(("h", key))
            if not x or time.time() - x[0] > 120:
                h = _get(f"https://fapi.binance.com/futures/data/openInterestHist?symbol={BIN[key]}&period=5m&limit=144")
                x = (time.time(), [(pd.Timestamp(int(r["timestamp"]), unit="ms"), float(r["sumOpenInterest"])) for r in h])     # snapshot times
                C[("h", key)] = x
            pts = sorted(x[1] + [(a, b) for a, b, _ in ring])
            return pd.Series([p[1] for p in pts], index=[p[0] for p in pts])
        if desk == "nifty" and key in ("NIFTY", "BANKNIFTY"):
            import chartmarks as CM
            return CM.oi_series("nifty", key)
        if desk == "nifty":                                     # stock future: OI comes with the 1-min bars
            b = _stock_bars(key)
            if b:
                return pd.Series([r["oi"] for r in b], index=[r["ts"] + pd.Timedelta(minutes=1) for r in b]).replace(0, pd.NA).dropna().astype(float)
        if desk == "metals" and key in HL:
            ring = C["oi"].setdefault(key, [])
            if not ring or time.time() - ring[-1][2] > 15:
                dex = "xyz" if HL[key].startswith("xyz:") else None
                j = _post({"type": "metaAndAssetCtxs", **({"dex": dex} if dex else {})})
                names = [u["name"] for u in j[0]["universe"]]
                if HL[key] in names:
                    ctx = j[1][names.index(HL[key])]
                    ring.append((pd.Timestamp.utcnow().tz_localize(None), float(ctx["openInterest"]), time.time()))
                    del ring[:-800]
            hist = []                                            # the tracker's own 15-min OI log for older points
            try:
                import glob
                f = sorted(glob.glob(os.path.join(os.environ.get("MP_METALS_DIR") or os.path.join(os.path.expanduser("~"), "Documents", "metals-tracker"), "datasets", "live-*", f"{key}_ctx.csv")))[-1]
                d = pd.read_csv(f).tail(40)
                hist = [(pd.Timestamp(t), float(v)) for t, v in zip(d.datetime, d.oi)]   # file is UTC (checked against its mtime)
            except Exception:
                pass
            pts = sorted(hist + [(a, b) for a, b, _ in ring])
            return pd.Series([p[1] for p in pts], index=[p[0] for p in pts]) if pts else None
    except Exception as e:
        C["oi_err"] = str(e)[:100]
    return None


def _sig(x, n=7):
    """Round to n significant digits (2026-10-27: round(x, 4) turned sub-1 coins like PUMP 0.0042 into equal entry / stop / target)."""
    return float(f"{x:.{n}g}")


def _zones(view, hv, px, off=0.0):
    lv = []
    sr = (view or {}).get("sr") or {}
    for k in ("support", "resistance"):
        lv += [(float(v), "S" if k == "support" else "R") for v in (sr.get(k) or []) if v]
    names = {"PDH": "prev day high", "PDL": "prev day low", "PWH": "prev week high", "PWL": "prev week low"}
    pts = [(float(sr[k]), names[k]) for k in names if sr.get(k)]
    pts += [(float(v), k) for k, v in ((view or {}).get("levels") or {}).items() if v]
    pts += [(float(v), "S/R swing") for v, _ in lv]
    for b in (((hv or {}).get("pressure") or {}).get("chart") or []):          # Verdict blocks (Nifty: spot -> futures by the basis)
        pts += [(b["lo"] + off, f"{b['strength']} block"), (b["hi"] + off, f"{b['strength']} block")]
    pts = sorted(p for p in pts if abs(p[0] / px - 1) <= 0.012)
    zs = []
    for v, n in pts:                     # merge levels < 0.1% apart, a zone never wider than 0.35% (no chaining)
        if zs and v - zs[-1]["hi"] <= v * 0.001 and v - zs[-1]["lo"] <= v * 0.0035:
            zs[-1]["hi"] = v; zs[-1]["parts"].append(n)
        else:
            zs.append({"lo": v, "hi": v, "parts": [n]})
    for z in zs:
        pad = px * 0.0004
        z["lo"] -= pad; z["hi"] += pad
        z["n"] = len(set(z["parts"]))
    return zs


def _one(B, z, side, px, oi):
    sg = 1 if side == "R" else -1
    def oi_at(ts, after=False):
        """OI before the push / the first OI value known after it (5-min history lags, so look a few minutes past the bar)."""
        if oi is None or not len(oi):
            return None
        if after:
            s = oi[oi.index >= ts + pd.Timedelta(minutes=4)]
            return float(s.iloc[0]) if len(s) else float(oi.iloc[-1])
        s = oi[oi.index <= ts]
        return float(s.iloc[-1]) if len(s) else None
    tests, k, last = [], 3, -99
    for k in range(3, len(B)):
        hit = (B[k]["h"] >= z["lo"] and B[k - 1]["c"] < z["lo"]) if side == "R" else (B[k]["l"] <= z["hi"] and B[k - 1]["c"] > z["hi"])
        if hit and k - last >= 4:
            o0, o1 = oi_at(B[k - 3]["ts"]), oi_at(B[k]["ts"], after=True)
            tests.append({"i": k, "t": str(B[k]["ts"])[11:16], "ext": B[k]["h"] if side == "R" else B[k]["l"],
                          "push": round(sum(b["delta"] for b in B[k - 2:k + 1]) * sg),
                          "oi_pct": round((o1 / o0 - 1) * 100, 3) if o0 and o1 else None, "closed_back": (B[k]["c"] < z["lo"]) if side == "R" else (B[k]["c"] > z["hi"])})
            last = k
    rules = {}
    inside = z["lo"] <= px <= z["hi"]
    status = "inside the block" if inside else "approaching"
    far_break = sum(1 for b in B[-2:] if (side == "R" and b["c"] > z["hi"]) or (side == "S" and b["c"] < z["lo"]))
    if tests:
        t1, tl = tests[0], tests[-1]
        after = sum(b["delta"] for b in B[tl["i"] + 1:tl["i"] + 3]) * sg
        rules = {"wick (closed back out)": bool(tl["closed_back"]),
                 "exhaustion (retest push < 25% of the first)": len(tests) > 1 and t1["push"] > 0 and tl["push"] < 0.25 * t1["push"],
                 "equal / lower high" if side == "R" else "equal / higher low": len(tests) > 1 and ((tl["ext"] <= t1["ext"] * 1.0005) if side == "R" else (tl["ext"] >= t1["ext"] * 0.9995)),
                 "OI not rising on the push": tl["oi_pct"] is not None and tl["oi_pct"] <= 0,
                 "sellers after the test" if side == "R" else "buyers after the test": len(B) > tl["i"] + 2 and after < 0}
        hits = sum(rules.values())
        neck = min(b["l"] for b in B[t1["i"]:tl["i"] + 1]) if side == "R" else max(b["h"] for b in B[t1["i"]:tl["i"] + 1])
        if far_break >= 2:
            status = "broke out" if side == "R" else "broke down"
        elif far_break == 1:
            status = "breaking"
        elif inside:
            status = "testing"
        elif len(tests) > 1 and ((side == "R" and px < neck) or (side == "S" and px > neck)):
            status = "rejected"
        elif hits >= 2:
            status = "rejection forming"
        else:
            status = "tested, undecided"
    if side == "S":                      # support wording: a rejection of lower prices = the support held
        status = {"rejected": "held (bounced)", "rejection forming": "holding"}.get(status, status)
    r = {"zone": {"lo": _sig(z["lo"]), "hi": _sig(z["hi"]), "side": side, "parts": list(dict.fromkeys(z["parts"]))[:6], "n_levels": z["n"]},
         "tests": [{k: v for k, v in t.items() if k != "i"} for t in tests[-4:]], "rules": rules, "status": status}
    if tests:
        r["neckline"] = neck
    return r


def get(desk, key, view=None, hv=None):
    bars = _bars(desk, key)
    if len(bars) < 10:
        return {"key": key, "status": "no 1-minute data", "bars": []}
    px = bars[-1]["c"]
    off = 0.0
    if hv and hv.get("price") and (hv.get("ref") == "spot" or desk in ("metals",) or (desk == "nifty" and key not in ("NIFTY", "BANKNIFTY"))) and abs(px / hv["price"] - 1) < 0.01:
        off = px - hv["price"]
    zs = _zones(view, hv, px, off)
    near = [z for z in zs if (z["lo"] - px) / px <= WIN[desk] and (px - z["hi"]) / px <= WIN[desk]]
    out = {"key": key, "desk": desk, "price": px, "at": str(bars[-1]["ts"])[:16], "tz": "UTC" if desk in ("crypto", "metals") else "IST", "est_delta": bool(bars and bars[-1].get("est")),
           "bars": [{"t": str(b["ts"])[11:16], "o": b["o"], "h": b["h"], "l": b["l"], "c": b["c"], "delta": round(b["delta"])} for b in bars[-45:]],
           "tested": "Fading rejections at blocks lost money after costs in 2023-26 tests (real levels = fake levels); OI during the push did not separate breaks from rejections. Description only."}
    if not near:
        nz = min(zs, key=lambda z: min(abs(z["lo"] - px), abs(z["hi"] - px)), default=None)
        out.update(status="no block near", note=f"nearest block {nz['lo']:.4g}-{nz['hi']:.4g}" if nz else "")
        out["zones_all"] = [{"lo": _sig(z["lo"]), "hi": _sig(z["hi"]), "parts": list(dict.fromkeys(z["parts"]))[:4], "n": z["n"]} for z in zs]
        C[("oiser", desk, key)] = _oi(desk, key)
        return out
    oi = _oi(desk, key)
    pick = lambda zs: [next((z for z in zs if z["n"] >= 2 or any("block" in p for p in z["parts"])), zs[0])] if zs else []   # a real block (2+ levels or a Verdict block) beats a lone line
    above = pick(sorted([z for z in near if (z["lo"] + z["hi"]) / 2 >= px], key=lambda z: z["lo"]))
    below = pick(sorted([z for z in near if (z["lo"] + z["hi"]) / 2 < px], key=lambda z: -z["hi"]))
    out["zones"] = [_one(B=bars, z=z, side=sd, px=px, oi=oi) for sd, zz in (("R", above), ("S", below)) for z in zz]
    out["zones_all"] = [{"lo": _sig(z["lo"]), "hi": _sig(z["hi"]), "parts": list(dict.fromkeys(z["parts"]))[:4], "n": z["n"]} for z in zs]
    C[("oiser", desk, key)] = oi
    lead = min(out["zones"], key=lambda r: min(abs(r["zone"]["lo"] - px), abs(r["zone"]["hi"] - px)))     # the block price is closest to
    out.update({k: lead[k] for k in ("zone", "tests", "rules", "status") if k in lead})
    if "neckline" in lead:
        out["neckline"] = lead["neckline"]
    out["oi_line"] = [[str(i)[11:16], round(v)] for i, v in (oi[oi.index >= bars[-45]["ts"]].items() if oi is not None and len(oi) else [])][-200:]
    z = out["zone"]; status = out["status"]; rules = out.get("rules") or {}; tests = out.get("tests") or []
    # log status changes (graded later)
    lk = (desk, key)
    sig = (status, _sig(z["lo"]))
    if C["last"].get(lk) != sig:
        C["last"][lk] = sig
        try:
            os.makedirs(LOG, exist_ok=True)
            with open(os.path.join(LOG, f"{datetime.now():%Y-%m-%d}.jsonl"), "a") as f:
                f.write(json.dumps({"at": datetime.now().isoformat(timespec="seconds"), "desk": desk, "key": key, "price": px, "status": status,
                                    "zone": out["zone"], "rules": rules, "tests": tests[-3:]}) + "\n")
        except Exception:
            pass
    return out
