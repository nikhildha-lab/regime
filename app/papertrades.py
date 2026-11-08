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
        "SOL": "SOL", "SILVER": "Silver", "COPPER": "Copper", "ALUMINUM": "Aluminium", "CRUDE": "WTI crude", "NATGAS": "Natural gas", "XRP": "XRP", "BNB": "BNB", "LINK": "LINK", "UNI": "UNI", "ZEC": "ZEC", "DASH": "DASH", "ES": "S&P 500 (ES)", "NQ": "Nasdaq 100 (NQ)", "SPY": "SPY"}   # 2026-11-04: 4H break forward test on more coins
# 2026-11-06 (Nikhil: "retire and fully remove"): retired + removed - touch (option buying), touchdom, sbt, dom (mixed plan account), ovn plain + h20,
# vcy_day_w, vcy_night_ts, vcy_night_tx. Their past trades: _archive/2026-11-03/backups/2026-11-06_retire/retired_trades.csv
RULES = {
    "b4h": {"label": "4H break", "edge": "tested edge (backtest 2023-26)"},
    "b4h_v4": {"label": "4H break · v4 (ETH / BTC record)", "edge": "new - picked on 2023-26 after a grid; judge on live trades"},
    "b4h_h2": {"label": "4H break · BTC H2 (half at 1R + trail)", "edge": "new - record beside BTC targets; history +0.32 % vs +0.28 % a trade"},
    "b4h_g2": {"label": "4H break · Gold v2 (targets)", "edge": "new - record beside Gold v1; history 32 trades, too few"},
    "dom_ix": {"label": "DOM Index (Nifty, Bank Nifty · synthetic)", "edge": "lead - +0.66R a trade over the first 3 sessions; judge after 30-40"},
    "dom_stocks": {"label": "DOM Stocks (record only)", "edge": "loss-making (-0.18R a trade, 312 trades) - needs new logic, not traded"},
    "touchv2": {"label": "Touch-minute v2 (gap filter, synthetic, Nifty only from 6 Nov)", "edge": "new 5 Nov - UNPROVEN: in the live records (26 Oct-5 Nov) only 5 Nov had a 1 %+ spot gap, the day the idea came from; forward test only"},
    "ovn_h30": {"label": "Overnight hold · synthetic + 0.3-delta hedge", "edge": "new 6 Nov - record beside plain; recent 12 nights -Rs 40,010"},
    "ovn_h30f": {"label": "Overnight hold · h30 + order-flow filter (last-hour CVD agrees)", "edge": "new 7 Nov - same trade as h30, taken only when the last hour's futures CVD points the same way; forward test only (no CVD history to backtest)"},
    "vcy_night": {"label": "Vol cycle · night: buy ATM straddle (15:25 -> next 09:45)", "edge": "new 6 Nov - recorded chain 5 Oct-5 Nov: 13 nights +26.7 pts/unit gross, net +Rs 1,458/lot, halves +14 / +37 (one trending month) - paper test"},
    "vcy_day": {"label": "Vol cycle · day: sell ATM straddle (10:30 -> 15:25)", "edge": "new 6 Nov - recorded chain: 14 days +10.3 pts/unit gross, net +Rs 399/lot, halves +8 / +13 - paper test"},
    "vcy_day_sk": {"label": "Vol cycle · day skew: credit spread on the side away from the 09:15-10:30 trend (0.3 %)", "edge": "record only 6 Nov - replay 14 days +25.0 pts vs +10.3 straddle, but from 3 down-trend days; fades at 0.4-0.5 %"},
    "vcy_day_bw": {"label": "Vol cycle · day broken-wing fly (1 % trend side / 3 % other)", "edge": "record only 6 Nov - replay 14 days +19.2 pts, net +Rs 736/lot; fades at 0.5 %"},
    "vcy_day_dh": {"label": "Vol cycle · day straddle, delta-hedged (synthetic, band ±1 lot on a 4-lot book)", "edge": "record only 6 Nov - replay 14 days +12.5 pts vs +10.3 plain, worst day -33.7 vs -52.4, net +Rs 493/lot, 1.4 hedges/day; band picked from 4 tried"},
    "us_desk": {"label": "US desk card (ES / NQ)", "edge": "new 7 Nov - the US desk agent's registered directional calls played on 5-min futures bars; record only, judge after 30+"},
    "us_manual": {"label": "US paper trade (your form)", "edge": "your own US paper trade from the dashboard form - record only"},
    "svb": {"label": "Stock Volume Break v1 · target 2R (MIS 5x)", "edge": "new 9 Nov - backtest 81 days: 149 trades, +0.23 % a trade after costs, halves +0.21 / +0.25; picked from a grid - judge after 30-40"},
    "svb_tr": {"label": "Stock Volume Break v1 · 2-ATR trail (MIS 5x)", "edge": "new 9 Nov - same entries, trail 2 ATR after +1R, target 3R: backtest +0.19 % a trade, win 53 %"},
    "fsb": {"label": "F&O Short Build-up v1 (stock futures, 1 lot)", "edge": "new 9 Nov - bhavcopy test Jul 2024-Nov 2026: 596 trades, +0.72 % a trade after costs (t 3.6), every year positive; judge after ~40"},
    "sorb": {"label": "Stocks in Play ORB (MIS 5x)", "edge": "small lead - history 61 trades, +0.17 % gross / +0.05 % net a trade, halves -0.04 / +0.13; paper from 5 Nov"},
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
        if r.get("rule") == "v1" and r["mkt"] not in ("GOLD", "SILVER", "COPPER", "ALUMINUM", "CRUDE", "NATGAS"):
            continue
        if r["mkt"] in ("BNB", "LINK", "UNI", "XRP"):          # 2026-11-06 (Nikhil: "retire and fully remove"): 4H break on these coins retired (negative 90-day check)
            continue
        gold_v1_off = r["mkt"] == "GOLD" and r.get("rule") == "v1"   # Gold v1 (2-day exit) retired; Gold v2 (targets) is read from the same row
        sd = r["side"] if isinstance(r["side"], int) else (1 if r["side"] == "long" else -1)
        risk = r.get("risk") or 0
        units = 100.0 / risk if risk else 0
        tg = r.get("targets")
        t = {"rule": "b4h", "key": r["mkt"], "side": "long" if sd > 0 else "short", "opened": _ist(pd.Timestamp(r["bar"]) + timedelta(hours=4)),
             "entry": r["entry"], "stop": r.get("stop"), "target": (tg[-1] if tg else None), "targets": tg,
             "how": ("3 targets 1R / 2R / 3R" if tg else f"trail {r.get('trail_atr', 1.5)} ATR after +1R" if r.get("exit_mode") == "trail" else "out after 2 days")
                    + f" · out by {_ist(r['exit_by'])[5:]} IST" if r.get("exit_by") else "",
             "instrument": "Gold" if r["mkt"] == "GOLD" else f"{NAME[r['mkt']]} futures" if r.get("hist") else f"{NAME[r['mkt']]} perpetual", "size": round(units * r["entry"]), "ccy": "$",
             "risk_money": 100, "status": r["status"], "why": r.get("dir", ""), "proven": r.get("proven"), "hist": r.get("hist"), "_rk": risk}
        # 2026-11-05 (Nikhil: "4h break doesn't have a target and trailing SL?"): display only - show the CURRENT stop (it moves with the
        # trail / targets) and say when trailing starts. Exits themselves are unchanged (break4h.py).
        if r["status"] == "open":
            cur = r.get("cur_stop") or r.get("stop"); t["stop"] = cur; t["stop0"] = r.get("stop")
            if r.get("exit_mode") == "h3":                  # 2026-11-05 SOL: half at 2R + trail
                t["target"] = round(r["entry"] + sd * 2 * risk, 4)
                t["how"] = (f"book HALF at 2R ({t['target']:,.2f}){' - done' if r.get('hit') else ''} · other half trails 1.5 ATR from +1R "
                            f"({r['entry'] + sd * risk:,.2f}){', ON - stop ' + format(cur, ',.2f') if r.get('trailing') else ''} · stop never below entry after +1R"
                            + (f" · out by {_ist(r['exit_by'])[5:]} IST" if r.get("exit_by") else ""))
            elif r.get("exit_mode") == "trail":
                t["how"] = (f"no fixed target · trailing {r.get('trail_atr', 1.5)} ATR now ON, stop moved to {cur:,.2f}" if r.get("trailing") else
                            f"no fixed target · trailing {r.get('trail_atr', 1.5)} ATR starts at +1R ({r['entry'] + sd * risk:,.2f})") + (f" · out by {_ist(r['exit_by'])[5:]} IST" if r.get("exit_by") else "")
            elif tg:
                t["how"] = (f"3 targets {' / '.join(f'{x:,.0f}' for x in tg)} (a third each) · stop to breakeven after T1, to T1 after T2"
                            + (f" · {r.get('hit')} hit, stop now {cur:,.2f}" if r.get("hit") else "") + (f" · out by {_ist(r['exit_by'])[5:]} IST" if r.get("exit_by") else ""))
            elif r["mkt"] == "GOLD":
                t["how"] = "no target, no trailing stop (tested that way) · block stop · out after 2 days" + (f" · out by {_ist(r['exit_by'])[5:]} IST" if r.get("exit_by") else "")
        if r["status"] == "closed":
            t.update(exit=r.get("exit"), exit_reason=r.get("exit_reason"), closed=_ist(pd.Timestamp(r.get("closed_bar")) + timedelta(hours=4)) if r.get("closed_bar") else "", r=r.get("R_net"),
                     pnl=round((r.get("R_net") or 0) * 100, 2), pct=r.get("bps_net") and round(r["bps_net"] / 100, 2), bps_net=r.get("bps_net"))
        elif px.get(r["mkt"]) and units:
            t.update(now=px[r["mkt"]], pnl_open=round(units * sd * (px[r["mkt"]] - r["entry"]), 2))
        if not gold_v1_off:
            out.append(t)
        v4 = r.get("v4")
        if isinstance(v4, dict):                            # 2026-11-05: ETH / BTC v4 shown as its own paper trade (record-only)
            B4 = hot("break4h"); c4 = B4.V4[r["mkt"]]
            w = {**t, "rule": "b4h_v4", "stop": v4.get("cur_stop") or v4.get("stop"), "targets": None, "status": v4["status"], "_rk": v4.get("risk") or risk,
                 "target": round(r["entry"] + sd * v4["risk"], 2) if r["mkt"] == "BTC" else None,
                 "how": (f"{c4['label']} · " + (f"book HALF at 1R ({r['entry'] + sd * v4['risk']:,.0f}), stop to entry, rest trails 1.5 ATR · out after 2 days"
                         if r["mkt"] == "BTC" else "no fixed target · trail 1.5 ATR from +1R · out after 3 days"))}
            for k_ in ("exit", "exit_reason", "closed", "r", "pnl", "pct", "now", "pnl_open"):
                w.pop(k_, None)
            if v4["status"] == "closed":
                w.update(exit=v4.get("exit"), exit_reason=v4.get("exit_reason"), r=v4.get("R_net"), pnl=round((v4.get("R_net") or 0) * 100, 2), bps_net=v4.get("bps_net"),
                         closed=_ist(pd.Timestamp(v4["closed_bar"]) + timedelta(hours=4)) if v4.get("closed_bar") else "")
            elif px.get(r["mkt"]) and v4.get("risk"):
                u4 = 100.0 / v4["risk"]; booked = 0.5 * sd * v4["risk"] if v4.get("hit") else 0.0; left = 0.5 if v4.get("hit") else 1.0
                w.update(now=px[r["mkt"]], pnl_open=round(u4 * (booked + left * sd * (px[r["mkt"]] - r["entry"])), 2))
            out.append(w)
        hv = r.get("h2")
        if r["mkt"] == "BTC" and hv:                        # 2026-11-05: BTC H2 shown as its own paper trade (record-only)
            h2 = {**t, "rule": "b4h_h2", "target": round(r["entry"] + sd * risk, 2), "targets": None, "stop": hv.get("cur_stop") or r.get("stop"),
                  "how": f"book HALF at 1R ({r['entry'] + sd * risk:,.0f}), stop to entry · other half trails 1.5 ATR · out after 2 days"
                         + (" · half booked" if hv.get("hit") else ""), "status": hv["status"]}
            for k_ in ("exit", "exit_reason", "closed", "r", "pnl", "pct", "now", "pnl_open"):
                h2.pop(k_, None)
            if hv["status"] == "closed":
                h2.update(exit=hv.get("exit"), exit_reason=hv.get("exit_reason"), r=hv.get("R_net"), pnl=round((hv.get("R_net") or 0) * 100, 2), bps_net=hv.get("bps_net"),
                          closed=_ist(pd.Timestamp(hv["closed_bar"]) + timedelta(hours=4)) if hv.get("closed_bar") else "")
            elif px.get("BTC") and units:
                booked = 0.5 * sd * risk if hv.get("hit") else 0.0; left = 0.5 if hv.get("hit") else 1.0
                h2.update(now=px["BTC"], pnl_open=round(units * (booked + left * sd * (px["BTC"] - r["entry"])), 2))
            out.append(h2)
        gv = r.get("gv2")
        if r["mkt"] == "GOLD" and gv:                       # 2026-11-05: Gold v2 (targets) shown as its own paper trade
            g2 = {**t, "rule": "b4h_g2", "target": gv["targets"][-1], "targets": gv["targets"], "stop": gv.get("cur_stop") or r.get("stop"),
                  "how": f"3 targets {' / '.join(f'{x:,.1f}' for x in gv['targets'])} (a third each) · stop to breakeven after T1, to T1 after T2 · out after 2 days"
                         + (f" · {gv['hit']} target(s) hit" if gv.get("hit") else ""), "status": gv["status"]}
            for k_ in ("exit", "exit_reason", "closed", "r", "pnl", "pct", "now", "pnl_open"):
                g2.pop(k_, None)
            if gv["status"] == "closed":
                g2.update(exit=gv.get("exit"), exit_reason=gv.get("exit_reason"), r=gv.get("R_net"), pnl=round((gv.get("R_net") or 0) * 100, 2),
                          closed=_ist(pd.Timestamp(gv["closed_bar"]) + timedelta(hours=4)) if gv.get("closed_bar") else "")
            elif px.get("GOLD") and units:
                left = 1 - (gv.get("hit") or 0) / 3
                booked = sum((1 / 3) * sd * (x - r["entry"]) for x in gv["targets"][:gv.get("hit") or 0])
                g2.update(now=px["GOLD"], pnl_open=round(units * (booked + left * sd * (px["GOLD"] - r["entry"])), 2))
            out.append(g2)
    return [_usdt(t) for t in out]


# 2026-11-05 (Nikhil: "make currency trades now with 100 USDT per trade with 5x leverage for all crypto coins"): money view only -
# every crypto 4H-break paper trade (and its record versions) = 100 USDT margin x 5 = 500 USDT position. P&L = 500 x net move after the
# 0.14 % round-trip cost. Trade logic unchanged. Paper only - never orders. Gold / metals / commodities keep the $100-risk view.
MARGIN_USDT, LEVERAGE, MM = 100.0, 5, 0.005           # MM = maintenance margin used for the liquidation estimate (isolated)
NON_CRYPTO = {"GOLD", "SILVER", "COPPER", "ALUMINUM", "CRUDE", "NATGAS"}


def _usdt(t):
    rk = t.pop("_rk", None)
    if t["key"] in NON_CRYPTO or not t.get("entry"):
        return t
    e = float(t["entry"]); pos = MARGIN_USDT * LEVERAGE; sd = 1 if t["side"] == "long" else -1
    t.update(ccy="USDT", size=pos, margin=MARGIN_USDT, leverage=LEVERAGE, qty=round(pos / e, 6),
             liq=round(e * (1 - sd * (1 / LEVERAGE - MM)), 4), risk_money=round(pos * rk / e, 2) if rk else None)
    if t["status"] == "closed" and t.get("bps_net") is not None:
        t["pnl"] = round(pos * t["bps_net"] / 1e4, 2); t["pct_margin"] = round(100 * t["pnl"] / MARGIN_USDT, 1)
    elif t.get("pnl_open") is not None and rk:
        t["pnl_open"] = round(t["pnl_open"] * rk / 100 * pos / e, 2)       # pnl_open was in $ for $100 risk -> USDT on a 500 position
    lq = t["liq"]; lqs = f"{lq:,.0f}" if lq >= 1000 else f"{lq:,.2f}" if lq >= 1 else f"{lq:.5f}"
    t["how"] = f"{MARGIN_USDT:.0f} USDT x {LEVERAGE} = {pos:.0f} USDT · liq ~{lqs} · " + (t.get("how") or "")
    return t


BN_OFF_FROM = "2026-11-06"   # 2026-11-06 (Nikhil: "remove bank nifty from this strategy"): Bank Nifty touch-minute dropped - 2-year study
                             # (claude/banknifty-level-study.md) found no edge at Bank Nifty level touches. Its cards are still recorded
                             # by touchtrades.py (research), but from this day they are no longer listed as touch-minute paper trades.


def _bn_off(c):
    return c.get("key") == "BANKNIFTY" and str(c.get("day", "")) >= BN_OFF_FROM


_GAP: dict = {}


def _gap_pct(hot, key, day):
    """Opening gap % (spot open vs previous close): the live prep note first, then the Yahoo daily file. Cached per key/day."""
    if (key, day) in _GAP:
        return _GAP[(key, day)]
    g = None
    try:
        gp = hot("prep").gap(key, day)
        g = gp["gap_pct"] if gp else None
    except Exception:
        g = None
    if g is None:
        try:
            import os
            ys = {"NIFTY": "^NSEI", "BANKNIFTY": "^NSEBANK"}[key]
            y = pd.read_csv(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data_store", "market_data", "yahoo_daily_deep.csv"),
                            usecols=["date", "symbol", "open", "close"])
            y = y[y.symbol == ys].drop_duplicates("date", keep="last").sort_values("date")
            i = y.index[y.date.astype(str).str[:10] == day]
            if len(i):
                pos = y.index.get_loc(i[-1])
                g = round(100 * (y.open.iloc[pos] / y.close.iloc[pos - 1] - 1), 2) if pos > 0 else None
        except Exception:
            g = None
    if g is not None:
        _GAP[(key, day)] = g
    return g


def _touchv2(hot):
    """2026-11-05 (Nikhil: "update the strategy"): touch-minute v2 = the SAME cards, entry, stop, trail and exits as the live touch-minute
    rule, plus ONE filter: if the opening gap is 1 % or more, only trades in the gap direction (no shorts after a big gap up, no longs
    after a big gap down). Money: synthetic future (ATM call + short ATM put, next-week expiry), 1 lot, after 4.5 / 12.5 pts.
    The old touch-minute rule is unchanged."""
    TT = hot("touchtrades")
    out = []
    try:
        import glob, os
        files = sorted(glob.glob(os.path.join(TT.CDIR, "cards_*.json")))[-30:]
        cards = [c for f in files for c in TT.load_cards(os.path.basename(f)[6:16])]
    except Exception:
        cards = []
    for c in cards:
        if _bn_off(c):
            continue
        sd = 1 if c["side"] == "long" else -1
        g = _gap_pct(hot, c["key"], c["day"])
        if g is not None and abs(g) >= 1.0 and (1 if g > 0 else -1) != sd:
            continue                                           # against a big opening gap - v2 skips it
        lot = c.get("lot") or TT.LOT[c["key"]]
        t = {"rule": "touchv2", "key": c["key"], "side": c["side"], "opened": f"{c['day']} {c['time']}", "entry": c.get("spot_entry"),
             "stop": c.get("spot_sl"), "target": None,
             "how": f"gap {g:+.2f}% {'(big - gap direction only)' if g is not None and abs(g) >= 1 else '(normal)'} · stop on spot · +1R breakeven, +2R trail 1R · out by {c.get('exit_by', '')}"
                    if g is not None else f"gap unknown · stop on spot · out by {c.get('exit_by', '')}",
             "instrument": f"{NAME.get(c['key'], c['key'])} synthetic future (ATM call + short ATM put), 1 lot", "size": None, "ccy": "₹",
             "status": c["status"], "why": c.get("why", ""), "_src": ("touchsyn", c)}
        if c["status"] == "closed" and c.get("spot_pts") is not None:
            t.update(exit=c.get("spot_exit"), exit_reason=c.get("exit_reason"), closed=f"{c['day']} {c.get('closed', '')}",
                     pnl=round((c["spot_pts"] - TT.SYN_PTS[c["key"]]) * lot),
                     r=round(c["spot_pts"] / c["R_pts"], 2) if c.get("R_pts") else None)
        elif c.get("spot_now") and c.get("spot_entry"):
            t.update(pnl_open=round((sd * (c["spot_now"] - c["spot_entry"]) - TT.SYN_PTS[c["key"]]) * lot), now=c.get("spot_now"))
        out.append(t)
    return out


def _sorb(hot):
    """2026-11-05: Stocks in Play ORB (app/stockorb.py) - MIS 5x paper, Rs 20,000 margin x 5 = Rs 1 lakh position, rupees after 0.12 %."""
    SO = hot("stockorb")
    out = []
    for x in SO.trades():
        t = {"rule": "sorb", "key": x["sym"], "side": x["side"], "opened": x["opened"], "entry": x["entry"], "stop": x["stop"], "target": None,
             "how": "stop = opening-range other side or 1 ATR (closer) · half off at 1.5R, stop to entry · rest out on a 5-min close through VWAP · all flat 15:10",
             "instrument": f"{x['sym']} (NSE stock, MIS 5x: Rs {x.get('margin', 20000):,} margin, qty {x.get('qty')})", "size": x.get("qty"), "ccy": "₹",
             "status": x["status"], "why": x.get("why", "") + (f" · book lean {x['book_lean']:+.2f}" if x.get("book_lean") is not None else "")}
        if x["status"] == "closed":
            t.update(exit=x.get("exit"), exit_reason=x.get("exit_reason"), closed=x.get("closed"), pnl=x.get("pnl"), pct_margin=x.get("pnl_margin_pct"))
        elif x.get("pnl_open") is not None:
            t.update(pnl_open=x["pnl_open"], now=x.get("now"))
        out.append(t)
    return out


def _svb(hot):
    """2026-11-08: Stock Volume Break v1 (app/stockvb.py) - two exit versions of each signal, MIS 5x paper, Rs 1 lakh position, after 0.12 %."""
    SV = hot("stockvb"); out = []
    for x in SV.trades():
        ver = x.get("ver", "svb")
        how = ("stop 2 ATR(5m) · target 2R · flat 15:15" if ver == "svb" else "stop 2 ATR(5m) · trail 2 ATR once +1R · target 3R · flat 15:15")
        t = {"rule": ver, "key": x["sym"], "side": x["side"], "opened": x["opened"], "entry": x["entry"], "stop": x.get("stop"), "target": x.get("target"),
             "how": how, "instrument": f"{x['sym']} (NSE stock, MIS 5x: Rs {x.get('margin', 20000):,} margin, qty {x.get('qty')})", "size": x.get("qty"), "ccy": "₹",
             "status": x["status"], "why": x.get("why", "")}
        if x["status"] == "closed":
            t.update(exit=x.get("exit"), exit_reason=x.get("exit_reason"), closed=x.get("closed"), pnl=x.get("pnl"), pct_margin=x.get("pnl_margin_pct"), r=x.get("r"))
        elif x.get("pnl_open") is not None:
            t.update(pnl_open=x["pnl_open"], now=x.get("now"))
        out.append(t)
    return out


def _fsb(hot):
    """2026-11-08: F&O Short Build-up v1 (app/fnoshort.py) - short stock futures, 1 lot, rupees after 0.06 %."""
    out = []
    for x in hot("fnoshort").trades():
        t = {"rule": "fsb", "key": x["sym"], "side": "short", "opened": x.get("opened"), "entry": x.get("entry"), "stop": x.get("stop"), "target": None,
             "how": "stop 5 % above entry (day's high) · out at the close of the 5th session", "instrument": f"{x['sym']} futures {x.get('expiry','')}, 1 lot ({x.get('lot')})",
             "size": x.get("lot"), "ccy": "₹", "status": x["status"], "why": x.get("why", "")}
        if x["status"] == "closed":
            t.update(exit=x.get("exit"), exit_reason=x.get("exit_reason"), closed=x.get("closed"), pnl=x.get("pnl"))
        elif x.get("pnl_open") is not None:
            t.update(pnl_open=x["pnl_open"], now=x.get("now"))
        out.append(t)
    return out


def _ovn(hot):
    """2026-11-06: Overnight hold (app/overnight.py) - three versions of the same signal, each its own row, rupees per 1 lot after costs."""
    O = hot("overnight"); out = []
    for r in O.trades():
        if r.get("status") not in ("open", "closed"):
            continue
        for v, rule in (("h30", "ovn_h30"), ("h30_flow", "ovn_h30f")):      # plain + h20 retired 6 Nov; h30_flow added 7 Nov
            V = (r.get("versions") or {}).get(v)
            if not V:
                continue
            legs = " + ".join(f"{'buy' if lg['qty'] > 0 else 'sell'} {lg['strike']:.0f} {lg['cp']}" for lg in V["legs"].values())
            t = {"rule": rule, "key": r["key"], "side": r["side"], "opened": f"{r['day']} {O.AT}", "entry": r["spot"], "stop": None, "target": None,
                 "how": f"exit next trading day at the first option snapshot after 09:15 · net delta {V['net_delta']}"
                        + (f" · order flow at 15:12: CVD last hour {(r.get('flow') or {}).get('cvd60', 0):+,} ({(r.get('flow') or {}).get('cvd60_pct')}% of volume), day {(r.get('flow') or {}).get('cvd_day', 0):+,}, book lean {(r.get('flow') or {}).get('book_lean')} - {(r.get('flow') or {}).get('h30_flow', '')}" if r.get("flow") else ""),
                 "instrument": f"{NAME.get(r['key'], r['key'])} {r.get('expiry')} options, 1 lot: {legs}", "size": None, "ccy": "₹",
                 "status": r["status"], "why": r.get("why", ""), "_src": ("ovn", (r, v))}
            if r["status"] == "closed":
                t.update(exit=r.get("exit_spot"), exit_reason="next-day open", closed=V.get("exit_snap"), pnl=V.get("pnl"))
            out.append(t)
    return out


def _vcy_greeks(r, v):
    """Live greeks line for an open vol-cycle row (from volcycle._monitor), rupees for 1 lot."""
    m = (r.get("mon") or {}).get(v) or {}; n = m.get("now"); a = m.get("attr") or {}
    if r.get("status") != "open" or not n:
        return ""
    L = 65
    s = (f" · live {n['ts'][11:16]}: Δ {n['delta'] * L:+.0f} qty, Γ {n['gamma'] * L * 100:+.2f} Δ/100pt, Θ ₹{n['theta'] * L:+,.0f}/day, "
         f"Vega ₹{n['vega'] * L:+,.0f}/vol · P&L ₹{n['pnl_pts'] * L:+,.0f} = Δ {a.get('delta', 0) * L:+,.0f} Γ {a.get('gamma', 0) * L:+,.0f} "
         f"Θ {a.get('theta', 0) * L:+,.0f} Vega {a.get('vega', 0) * L:+,.0f}")
    h = (r.get("mon") or {}).get("hedge_now")
    if v == "dhedge" and h:
        last = ((r.get("mon") or {}).get("hedge_log") or [{}])[-1]
        s += f" · hedge: holding {h['held_lots']:+.0f} synthetic lot(s), book delta {h['book_delta_lots']:+.2f} lots" + (f", last: {last.get('action')} at {last.get('ts')}" if last else "")
    return s


def _vcy(hot):
    """2026-11-06: Nifty day / night vol cycle (app/volcycle.py) - night long straddle, day short straddle (naked + wings), each its own row."""
    V = hot("volcycle"); out = []
    for r in V.trades():
        if r.get("status") not in ("open", "closed"):
            continue
        for v, rule in ((("plain", "vcy_night"),) if r["rule"] == "night"          # tilt spread / tilt straddle / wings retired 6 Nov
                        else (("plain", "vcy_day"), ("skew", "vcy_day_sk"), ("bwing", "vcy_day_bw"), ("dhedge", "vcy_day_dh"))):
            X = (r.get("versions") or {}).get(v)
            if not X:
                continue
            legs = " + ".join(f"{'buy' if lg['qty'] > 0 else 'sell'} {lg['strike']:.0f} {lg['cp']}" for lg in X["legs"].values())
            ctx = r.get("context") or {}
            side = ("long" if r["rule"] == "night" else "short") if v in ("plain", "wings") else \
                   ("long" if ctx.get("signal") == "strong close" or ctx.get("trend") == "up" else "short" if ctx.get("signal") == "weak close" or ctx.get("trend") == "down" else "short")
            t = {"rule": rule, "key": r["key"], "side": side, "opened": f"{r['day']} {r['snap']}",
                 "entry": r["spot"], "stop": None, "target": None,
                 "how": ("exit next trading day at the first snapshot >= 09:45" if r["rule"] == "night" else "exit at the last snapshot <= 15:25 (no stop; worst move recorded)")
                        + f" · net delta {X['net_delta']}" + _vcy_greeks(r, v),
                 "instrument": f"{NAME.get(r['key'], r['key'])} {r.get('expiry')} options, 1 lot: {legs}", "size": None, "ccy": "₹",
                 "status": r["status"], "why": r.get("why", ""), "_src": ("vcy", (r, v))}
            if r["status"] == "closed":
                t.update(exit=r.get("exit_spot"), exit_reason="time exit", closed=r.get("closed"), pnl=X.get("pnl"))
            out.append(t)
    return out


def _us(hot):
    """2026-11-07 (Nikhil: "We haven't done for US market for paper trade"): US desk cards + the US paper-trade form (app/uspaper.py).
    Only filled trades (open / closed) are listed here; waiting / not filled / cancelled show on the US paper trades card."""
    return [t for t in hot("uspaper").rows() if t["status"] in ("open", "closed")]


def _dom_ix(hot):
    """2026-11-06 (Nikhil: "separate out stocks ... focus on Nifty, Bank Nifty"): EVERY DOM index trade (Nifty / Bank Nifty), synthetic
    future 1 lot, rupees after costs. "plan" = the index-only plan account (first 3 index trades a day, -2R day stop)."""
    D = hot("domcards")
    out = []
    try:
        lpx = D._live_px()
    except Exception:
        lpx = None
    for x in [t for t in D.plan_ix_fill(D.trades() or []) if t["key"] in D.INDEX and not t.get("late_levels")]:
        if x["status"] == "open" and lpx:
            x = D._with_live(x, lpx)
        t = {"rule": "dom_ix", "key": x["key"], "side": x["side"], "opened": x["opened"], "entry": x["entry"], "stop": x["stop"], "target": x["target"],
             "how": f"{x['setup']} · target {x['target_note']} · out after 60 min or 15:20" + (" · index plan account" if x.get("plan_ix") else ""),
             "instrument": NAME.get(x["key"], x["key"]) + " synthetic future, 1 lot", "plan": bool(x.get("plan_ix")),
             "size": None, "ccy": "₹", "status": x["status"], "why": x.get("why", ""), "_src": ("dom", x)}
        if x["status"] == "closed":
            t.update(exit=x.get("exit"), exit_reason=x.get("exit_reason"), closed=x.get("closed"), pnl=x.get("pnl"), r=x.get("r"))
        elif x.get("pnl_open") is not None:
            t.update(pnl_open=x["pnl_open"], now=x.get("now"))
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


def _add_legs(hot, show, allt):
    """2026-11-06 (Nikhil: show the instrument deployed - option buying / selling, synthetic with or without hedge, exact strikes
    with premiums, and check the premiums). Adds t["legs"] (structured) + t["legs_text"] (one line) to the rows shown. Never changes pnl."""
    try:
        LG = hot("legs")
    except Exception:
        LG = None
    for t in show:
        kind, src = t.get("_src") or (None, None)
        try:
            L = (LG.for_touch(src, synth=True) if kind == "touchsyn"
                 else LG.for_dom(src) if kind == "dom" else LG.for_ovn(*src) if kind == "ovn" else LG.for_vcy(*src) if kind == "vcy" else None) if LG else None
        except Exception as e:
            L = {"ok": False, "why": f"legs: {type(e).__name__}"}
        if L:
            t["legs"] = {k: v for k, v in L.items() if not k.startswith("_")}
            t["legs_text"] = LG.text(L)
        else:
            t["legs_text"] = t.get("instrument", "")
    _add_review(hot, show)
    for t in allt:
        t.pop("_src", None)


def _risk_pts(t):
    """First stop distance in price points, from the rule's own record (the shown stop may have moved to breakeven)."""
    kind, src = t.get("_src") or (None, None)
    try:
        if kind == "touchsyn":
            return src.get("R_pts")
        if kind == "dom":
            return src.get("risk")
    except Exception:
        pass
    if t.get("_rk"):
        return t["_rk"]
    if t.get("stop0") is not None and t.get("entry") is not None:
        return abs(t["entry"] - t["stop0"]) or None
    return None


def _add_review(hot, show):
    """2026-11-06 (Nikhil: same columns as DOM Setups for All Paper Trades): t["review"] = right / wrong / improve from the trade's
    own price path (app/tradereview.py). Ideas only - rules unchanged."""
    try:
        TR = hot("tradereview")
    except Exception:
        return
    for t in show:
        if t.get("status") != "closed":
            continue
        try:
            if t.get("rule") in ("dom_ix", "dom_stocks") and (t.get("_src") or (None, None))[0] == "dom":
                r = hot("domcards")._rev_cached(t["_src"][1])        # DOM trades: the DOM card's own review
            else:
                r = TR.cached(t, _risk_pts(t))
        except Exception as e:
            r = {"right": "", "wrong": "", "improve": "-", "err": str(e)[:80]}
        if r:
            t["review"] = {k: v for k, v in r.items() if not str(k).startswith("_")}


def all_rows(hot, px) -> list:
    """2026-11-06: EVERY paper trade of every rule and market (all tabs), with legs + review - for the single CSV log (app/tradelog.py).
    Same rows as api() before any filter, plus every DOM trade (api() lists only the plan account)."""
    allt, errs = [], {}
    for nm, fn in (("b4h", lambda: _b4h(hot, px)), ("touchv2", lambda: _touchv2(hot)),
                   ("sorb", lambda: _sorb(hot)), ("svb", lambda: _svb(hot)), ("fsb", lambda: _fsb(hot)), ("ovn", lambda: _ovn(hot)), ("vcy", lambda: _vcy(hot)), ("us", lambda: _us(hot))):
        try:
            allt += fn()
        except Exception as e:
            errs[nm] = str(e)[:120]
    try:
        D = hot("domcards")
        for x in D.plan_ix_fill(D._load() or []):
            ix = x["key"] in D.INDEX
            t = {"rule": "dom_ix" if ix else "dom_stocks", "key": x["key"], "side": x["side"], "opened": x["opened"], "entry": x["entry"], "stop": x["stop"], "target": x["target"],
                 "how": f"{x['setup']} · target {x.get('target_note', '')} · out after 60 min or 15:20", "plan": bool(x.get("plan_ix") if ix else x.get("plan")),
                 "instrument": NAME.get(x["key"], x["key"]) + (" synthetic future, 1 lot" if x["key"] in D.INDEX else " (stock)"),
                 "ccy": "₹", "status": x["status"], "why": x.get("why", ""), "_src": ("dom", x)}
            if x["status"] == "closed":
                t.update(exit=x.get("exit"), exit_reason=x.get("exit_reason"), closed=x.get("closed"), pnl=x.get("pnl"), r=x.get("r"))
            allt.append(t)
    except Exception as e:
        errs["dom"] = str(e)[:120]
    for t in allt:
        if t["rule"] in RULES:
            t["market"] = NAME.get(t["key"], t["key"]); t["rule_label"] = RULES[t["rule"]]["label"]; t["group"] = group_of(t["key"])
    _add_legs(hot, allt, allt)
    return allt, errs


def api(hot, px, keys=None, group=None) -> dict:
    allt, errs = [], {}
    for nm, fn in (("b4h", lambda: _b4h(hot, px)), ("touchv2", lambda: _touchv2(hot)), ("dom_ix", lambda: _dom_ix(hot)), ("sorb", lambda: _sorb(hot)), ("svb", lambda: _svb(hot)), ("fsb", lambda: _fsb(hot)), ("ovn", lambda: _ovn(hot)), ("vcy", lambda: _vcy(hot)), ("us", lambda: _us(hot))):
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
            t["edge"] = ("forward paper test - " + t["hist"]) if t.get("hist") else "UNPROVEN - forward paper test (not backtested on this coin)"
    op = sorted([t for t in allt if t["status"] == "open"], key=lambda t: t["opened"], reverse=True)
    cl = sorted([t for t in allt if t["status"] == "closed"], key=lambda t: t.get("closed") or t["opened"], reverse=True)
    _add_legs(hot, op + cl[:60], allt)                    # 2026-11-06: exact strikes + premiums per trade (app/legs.py, read-only)
    today = datetime.now().strftime("%Y-%m-%d")
    tot = {}
    for t in cl:
        k = (t["rule"], t["ccy"])
        z = tot.setdefault(k, {"rule": RULES[t["rule"]]["label"], "edge": RULES[t["rule"]]["edge"], "ccy": t["ccy"], "n": 0, "wins": 0, "pnl": 0.0, "today_n": 0, "today_pnl": 0.0})
        z["n"] += 1; z["wins"] += (t.get("pnl") or 0) > 0; z["pnl"] = round(z["pnl"] + (t.get("pnl") or 0), 2)
        if str(t.get("closed", ""))[:10] == today:
            z["today_n"] += 1; z["today_pnl"] = round(z["today_pnl"] + (t.get("pnl") or 0), 2)
    return {"open": op, "closed": cl[:60], "totals": list(tot.values()), "errors": errs,
            "note": "Paper trades only (never orders). 4H break crypto = 100 USDT margin x 5 leverage = 500 USDT a trade, P&L after 0.14 % costs; Gold / metals / commodities $ = a $10,000 account risking 1% ($100) a trade. Touch-minute = 1 option lot in rupees "
                    "(premiums are estimates). DOM hold / break = index: synthetic future, 1 lot, rupees after costs; stocks: Rs 1,000 risked a trade, after costs. "
                    "Touch + DOM v1 = touch-minute signal with the DOM book agreeing, synthetic future, 1 lot, rupees after costs. "
                    "US desk / US form = $10,000 account risking 1% ($100) a trade on ES / NQ / SPY 5-min bars, after ES 0.5 pt / NQ 2 pt / SPY $0.04 costs. "
                    "Strong Block Touch v2 = strong block (evening-before map) + touch-minute confirm, synthetic future, 1 lot, rupees after costs."}
