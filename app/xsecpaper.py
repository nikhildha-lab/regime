"""
Cross-sectional momentum basket - PAPER ONLY (2026-10-29, Nikhil approved the Edge board + forward test of the one research lead).
Rule fixed in advance from claude/alt-edge-search.md (best tested setting, chosen before this live run):
  universe : 29 established Binance USDT perps (data_store/research/alts1m/established.txt)
  daily at 00:00 UTC : rank coins by their 72 h return; long the 5 best, short the 5 worst, equal weight; hold 24 h
  P&L per sleeve = (mean long return - mean short return) / 2  (per unit of gross exposure); cost 0.10% per rebuild
Never orders. Recorded in data_store/livedesk/xsec/sleeves.json and graded by the Edge board.
"""
from __future__ import annotations

import json, os, time, urllib.request
from datetime import datetime, timezone

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
DIR = os.path.join(ROOT, "data_store", "livedesk", "xsec")
F = os.path.join(DIR, "sleeves.json")
L, N, COST = 72, 5, 0.10
C: dict = globals().get("C") or {}


def _get(url):
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "synaptic"}), timeout=15) as r:
        return json.loads(r.read().decode())


def _uni():
    try:
        return open(os.path.join(ROOT, "data_store", "research", "alts1m", "established.txt")).read().split()
    except Exception:
        return ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "BNBUSDT", "DOGEUSDT", "ADAUSDT", "LINKUSDT", "AVAXUSDT", "LTCUSDT"]


def _load():
    try:
        return json.load(open(F))
    except Exception:
        return {"sleeves": []}


def _save(s):
    os.makedirs(DIR, exist_ok=True)
    json.dump(s, open(F + ".tmp", "w"), indent=0); os.replace(F + ".tmp", F)


def _closes(sym, t_ms):
    """1h closes: the bar closing at t_ms (UTC hour) and the one L hours earlier."""
    k = _get(f"https://fapi.binance.com/fapi/v1/klines?symbol={sym}&interval=1h&endTime={t_ms - 1}&limit={L + 2}")
    k = [r for r in k if int(r[6]) < t_ms]           # completed bars only
    if len(k) < L + 1:
        return None, None
    return float(k[-1][4]), float(k[-1 - L][4])


def tick(force=False):
    """Run at most once per UTC day, after 00:05 UTC: close yesterday's sleeve, open today's."""
    now = datetime.now(timezone.utc)
    day = now.strftime("%Y-%m-%d")
    mins = now.hour * 60 + now.minute
    if not (5 <= mins <= 120) and not force:     # only near the 00:00 UTC close, so entry / exit prices are the real 00:00 closes
        return None                              # (a day missed while the Mac was off is skipped, not back-filled)
    st = _load()
    if st["sleeves"] and st["sleeves"][-1]["day"] == day:
        return None
    t0 = int(datetime(now.year, now.month, now.day, tzinfo=timezone.utc).timestamp() * 1000)
    px, ret = {}, {}
    for s in _uni():
        try:
            c, c0 = _closes(s, t0)
            if c and c0:
                px[s] = c; ret[s] = c / c0 - 1
        except Exception as e:
            C["err"] = f"{s}: {str(e)[:80]}"
        time.sleep(0.1)
    if len(px) < 2 * N + 5:
        C["err"] = f"only {len(px)} coins priced"; return None
    for sl in st["sleeves"]:                              # close the open sleeve at today's 00:00 UTC closes
        if sl["status"] == "open":
            lr = [px[s] / sl["entry"][s] - 1 for s in sl["long"] if s in px]
            sr = [px[s] / sl["entry"][s] - 1 for s in sl["short"] if s in px]
            if lr and sr:
                g = (sum(lr) / len(lr) - sum(sr) / len(sr)) / 2 * 100
                sl.update(status="closed", closed=day, gross_pct=round(g, 3), net_pct=round(g - COST, 3),
                          long_pct=round(sum(lr) / len(lr) * 100, 3), short_pct=round(sum(sr) / len(sr) * 100, 3))
    rk = sorted(ret, key=lambda s: ret[s], reverse=True)
    st["sleeves"].append({"day": day, "opened_utc": now.isoformat(timespec="minutes"), "status": "open", "long": rk[:N], "short": rk[-N:],
                          "entry": {s: px[s] for s in rk[:N] + rk[-N:]}, "ret72": {s: round(ret[s] * 100, 2) for s in rk[:N] + rk[-N:]}, "n_ranked": len(rk)})
    _save(st)
    return st["sleeves"][-1]


def record():
    st = _load(); cl = [s for s in st["sleeves"] if s["status"] == "closed"]
    op = next((s for s in st["sleeves"] if s["status"] == "open"), None)
    tot = 1.0
    for s in cl:
        tot *= 1 + s["gross_pct"] / 100
    return {"closed": len(cl), "win": round(100 * sum(s["gross_pct"] > 0 for s in cl) / len(cl)) if cl else None,
            "avg_bps": round(sum(s["gross_pct"] for s in cl) / len(cl) * 100, 1) if cl else None,
            "avg_net_bps": round(sum(s["net_pct"] for s in cl) / len(cl) * 100, 1) if cl else None,
            "total_pct": round((tot - 1) * 100, 2) if cl else None, "since": st["sleeves"][0]["day"] if st["sleeves"] else None,
            "open": {k: op[k] for k in ("day", "long", "short")} if op else None, "err": C.get("err")}
