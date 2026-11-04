"""
System health monitor (2026-10-17). Runs every Market Pulse cycle (updater.update_all) on the Mac itself.
Checks every link the desk depends on, writes data_store/agent/health.json, shows a macOS notification when
something breaks, and in market hours restarts the Dhan order-flow recorders if they stopped.
Never prints or stores tokens - only their age / expiry.

Checks
  dhan_token      ~/Documents/nifty-live-tracker/.dhan_token expiry (shared with ~/Documents/dhan/.env)
  dhan_keepalive  dhan_keepalive.sh running (renews the 24h token outside market hours)
  tracker         Nifty Live Tracker (run.py, port 8899): footprint / CVD / OI / profile -> snapshot.json age
  depth_recorder  ~/Documents/dhan/orderflow/depth_recorder.py running + today's 20-level snapshots growing
  tick_recorder   ~/Documents/dhan/orderflow/recorder.py running + today's trade ticks growing
  market_pulse    dashboard JSON age
  india_quotes    live quotes for sectors / F&O stocks age
  nse_eod         stock EOD closes up to the last session
  desk_agent      Desk Analyst notes age
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from datetime import datetime, timedelta

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
HOME = os.path.expanduser("~")
TRACKER = (os.environ.get("MP_NIFTY_TRACKER_DIR") or os.path.join(HOME, "Documents", "nifty-live-tracker"))
DHAN = (os.environ.get("MP_DHAN_DIR") or os.path.join(HOME, "Documents", "dhan"))
OF = os.path.join(DHAN, "orderflow")
OUT = os.path.join(ROOT, "data_store", "agent", "health.json")
NOTIFY_STATE = os.path.join(ROOT, "data_store", "agent", "health_notified.json")


def _now():
    return datetime.now()


def market_hours(n=None):
    n = n or _now()
    return n.weekday() < 5 and (9 * 60 + 10) <= n.hour * 60 + n.minute <= (15 * 60 + 31)


def trading_day(n=None):
    return (n or _now()).weekday() < 5


def _running(pattern: str) -> bool:
    try:
        return subprocess.run(["pgrep", "-f", pattern], capture_output=True, timeout=5).returncode == 0
    except Exception:
        return False


def _age_min(path):
    return None if not os.path.exists(path) else round((time.time() - os.path.getmtime(path)) / 60, 1)


def _chk(name, status, detail, fix=""):
    return {"name": name, "status": status, "detail": detail, "fix": fix}


def check_dhan_token():
    p = os.path.join(TRACKER, ".dhan_token")
    if not os.path.exists(p):
        return _chk("Dhan token", "fail", "no token saved", "Double-click Dhan-Login on the Desktop and paste a fresh token.")
    try:
        d = json.load(open(p))
    except Exception:
        return _chk("Dhan token", "fail", "token file unreadable", "Run Dhan-Login again.")
    exp = d.get("expiry")
    saved = d.get("saved_at")
    try:
        e = datetime.fromisoformat(str(exp).replace("Z", "")[:19]) if exp else None
    except Exception:
        e = None
    if e is None and saved:
        try:
            e = datetime.fromisoformat(str(saved)[:19]) + timedelta(hours=24)
        except Exception:
            e = None
    if e is None:
        return _chk("Dhan token", "warn", "expiry unknown", "Run Dhan-Login if Dhan data stops.")
    hrs = (e - _now()).total_seconds() / 3600
    if hrs <= 0:
        return _chk("Dhan token", "fail", f"expired {abs(hrs):.0f} h ago", "Double-click Dhan-Login and paste a fresh token (web.dhan.co -> Access DhanHQ APIs).")
    if hrs < 6:
        return _chk("Dhan token", "warn", f"expires in {hrs:.1f} h", "Keep the Mac on so the keep-alive renews it, or run Dhan-Login.")
    return _chk("Dhan token", "ok", f"valid for {hrs:.0f} h more")


def check_keepalive():
    if _running("dhan_keepalive.sh"):
        return _chk("Dhan keep-alive", "ok", "running (renews the token outside market hours)")
    return _chk("Dhan keep-alive", "warn", "not running - the 24h token will not be renewed",
                "Double-click Dhan-Login once (it starts the keep-alive).")


def check_tracker():
    p = os.path.join(TRACKER, "snapshot.json")
    run = _running("run.py --mode live")
    age = _age_min(p)
    if not market_hours():
        return _chk("Order-flow tracker", "ok" if not run else "ok", f"market closed · last snapshot {age} min ago" if age is not None else "market closed")
    if not run:
        return _chk("Order-flow tracker", "fail", "not running - no footprint / CVD / OI flow today",
                    "Double-click Start-Tracker-LIVE (it also starts the depth recorder).")
    if age is None or age > 5:
        return _chk("Order-flow tracker", "warn", f"running but snapshot is {age} min old", "Check the Dhan token; restart Start-Tracker-LIVE.")
    return _chk("Order-flow tracker", "ok", f"live · snapshot {age} min old")


def _today_file(folder, suffix):
    return os.path.join(OF, "data", folder, f"{_now():%Y-%m-%d}{suffix}")


def _start(script):
    py = os.path.join(DHAN, "venv", "bin", "python")
    if not os.path.exists(py) or not os.path.exists(os.path.join(OF, script)):
        return False
    log = open(os.path.join(OF, script.replace(".py", ".log")), "a")
    subprocess.Popen([py, "-u", script], cwd=OF, stdout=log, stderr=log, start_new_session=True)
    return True


def check_recorder(name, script, folder, suffix, max_age=3):
    p = _today_file(folder, suffix)
    run = _running(script)
    age = _age_min(p)
    if not market_hours():
        return _chk(name, "ok", "market closed" + (f" · today's file {os.path.getsize(p)//1024} KB" if os.path.exists(p) else ""))
    restarted = ""
    if not run:
        restarted = " · restarted automatically" if _start(script) else " · could not restart"
        return _chk(name, "warn" if restarted.endswith("automatically") else "fail", "was not running" + restarted,
                    "If this repeats, check the Dhan token / 20-level data subscription (806/807 errors in the .log).")
    if age is None:
        return _chk(name, "warn", "running but no data file yet", "Wait 2 minutes; if still empty check the .log for 806/807.")
    if age > max_age:
        return _chk(name, "warn", f"running but last write {age} min ago", "Feed may have dropped; it reconnects itself - check the .log.")
    return _chk(name, "ok", f"recording · {os.path.getsize(p)//1024} KB today")


def check_engine():
    """Order-flow engine (of_engine.py): process, live.json age, each Dhan feed's state. Restarts it in market hours."""
    live = os.path.join(OF, "data", "of", "live.json")
    run = _running("of_engine.py")
    age = _age_min(live)
    name = "Order-flow engine (Dhan depth)"
    if not market_hours():
        return _chk(name, "ok", "market closed" + (f" · last update {age:.0f} min ago" if age is not None else ""))
    if not run:
        if _running("depth_recorder.py") or _running("orderflow/recorder.py"):
            return _chk(name, "warn", "old recorders are running instead of the engine",
                        "Close the old 'Dhan Recorders' window; Start-Tracker-LIVE starts the engine.")
        ok = _start("of_engine.py")
        return _chk(name, "warn" if ok else "fail", "was not running" + (" · restarted automatically" if ok else " · could not restart"),
                    "If this repeats, check of_engine.log (806 = Data API plan, 807 = token expired).")
    if age is None or age > 1:
        return _chk(name, "warn", f"running but live data is {age} min old", "Check the Dhan token and of_engine.log.")
    try:
        feeds = json.load(open(live)).get("feeds") or {}
    except Exception:
        feeds = {}
    bad = [f"{k}: {v.get('detail') or v.get('state')}" for k, v in feeds.items() if v.get("state") != "connected"]
    if bad:
        return _chk(name, "warn", "feed problem - " + "; ".join(bad)[:150], "It reconnects by itself; 806/807 in the detail need Dhan-Login or the Data API plan.")
    return _chk(name, "ok", f"live · {len(feeds)} Dhan feeds connected")


def check_api_key():
    try:
        import agent_runner as AR
        st = AR.status()
    except Exception as e:
        return _chk("Instant agent (Claude API)", "warn", f"module error: {str(e)[:60]}")
    if not st["key_saved"]:
        return _chk("Instant agent (Claude API)", "ok", "off (optional) - no API key, so big news waits for the next scheduled run",
                    "Optional: open http://127.0.0.1:8050/agent/setup to add a key.")   # 2026-11-03 (Nikhil): optional, not a warning
    if st.get("last_error"):
        return _chk("Instant agent (Claude API)", "warn", f"last run failed: {st['last_error'][:80]}", "Check the key / credit at console.anthropic.com.")
    return _chk("Instant agent (Claude API)", "ok", f"ready · {st['calls_today']} calls, ${st['usd_today']} today")


def check_file(name, path, max_min_open, max_min_closed, fix):
    age = _age_min(path)
    if age is None:
        return _chk(name, "fail", "missing", fix)
    lim = max_min_open if market_hours() else max_min_closed
    if age > lim:
        return _chk(name, "warn", f"{age:.0f} min old", fix)
    return _chk(name, "ok", f"{age:.0f} min old")


def notify(failing):
    """macOS notification once per problem per day."""
    try:
        st = json.load(open(NOTIFY_STATE)) if os.path.exists(NOTIFY_STATE) else {}
    except Exception:
        st = {}
    day = f"{_now():%Y-%m-%d}"
    new = [c for c in failing if st.get(c["name"]) != day]
    if not new:
        return
    msg = "; ".join(f"{c['name']}: {c['detail']}" for c in new)[:220]
    try:
        subprocess.run(["osascript", "-e", f'display notification "{msg}" with title "Synaptic desk - needs attention" sound name "Basso"'],
                       timeout=5, capture_output=True)
    except Exception:
        pass
    for c in new:
        st[c["name"]] = day
    with open(NOTIFY_STATE, "w") as f:
        json.dump(st, f)


def run_all() -> dict:
    checks = [check_dhan_token(), check_keepalive(), check_tracker(),
              check_engine(),
              check_api_key(),
              check_file("Market Pulse data", os.path.join(ROOT, "data_store", "dashboard", "market_pulse_live.json"), 15, 24 * 60,
                         "Double-click Market Pulse on the Desktop."),
              check_file("Live stock / sector quotes", os.path.join(ROOT, "data_store", "india", "live_quotes.json"), 15, 24 * 60,
                         "Needs Market Pulse running and a valid Dhan login (Dhan-Login on the Desktop)."),
              check_file("Desk Analyst notes", os.path.join(ROOT, "data_store", "agent", "desk_notes.json"), 17 * 60, 4 * 24 * 60,
                         "The Desk Lead runs 08:45 and 16:45 on weekdays (scheduled task in Claude); the last run may have hit the Claude usage limit.")]
    worst = "fail" if any(c["status"] == "fail" for c in checks) else "warn" if any(c["status"] == "warn" for c in checks) else "ok"
    out = {"at": f"{_now():%Y-%m-%d %H:%M:%S}", "market_hours": market_hours(), "overall": worst, "checks": checks}
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT + ".tmp", "w") as f:
        json.dump(out, f, indent=1)
    os.replace(OUT + ".tmp", OUT)
    bad = [c for c in checks if c["status"] == "fail" or (c["status"] == "warn" and market_hours())]
    if bad and trading_day():
        notify(bad)
    return {"overall": worst, "problems": [c["name"] for c in checks if c["status"] != "ok"]}


JOBS = [("health", run_all)]

if __name__ == "__main__":
    print(json.dumps(run_all(), indent=1))
