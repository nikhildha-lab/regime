"""
Support / resistance and volume profile from daily bars (no look-ahead: a level used on day t is built from bars
up to t; a swing point counts only after the K bars that confirm it have closed).

Level types
  swing      swing highs / lows (K=5 bars each side) of the past year, merged when within 0.5 ATR; strength = touches
  profile    60-day volume profile: each day's volume spread evenly over its high-low range. POC = busiest price,
             VAH / VAL = edges of the 70% value area. Markets without volume use time-at-price (market profile).
  prior      previous week's and previous month's high and low

Test (scripts/test_levels.py): when the day's low touches a support (within 0.25 ATR) and the close holds above
it - or the mirror for resistance - does price move away from the level over the next 5 days more often than
at fake levels placed a random 1-3 ATR away?
"""
from __future__ import annotations

import numpy as np
import pandas as pd

K = 5


def atr(b: pd.DataFrame, n: int = 14) -> pd.Series:
    h, l, c = b["high"], b["low"], b["close"]
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


def swings(b: pd.DataFrame, k: int = K) -> tuple[pd.Series, pd.Series]:
    """Swing high/low prices indexed by the day they become KNOWN (k bars after the pivot)."""
    h, l = b["high"], b["low"]
    win = 2 * k + 1
    is_hi = h == h.rolling(win, center=True).max()
    is_lo = l == l.rolling(win, center=True).min()
    sh = h.where(is_hi).shift(k)            # known k bars later
    sl = l.where(is_lo).shift(k)
    return sh.dropna(), sl.dropna()


def _cluster(prices: np.ndarray, tol: float) -> list[tuple[float, int]]:
    if len(prices) == 0:
        return []
    p = np.sort(prices)
    out, grp = [], [p[0]]
    for x in p[1:]:
        if x - grp[-1] <= tol:
            grp.append(x)
        else:
            out.append((float(np.mean(grp)), len(grp)))
            grp = [x]
    out.append((float(np.mean(grp)), len(grp)))
    return out


def profile(b: pd.DataFrame, n: int = 60, bins: int = 60) -> dict | None:
    x = b.iloc[-n:]
    if len(x) < 20:
        return None
    lo, hi = float(x["low"].min()), float(x["high"].max())
    if hi <= lo:
        return None
    edges = np.linspace(lo, hi, bins + 1)
    mid = (edges[:-1] + edges[1:]) / 2
    vol = x["volume"].fillna(0).astype(float).values
    use_vol = (vol > 0).mean() > 0.8
    w = vol if use_vol else np.ones(len(x))
    hist = np.zeros(bins)
    for h_, l_, w_ in zip(x["high"].values, x["low"].values, w):
        m = (mid >= l_) & (mid <= h_)
        if m.any():
            hist[m] += w_ / m.sum()
    poc_i = int(hist.argmax())
    # value area: grow from POC until 70% of the total
    tot, a, z = hist.sum(), poc_i, poc_i
    acc = hist[poc_i]
    while acc < 0.7 * tot and (a > 0 or z < bins - 1):
        left = hist[a - 1] if a > 0 else -1
        right = hist[z + 1] if z < bins - 1 else -1
        if right >= left:
            z += 1
            acc += hist[z]
        else:
            a -= 1
            acc += hist[a]
    return {"poc": float(mid[poc_i]), "vah": float(edges[z + 1]), "val": float(edges[a]),
            "kind": "volume" if use_vol else "time-at-price", "days": int(len(x))}


def levels_at(b: pd.DataFrame, sh: pd.Series, sl: pd.Series, a: float) -> list[dict]:
    """All levels known at the last bar of b."""
    t = b.index[-1]
    out = []
    yr = b.index[max(0, len(b) - 252)]
    for name, s in (("swing high", sh), ("swing low", sl)):
        pts = s[(s.index > yr) & (s.index <= t)].values
        for px, n in _cluster(pts, 0.5 * a):
            out.append({"price": px, "type": "swing", "name": name, "strength": n})
    pr = profile(b)
    if pr:
        for k in ("poc", "vah", "val"):
            out.append({"price": pr[k], "type": "profile", "name": k.upper(), "strength": 2 if k == "poc" else 1})
    wk = b["high"].resample("W-FRI").max(), b["low"].resample("W-FRI").min()
    mo = b["high"].resample("ME").max(), b["low"].resample("ME").min()
    for lab, (H, L) in (("week", wk), ("month", mo)):
        H, L = H[H.index < t.normalize() - pd.Timedelta(days=0)], L[L.index < t.normalize()]
        if len(H) >= 2:
            # the last COMPLETE period: its end must be before today
            out.append({"price": float(H.iloc[-1]), "type": "prior", "name": f"prior {lab} high", "strength": 1})
            out.append({"price": float(L.iloc[-1]), "type": "prior", "name": f"prior {lab} low", "strength": 1})
    return out


def level_book(b: pd.DataFrame, start: str = "2011-01-01") -> list[tuple]:
    """For each day i from start: (i, atr known at i-1, list of (price, type)) built from bars before day i."""
    a_ = atr(b)
    sh, sl = swings(b)
    out = []
    for i in np.where(b.index >= start)[0]:
        if i < 260:
            continue
        a = float(a_.iloc[i - 1])
        lv = levels_at(b.iloc[:i], sh[sh.index < b.index[i]], sl[sl.index < b.index[i]], a)
        out.append((i, a, [(x["price"], x["type"]) for x in lv]))
    return out


def flatten(book: list[tuple]) -> dict:
    i, a, p, t, j = [], [], [], [], []
    for k, (ii, aa, lv) in enumerate(book):
        for px, ty in lv:
            i.append(ii); a.append(aa); p.append(px); t.append(ty); j.append(k)
    return {"i": np.array(i), "a": np.array(a), "p": np.array(p), "t": np.array(t), "j": np.array(j)}


def touches_from_book(b: pd.DataFrame, book, offsets: np.ndarray | None = None) -> pd.DataFrame:
    """Touch events; offsets (one per day, in ATR) move every level of that day - used for the fake-level test.
    `book` may be the list from level_book() or its flatten()ed dict (faster for repeated calls)."""
    F = book if isinstance(book, dict) else flatten(book)
    c, h, l = b["close"].values, b["high"].values, b["low"].values
    fwd = (b["close"].shift(-5) / b["close"] - 1).values
    i, a = F["i"], F["a"]
    p = F["p"] + (0.0 if offsets is None else offsets[F["j"]] * a)
    prev = c[i - 1]
    sup = (p < prev) & (l[i] <= p + 0.25 * a) & (c[i] > p)
    res = (p > prev) & (h[i] >= p - 0.25 * a) & (c[i] < p)
    m = sup | res
    return pd.DataFrame({"i": i[m], "type": F["t"][m], "side": np.where(sup, 1, -1)[m], "fwd": fwd[i[m]]})


def touch_events(b: pd.DataFrame, start: str = "2011-01-01", every: int = 1) -> pd.DataFrame:
    """For each day from `start`, the levels known the day BEFORE; record touches and the next-5-day outcome."""
    a_ = atr(b)
    sh, sl = swings(b)
    c, h, l = b["close"], b["high"], b["low"]
    fwd = c.shift(-5) / c - 1
    idx = np.where(b.index >= start)[0]
    rows = []
    for i in idx[::every]:
        if i < 260:
            continue
        past = b.iloc[:i]                                # known before day i
        a = float(a_.iloc[i - 1])
        lv = levels_at(past, sh[sh.index < b.index[i]], sl[sl.index < b.index[i]], a)
        prev = float(c.iloc[i - 1])
        for L in lv:
            p = L["price"]
            if p < prev and l.iloc[i] <= p + 0.25 * a and c.iloc[i] > p:            # support test that held
                side = 1
            elif p > prev and h.iloc[i] >= p - 0.25 * a and c.iloc[i] < p:          # resistance test that held
                side = -1
            else:
                continue
            rows.append({"date": b.index[i], "type": L["type"], "name": L["name"], "strength": L["strength"],
                         "side": side, "dist_atr": (prev - p) / a, "fwd": fwd.iloc[i]})
    return pd.DataFrame(rows)


def nearest(b: pd.DataFrame, verdicts: dict | None = None, n: int = 3) -> dict:
    a = float(atr(b).iloc[-1])
    sh, sl = swings(b)
    lv = levels_at(b, sh, sl, a)
    px = float(b["close"].iloc[-1])
    sup = sorted([x for x in lv if x["price"] < px], key=lambda x: -x["price"])
    res = sorted([x for x in lv if x["price"] > px], key=lambda x: x["price"])

    def merge(xs):
        out = []
        for x in xs:
            if out and abs(out[-1]["price"] - x["price"]) <= 0.3 * a:
                out[-1]["names"].append(x["name"])
                out[-1]["strength"] += x["strength"]
                out[-1]["types"].add(x["type"])
            else:
                out.append({"price": x["price"], "names": [x["name"]], "strength": x["strength"], "types": {x["type"]}})
        for o in out:
            o["dist_atr"] = round((o["price"] - px) / a, 2)
            o["dist_pct"] = round((o["price"] / px - 1) * 100, 2)
            o["tested"] = sorted({(verdicts or {}).get(t, "untested") for t in o["types"]})
            o["types"] = sorted(o["types"])
            o["price"] = round(o["price"], 2)
        return out[:n]
    pr = profile(b)
    return {"close": px, "atr": round(a, 2), "supports": merge(sup), "resistances": merge(res),
            "profile": None if pr is None else {k: (round(v, 2) if isinstance(v, float) else v) for k, v in pr.items()}}


def intraday_profile(m1: pd.DataFrame, bins: int = 80) -> dict | None:
    """Volume profile from 1-minute bars (each bar's volume spread over its high-low). m1: open/high/low/close/volume."""
    if m1 is None or len(m1) < 30:
        return None
    x = m1.rename(columns=str.lower)
    return profile(x.assign(volume=x["volume"].astype(float)), n=len(x), bins=bins)
