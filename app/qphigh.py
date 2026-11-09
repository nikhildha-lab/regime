"""CRYPTO 30-DAY HIGH - PAPER ONLY, RECORD-ONLY (2026-11-10, Nikhil: "get back to Quantpedia .. both, winners first").

Quantpedia idea "x-day high" re-tested the MASTER_BACKTESTER way (scripts/research/quantpedia_gauntlet.py, data_store/research/
quantpedia_gauntlet.txt): long while the DAILY close is the 30-day high, after 0.05 %/side.
   BTC 2019-26: Sharpe 0.89 (buy-and-hold 0.92) with a third of the drawdown (-33 % vs -77 %), in the market 12 % of days, train 1.02 / test 0.45 -> PASSES
   SOL 2020-26: Sharpe 1.23 (buy-and-hold 1.03), maxDD -49 %, 6 of 6 years positive, train 1.36 / test 0.86                    -> PASSES
   ETH 2019-26: Sharpe 0.86 (0.84), maxDD -25 %, train 0.96 / test 0.56, but only 4 of 8 years positive                          -> borderline, recorded with a flag
   (the other 12 coins have 2 years of data only - not judged)
Rule (fixed, as published):
   at each UTC daily close: if close >= the highest close of the last 30 sessions (this one included) -> be LONG for the next day
   exit = the first UTC close that is NOT a 30-day high (position closed at that close); no stop, no target - that is the tested rule;
   the daily re-check is the risk control (a position lasts one day unless the high keeps being made)
Size: Nikhil's crypto paper convention - 100 USDT margin at 5x = 500 USDT notional. Costs 0.05 % per side. Record only, no plan account.
Data: Binance USD-M daily klines (same source as app/break4h.py). State: data_store/livedesk/qphigh/state.json.
"""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timezone

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
OUT = os.path.join(ROOT, "data_store", "livedesk", "qphigh")
COINS = {"BTC": "passes", "SOL": "passes", "ETH": "borderline (4 of 8 years)"}
X, NOTIONAL, COST = 30, 500.0, 0.05
S: dict = {"t": 0, "busy": False, "err": None}


def _load():
    try:
        return json.load(open(os.path.join(OUT, "state.json")))
    except Exception:
        return {"trades": [], "last_day": {}}


def _save(st):
    os.makedirs(OUT, exist_ok=True); p = os.path.join(OUT, "state.json"); tmp = p + ".tmp"
    json.dump(st, open(tmp, "w"), indent=1, default=str); os.replace(tmp, p)


def _daily(coin):
    import break4h as B
    d = B._klines(f"{coin}USDT", "1d", 60)
    return d


def _work():
    try:
        st = _load(); now = datetime.now(timezone.utc)
        for coin in COINS:
            d = _daily(coin)
            if d is None or len(d) < X + 2:
                continue
            # the last row is the UNFINISHED UTC day; the one before is the latest closed session
            done = d.iloc[:-1]; last = done.iloc[-1]; day = str(done.index[-1])[:10]
            if st["last_day"].get(coin) == day:
                pass                                                     # this close already handled
            else:
                is_high = float(last.close) >= float(done.close.tail(X).max())
                open_t = next((t for t in st["trades"] if t["coin"] == coin and t["status"] == "open"), None)
                if open_t:
                    if is_high:
                        open_t["days"] = open_t.get("days", 1) + 1
                    else:                                                # exit at this close
                        px = float(last.close); pct = (px / open_t["entry"] - 1) * 100 - 2 * COST
                        open_t.update(status="closed", exit=px, closed=f"{day} 23:59", exit_reason="close not a 30-day high", pnl_pct=round(pct, 3),
                                      pnl=round(NOTIONAL * pct / 100, 2), r=None)
                elif is_high:
                    px = float(last.close)
                    st["trades"].append({"coin": coin, "status": "open", "entry": px, "opened": f"{day} 23:59", "signal_day": day, "days": 1,
                                         "why": f"{day} UTC close {px:.6g} is the highest close of the last {X} sessions", "verdict": COINS[coin]})
                st["last_day"][coin] = day
            # mark the open position to the live (unfinished-day) price
            for t in st["trades"]:
                if t["coin"] == coin and t["status"] == "open":
                    lp = float(d.close.iloc[-1]); t["now"] = lp; t["pnl_open_pct"] = round((lp / t["entry"] - 1) * 100 - COST, 3); t["pnl_open"] = round(NOTIONAL * t["pnl_open_pct"] / 100, 2)
        st["at"] = now.isoformat(timespec="seconds"); _save(st); S["err"] = None
    except Exception as e:
        S["err"] = f"{type(e).__name__}: {str(e)[:160]}"
    finally:
        S["busy"] = False


def tick(force=False):
    if S["busy"] or (not force and time.time() - S["t"] < 300):
        return False
    S["t"] = time.time(); S["busy"] = True
    threading.Thread(target=_work, name="qphigh", daemon=True).start()
    return True


def trades():
    return [t for t in _load().get("trades", []) if t.get("status") in ("open", "closed")]


def api():
    st = _load()
    return {"name": "Crypto 30-Day High", "record_only": True, "coins": COINS, "open": [t for t in st.get("trades", []) if t["status"] == "open"],
            "closed": [t for t in st.get("trades", []) if t["status"] == "closed"][-30:], "last_day": st.get("last_day"), "err": S.get("err"), "at": st.get("at"),
            "rule": f"Long at the UTC close when it is the highest close of the last {X} sessions; out at the first close that is not. No stop or target (the tested rule). 500 USDT notional. Record only."}
