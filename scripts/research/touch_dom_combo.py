"""TOUCH + DOM combo - history check (2026-11-04, Nikhil: "combination of touch min and DOM .. check with history .. always use
synthetic future"). Paper research only, never orders. Rules fixed BEFORE running (copied from the two live rules):

  levels  : futures prices, per day: prev day high / low / close, prev day POC / VAH / VAL (70 % value area from footprint bins),
            today's initial balance high / low (09:15-10:15, used after 10:15), round numbers (Nifty 100s, Bank Nifty 500s)
  touch   : a 1-min bar reaches a level approached from the other side (prev close above -> support test, below -> resistance);
            first touch per level per day, re-touch after 15 min; 09:30-14:30
  TOUCH   : touch bar delta / volume > +0.2 at support (long) or < -0.2 at resistance (short)   (touch-minute rule)
  DOM     : 20-level book imbalance, mean of the last 30 s of the touch bar minus its own 30-min median: >= +0.10 for a long,
            <= -0.10 for a short   (DOM rule's book-lean filter)
  COMBO   : TOUCH and DOM together
  trade   : enter at the touch bar close; stop on 1-min closes 0.10 % past the level (at least 0.05 % from entry); +1R -> breakeven;
            +2R -> trail 1R behind the best close; out after 30 min or 15:20. One open trade per index.
  money   : synthetic future (buy ATM call + sell ATM put), 1 lot: Nifty 65, Bank Nifty 30; cost 4.5 / 12.5 pts round trip.
Data: ~/Documents/dhan/orderflow/data/of/<day>/sec_<SYM>.csv (per second: book, mid, market buys / sells).
"""
import json, os, sys
import numpy as np, pandas as pd

OF = os.environ.get("MP_OF_DIR") or os.path.expanduser("~/Documents/dhan/orderflow/data/of")
LOT = {"NIFTY": 65, "BANKNIFTY": 30}; COST = {"NIFTY": 4.5, "BANKNIFTY": 12.5}; RND = {"NIFTY": 100, "BANKNIFTY": 500}
TD, DOMT, SLB, MINR = 0.2, 0.10, 0.001, 0.0005


BIN = {"NIFTY": 1.0, "BANKNIFTY": 5.0}


def _sec(day, sym):
    s = pd.read_csv(os.path.join(OF, day, f"sec_{sym}.csv"), usecols=["t", "bid", "ask", "mid", "last", "buy_vol", "sell_vol", "imb20"])
    s = s[(s.bid > 0) & (s.ask > 0)].copy()
    s["time"] = pd.to_datetime(s.t, unit="s", utc=True).dt.tz_convert("Asia/Kolkata").dt.strftime("%H:%M")
    return s[(s.time >= "09:15") & (s.time < "15:30")]


def bars(day, sym):
    """1-min bars from the per-second file (the footprint file is empty on some days): OHLC of the mid, delta = market buys -
    market sells, volume bins at the last trade price. Days without a trade feed have vol 0 (no touch-minute signal possible)."""
    s = _sec(day, sym); g = s.groupby("time")
    B = pd.DataFrame({"t": g.t.min() // 60 * 60, "o": g.mid.first(), "h": g.mid.max(), "l": g.mid.min(), "c": g.mid.last(),
                      "delta": g.buy_vol.sum() - g.sell_vol.sum(), "vol": g.buy_vol.sum() + g.sell_vol.sum()}).reset_index()
    tr = s[(s.buy_vol + s.sell_vol) > 0].dropna(subset=["last"])
    tr = tr.assign(p=(tr["last"] / BIN[sym]).round() * BIN[sym], v=tr.buy_vol + tr.sell_vol)
    vb = tr.groupby(["time", "p"]).v.sum()
    B["bins"] = [[[p, v, 0] for p, v in vb.loc[t].items()] if t in vb.index.get_level_values(0) else [] for t in B.time]
    return B.sort_values("t").reset_index(drop=True)


def value_area(df):
    vol = {}
    for bl in df.bins:
        for lv in bl:
            vol[lv[0]] = vol.get(lv[0], 0) + (lv[1] or 0) + (lv[2] or 0)
    if not vol:
        return None
    s = pd.Series(vol).sort_index(); poc = s.idxmax(); tot = s.sum()
    i = j = s.index.get_loc(poc); acc = s.iloc[i]
    while acc < 0.7 * tot and (i > 0 or j < len(s) - 1):
        up = s.iloc[j + 1] if j < len(s) - 1 else -1; dn = s.iloc[i - 1] if i > 0 else -1
        if up >= dn: j += 1; acc += s.iloc[j]
        else: i -= 1; acc += s.iloc[i]
    return float(poc), float(s.index[j]), float(s.index[i])


def book(day, sym):
    s = pd.read_csv(os.path.join(OF, day, f"sec_{sym}.csv"), usecols=["t", "imb20", "mid", "bid", "ask"])
    s = s[(s.bid > 0) & (s.ask > 0)].dropna(subset=["imb20"])
    return s.set_index("t").imb20.sort_index()


def lean(imb, t_end):
    last = imb.loc[t_end - 30:t_end]; base = imb.loc[t_end - 1800:t_end]
    if len(last) < 10 or len(base) < 300:
        return None
    return float(last.mean() - base.median())


def run():
    days = sorted(d for d in os.listdir(OF) if len(d) == 10 and d[4] == "-")
    rows = []
    for sym in ("NIFTY", "BANKNIFTY"):
        prev = None
        for day in days:
            try:
                B = bars(day, sym); imb = book(day, sym)
            except Exception as e:
                print("skip", day, sym, e); prev = None; continue
            if len(B) < 60:
                continue
            if B.vol.sum() == 0:                       # no trade feed that day (29-30 Oct): no touch-minute delta -> no trades,
                prev = B; continue                     # but its high / low / close still serve as the previous day
            if prev is not None:
                va = value_area(prev)
                lv = {"PDH": prev.h.max(), "PDL": prev.l.min(), "PDC": prev.c.iloc[-1]}
                if va: lv.update({"pPOC": va[0], "pVAH": va[1], "pVAL": va[2]})
                ib = B[(B.time >= "09:15") & (B.time < "10:15")]
                ibh, ibl = ib.h.max(), ib.l.min()
                lo, hi = B.l.min(), B.h.max(); r = RND[sym]
                rounds = {f"R{int(x)}": float(x) for x in np.arange(np.floor(lo / r) * r, hi + r, r)}
                last_touch = {}; busy_until = "00:00"
                for i in range(1, len(B)):
                    b, p = B.iloc[i], B.iloc[i - 1]
                    if not ("09:30" <= b.time <= "14:30") or b.time < busy_until:
                        continue
                    L = dict(lv); L.update(rounds)
                    if b.time >= "10:15": L.update({"IBH": ibh, "IBL": ibl})
                    for name, x in L.items():
                        side = 1 if (p.c > x and b.l <= x) else -1 if (p.c < x and b.h >= x) else 0
                        if not side:
                            continue
                        key = round(x, 1)
                        if key in last_touch and (b.t - last_touch[key]) < 900:
                            continue
                        last_touch[key] = b.t
                        td = b.delta / b.vol if b.vol else 0
                        ln = lean(imb, b.t + 59)
                        touch_ok = td > TD if side > 0 else td < -TD
                        dom_ok = ln is not None and (ln >= DOMT if side > 0 else ln <= -DOMT)
                        dom_against = ln is not None and (ln <= -DOMT if side > 0 else ln >= DOMT)
                        e = b.c
                        stop = min(x * (1 - SLB), e * (1 - MINR)) if side > 0 else max(x * (1 + SLB), e * (1 + MINR))
                        R = abs(e - stop); sl, best, trail, why = stop, e, 0, "30 min"
                        fut = B.iloc[i + 1:i + 31]; fut = fut[fut.time <= "15:20"]
                        ex = e
                        for _, f in fut.iterrows():
                            ex = f.c
                            if side * (f.c - sl) <= 0:
                                why = ["SL", "breakeven", "trail"][trail]; break
                            best = max(best, f.c) if side > 0 else min(best, f.c); mv = side * (best - e)
                            if mv >= 2 * R:
                                nt = best - side * R; sl = max(sl, nt) if side > 0 else min(sl, nt); trail = 2
                            elif mv >= R and trail == 0:
                                sl = e; trail = 1
                        pts = side * (ex - e)
                        rows.append({"day": day, "sym": sym, "time": b.time, "level": name, "side": side, "td": round(td, 3),
                                     "lean": None if ln is None else round(ln, 3), "touch": touch_ok, "dom": dom_ok, "against": dom_against,
                                     "pts": round(pts, 1), "net": round(pts - COST[sym], 1), "rs": round((pts - COST[sym]) * LOT[sym]), "exit": why})
                        break                                   # one level per bar
            prev = B
    return pd.DataFrame(rows), days


def table(T, days):
    half = days[len(days) // 2] if days else ""
    def st(x):
        if not len(x): return "n 0"
        return (f"n {len(x):3d} · win {100 * (x.net > 0).mean():3.0f}% · gross {x.pts.mean():+6.1f} pts · net {x.net.mean():+6.1f} pts · "
                f"Rs/lot {x.rs.sum():+9,.0f} · 1st half {x[x.day < half].rs.sum():+8,.0f} · 2nd half {x[x.day >= half].rs.sum():+8,.0f}")
    out = []
    for sym in ("NIFTY", "BANKNIFTY"):
        S = T[T.sym == sym]
        out.append(f"\n{sym}  ({S.day.nunique()} days, split at {half})")
        for lab, m in (("all touches (fade)", S.index == S.index), ("TOUCH only", S.touch), ("COMBO touch + DOM", S.touch & S.dom),
                       ("touch, DOM against", S.touch & S.against), ("DOM only", S.dom & ~S.touch)):
            out.append(f"  {lab:22s} {st(S[m])}")
    return "\n".join(out)


if __name__ == "__main__":
    T, days = run()
    os.makedirs("data_store/research/touch_dom", exist_ok=True)
    T.to_csv("data_store/research/touch_dom/trades.csv", index=False)
    print("days", days[0], "->", days[-1], len(days)); print(table(T, days))
    C = T[T.touch & T.dom]
    print("\nCOMBO trades:"); print(C[["day", "sym", "time", "level", "side", "td", "lean", "pts", "rs", "exit"]].to_string(index=False))
