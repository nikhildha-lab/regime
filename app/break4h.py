"""
4-hour block-break trades - PAPER ONLY (2026-10-24, approved by Nikhil). Never places orders.

Rule (tested 2023-26 in scripts/research/blocks_xmkt.py, report data_store/research/blocks_xmkt/REPORT.md):
  map   : blocks from the last 30 days - prev week / month high-low, prev day high / low / close, 30-day volume profile
          (POC, VAH, VAL, high-volume nodes), daily swing highs / lows, round numbers; density peaks, band 0.4 %
  signal: a 4H bar enters a block from outside AND trades beyond its far edge in the same bar
          crypto also needs real taker volume in the break direction (buyers for an up-break, sellers for a down-break,
          net >= 5 % of the bar's volume)
  trade : enter at that bar's close WITH the break; stop just back past the near edge (+0.04 %); exit after 12 bars (2 days)
  graded: R and bps after 0.14 % round-trip cost
Tested: ETH net +38 / +62 bps a trade (both halves, every year), BTC +3 / +17, GOLD +21 / +10 (no taker data; levels
mattered on gold, not on BTC). Win rate 20-30 %: expect long losing streaks.
Data: crypto 4H + daily klines from Binance futures (fapi), gold from GC=F 1h (data_store/market_data/synaptic_crypto_gold.csv).
"""
from __future__ import annotations

import json
import os
import time
import urllib.request

import numpy as np
import pandas as pd

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
DIR = os.path.join(ROOT, "data_store", "livedesk", "break4h")
GOLD_CSV = os.path.join(ROOT, "data_store", "market_data", "synaptic_crypto_gold.csv")
MKTS = {"ETH": "ETHUSDT", "BTC": "BTCUSDT", "GOLD": None}      # SOL removed 2026-10-30 (Nikhil: stop SOL trades)
# 2026-11-04 (Nikhil: "add for other coins and keep recording"): the same v3 rule as a FORWARD PAPER TEST on the other coins.
# NOT backtested on these coins -> proven False, shown "unproven"; uses ETH's exit (block stop, trail 1.5 ATR after +1R).
ALTS = {"ZEC": "ZECUSDT", "DASH": "DASHUSDT"}      # 2026-11-06 (Nikhil: "retire and fully remove"): XRP, BNB, LINK, UNI retired (negative 90-day check)
MKTS.update(ALTS)
ROUND = {"BTC": 1000, "ETH": 50, "SOL": 5, "GOLD": 50}


def _rstep(mkt, spot):
    """Round-number step; coins without a set step get a 1 / 2 / 2.5 / 5 x 10^k step near 2.5 % of price."""
    if mkt in ROUND:
        return ROUND[mkt]
    tgt = spot * 0.025; k = 10 ** np.floor(np.log10(tgt))
    return float(min((m * k for m in (1, 2, 2.5, 5, 10)), key=lambda v: abs(v - tgt)))
BAND, COST, HOLD = 0.004, 14.0, 12
# v2 (2026-10-29, Nikhil: "make the improvements on the logic, also change the trend logic") - tested in
# scripts/research/b4h_exit_study.py + b4h_trail_study.py on the same 2023-26 trades (5-min replay), picked after seeing the data,
# so v1 keeps running beside it as the reference record:
#   trend : ETH and BTC trade only WITH BTC's 24 h trend (BTC close vs 6 completed 4H bars earlier). Against-trend breaks lost in
#           both halves on both coins (ETH -72 / -14 bps, BTC -78 / -94); with-trend ETH +73 / +92, BTC +20 / +45 (gross).
#   stop  : ETH the block stop (as v1); BTC the block stop + 0.5 x ATR(14, 4H) of room (BTC +28 / +61 vs +14 / +36).
#   exit  : ETH trail 1.5 x ATR behind the best 4H close once the trade is +1R; BTC 3 targets 1R / 2R / 3R (thirds), stop to
#           breakeven after T1 and to T1 after T2. Both: out after 2 days (12 bars). A pure trailing stop with no block stop was worse.
#   grade : A+ when the taker delta is beyond 0.10 (ETH +107 / +201 bps), tracked, not a filter.
V2 = {"ETH": {"room": 0.0, "exit": "trail", "trail": 1.5}, "BTC": {"room": 0.5, "exit": "targets"}}
# v3 = the ONLY crypto rule from 29 Oct 2026 (Nikhil: "with best conditions for BTC, ETH and SOL .. clean up the old trade logs
# and start recording with the latest logic"). Old crypto v1 / v2 rows moved to trades_archive_crypto_2026-10-29.json. Gold stays v1.
#   = v2 stops / exits + BTC 24 h trend filter + ATR mood filter: ATR(14, 4H) as % of price ranked against its previous 180 days;
#     'wild' (top third) is skipped (scripts/research/b4h_atr_regime.py: v1 wild ETH -33 / +19, BTC -58 / +12, SOL +50 / -93 bps vs
#     calm ETH +122 / +155, BTC +70 / +67, SOL +40 / +41).
#   SOL: paper test only, UNPROVEN (b4h_sol_study.py failed overall); uses ETH's exit (block stop, trail 1.5 ATR after +1R).
#   Skipped signals (against the trend / wild ATR) are still recorded as rule 'skip' and graded the same way, so the filters
#   can be checked forward. They are never counted in the v3 record.
V3 = {"ETH": {"room": 0.0, "exit": "trail", "trail": 1.5, "proven": True}, "BTC": {"room": 0.5, "exit": "targets", "proven": True},
      }
V3.update({k: {"room": 0.0, "exit": "trail", "trail": 1.5, "proven": False} for k in ALTS})   # 2026-11-04 forward test
# 2026-11-05 (Nikhil: "SOL with targets or trailing ... both" -> "ok add .. SOL we improved"): SOL back as a v3 market with a NEW exit
# 'h3' = HALF booked at 2R; the other half trails 1.5 ATR behind the best 4H close once +1R; after +1R the stop is never below entry;
# out after 2 days. History (scripts/research/b4h_hybrid.py, 162 SOL trades 2023-26, v3 conditions, net of 0.14 %): +0.28 % a trade,
# halves +0.24 / +0.31, worst drawdown -22 % (pure trail +0.37 %, -28 %; targets +0.10 %). Other markets' rules are unchanged.
MKTS["SOL"] = "SOLUSDT"
# 2026-11-05 (Nikhil: "can we start testing all metals and commodities"): the Gold v1 signal (no taker data) as a FORWARD PAPER TEST on
# the other futures, live 4H bars from the Yahoo 1h file (data_store/market_data/synaptic_intraday_all.csv), each with the exit that was
# best in scripts/research/b4h_commod.py (May 2024 - Nov 2026, net of 0.14 %). Only Silver held in both halves; the rest are records only.
INTRA_CSV = os.path.join(ROOT, "data_store", "market_data", "synaptic_intraday_all.csv")
COMMOD = {"SILVER": ("SI=F", "trail", True,  "history: 92 trades, +0.19 % a trade, halves +0.19 / +0.20 (trail)"),
          "NATGAS": ("NG=F", "h3",    False, "history mixed: 140 trades, +0.12 % a trade, halves +0.00 / +0.24 (H3)"),
          "COPPER": ("HG=F", "targets", False, "history negative: 69 trades, -0.10 % a trade (targets, least bad)"),
          "ALUMINUM": ("ALI=F", "h3",  False, "history negative: 43 trades, -0.13 % a trade (H3, least bad)"),
          "CRUDE": ("CL=F", "targets", False, "history negative: 99 trades, -0.30 % a trade (targets, least bad)")}
MKTS.update({k: None for k in COMMOD})
V3["SOL"] = {"room": 0.0, "exit": "h3", "trail": 1.5, "proven": True}
# 2026-11-05: BTC H2 = a record-only version on every BTC v3 trade (the live BTC trade keeps its 3 targets): HALF at 1R, stop to
# breakeven, the other half trails 1.5 ATR; 2 days. History: +0.32 % vs targets +0.28 % a trade, halves +0.14 / +0.54 vs +0.10 / +0.50,
# worst drawdown -13.7 % vs -15.0 %.
BTC_H2 = "BTC H2 · half at 1R + trail"
# 2026-11-05 (Nikhil: "enhance the logic for BTC and ETH wherever possible"): v4 = RECORD-ONLY versions beside v3 (v3 unchanged).
# scripts/research/b4h_enhance.py, 2023-26, picked AFTER a one-at-a-time grid (only ideas better in both halves with >= 50 % of trades):
#   ETH v4: only weekday entries (UTC) + BTC 24 h move >= 1 % with the trade + hold 3 days (trail 1.5 ATR as v3)
#           +1.35 % a trade vs +1.04 % (halves +1.30 / +1.37 vs +0.94 / +1.10) but 139 trades vs 222 -> total +187 % vs +231 %
#   BTC v4: BTC 24 h move >= 1 % with the trade + stop room 1.0 ATR (v3 0.5) + H2 exit (half at 1R, stop to entry, rest trails 1.5 ATR)
#           +0.61 % a trade vs +0.28 % (halves +0.28 / +1.01 vs +0.10 / +0.50), 140 trades vs 170 -> total +85 % vs +47 %
# Chosen after seeing ~15 ideas, so some of the gain can be luck: judged on live trades before anything replaces v3.
V4 = {"ETH": {"label": "ETH v4 · weekday + trend 1% + 3 days", "trend": 0.01, "weekday": True, "hold": 18},
      "BTC": {"label": "BTC v4 · trend 1% + 1.0 ATR room + H2", "trend": 0.01, "weekday": False, "hold": HOLD}}


def _v4(r, after):
    """None if the v3 trade does not qualify for v4; else the v4 record (same entry, own stop / exit)."""
    cfg = V4.get(r["mkt"]); s_ = r["side"]; b24 = r.get("btc_24h_pct")
    if not cfg or b24 is None or b24 / 100 * s_ < cfg["trend"]:
        return None
    if cfg["weekday"] and (pd.Timestamp(r["bar"]) + pd.Timedelta(hours=4)).weekday() >= 5:
        return None
    a = r.get("atr") or 0
    if r["mkt"] == "BTC":
        st = r["stop"] - s_ * 0.5 * a                         # v3 BTC stop already has 0.5 ATR room -> 1.0 ATR in total
        v = {"entry": r["entry"], "side": s_, "stop": st, "risk": abs(r["entry"] - st), "atr": a, "trail_atr": 1.5, "exit_mode": "h2", "status": "open"}
    else:
        v = {"entry": r["entry"], "side": s_, "stop": r["stop"], "risk": r["risk"], "atr": a, "trail_atr": 1.5, "exit_mode": "trail", "status": "open"}
    _manage(v, after, hold=cfg["hold"])
    keep = ("status", "exit", "exit_reason", "bps", "bps_net", "R", "R_net", "cur_stop", "hit", "closed_bar", "trailing", "stop", "risk")
    return {k2: (round(v[k2], 2) if isinstance(v.get(k2), float) else v.get(k2)) for k2 in keep}


def _atr_rank(done, j):
    """ATR % of price at bar j ranked against the previous 180 days (1079 bars); None if not enough history."""
    h, l, c = done.high.values, done.low.values, done.close.values
    tr = np.maximum(h - l, np.maximum(abs(h - np.r_[c[0], c[:-1]]), abs(l - np.r_[c[0], c[:-1]])))
    p = pd.Series(tr).ewm(alpha=1 / 14, adjust=False).mean().values / c
    w = p[max(0, j - 1079):j + 1]
    return float((w[:-1] < w[-1]).mean()) if len(w) >= 540 else None


def _mood(rk):
    return None if rk is None else "calm" if rk <= 1 / 3 else "normal" if rk <= 2 / 3 else "wild"


def _atr(done, j):
    h, l, c = done.high.values, done.low.values, done.close.values
    tr = np.maximum(h - l, np.maximum(abs(h - np.r_[c[0], c[:-1]]), abs(l - np.r_[c[0], c[:-1]])))
    return float(pd.Series(tr).ewm(alpha=1 / 14, adjust=False).mean().iloc[j])


def _btc_trend(t_last):
    """BTC 24 h return to the close of the 4H bar t_last (completed bars only); None if unknown."""
    try:
        b = bars("BTC")[0]; b = b[b.done & (b.index <= t_last)]
        return float(b.close.iloc[-1] / b.close.iloc[-7] - 1) if len(b) > 7 else None
    except Exception:
        return None


def _close(r, ex_px, why, t_):
    """Finish a trade; pnl_pts already holds closed parts (thirds) for v2 targets."""
    left = r.get("left", 1.0)
    pts = r.get("pnl_pts", 0.0) + left * r["side"] * (ex_px - r["entry"])
    bps = pts / r["entry"] * 1e4
    r.update(status="closed", exit=round(ex_px, 2), exit_reason=why, closed_bar=str(t_), left=0.0, pnl_pts=round(pts, 4),
             bps=round(bps, 1), bps_net=round(bps - COST, 1), R=round(pts / r["risk"], 2),
             R_net=round((pts - r["entry"] * COST / 1e4) / r["risk"], 2))


def _manage_hybrid(r, after, hold=None):
    """2026-11-05: 'h2' (half at 1R, stop to breakeven, rest trails) and 'h3' (half at 2R, rest trails from +1R, stop never below
    entry after +1R). Same order as the research (b4h_hybrid.py): stop first (4H high / low), then the target, then the trail on the close."""
    mode, s, e, R_ = r["exit_mode"], r["side"], r["entry"], r["risk"]; a = r.get("atr") or 0; k_tr = r.get("trail_atr") or 1.5
    tgt_r = 1.0 if mode == "h2" else 2.0
    for n_, (t_, x) in enumerate(after.iterrows(), 1):
        cur = r.get("cur_stop", r["stop"])
        if (s < 0 and x.high >= cur) or (s > 0 and x.low <= cur):
            why = "stop" if cur == r["stop"] else ("breakeven stop" if abs(cur - e) < 1e-9 else "trailing stop")
            _close(r, cur, why, t_); return True
        tg = e + s * tgt_r * R_
        if not r.get("hit") and ((s > 0 and x.high >= tg) or (s < 0 and x.low <= tg)):
            r["pnl_pts"] = 0.5 * s * (tg - e); r["left"] = 0.5; r["hit"] = 1
            if mode == "h2":
                cur = max(cur, e) if s > 0 else min(cur, e)
        best = max(r.get("best", e), x.close) if s > 0 else min(r.get("best", e), x.close); r["best"] = best
        on = (mode == "h3" and s * (best - e) >= R_) or (mode == "h2" and r.get("hit"))
        if on:
            nt = best - s * k_tr * a
            cur = max(cur, nt) if s > 0 else min(cur, nt); r["trailing"] = True
            if mode == "h3":
                cur = max(cur, e) if s > 0 else min(cur, e)
        r["cur_stop"] = cur
        if n_ >= (hold or HOLD):
            _close(r, x.close, f"{(hold or HOLD) // 6} days", t_); return True
    return False


def _manage(r, after, hold=None):
    """Walk completed 4H bars after entry. Stop checked first in each bar (conservative). hold = bars (default HOLD = 2 days)."""
    mode = r.get("exit_mode", "fixed"); s = r["side"]
    if mode in ("h2", "h3"):
        return _manage_hybrid(r, after, hold)
    for n_, (t_, x) in enumerate(after.iterrows(), 1):
        cur = r.get("cur_stop", r["stop"])
        if (s < 0 and x.high >= cur) or (s > 0 and x.low <= cur):
            why = "stop" if mode == "fixed" or cur == r["stop"] else ("trailing stop" if mode == "trail" else ("breakeven stop" if r.get("hit") == 1 else f"stop at T{r.get('hit', 1) - 1}"))
            _close(r, cur, why, t_); return True
        if mode == "targets":
            for k in range(r.get("hit", 0), 3):
                tg = r["targets"][k]
                if (s > 0 and x.high >= tg) or (s < 0 and x.low <= tg):
                    r["pnl_pts"] = r.get("pnl_pts", 0.0) + (1 / 3) * s * (tg - r["entry"]); r["left"] = round(r.get("left", 1.0) - 1 / 3, 6)
                    r["hit"] = k + 1; r["cur_stop"] = r["entry"] if k == 0 else r["targets"][k - 1]
                else:
                    break
            if r.get("left", 1.0) <= 1e-6:
                _close(r, r["targets"][2], "T3", t_); return True
        if mode == "trail":
            best = max(r.get("best", r["entry"]), x.close) if s > 0 else min(r.get("best", r["entry"]), x.close)
            r["best"] = best
            if s * (best - r["entry"]) >= r["risk"]:
                nt = best - s * r["trail_atr"] * r["atr"]
                r["cur_stop"] = max(cur, nt) if s > 0 else min(cur, nt); r["trailing"] = True
        if n_ >= (hold or HOLD):
            _close(r, x.close, f"{(hold or HOLD) // 6} days", t_); return True
    return False
_C: dict = {}

# Record-only versions on every v3 trade (2026-10-30, Nikhil "ok" after the stop / trail study, b4h_stop3.py): they never change
# the trade. Compared after ~30 trades. Same 4H-bar management as the trade itself (stop first in each bar), max 2 days.
VARIANTS = {"2% stop, trail 1.5 ATR": (0.02, "trail", 1.5), "2% stop, trail 3 ATR": (0.02, "trail", 3.0), "block stop, trail 3 ATR": (None, "trail", 3.0)}


# 2026-11-05 (Nikhil: "I need rules for trailing logic else targets ... how will I book profits"): GOLD v2 = a NEW record-only version
# on every Gold v1 trade (v1 itself unchanged): 3 targets 1R / 2R / 3R, a third each (hit by a 4H high / low), stop to breakeven after
# T1 and to T1 after T2, the rest out after 2 days. History (scripts/research/b4h_exit_v4.py, 32 rebuilt Gold trades 2024-26):
# v1 net -0.08 % a trade, v2 +0.05 %, worst drawdown -10.3 % -> -4.8 %, win 34 % -> 53 %; halves mixed (-0.13 / +0.24 vs -0.06 / -0.11). Too few to judge.
GOLD_V2 = "Gold v2 · 3 targets 1R/2R/3R"


def _btc_h2(r, after):
    v = {"entry": r["entry"], "side": r["side"], "stop": r["stop"], "risk": r["risk"], "atr": r.get("atr") or 0, "trail_atr": 1.5,
         "exit_mode": "h2", "status": "open"}
    _manage(v, after)
    keep = ("status", "exit", "exit_reason", "bps", "bps_net", "R", "R_net", "cur_stop", "hit", "closed_bar", "trailing")
    return {k2: (round(v[k2], 2) if isinstance(v.get(k2), float) else v.get(k2)) for k2 in keep}


def _gold_v2(r, after):
    e, s_, st = r["entry"], r["side"], r["stop"]; R_ = abs(e - st)
    v = {"entry": e, "side": s_, "stop": st, "risk": R_, "exit_mode": "targets", "targets": [round(e + s_ * k * R_, 4) for k in (1, 2, 3)], "status": "open"}
    _manage(v, after)
    keep = ("status", "exit", "exit_reason", "bps", "bps_net", "R", "R_net", "cur_stop", "hit", "targets", "closed_bar")
    return {k2: (round(v[k2], 2) if isinstance(v.get(k2), float) else v.get(k2)) for k2 in keep}


def _variants(r, after):
    out = {}
    for nm, (pct, mode, k) in VARIANTS.items():
        e, s_ = r["entry"], r["side"]
        st = e * (1 - s_ * pct) if pct else r["stop"]
        v = {"entry": e, "side": s_, "stop": st, "risk": abs(e - st), "exit_mode": mode, "trail_atr": k, "atr": r.get("atr") or 0, "status": "open"}
        _manage(v, after)
        out[nm] = {k2: (round(v[k2], 2) if isinstance(v.get(k2), float) else v.get(k2)) for k2 in ("status", "exit", "exit_reason", "bps", "bps_net", "R", "cur_stop")}
    return out


def _klines(sym, interval, limit):
    url = f"https://fapi.binance.com/fapi/v1/klines?symbol={sym}&interval={interval}&limit={limit}"
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "MarketPulse"}), timeout=15) as r:
        rows = json.loads(r.read())
    d = pd.DataFrame(rows).iloc[:, :11]
    d.columns = ["t", "open", "high", "low", "close", "volume", "tc", "qv", "n", "tbv", "tbq"]
    d["t"] = pd.to_datetime(d.t, unit="ms")
    d = d.set_index("t")[["open", "high", "low", "close", "volume", "tbv"]].astype(float)
    return d


def bars(mkt):
    """(4H bars incl. 'delta' and 'done' flag, daily bars). Cached 4 min."""
    c = _C.get(("bars", mkt))
    if c and time.time() - c[0] < 240:
        return c[1]
    if MKTS[mkt]:
        b4 = _klines(MKTS[mkt], "4h", 1500); d1 = _klines(MKTS[mkt], "1d", 80)
        b4["delta"] = (2 * b4.tbv - b4.volume) / b4.volume.replace(0, np.nan)
        b4["done"] = b4.index + pd.Timedelta(hours=4) <= pd.Timestamp.utcnow().tz_localize(None)
    else:
        ysym = "GC=F" if mkt == "GOLD" else COMMOD[mkt][0]                # 2026-11-05: metals / commodities from the Yahoo 1h file
        g = pd.read_csv(GOLD_CSV if mkt == "GOLD" else INTRA_CSV, usecols=["datetime_utc", "symbol", "interval", "open", "high", "low", "close", "volume"])
        g = g[(g.symbol == ysym) & (g.interval == "1h")]
        g["t"] = pd.to_datetime(g.datetime_utc); g = g.set_index("t").sort_index()[["open", "high", "low", "close", "volume"]].astype(float)
        g = g[g.index >= g.index.max() - pd.Timedelta(days=90)]
        b4 = g.resample("4h").agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}).dropna(subset=["close"])
        cnt = g.close.resample("4h").count().reindex(b4.index)
        b4["delta"] = np.nan
        b4["done"] = (b4.index + pd.Timedelta(hours=4) <= g.index.max() + pd.Timedelta(hours=1)) & (cnt >= 3)
        d1 = g.resample("1D").agg({"high": "max", "low": "min", "close": "last"}).dropna()
    _C[("bars", mkt)] = (time.time(), (b4, d1))
    return b4, d1


def build_map(mkt, b4, d1, day):
    """Blocks for one UTC day, from data before that day (same as the research map)."""
    hist = b4[(b4.index >= day - pd.Timedelta(days=30)) & (b4.index < day)]
    if len(hist) < 20:
        return []
    spot = hist.close.iloc[-1]; items = []
    dd = d1[d1.index < day]
    items += [(dd.high.iloc[-1], 2, "price"), (dd.low.iloc[-1], 2, "price"), (dd.close.iloc[-1], 1, "price")]
    for z in (dd[dd.index.to_period("W") == day.to_period("W") - 1], dd[dd.index.to_period("M") == day.to_period("M") - 1]):
        if len(z):
            items += [(z.high.max(), 3, "alert"), (z.low.min(), 3, "alert")]
    step = spot * BAND / 3
    tp = ((hist.high + hist.low + hist.close) / 3 / step).round() * step
    prof = hist.volume.groupby(tp.values).sum().sort_index()
    if len(prof) > 5:
        sm = prof.rolling(5, center=True, min_periods=1).mean(); poc = sm.idxmax()
        order = prof.sort_values(ascending=False); vaz = order[(order.cumsum() / order.sum()) <= 0.7].index
        items += [(poc, 2.5, "price"), (max(vaz), 1.5, "price"), (min(vaz), 1.5, "price")]
        for i in range(3, len(sm) - 3):
            if sm.iloc[i] == sm.iloc[i - 3:i + 4].max() and sm.iloc[i] > 1.3 * sm.mean() and abs(sm.index[i] / poc - 1) > 3 * BAND:
                items.append((sm.index[i], 1.5, "price"))
    sw = dd[dd.index >= day - pd.Timedelta(days=30)]; n = len(sw)
    for j in range(1, n - 1):
        w = 0.5 + 0.5 * j / n
        if sw.high.iloc[j] >= sw.high.iloc[j - 1] and sw.high.iloc[j] >= sw.high.iloc[j + 1]: items.append((sw.high.iloc[j], w, "price"))
        if sw.low.iloc[j] <= sw.low.iloc[j - 1] and sw.low.iloc[j] <= sw.low.iloc[j + 1]: items.append((sw.low.iloc[j], w, "price"))
    lo_r, hi_r = spot * 0.9, spot * 1.1
    rs = _rstep(mkt, spot)
    for r in np.arange(np.ceil(lo_r / rs) * rs, hi_r, rs):
        items.append((r, 1, "round"))
    items = [x for x in items if np.isfinite(x[0]) and lo_r <= x[0] <= hi_r]     # 2026-11-03 audit: NaN levels were dropped silently by numpy
    P = np.array([x[0] for x in items]); Wt = np.array([x[1] for x in items]); K = [x[2] for x in items]
    grid = np.arange(lo_r, hi_r, step); bd = spot * BAND
    with np.errstate(all="ignore"):                    # macOS Accelerate BLAS prints spurious matmul warnings (results are fine)
        score = (np.abs(P[None, :] - grid[:, None]) <= bd).astype(float) @ Wt
    peaks = []
    for i in np.argsort(-score):
        if score[i] < 2.5 or len(peaks) >= 25:
            break
        if all(abs(grid[i] - grid[j]) >= 4 * bd for j in peaks):
            peaks.append(i)
    out = []
    for i in sorted(peaks, key=lambda i: grid[i]):
        pk = score[i]; lo = hi = i
        while lo > 0 and score[lo - 1] >= 0.8 * pk: lo -= 1
        while hi < len(grid) - 1 and score[hi + 1] >= 0.8 * pk: hi += 1
        kinds = {K[q] for q in np.where((P >= grid[lo] - bd) & (P <= grid[hi] + bd))[0]}
        out.append({"lo": float(grid[lo] - bd / 2), "hi": float(grid[hi] + bd / 2), "score": round(float(pk), 1),
                    "strength": "strong" if pk >= 8 else "medium" if pk >= 5 else "weak", "alert": "alert" in kinds})
    return out


def _load():
    try:
        return json.load(open(os.path.join(DIR, "trades.json")))
    except Exception:
        return []


def _save(rows):
    os.makedirs(DIR, exist_ok=True)
    tmp = os.path.join(DIR, "trades.json.tmp")
    json.dump(rows, open(tmp, "w"), indent=1, default=str)
    os.replace(tmp, os.path.join(DIR, "trades.json"))


def tick(force=False) -> dict:
    """Check every market on completed 4H bars. Cheap: runs fully at most once a minute."""
    if not force and time.time() - _C.get("tick", 0) < 60:
        return {"skipped": True}
    _C["tick"] = time.time()
    rows, changed, status = _load(), False, {}
    for mkt in MKTS:
        try:
            b4, d1 = bars(mkt)
        except Exception as e:
            status[mkt] = f"data error: {e}"[:120]; continue
        done = b4[b4.done]
        if len(done) < 30:
            status[mkt] = "not enough bars"; continue
        # record-only versions (v3 trades; keep updating until all versions are closed)
        for r in rows:
            if r["mkt"] != mkt or r.get("rule") != "v3" or r.get("var_done"):
                continue
            vv = _variants(r, done[done.index > pd.Timestamp(r["bar"])])
            if vv != r.get("var"):
                r["var"] = vv; changed = True
            if all(x["status"] == "closed" for x in vv.values()):
                r["var_done"] = True; changed = True
        # Gold v2 (targets) - record-only, never changes the v1 trade
        for r in rows:
            if mkt != "GOLD" or r["mkt"] != "GOLD" or r.get("rule", "v1") != "v1" or (r.get("gv2_done") and r.get("gv2_fix")):
                continue                                     # gv2_fix: recompute rows graded before the 5 Nov fix (wrong market's bars)
            gv = _gold_v2(r, done[done.index > pd.Timestamp(r["bar"])])
            if gv != r.get("gv2") or not r.get("gv2_fix"):
                r["gv2"] = gv; r["gv2_fix"] = 1; r["gv2_done"] = gv["status"] == "closed"; changed = True
            if gv["status"] == "closed":
                r["gv2_done"] = True; changed = True
        # v4 (ETH / BTC) - record-only, never changes the v3 trade
        for r in rows:
            if mkt not in V4 or r["mkt"] != mkt or r.get("rule") != "v3" or r.get("v4_done"):
                continue
            vv4 = _v4(r, done[done.index > pd.Timestamp(r["bar"])])
            if vv4 is None:
                if r.get("v4") is not False:
                    r["v4"] = False; r["v4_done"] = True; changed = True
                continue
            if vv4 != r.get("v4"):
                r["v4"] = vv4; r["v4_done"] = vv4["status"] == "closed"; changed = True
        # BTC H2 - record-only, never changes the BTC trade
        for r in rows:
            if mkt != "BTC" or r["mkt"] != "BTC" or r.get("rule") != "v3" or r.get("h2_done"):
                continue
            hv = _btc_h2(r, done[done.index > pd.Timestamp(r["bar"])])
            if hv != r.get("h2"):
                r["h2"] = hv; r["h2_done"] = hv["status"] == "closed"; changed = True
        # manage open trades
        for r in rows:
            if r["mkt"] != mkt or r["status"] != "open":
                continue
            after = done[done.index > pd.Timestamp(r["bar"])]
            st0 = json.dumps(r, sort_keys=True, default=str)
            if r.get("exit_mode", "fixed") != "fixed":
                for k_ in ("cur_stop", "best", "hit", "left", "pnl_pts", "trailing"):   # replay from the entry every time (deterministic)
                    r.pop(k_, None)
            _manage(r, after)
            if json.dumps(r, sort_keys=True, default=str) != st0:
                changed = True
        # new signal on the last completed bar
        j = len(done) - 1; t_last = done.index[j]
        key = f"{mkt}|{t_last}"
        if key in {r["sig"].replace("|v2", "").replace("|v3", "").replace("|skip", "") for r in rows} or _C.get(("checked", mkt)) == str(t_last):
            status[mkt] = f"checked bar {t_last:%d %b %H:%M} UTC"; continue
        _C[("checked", mkt)] = str(t_last)
        day = t_last.normalize()
        bl = build_map(mkt, b4, d1, day)
        _C[("map", mkt)] = bl
        O, Hh, L, Cc = done.open.values, done.high.values, done.low.values, done.close.values
        sig = None
        for b in bl:
            side = 1 if Cc[j - 1] > b["hi"] and L[j] <= b["hi"] else -1 if Cc[j - 1] < b["lo"] and Hh[j] >= b["lo"] else 0
            if not side:
                continue
            if any((Cc[k - 1] > b["hi"] and L[k] <= b["hi"]) or (Cc[k - 1] < b["lo"] and Hh[k] >= b["lo"]) for k in range(max(1, j - 3), j)):
                continue                                     # same block visited in the last 3 bars
            far, near = (b["lo"], b["hi"]) if side > 0 else (b["hi"], b["lo"])
            if side * ((L[j] if side > 0 else Hh[j]) - far) >= 0:
                continue                                     # not pierced in this bar
            dl = done.delta.iloc[j]
            if MKTS[mkt] and not (pd.notna(dl) and dl * side < -0.05):
                status[mkt] = f"{t_last:%d %b %H:%M}: block {b['lo']:.0f}-{b['hi']:.0f} pierced, taker volume not with the break - no trade"
                continue
            e = Cc[j]; stop = near + side * 0.1 * BAND * e
            if side * (stop - e) <= 0:
                continue
            sig = {"sig": key, "rule": "v1", "mkt": mkt, "bar": str(t_last), "status": "open", "side": -side,
                   "dir": "SHORT - support block broke" if side > 0 else "LONG - resistance block broke",
                   "entry": round(e, 2), "stop": round(stop, 2), "risk": round(abs(stop - e), 2), "risk_bps": round(abs(stop - e) / e * 1e4, 1),
                   "block": {k: (round(v, 2) if isinstance(v, float) else v) for k, v in b.items()},
                   "taker_delta": None if pd.isna(dl) else round(float(dl), 3), "exit_by": str(t_last + pd.Timedelta(hours=4 * (HOLD + 1))),
                   "created": pd.Timestamp.utcnow().strftime("%Y-%m-%d %H:%M UTC")}
            break
        if sig and mkt in COMMOD:                            # 2026-11-05: commodity exit (v1 signal kept as rule 'v1')
            _ys, ex_, pr_, note_ = COMMOD[mkt]; a_ = _atr(done, j); e_, ts_, R_ = sig["entry"], sig["side"], sig["risk"]
            sig.update(exit_mode=ex_, atr=round(a_, 4), trail_atr=1.5, proven=pr_, hist=note_)
            if ex_ == "targets":
                sig["targets"] = [round(e_ + ts_ * k * R_, 4) for k in (1, 2, 3)]
        if sig and mkt in V3:                                # crypto: v3 only
            cfg = V3[mkt]; a = _atr(done, j); e = sig["entry"]; ts = sig["side"]
            tr = _btc_trend(t_last); rk = _atr_rank(done, j); md = _mood(rk)
            why = []
            if tr is None or tr * ts <= 0: why.append(f"against BTC 24h trend ({'n/a' if tr is None else f'{tr*100:+.2f}%'})")
            if md is None: why.append("ATR history n/a")
            elif md == "wild": why.append(f"wild ATR (rank {rk*100:.0f}%)")
            stop = sig["stop"] - ts * cfg["room"] * a; R = abs(e - stop)
            v3 = {**sig, "sig": key + ("|skip" if why else "|v3"), "rule": "skip" if why else "v3", "stop": round(stop, 2), "risk": round(R, 2),
                  "risk_bps": round(R / e * 1e4, 1), "atr": round(a, 4), "atr_rank": None if rk is None else round(rk, 2), "mood": md,
                  "btc_24h_pct": None if tr is None else round(tr * 100, 2), "exit_mode": cfg["exit"], "proven": cfg["proven"],
                  "grade": "A+" if sig["taker_delta"] is not None and abs(sig["taker_delta"]) > 0.10 else "A"}
            if why: v3["skip_reason"] = " + ".join(why)
            if cfg["exit"] == "targets": v3["targets"] = [round(e + ts * k * R, 4) for k in (1, 2, 3)]
            else: v3["trail_atr"] = cfg["trail"]
            if any(r["mkt"] == mkt and r["status"] == "open" and r.get("rule") == v3["rule"] for r in rows):
                status[mkt] = f"signal at {t_last:%d %b %H:%M} UTC while a {v3['rule']} trade is open - not recorded"
            else:
                rows.append(v3); changed = True
                status[mkt] = (f"signal at {t_last:%d %b %H:%M} UTC SKIPPED ({v3['skip_reason']}) - tracked as 'skip'" if why
                               else f"NEW paper trade at {t_last:%d %b %H:%M} UTC ({v3['grade']}, {md} ATR)")
        elif sig:
            msgs = []
            if not any(r["mkt"] == mkt and r["status"] == "open" and r.get("rule", "v1") == "v1" for r in rows):
                rows.append(sig); changed = True; msgs.append("v1 opened")
            if mkt in V2:                                    # v2: BTC 24 h trend filter + new stop / exit
                tr = _btc_trend(t_last); ts = sig["side"]
                if tr is None or tr * ts <= 0:
                    msgs.append(f"v2 skipped: against BTC 24h trend ({'n/a' if tr is None else f'{tr*100:+.2f}%'})")
                elif any(r["mkt"] == mkt and r["status"] == "open" and r.get("rule") == "v2" for r in rows):
                    msgs.append("v2: a v2 trade is already open")
                else:
                    cfg = V2[mkt]; a = _atr(done, j); e = sig["entry"]
                    stop = sig["stop"] - ts * cfg["room"] * a; R = abs(e - stop)
                    v2 = {**sig, "sig": key + "|v2", "rule": "v2", "stop": round(stop, 2), "risk": round(R, 2), "risk_bps": round(R / e * 1e4, 1),
                          "atr": round(a, 2), "btc_24h_pct": round(tr * 100, 2), "exit_mode": cfg["exit"],
                          "grade": "A+" if sig["taker_delta"] is not None and abs(sig["taker_delta"]) > 0.10 else "A"}
                    if cfg["exit"] == "targets":
                        v2["targets"] = [round(e + ts * k * R, 2) for k in (1, 2, 3)]
                    else:
                        v2["trail_atr"] = cfg["trail"]
                    rows.append(v2); changed = True; msgs.append(f"v2 opened ({v2['grade']})")
            status[mkt] = f"NEW paper trade at {t_last:%d %b %H:%M} UTC - " + " · ".join(msgs) if msgs else "signal while trades are open - skipped"
        elif mkt not in status:
            status[mkt] = f"checked bar {t_last:%d %b %H:%M} UTC - no break"
    if changed:
        _save(rows)
    _C["status"] = status
    return status


def api() -> dict:
    try:
        tick()
    except Exception as e:
        _C["status"] = {"error": str(e)[:160]}
    rows = _load(); out = {}
    for mkt in MKTS:
        try:
            b4, d1 = bars(mkt)
            px = float(b4.close.iloc[-1])
            bl = _C.get(("map", mkt)) or build_map(mkt, b4, d1, b4[b4.done].index[-1].normalize())
            _C[("map", mkt)] = bl
            near = sorted(bl, key=lambda b: abs((b["lo"] + b["hi"]) / 2 - px))[:6]
            out[mkt] = {"price": px, "last_bar": str(b4[b4.done].index[-1]), "blocks": sorted(near, key=lambda b: -b["hi"]),
                        "open": [r for r in rows if r["mkt"] == mkt and r["status"] == "open" and r.get("rule") != "skip"],
                        "skipped_open": [r for r in rows if r["mkt"] == mkt and r["status"] == "open" and r.get("rule") == "skip"], "mood_now": (lambda dn: _mood(_atr_rank(dn, len(dn) - 1)) if MKTS[mkt] else None)(b4[b4.done])}
        except Exception as e:
            out[mkt] = {"error": str(e)[:160]}
    closed = [r for r in rows if r["status"] == "closed"]
    sc = {}
    vsc = {}
    for mkt in MKTS:
        for nm in VARIANTS:
            z = [r["var"][nm] for r in closed if r["mkt"] == mkt and r.get("rule") == "v3" and r.get("var", {}).get(nm, {}).get("status") == "closed"]
            if z:
                vsc[f"{mkt} {nm}"] = {"n": len(z), "avg_bps_net": round(sum(x["bps_net"] for x in z) / len(z), 1), "win_pct": round(100 * sum(x["bps_net"] > 0 for x in z) / len(z))}
    for m4, c4 in V4.items():
        z4 = [r["v4"] for r in closed if r["mkt"] == m4 and isinstance(r.get("v4"), dict) and r["v4"].get("status") == "closed"]
        if z4:
            vsc[c4["label"]] = {"n": len(z4), "avg_bps_net": round(sum(x["bps_net"] for x in z4) / len(z4), 1), "win_pct": round(100 * sum(x["bps_net"] > 0 for x in z4) / len(z4))}
    zb = [r["h2"] for r in closed if r["mkt"] == "BTC" and r.get("h2", {}).get("status") == "closed"]
    if zb:
        vsc[BTC_H2] = {"n": len(zb), "avg_bps_net": round(sum(x["bps_net"] for x in zb) / len(zb), 1), "win_pct": round(100 * sum(x["bps_net"] > 0 for x in zb) / len(zb))}
    zg = [r["gv2"] for r in closed if r["mkt"] == "GOLD" and r.get("gv2", {}).get("status") == "closed"]
    if zg:
        vsc[GOLD_V2] = {"n": len(zg), "avg_bps_net": round(sum(x["bps_net"] for x in zg) / len(zg), 1), "win_pct": round(100 * sum(x["bps_net"] > 0 for x in zg) / len(zg))}
    for rule in ("v1", "v2", "v3", "skip"):
        for mkt in MKTS:
            z = [r for r in closed if r["mkt"] == mkt and r.get("rule", "v1") == rule]
            if z:
                sc[f"{mkt} {rule}"] = {"n": len(z), "win_pct": round(100 * sum(1 for r in z if r["bps_net"] > 0) / len(z)), "avg_bps_net": round(sum(r["bps_net"] for r in z) / len(z), 1),
                       "avg_R_net": round(sum(r["R_net"] for r in z) / len(z), 2), "verdict": "too early" if len(z) < 30 else "holding" if sum(r["bps_net"] for r in z) > 0 else "not holding"}

    return {"markets": out, "status": _C.get("status", {}), "recent": rows[-20:][::-1], "scorecard": sc, "variants": vsc, "variant_names": list(VARIANTS),
            "rule": "4H bar enters a block and breaks its far edge (crypto: taker volume with the break) -> follow at the close. Crypto (v3, from 29 Oct): only with BTC's 24 h trend and when ATR is calm or normal vs the last 180 days (wild skipped); ETH: stop back past the block, trail 1.5 ATR once +1R; BTC: stop +0.5 ATR room, 3 targets 1/2/3R; out after 2 days. Gold (v1): stop back past the block, exit after 2 days. Paper only.",
            "tested": {"ETH": "v3 conditions 2023-24 / 2025-26: calm +125 / +184, normal +95 / +62 bps gross", "BTC": "v3 conditions: calm +58 / +57, normal 0 / +69 bps gross",
                       "SOL": "UNPROVEN - failed the 4H test overall (+31 / -2 bps gross); paper test only",
                       **{k: "UNPROVEN - not backtested on this coin; forward paper test from 4 Nov 2026 (same v3 rule, ETH exit)" for k in ALTS}, "GOLD": "+21 / +10 bps net (no taker data)"}}
