"""STOCK VOLUME BREAK v1 - PAPER ONLY (2026-11-08, Nikhil: "update the strategy and start paper trading .. we can loosen the trail ATR to
2 ATR .. if the default SL is tight then loosen it up"). Never places orders. New rule beside DOM stocks (DOM stocks is unchanged).

Rule (scripts/research/stock_volume_adx_backtest.py + stock_vadx_deepcheck.py, 48 F&O stocks, 81 sessions 14 Jul - 6 Nov 2026):
  levels   yesterday's high / low and today's 09:15-09:30 opening range high / low
  signal   a 5-min close crosses a level (previous 5-min close on the other side), the first such cross of that level in that
           direction today, signal bars 09:30-14:30
  filters  that bar's volume >= 6x the median of the same 5-min slot over the previous 10 sessions
           ADX(14, 5-min) > 25 with +DI above -DI for a long (below for a short)
           day volume so far >= 2x the average to the same time over the previous 10 sessions
  entry    the next 5-min bar's open (= the open of the first minute after the signal bar)
  stop     2 x ATR(14, 5-min) from entry (min 0.15 % of price) - the 1-ATR stop tested too tight (costs ~0.8R a trade)
  exits    two versions of every signal, each its own paper row:
             svb     target 2R, flat 15:15                          backtest +0.23 % a trade after 0.12 % costs, win 48 %, 149 trades
             svb_tr  trail 2 ATR once +1R, target 3R, flat 15:15     backtest +0.19 % a trade, win 53 % (Nikhil's 2-ATR trail)
  money    MIS 5x paper: Rs 20,000 margin x 5 = Rs 1,00,000 position, costs 0.12 % round trip; one open trade per stock per version
Judge after 30-40 trades. Backtest was picked from ~4,500 combinations (centre of the only cluster positive in both halves), so live
results are expected to be weaker.
Data: 1-min bars from the Mac's Dhan login (stockorb._bars1): all 48 stocks once per 5-min bar close, open trades every minute.
Previous sessions (normal volume, ADX / ATR warm-up, yesterday's high / low): data_store/research/stocks_intraday + Dhan for any gap.
"""
from __future__ import annotations

import importlib.util
import json
import os
import time
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
OUT = os.path.join(ROOT, "data_store", "livedesk", "stockvb")
HIST = os.path.join(ROOT, "data_store", "research", "stocks_intraday")
SPIKE, ADX_MIN, RVOL, STOP_ATR, MIN_STOP = 6.0, 25.0, 2.0, 2.0, 0.0015
TOPVOL = True             # 2026-11-09: trade only the day's top 10 tracked F&O stocks by 09:15-09:30 traded value (app/topvol.py)
VERS = {"svb": {"target": 2.0, "trail": None, "label": "target 2R"},
        "svb_tr": {"target": 3.0, "trail": 2.0, "trail_from": 1.0, "label": "trail 2 ATR after +1R, target 3R"}}
COST_PCT, MARGIN, LEV = 0.12, 20000, 5
POS = MARGIN * LEV
S: dict = {"t": 0, "last_slot": None}
_PREV: dict = {}


def _bt():
    if "bt" not in S:
        p = os.path.join(ROOT, "scripts", "research", "stock_volume_adx_backtest.py")
        sp = importlib.util.spec_from_file_location("stock_volume_adx_backtest", p); m = importlib.util.module_from_spec(sp); sp.loader.exec_module(m)
        S["bt"] = m                                 # same ADX / DI / ATR and bar maths as the backtest
    return S["bt"]


def _p(day):
    return os.path.join(OUT, f"day_{day}.json")


def _load(day):
    try:
        return json.load(open(_p(day)))
    except Exception:
        return {"day": day, "trades": [], "missed": [], "scans": 0}


def _save(day, st):
    os.makedirs(OUT, exist_ok=True)
    tmp = _p(day) + f".{os.getpid()}.tmp"
    json.dump(st, open(tmp, "w"), indent=1, default=str)
    os.replace(tmp, _p(day))


def _prev(sym, day):
    """1-min bars of the last ~15 sessions before `day`: downloaded history, plus Dhan for days after it."""
    k = (sym, day)
    if k in _PREV:
        return _PREV[k]
    d = pd.DataFrame()
    try:
        d = pd.read_csv(os.path.join(HIST, f"{sym}_1m.csv.gz"), parse_dates=["ts"])
        d = d[d.ts.dt.strftime("%Y-%m-%d") < day]
    except Exception:
        pass
    last = d.ts.max().date() if len(d) else date.fromisoformat(day) - timedelta(days=25)
    if (date.fromisoformat(day) - last).days > 1:
        try:
            import dhan_live as DL
            sid = DL.ids_equity(sym)
            b = DL.intraday("NSE_EQ", sid, "EQUITY", last + timedelta(days=1), date.fromisoformat(day) - timedelta(days=1), 1, oi=False) if sid else []
            if b:
                g = pd.DataFrame(b); g["ts"] = pd.to_datetime(g.ts.astype(str).str[:19])
                d = pd.concat([d, g[["ts", "open", "high", "low", "close", "volume"]]])
            time.sleep(0.25)
        except Exception as e:
            S["prev_err"] = f"{sym}: {str(e)[:100]}"
    if len(d):
        d = d.drop_duplicates("ts").sort_values("ts")
        d = d[d.ts.dt.weekday < 5]                     # 2026-11-08: weekend special sessions (Muhurat) are not "yesterday" - skip them
        keep = sorted(d.ts.dt.strftime("%Y-%m-%d").unique())[-15:]
        d = d[d.ts.dt.strftime("%Y-%m-%d").isin(keep)]
    _PREV[k] = d
    return d


def _frame(sym, day, today):
    d = pd.concat([_prev(sym, day), today.reset_index()[["ts", "open", "high", "low", "close", "volume"]]], ignore_index=True)
    d = d.drop_duplicates("ts").sort_values("ts")
    d["day"] = d.ts.dt.strftime("%Y-%m-%d"); d["mn"] = d.ts.dt.hour * 60 + d.ts.dt.minute - 555
    return d[(d.mn >= 0) & (d.mn < 375)]


def _signal(sym, day, d, j):
    """Signals on the closed 5-min bar `j` of `day` (same maths as the backtest build). Returns a list of signal dicts."""
    B = _bt()
    b = B.agg(d, 5)
    b["adx"], b["pdi"], b["mdi"], b["atr"] = B.adx_di(b)
    days = sorted(b.day.unique())
    if day not in days or days.index(day) < 5:
        return []
    pv = b.pivot(index="day", columns="slot", values="v").reindex(index=days)
    norm = pv.shift(1).rolling(10, min_periods=5).median()
    cum = pv.fillna(0).cumsum(axis=1); cnorm = cum.shift(1).rolling(10, min_periods=5).mean()
    x = b[b.day == day].set_index("slot")
    if j not in x.index or (j - 1) not in x.index or not all(s in x.index for s in (0, 1, 2)):
        return []
    prev_day = days[days.index(day) - 1]; pd_ = b[b.day == prev_day]
    lv = {"PDH": float(pd_.h.max()), "PDL": float(pd_.l.min()), "ORH": float(x.loc[[0, 1, 2]].h.max()), "ORL": float(x.loc[[0, 1, 2]].l.min())}
    r, p = x.loc[j], x.loc[j - 1]
    spike = pv.at[day, j] / norm.at[day, j] if norm.at[day, j] else np.nan
    rvol = cum.at[day, j] / cnorm.at[day, j] if cnorm.at[day, j] else np.nan
    out = []
    for name, L0 in lv.items():
        for sd in (1, -1):
            crossed = (p.c <= L0 < r.c) if sd > 0 else (p.c >= L0 > r.c)
            if not crossed:
                continue
            earlier = any(((x.loc[k - 1].c <= L0 < x.loc[k].c) if sd > 0 else (x.loc[k - 1].c >= L0 > x.loc[k].c))
                          for k in range(3, j) if k in x.index and (k - 1) in x.index)
            if earlier:
                continue                       # not the first cross of this level in this direction today
            ok_di = (r.pdi > r.mdi) == (sd > 0)
            out.append({"sym": sym, "level": name, "level_px": round(L0, 2), "s": sd, "slot": int(j), "spike": round(float(spike), 2),
                        "rvol": round(float(rvol), 2), "adx": round(float(r.adx), 1), "di_with": bool(ok_di), "atr": float(r.atr),
                        "bar_close": float(r.c),
                        "pass": bool(spike >= SPIKE and r.adx > ADX_MIN and ok_di and rvol >= RVOL)})
    return out


def _manage(tr, today):
    """Walk the trade's 1-min path from entry (stop checked before the trail moves, as in the backtest); flat at 15:15."""
    V = VERS[tr["ver"]]; e, sd, dist, atr = tr["entry"], tr["s"], tr["risk"], tr["atr"]
    stop = e - sd * dist; best = e
    g = today[today.index >= pd.Timestamp(tr["entry_ts"])]
    prev_c, prev_t = e, tr["entry_ts"]
    for t_, x in g.iterrows():
        if t_.strftime("%H:%M") >= "15:15":
            return _close(tr, float(prev_c), "15:15", prev_t)
        if (sd > 0 and x.low <= stop) or (sd < 0 and x.high >= stop):
            return _close(tr, stop, "trail stop" if stop != e - sd * dist else "stop", t_)
        fav = x.high if sd > 0 else x.low
        if V["target"] and sd * (fav - e) >= V["target"] * dist:
            return _close(tr, e + sd * V["target"] * dist, f"target {V['target']:g}R", t_)
        best = max(best, x.high) if sd > 0 else min(best, x.low)
        if V["trail"] and sd * (best - e) >= V["trail_from"] * dist:
            ts_ = best - sd * V["trail"] * atr
            stop = max(stop, ts_) if sd > 0 else min(stop, ts_)
        prev_c, prev_t = x.close, t_
    if len(g) and datetime.now().strftime("%H:%M") >= "15:20":
        return _close(tr, float(g.close.iloc[-1]), "15:15", g.index[-1])
    now = float(today.close.iloc[-1]) if len(today) else e
    tr.update(now=round(now, 2), stop_now=round(stop, 2), pnl_open=round(POS * (sd * (now - e) / e * 100 - COST_PCT) / 100))
    return False


def _close(tr, px, why, t_):
    g = tr["s"] * (px - tr["entry"]) / tr["entry"] * 100
    tr.update(status="closed", exit=round(float(px), 2), exit_reason=why, closed=str(t_)[:16], gross_pct=round(g, 3), net_pct=round(g - COST_PCT, 3),
              pnl=round(POS * (g - COST_PCT) / 100), r=round((g - COST_PCT) / 100 * tr["entry"] / tr["risk"], 2), pnl_margin_pct=round(LEV * (g - COST_PCT), 2))
    for k in ("now", "pnl_open", "stop_now"):
        tr.pop(k, None)
    return True


def tick(force=False) -> bool:
    now = datetime.now(); hm = now.strftime("%H:%M")
    if S.get("busy") or (not force and (time.time() - S["t"] < 55 or now.weekday() >= 5 or not ("09:31" <= hm <= "15:25"))):
        return False
    import nsecal as NSEC
    if not force and not NSEC.session_ok():                 # 2026-11-10 (Nikhil): no Indian paper trades on NSE holidays / frozen chains
        return False
    S["t"] = time.time(); S["busy"] = True
    import threading
    threading.Thread(target=_work, name="stockvb", daemon=True).start()
    return True


def _work():
    try:
        _work_inner()
        S["err"] = None
    except Exception as e:
        S["err"] = f"{type(e).__name__}: {str(e)[:160]}"
    finally:
        S["busy"] = False


def _work_inner():
    from stockhist import STOCKS
    SO = __import__("stockorb")
    now = datetime.now(); day = now.strftime("%Y-%m-%d"); st = _load(day)
    now_mn = now.hour * 60 + now.minute - 555
    j = now_mn // 5 - 1                                      # last CLOSED 5-min slot
    scan = 3 <= j <= 62 and st.get("last_slot") != j
    open_syms = {t["sym"] for t in st["trades"] if t["status"] == "open"}
    for sym in (STOCKS if scan else sorted(open_syms)):
        try:
            today = SO._bars1(sym, day); time.sleep(0.2)
        except Exception as e:
            st["err"] = f"{sym}: {str(e)[:100]}"; continue
        if not len(today):
            continue
        if scan:
            try:
                sigs = _signal(sym, day, _frame(sym, day, today), j)
            except Exception as e:
                st["err"] = f"{sym} signal: {str(e)[:100]}"; sigs = []
            for sg in sigs:
                st.setdefault("signals", []).append({**sg, "at": now.strftime("%H:%M")})
                if not sg["pass"]:
                    continue
                if TOPVOL:                                   # 2026-11-09 (Nikhil): day's universe = top 10 by opening traded value (app/topvol.py)
                    import topvol as TV
                    ok, note = TV.allowed(sym, day)
                    if not ok:
                        st.setdefault("skipped", []).append({**sg, "why": note, "at": now.strftime("%H:%M")}); continue
                t_in = datetime.fromisoformat(day) + timedelta(minutes=555 + (j + 1) * 5)
                if now - t_in > timedelta(minutes=10):             # restart / late scan - never enter at a stale price
                    st["missed"].append({**sg, "why": "signal found late"}); continue
                for ver, V in VERS.items():
                    if any(t["sym"] == sym and t["ver"] == ver and t["status"] == "open" for t in st["trades"]):
                        continue
                    st["trades"].append({"sym": sym, "day": day, "ver": ver, "s": sg["s"], "side": "long" if sg["s"] > 0 else "short",
                                         "signal_bar": f"{day} {(t_in - timedelta(minutes=5)):%H:%M}", "entry_ts": str(t_in), "opened": f"{day} {t_in:%H:%M}",
                                         "entry": None, "atr": sg["atr"], "level": sg["level"], "level_px": sg["level_px"], "spike": sg["spike"],
                                         "rvol": sg["rvol"], "adx": sg["adx"], "status": "pending", "margin": MARGIN, "leverage": LEV,
                                         "why": f"5-min close {'above' if sg['s'] > 0 else 'below'} {sg['level']} {sg['level_px']:,} · volume {sg['spike']}x normal · "
                                                f"day volume {sg['rvol']}x · ADX {sg['adx']} with DI · exit: {V['label']}"})
        for tr in [t for t in st["trades"] if t["sym"] == sym and t["status"] in ("pending", "open")]:
            if tr["status"] == "pending":
                m = today[today.index >= pd.Timestamp(tr["entry_ts"])]
                if not len(m):
                    continue
                e = float(m.open.iloc[0]); dist = max(STOP_ATR * tr["atr"], e * MIN_STOP)
                tr.update(status="open", entry=round(e, 2), risk=round(dist, 4), stop=round(e - tr["s"] * dist, 2),
                          target=round(e + tr["s"] * VERS[tr["ver"]]["target"] * dist, 2), risk_pct=round(dist / e * 100, 2), qty=int(POS // e))
            _manage(tr, today)
    if scan:
        st["last_slot"] = j; st["scans"] = st.get("scans", 0) + 1
    st["at"] = now.isoformat(timespec="seconds"); _save(day, st)


def trades(days=60):
    files = sorted(f for f in os.listdir(OUT) if f.startswith("day_") and f.endswith(".json")) if os.path.isdir(OUT) else []
    out = []
    for f in files[-days:]:
        try:
            out += [t for t in json.load(open(os.path.join(OUT, f))).get("trades", []) if t.get("status") in ("open", "closed")]
        except Exception:
            pass
    return out


def status():
    day = datetime.now().strftime("%Y-%m-%d"); st = _load(day)
    return {"day": day, "scans": st.get("scans"), "last_slot": st.get("last_slot"), "at": st.get("at"), "trades": len(st["trades"]),
            "signals_seen": len(st.get("signals", [])), "passed": sum(1 for s in st.get("signals", []) if s.get("pass")),
            "missed": len(st.get("missed", [])), "err": S.get("err") or st.get("err"), "busy": S.get("busy")}
