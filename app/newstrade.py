"""NEWS REACTION INTRADAY - PAPER ONLY (2026-11-10, Nikhil: "any bad or good news can turn into a trade ... intraday MIS
with 10k into 5x 50k margin long or short stock for the day, fixed sl, trail sl and target"). Never places orders.

  universe : the 48 F&O stocks the desk already prices (app/stockhist.STOCKS)
  trigger  : a headline in data_store/agent/news.json published in the last 15 minutes that names one of those stocks -
             every NSE corporate announcement (they carry the symbol), or any other India item of severity >= 2 whose
             title contains the symbol
  side     : the market decides, not the words. Compare the last closed 1-min price with the price 5 minutes before the
             headline: >= +0.25 % -> long, <= -0.25 % -> short, anything in between -> no trade (no reaction, no edge)
  entry    : the close of the next closed 1-min bar, 09:20-14:30 only
  money    : MIS 5x paper - Rs 10,000 margin x 5 = Rs 50,000 a position; quantity = 50,000 / entry
  stop     : fixed 1.0 % from entry
  target   : 2.0 % from entry
  trail    : at +1.0 % the stop moves to entry (breakeven); after that it trails 0.5 % behind the best price reached
  time stop: everything flat at 15:15
  limits   : max 3 trades a day, one trade per stock per day, never two positions in the same stock
  costs    : 0.12 % round trip (brokerage, charges and slippage, same as the other MIS paper rules)

NOT BACKTESTED. There is no archive of NSE announcements to test against, so this rule cannot be put through the
usual 70/30 split - it is record-only and judged on its own forward trades. Expect it to be hard: by the time an RSS
item or a filing reaches us the move is often over, which is exactly what the 0.25 % reaction filter is there to catch.
Judge after about 50 trades.
"""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import date, datetime, timedelta

import pandas as pd

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
OUT = os.path.join(ROOT, "data_store", "livedesk", "newstrade")
NEWS = os.path.join(ROOT, "data_store", "agent", "news.json")

MARGIN, LEV = 10000, 5
POS = MARGIN * LEV
SL_PCT, TGT_PCT = 1.0, 2.0
TRAIL_ARM, TRAIL_PCT = 1.0, 0.5
COST_PCT = 0.12
REACT_PCT = 0.25               # the move since the headline that counts as a reaction
FRESH_MIN = 15                 # a headline older than this is yesterday's news
MAX_DAY = 3
ENTRY_FROM, ENTRY_TO, FLAT_AT = "09:20", "14:30", "15:15"

S: dict = {"t": 0}


def _universe() -> list:
    try:
        import stockhist as SH
        return list(SH.STOCKS)[:48]
    except Exception:
        return ["RELIANCE", "HDFCBANK", "ICICIBANK", "INFY", "TCS", "SBIN", "AXISBANK", "LT", "BHARTIARTL", "ITC"]


def _p(day):
    return os.path.join(OUT, f"day_{day}.json")


def _load(day):
    try:
        return json.load(open(_p(day)))
    except Exception:
        return {"day": day, "trades": [], "seen": []}


def _save(day, st):
    os.makedirs(OUT, exist_ok=True)
    tmp = _p(day) + f".{os.getpid()}.tmp"
    json.dump(st, open(tmp, "w"), indent=1, default=str)
    os.replace(tmp, _p(day))


def _bars1(sym, day):
    """closed 1-min bars for today from the Mac's Dhan login"""
    import dhan_live as DL
    sid = DL.ids_equity(sym)
    if not sid:
        return pd.DataFrame()
    b = DL.intraday("NSE_EQ", sid, "EQUITY", date.fromisoformat(day), date.fromisoformat(day), 1, oi=False)
    if not b:
        return pd.DataFrame()
    d = pd.DataFrame(b)
    d["ts"] = pd.to_datetime(d.ts.astype(str).str[:19])
    d = d[d.ts.dt.strftime("%Y-%m-%d") == day]
    now_m = datetime.now().strftime("%H:%M")
    return d[d.ts.dt.strftime("%H:%M") < now_m].set_index("ts").sort_index()


def _headlines(day) -> list:
    """fresh India headlines that name one of our stocks -> [{sym, title, source, at, severity}]"""
    try:
        N = json.load(open(NEWS))
    except Exception:
        return []
    uni = _universe()
    cut = datetime.now() - timedelta(minutes=FRESH_MIN)
    out = []
    pools = (N.get("nse_announcements") or []) + (N.get("shock_items") or []) + (N.get("top") or [])
    for g in pools:
        try:
            ts = pd.to_datetime(str(g.get("published") or g.get("at") or "")[:19], errors="coerce")
            if ts is None or pd.isna(ts) or ts.to_pydatetime() < cut:
                continue
            sym = g.get("symbol")
            if sym not in uni:
                if int(g.get("severity") or 0) < 2 or (g.get("region") and g["region"] != "india"):
                    continue
                t = (g.get("title") or "").upper()
                sym = next((s for s in uni if s in t), None)
            if not sym:
                continue
            out.append({"sym": sym, "title": (g.get("title") or "")[:160], "source": g.get("source") or "",
                        "at": ts.strftime("%Y-%m-%d %H:%M"), "severity": int(g.get("severity") or 0),
                        "key": f"{sym}|{(g.get('title') or '')[:80]}"})
        except Exception:
            continue
    return out


def _manage(tr, b):
    """walk the bars after entry: stop, target, breakeven arm, trail, 15:15 flat"""
    after = b[b.index > pd.Timestamp(tr["bar"])]
    for t_, r in after.iterrows():
        hi, lo, c = float(r["high"]), float(r["low"]), float(r["close"])
        s, e = tr["s"], tr["entry"]
        best = max(tr.get("best", e), hi) if s > 0 else min(tr.get("best", e), lo)
        tr["best"] = round(best, 2)
        move = s * (best - e) / e * 100
        if move >= TRAIL_ARM:                                     # breakeven, then trail
            trail = best * (1 - TRAIL_PCT / 100) if s > 0 else best * (1 + TRAIL_PCT / 100)
            tr["stop"] = round(max(tr["stop"], e, trail) if s > 0 else min(tr["stop"], e, trail), 2)
            tr["trailing"] = True
        if (s > 0 and lo <= tr["stop"]) or (s < 0 and hi >= tr["stop"]):
            return _close(tr, tr["stop"], "trail stop" if tr.get("trailing") else "stop", t_)
        if (s > 0 and hi >= tr["target"]) or (s < 0 and lo <= tr["target"]):
            return _close(tr, tr["target"], "target", t_)
        if t_.strftime("%H:%M") >= FLAT_AT:
            return _close(tr, c, "15:15 flat", t_)
    return False


def _close(tr, px, why, t_):
    g = tr["s"] * (float(px) - tr["entry"]) / tr["entry"] * 100
    tr.update(status="closed", exit=round(float(px), 2), exit_reason=why, closed=str(t_)[:16],
              gross_pct=round(g, 3), net_pct=round(g - COST_PCT, 3),
              pnl=round(POS * (g - COST_PCT) / 100), pnl_margin_pct=round(LEV * (g - COST_PCT), 2))
    return True


def _work():
    try:
        _work_inner()
    except Exception as e:
        S["error"] = f"{type(e).__name__}: {e}"[:160]
    finally:
        S["busy"] = False


def _work_inner():
    now = datetime.now()
    day = now.strftime("%Y-%m-%d")
    st = _load(day)
    bars = {}

    for tr in st["trades"]:                                        # manage what is open first
        if tr.get("status") != "open":
            continue
        b = bars.get(tr["sym"])
        if b is None:
            b = bars[tr["sym"]] = _bars1(tr["sym"], day)
        if not b.empty:
            _manage(tr, b)

    hm = now.strftime("%H:%M")
    open_syms = {t["sym"] for t in st["trades"]}
    if ENTRY_FROM <= hm <= ENTRY_TO and len([t for t in st["trades"]]) < MAX_DAY:
        for h in _headlines(day):
            if h["key"] in st["seen"]:
                continue
            st["seen"].append(h["key"])
            if h["sym"] in open_syms or len(st["trades"]) >= MAX_DAY:
                continue
            b = bars.get(h["sym"])
            if b is None:
                b = bars[h["sym"]] = _bars1(h["sym"], day)
            if b.empty or len(b) < 6:
                continue
            t0 = pd.Timestamp(h["at"])
            before = b[b.index <= t0 - timedelta(minutes=5)]
            if before.empty:
                continue
            base = float(before["close"].iloc[-1])
            last = float(b["close"].iloc[-1])
            react = (last - base) / base * 100
            if abs(react) < REACT_PCT:
                st.setdefault("skipped", []).append({"sym": h["sym"], "at": h["at"], "react_pct": round(react, 2),
                                                     "why": "no clear reaction", "title": h["title"]})
                continue
            s = 1 if react > 0 else -1
            e = last
            tr = {"sym": h["sym"], "day": day, "s": s, "side": "long" if s > 0 else "short",
                  "bar": str(b.index[-1]), "opened": f"{day} {b.index[-1]:%H:%M}", "entry": round(e, 2),
                  "stop": round(e * (1 - SL_PCT / 100) if s > 0 else e * (1 + SL_PCT / 100), 2),
                  "target": round(e * (1 + TGT_PCT / 100) if s > 0 else e * (1 - TGT_PCT / 100), 2),
                  "qty": int(POS // e), "margin": MARGIN, "leverage": LEV, "status": "open", "best": round(e, 2),
                  "news": h["title"], "source": h["source"], "news_at": h["at"], "severity": h["severity"],
                  "react_pct": round(react, 2),
                  "why": f"{h['source']}: {h['title'][:90]} - price moved {react:+.2f}% in the 5 min after it"}
            st["trades"].append(tr)
            open_syms.add(h["sym"])
            b2 = bars[h["sym"]]
            _manage(tr, b2)

    st["at"] = now.isoformat(timespec="seconds")
    _save(day, st)


def tick(force=False) -> bool:
    """called from the live-desk loop; the work runs in its own thread (it makes Dhan data calls)"""
    now = datetime.now()
    hm = now.strftime("%H:%M")
    if S.get("busy") or (not force and (time.time() - S["t"] < 60 or now.weekday() >= 5 or not (ENTRY_FROM <= hm <= "15:16"))):
        return False
    try:
        import nsecal as NSEC
        if not force and not NSEC.session_ok():
            return False
    except Exception:
        pass
    S["t"] = time.time()
    S["busy"] = True
    threading.Thread(target=_work, name="newstrade", daemon=True).start()
    return True


def trades(days=40):
    files = sorted(f for f in os.listdir(OUT) if f.startswith("day_") and f.endswith(".json")) if os.path.isdir(OUT) else []
    out = []
    for f in files[-days:]:
        try:
            out += json.load(open(os.path.join(OUT, f))).get("trades", [])
        except Exception:
            pass
    return out


def api(days=40) -> dict:
    ts = trades(days)
    done = [t for t in ts if t.get("status") == "closed"]
    wins = [t for t in done if (t.get("net_pct") or 0) > 0]
    return {"rule": "News Reaction Intraday · record only, not backtested",
            "money": f"Rs {MARGIN:,} margin x {LEV} = Rs {POS:,} a position, MIS, flat by {FLAT_AT}",
            "exits": f"stop {SL_PCT}% · target {TGT_PCT}% · breakeven at +{TRAIL_ARM}% then trail {TRAIL_PCT}% · flat {FLAT_AT}",
            "trades": ts[-60:], "open": [t for t in ts if t.get("status") == "open"],
            "n": len(done), "wins": len(wins),
            "hit_pct": round(100 * len(wins) / len(done), 1) if done else None,
            "avg_net_pct": round(sum(t.get("net_pct") or 0 for t in done) / len(done), 3) if done else None,
            "pnl": round(sum(t.get("pnl") or 0 for t in done)) if done else 0,
            "error": S.get("error"), "at": S.get("t")}
