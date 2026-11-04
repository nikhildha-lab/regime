"""
Instant agent runs through the Claude API (2026-10-17). Used when waiting for the next scheduled run is too slow:
a big-news / market shock wakes the News Watcher at once (analysis in ~30-60 s instead of up to an hour).

Key: pasted by Nikhil at http://127.0.0.1:8050/agent/setup -> data_store/secrets/anthropic_key.txt (this Mac only,
file mode 600, never printed, never sent anywhere except api.anthropic.com).
Spend guard (data_store/agent/agent_config.json): max calls per day and max estimated USD per day, at most one run
per 10 minutes for the same shock. Usage log: data_store/agent/api_usage.jsonl
Read-only for markets: it never places orders; it only writes data_store/agent/out/news.json and a notification.
"""
from __future__ import annotations

import json
import os
import re
import ssl
import subprocess
import threading
import time
import urllib.request
from datetime import datetime

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
KEY = os.path.join(ROOT, "data_store", "secrets", "anthropic_key.txt")
AG = os.path.join(ROOT, "data_store", "agent")
CFG = os.path.join(AG, "agent_config.json")
USAGE = os.path.join(AG, "api_usage.jsonl")
STATE = os.path.join(AG, "instant_state.json")
API = "https://api.anthropic.com/v1"
DEFAULT_CFG = {"daily_call_limit": 30, "daily_budget_usd": 5.0, "model": "auto", "min_gap_minutes": 10,
               "price_per_mtok_in": 3.0, "price_per_mtok_out": 15.0}
_LOCK = threading.Lock()


def _ctx():
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def cfg() -> dict:
    c = dict(DEFAULT_CFG)
    if os.path.exists(CFG):
        try:
            c.update(json.load(open(CFG)))
        except Exception:
            pass
    else:
        os.makedirs(AG, exist_ok=True)
        json.dump(c, open(CFG, "w"), indent=1)
    return c


def has_key() -> bool:
    return os.path.exists(KEY) and len(open(KEY).read().strip()) > 20


def save_key(k: str) -> str | None:
    k = k.strip()
    if not k.startswith("sk-ant-") or len(k) < 30:
        return "That does not look like a Claude API key (it starts with sk-ant-)."
    os.makedirs(os.path.dirname(KEY), exist_ok=True)
    with open(KEY, "w") as f:
        f.write(k)
    os.chmod(KEY, 0o600)
    return None


def _req(method, path, body=None, timeout=120):
    k = open(KEY).read().strip()
    r = urllib.request.Request(API + path, method=method, data=json.dumps(body).encode() if body else None,
                               headers={"x-api-key": k, "anthropic-version": "2023-06-01", "content-type": "application/json"})
    with urllib.request.urlopen(r, timeout=timeout, context=_ctx()) as resp:
        return json.loads(resp.read())


def model() -> str:
    c = cfg()
    if c.get("model") and c["model"] != "auto":
        return c["model"]
    st = json.load(open(STATE)) if os.path.exists(STATE) else {}
    if st.get("model") and time.time() - st.get("model_at", 0) < 86400:
        return st["model"]
    try:
        ids = [m["id"] for m in _req("GET", "/models?limit=50").get("data", [])]
        pick = next((i for i in ids if "sonnet" in i), ids[0] if ids else None)
    except Exception:
        pick = None
    pick = pick or "claude-sonnet-4-5"
    st.update(model=pick, model_at=time.time())
    json.dump(st, open(STATE, "w"))
    return pick


def usage_today() -> dict:
    day, n, usd = datetime.now().strftime("%Y-%m-%d"), 0, 0.0
    if os.path.exists(USAGE):
        for ln in open(USAGE):
            try:
                u = json.loads(ln)
            except Exception:
                continue
            if u.get("at", "").startswith(day):
                n += 1
                usd += u.get("usd", 0)
    return {"calls": n, "usd": round(usd, 3)}


def status() -> dict:
    c, u = cfg(), usage_today()
    st = json.load(open(STATE)) if os.path.exists(STATE) else {}
    return {"key_saved": has_key(), "calls_today": u["calls"], "usd_today": u["usd"], "limits": {k: c[k] for k in ("daily_call_limit", "daily_budget_usd")},
            "last_run": st.get("last_run"), "last_error": st.get("last_error"), "model": st.get("model")}


def ask(system: str, user: str, web_search=True, max_tokens=2000, purpose="") -> str:
    c, u = cfg(), usage_today()
    if u["calls"] >= c["daily_call_limit"] or u["usd"] >= c["daily_budget_usd"]:
        raise RuntimeError(f"daily limit reached ({u['calls']} calls, ${u['usd']})")
    body = {"model": model(), "max_tokens": max_tokens, "system": system, "messages": [{"role": "user", "content": user}]}
    if web_search:
        body["tools"] = [{"type": "web_search_20250305", "name": "web_search", "max_uses": 3}]
    try:
        r = _req("POST", "/messages", body)
    except Exception:
        if not web_search:
            raise
        body.pop("tools", None)
        r = _req("POST", "/messages", body)
    text = "".join(b.get("text", "") for b in r.get("content", []) if b.get("type") == "text")
    us = r.get("usage", {})
    usd = (us.get("input_tokens", 0) * c["price_per_mtok_in"] + us.get("output_tokens", 0) * c["price_per_mtok_out"]) / 1e6
    with open(USAGE, "a") as f:
        f.write(json.dumps({"at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "purpose": purpose, "model": body["model"],
                            "in": us.get("input_tokens"), "out": us.get("output_tokens"), "usd": round(usd, 4)}) + "\n")
    return text


def _json_from(text: str) -> dict | None:
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        return None


def _notify(title, msg):
    msg = msg.replace('"', "'")[:220]
    try:
        subprocess.run(["osascript", "-e", f'display notification "{msg}" with title "{title}" sound name "Glass"'], timeout=5, capture_output=True)
    except Exception:
        pass


def _context() -> str:
    """Compact facts for the News Watcher (only what it needs)."""
    news = json.load(open(os.path.join(AG, "news.json"))) if os.path.exists(os.path.join(AG, "news.json")) else {}
    mp = os.path.join(ROOT, "data_store", "dashboard", "market_pulse_live.json")
    j = json.load(open(mp)) if os.path.exists(mp) else {}
    I = j.get("india") or {}
    m = j.get("markets") or {}
    xr = {k: {x: (m.get(k, {}).get("exposure_regime") or {}).get(x) for x in ("regime", "days_in_regime", "size_blend", "close", "bar")}
          for k in ("NIFTY", "BANKNIFTY")}
    sw = (I.get("stocks") or {}).get("swing") or {}
    po = (I.get("stocks") or {}).get("positional") or {}
    watch = sorted({r["symbol"] for k in ("pullbacks", "breakouts") for r in sw.get(k, [])} |
                   {r["symbol"] for r in po.get("leaders", [])})
    live = json.load(open(os.path.join(ROOT, "data_store", "india", "live_quotes.json"))) if os.path.exists(os.path.join(ROOT, "data_store", "india", "live_quotes.json")) else {}
    q = (live.get("quotes") or {})
    moves = {k: (round(100 * (q[k]["ltp"] / q[k]["prev_close"] - 1), 2) if q.get(k, {}).get("prev_close") else None)
             for k in ("Nifty 50", "Nifty Bank", "India VIX", "Nifty IT", "Nifty PSU Bank", "Nifty Metal", "Nifty Pharma", "Nifty FMCG")}
    keep = lambda xs: [{k: g.get(k) for k in ("title", "sources", "severity", "published", "new")} for g in (xs or [])]
    ctx = {"now_ist": datetime.now().strftime("%Y-%m-%d %H:%M"), "shock": news.get("shock"),
           "big_headlines": keep(news.get("shock_items"))[:10], "market_moving": keep(news.get("top"))[:12],
           "nse_announcements_watchlist": [g["title"] for g in (news.get("nse_announcements") or []) if g.get("symbol") in watch][:10],
           "live_moves_pct": moves, "live_quotes_at": live.get("at"), "regime": xr, "events": j.get("events"),
           "overnight": [{k: t.get(k) for k in ("label", "value", "change_5d")} for t in (I.get("overnight") or [])],
           "watchlist": watch}
    return json.dumps(ctx, default=str)


def instant_news(triggers: list[str]):
    """Run the News Watcher now (background thread). Safe to call often: rate-limited and budget-capped."""
    if not has_key():
        return
    if not _LOCK.acquire(blocking=False):
        return
    try:
        st = json.load(open(STATE)) if os.path.exists(STATE) else {}
        sig = "|".join(sorted(t[:60] for t in triggers))
        gap = cfg()["min_gap_minutes"] * 60
        if st.get("last_sig") == sig and time.time() - st.get("last_ts", 0) < 3 * 3600:
            return                                   # same shock already analysed
        if time.time() - st.get("last_ts", 0) < gap and not any(t.startswith("Market:") for t in triggers):
            return
        pdir = os.path.join(AG, "personas")
        system = "\n\n".join(open(os.path.join(pdir, f)).read() for f in ("00_COMMON_RULES.md", "news/PERSONA.md", "news/N4_instant_shock.md")
                             if os.path.exists(os.path.join(pdir, f)))
        system += ("\n\nYou are running in INSTANT mode, woken by these triggers. You cannot read files: all facts are in the "
                   "user message (JSON). Use web search to confirm big stories. Reply with ONLY the JSON object of the "
                   "News Watcher output format (persona, as_of, shock_mode, headline, big_news, events_next_3_sessions, "
                   "stock_flags, move_explained, label). Keep the headline under 25 words and start it with 'BIG NEWS:' "
                   "when shock_mode is true.")
        user = "Triggers:\n- " + "\n- ".join(triggers) + "\n\nFacts (JSON):\n" + _context()
        text = ask(system, user, web_search=True, purpose="news instant")
        out = _json_from(text) or {"persona": "news", "headline": text[:300]}
        out.update(via="instant (Claude API)", as_of=datetime.now().strftime("%Y-%m-%d %H:%M"), triggers=triggers)
        os.makedirs(os.path.join(AG, "out"), exist_ok=True)
        with open(os.path.join(AG, "out", "news.json"), "w") as f:
            json.dump(out, f, indent=1)
        try:                                     # same run protocol as the scheduled personas: guard check + run log
            import subprocess, sys as _sys
            root = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
            subprocess.run([_sys.executable, os.path.join(root, "scripts/agent/guard_check.py"), "--file",
                            os.path.join(AG, "out", "news.json")], cwd=root, capture_output=True, timeout=60)
            subprocess.run([_sys.executable, os.path.join(root, "scripts/agent/agent_log.py"), "run", "--persona", "news",
                            "--activity", "N4", "--out", os.path.join(AG, "out", "news.json"),
                            "--headline", str(out.get("headline") or "")[:200]], cwd=root, capture_output=True, timeout=60)
        except Exception:
            pass
        _notify("Synaptic News Watcher", out.get("headline") or "analysis ready - see the Desk tab")
        st.update(last_sig=sig, last_ts=time.time(), last_run=out["as_of"], last_error=None)
    except Exception as e:
        st = json.load(open(STATE)) if os.path.exists(STATE) else {}
        st.update(last_error=f"{datetime.now():%H:%M} {str(e)[:200]}")
    finally:
        json.dump(st, open(STATE, "w"))
        _LOCK.release()


def setup_page(msg: str = "") -> bytes:
    s = status()
    return f"""<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Claude API key</title><body style="font-family:system-ui;max-width:640px;margin:40px auto;padding:0 16px;line-height:1.5">
<h2>Claude API key for instant agent runs</h2>
<p>Used only when big news or a market shock needs analysis at once. Saved on this Mac only
(data_store/secrets/anthropic_key.txt), never shown again. Get a key at console.anthropic.com -> API keys.</p>
<p>Status: <b>{'key saved' if s['key_saved'] else 'no key yet'}</b> · today {s['calls_today']} calls, about ${s['usd_today']}
(limits {s['limits']['daily_call_limit']} calls / ${s['limits']['daily_budget_usd']} per day - change in data_store/agent/agent_config.json)
{('<br>Last run ' + s['last_run']) if s.get('last_run') else ''}{('<br><span style="color:#b00">Last error: ' + s['last_error'] + '</span>') if s.get('last_error') else ''}</p>
{f'<p style="color:#b00">{msg}</p>' if msg else ''}
<form method="post" action="/agent/setup"><input type="password" name="key" placeholder="sk-ant-..." style="width:100%;padding:8px;font-size:15px" autocomplete="off">
<p><button style="padding:8px 16px;font-size:15px">Save key</button> <a href="/">back to Market Pulse</a></p></form></body>""".encode()


if __name__ == "__main__":
    print(json.dumps(status(), indent=1))
