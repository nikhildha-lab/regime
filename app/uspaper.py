"""US MARKET PAPER TRADES (2026-11-07, Nikhil: "We haven't done for US market for paper trade" + "Form on the dashboard").

Two sources, one record, same fields as every other paper trade (app/papertrades.py):
  * us_desk   - every DIRECTIONAL call the US desk agent registers (data_store/agent/logs/agent_calls.csv, persona "us").
                Its entry zone, stop, target and time stop, played on ES / NQ futures 5-minute bars (levels are on ES / NQ).
  * us_manual - trades Nikhil types into the "US paper trades" form on the dashboard (stored in data_store/paper/us_paper.json).
Results are re-computed from the bars every time (nothing to drift):
  - Fill: the first 5-min bar that trades into the entry zone, inside the fill window (2 US sessions for desk calls,
    the chosen days for a form trade). Price = bar open clipped into the zone. Blank entry = next bar's open (market).
    Not filled in the window -> "not filled" (kept, not counted).
  - Exit: stop or target on a 5-min bar (both in one bar -> stop, the cautious read), else the time stop (close of the last bar).
  - Money: a $10,000 account risking 1% ($100) a trade, like the other $ paper rules: $ = R after costs x 100.
    Costs round trip: ES 0.5 pt, NQ 2 pt, SPY $0.04 a share. Size shown as micro futures (MES $5/pt, MNQ $2/pt) or SPY shares.
Prices: Yahoo 5-min bars from synaptic_intraday_all.csv (updated by app/updater.py; can lag ~15-30 min). Paper only - never orders.
"""
from __future__ import annotations

import json
import math
import os
from datetime import datetime, timedelta

import pandas as pd

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
BARS_CSV = os.path.join(ROOT, "data_store", "market_data", "synaptic_intraday_all.csv")
CALLS_CSV = os.path.join(ROOT, "data_store", "agent", "logs", "agent_calls.csv")
STORE = os.path.join(ROOT, "data_store", "paper", "us_paper.json")

MKTS = {"ES": "ES=F", "NQ": "NQ=F", "SPY": "SPY"}
COST = {"ES": 0.5, "NQ": 2.0, "SPY": 0.04}                    # round trip, price units
MICRO = {"ES": ("MES", 5.0), "NQ": ("MNQ", 2.0)}             # micro future, $ per point
RISK_USD = 100.0
DESK_FILL_DAYS = 2
_C: dict = {}


def _now_ist() -> datetime:
    return datetime.utcnow() + timedelta(hours=5, minutes=30)


# ---------- data ----------
def bars(mkt: str) -> pd.DataFrame:
    """5-min OHLC in IST (index = bar start), cached by file mtime."""
    mt = os.path.getmtime(BARS_CSV)
    if _C.get("mt") != mt:
        d = pd.read_csv(BARS_CSV, usecols=["datetime_utc", "symbol", "interval", "open", "high", "low", "close"])
        d = d[(d["interval"] == "5m") & d["symbol"].isin(MKTS.values())].copy()
        d["t"] = pd.to_datetime(d["datetime_utc"].astype(str).str[:19]) + pd.Timedelta(hours=5, minutes=30)
        _C.clear(); _C["mt"] = mt
        for k, s in MKTS.items():
            _C[k] = (d[d["symbol"] == s].drop_duplicates("t", keep="last").set_index("t").sort_index()
                     [["open", "high", "low", "close"]].astype(float))
    return _C[mkt]


def last_px(mkt: str):
    b = bars(mkt)
    return (float(b["close"].iloc[-1]), b.index[-1].strftime("%Y-%m-%d %H:%M")) if len(b) else (None, None)


def _load() -> dict:
    try:
        with open(STORE) as f:
            return json.load(f)
    except Exception:
        return {"manual": []}


def _save(S: dict):
    os.makedirs(os.path.dirname(STORE), exist_ok=True)
    tmp = STORE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(S, f, indent=1)
    os.replace(tmp, STORE)


def _f(x):
    try:
        v = float(x)
        return v if math.isfinite(v) else None
    except Exception:
        return None


# ---------- the trade engine (same for both sources) ----------
def play(spec: dict) -> dict:
    """spec: mkt, side, lo, hi (None = market), stop, target, at (IST 'YYYY-MM-DD HH:MM'), fill_days, hold_days, closed_by_hand."""
    mkt, sd = spec["mkt"], (1 if spec["side"] == "long" else -1)
    b = bars(mkt)
    t0 = pd.Timestamp(spec["at"])
    after = b[b.index > t0]
    fill_by = t0 + pd.offsets.BDay(spec["fill_days"])
    out = {"status": "waiting", "entry": None, "opened": None}
    lo, hi = spec.get("lo"), spec.get("hi")
    k0 = None
    hand = spec.get("closed_by_hand")
    for i, (t, r) in enumerate(after.iterrows()):
        if t > fill_by:
            break
        if hand and t >= pd.Timestamp(hand["at"]):
            out["status"] = "cancelled"; out["exit_reason"] = "cancelled by hand before a fill"
            return out
        if lo is None:
            out["entry"], k0 = float(r["open"]), i
            break
        if r["low"] <= hi and r["high"] >= lo:
            out["entry"], k0 = float(min(max(r["open"], lo), hi)), i
            break
    if k0 is None:
        if hand:                                       # cancelled from the form before any bar reached the entry
            out["status"] = "cancelled"; out["exit_reason"] = "cancelled by hand before a fill"
            return out
        if len(after) and after.index[-1] > fill_by:
            out["status"] = "not_filled"
        return out
    t_fill = after.index[k0]
    out["opened"] = t_fill.strftime("%Y-%m-%d %H:%M")
    e, stop, tgt = out["entry"], spec["stop"], spec.get("target")
    risk = abs(e - stop)
    if risk <= 0 or (e - stop) * sd <= 0:            # filled beyond the stop (gap) - void, never a fake result
        out.update(status="void", exit_reason="filled past the stop")
        return out
    exit_by = t_fill + pd.offsets.BDay(spec["hold_days"])
    path = after.iloc[k0:]
    ex = None
    for t, r in path.iterrows():
        if hand and t >= pd.Timestamp(hand["at"]):
            ex = (hand["px"], "closed by hand", t); break
        if t > exit_by:
            break
        if (r["low"] <= stop) if sd > 0 else (r["high"] >= stop):
            ex = (stop, "stop", t); break
        if tgt is not None and ((r["high"] >= tgt) if sd > 0 else (r["low"] <= tgt)):
            ex = (tgt, "target", t); break
    seen = path[path.index <= exit_by]
    if ex is None and len(path) and path.index[-1] > exit_by and len(seen):
        ex = (float(seen["close"].iloc[-1]), "time stop", seen.index[-1])
    mfe = (seen["high"].max() - e) if sd > 0 else (e - seen["low"].min())
    mae = (e - seen["low"].min()) if sd > 0 else (seen["high"].max() - e)
    out.update(risk=risk, exit_by=exit_by.strftime("%Y-%m-%d %H:%M"), mfe_r=round(mfe / risk, 2), mae_r=round(mae / risk, 2))
    if ex:
        r_net = (sd * (ex[0] - e) - COST[mkt]) / risk
        out.update(status="closed", exit=round(float(ex[0]), 2), exit_reason=ex[1], closed=ex[2].strftime("%Y-%m-%d %H:%M"),
                   r=round(r_net, 2), pnl=round(r_net * RISK_USD, 2))
    else:
        now = float(path["close"].iloc[-1])
        out.update(status="open", now=now, now_at=path.index[-1].strftime("%Y-%m-%d %H:%M"),
                   pnl_open=round((sd * (now - e) - COST[mkt]) / risk * RISK_USD, 2))
    return out


def _size_text(mkt, risk):
    if not risk:
        return ""
    if mkt in MICRO:
        nm, pv = MICRO[mkt]
        n = int(RISK_USD // (risk * pv))
        return f"{n} {nm}" if n >= 1 else f"under 1 {nm} (1 {nm} risks ${risk * pv:,.0f})"
    return f"{int(RISK_USD // risk)} SPY shares"


# ---------- sources ----------
def _desk_specs() -> list:
    try:
        c = pd.read_csv(CALLS_CSV)
    except Exception:
        return []
    c = c[(c["persona"] == "us") & (c["kind"] == "directional")]
    out = []
    for _, r in c.iterrows():
        sym, basis = str(r["symbol"]).upper(), str(r.get("basis") or "")
        mkt = ("NQ" if sym in ("NDX", "NQ", "QQQ") else "ES") if (basis == "future" or sym in ("SP500", "ES", "NDX", "NQ")) else ("SPY" if sym == "SPY" else None)
        lo, hi, stop = _f(r["entry_lo"]), _f(r["entry_hi"]), _f(r["stop"])
        if not mkt or stop is None:
            continue
        hi = hi if hi is not None else lo
        n = int(str(r["horizon"])[:-1]) if str(r["horizon"])[-1:] == "d" else 1
        out.append({"id": r["call_id"], "src": "us_desk", "mkt": mkt, "side": r["direction"], "lo": lo, "hi": hi, "stop": stop,
                    "target": _f(r["target"]), "at": str(r["ts"])[:16].replace("T", " "), "fill_days": DESK_FILL_DAYS, "hold_days": n,
                    "why": str(r.get("why") or ""), "invalid_if": str(r.get("invalid_if") or ""), "labels": str(r.get("labels") or ""),
                    "desk_size": _f(r.get("size")), "sym": sym})
    return out


def _manual_specs() -> list:
    return [dict(m, src="us_manual") for m in _load().get("manual", []) if not m.get("void")]


def rows() -> list:
    """Every US paper trade (desk + form) in the papertrades row format. status: open | closed | waiting | not_filled | cancelled | void."""
    out = []
    for s in _desk_specs() + _manual_specs():
        try:
            res = play(s)
        except Exception as e:
            res = {"status": "error", "err": str(e)[:100]}
        mk = s["mkt"]
        zone = ("market" if s.get("lo") is None else f"{s['lo']:,.2f}" if s["lo"] == s["hi"] else f"{s['lo']:,.2f}-{s['hi']:,.2f}")
        t = {"rule": s["src"], "id": s["id"], "key": mk, "side": s["side"], "opened": res.get("opened") or s["at"],
             "entry": res.get("entry"), "stop": s["stop"], "target": s.get("target"), "ccy": "$", "risk_money": RISK_USD,
             "instrument": {"ES": "S&P 500 futures (ES)", "NQ": "Nasdaq 100 futures (NQ)", "SPY": "SPY (ETF)"}[mk]
                           + (f" · {_size_text(mk, res.get('risk'))}" if res.get("risk") else ""),
             "how": (f"entry {zone} · fill window {s['fill_days']} US session(s) from {s['at'][5:]} IST · time stop {s['hold_days']} session(s)"
                     + (f" (out by {res['exit_by'][5:]} IST)" if res.get("exit_by") else "") + " · stop first if stop and target in one 5-min bar"),
             "status": res["status"], "why": s.get("why") or s.get("note") or "", "registered": s["at"]}
        if s["src"] == "us_desk":
            t["call_id"] = s["id"]
            t["why"] = f"{s['id']} ({s['sym']}, desk size {s.get('desk_size')}x engine, labels {s.get('labels')}): {t['why']}"
        for k in ("exit", "exit_reason", "closed", "r", "pnl", "now", "pnl_open", "mfe_r", "mae_r"):
            if res.get(k) is not None:
                t[k] = res[k]
        if res.get("status") == "error":
            t["why"] += f" · error: {res.get('err')}"
        out.append(t)
    return out


# ---------- form actions (GET /api/livedesk/uspaper?act=...) ----------
def add(q: dict) -> dict:
    mkt = str(q.get("mkt", "")).upper()
    side = str(q.get("side", "")).lower()
    if mkt not in MKTS:
        return {"ok": False, "error": "market must be ES, NQ or SPY"}
    if side not in ("long", "short"):
        return {"ok": False, "error": "side must be long or short"}
    lo, hi, stop, tgt = _f(q.get("lo")), _f(q.get("hi")), _f(q.get("stop")), _f(q.get("target"))
    if hi is None:
        hi = lo
    if lo is not None and hi is not None and lo > hi:
        lo, hi = hi, lo
    sd = 1 if side == "long" else -1
    px, px_at = last_px(mkt)
    ref = lo if lo is not None else px
    if stop is None or ref is None:
        return {"ok": False, "error": "a stop is required (and a price must be available)"}
    ref_far = hi if (sd > 0 and hi is not None) else lo if lo is not None else px
    if (ref - stop) * sd <= 0 or (ref_far - stop) * sd <= 0:
        return {"ok": False, "error": f"stop must be {'below' if sd > 0 else 'above'} the entry"}
    if tgt is not None and (tgt - (hi if sd > 0 and hi is not None else ref)) * sd <= 0:
        return {"ok": False, "error": f"target must be {'above' if sd > 0 else 'below'} the entry"}
    if px and abs(ref - px) / px > 0.08:
        return {"ok": False, "error": f"entry is more than 8% from the last {mkt} price {px:,.2f} ({px_at} IST) - check the instrument"}
    try:
        days = max(1, min(10, int(q.get("days") or 1)))
    except Exception:
        days = 1
    now = _now_ist()
    S = _load()
    n = 1 + sum(1 for m in S.get("manual", []) if m["id"].startswith(f"USM-{now:%Y%m%d}"))
    rec = {"id": f"USM-{now:%Y%m%d-%H%M}-{n}", "mkt": mkt, "side": side, "lo": lo, "hi": hi, "stop": stop, "target": tgt,
           "at": now.strftime("%Y-%m-%d %H:%M"), "fill_days": days, "hold_days": days, "note": str(q.get("note") or "")[:300],
           "added": now.isoformat(timespec="seconds")}
    S.setdefault("manual", []).append(rec)
    _save(S)
    return {"ok": True, "id": rec["id"], "last_px": px, "last_px_at": px_at}


def close(q: dict) -> dict:
    """Close a form trade at the latest 5-min close."""
    S = _load()
    for m in S.get("manual", []):
        if m["id"] == q.get("id") and not m.get("void") and not m.get("closed_by_hand"):
            px, at = last_px(m["mkt"])
            if px is None:
                return {"ok": False, "error": "no price"}
            m["closed_by_hand"] = {"px": px, "at": at}
            _save(S)
            return {"ok": True, "id": m["id"], "px": px, "at": at}
    return {"ok": False, "error": "form trade not found or already closed (desk trades follow their own stop / target / time stop)"}


def void(q: dict) -> dict:
    """Mark a form trade as a typing mistake (kept in the file, never counted)."""
    S = _load()
    for m in S.get("manual", []):
        if m["id"] == q.get("id") and not m.get("void"):
            m["void"] = _now_ist().isoformat(timespec="seconds")
            _save(S)
            return {"ok": True, "id": m["id"]}
    return {"ok": False, "error": "form trade not found"}


def api(q: dict) -> dict:
    act = q.get("act", "list")
    if act == "add":
        return add(q)
    if act == "close":
        return close(q)
    if act == "void":
        return void(q)
    R = rows()
    px = {k: last_px(k) for k in MKTS}
    cl = [t for t in R if t["status"] == "closed"]
    tot = {}
    for t in cl:
        z = tot.setdefault(t["rule"], {"n": 0, "wins": 0, "pnl": 0.0, "r": 0.0})
        z["n"] += 1; z["wins"] += (t.get("pnl") or 0) > 0; z["pnl"] = round(z["pnl"] + t["pnl"], 2); z["r"] = round(z["r"] + t["r"], 2)
    return {"rows": R, "totals": tot, "px": {k: {"px": v[0], "at": v[1]} for k, v in px.items()},
            "counts": {s: sum(1 for t in R if t["status"] == s) for s in ("open", "closed", "waiting", "not_filled", "cancelled", "void")}}
