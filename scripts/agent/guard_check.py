#!/usr/bin/env python3
"""
Synaptic desk - deterministic guardrail check (2026-10-17). Standard library only.
Run by every persona right after writing its output (--file), and by the Governor over everything written today.
Verdicts: PASS · WARN (Governor reviews) · BLOCK (output must not be used; the Desk Lead skips it, the dashboard flags it).

Checks
  json      file is valid JSON
  label     every label is tested / watch / context / fact; every idea has labels
  card      every idea has an invalidation (stop / invalid_if), a time stop and a registered call_id
  size      size hint <= 1.0 x engine size per idea, and all ideas on one index together <= 1.0
  failed    a failed signal (HMM, GMM, news mood, meta-score, net pressure, vol-of-vol, macro score) used as a reason
  phrase    hype or execution language ("sure shot", "guaranteed", "I have placed", "auto-execute", ...)
  shock     ideas while shock mode is on
  halt      ideas while the desk is halted (memory/halt.json)
  health    ideas while system health is "fail" (WARN - the Governor checks whether the idea uses the broken input)
  volshort  naked short options in High Volatility, or fresh short options on expiry day after 14:30
  level     Nifty / Bank Nifty prices more than 8% from the last close (wrong instrument or invented number)
  stale     file older than its schedule allows (WARN)
Usage: guard_check.py [--file data_store/agent/out/intraday.json] [--date YYYY-MM-DD]
Exit code 2 when --file gets a BLOCK.
"""
import argparse, csv, datetime as dt, glob, json, os, re, sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
AG = os.environ.get("SYNAPTIC_AGENT_DIR") or os.path.join(ROOT, "data_store", "agent")   # override only for tests
LOGS = os.path.join(AG, "logs")
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
LABELS = {"tested", "watch", "context", "fact"}
REGISTRY_FROM = "2026-10-19"          # call registration mandatory from the first live session
IDEA_KEYS = ("ideas", "trade_prep")
SKIP_IDEAS = ("intraday_review.json", "positional_weekly.json", "swing_weekly.json", "lead_weekly.json", "swing_followup.json",
              "crypto_weekly.json")
LEVEL_TOL = {"NIFTY": 0.08, "BANKNIFTY": 0.08, "ES": 0.08, "NQ": 0.10, "BTC": 0.15, "ETH": 0.20, "SOL": 0.25}
CRYPTO_M = ("BTC", "ETH", "SOL")
US_M = ("ES", "NQ", "SPY", "QQQ", "US")
FAILED = r"\b(hmm|gmm|news mood|mood score|meta[- ]score|net pressure|vol[- ]of[- ]vol|macro (backdrop )?score)\b"
WEAK = r"\b(weekly trend|monthly trend|1d direction|daily v2|weekly v2|alignment)\b"
HYPE = (r"sure[- ]shot|guarantee|can'?t lose|risk[- ]free|no[- ]brainer|100% (sure|certain)|jackpot|all[- ]in|"
        r"i (have )?placed|order (is )?placed|auto[- ]?execut|executing the trade|place the order now")
SHORTOPT = r"short straddle|short strangle|naked|sell (the )?(atm |otm )?(call|put|ce|pe)\b|write (call|put)s?"
DEFINED = r"hedge|defined[- ]risk|spread|iron|wing|butterfly|condor"
MAX_AGE_H = {"news.json": 3, "intraday.json": 7, "swing.json": 20, "positional.json": 26, "desk_notes.json": 9}


def now():
    return dt.datetime.now(IST)


def load(p):
    try:
        return json.load(open(p)), None
    except Exception as e:
        return None, str(e)[:120]


def walk_text(o):
    if isinstance(o, dict):
        return " ".join(walk_text(v) for v in o.values())
    if isinstance(o, list):
        return " ".join(walk_text(v) for v in o)
    return str(o) if isinstance(o, str) else ""


def find_ideas(o):
    out = []
    if isinstance(o, dict):
        for k, v in o.items():
            if k in IDEA_KEYS and isinstance(v, list):
                out += [x for x in v if isinstance(x, dict) and x]
            else:
                out += find_ideas(v)
    elif isinstance(o, list):
        for v in o:
            out += find_ideas(v)
    return out


def find_labels(o):
    out = []
    if isinstance(o, dict):
        for k, v in o.items():
            if k == "label" and isinstance(v, str):
                out.append(v)
            elif k == "labels" and isinstance(v, list):
                out += [x for x in v if isinstance(x, str)]
            else:
                out += find_labels(v)
    elif isinstance(o, list):
        for v in o:
            out += find_labels(v)
    return out


def size_of(x):
    v = x.get("size_hint", x.get("size"))
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    m = re.search(r"(\d+(?:\.\d+)?)\s*x", str(v)) or re.search(r"^(\d+(?:\.\d+)?)$", str(v).strip())
    return float(m.group(1)) if m else None


def market_of(x):
    t = (str(x.get("market", "")) + " " + str(x.get("instrument", "")) + " " + str(x.get("idea", ""))).upper()
    for k, m in (("BTC", "BTC"), ("ETH", "ETH"), ("SOL", "SOL"), ("SP500", "ES"), ("S&P", "ES"), ("ES ", "ES"), ("NDX", "NQ"),
                 ("NASDAQ", "NQ"), ("NQ ", "NQ"), ("SPY", "SPY"), ("QQQ", "QQQ")):
        if k in t + " ":
            return m
    if "BANKNIFTY" in t.replace(" ", "") or "BANK NIFTY" in t:
        return "BANKNIFTY"
    if "NIFTY" in t:
        return "NIFTY"
    return None


def context():
    c = {"closes": {}, "regime": {}, "shock": False, "health": None, "expiry": False, "halt": None, "calls": {}}
    d, _ = load(os.path.join(ROOT, "data_store", "dashboard", "market_pulse_live.json"))
    if d:
        for m, p in ((d.get("india") or {}).get("playbook") or {}).items():
            c["closes"][m] = p.get("close")
            c["regime"][m] = p.get("regime")
            c["expiry"] = c["expiry"] or bool((p.get("events") or {}).get("weekly_expiry"))
        for m in ("NIFTY", "BANKNIFTY", "ES", "NQ", "BTC", "ETH", "SOL"):   # fallback: the exposure regime block (always present)
            er = ((d.get("markets") or {}).get(m) or {}).get("exposure_regime") or {}
            c["closes"].setdefault(m, er.get("close"))
            c["regime"].setdefault(m, er.get("regime"))
            if not c["closes"].get(m):
                c["closes"][m] = er.get("close")
            if not c["regime"].get(m):
                c["regime"][m] = er.get("regime")
    n, _ = load(os.path.join(AG, "news.json"))
    c["shock"] = bool(((n or {}).get("shock") or {}).get("active"))
    h, _ = load(os.path.join(AG, "health.json"))
    c["health"] = (h or {}).get("overall")
    hl, _ = load(os.path.join(AG, "memory", "halt.json"))
    if hl and hl.get("active"):
        c["halt"] = hl.get("reason", "halted")
    cp = os.path.join(LOGS, "agent_calls.csv")
    if os.path.exists(cp):
        c["calls"] = {r["call_id"]: r for r in csv.DictReader(open(cp))}
    return c


def check(path, c):
    issues = []
    add = lambda lvl, code, msg: issues.append({"level": lvl, "code": code, "msg": msg[:220]})
    name = os.path.basename(path)
    o, err = load(path)
    if err:
        add("BLOCK", "json", f"not valid JSON: {err}")
        return issues
    age_h = (now().timestamp() - os.path.getmtime(path)) / 3600
    if name in MAX_AGE_H and age_h > MAX_AGE_H[name] and now().weekday() < 5 and 9 <= now().hour < 16:
        add("WARN", "stale", f"{name} is {age_h:.1f} h old")
    for lab in find_labels(o):
        if lab not in LABELS and lab.lower() not in ("none", "") and not lab.startswith(("paper", "failed")):
            add("WARN", "label", f"unknown label '{lab}'")
    text = walk_text(o)
    if re.search(HYPE, text, re.I):
        add("BLOCK", "phrase", f"hype or execution language: '{re.search(HYPE, text, re.I).group(0)}'")
    ideas = [] if name in SKIP_IDEAS else find_ideas(o)
    if ideas and c["halt"]:
        add("BLOCK", "halt", f"{len(ideas)} idea(s) while the desk is halted ({c['halt']})")
    if ideas and c["shock"] and "shock" not in text.lower():
        add("BLOCK", "shock", f"{len(ideas)} idea(s) while shock mode is on and the output does not mention it")
    if ideas and c["health"] == "fail":
        add("WARN", "health", "ideas while system health is 'fail' - check they do not depend on the broken input")
    today = now().strftime("%Y-%m-%d")
    for i, x in enumerate(ideas):
        tag = f"idea {i + 1}"
        if not any(x.get(k) not in (None, "", 0) for k in ("stop", "invalid_if", "invalidation")):
            add("BLOCK", "card", f"{tag}: no invalidation (stop / invalid_if)")
        if not x.get("time_stop") and name != "desk_notes.json":
            add("WARN", "card", f"{tag}: no time stop")
        labs = x.get("labels") or ([x["label"]] if x.get("label") else [])
        if not labs:
            add("BLOCK", "label", f"{tag}: no evidence label")
        cid = x.get("call_id")
        if today >= REGISTRY_FROM:
            if not cid:
                add("BLOCK", "card", f"{tag}: no call_id - register it with scripts/agent/agent_log.py call")
            elif cid not in c["calls"]:
                add("BLOCK", "card", f"{tag}: call_id {cid} not in logs/agent_calls.csv")
        sz = size_of(x)
        if sz is not None and sz > 1.0:
            add("BLOCK", "size", f"{tag}: size hint {sz}x is above the engine size")
        why = " ".join(str(x.get(k, "")) for k in ("why", "why_now", "reason", "fits_regime", "idea"))
        if re.search(FAILED, why, re.I):
            add("BLOCK", "failed", f"{tag}: uses a failed signal as a reason ('{re.search(FAILED, why, re.I).group(0)}')")
        elif re.search(WEAK, why, re.I):
            add("WARN", "failed", f"{tag}: leans on an untested input ('{re.search(WEAK, why, re.I).group(0)}') - fine as context only")
        m = market_of(x)
        st = " ".join(str(x.get(k, "")) for k in ("structure", "idea", "instrument"))
        if re.search(SHORTOPT, st, re.I) and not re.search(DEFINED, st, re.I):
            if m and c["regime"].get(m) == "High_Volatility":
                add("BLOCK", "volshort", f"{tag}: naked short options in High Volatility")
            if c["expiry"] and now().hour * 60 + now().minute >= 870:
                add("BLOCK", "volshort", f"{tag}: fresh short options on expiry day after 14:30")
        close = c["closes"].get(m) if m else None
        if close:
            nums = [x.get(k) for k in ("entry", "entry_lo", "entry_hi", "stop", "target") if isinstance(x.get(k), (int, float))]
            nums += [float(v.replace(",", "")) for v in re.findall(r"\b\d{2},?\d{3}(?:\.\d+)?\b",
                     " ".join(str(x.get(k, "")) for k in ("entry_zone", "invalid_if", "target", "stop")))]
            tol = LEVEL_TOL.get(m, 0.08)
            far = [v for v in nums if v > (1000 if m not in ("SOL", "ETH") else 10) and abs(v / close - 1) > tol]
            if far:
                add("BLOCK", "level", f"{tag}: level(s) {far[:3]} are >{int(tol * 100)}% from {m} close {close} - wrong instrument or invented")
        if m in CRYPTO_M and name not in SKIP_IDEAS:
            lev, liq, st = x.get("leverage"), x.get("liq_price"), x.get("stop")
            ent = x.get("entry") or x.get("entry_mid")
            if lev is None or liq is None:
                add("BLOCK", "leverage", f"{tag}: crypto idea without leverage and liquidation price")
            elif isinstance(lev, (int, float)) and lev > 25:
                add("BLOCK", "leverage", f"{tag}: leverage {lev}x above the 25x ceiling")
            elif all(isinstance(v, (int, float)) for v in (ent, liq, st)) and abs(ent - liq) < 2 * abs(ent - st):
                add("BLOCK", "leverage", f"{tag}: liquidation {liq} closer than 2x the stop distance")
        if m in US_M and cid and cid in c["calls"] and not x.get("preplanned"):
            hh = c["calls"][cid].get("ts", "")[11:16]
            if hh and (hh >= "23:30" or hh < "07:00"):
                add("BLOCK", "latenight", f"{tag}: new US idea at {hh} IST that was not in the pre-open plan")
        if cid and cid in c["calls"] and isinstance(x.get("stop"), (int, float)):
            reg = c["calls"][cid].get("stop")
            if reg and abs(float(reg) - float(x["stop"])) > 0.5:
                add("WARN", "card", f"{tag}: stop {x['stop']} differs from the registered stop {reg}")
    return issues


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file"); ap.add_argument("--date")
    a = ap.parse_args()
    c = context()
    day = a.date or now().strftime("%Y-%m-%d")
    if a.file:
        files = [a.file if os.path.isabs(a.file) else os.path.join(ROOT, a.file)]
    else:
        files = sorted(glob.glob(os.path.join(AG, "out", "*.json"))) + [os.path.join(AG, "desk_notes.json")]
        files = [f for f in files if os.path.exists(f) and dt.datetime.fromtimestamp(os.path.getmtime(f), IST).strftime("%Y-%m-%d") == day]
    res = {}
    for f in files:
        iss = check(f, c)
        v = "BLOCK" if any(i["level"] == "BLOCK" for i in iss) else "WARN" if iss else "PASS"
        res[os.path.relpath(f, ROOT)] = {"verdict": v, "issues": iss,
                                         "checked": now().isoformat(timespec="seconds")}
    # total size per index across all of today's ideas
    if not a.file:
        tot = {}
        for f in files:
            o, _ = load(f)
            if o is None or os.path.basename(f) in SKIP_IDEAS:
                continue
            for x in find_ideas(o):
                m, sz = market_of(x), size_of(x)
                if m and sz:
                    tot[m] = tot.get(m, 0) + sz
        for m, t in tot.items():
            if t > 1.0 + 1e-9:
                res.setdefault("_desk", {"verdict": "WARN", "issues": []})["issues"].append(
                    {"level": "WARN", "code": "size", "msg": f"{m}: ideas across personas add to {t:.2f}x engine size (Lead must cut to <= 1.0)"})
    os.makedirs(LOGS, exist_ok=True)
    rp = os.path.join(LOGS, "guard_report.json")
    old, _ = load(rp)
    if not a.file or not old or old.get("date") != day:
        old = {"date": day, "files": {}} if (not old or old.get("date") != day) else old
    old["files"].update(res)
    old["at"] = now().isoformat(timespec="seconds")
    vs = [v["verdict"] for v in old["files"].values()]
    old["summary"] = {k: vs.count(k) for k in ("PASS", "WARN", "BLOCK")}
    old["context"] = {"shock": c["shock"], "health": c["health"], "halt": c["halt"]}
    json.dump(old, open(rp, "w"), indent=1)
    gl = os.path.join(LOGS, "governance_log.csv")
    new = not os.path.exists(gl)
    with open(gl, "a", newline="") as fh:
        w = csv.writer(fh)
        if new:
            w.writerow(["ts", "file", "level", "code", "msg"])
        for f, r in res.items():
            for i in r["issues"]:
                w.writerow([now().isoformat(timespec="seconds"), f, i["level"], i["code"], i["msg"]])
    for f, r in res.items():
        print(f"{r['verdict']:5}  {f}")
        for i in r["issues"]:
            print(f"   {i['level']:5} {i['code']:8} {i['msg']}")
    if not res:
        print("nothing written today to check")
    if a.file and any(r["verdict"] == "BLOCK" for r in res.values()):
        sys.exit(2)


if __name__ == "__main__":
    main()
