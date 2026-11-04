"""Check data_store/agent/out/global_narrative.json before the dashboard shows it (2026-11-04).
Usage: python3 scripts/agent/narrative_check.py   -> prints OK or the problems (exit 1)."""
import json
import os
import sys
from datetime import datetime

P = os.path.join(os.path.dirname(__file__), "..", "..", "data_store", "agent", "out", "global_narrative.json")
errs = []
try:
    s = json.load(open(P))
except Exception as e:
    print(f"BLOCK: cannot read {P}: {e}"); sys.exit(1)
try:
    datetime.strptime(str(s.get("as_of", ""))[:16], "%Y-%m-%d %H:%M")
except Exception:
    errs.append("as_of must be 'YYYY-MM-DD HH:MM' (IST)")
if not str(s.get("headline", "")).strip():
    errs.append("headline missing")
if not (isinstance(s.get("summary"), list) and 1 <= len(s["summary"]) <= 4 and all(isinstance(p, str) and p.strip() for p in s["summary"])):
    errs.append("summary must be 1-4 paragraphs (strings)")
for k, req in (("themes", ("name", "what")), ("turning_points", ("market", "what")), ("watch", ("what",)), ("sources", ("title", "url"))):
    v = s.get(k, [])
    if not isinstance(v, list) or any(not isinstance(x, dict) or any(not x.get(r) for r in req) for x in v):
        errs.append(f"{k}: list of objects with {', '.join(req)}")
if len(s.get("sources") or []) < 2:
    errs.append("at least 2 sources")
bad = [w for w in ("buy ", "sell ", "go long", "go short") if any(w in str(p).lower() for p in (s.get("summary") or []))]
if bad:
    errs.append(f"summary reads like trade advice ({', '.join(bad)}) - describe, do not recommend")
print("OK" if not errs else "BLOCK: " + " | ".join(errs)); sys.exit(1 if errs else 0)
