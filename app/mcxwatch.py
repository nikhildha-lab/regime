"""Keeps the MCX order-flow recorder (~/Documents/dhan/orderflow/mcx_recorder.py) running in the EVENING MCX session, 15:40-23:58 (2026-11-05; evening only since 2026-11-06).
Called from the live-desk loop; checks at most every 2 minutes. Starts the recorder only if its live.json is stale AND no
mcx_recorder.py process exists, so it never runs twice. Read-only data recorder - never places orders."""
from __future__ import annotations

import os
import subprocess
import time
from datetime import datetime

DHAN = os.environ.get("MP_DHAN_DIR") or os.path.join(os.path.expanduser("~"), "Documents", "dhan")
OFD = os.path.join(DHAN, "orderflow")
LIVE = os.path.join(OFD, "data", "mcx", "live.json")
S = {"t": 0, "last": None}


def tick():
    if time.time() - S["t"] < 120:
        return S["last"]
    S["t"] = time.time()
    now = datetime.now()
    # 2026-11-06 (Nikhil: "record mcx in the evening"): during NSE hours the MCX socket was refused all day and its retries drew
    # HTTP 429s on the Dhan account. The recorder now runs 15:40-23:58 only. A recorder still running in NSE hours (the old 08:58
    # version) is stopped here, so the NSE tracker / order-flow engine keep their connections; the evening start loads the new code.
    hm = now.strftime("%H:%M")
    if now.weekday() < 5 and "09:00" <= hm < "15:38":
        if subprocess.run(["pgrep", "-f", "mcx_recorder.py"], capture_output=True).returncode == 0:
            subprocess.run(["pkill", "-f", "mcx_recorder.py"])
            S["last"] = f"stopped the recorder at {hm} (NSE hours - evening only)"; return S["last"]
    if now.weekday() >= 5 or not ("15:38" <= hm <= "23:50"):
        S["last"] = "outside the evening MCX window (15:40-23:58)"; return S["last"]
    try:
        if time.time() - os.path.getmtime(LIVE) < 180:
            S["last"] = "running"; return S["last"]
    except OSError:
        pass
    if subprocess.run(["pgrep", "-f", "mcx_recorder.py"], capture_output=True).returncode == 0:
        S["last"] = "process up, status file stale"; return S["last"]
    py = os.path.join(DHAN, "venv", "bin", "python")
    log = open(os.path.join(OFD, "mcx_recorder.log"), "a")
    cmd = (["caffeinate", "-i"] if subprocess.run(["which", "caffeinate"], capture_output=True).returncode == 0 else []) + [py, "-u", "mcx_recorder.py"]
    p = subprocess.Popen(cmd, cwd=OFD, stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
    S["last"] = f"started pid {p.pid} at {now:%H:%M}"
    return S["last"]
