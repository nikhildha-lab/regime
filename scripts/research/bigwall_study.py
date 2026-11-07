"""BIG WALLS IN THE 200-LEVEL BOOK - do they hold? (2026-11-07, Nikhil: "ok let's use [200-level depth] for big walls .. can we do that").

Data: the Dhan order-flow engine's per-minute heatmap (data/of/<day>/heatmap_NIFTY.csv.gz = resting bid / ask quantity per 2-pt bucket
within +-1 % of mid, from the 200-level book) and the 1-second futures mid (sec_NIFTY.csv). Nifty only (Bank Nifty is 20-level).
Big wall (fixed before looking at results): a bucket whose resting qty is >= K x the median non-empty bucket on that side that minute,
within 0.5 % of mid, standing for >= 3 consecutive minutes. Event = the 3rd minute.
Questions:
  1. When price comes to a standing big wall (within 1 pt), does it bounce (moves 12 pts away first) or break (12 pts through first)?
     Control: the same test on normal-size buckets at the same distance.
  2. When a big wall disappears BEFORE price reaches it ("pulled"), what does price do in the next 15 min (toward the old wall = the wall
     was fake support / resistance)?
Run: python3 scripts/research/bigwall_study.py
"""
from __future__ import annotations

import io
import os
import sys
import zlib

import numpy as np
import pandas as pd

OFD = os.path.join(os.environ.get("MP_DHAN_DIR") or os.path.join(os.path.expanduser("~"), "Documents", "dhan"), "orderflow", "data", "of")
K = float(sys.argv[1]) if len(sys.argv) > 1 else 4.0
NEAR, MOVE = 0.005, 12.0
AGE = int(sys.argv[2]) if len(sys.argv) > 2 else 3


def _gz_text(path):
    """The engine appends one gzip member per write; a member cut by a restart is corrupt. Read every good member, skip bad bytes."""
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
    txt = b"".join(out).decode("utf-8", "ignore")
    return "\n".join(l for l in txt.splitlines() if l.count(",") == 4)


def day_data(day, sym="NIFTY"):
    hp = os.path.join(OFD, day, f"heatmap_{sym}.csv.gz")
    if not os.path.exists(hp) or os.path.getsize(hp) < 50_000:
        return None
    h = pd.read_csv(io.StringIO(_gz_text(hp)), header=None, names=["t", "sym", "p", "bid", "ask"], on_bad_lines="skip")
    h = h.apply(pd.to_numeric, errors="coerce").dropna(subset=["t", "p"]) if h.dtypes["t"] == object else h
    s = pd.read_csv(os.path.join(OFD, day, f"sec_{sym}.csv"), usecols=["t", "mid"]).dropna()
    return h, s.set_index("t").mid


def run():
    days = sorted(d for d in os.listdir(OFD) if d.startswith("20"))
    touch, pulled = [], []
    for day in days:
        x = day_data(day)
        if x is None:
            continue
        h, mid = x
        mids = mid.groupby(mid.index // 60 * 60).last()
        state = {}                                   # (side, p) -> minutes in a row big
        for t, g in h.groupby("t"):
            m = mids.get(t // 60 * 60)
            if m is None or np.isnan(m):
                continue
            now_big = set()
            for side, col in ((1, "bid"), (-1, "ask")):
                gg = g[(g[col] > 0) & ((g.p - m).abs() / m <= NEAR)]
                if len(gg) < 10:
                    continue
                med = gg[col].median()
                for p, q in zip(gg.p, gg[col]):
                    if q >= K * med:
                        now_big.add((side, p))
                        n = state.get((side, p), 0) + 1; state[(side, p)] = n
                        if n == AGE:
                            touch.append({"day": day, "t": t, "side": side, "p": p, "mult": q / med, "kind": "wall"})
                # control: normal buckets (0.8-1.2x median) at least 4 buckets from mid, once per 10 min
                if t % 600 == 0:
                    norm = gg[(gg[col] >= 0.8 * med) & (gg[col] <= 1.2 * med) & ((gg.p - m).abs() >= 8)]
                    if len(norm):
                        r = norm.sample(1, random_state=int(t) % 1000)
                        touch.append({"day": day, "t": t, "side": side, "p": float(r.p.iloc[0]), "mult": 1.0, "kind": "control"})
            for k in [k for k in state if k not in now_big]:
                if state[k] >= AGE and abs(k[1] - m) >= 6:          # gone while price was still away = pulled, not eaten
                    side, p = k
                    pulled.append({"day": day, "t": t, "side": side, "p": p, "m": m})
                state.pop(k)
        # outcomes from the 1-second path
        ts = mid.index.values; px = mid.values
        for ev in [e for e in touch if e["day"] == day]:
            i0 = np.searchsorted(ts, ev["t"]); end = np.searchsorted(ts, ev["t"] + 1800)
            seg = px[i0:end]; wall = ev["p"] + (1 if ev["side"] > 0 else 0)          # bucket top for bids, bottom for asks
            hit = np.where(np.abs(seg - wall) <= 1.0)[0] if len(seg) else []
            if not len(hit):
                ev["reached"] = False; continue
            j = i0 + hit[0]; after = px[j:j + 1800]; sd = ev["side"]          # bid wall = support: bounce = up
            up = np.where(sd * (after - wall) >= MOVE)[0]; dn = np.where(sd * (after - wall) <= -MOVE)[0]
            ev["reached"] = True
            ev["held"] = None if (not len(up) and not len(dn)) else bool(len(up) and (not len(dn) or up[0] < dn[0]))
        for ev in [e for e in pulled if e["day"] == day]:
            i0 = np.searchsorted(ts, ev["t"]); seg = px[i0:i0 + 900]
            if len(seg) > 60:
                # + = price moved toward where the wall was (bid wall pulled -> price fell)
                ev["toward"] = float(-ev["side"] * (seg[-1] - seg[0]))
                ev["dist"] = abs(ev["p"] - ev["m"])
    T = pd.DataFrame(touch); P = pd.DataFrame(pulled)
    print(f"K = {K}x median | days used {T.day.nunique() if len(T) else 0}")
    for kind in ("wall", "control"):
        a = T[(T.kind == kind)]
        r = a[a.reached == True]; dec = r[r.held.notna()]
        print(f"{kind:8s}: events {len(a)}, price reached it {len(r)} ({100*len(r)/max(1,len(a)):.0f}%), decided {len(dec)}, "
              f"HELD (bounced {MOVE:.0f} pts first) {100*dec.held.astype(bool).mean():.1f}%" if len(dec) else f"{kind}: none decided")
        if kind == "wall" and len(dec):
            for lo_, hi_ in ((K, 6), (6, 10), (10, 1e9)):
                b = dec[(dec.mult >= lo_) & (dec.mult < hi_)]
                if len(b) >= 10:
                    print(f"     size {lo_:.0f}-{hi_ if hi_ < 1e9 else 'up'}x: n {len(b)} held {100*b.held.astype(bool).mean():.1f}%")
            for d, b in dec.groupby("day"):
                pass
            print("     held % by day:", {d: round(100 * b.held.astype(bool).mean()) for d, b in dec.groupby("day") if len(b) >= 5})
    if len(P) and "toward" in P:
        q = P.dropna(subset=["toward"]); q = q[q.dist <= 30]
        print(f"pulled walls within 30 pts: {len(q)} | next 15 min moved TOWARD the old wall {100*(q.toward > 0).mean():.1f}% | avg {q.toward.mean():+.1f} pts")


if __name__ == "__main__":
    run()
