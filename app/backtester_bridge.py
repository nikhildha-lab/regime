"""
Dashboard PDF snapshot endpoint (2026-10-28).
  POST /api/backtester/_snapshot -> saves a rendered dashboard page to data_store/snapshots/<date>/<name>.html (local only)
The MASTER_BACKTESTER page, summary, library and rebuild routes were removed with the Backtests tab (2026-11-01).
Reloaded on every request by app/server.py. Research only - no orders.
"""
from __future__ import annotations

import os


def handle(handler, method: str, path: str) -> bool:
    """Returns True when the request was handled."""
    if method == "POST" and path == "/api/backtester/_snapshot":     # 2026-10-28: save a rendered dashboard page for the PDF snapshot (local only)
        import json as _j, re as _re, datetime as _dt
        origin = handler.headers.get("Origin") or ""
        if origin and not origin.startswith(("http://127.0.0.1", "http://localhost")):
            handler._send(403, b"forbidden", "text/plain"); return True
        n = int(handler.headers.get("Content-Length") or 0)
        if n > 30_000_000:
            handler._send(413, b"too big", "text/plain"); return True
        body = handler.rfile.read(n)
        name = _re.sub(r"[^a-zA-Z0-9_-]", "", (handler.headers.get("X-Snap-Name") or "page"))[:40] or "page"
        out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data_store", "snapshots", _dt.date.today().isoformat())
        os.makedirs(out, exist_ok=True)
        with open(os.path.join(out, name + ".html"), "wb") as f:
            f.write(body)
        handler._send(200, _j.dumps({"saved": name, "bytes": len(body)}).encode(), "application/json"); return True
    return False
