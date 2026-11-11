"""
ORDER FLOW RESEARCH - the standing study (2026-11-11, Nikhil: "yes for 12 month rebuild and study result ... it needs to keep
updating to find strong patterns"). Plan: claude/orderflow-research-plan-2026-11-11.md (phase 3).

What it does, every 6 hours in a background thread inside the dashboard:
  1. Bars: 12 months of real Binance footprint for BTC / ETH / SOL (app/ofhistory.py, 5-min bars merged to 5 / 15 / 30) and
     the recorded footprint of every other market (alts, metals, energy, Nifty / Bank Nifty futures, Nifty 50 stocks).
  2. Events: the same detector the live card uses (app/ofpatterns.py), each with its context AS OF that bar.
  3. Scenarios - a FIXED list written down before any result was seen (no searching for new combinations):
       singles (7 patterns), singles by context (with / against a trend day, rotation day, at yesterday's value edge),
       8 overlaps (two patterns, same side, within 2 bars) and 4 sequences (scripts in order inside one session).
  4. Each scenario x scope x timeframe is one test. Outcome = the move 6 bars later in the pattern's direction, in basis
     points, minus the round-trip cost (crypto / metals 14 bp, Nifty 18 pts, Bank Nifty 45 pts, stocks 10 bp).
  5. MASTER_BACKTESTER checks: first 70 % of dates vs last 30 %, quarter by quarter (where a year exists), 2x and 3x costs,
     best three trades removed, and Benjamini-Hochberg across ALL tests in the run (the price of testing many things).
  6. Verdict: STRONG only if every check passes. Results, a running streak per test, and FINDINGS_ofstudy.md are written each
     run, so a pattern has to stay strong run after run to matter.

Research only. Nothing here trades. Bars in a session only ever see earlier bars.
"""
from __future__ import annotations

import json
import math
import os
import threading
import time
from datetime import datetime, timedelta, timezone

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
OUT = os.path.join(ROOT, "data_store", "research", "ofstudy")
CACHE = os.path.join(OUT, "cache")
EVERY = 6 * 3600
HORIZON = 6
_CODE = os.path.getmtime(__file__) if os.path.exists(__file__) else 0
_J = globals().get("_J") or {"gen": 0, "thread": None, "status": {}}

PNAME = {"stack": "Stacked Imbalances", "absorb": "Absorption", "exhaust": "Exhaustion", "unfinished": "Unfinished Auction",
         "finished": "Finished Auction", "trapped": "Trapped Traders", "div": "Delta Divergence"}
OVERLAPS = [("absorb", "trapped", "Absorption + Trapped Traders"), ("exhaust", "finished", "Exhaustion + Finished Auction"),
            ("stack", "unfinished", "Stacked Imbalances + Unfinished Auction"), ("div", "absorb", "Divergence + Absorption"),
            ("exhaust", "div", "Exhaustion + Divergence"), ("trapped", "stack", "Trapped Traders + Stacked Imbalances"),
            ("absorb", "exhaust", "Absorption + Exhaustion"), ("finished", "trapped", "Finished Auction + Trapped Traders")]
SEQUENCES = {"seq_fail": "Failed Auction Script: outside value → exhaustion → trapped → back inside value",
             "seq_turn": "Absorb And Turn: absorption → stacked imbalances same side within 3 bars",
             "seq_cont": "Trend Continuation: stacked → pullback absorbed → close beyond the impulse",
             "seq_flip": "Trap Flip: trapped traders → stacked imbalances same side within 3 bars"}


def _mods():
    import ofpatterns as O
    import ofhistory as H
    return O, H


def _cost_bps(key, price, O):
    if key in O.COST_PTS:
        return O.COST_PTS[key] / price * 1e4
    if key in (O.S.get("stocks") or ()):
        return 10.0
    return 14.0


def _scopes(O, H):
    """(scope, key, tf, loader) for every book."""
    out = []
    for c in H.COINS:
        for tf in (5, 15, 30):
            out.append(("Crypto Majors · 12 Mo", c, tf, ("hist", c)))
    O.S["stocks"] = O.stocks()
    for k, (desk, src, tfs) in O.MARKETS.items():
        if k in H.COINS:
            continue
        sc = "India Index · Live" if k in O.COST_PTS else "Crypto Alts · Live" if desk == "crypto" else "Metals & Energy · Live"
        for tf in tfs:
            out.append((sc, k, tf, ("live", desk, src)))
    for k in O.S["stocks"]:
        for tf in (15, 30):
            out.append(("India Stocks · Live", k, tf, ("live", "nifty", "fp15")))
    return out


def _bars(O, H, key, tf, how, cache):
    if how[0] == "hist":
        if "b5" not in cache:
            cache["b5"] = H.bars(key)
        return cache["b5"] if tf == 5 else O.merge(cache["b5"], tf, 0, base=5)
    return O._book_bars(key, how[1], how[2], tf, O.EV_BACKFILL_DAYS, cache)


def _session_rows(O, key, tf, sess, prev, hist):
    """Scenario occurrences of one session: list of dicts (scenario, side, bar, x6, x12, cost)."""
    recs = O._event_records(key, tf, sess, prev, hist, [], False)
    n = len(sess)
    if n < HORIZON + 2:
        return [], (0.0, 0)
    c = [b["c"] for b in sess]
    prior = O.profile(prev) if prev else None
    inval = [bool(prior and prior["val"] <= x <= prior["vah"]) for x in c]
    fwd = lambda i, h: None if i + h >= n else (c[i + h] / c[i] - 1) * 1e4
    base6 = [fwd(i, HORIZON) for i in range(n)]
    out = []

    def add(sc, side, i):
        sd = 1 if side == "long" else -1
        x6, x12 = fwd(i, HORIZON), fwd(i, 12)
        if x6 is None:
            return
        out.append({"sc": sc, "side": side, "i": i, "d": sess[i]["d"], "x6": sd * x6, "x12": None if x12 is None else sd * x12,
                    "cost": _cost_bps(key, c[i], O)})

    idx = {b["t"]: i for i, b in enumerate(sess)}
    at = {}
    for r in recs:
        i = idx.get(r["t"])
        if i is None:
            continue
        r["_i"] = i
        at.setdefault((r["pattern"], r["side"]), []).append(i)
        p = r["pattern"]
        add("single|" + p, r["side"], i)
        dt = (r["ctx"] or {}).get("day_type") or ""
        if dt.startswith("Trend"):
            withd = (dt == "Trend Up") == (r["side"] == "long")
            add(("ctx_withtrend|" if withd else "ctx_againsttrend|") + p, r["side"], i)
        elif dt == "Rotation":
            add("ctx_rotation|" + p, r["side"], i)
        cx = r["ctx"] or {}
        if prior and cx.get("atr") and r["close"]:
            atr_pct = cx["atr"] / r["close"] * 100
            e = min(abs(cx.get("prior_vah_pct") or 99), abs(cx.get("prior_val_pct") or 99))
            if e <= 0.5 * atr_pct:
                add("ctx_valueedge|" + p, r["side"], i)
    # overlaps: B within 2 bars after (or same bar as) A, same side; counted once at the later bar
    for a, b, _ in OVERLAPS:
        for side in ("long", "short"):
            A, B = at.get((a, side), []), at.get((b, side), [])
            seen = set()
            for ia in A:
                for ib in B:
                    j = max(ia, ib)
                    if abs(ib - ia) <= 2 and j not in seen:
                        seen.add(j); add(f"ovl|{a}+{b}", side, j)
    # sequences
    for side in ("long", "short"):
        sd = 1 if side == "long" else -1
        ex, tr, ab, st = at.get(("exhaust", side), []), at.get(("trapped", side), []), at.get(("absorb", side), []), at.get(("stack", side), [])
        done = set()
        for i0 in ex:                                   # failed auction script
            if inval[i0]:
                continue
            t1 = next((j for j in tr if i0 <= j <= i0 + 4), None)
            if t1 is None:
                continue
            k = next((j for j in range(t1, min(n, t1 + 5)) if inval[j]), None)
            if k is not None and ("f", k) not in done:
                done.add(("f", k)); add("seq|seq_fail", side, k)
        for i0 in ab:                                   # absorb and turn
            j = next((j for j in st if i0 < j <= i0 + 3), None)
            if j is not None and ("t", j) not in done:
                done.add(("t", j)); add("seq|seq_turn", side, j)
        for i0 in st:                                   # trend continuation
            ext = sess[i0]["h"] if sd > 0 else sess[i0]["l"]
            pb = next((j for j in ab if i0 < j <= i0 + 6), None)
            if pb is None:
                continue
            k = next((j for j in range(pb + 1, min(n, pb + 7)) if (c[j] > ext if sd > 0 else c[j] < ext)), None)
            if k is not None and ("c", k) not in done:
                done.add(("c", k)); add("seq|seq_cont", side, k)
        for i0 in tr:                                   # trap flip
            j = next((j for j in st if i0 < j <= i0 + 3), None)
            if j is not None and ("p", j) not in done:
                done.add(("p", j)); add("seq|seq_flip", side, j)
    base = [x for x in base6 if x is not None]
    return out, (sum(base), len(base))


def _book(O, H, scope, key, tf, how, today_cut):
    """Every scenario row of one book, using a per-day disk cache for days that can no longer change."""
    folder = os.path.join(CACHE, f"{key}_{tf}".replace("/", "_"))
    os.makedirs(folder, exist_ok=True)
    rows, bsum, bn = [], 0.0, 0
    bars = _bars(O, H, key, tf, how, {})
    S_ = O.sessions(bars)
    for i, sess in enumerate(S_):
        d = sess[0]["d"]
        f = os.path.join(folder, f"{d}.json")
        if d < today_cut and os.path.exists(f):
            try:
                c = json.load(open(f)); rows += c["rows"]; bsum += c["bs"]; bn += c["bn"]
                continue
            except Exception:
                pass
        hist = [b for x in S_[max(0, i - 3):i] for b in x]
        try:
            r, (s, n) = _session_rows(O, key, tf, sess, S_[i - 1] if i else None, hist)
        except Exception:
            continue
        rows += r; bsum += s; bn += n
        if d < today_cut:
            try:
                json.dump({"rows": r, "bs": s, "bn": n}, open(f, "w"), separators=(",", ":"))
            except Exception:
                pass
    return rows, (bsum / bn if bn else 0.0)


def _stats(rows, base_long, quarters_needed):
    """One test's numbers. rows: occurrences with x6 (bp, pattern direction) and cost (bp)."""
    xs = [r["x6"] for r in rows]
    n = len(xs)
    if not n:
        return None
    cost = [r["cost"] for r in rows]
    net = [x - c for x, c in zip(xs, cost)]
    mean = lambda a: sum(a) / len(a) if a else None
    sd = (sum((v - mean(net)) ** 2 for v in net) / (n - 1)) ** 0.5 if n > 1 else 0.0
    t = mean(net) / (sd / math.sqrt(n)) if sd > 0 else 0.0
    p = math.erfc(abs(t) / math.sqrt(2))
    ds = sorted({r["d"] for r in rows})
    cut = ds[int(len(ds) * 0.7)] if len(ds) > 3 else None
    tr = [v for v, r in zip(net, rows) if cut and r["d"] < cut]
    te = [v for v, r in zip(net, rows) if cut and r["d"] >= cut]
    te2 = [x - 2 * c for x, c, r in zip(xs, cost, rows) if cut and r["d"] >= cut]
    q = {}
    for v, r in zip(net, rows):
        k = r["d"][:4] + "Q" + str((int(r["d"][5:7]) - 1) // 3 + 1)
        q.setdefault(k, []).append(v)
    qpos = sum(1 for a in q.values() if mean(a) > 0)
    b3 = sorted(net)[:-3] if n > 3 else []
    exc = [r["x6"] - (base_long if r["side"] == "long" else -base_long) for r in rows]
    return {"n": n, "win": round(100 * sum(1 for x in xs if x > 0) / n), "gross": round(mean(xs), 2), "net1": round(mean(net), 2),
            "net2": round(mean([x - 2 * c for x, c in zip(xs, cost)]), 2), "net3": round(mean([x - 3 * c for x, c in zip(xs, cost)]), 2),
            "excess": round(mean(exc), 2), "cost": round(mean(cost), 1), "t": round(t, 2), "p": p,
            "train": round(mean(tr), 2) if tr else None, "test": round(mean(te), 2) if te else None, "test2": round(mean(te2), 2) if te2 else None,
            "n_test": len(te), "split": cut, "quarters": len(q), "q_pos": qpos, "best3_removed": round(mean(b3), 2) if b3 else None,
            "x12": round(mean([r["x12"] for r in rows if r["x12"] is not None]), 2) if any(r["x12"] is not None for r in rows) else None,
            "first": ds[0], "last": ds[-1], "days": len(ds)}


def _label(sc):
    kind, rest = sc.split("|", 1)
    if kind == "single":
        return "Single", PNAME.get(rest, rest)
    if kind.startswith("ctx_"):
        ctx = {"ctx_withtrend": "With Trend Day", "ctx_againsttrend": "Against Trend Day", "ctx_rotation": "Rotation Day", "ctx_valueedge": "At Value Edge"}[kind]
        return "Context", f"{PNAME.get(rest, rest)} · {ctx}"
    if kind == "ovl":
        a, b = rest.split("+")
        return "Overlap", next((l for x, y, l in OVERLAPS if x == a and y == b), rest)
    return "Sequence", SEQUENCES.get(rest, rest)


def run() -> dict:
    O, H = _mods()
    t0 = time.time()
    today_cut = (datetime.now(timezone.utc).date() - timedelta(days=1)).isoformat()
    groups, base = {}, {}
    books = _scopes(O, H)
    st = _J["status"]
    for k, (scope, key, tf, how) in enumerate(books):
        st.update(stage=f"book {k + 1} of {len(books)}: {key} {tf}M")
        try:
            rows, b = _book(O, H, scope, key, tf, how, today_cut)
        except Exception as e:
            st["err"] = f"{key} {tf}: {type(e).__name__}: {e}"[:200]
            continue
        for r in rows:
            r["mkt"] = key
            groups.setdefault((scope, tf, r["sc"]), []).append(r)
        base[(scope, tf, key)] = b
    tests = []
    for (scope, tf, sc), rows in groups.items():
        bl = sum(base.get((scope, tf, r["mkt"]), 0.0) * (1 if r["side"] == "long" else -1) for r in rows) / len(rows)
        s = _stats(rows, 0.0, 3)
        if not s:
            continue
        s["excess"] = round(s["gross"] - bl, 2)
        kind, label = _label(sc)
        tests.append({"id": f"{scope}|{tf}|{sc}", "scope": scope, "tf": tf, "sc": sc, "kind": kind, "label": label,
                      "markets": len({r["mkt"] for r in rows}), "long": sum(1 for r in rows if r["side"] == "long"), **s})
    # Benjamini-Hochberg over every test in this run
    ps = sorted((t["p"], i) for i, t in enumerate(tests))
    m = len(ps)
    qv = [1.0] * m
    run_min = 1.0
    for rank in range(m, 0, -1):
        p, i = ps[rank - 1]
        run_min = min(run_min, p * m / rank)
        qv[i] = run_min
    prev = {}
    try:
        prev = {t["id"]: t for t in json.load(open(os.path.join(OUT, "latest.json"))).get("tests", [])}
    except Exception:
        pass
    today = datetime.now().strftime("%Y-%m-%d %H:%M")
    for t, q in zip(tests, qv):
        hist12 = t["scope"].endswith("12 Mo")
        min_n = 100 if hist12 else 40
        t["q"] = round(q, 4)
        t["p"] = round(t["p"], 5)
        checks = {"enough": t["n"] >= min_n, "train": (t["train"] or -1) > 0, "test": (t["test"] or -1) > 0, "test_2x": (t["test2"] or -1) > 0,
                  "best3": (t["best3_removed"] or -1) > 0, "fdr": q <= 0.10,
                  "days": t["days"] >= 40, "quarters": (t["q_pos"] >= max(3, math.ceil(0.75 * t["quarters"]))) if t["quarters"] >= 4 else True}
        t["checks"] = checks
        if not checks["enough"]:
            v = "Too Few"
        elif all(checks.values()):
            v = "Strong"
        elif checks["train"] and checks["test"]:
            v = "Promising"
        elif (t["net1"] or 0) < 0 and (t["test"] or 0) < 0:
            v = "No Edge"
        else:
            v = "Mixed"
        t["verdict"] = v
        p0 = prev.get(t["id"]) or {}
        t["streak"] = (p0.get("streak", 0) + 1) if v == "Strong" and p0.get("verdict") == "Strong" else (1 if v == "Strong" else 0)
        t["first_strong"] = p0.get("first_strong") if p0.get("first_strong") and v == "Strong" else (today if v == "Strong" else None)
        t["was"] = p0.get("verdict")
    order = {"Strong": 0, "Promising": 1, "Mixed": 2, "No Edge": 3, "Too Few": 4}
    tests.sort(key=lambda t: (order[t["verdict"]], -(t["net1"] or -99)))
    cov = {}
    for scope, key, tf, how in books:
        cov.setdefault(scope, set()).add(key)
    res = {"at": today, "secs": round(time.time() - t0, 1), "n_tests": len(tests), "horizon": HORIZON,
           "coverage": {k: sorted(v) for k, v in cov.items()},
           "hist": H.status().get("have"), "counts": {v: sum(1 for t in tests if t["verdict"] == v) for v in order},
           "rule": "Outcome = move 6 bars later in the pattern's direction minus round-trip cost (crypto / metals 14 bp, Nifty 18 pts, Bank Nifty 45 pts, stocks 10 bp). "
                   "STRONG = enough events (100 on 12-month data, 40 on live) AND at least 40 separate days AND positive after cost in the first 70 % of dates AND in the last 30 % AND at 2x cost in the last 30 % "
                   "AND with the best 3 removed AND quarter by quarter (where a year exists) AND Benjamini-Hochberg q <= 0.10 across every test in the run.",
           "tests": tests}
    os.makedirs(OUT, exist_ok=True)
    tmp = os.path.join(OUT, "latest.json.tmp")
    json.dump(res, open(tmp, "w"), default=str); os.replace(tmp, os.path.join(OUT, "latest.json"))
    with open(os.path.join(OUT, "history.jsonl"), "a") as f:
        f.write(json.dumps({"at": today, "counts": res["counts"], "strong": [t["id"] for t in tests if t["verdict"] == "Strong"]}) + "\n")
    _findings(res)
    return res


def _findings(res):
    L = [f"# FINDINGS - Order Flow Research ({res['at']})", "",
         "Written automatically by app/ofstudy.py after every run (every 6 hours). Research only - not a trading rule.", "",
         f"Tests this run: {res['n_tests']} · " + " · ".join(f"{k} {v}" for k, v in res["counts"].items()), "",
         "Rule: " + res["rule"], "", "Coverage: " + "; ".join(f"{k}: {len(v)} markets" for k, v in res["coverage"].items()), ""]
    for v in ("Strong", "Promising"):
        rows = [t for t in res["tests"] if t["verdict"] == v][:25]
        L += [f"## {v} ({sum(1 for t in res['tests'] if t['verdict'] == v)})", ""]
        if not rows:
            L += ["None.", ""]
            continue
        L += ["| Scope | TF | Scenario | n | Win % | Net bp | Train | Test | Test 2x | Best 3 Out | q | Streak |", "|---|---|---|---|---|---|---|---|---|---|---|---|"]
        L += [f"| {t['scope']} | {t['tf']}M | {t['label']} | {t['n']} | {t['win']} | {t['net1']} | {t['train']} | {t['test']} | {t['test2']} | {t['best3_removed']} | {t['q']} | {t['streak']} |" for t in rows]
        L += [""]
    open(os.path.join(OUT, "FINDINGS_ofstudy.md"), "w").write("\n".join(L) + "\n")


def _hist_days() -> int:
    try:
        import ofhistory as H
        return sum((v or {}).get("days", 0) for v in (H.status().get("have") or {}).values())
    except Exception:
        return 0


def _loop(gen):
    st = _J["status"]
    while _J["gen"] == gen:
        st.update(state="running", started=datetime.now().isoformat(timespec="seconds"), err=None)
        try:
            r = run()
            st.update(state="idle", last=r["at"], secs=r["secs"], n_tests=r["n_tests"], counts=r["counts"], stage=None)
        except Exception as e:
            st.update(state="error", err=f"{type(e).__name__}: {e}"[:200])
        nxt = time.time() + EVERY
        st["next"] = datetime.fromtimestamp(nxt).isoformat(timespec="minutes")
        h0 = _hist_days()
        while _J["gen"] == gen and time.time() < nxt and not _J.get("now"):
            time.sleep(10)
            if int(time.time()) % 600 < 10 and _hist_days() - h0 >= 90:
                break                                         # the 12-month rebuild added a month for each coin: run again now
        _J["now"] = False


def start(now=False) -> dict:
    # a reloaded file starts a fresh thread on the new code (the old loop sees the new gen and ends)
    if _J.get("code") != _CODE and _J.get("thread") is not None and _J["thread"].is_alive():
        _J["gen"] += 1
        _J["thread"] = None
    th = _J.get("thread")
    if th is not None and th.is_alive():
        if now:
            _J["now"] = True
        return status()
    _J["gen"] += 1
    th = threading.Thread(target=_loop, args=(_J["gen"],), daemon=True, name="ofstudy")
    _J["thread"] = th
    _J["code"] = _CODE
    th.start()
    return status()


def status() -> dict:
    th = _J.get("thread")
    return {"running": bool(th and th.is_alive()), **(_J.get("status") or {})}


def api(view: str = "") -> dict:
    try:
        res = json.load(open(os.path.join(OUT, "latest.json")))
    except Exception:
        res = {"tests": [], "missing": True}
    res["job"] = status()
    try:
        import ofhistory as H
        res["hist_job"] = H.status()
    except Exception:
        pass
    return res
