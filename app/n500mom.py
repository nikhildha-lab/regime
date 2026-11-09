"""NIFTY 500 MOMENTUM 12-1 - monthly basket, PAPER ONLY, RECORD-ONLY (2026-11-10, Nikhil: "ok add", after the Quantpedia gauntlet).

Rule (as tested in scripts/research/quantpedia_gauntlet.py: excess over the equal-weight universe Sharpe +0.66, 17 of 21 years, 2006-26):
   at each month-end close: score = close one month ago / close 12 months ago - 1 (12-1 momentum, skipping the last month)
   hold the TOP DECILE of the Nifty 500 names that have 13 months of history, equal weight, for the next month; rebalance at the next month-end
   costs 0.15 % per side on the weight actually traded (Indian delivery)   notional Rs 1,00,000
One record per month ("basket trade"): opened at the month-end close, closed at the next month-end close, P&L in rupees; the open month is
marked daily. Survivorship caveat: the universe is today's Nifty 500 list.
Data: month-end closes from (a) the research file MASTER_BACKTESTER/data/nse_nifty500 (to 28 Oct 2026), (b) the app's NSE bhavcopy closes
(data_store/market_data/nse_stocks_eod.csv, extended to the Nifty 500 list from today), (c) a Yahoo gap-fill (<SYM>.NS) for names that
miss a month-end close. Cache: data_store/livedesk/n500mom/prices.csv. State: data_store/livedesk/n500mom/state.json.
"""
from __future__ import annotations

import glob
import json
import os
import threading
import time
from datetime import datetime

import numpy as np
import pandas as pd

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
DOCS = os.environ.get("MP_DOCS") or os.path.join(os.path.expanduser("~"), "Documents")
RES = os.path.join(DOCS, "MASTER_BACKTESTER", "data", "nse_nifty500")
EOD = os.path.join(ROOT, "data_store", "market_data", "nse_stocks_eod.csv")
OUT = os.path.join(ROOT, "data_store", "livedesk", "n500mom")
NOTIONAL, COST, DECILE = 100000.0, 0.0015, 0.10
S: dict = {"t": 0, "busy": False, "err": None, "yahoo": None}


def _load():
    try:
        return json.load(open(os.path.join(OUT, "state.json")))
    except Exception:
        return {"baskets": []}


def _save(st):
    os.makedirs(OUT, exist_ok=True); p = os.path.join(OUT, "state.json"); tmp = p + ".tmp"
    json.dump(st, open(tmp, "w"), indent=1, default=str); os.replace(tmp, p)


def universe():
    try:
        return sorted(pd.read_csv(os.path.join(RES, "ind_nifty500list.csv")).Symbol.dropna().unique().tolist())
    except Exception:
        return []


def _prices():
    """long table sym, date, close - research file + EOD + cache, de-duplicated."""
    os.makedirs(OUT, exist_ok=True); cache = os.path.join(OUT, "prices.csv")
    parts = []
    if os.path.exists(cache):
        parts.append(pd.read_csv(cache, parse_dates=["date"]))
    else:
        for f in sorted(glob.glob(os.path.join(RES, "nifty500_daily_part*.csv"))):
            d = pd.read_csv(f, usecols=["sym", "day", "c"]); d = d[~d.sym.str.startswith("^")]
            parts.append(pd.DataFrame({"sym": d.sym, "date": pd.to_datetime(d.day, unit="D"), "close": d.c}))
    if os.path.exists(EOD):
        e = pd.read_csv(EOD, usecols=["date", "symbol", "close"]); parts.append(pd.DataFrame({"sym": e.symbol, "date": pd.to_datetime(e.date), "close": e.close}))
    P = pd.concat(parts, ignore_index=True).dropna().drop_duplicates(["sym", "date"], keep="last").sort_values(["sym", "date"])
    P.to_csv(cache, index=False)
    return P


def _yahoo_fill(syms, days=45):
    """gap-fill recent daily closes from Yahoo for the given names (sequential, ~0.3 s apart); appends to the cache."""
    import updater as U
    rows = []
    for i, s in enumerate(syms):
        try:
            df, tz = U.yahoo_chart(f"{s}.NS", "1d", f"{days}d")
            if len(df):
                ts = df.ts.dt.tz_convert("Asia/Kolkata").dt.normalize().dt.tz_localize(None)
                rows.append(pd.DataFrame({"sym": s, "date": ts.values, "close": df.close.values}))
        except Exception as e:
            S["yahoo"] = f"{s}: {str(e)[:60]}"
        time.sleep(0.3)
    if rows:
        cache = os.path.join(OUT, "prices.csv"); old = pd.read_csv(cache, parse_dates=["date"]) if os.path.exists(cache) else pd.DataFrame(columns=["sym", "date", "close"])
        P = pd.concat([old, *rows], ignore_index=True).drop_duplicates(["sym", "date"], keep="last").sort_values(["sym", "date"]); P.to_csv(cache, index=False)
    return len(rows)


def month_ends(dates):
    s = pd.Series(pd.to_datetime(sorted(set(dates)))); g = s.groupby([s.dt.year, s.dt.month]).max()
    return [pd.Timestamp(x) for x in g.values]


def _work():
    try:
        st = _load(); U = universe()
        P = _prices(); P = P[P.sym.isin(U)]
        W = P.pivot_table(index="date", columns="sym", values="close").sort_index()
        me = month_ends(W.index)
        today = pd.Timestamp(datetime.now().date())
        # the last complete month-end (a date in a month that has ended)
        done = [d for d in me if (d.year, d.month) < (today.year, today.month)]
        if len(done) < 14:
            S["err"] = "not enough month-ends"; return
        last_me = done[-1]
        # gap-fill: names without a close on the last month-end (research file ends 28 Oct; EOD covers ~200 names)
        have = W.loc[last_me].dropna().index if last_me in W.index else pd.Index([])
        missing = [s for s in U if s not in have]
        if missing and not st.get("filled_for") == str(last_me)[:10]:
            n = _yahoo_fill(missing[:600])
            st["filled_for"] = str(last_me)[:10]; st["filled_n"] = n; _save(st)
            P = _prices(); P = P[P.sym.isin(U)]; W = P.pivot_table(index="date", columns="sym", values="close").sort_index(); me = month_ends(W.index)
        M = W.reindex([d for d in me if d <= last_me])
        if len(M) < 14:
            S["err"] = "not enough month-end rows"; return
        # basket at the last complete month-end
        cur = next((b for b in st["baskets"] if b["formed"] == str(last_me)[:10]), None)
        if cur is None:
            score = (M.iloc[-2] / M.iloc[-13] - 1).dropna()
            score = score[(M.iloc[-1].reindex(score.index).notna())]
            n = max(10, int(round(len(score) * DECILE)))
            top = score.sort_values(ascending=False).head(n)
            px0 = M.iloc[-1].reindex(top.index)
            prev = st["baskets"][-1] if st["baskets"] else None
            w_new = {s: 1.0 / n for s in top.index}; w_old = {h["sym"]: h["w"] for h in (prev or {}).get("holdings", [])} if prev else {}
            turn = sum(abs(w_new.get(s, 0) - w_old.get(s, 0)) for s in set(w_new) | set(w_old))
            # close the previous basket at this month-end
            if prev and prev["status"] == "open":
                val = sum(h["w"] * (float(W.loc[last_me].get(h["sym"], np.nan)) / h["px0"]) for h in prev["holdings"] if pd.notna(W.loc[last_me].get(h["sym"], np.nan)))
                pct = (val - 1) * 100 - 100 * COST * prev.get("turnover", 2.0)
                prev.update(status="closed", closed=str(last_me)[:10], exit_value=round(val, 5), pnl_pct=round(pct, 3), pnl=round(NOTIONAL * pct / 100))
            cur = {"formed": str(last_me)[:10], "status": "open", "n": n, "universe": int(score.shape[0]), "turnover": round(turn, 3),
                   "holdings": [{"sym": s, "w": 1.0 / n, "px0": float(px0[s]), "mom": round(float(top[s]) * 100, 1)} for s in top.index if pd.notna(px0[s])],
                   "why": f"{str(last_me)[:10]}: top {n} of {score.shape[0]} Nifty 500 names by 12-1 momentum (one month ago vs 12 months ago), equal weight"}
            st["baskets"].append(cur)
        # mark the open basket at the latest closes
        lastrow = W.iloc[-1]; asof = str(W.index[-1])[:10]
        vals = [(h["w"] * float(lastrow.get(h["sym"], np.nan)) / h["px0"]) for h in cur["holdings"] if pd.notna(lastrow.get(h["sym"], np.nan))]
        if vals:
            val = sum(vals) + (1 - sum(h["w"] for h in cur["holdings"] if pd.notna(lastrow.get(h["sym"], np.nan))))      # names without a fresh close count flat
            cur["now_value"] = round(val, 5); cur["as_of"] = asof; cur["pnl_open_pct"] = round((val - 1) * 100 - 100 * COST * cur.get("turnover", 2.0), 3)
            cur["pnl_open"] = round(NOTIONAL * cur["pnl_open_pct"] / 100); cur["marked"] = sum(1 for h in cur["holdings"] if pd.notna(lastrow.get(h["sym"], np.nan)))
        st["at"] = datetime.now().isoformat(timespec="seconds"); _save(st); S["err"] = None
    except Exception as e:
        S["err"] = f"{type(e).__name__}: {str(e)[:160]}"
    finally:
        S["busy"] = False


def tick(force=False):
    if S["busy"] or (not force and time.time() - S["t"] < 1800):
        return False
    S["t"] = time.time(); S["busy"] = True
    threading.Thread(target=_work, name="n500mom", daemon=True).start()
    return True


def trades():
    return [b for b in _load().get("baskets", []) if b.get("status") in ("open", "closed")]


def api():
    st = _load()
    return {"name": "Nifty 500 Momentum 12-1", "record_only": True, "baskets": st.get("baskets", [])[-13:], "err": S.get("err"), "yahoo": S.get("yahoo"), "at": st.get("at"),
            "rule": "At each month-end close: rank Nifty 500 names by (close one month ago / close 12 months ago - 1); hold the top decile equal weight for a month; rebalance next month-end. Rs 1,00,000 notional, 0.15 % per side on the weight traded. Record only."}
