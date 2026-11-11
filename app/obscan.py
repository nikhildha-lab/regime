"""ORDER-BLOCK BREAKS FOR STRONGEST READINGS (2026-11-11, Nikhil: "order blocks are not shown in the chart very accurately .. idea for
strongest reading is to find top contenders with an order block which they have breached in either direction").

Same order-block detection as BuildUp v2 (app/fnoshort._structure), run once a day over every F&O stock from the compact futures files
(data_store/research/fno_hist/fut_<day>.csv.gz, most-held contract, chained across rolls):
  bull OB  = a down day j followed within 3 sessions by a close >= high[j] + 1.5 ATR(14)        zone = [low j, high j]
  supply   = an up day j followed within 3 sessions by a close <= low[j] - 1.5 ATR(14)         zone = [low j, high j]
Per stock, as of the last stored session:
  supply_up  nearest supply ABOVE the last close that no close has crossed since it formed (the one a rally must break)
  ob_dn      nearest bull OB BELOW the last close that no close has crossed since it formed (the one a fall must break)
  broke_up   a supply whose top was first closed above in the last 3 sessions and has not been lost since
  broke_dn   a bull OB whose bottom was first closed below in the last 3 sessions and has not been regained since
  candles    the last 25 sessions [day, o, h, l, c]
Prices are in the last session's most-held-contract terms ("close"); the page rescales them to its own futures price.
The live break (today's price through the zone) is judged on the page. Context only - not a tested signal, never orders.
"""
from __future__ import annotations

import json
import os
import time

import numpy as np
import pandas as pd

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
HIST = os.path.join(ROOT, "data_store", "research", "fno_hist")
OUT = os.path.join(ROOT, "data_store", "livedesk", "optlab")
LOOK = 60            # sessions an order block stays relevant
RECENT = 3           # a break in the last 3 sessions still counts as fresh
N_CANDLES = 25
_M: dict = {}


def _zones(C, O, H, L, A, up):
    """Order blocks (j, lo, hi, confirm_k). up=True -> bull OBs, else supply."""
    out, seen = [], set()
    n = len(C)
    for k in range(n):
        for j in (k - 1, k - 2, k - 3):
            if j < 0 or j in seen or not np.isfinite(A[j]):
                continue
            if up and C[j] < O[j] and C[k] >= H[j] + 1.5 * A[j]:
                out.append((j, L[j], H[j], k)); seen.add(j)
            elif not up and C[j] > O[j] and C[k] <= L[j] - 1.5 * A[j]:
                out.append((j, L[j], H[j], k)); seen.add(j)
    return out


def build(day: str | None = None) -> dict:
    fs = sorted(f for f in os.listdir(HIST) if f.startswith("fut_") and f.endswith(".csv.gz"))
    if day:
        fs = [f for f in fs if f[4:14] <= day]
    fs = fs[-90:]
    day = fs[-1][4:14]
    F = pd.concat([pd.read_csv(os.path.join(HIST, f), usecols=["day", "sym", "kind", "open", "high", "low", "close", "prev", "oi"]) for f in fs])
    F = F[(F.kind == "STF") & (F.close > 0)].sort_values("oi").groupby(["sym", "day"]).tail(1).sort_values("day")
    out = {}
    for sym, g in F.groupby("sym"):
        if len(g) < 30 or g.day.iloc[-1] != day:
            continue
        g = g.copy(); g["ret"] = (g.close / g.prev - 1).where(g.prev > 0, 0)
        C = (1 + g.ret).cumprod().values * 100
        O, H, L = (g[k].values / g.close.values * C for k in ("open", "high", "low"))
        tr = np.maximum(H - L, np.maximum(np.abs(H - np.roll(C, 1)), np.abs(L - np.roll(C, 1)))); tr[0] = H[0] - L[0]
        A = pd.Series(tr).rolling(14, min_periods=5).mean().values
        n = len(C); i = n - 1
        sc = float(g.close.iloc[-1]) / C[i]; r2 = lambda v: round(float(v) * sc, 2)
        days = [str(d) for d in g.day.values]
        z = lambda b, extra=None: dict({"lo": r2(b[1]), "hi": r2(b[2]), "day": days[b[0]]}, **(extra or {}))
        res = {"close": r2(C[i]), "day": day,
               "candles": [[days[k], r2(O[k]), r2(H[k]), r2(L[k]), r2(C[k])] for k in range(max(0, n - N_CANDLES), n)]}
        # supply above: intact (no close above its top since it was confirmed)
        sup = [b for b in _zones(C, O, H, L, A, False) if i - LOOK <= b[0] < i]
        intact = [b for b in sup if b[2] > C[i] and C[b[3]:i + 1].max() <= b[2]]
        if intact:
            res["supply_up"] = z(min(intact, key=lambda b: b[2]))
        brk = []
        for b in sup:
            after = C[b[3] + 1:i + 1]
            if not len(after):
                continue
            first = next((k for k in range(b[3] + 1, i + 1) if C[k] > b[2]), None)
            if first is not None and first >= i - RECENT + 1 and C[first:i + 1].min() > b[2]:
                brk.append((b, first))
        if brk:
            b, k = max(brk, key=lambda x: x[0][2])
            res["broke_up"] = z(b, {"on": days[k], "ago": i - k})
        # bull OB below: intact (no close below its bottom since it was confirmed)
        bull = [b for b in _zones(C, O, H, L, A, True) if i - LOOK <= b[0] < i]
        intact = [b for b in bull if b[1] < C[i] and C[b[3]:i + 1].min() >= b[1]]
        if intact:
            res["ob_dn"] = z(max(intact, key=lambda b: b[1]))
        brk = []
        for b in bull:
            first = next((k for k in range(b[3] + 1, i + 1) if C[k] < b[1]), None)
            if first is not None and first >= i - RECENT + 1 and C[first:i + 1].max() < b[1]:
                brk.append((b, first))
        if brk:
            b, k = min(brk, key=lambda x: x[0][1])
            res["broke_dn"] = z(b, {"on": days[k], "ago": i - k})
        out[sym] = res
    return {"day": day, "built": time.strftime("%Y-%m-%d %H:%M"), "syms": out}


def get() -> dict:
    """Cached per latest stored session (memory, then disk)."""
    try:
        last = sorted(f[4:14] for f in os.listdir(HIST) if f.startswith("fut_") and f.endswith(".csv.gz"))[-1]
    except Exception as e:
        return {"err": f"no futures history: {e}"}
    if _M.get("day") == last:
        return _M["v"]
    p = os.path.join(OUT, f"obscan_{last}.json")
    try:
        v = json.load(open(p))
    except Exception:
        v = build(last)
        os.makedirs(OUT, exist_ok=True)
        tmp = p + ".tmp"; json.dump(v, open(tmp, "w"), separators=(",", ":")); os.replace(tmp, p)
    _M.update(day=last, v=v)
    return v
