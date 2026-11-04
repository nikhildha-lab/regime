#!/usr/bin/env python3
"""
Synaptic regime MCP (2026-10-17) - THIN read-only layer over regime score v2. Standard library only, stdio transport.
All scoring lives in Synaptic (src/common/regime_engine/score_v2*.py, run by the Market Pulse export); this server only reads
data_store/dashboard/market_pulse_live.json (falls back to computing v2 itself if that file has no score_v2 block).

Tools
  get_regime(symbol, timeframe)   one timeframe: label, direction, trend, vol, character, reasons, test status
  get_mtf_regime(symbol)          all five timeframes + alignment + intraday bias + the 5-state daily regime and size
  explain_regime(symbol, timeframe?)  plain-English why, with the evidence label of every part
symbol: NIFTY | BANKNIFTY.  timeframe: 5m | 15m | 75m | 1D | 1W (aliases: 4h -> 75m, daily -> 1D, weekly -> 1W).
Indications only: nothing here places or sizes orders.

Claude Desktop config (Settings -> Developer -> Edit config), under "mcpServers":
  "synaptic-regime": {"command": "/usr/bin/python3",
                      "args": ["/Users/<you>/Documents/synaptic-main/Backtrader_App/app/regime_mcp.py"]}
"""
import json
import os
import sys
import time

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
LIVE_JSON = os.path.join(ROOT, "data_store", "dashboard", "market_pulse_live.json")
PROTO = "2024-11-05"
ALIAS = {"4h": "75m", "4H": "75m", "75min": "75m", "daily": "1D", "1d": "1D", "day": "1D", "weekly": "1W", "1w": "1W", "week": "1W",
         "5min": "5m", "15min": "15m"}
SYM = {"NIFTY": "NIFTY", "NIFTY50": "NIFTY", "^NSEI": "NIFTY", "BANKNIFTY": "BANKNIFTY", "NIFTYBANK": "BANKNIFTY", "BANK NIFTY": "BANKNIFTY"}
_CACHE = {"t": 0, "data": None}


def _block():
    if time.time() - _CACHE["t"] < 20 and _CACHE["data"]:
        return _CACHE["data"]
    data, age = None, None
    if os.path.exists(LIVE_JSON):
        try:
            data = json.load(open(LIVE_JSON))
            age = round((time.time() - os.path.getmtime(LIVE_JSON)) / 60, 1)
        except Exception:
            data = None
    sv = (data or {}).get("score_v2")
    src = "Market Pulse export"
    if not sv or sv.get("error"):
        sys.path.insert(0, ROOT)
        from src.common.regime_engine import score_v2_live as L
        sv, age, src = L.live(ROOT), 0.0, "computed now (export had no score v2)"
    out = {"sv": sv, "age_min": age, "source": src, "exposure": {}}
    for m in ("NIFTY", "BANKNIFTY"):
        x = (((data or {}).get("india") or {}).get("playbook") or {}).get(m) or {}
        if x:
            out["exposure"][m] = {k: x.get(k) for k in ("regime", "days_in_regime", "size_today", "size_v2_paper", "warnings")}
    _CACHE.update(t=time.time(), data=out)
    return out


def _sym(s):
    s = SYM.get(str(s or "NIFTY").upper().replace("_", " ").strip(), SYM.get(str(s).upper()))
    if not s:
        raise ValueError("symbol must be NIFTY or BANKNIFTY")
    return s


def _tf(t):
    t = ALIAS.get(str(t), str(t))
    if t not in ("5m", "15m", "75m", "1D", "1W"):
        raise ValueError("timeframe must be 5m, 15m, 75m (India 4H), 1D or 1W")
    return t


def _meta(b):
    v = (b["sv"].get("validation") or {})
    return {"data_age_min": b["age_min"], "source": b["source"], "tested_on": v.get("tested_on"), "horizons": v.get("horizons"),
            "rule": "label 'tested' = passed walk-forward test (both halves, luck < 5%); 'context' = shown, not proven. Indications only."}


def get_regime(symbol="NIFTY", timeframe="15m"):
    b, m, tf = _block(), _sym(symbol), _tf(timeframe)
    mk = (b["sv"].get("markets") or {}).get(m) or {}
    row = next((r for r in mk.get("timeframes", []) if r["tf"] == tf), None)
    return {"symbol": m, "timeframe": tf, "regime": row, **_meta(b)} if row else {"symbol": m, "timeframe": tf, "error": "no data", **_meta(b)}


def get_mtf_regime(symbol="NIFTY"):
    b, m = _block(), _sym(symbol)
    mk = (b["sv"].get("markets") or {}).get(m) or {}
    return {"symbol": m, "timeframes": [{k: r.get(k) for k in ("tf", "name", "bar", "label", "raw_label", "bars_in_label", "direction",
                                                                "trend", "vol", "character", "status", "trust")} for r in mk.get("timeframes", [])],
            "alignment": mk.get("alignment"), "intraday_bias_5m_15m": mk.get("intraday_bias"),
            "daily_exposure_regime": b["exposure"].get(m), "note": b["sv"].get("note"), "errors": mk.get("errors"), **_meta(b)}


def explain_regime(symbol="NIFTY", timeframe=None):
    b, m = _block(), _sym(symbol)
    sys.path.insert(0, ROOT)
    from src.common.regime_engine.score_v2_live import explain
    txt = explain(b["sv"], m, _tf(timeframe) if timeframe else None)
    ex = b["exposure"].get(m)
    if ex and not timeframe:
        txt += (f"\nDaily 5-state regime: {ex.get('regime')} (day {ex.get('days_in_regime')}), size today {ex.get('size_today')}x "
                f"[tested], paper v2 size {ex.get('size_v2_paper')}x [paper].")
    return {"symbol": m, "text": txt, "note": b["sv"].get("note"), **_meta(b)}


P_SYM = {"symbol": {"type": "string", "enum": ["NIFTY", "BANKNIFTY"]}}
P_TF = {"timeframe": {"type": "string", "enum": ["5m", "15m", "75m", "1D", "1W"]}}
TOOLS = {
    "get_regime": (get_regime, "Regime score v2 for one timeframe of NIFTY or BANKNIFTY: label (Trending up / down, Range, Volatile, Mixed), "
                   "direction -100..+100, trend and volatility 0-100 vs own last year, top reasons, and whether that timeframe passed testing.",
                   {**P_SYM, **P_TF}),
    "get_mtf_regime": (get_mtf_regime, "All five timeframes (5m, 15m, 75m, 1D, 1W) for NIFTY or BANKNIFTY, with alignment, intraday bias "
                       "and the tested daily 5-state regime + size.", P_SYM),
    "explain_regime": (explain_regime, "Plain-English explanation of the current regime for NIFTY or BANKNIFTY (one timeframe or all), "
                       "with the evidence label of each part.", {**P_SYM, **P_TF}),
}


def _reply(i, result=None, error=None):
    msg = {"jsonrpc": "2.0", "id": i}
    msg["error" if error else "result"] = error or result
    sys.stdout.write(json.dumps(msg, default=str) + "\n")
    sys.stdout.flush()


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except Exception:
            continue
        m, i = req.get("method"), req.get("id")
        if m == "initialize":
            _reply(i, {"protocolVersion": (req.get("params") or {}).get("protocolVersion", PROTO),
                       "capabilities": {"tools": {}}, "serverInfo": {"name": "synaptic-regime", "version": "1.0"}})
        elif m == "tools/list":
            _reply(i, {"tools": [{"name": k, "description": d, "inputSchema": {"type": "object", "properties": props}}
                                 for k, (_f, d, props) in TOOLS.items()]})
        elif m == "tools/call":
            p = req.get("params") or {}
            name, args = p.get("name"), p.get("arguments") or {}
            if name not in TOOLS:
                _reply(i, error={"code": -32601, "message": f"unknown tool {name}"})
                continue
            try:
                _reply(i, {"content": [{"type": "text", "text": json.dumps(TOOLS[name][0](**args), default=str)}]})
            except Exception as e:
                _reply(i, {"content": [{"type": "text", "text": f"error: {e}"}], "isError": True})
        elif m == "ping":
            _reply(i, {})
        elif i is not None and not str(m).startswith("notifications/"):
            _reply(i, error={"code": -32601, "message": f"method not found: {m}"})


if __name__ == "__main__":
    main()
