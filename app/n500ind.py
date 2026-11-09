"""NIFTY 500 INDUSTRY ROTATION - monthly basket, PAPER ONLY, RECORD-ONLY (2026-11-10, Nikhil: "can we add more" -> sweep 3 + gauntlet).

Rule (scripts/research/quantpedia_gauntlet.py g6: excess over the equal-weight Nifty 500 Sharpe +0.79, 16 of 21 years 2006-26, train 0.84 /
test 0.65, 2x costs 0.68; neighbours top 2 / top 4 similar, 3-m and 12-m look-backs weaker but positive):
   industries = the Industry column of the Nifty 500 list, each an equal-weight basket of its names (industries with >= 5 names)
   at each month-end close: rank industries by their 6-month return; hold the TOP 3, one third each, names equal weight inside; a month
   costs 0.15 % per side on the weight traded   notional Rs 1,00,000
Shares the price cache and helpers of app/n500mom.py. One record per month ("basket trade"). Survivorship caveat as for n500mom.
State: data_store/livedesk/n500ind/state.json.
"""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime

import numpy as np
import pandas as pd

import n500mom as NM

OUT = os.path.join(NM.ROOT, "data_store", "livedesk", "n500ind")
NOTIONAL, COST, TOPK, LOOK = 100000.0, 0.0015, 3, 6
S: dict = {"t": 0, "busy": False, "err": None}


def _load():
    try:
        return json.load(open(os.path.join(OUT, "state.json")))
    except Exception:
        return {"baskets": []}


def _save(st):
    os.makedirs(OUT, exist_ok=True); p = os.path.join(OUT, "state.json"); tmp = p + ".tmp"
    json.dump(st, open(tmp, "w"), indent=1, default=str); os.replace(tmp, p)


def industries():
    d = pd.read_csv(os.path.join(NM.RES, "ind_nifty500list.csv")); return d.set_index("Symbol").Industry.dropna().to_dict()


def _work():
    try:
        st = _load(); U = NM.universe(); IND = industries()
        P = NM._prices(); P = P[P.sym.isin(U)]
        W = P.pivot_table(index="date", columns="sym", values="close").sort_index(); me = NM.month_ends(W.index)
        today = pd.Timestamp(datetime.now().date()); done = [d for d in me if (d.year, d.month) < (today.year, today.month)]
        if len(done) < LOOK + 2:
            S["err"] = "not enough month-ends"; return
        last_me = done[-1]; M = W.reindex(done)
        cur = next((b for b in st["baskets"] if b["formed"] == str(last_me)[:10]), None)
        if cur is None:
            # industry equal-weight index from monthly closes of the names with the data (chain-linked monthly returns)
            MR = (M / M.shift(1) - 1).clip(-0.5, 1.0)
            grp = {}
            for s in M.columns:
                g = IND.get(s)
                if g: grp.setdefault(g, []).append(s)
            grp = {g: s for g, s in grp.items() if len(s) >= 5}
            IR = pd.DataFrame({g: MR[s].mean(axis=1) for g, s in grp.items()}); IP = (1 + IR.fillna(0)).cumprod()
            m6 = (IP.iloc[-1] / IP.iloc[-1 - LOOK] - 1).dropna().sort_values(ascending=False)
            top = list(m6.head(TOPK).index)
            hold = []
            for g in top:
                names = [s for s in grp[g] if pd.notna(M.iloc[-1].get(s, np.nan))]
                for s in names: hold.append({"sym": s, "ind": g, "w": 1.0 / TOPK / len(names), "px0": float(M.iloc[-1][s])})
            prev = st["baskets"][-1] if st["baskets"] else None
            w_new = {h["sym"]: h["w"] for h in hold}; w_old = {h["sym"]: h["w"] for h in (prev or {}).get("holdings", [])} if prev else {}
            turn = sum(abs(w_new.get(s, 0) - w_old.get(s, 0)) for s in set(w_new) | set(w_old))
            if prev and prev["status"] == "open":
                val = sum(h["w"] * (float(W.loc[last_me].get(h["sym"], np.nan)) / h["px0"]) for h in prev["holdings"] if pd.notna(W.loc[last_me].get(h["sym"], np.nan)))
                pct = (val - 1) * 100 - 100 * COST * prev.get("turnover", 2.0)
                prev.update(status="closed", closed=str(last_me)[:10], exit_value=round(val, 5), pnl_pct=round(pct, 3), pnl=round(NOTIONAL * pct / 100))
            cur = {"formed": str(last_me)[:10], "status": "open", "n": len(hold), "industries": top, "scores": {g: round(float(m6[g]) * 100, 1) for g in m6.index}, "turnover": round(turn, 3), "holdings": hold,
                   "why": f"{str(last_me)[:10]}: top {TOPK} of {len(grp)} Nifty 500 industries by {LOOK}-month return - " + ", ".join(f"{g} {m6[g]*100:+.1f}%" for g in top)}
            st["baskets"].append(cur)
        lastrow = W.iloc[-1]; asof = str(W.index[-1])[:10]
        vals = [(h["w"] * float(lastrow.get(h["sym"], np.nan)) / h["px0"]) for h in cur["holdings"] if pd.notna(lastrow.get(h["sym"], np.nan))]
        if vals:
            val = sum(vals) + (1 - sum(h["w"] for h in cur["holdings"] if pd.notna(lastrow.get(h["sym"], np.nan))))
            cur["now_value"] = round(val, 5); cur["as_of"] = asof; cur["pnl_open_pct"] = round((val - 1) * 100 - 100 * COST * cur.get("turnover", 2.0), 3)
            cur["pnl_open"] = round(NOTIONAL * cur["pnl_open_pct"] / 100); cur["marked"] = len(vals)
        st["at"] = datetime.now().isoformat(timespec="seconds"); _save(st); S["err"] = None
    except Exception as e:
        S["err"] = f"{type(e).__name__}: {str(e)[:160]}"
    finally:
        S["busy"] = False


def tick(force=False):
    if S["busy"] or (not force and time.time() - S["t"] < 1800):
        return False
    S["t"] = time.time(); S["busy"] = True
    threading.Thread(target=_work, name="n500ind", daemon=True).start()
    return True


def trades():
    return [b for b in _load().get("baskets", []) if b.get("status") in ("open", "closed")]


def api():
    st = _load()
    return {"name": "Nifty 500 Industry Rotation", "record_only": True, "baskets": st.get("baskets", [])[-13:], "err": S.get("err"), "at": st.get("at"),
            "rule": f"At each month-end: rank the Nifty 500 industries (equal-weight baskets) by {LOOK}-month return; hold the top {TOPK}, a third each, names equal weight inside, for a month. Rs 1,00,000 notional, 0.15 % per side on the weight traded. Record only."}
