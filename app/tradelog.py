"""ONE CSV OF EVERY PAPER TRADE (2026-11-06, Nikhil: "record them somewhere in the backend for all strategies and trades across all tabs
in a single CSV file as well").

data_store/livedesk/trade_log.csv - one row per paper trade, every rule, every market (Indian index + stocks, crypto, US, metals,
commodities), open and closed, with the instrument / strikes / premiums (app/legs.py) and the review notes (app/tradereview.py,
domcards review for DOM). Rewritten every 5 min by the live-desk loop (tick) and on demand (write). Paper only - never orders.
The file is a record for reading in Excel / pandas; nothing reads it back to change a rule.
"""
from __future__ import annotations

import csv
import os
import time
from datetime import datetime

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
PATH = os.path.join(ROOT, "data_store", "livedesk", "trade_log.csv")
EVERY = 300
S: dict = {"t": 0, "n": 0, "err": None, "at": None}
COLS = ["rule", "rule_label", "group", "market", "key", "side", "status", "plan_account", "opened_ist", "closed_ist",
        "entry", "stop", "target", "exit", "exit_reason", "pnl", "ccy", "r", "pnl_open", "now",
        "instrument", "legs", "legs_expiry", "legs_gross", "legs_costs", "legs_net", "premium_check",
        "why_taken", "went_right", "went_wrong", "where_to_improve", "best_R", "worst_R", "after_exit_best_R", "day_move_pct",
        "how_it_exits", "logged_at"]


def _row(t, now):
    L = t.get("legs") or {}
    R = t.get("review") or {}
    chk = "; ".join(L.get("flags") or [])
    if not chk and L.get("prem_pts") is not None and L.get("pts_result") is not None:
        chk = f"ok: legs {L['prem_pts']:+.1f} pts vs points {L['pts_result']:+.1f}"
    return {"rule": t.get("rule"), "rule_label": t.get("rule_label"), "group": t.get("group"), "market": t.get("market"), "key": t.get("key"),
            "side": t.get("side"), "status": t.get("status"), "plan_account": t.get("plan", ""),
            "opened_ist": t.get("opened"), "closed_ist": t.get("closed", ""), "entry": t.get("entry"), "stop": t.get("stop"),
            "target": t.get("target") if not t.get("targets") else " / ".join(str(x) for x in t["targets"]),
            "exit": t.get("exit"), "exit_reason": t.get("exit_reason"), "pnl": t.get("pnl"), "ccy": t.get("ccy"), "r": t.get("r"),
            "pnl_open": t.get("pnl_open"), "now": t.get("now"), "instrument": t.get("instrument"), "legs": t.get("legs_text", ""),
            "legs_expiry": L.get("expiry"), "legs_gross": L.get("gross"), "legs_costs": L.get("costs"), "legs_net": L.get("net"),
            "premium_check": chk, "why_taken": t.get("why", ""), "went_right": R.get("right", ""), "went_wrong": R.get("wrong", ""),
            "where_to_improve": R.get("improve", ""), "best_R": R.get("mfe"), "worst_R": R.get("mae"), "after_exit_best_R": R.get("post_best"),
            "day_move_pct": R.get("day_move_pct"), "how_it_exits": t.get("how", ""), "logged_at": now}


def write(hot, px=None) -> dict:
    PT = hot("papertrades")
    rows, errs = PT.all_rows(hot, px or {})
    now = datetime.now().isoformat(timespec="seconds")
    rows = sorted(rows, key=lambda t: (str(t.get("opened")), str(t.get("rule"))))
    os.makedirs(os.path.dirname(PATH), exist_ok=True)
    tmp = PATH + f".{os.getpid()}.tmp"
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=COLS)
        w.writeheader()
        for t in rows:
            w.writerow(_row(t, now))
    os.replace(tmp, PATH)
    try:
        hot("tradereview").flush(force=True); hot("legs")._flush()
    except Exception:
        pass
    S.update(t=time.time(), n=len(rows), err=errs or None, at=now)
    return {"path": PATH, "rows": len(rows), "errors": errs}


def tick(hot, px=None, force=False):
    if not force and time.time() - S["t"] < EVERY:
        return False
    S["t"] = time.time()
    if S.get("busy"):
        return False

    def run():                                   # in the background: the live-desk loop must not wait ~5 s for the file
        S["busy"] = True
        try:
            write(hot, px)
        except Exception as e:
            S["err"] = f"{type(e).__name__}: {str(e)[:160]}"
        finally:
            S["busy"] = False
    import threading
    threading.Thread(target=run, name="tradelog", daemon=True).start()
    return True
