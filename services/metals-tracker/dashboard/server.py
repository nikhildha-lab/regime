"""FastAPI dashboard: serves the SPA and streams snapshots over a websocket."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Callable, Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse

STATIC = Path(__file__).parent / "static"


def _slim(snap: dict, focus: str | None) -> dict:
    """Ship ONE instrument in full + a light overview row per coin (50+ coins)."""
    insts = snap.get("instruments", {})
    if not insts:
        return snap
    if focus not in insts:
        focus = next(iter(insts))
    gen_date = str(snap.get("generated_at") or "")[:10]
    ov = []
    for k, v in insts.items():
        su = v.get("setup") or {}
        d = v.get("daily_candles") or []
        pc = None
        if len(d) >= 2:
            last = d[-1]
            pc = d[-2].get("c") if last.get("iso") == gen_date else last.get("c")
        spot = v.get("spot")
        chg = round((spot - pc) / pc * 100, 2) if (spot and pc) else None
        ov.append({
            "key": k, "spot": spot, "chg": chg,
            "stance": (v.get("regime") or {}).get("stance"),
            "grade": su.get("grade"), "side": su.get("side"),
            "state": su.get("state"), "mode": su.get("mode"),
            "qualified": bool(su and (su.get("grade") == "A+" or
                              (su.get("grade") == "A" and su.get("mode") == "continuation"))),
            "cvd": (v.get("imbalance") or {}).get("cum_delta"),
            "vol": v.get("day_vol_usd"),
        })
    out = {k: v for k, v in snap.items() if k != "instruments"}
    out["instruments"] = {focus: insts[focus]}
    out["overview"] = ov
    out["focus"] = focus
    return out


def build_app(get_snapshot: Callable[[], dict], refresh_ms: int = 1500,
              refresh_history: Callable[[], str] | None = None,
              set_focus: Callable[[str], None] | None = None) -> FastAPI:
    app = FastAPI(title="Crypto Live Tracker — BTC · ETH · SOL")

    @app.get("/", response_class=HTMLResponse)
    def index():
        return (STATIC / "index.html").read_text()

    @app.get("/dash", response_class=HTMLResponse)
    def combined():
        return (STATIC / "combined.html").read_text()

    @app.get("/api/snapshot", response_class=JSONResponse)
    def snapshot(focus: Optional[str] = None, full: int = 0):
        snap = get_snapshot()
        if full:
            return snap
        if focus and set_focus:
            set_focus(focus)
        return _slim(snap, focus)

    @app.post("/api/refresh_history", response_class=JSONResponse)
    def refresh_hist():
        """Re-fetch chart history on demand (dashboard button)."""
        if refresh_history is None:
            return {"ok": False, "detail": "only available in LIVE mode"}
        try:
            return {"ok": True, "detail": str(refresh_history())}
        except Exception as e:
            return {"ok": False, "detail": str(e)}

    @app.websocket("/ws")
    async def ws(socket: WebSocket):
        await socket.accept()
        focus = socket.query_params.get("focus") or None
        if focus and set_focus:
            set_focus(focus)
        try:
            while True:
                await socket.send_text(json.dumps(_slim(get_snapshot(), focus)))
                await asyncio.sleep(max(0.25, refresh_ms / 1000))
        except WebSocketDisconnect:
            return
        except Exception:
            return

    return app
