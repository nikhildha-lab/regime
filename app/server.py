"""
Market Pulse - live local app.

    ./run_market_pulse.sh                      (macOS / Linux; first start installs pandas + numpy into .venv)
    python app/server.py [--port 8050] [--every 5] [--no-browser]

Opens http://127.0.0.1:8050 in your browser. A background worker loops:
    1. app/updater.py        fetch new prices (Yahoo, Dhan) and NSE end-of-day files
    2. export_dashboard_data rebuild every regime / recommendation "as of now"  (subprocess, ~30 s)
    3. the page notices the new data and redraws itself (it checks every 10 s)

Only listens on 127.0.0.1 (this computer). Nothing here places orders.
"""
from __future__ import annotations

import argparse
import importlib
import json
import os
import subprocess
import sys
import threading
import time
import traceback
import urllib.parse
import webbrowser
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
import updater  # noqa: E402

TEMPLATE = os.path.join(ROOT, "dashboard", "market_pulse_template.html")
LIVE_JSON = os.path.join(ROOT, "data_store", "dashboard", "market_pulse_live.json")


class Worker:
    def __init__(self, every_min: float, nse_every_min: float = 30):
        self.every = every_min
        self.nse_every = nse_every_min
        self.state = "idle"
        self.next_run = datetime.now()
        self.last_nse = 0.0
        self.last_error = None
        self.last_update = None
        self.kick = threading.Event()
        self.lock = threading.Lock()

    def status(self) -> dict:
        gen = None
        if os.path.exists(LIVE_JSON):
            try:
                with open(LIVE_JSON) as f:
                    gen = json.load(f)["meta"]["generated"]
            except Exception:
                pass
        return {"state": self.state, "next_run": self.next_run.strftime("%Y-%m-%d %H:%M:%S"), "generated": gen,
                "last_error": self.last_error, "last_update": self.last_update}

    def run_once(self):
        with self.lock:
            self.last_error = None
            try:
                self.state = "updating"
                importlib.reload(updater)                 # pick up updater fixes without restarting the app
                nse = time.time() - self.last_nse > self.nse_every * 60
                st = updater.update_all(include_nse=nse)
                if nse:
                    self.last_nse = time.time()
                bad = [k for k, v in st["sources"].items() if not v["ok"]]
                self.last_update = {"at": st["started"], "seconds": st["seconds"], "failed": bad,
                                    "errors": {k: st["sources"][k].get("error") for k in bad}}
                self.state = "building"
                updater.log("recomputing regimes ...")
                r = subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "export_dashboard_data.py"),
                                    "--as-of", "now", "--out", LIVE_JSON], cwd=ROOT, capture_output=True, text=True,
                                   timeout=600)   # never let a stuck recalculation freeze the refresh loop
                if r.returncode != 0:
                    raise RuntimeError("export failed: " + r.stderr.strip().splitlines()[-1] if r.stderr.strip() else "export failed")
                updater.log("dashboard data ready")
                if bad:
                    self.last_error = "could not reach: " + ", ".join(bad) + " (showing the latest data we have)"
            except Exception as e:
                self.last_error = str(e)[:300]
                updater.log("ERROR " + traceback.format_exc())
            finally:
                self.state = "idle"
                self.next_run = datetime.now() + timedelta(minutes=self.every)

    def loop(self):
        while True:
            self.run_once()
            self.kick.wait(timeout=self.every * 60)
            self.kick.clear()


def make_handler(worker: Worker):
    class H(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass

        def _send(self, code, body: bytes, ctype: str):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = self.path.split("?")[0]
            if path in ("/", "/index.html"):
                html = open(TEMPLATE).read().replace("__DATA__", "null")
                page = ('<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
                        '<meta name="viewport" content="width=device-width, initial-scale=1">\n</head>\n<body>\n'
                        + html + "\n</body>\n</html>\n")
                return self._send(200, page.encode(), "text/html; charset=utf-8")
            if path == "/api/pulse":
                if not os.path.exists(LIVE_JSON):
                    return self._send(503, b'{"error":"first refresh still running"}', "application/json")
                return self._send(200, open(LIVE_JSON, "rb").read(), "application/json")
            if path == "/api/status":
                return self._send(200, json.dumps(worker.status()).encode(), "application/json")
            if path == "/api/desk_facts":
                p_ = os.path.join(ROOT, "data_store", "agent", "desk_facts.json")
                return self._send(200, open(p_, "rb").read() if os.path.exists(p_) else b'{}', "application/json")
            if path == "/api/intraday":
                p_ = os.path.join(ROOT, "data_store", "flow", "oc_live.json")
                return self._send(200, open(p_, "rb").read() if os.path.exists(p_) else b'{"symbols":{}}', "application/json")
            if path in ("/api/stream", "/api/live"):           # live stream on the page's own address (app/stream.py)
                import stream
                if path == "/api/live":
                    return self._send(200, stream.HUB.body, "application/json")
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Connection", "keep-alive")
                self.end_headers()
                return stream.sse_loop(self.wfile)
            if path in ("/agent/setup", "/api/agent"):           # Claude API key for instant agent runs (app/agent_runner.py)
                import agent_runner as AR
                if path == "/api/agent":
                    return self._send(200, json.dumps(AR.status()).encode(), "application/json")
                return self._send(200, AR.setup_page(), "text/html; charset=utf-8")
            if path.startswith("/api/livedesk"):                 # Live Desk views + paper trades (app/livedesk.py)
                import livedesk as LD
                q = dict(urllib.parse.parse_qsl(self.path.split("?", 1)[1])) if "?" in self.path else {}
                body = LD.handle_get(path, q)
                return self._send(200, json.dumps(body, default=str).encode(), "application/json")
            if path == "/api/flow":
                try:
                    import flow
                    st = flow.status()
                except Exception as e:
                    st = {"error": str(e)}
                return self._send(200, json.dumps(st, default=str).encode(), "application/json")
            return self._send(404, b"not found", "text/plain")

        def _redirect(self, url: str):
            self.send_response(302)
            self.send_header("Location", url)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()

        def do_POST(self):
            if self.path == "/agent/setup":
                import agent_runner as AR
                origin = self.headers.get("Origin") or ""
                if origin and not origin.startswith(("http://127.0.0.1", "http://localhost")):
                    return self._send(403, b"forbidden", "text/plain")
                n = int(self.headers.get("Content-Length") or 0)
                f = dict(urllib.parse.parse_qsl(self.rfile.read(min(n, 10000)).decode()))
                err = AR.save_key(f.get("key", ""))
                return self._send(200, AR.setup_page(err or "Saved. Instant runs are on."), "text/html; charset=utf-8")
            if self.path.startswith("/api/backtester/_snapshot"):     # dashboard PDF snapshots (app/backtester_bridge.py)
                import importlib
                import backtester_bridge as BB
                importlib.reload(BB)
                if BB.handle(self, "POST", self.path.split("?")[0]):
                    return
            if self.path == "/api/refresh":
                worker.next_run = datetime.now()
                worker.kick.set()
                return self._send(202, b'{"ok":true}', "application/json")
            return self._send(404, b"not found", "text/plain")
    return H


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8050)))
    ap.add_argument("--every", type=float, default=5, help="minutes between refreshes")
    ap.add_argument("--no-browser", action="store_true")
    a = ap.parse_args()
    os.makedirs(os.path.dirname(LIVE_JSON), exist_ok=True)
    worker = Worker(a.every)
    threading.Thread(target=worker.loop, daemon=True).start()
    updater.ensure_background()
    try:
        import livedesk
        livedesk.start()                                   # trade cards + log book + grader over the live trackers
    except Exception as e:
        print("livedesk not started:", e, flush=True)
    try:
        import desk_facts
        desk_facts.start()                                 # no-AI desk facts every 5 min -> data_store/agent/desk_facts.json
    except Exception as e:
        print("desk_facts not started:", e, flush=True)
    class _QuietServer(ThreadingHTTPServer):
        def handle_error(self, request, client_address):          # 2026-10-23 audit: a browser closing a poll early is not an error
            import sys as _s
            if isinstance(_s.exc_info()[1], (ConnectionResetError, BrokenPipeError)):
                return
            super().handle_error(request, client_address)
    srv = _QuietServer(("127.0.0.1", a.port), make_handler(worker))
    url = f"http://127.0.0.1:{a.port}"
    print(f"\n  Market Pulse is running at {url}\n  Leave this window open. Press Ctrl+C to stop.\n", flush=True)
    if not a.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
