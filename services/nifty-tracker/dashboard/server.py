"""FastAPI dashboard: serves the SPA and streams snapshots over a websocket."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Callable

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse

STATIC = Path(__file__).parent / "static"


def build_app(get_snapshot: Callable[[], dict], refresh_ms: int = 1500,
              refresh_history: Callable[[], str] | None = None) -> FastAPI:
    app = FastAPI(title="Nifty / Bank Nifty Live Tracker")

    @app.get("/", response_class=HTMLResponse)
    def index():
        return (STATIC / "index.html").read_text()

    @app.get("/api/snapshot", response_class=JSONResponse)
    def snapshot():
        return get_snapshot()

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
        try:
            while True:
                await socket.send_text(json.dumps(get_snapshot()))
                await asyncio.sleep(max(0.25, refresh_ms / 1000))
        except WebSocketDisconnect:
            return
        except Exception:
            return

    return app
