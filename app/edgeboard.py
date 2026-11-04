"""
Edge board (2026-10-29, Nikhil). ONE place that lists only what passed a test on data it was not built on, what is still a lead,
and what failed - each with its evidence (project doc), how it is used, and its live record since it went live. Plus the daily data check.
Evidence text is copied from the research docs; nothing here is re-estimated. Add a row only after a test on unseen data.
"""
from __future__ import annotations

import json, os

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
LD = os.path.join(ROOT, "data_store", "livedesk")

EDGES = [
    # ---------------- passed on unseen data -> used
    {"id": "b4h", "tier": "passed", "name": "4H block break + taker delta", "markets": "ETH, BTC (Gold without delta); SOL paper test (unproven)",
     "what": "A block pierced in the first 4H bar, with taker buy/sell in the break direction -> follow at the close; stop back past the near edge; exit after 2 days.",
     "evidence": "2023-26, both halves, after 0.14% costs: ETH +38.5 / +62.1 bps a trade (n 191 / 239), positive every year; BTC +2.8 / +17.3 (3 of 4 years); Gold +21.4 / +9.5 (small sample). Works on fake levels too - the engine is momentum + delta.",
     "use": "Paper cards on Crypto / Metals tabs. Crypto record restarted 29 Oct on v3: with BTC's 24h trend, calm / normal ATR only (2023-26: calm ETH +122 / +155, BTC +70 / +67 bps gross; wild ETH -33 / +19, BTC -58 / +12), ETH / SOL trail 1.5 ATR after +1R, BTC +0.5 ATR room + 3 targets. Skipped signals tracked. Under 30 live trades = too early.", "doc": "claude/edge-board.md", "live": "break4h"},
    {"id": "stress", "tier": "passed", "name": "Stress flags -> half size for trend strategies", "markets": "8 of 10 markets",
     "what": "Any of: volatility stress (VIX / DVOL / ATR jump), global risk-off, crash-news attention -> trend setups at half size.",
     "evidence": "Cut-offs set on 2015-23, judged on 2024-26 with a luck test: trend-strategy average 0.031 -> 0.042R; passed in 2024, 2025 and 2026.",
     "use": "Proposal - advisory only, not yet switched on (needs your approval).", "doc": "claude/weak-points-study.md", "live": None},
    {"id": "prune", "tier": "passed", "name": "Pruned strategy legs ('Run' in the backtester)", "markets": "Crypto, Gold, Silver, US (fails India, NQ)",
     "what": "59 of 269 strategy legs kept, picked on 2015-23.",
     "evidence": "Judged on 2024-26: portfolio risk-adjusted 1.08 -> 5.95 vs a random-pick 95th percentile of 4.15; held every year. Absolute Sharpe inflated - the comparison counts.",
     "use": "Strategy recommendations 'Run' / 'Paper' status.", "doc": "claude/experiments-pruning-intraday.md", "live": None},
    {"id": "size", "tier": "passed", "name": "Regime size + Volatile / Range move size", "markets": "Nifty, Bank Nifty (sizing only)",
     "what": "5-state daily regime sets position size; the Volatile label means bigger next moves, Range smaller.",
     "evidence": "Volatile -> ~1.4x bigger next moves on 5m / 15m / 75m (both indices) and Nifty daily; Range -> ~0.8x on 5m. Direction labels FAILED - size and stop width only.",
     "use": "Regime box size; stop width. Never a direction call.", "doc": "claude/regime-score-v2.md", "live": None},
    {"id": "gap", "tier": "passed", "name": "Gap and event day ranges", "markets": "Nifty",
     "what": "Bigger gap = bigger day range; big gaps (>0.6x avg range) fill the same day only ~25%; RBI 10:00-10:30 range 1.5x; Budget 2.5x; election 8x.",
     "evidence": "Nifty 2021-26, both halves + luck test. FOMC next session NOT bigger.",
     "use": "Overnight prep rules: half size / later entry on big gaps and event days.", "doc": "claude/overnight-prep.md", "live": None},
    {"id": "blocks", "tier": "passed", "name": "Strong blocks hold (where, not which way)", "markets": "Nifty",
     "what": "Strong blocks (price level + big-OI strike, known the evening before) held 77-80% and broke 3-5%.",
     "evidence": "2024-26, same in both halves. But fading them makes ~0 even before costs - use for stops / targets, not entries.",
     "use": "Stops, targets, not selling into strong support / buying into strong resistance.", "doc": "claude/nifty-blocks-no-cost.md", "live": None},
    # ---------------- leads: positive somewhere, not proven -> paper forward test
    {"id": "xsec", "tier": "lead", "name": "Cross-coin momentum basket", "markets": "29 established coins",
     "what": "Daily 00:00 UTC: long the 5 best / short the 5 worst 72 h performers, hold 24 h (market neutral).",
     "evidence": "Jan-Oct 2026, after costs: +11.6 bps a day, Sharpe 1.4, positive in all 4 periods; but about half of 18 settings are near zero - weak evidence.",
     "use": "Paper basket from 29 Oct, forward test 3-4 weeks.", "doc": "claude/alt-edge-search.md", "live": "xsec"},
    {"id": "touchconf", "tier": "lead", "name": "Touch-minute confirmation (bounce at a key level)", "markets": "Nifty, Bank Nifty",
     "what": "Key level touched and the touch minute closes with buyers in control at support / sellers at resistance (delta beyond 0.2) -> enter at that close, exit 30 min later.",
     "evidence": "21-23 Oct look: +16.5 bps at 30 min, 11 of 13 positive, but ~10 independent events and ~25 splits looked at. Replay of touches logged 26-28 Oct (one trade per index): Nifty 8 trades +12.8 bps avg, Bank Nifty 6 trades -5.4.",
     "use": "Paper trades from 30 Oct (Nikhil). Judged by the fixed touch_review test after ~35 sessions.", "doc": "claude/levels-fii-footprint.md", "live": "touchtrades"},
    {"id": "dom", "tier": "lead", "name": "DOM hold / break at a level", "markets": "Nifty, Bank Nifty futures + 48 F&O stocks",
     "what": "Level first, DOM second: price tests a fixed level (blocks / OI walls / yesterday's high-low-close / opening range) and the "
             "20-level book + market orders confirm a hold (bounce) or a break. Target next level (2R+), stop beyond the level, 60 min max.",
     "evidence": "None yet - order flow alone has no edge; as a filter it added ~+0.10R (crypto, real trades). 20-level book lean leads "
                 "Nifty 1-5 min but by less than the cost (3 days). First replay of 3 Nov: about 0R gross, -0.2R after costs (one day).",
     "use": "Paper trades from 3 Nov (Nikhil). Judge after 30-40 sessions: positive after costs in both halves with 30+ trades.",
     "doc": "claude/dom-strategy-test-plan.md", "live": "dom"},
    # ---------------- failed -> context only
    {"id": "reject", "tier": "failed", "name": "Block rejection / held at support (shadow trades)", "markets": "all 50 coins, metals, Nifty",
     "what": "Fade 2+ tests of a block after a close past the neckline.", "evidence": "90 days x 50 coins: 33,597 shorts, 40% wins at 1.5R, -0.3 bps gross; longs -0.5; 3R about 0. No condition passed validation.",
     "use": "Dropped for good on 3 Nov with Block story (Nikhil). Research record only.", "doc": "claude/alt-edge-search.md", "live": None},
    {"id": "dir", "tier": "failed", "name": "Regime / timeframe direction, impact scores, Verdict", "markets": "all",
     "what": "Trend direction labels, daily score, impact score, 5m/15m verdict.", "evidence": "Direction labels failed on 75m / daily / weekly; the impact-score weight test found no section that predicted the next 5 days.",
     "use": "Context only.", "doc": "claude/regime-score-v2.md", "live": None},
    {"id": "other", "tier": "failed", "name": "CRT, breakouts, big-candle fades, pullback (50 coins), tracker setup cards", "markets": "crypto, gold, indices",
     "what": "Candle-range raids, 24 h breakouts, fading big 5-min bars, live-tracker setups.",
     "evidence": "CRT ~0 on every market; breakouts follow market direction only; fades -7 to -43 bps; tracker setups India -0.05R, crypto -0.12R net.",
     "use": "Not traded. Tracker setup cards and the 'pullback after a big-volume move' shadow trades dropped for good on 3 Nov (Nikhil).", "doc": "claude/crt-backtest-btc.md", "live": None},
]


def _break4h():
    try:
        t = json.load(open(os.path.join(LD, "break4h", "trades.json")))
    except Exception:
        return None
    v2 = [x for x in t if x.get("rule") == "skip"]; v2c = [x for x in v2 if x.get("status") == "closed"]
    t = [x for x in t if x.get("rule") == "v3" or (x.get("mkt") == "GOLD" and x.get("rule", "v1") == "v1")]
    cl = [x for x in t if x.get("status") == "closed"]
    return {"v2": {"n": len(v2c), "open": sum(1 for x in v2 if x.get("status") == "open"), "avg_bps": round(sum(x["bps"] for x in v2c) / len(v2c), 1) if v2c else None,
                   "R": round(sum(x["R"] for x in v2c), 2) if v2c else None, "since": "29 Oct", "label": "skipped signals (against trend / wild ATR) - what they would have made"},
            "n": len(cl), "open": sum(1 for x in t if x.get("status") == "open"), "win": round(100 * sum(x.get("bps", 0) > 0 for x in cl) / len(cl)) if cl else None,
            "avg_bps": round(sum(x.get("bps", 0) for x in cl) / len(cl), 1) if cl else None, "avg_net_bps": round(sum(x.get("bps_net", 0) for x in cl) / len(cl), 1) if cl else None,
            "R": round(sum(x.get("R", 0) for x in cl), 2) if cl else None, "since": "29 Oct (crypto v3; gold v1 since 24 Oct)"}


def api():
    live = {}
    for e in EDGES:
        try:
            if e["live"] == "break4h": live[e["id"]] = _break4h()
            elif e["live"] == "xsec":
                import xsecpaper as X; live[e["id"]] = X.record()
            elif e["live"] == "dom":
                import livedesk as LD
                r = LD._hot("domcards").api()["record"]
                live[e["id"]] = {"n": r["all"]["n"], "open": None, "win": r["all"].get("win_pct"), "avg_r_gross": r["all"].get("avg_r_gross"),
                                 "avg_r": r["all"].get("avg_r"), "sessions": r["sessions"], "since": r["since"]}
            elif e["live"] == "touchtrades":
                import touchtrades as TT
                allc = [t for d in sorted(os.listdir(TT.TDIR)) if d.endswith(".json") and d[:-5] >= TT.LIVE_FROM for t in TT.trades_for(d[:-5])]
                cl = [t for t in allc if t["status"] == "closed"]
                live[e["id"]] = {"n": len(cl), "open": sum(t["status"] == "open" for t in allc), "win": round(100 * sum(t["bps"] > 0 for t in cl) / len(cl)) if cl else None,
                                 "avg_bps": round(sum(t["bps"] for t in cl) / len(cl), 1) if cl else None, "since": "30 Oct"}
        except Exception as ex:
            live[e["id"]] = {"error": str(ex)[:100]}
    return {"edges": EDGES, "live": live,
            "rule": "A row moves up to 'passed' only after a test on data it was not built on; going live needs your approval. Indications only."}
