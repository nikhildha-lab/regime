"""ALL PAPER TRADES IN ONE PLACE (2026-10-30, Nikhil: "place all trades at one place ... paper trades, not shadow trades ...
trade cards in the relevant sections").

Joins the three paper-trade rules into one list with the same fields:
  * 4H break        - ETH / BTC v3, Gold v1. Money shown as a $10,000 account risking 1% ($100) a trade: $ = R after costs x 100.
  * Touch-minute    - Nifty / Bank Nifty option cards (1 lot, rupees after costs; option prices are Black-Scholes estimates).
  * DOM hold / break - plan account (index: synthetic future 1 lot in rupees; stocks: Rs 1,000 risk).
  * Touch + DOM v1  - touch-minute cards confirmed by the DOM book lean (from 5 Nov), synthetic future 1 lot, rupees.
Shadow trades (research records) and skipped signals are NOT included. Paper only - nothing here ever places an order.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd

NAME = {"BTC": "BTC", "ETH": "ETH", "GOLD": "Gold", "NIFTY": "Nifty", "BANKNIFTY": "Bank Nifty",
        "XRP": "XRP", "BNB": "BNB", "LINK": "LINK", "UNI": "UNI", "ZEC": "ZEC", "DASH": "DASH"}   # 2026-11-04: 4H break forward test on more coins
RULES = {
    "b4h": {"label": "4H break", "edge": "tested edge (backtest 2023-26)"},
    "touch": {"label": "Touch-minute option", "edge": "lead - forward test"},
    "dom": {"label": "DOM hold / break (plan account)", "edge": "lead - paper test (judge after 30-40 sessions)"},
    "touchdom": {"label": "Touch + DOM v1 (synthetic)", "edge": "new - history only 4 trades; forward paper test from 5 Nov"},
}


def _ist(ts_utc):
    try:
        return (pd.Timestamp(str(ts_utc).replace(" UTC", "")) + timedelta(hours=5, minutes=30)).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return str(ts_utc)[:16]


def _b4h(hot, px):
    rows = hot("break4h")._load()
    out = []
    for r in rows:
        if r.get("rule") not in ("v3", "v1") or r.get("mkt") not in NAME:
            continue
        if r.get("rule") == "v1" and r["mkt"] != "GOLD":
            continue
        sd = r["side"] if isinstance(r["side"], int) else (1 if r["side"] == "long" else -1)
        risk = r.get("risk") or 0
        units = 100.0 / risk if risk else 0
        tg = r.get("targets")
        t = {"rule": "b4h", "key": r["mkt"], "side": "long" if sd > 0 else "short", "opened": _ist(pd.Timestamp(r["bar"]) + timedelta(hours=4)),
             "entry": r["entry"], "stop": r.get("stop"), "target": (tg[-1] if tg else None), "targets": tg,
             "how": ("3 targets 1R / 2R / 3R" if tg else f"trail {r.get('trail_atr', 1.5)} ATR after +1R" if r.get("exit_mode") == "trail" else "out after 2 days")
                    + f" · out by {_ist(r['exit_by'])[5:]} IST" if r.get("exit_by") else "",
             "instrument": f"{NAME[r['mkt']]} perpetual" if r["mkt"] != "GOLD" else "Gold", "size": round(units * r["entry"]), "ccy": "$",
             "risk_money": 100, "status": r["status"], "why": r.get("dir", ""), "proven": r.get("proven")}
        if r["status"] == "closed":
            t.update(exit=r.get("exit"), exit_reason=r.get("exit_reason"), closed=_ist(pd.Timestamp(r.get("closed_bar")) + timedelta(hours=4)) if r.get("closed_bar") else "", r=r.get("R_net"),
                     pnl=round((r.get("R_net") or 0) * 100, 2), pct=r.get("bps_net") and round(r["bps_net"] / 100, 2))
        elif px.get(r["mkt"]) and units:
            t.update(now=px[r["mkt"]], pnl_open=round(units * sd * (px[r["mkt"]] - r["entry"]), 2))
        out.append(t)
    return out


def _touch(hot):
    TT = hot("touchtrades")
    out = []
    try:
        import glob, os
        files = sorted(glob.glob(os.path.join(TT.CDIR, "cards_*.json")))[-30:]
        cards = [c for f in files for c in TT.load_cards(os.path.basename(f)[6:16])]
    except Exception:
        cards = []
    for c in cards:
        o = c.get("option") or {}
        t = {"rule": "touch", "key": c["key"], "side": c["side"], "opened": f"{c['day']} {c['time']}", "entry": c.get("spot_entry"),
             "stop": c.get("spot_sl"), "target": None, "how": f"stop on spot · {c.get('trail', '')} · out by {c.get('exit_by', '')}",
             "instrument": (f"BUY {o['name']} @ ~{c.get('prem_entry')}" if o else "spot only (no option chain)"),
             "size": c.get("capital"), "ccy": "₹", "status": c["status"], "why": c.get("why", "")}
        if c["status"] == "closed":
            t.update(exit=c.get("spot_exit"), exit_reason=c.get("exit_reason"), closed=f"{c['day']} {c.get('closed', '')}", pnl=c.get("rs_net"),
                     pct=c.get("ret_pct"), r=round(c["spot_pts"] / c["R_pts"], 2) if c.get("R_pts") and c.get("spot_pts") is not None else None)
        elif c.get("prem_now") and c.get("prem_entry"):
            t.update(pnl_open=round((c["prem_now"] - c["prem_entry"]) * c.get("lot", 1)), now=c.get("prem_now"))
        out.append(t)
    return out


def _touchdom(hot):
    """2026-11-04: Touch + DOM combo v1 - touch-minute cards whose DOM book lean agreed at entry. Synthetic future, 1 lot, rupees."""
    TT = hot("touchtrades")
    import glob, os
    files = sorted(glob.glob(os.path.join(TT.CDIR, "cards_*.json")))[-30:]
    out = []
    for c in [c for f in files for c in TT.load_cards(os.path.basename(f)[6:16]) if c.get("combo")]:
        sd = 1 if c["side"] == "long" else -1
        t = {"rule": "touchdom", "key": c["key"], "side": c["side"], "opened": f"{c['day']} {c['time']}", "entry": c.get("spot_entry"),
             "stop": c.get("spot_sl"), "target": None, "how": f"stop on spot · {c.get('trail', '')} · out by {c.get('exit_by', '')}",
             "instrument": f"{NAME.get(c['key'], c['key'])} synthetic future (ATM call + short ATM put), 1 lot", "size": None, "ccy": "₹",
             "status": c["status"], "why": f"{c.get('why', '')} · book lean {c.get('dom_lean'):+.2f} vs normal"}
        if c["status"] == "closed":
            t.update(exit=c.get("spot_exit"), exit_reason=c.get("exit_reason"), closed=f"{c['day']} {c.get('closed', '')}", pnl=c.get("synth_rs_net"),
                     r=round(c["spot_pts"] / c["R_pts"], 2) if c.get("R_pts") and c.get("spot_pts") is not None else None)
        elif c.get("spot_now"):
            t.update(pnl_open=round((sd * (c["spot_now"] - c["spot_entry"]) - TT.SYN_PTS[c["key"]]) * c.get("lot", 1)), now=c.get("spot_now"))
        out.append(t)
    return out


def _dom(hot):
    """2026-11-03: DOM hold / break paper test (app/domcards.py). Rs = R after costs x Rs 1,000 risk.
    Only the plan account (first 3 trades a day, -2R day stop) is listed here; every DOM trade (~50-90 a day) is in the DOM Setups card."""
    D = hot("domcards")
    out = []
    for x in [t for t in (D.trades() or []) if t.get("plan")]:
        t = {"rule": "dom", "key": x["key"], "side": x["side"], "opened": x["opened"], "entry": x["entry"], "stop": x["stop"], "target": x["target"],
             "how": f"{x['setup']} · target {x['target_note']} · out after 60 min or 15:20",
             "instrument": NAME.get(x["key"], x["key"]) + (" synthetic future, 1 lot" if x["key"] in D.INDEX else " (stock)"),
             "size": None, "ccy": "₹", "risk_money": D.RISK_RS, "status": x["status"], "why": x.get("why", "")}
        if x["status"] == "closed":
            t.update(exit=x.get("exit"), exit_reason=x.get("exit_reason"), closed=x.get("closed"), pnl=x.get("pnl"), r=x.get("r"))
        out.append(t)
    return out


# 2026-11-04 (Nikhil): the "All paper trades" card follows the tab - group = india | crypto | us | metals | global
GROUPS = {"crypto": {"BTC", "ETH", "SOL", "BNB", "XRP", "UNI", "ZEC", "LINK", "DASH"}, "us": {"ES", "NQ", "US_STOCKS", "SPY", "QQQ"},
          "metals": {"GOLD", "SILVER", "COPPER", "ALUMINUM"}, "global": {"CRUDE", "NATGAS"}}


def group_of(key: str) -> str:
    for g, ks in GROUPS.items():
        if key in ks:
            return g
    return "india"                     # Nifty, Bank Nifty and NSE stocks


def api(hot, px, keys=None, group=None) -> dict:
    allt, errs = [], {}
    for nm, fn in (("b4h", lambda: _b4h(hot, px)), ("touch", lambda: _touch(hot)), ("dom", lambda: _dom(hot)), ("touchdom", lambda: _touchdom(hot))):
        try:
            allt += fn()
        except Exception as e:
            errs[nm] = str(e)[:120]
    if keys:
        allt = [t for t in allt if t["key"] in keys]
    if group:
        allt = [t for t in allt if group_of(t["key"]) == group]
    for t in allt:
        t["market"] = NAME.get(t["key"], t["key"]); t["rule_label"] = RULES[t["rule"]]["label"]; t["edge"] = RULES[t["rule"]]["edge"]
        if t.get("proven") is False:
            t["edge"] = "UNPROVEN - forward paper test (not backtested on this coin)"
    op = sorted([t for t in allt if t["status"] == "open"], key=lambda t: t["opened"], reverse=True)
    cl = sorted([t for t in allt if t["status"] == "closed"], key=lambda t: t.get("closed") or t["opened"], reverse=True)
    today = datetime.now().strftime("%Y-%m-%d")
    tot = {}
    for t in cl:
        k = (t["rule"], t["ccy"])
        z = tot.setdefault(k, {"rule": RULES[t["rule"]]["label"], "edge": RULES[t["rule"]]["edge"], "ccy": t["ccy"], "n": 0, "wins": 0, "pnl": 0.0, "today_n": 0, "today_pnl": 0.0})
        z["n"] += 1; z["wins"] += (t.get("pnl") or 0) > 0; z["pnl"] = round(z["pnl"] + (t.get("pnl") or 0), 2)
        if str(t.get("closed", ""))[:10] == today:
            z["today_n"] += 1; z["today_pnl"] = round(z["today_pnl"] + (t.get("pnl") or 0), 2)
    return {"open": op, "closed": cl[:60], "totals": list(tot.values()), "errors": errs,
            "note": "Paper trades only (never orders). 4H break $ = a $10,000 account risking 1% ($100) a trade. Touch-minute = 1 option lot in rupees "
                    "(premiums are estimates). DOM hold / break = index: synthetic future, 1 lot, rupees after costs; stocks: Rs 1,000 risked a trade, after costs. "
                    "Touch + DOM v1 = touch-minute signal with the DOM book agreeing, synthetic future, 1 lot, rupees after costs."}
