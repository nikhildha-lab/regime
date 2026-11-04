#!/usr/bin/env python3
"""
Synaptic desk - agent log, call registry and memory helpers (2026-10-17). Standard library only.
Every scheduled persona run uses this so the Learner can learn and the Governor can audit.

  call   register a trade-prep / range / no-trade call -> prints its call_id (put it in your output card)
  run    log one finished run (also writes the activity_log.csv line) -> logs/agent_log.jsonl
  candidate  (Learner) add a lesson candidate with evidence -> memory/lesson_candidates.jsonl
  lesson     (Learner) promote a lesson into memory/desk_memory.md (evidence rules enforced)
  change     record any memory / addenda change -> memory/changelog.md
  show       print today's runs, calls and guard verdicts (short)
Examples:
  python3 scripts/agent/agent_log.py call --persona intraday --activity I2 --symbol NIFTY --kind directional --direction long \
      --entry-lo 22950 --entry-hi 22980 --stop 22890 --target 23080 --horizon 90m --confidence 55 --labels tested,watch \
      --size 0.5 --why "4H early turn up + 15m Range->Trending up"
  python3 scripts/agent/agent_log.py run --persona intraday --activity I2 --out data_store/agent/out/intraday.json \
      --headline "Open drive up; one long idea" --calls I2-20261019-0925-1 --unsure "order flow feed was 40 s old"
Indications only - nothing here places orders.
"""
import argparse, csv, datetime as dt, json, os, re, sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
AG = os.environ.get("SYNAPTIC_AGENT_DIR") or os.path.join(ROOT, "data_store", "agent")   # override only for tests
LOGS, MEM = os.path.join(AG, "logs"), os.path.join(AG, "memory")
CALLS = os.path.join(LOGS, "agent_calls.csv")
RUNS = os.path.join(LOGS, "agent_log.jsonl")
ACT = os.path.join(AG, "activity_log.csv")
CALL_COLS = ["call_id", "ts", "persona", "activity", "symbol", "kind", "direction", "structure", "basis", "entry_lo", "entry_hi",
             "stop", "target", "range_lo", "range_hi", "horizon", "confidence", "labels", "size", "regime", "v2_character", "why",
             "invalid_if", "status", "leverage", "liq_price", "preplanned"]
KINDS = {"directional", "range", "no_trade"}
CRYPTO = {"BTC", "ETH", "SOL", "BTCUSDT", "ETHUSDT", "SOLUSDT"}
LABELS = {"tested", "watch", "context", "fact"}
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))


def now():
    return dt.datetime.now(IST)


def _num(x):
    if x in (None, ""):
        return ""
    return float(x)


def _horizon_ok(h):
    return bool(re.fullmatch(r"\d+(m|d)", h or ""))


def _state():
    """Snapshot of regime / health / shock at run time, from the desk files (never fails)."""
    s = {}
    try:
        d = json.load(open(os.path.join(ROOT, "data_store", "dashboard", "market_pulse_live.json")))
        for m, p in ((d.get("india") or {}).get("playbook") or {}).items():
            sv = (p.get("score_v2") or {}).get("labels") or {}
            s[m] = {"regime": p.get("regime"), "size": p.get("size_today"),
                    "v2": {k: v.get("label") for k, v in sv.items()}}
        s["export_generated"] = (d.get("meta") or {}).get("generated")
    except Exception as e:
        s["export_error"] = str(e)[:80]
    for name in ("health", "news"):
        try:
            j = json.load(open(os.path.join(AG, f"{name}.json")))
            s[name] = j.get("overall") if name == "health" else bool((j.get("shock") or {}).get("active"))
        except Exception:
            s[name] = None
    return s


def cmd_call(a):
    if a.kind not in KINDS:
        sys.exit(f"kind must be one of {sorted(KINDS)}")
    if not _horizon_ok(a.horizon):
        sys.exit("horizon like 60m, 90m, 1d, 10d")
    labels = [x.strip() for x in (a.labels or "").split(",") if x.strip()]
    bad = [x for x in labels if x not in LABELS]
    if bad or not labels:
        sys.exit(f"labels must be from {sorted(LABELS)}")
    if a.kind == "directional":
        if a.direction not in ("long", "short") or a.stop is None:
            sys.exit("directional call needs --direction long|short and --stop")
        lo, hi = a.entry_lo, a.entry_hi if a.entry_hi is not None else a.entry_lo
        if lo is not None and hi is not None:
            if a.direction == "long" and not (a.stop < min(lo, hi)):
                sys.exit("long: stop must be below the entry zone")
            if a.direction == "short" and not (a.stop > max(lo, hi)):
                sys.exit("short: stop must be above the entry zone")
        if a.target is not None and lo is not None:
            if (a.direction == "long" and a.target <= max(lo, hi)) or (a.direction == "short" and a.target >= min(lo, hi)):
                sys.exit("target must be beyond the entry zone in the trade's direction")
    if a.kind == "range" and (a.range_lo is None or a.range_hi is None or a.range_lo >= a.range_hi):
        sys.exit("range call needs --range-lo < --range-hi")
    if a.size is not None and a.size > 1.0:
        sys.exit("size hint is x engine size and may not exceed 1.0")
    if a.symbol.upper() in CRYPTO and a.kind == "directional":            # crypto perpetuals: leverage + liquidation maths required
        if a.leverage is None or a.liq_price is None:
            sys.exit("crypto idea needs --leverage and --liq-price")
        if a.leverage > 25:
            sys.exit("leverage above the 25x ceiling")
        lo, hi = a.entry_lo, a.entry_hi if a.entry_hi is not None else a.entry_lo
        if lo is not None:
            mid = (lo + hi) / 2
            if abs(mid - a.liq_price) < 2 * abs(mid - a.stop):
                sys.exit("liquidation price must be at least 2x the stop distance away - lower the leverage")
    t = now()
    os.makedirs(LOGS, exist_ok=True)
    n = 1
    if os.path.exists(CALLS):
        n += sum(1 for r in csv.DictReader(open(CALLS)) if r["call_id"].startswith(f"{a.activity}-{t:%Y%m%d}-"))
    cid = f"{a.activity}-{t:%Y%m%d-%H%M}-{n}"
    st = _state().get(a.symbol, {})
    row = {"call_id": cid, "ts": t.isoformat(timespec="seconds"), "persona": a.persona, "activity": a.activity, "symbol": a.symbol.upper(),
           "kind": a.kind, "direction": a.direction or "", "structure": a.structure or "", "basis": a.basis,
           "entry_lo": _num(a.entry_lo), "entry_hi": _num(a.entry_hi if a.entry_hi is not None else a.entry_lo), "stop": _num(a.stop),
           "target": _num(a.target), "range_lo": _num(a.range_lo), "range_hi": _num(a.range_hi), "horizon": a.horizon,
           "confidence": a.confidence if a.confidence is not None else "", "labels": "|".join(labels), "size": _num(a.size),
           "regime": st.get("regime") or "", "v2_character": json.dumps(st.get("v2") or {}), "why": (a.why or "")[:300],
           "invalid_if": (a.invalid_if or "")[:200], "status": "open", "leverage": _num(a.leverage), "liq_price": _num(a.liq_price),
           "preplanned": "yes" if a.preplanned else ""}
    new = not os.path.exists(CALLS)
    with open(CALLS, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CALL_COLS)
        if new:
            w.writeheader()
        w.writerow(row)
    print(cid)


def cmd_run(a):
    t = now()
    rec = {"run_id": f"{a.activity}-{t:%Y%m%d-%H%M%S}", "ts": t.isoformat(timespec="seconds"), "persona": a.persona,
           "activity": a.activity, "out": a.out, "headline": (a.headline or "")[:300],
           "calls": [c for c in (a.calls or "").split(",") if c], "unsure": (a.unsure or "")[:400],
           "skipped": a.skipped or "", "errors": (a.errors or "")[:400], "state": _state()}
    if a.out:
        p = a.out if os.path.isabs(a.out) else os.path.join(ROOT, a.out)
        rec["out_ok"] = False
        try:
            json.load(open(p)); rec["out_ok"] = True
        except Exception as e:
            rec["out_error"] = str(e)[:120]
    os.makedirs(LOGS, exist_ok=True)
    with open(RUNS, "a") as f:
        f.write(json.dumps(rec) + "\n")
    new = not os.path.exists(ACT)
    with open(ACT, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["date", "time", "persona", "activity", "headline"])
        w.writerow([f"{t:%Y-%m-%d}", f"{t:%H:%M}", a.persona, a.activity, rec["headline"]])
    print(rec["run_id"], "out_ok" if rec.get("out_ok", True) else "OUT INVALID: " + rec.get("out_error", ""))


def cmd_candidate(a):
    t = now()
    os.makedirs(MEM, exist_ok=True)
    rec = {"ts": t.isoformat(timespec="seconds"), "key": a.key, "kind": a.kind, "persona": a.persona, "text": a.text,
           "n": a.n, "metric": a.metric, "value": a.value, "baseline": a.baseline, "evidence": a.evidence}
    with open(os.path.join(MEM, "lesson_candidates.jsonl"), "a") as f:
        f.write(json.dumps(rec) + "\n")
    print("candidate saved:", a.key)


MIN_N = {"market": 12, "process": 3, "mistake": 1}


def cmd_lesson(a):
    """Promote a lesson. Market lessons need n >= 12 graded calls and a gap vs baseline; process >= 3 repeats; mistake >= 1."""
    if a.n < MIN_N[a.kind]:
        sys.exit(f"{a.kind} lesson needs n >= {MIN_N[a.kind]} (got {a.n}) - keep it as a candidate")
    p = os.path.join(MEM, "desk_memory.md")
    s = open(p).read() if os.path.exists(p) else ""
    ids = [int(x) for x in re.findall(r"\[L-(\d{4})\]", s)]
    lid = f"L-{(max(ids) + 1) if ids else 1:04d}"
    t = now()
    review = (t + dt.timedelta(days=90 if a.kind == "market" else 180)).strftime("%Y-%m-%d")
    line = (f"- [{lid}] {a.text} (persona: {a.persona}; evidence: {a.evidence}; n={a.n}; added {t:%Y-%m-%d}; "
            f"review by {review}; status: active)")
    head = {"market": "## Market lessons", "process": "## Process lessons", "mistake": "## Mistakes to avoid"}[a.kind]
    if head not in s:
        sys.exit("desk_memory.md is missing its section headings")
    s = s.replace(head + "\n", head + "\n" + line + "\n", 1)
    s = _drop_placeholder(s, head)
    active = len(re.findall(r"status: active", s))
    if active > 40:
        sys.exit("memory full (40 active lessons): retire one first (status: retired) - nothing written")
    open(p, "w").write(s)
    _change(f"added {lid} ({a.kind}) - {a.text[:80]}", "learner", a.evidence)
    print(lid)


def _drop_placeholder(s, head):
    i = s.index(head)
    j = s.find("\n## ", i + len(head))
    sec = s[i:j if j > 0 else len(s)]
    sec2 = re.sub(r"\n\(none yet[^\n]*\)", "", sec)
    return s.replace(sec, sec2, 1)


def _change(what, who, why):
    t = now()
    p = os.path.join(MEM, "changelog.md")
    new = not os.path.exists(p)
    with open(p, "a") as f:
        if new:
            f.write("# Memory and addenda changelog\n\n| When (IST) | Who | Change | Evidence / why |\n| --- | --- | --- | --- |\n")
        f.write(f"| {t:%Y-%m-%d %H:%M} | {who} | {what.replace('|', '/')} | {str(why).replace('|', '/')} |\n")


def cmd_change(a):
    _change(a.what, a.who, a.why)
    print("logged")


def cmd_show(a):
    day = a.date or f"{now():%Y-%m-%d}"
    if os.path.exists(RUNS):
        for l in open(RUNS):
            r = json.loads(l)
            if r["ts"].startswith(day):
                print("RUN ", r["ts"][11:16], r["persona"], r["activity"], "|", r["headline"][:90], "| calls:", len(r["calls"]))
    if os.path.exists(CALLS):
        for r in csv.DictReader(open(CALLS)):
            if r["ts"].startswith(day):
                print("CALL", r["call_id"], r["symbol"], r["kind"], r["direction"], r["entry_lo"], r["stop"], r["target"], r["horizon"], r["status"])
    g = os.path.join(LOGS, "guard_report.json")
    if os.path.exists(g):
        j = json.load(open(g))
        print("GUARD", j.get("at"), j.get("summary"))


def main():
    ap = argparse.ArgumentParser()
    sp = ap.add_subparsers(dest="cmd", required=True)
    c = sp.add_parser("call")
    for k in ("persona", "activity", "symbol", "kind", "horizon", "labels"):
        c.add_argument("--" + k, required=True)
    c.add_argument("--direction"); c.add_argument("--structure"); c.add_argument("--basis", default="index")
    for k in ("entry-lo", "entry-hi", "stop", "target", "range-lo", "range-hi", "size"):
        c.add_argument("--" + k, type=float)
    c.add_argument("--confidence", type=int); c.add_argument("--why"); c.add_argument("--invalid-if")
    c.add_argument("--leverage", type=float); c.add_argument("--liq-price", type=float); c.add_argument("--preplanned", action="store_true")
    r = sp.add_parser("run")
    for k in ("persona", "activity"):
        r.add_argument("--" + k, required=True)
    for k in ("out", "headline", "calls", "unsure", "skipped", "errors"):
        r.add_argument("--" + k)
    cd = sp.add_parser("candidate")
    for k in ("key", "kind", "persona", "text", "evidence"):
        cd.add_argument("--" + k, required=True)
    cd.add_argument("--n", type=int, required=True); cd.add_argument("--metric"); cd.add_argument("--value", type=float)
    cd.add_argument("--baseline", type=float)
    le = sp.add_parser("lesson")
    le.add_argument("--kind", choices=["market", "process", "mistake"], required=True)
    for k in ("persona", "text", "evidence"):
        le.add_argument("--" + k, required=True)
    le.add_argument("--n", type=int, required=True)
    ch = sp.add_parser("change")
    for k in ("what", "who", "why"):
        ch.add_argument("--" + k, required=True)
    sh = sp.add_parser("show"); sh.add_argument("--date")
    a = ap.parse_args()
    {"call": cmd_call, "run": cmd_run, "candidate": cmd_candidate, "lesson": cmd_lesson, "change": cmd_change, "show": cmd_show}[a.cmd](a)


if __name__ == "__main__":
    main()
