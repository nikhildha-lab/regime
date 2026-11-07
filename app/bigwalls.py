"""BIG WALLS - large resting orders in the Nifty / Bank Nifty futures book (2026-11-07, Nikhil: "can we see this info on the dashboard").

Source: the Dhan order-flow engine's per-minute liquidity slices (resting bid / ask qty per price bucket within +-1 % of price, from the
200-level book; Bank Nifty is 200-level from 9 Nov, 20-level before):
    live      : data/of/<today>/heat_live_<SYM>.json   (last 60 minutes, rewritten every minute by of_engine.py)
    otherwise : the latest day's heatmap_<SYM>.csv.gz   (complete only after the close) -> "last session close"
Big wall (same rule as scripts/research/bigwall_study.py): a bucket >= 4x the median bucket on its side, within 0.6 % of price, present in
>= 70 % of the last 15 minutes. Also: walls that stood >= 10 min in the last hour and vanished while price was still away ("pulled").
Context only: on 9 days of Nifty data walls did NOT hold better than ordinary levels (47 % vs 52 %). Read-only, never trades.
"""
from __future__ import annotations

import io
import json
import os
import time
import zlib

import pandas as pd

OF_DIR = os.path.join(os.environ.get("MP_DHAN_DIR") or os.path.join(os.path.expanduser("~"), "Documents", "dhan"), "orderflow", "data", "of")
K, NEAR, WIN, PRESENT = 4.0, 0.006, 15, 0.7
S: dict = {"cache": {}}


def _gz_text(path):
    raw = open(path, "rb").read(); out = []; i = 0
    while i < len(raw):
        j = raw.find(b"\x1f\x8b\x08", i)
        if j < 0:
            break
        d = zlib.decompressobj(31)
        try:
            out.append(d.decompress(raw[j:])); i = len(raw) - len(d.unused_data) if d.eof else len(raw)
        except zlib.error:
            i = j + 3
    return "\n".join(l for l in b"".join(out).decode("utf-8", "ignore").splitlines() if l.count(",") == 4)


def _mid_path(day, sym):
    """Per-minute last mid from sec_<SYM>.csv (flushed every minute)."""
    s = pd.read_csv(os.path.join(OF_DIR, day, f"sec_{sym}.csv"), usecols=["t", "mid"]).dropna()
    return s.groupby(s.t // 60 * 60).mid.last()


def _load(sym):
    days = sorted(d for d in os.listdir(OF_DIR) if len(d) == 10 and d[4] == "-")
    today = time.strftime("%Y-%m-%d")
    lp = os.path.join(OF_DIR, today, f"heat_live_{sym}.json")
    if os.path.exists(lp) and time.time() - os.path.getmtime(lp) < 300:
        h = pd.DataFrame(json.load(open(lp)), columns=["t", "sym", "p", "bid", "ask"])
        return h, _mid_path(today, sym), today, "live"
    for day in reversed(days):
        hp = os.path.join(OF_DIR, day, f"heatmap_{sym}.csv.gz")
        if day == today or not os.path.exists(hp) or os.path.getsize(hp) < 5_000:
            continue
        h = pd.read_csv(io.StringIO(_gz_text(hp)), header=None, names=["t", "sym", "p", "bid", "ask"], on_bad_lines="skip")
        for c in ("t", "p", "bid", "ask"):
            h[c] = pd.to_numeric(h[c], errors="coerce")
        h = h.dropna(subset=["t", "p"])
        if len(h):
            return h, _mid_path(day, sym), day, "last session close"
    return None


def _big(h, mids):
    """{minute: {(side, price): (qty, multiple)}} for every minute that has a price."""
    out = {}
    for t, g in h.groupby("t"):
        m = mids.get(t // 60 * 60)
        if m is None or m != m:
            continue
        cur = {}
        for side, col in ((1, "bid"), (-1, "ask")):
            gg = g[(g[col] > 0) & ((g.p - m).abs() / m <= NEAR) & ((g.p < m) if side > 0 else (g.p > m))]
            if len(gg) < 8:
                continue
            med = float(gg[col].median()) or 1.0
            for p, q in zip(gg.p, gg[col]):
                if q >= K * med:
                    cur[(side, float(p))] = (float(q), q / med)
        out[int(t)] = (float(m), cur)
    return out


def walls(sym="NIFTY"):
    x = _load(sym)
    if x is None:
        return {"sym": sym, "err": "no order-book data yet"}
    h, mids, day, src = x
    B = _big(h, mids)
    if not B:
        return {"sym": sym, "err": "no usable minutes", "day": day}
    ts = sorted(B); last = ts[-1]; px = B[last][0]; win = [t for t in ts if t > last - WIN * 60]
    cnt, qs, mult = {}, {}, {}
    for t in win:
        for k, (q, mu) in B[t][1].items():
            cnt[k] = cnt.get(k, 0) + 1; qs.setdefault(k, []).append(q); mult.setdefault(k, []).append(mu)
    # how many minutes in a row (up to 60) each wall has been big, counting back from now
    def age(k):
        n = 0
        for t in reversed(ts[-60:]):
            if k in B[t][1]:
                n += 1
            else:
                break
        return n
    rows = []
    for k, n in cnt.items():
        if n < PRESENT * len(win):
            continue
        side, p = k; q = qs[k]
        now_q = B[last][1].get(k, (0.0, 0))[0]; first_q = q[0]
        trend = "growing" if now_q > first_q * 1.25 else "shrinking" if now_q < first_q * 0.75 else "steady"
        rows.append({"side": "buy" if side > 0 else "sell", "price": p, "qty_now": round(now_q), "qty_med": round(float(pd.Series(q).median())),
                     "x": round(float(pd.Series(mult[k]).median()), 1), "dist": round(p - px, 1), "dist_pct": round(100 * (p - px) / px, 2),
                     "age_min": age(k), "trend": trend})
    above = sorted([r for r in rows if r["side"] == "sell"], key=lambda r: r["dist"])[:6]
    below = sorted([r for r in rows if r["side"] == "buy"], key=lambda r: -r["dist"])[:6]
    # pulled: big for >= 10 min in the last hour, then gone while price was >= 6 pts away
    pulled, run = [], {}
    for t in ts[-60:]:
        m, cur = B[t]
        for k in list(run):
            if k not in cur:
                if run[k] >= 10 and abs(k[1] - m) >= 6 and t > last - 30 * 60:
                    pulled.append({"side": "buy" if k[0] > 0 else "sell", "price": k[1], "stood_min": run[k], "at": time.strftime("%H:%M", time.localtime(t))})
                run.pop(k)
        for k in cur:
            run[k] = run.get(k, 0) + 1
    return {"sym": sym, "day": day, "source": src, "as_of": time.strftime("%H:%M", time.localtime(last)), "price": round(px, 1),
            "above": above, "below": below, "pulled": pulled[-6:], "window_min": len(win),
            "rule": f">= {K:g}x the normal level size, within {100*NEAR:.1f}% of price, present {int(100*PRESENT)}% of the last {WIN} min",
            "note": "Futures prices. Context only - on 9 days of Nifty data big walls did not hold better than ordinary levels "
                    "(47% vs 52%); the very biggest usually broke. Walls can be cancelled any second."}


def api(sym="NIFTY"):
    sym = (sym or "NIFTY").upper()
    c = S["cache"].get(sym)
    if c and time.time() - c[0] < 55:
        return c[1]
    try:
        r = walls(sym)
    except Exception as e:
        r = {"sym": sym, "err": f"{type(e).__name__}: {str(e)[:140]}"}
    S["cache"][sym] = (time.time(), r)
    return r


if __name__ == "__main__":
    import sys
    print(json.dumps(api(sys.argv[1] if len(sys.argv) > 1 else "NIFTY"), indent=1))
