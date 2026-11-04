"""Paper-trading validator — does the signal actually work?

When a *qualified* signal (score >= paper.min_score) fires and has a valid plan,
a paper trade is opened at the current price with the plan's structural stop and
target. Each cycle every open trade is marked against the live/replay price:
  * price hits target  -> WIN   (R = +reward/risk)
  * price hits stop     -> LOSS  (R = -1)

Outcomes are measured in R-multiples on the underlying, so the same yardstick
tests the *edge of the signal* regardless of leverage. Crypto is 24/7, so there
is no session-end flat by default (`session.eod_flatten: false`) — trades run to
target or stop. Closed trades persist to paper/paper_trades.jsonl and stats
accumulate across sessions.
"""
from __future__ import annotations

import datetime as dt
import os
import json
import threading
from pathlib import Path

from .config import Config, ROOT


def _parse_hhmm(s):
    try:
        h, m = s.split(":")
        return dt.time(int(h), int(m))
    except Exception:
        return dt.time(23, 59)


class PaperBook:
    def __init__(self, cfg: Config, path=None, load=True):
        self.cfg = cfg
        self.enabled = bool(cfg.a("paper", "enabled", default=True))
        self._mode = str(getattr(cfg, "run_mode", "") or "live")
        self.min_score = int(cfg.a("paper", "min_score", default=3))
        self.max_per_side = int(cfg.a("paper", "max_open_per_side", default=1))
        self.regime_gate = bool(cfg.a("paper", "regime_gate", default=True))
        self.include_setups = bool(cfg.a("paper", "include_setups", default=True))
        self.min_vol = float(cfg.a("paper", "min_day_vol_usd", default=0) or 0)
        self.max_hold_min = float(cfg.a("paper", "max_hold_minutes", default=0) or 0)   # time exit (0 = off)
        self.min_stop_pct = float(cfg.a("paper", "min_stop_pct", default=0.5) or 0.5) / 100   # stop never tighter than this
        self.min_natural_stop_pct = float(cfg.a("paper", "min_natural_stop_pct", default=0) or 0) / 100   # skip tighter setups
        self.max_stop_pct = max(self.min_stop_pct, float(cfg.a("paper", "max_stop_pct", default=1.5) or 1.5) / 100)
        self.qualified_only = bool(int(os.getenv("PAPER_QUALIFIED_ONLY", "0") or 0)) \
            or bool(cfg.a("paper", "qualified_only", default=False))
        self.eod_flatten = bool(cfg.a("session", "eod_flatten", default=False))
        self.close_t = _parse_hhmm(cfg.a("session", "market_close", default="23:59"))
        self.path = Path(path) if path else \
            ROOT / str(cfg.a("paper", "file", default="paper/paper_trades.jsonl"))
        if path is None and self._mode != "live":
            self.path = self.path.with_name(
                f"{self.path.stem}.{self._mode}{self.path.suffix}")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.open: dict[str, dict] = {}
        self.closed: list[dict] = self._load() if load else []
        # open trades survive a tracker restart (2026-10-21): saved to <file>.open.json
        self.open_path = self.path.with_name(self.path.stem + ".open.json")
        self._open_saved = None
        if load and self.open_path.exists():
            try:
                self.open = json.loads(self.open_path.read_text()) or {}
                self._open_saved = json.dumps(self.open, sort_keys=True, default=str)
            except Exception:
                self.open = {}
        ids = [t.get("id", 0) for t in self.closed] + [t.get("id", 0) for t in self.open.values()]
        self._id = max([len(self.closed)] + [int(i or 0) for i in ids]) + 1

    def _load(self) -> list[dict]:
        if not self.path.exists():
            return []
        out = []
        for line in self.path.read_text().splitlines():
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except Exception:
                    pass
        return out

    def _save_open(self):
        try:
            txt = json.dumps(self.open, sort_keys=True, default=str)
            if txt != self._open_saved:
                tmp = self.open_path.with_suffix(".tmp")
                tmp.write_text(txt)
                tmp.replace(self.open_path)
                self._open_saved = txt
        except Exception as e:
            print(f"[paper] could not save open trades: {e}")

    def _persist(self, trade: dict):
        trade.setdefault("mode_run", self._mode)
        with self.path.open("a") as f:
            f.write(json.dumps(trade) + "\n")

    # ---- main cycle ------------------------------------------------------
    def update(self, snapshot: dict, market_dt=None) -> dict:
        if not self.enabled:
            return {"enabled": False}
        self._cur_dt = market_dt.isoformat() if hasattr(market_dt, "isoformat") \
            else dt.datetime.utcnow().isoformat()
        insts = snapshot.get("instruments", {})
        prices = {k: v.get("spot") for k, v in insts.items()}
        sess_time = None
        for v in insts.values():
            cs = v.get("candles") or []
            if cs:
                try:
                    t = dt.datetime.strptime(cs[-1]["t"], "%H:%M").time()
                    sess_time = max(sess_time, t) if sess_time else t
                except Exception:
                    pass
        eod = bool(self.eod_flatten and sess_time and sess_time >= self.close_t)

        with self.lock:
            self._manage(prices, eod, insts)
            self._open_new(insts, prices)
            self._open_new_setups(insts, prices)
            self._save_open()
            return self._panel()

    def tick(self, snapshot):
        """Intrabar management every compute cycle (~1.5s): stops, targets,
        breakeven and trail react to live prices, not only at 15-min closes."""
        if not self.enabled or not self.open:
            return None
        self._cur_dt = dt.datetime.utcnow().isoformat()
        insts = snapshot.get("instruments", {})
        prices = {k: v.get("spot") for k, v in insts.items()}
        with self.lock:
            self._manage(prices, False, insts)
            self._save_open()
            return self._panel()

    def _manage(self, prices, eod, insts=None):
        for key, tr in list(self.open.items()):
            px = prices.get(tr["instrument"])
            if px is None:
                continue
            d = 1 if tr["direction"] == "long" else -1
            entry = tr["entry"]
            risk = tr.get("risk_init") or abs(entry - tr["stop_loss"]) or 1e-9
            t2 = tr.get("target2")
            if t2 is not None:
                # ---- v3 management: BE at +1R -> lock +1R at +2R -> trail -> out at 3R
                rm = d * (px - entry) / risk
                if rm >= 1 and not tr.get("be_moved"):
                    tr["stop_loss"] = round(entry, 6)
                    tr["be_moved"] = True
                if rm >= 2:
                    tr["t1_hit"] = True
                    if d * (entry + d * risk - tr["stop_loss"]) > 0:
                        tr["stop_loss"] = round(entry + d * risk, 6)   # +1R locked
                if rm >= 3:
                    tr["t2_hit"] = True
                    if d * (entry + d * 2 * risk - tr["stop_loss"]) > 0:
                        tr["stop_loss"] = round(entry + d * 2 * risk, 6)  # +2R locked
                # trail only after T1, on the 3-completed-candle extreme (same rule
                # as the entry stop) - a 1-candle trail from +1R was choking winners
                if tr.get("t1_hit") and insts:
                    cands = (insts.get(tr["instrument"], {}) or {}).get("chart_candles") or []
                    comp = cands[:-1][-3:]
                    if len(comp) == 3:
                        trail = (min(c["l"] for c in comp) * (1 - 0.0005) if d > 0
                                 else max(c["h"] for c in comp) * (1 + 0.0005))
                        if d * (trail - tr["stop_loss"]) > 0:
                            tr["stop_loss"] = round(trail, 6)
                            tr["trailed"] = True
                t3 = tr.get("target3")
                hit = None
                if t3 is not None and d * (px - t3) >= 0:
                    hit = ("target3", t3)
                elif t3 is None and d * (px - t2) >= 0:
                    hit = ("target2", t2)
                elif d * (tr["stop_loss"] - px) >= 0:
                    reason = ("trail" if tr.get("trailed")
                              else "lock+2R" if tr.get("t2_hit")
                              else "lock+1R" if tr.get("t1_hit")
                              else "breakeven" if tr.get("be_moved") else "stop")
                    hit = (reason, tr["stop_loss"])
            else:
                # ---- legacy management (pre-v3 rows)
                hit = None
                if d * (tr["stop_loss"] - px) >= 0:
                    hit = ("stop", tr["stop_loss"])
                elif d * (px - tr["target1"]) >= 0:
                    hit = ("target", tr["target1"])
            if hit is None and eod:
                hit = ("eod", px)
            if hit is None and self.max_hold_min:
                try:
                    t0 = dt.datetime.fromisoformat(str(tr.get("entry_dt") or tr["opened_at"]).replace(" ", "T")[:19])
                    t1 = dt.datetime.fromisoformat(str(getattr(self, "_cur_dt", "") or dt.datetime.utcnow().isoformat())[:19])
                    if (t1 - t0).total_seconds() / 60 >= self.max_hold_min:
                        hit = (f"time ({int(self.max_hold_min // 60)} h)", px)
                except Exception:
                    pass
            if hit:
                self._close(key, tr, hit[0], hit[1])

    def _close(self, key, tr, reason, exit_px):
        entry = tr["entry"]
        risk = tr.get("risk_init") or abs(entry - tr["stop_loss"]) or 1e-9
        pnl_pts = (exit_px - entry) if tr["direction"] == "long" else (entry - exit_px)
        tr.update({
            "status": "closed", "exit": round(exit_px, 4), "exit_reason": reason,
            "exit_dt": getattr(self, "_cur_dt", None),
            "pnl_pts": round(pnl_pts, 4), "r": round(pnl_pts / risk, 2),
            "pnl_usd_100": round(100.0 * pnl_pts / entry, 2) if entry else 0.0,
            "result": "win" if pnl_pts > 0 else "loss" if pnl_pts < 0 else "flat",
            # after costs (2026-10-22): 0.05% fee + 0.02% slippage per side
            "r_net": round(pnl_pts / risk - 0.0014 * entry / risk, 2),
            "pnl_usd_100_net": round(100.0 * pnl_pts / entry - 0.14, 2) if entry else 0.0,
        })
        self.closed.append(tr)
        self._persist(tr)
        self.open.pop(key, None)

    def _open_new(self, insts, prices):
        for key, v in insts.items():
            price = prices.get(key)
            if price is None:
                continue
            open_sides = [t for t in self.open.values() if t["instrument"] == key]
            for sd in v.get("signals", []):
                if sd["score"] < self.min_score:
                    continue
                if self.regime_gate and sd.get("with_regime") is False:
                    continue
                plan = sd.get("plan")
                if not plan or plan.get("stop_loss") is None or plan.get("target1") is None:
                    continue
                okey = f"{key}:{sd['direction']}"
                if okey in self.open:
                    continue
                if any(t["direction"] != sd["direction"] for t in open_sides):
                    continue                      # never hold both sides of one coin
                if len([t for t in open_sides if t["direction"] == sd["direction"]]) >= self.max_per_side:
                    continue
                if sd["direction"] == "long" and not (plan["stop_loss"] < price < plan["target1"]):
                    continue
                if sd["direction"] == "short" and not (plan["target1"] < price < plan["stop_loss"]):
                    continue
                mp = v.get("market_profile", {})
                tr = {
                    "id": self._id, "instrument": key, "direction": sd["direction"],
                    "source": "signal",
                    "score": sd["score"], "instrument_type": plan["instrument"],
                    "entry": round(price, 4), "stop_loss": plan["stop_loss"],
                    "target1": plan["target1"], "rr": plan.get("rr"),
                    "day_type": mp.get("day_type"), "ib_ext": mp.get("ib_extension"),
                    "with_regime": sd.get("with_regime"),
                    "regime": v.get("regime", {}).get("regime"),
                    "opened_at": dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
                    "entry_dt": getattr(self, "_cur_dt", None),
                    "rationale": sd["rationale"][:160], "status": "open",
                }
                self._id += 1
                self.open[okey] = tr

    def _open_new_setups(self, insts, prices):
        if not self.include_setups:
            return
        for key, v in insts.items():
            price = prices.get(key)
            if price is None:
                continue
            su = v.get("setup")
            if not su or not su.get("actionable"):
                continue
            # liquidity gate: absorption/order flow is only meaningful in deep markets
            if self.min_vol and (v.get("day_vol_usd") or 0) < self.min_vol:
                continue
            # day-roll guard: at 00:00 UTC IB/VWAP/POC reset to a single candle, so
            # "reversal at IB" is guaranteed noise. Wait until the IB window closes.
            _need = max(2, int(self.cfg.ib_minutes // max(1, self.cfg.candle_minutes)))
            if len(v.get("candles") or []) <= _need:
                continue
            sl, tgt = su.get("stop_loss"), su.get("target1")
            if sl is None or tgt is None:
                continue
            direction = su["side"]
            if direction == "long" and not (sl < price < tgt):
                continue
            if direction == "short" and not (tgt < price < sl):
                continue
            okey = f"{key}:setup:{direction}"
            if okey in self.open:
                continue
            if any(t["instrument"] == key and t["direction"] != direction for t in self.open.values()):
                continue                          # never hold both sides of one coin
            same = [t for t in self.open.values() if t["instrument"] == key
                    and t.get("source") == "setup" and t["direction"] == direction]
            if len(same) >= self.max_per_side:
                continue
            tk = v.get("ticket") or {}
            vehicle = tk.get("vehicle") or "perp"
            # plan v3 (user spec): stop = prev-3-candle extreme with 0.05pct buffer,
            # clamped to [0.5pct, 1.5pct] of price; T1=2R (lock +1R), T2=3R exit;
            # breakeven at +1R then candle-low trail (see _manage).
            _q = bool(su.get("grade") == "A+" or
                      (su.get("grade") == "A" and su.get("mode") == "continuation"))
            if self.qualified_only and not _q:
                continue
            d = 1 if direction == "long" else -1
            cands = (v.get("chart_candles") or v.get("candles") or [])
            comp = cands[:-1] if len(cands) >= 2 else cands
            risk = None
            if len(comp) >= 3 and price:
                if d > 0:
                    ref = min(c["l"] for c in comp[-3:]) * (1 - 0.0005)
                    risk = price - ref
                else:
                    ref = max(c["h"] for c in comp[-3:]) * (1 + 0.0005)
                    risk = ref - price
            if not risk or risk <= 0:
                risk = abs(price - sl) if sl else price * 0.006
            # 2026-10-23 (Nikhil): skip setups whose structure stop is too tight to survive costs.
            # Full 1-year replay (correct risk): -0.073R -> -0.013R per trade after costs (-318R -> -26R);
            # forcing every stop wider did not help. So: keep the structure stop, skip tight ones.
            if self.min_natural_stop_pct and risk < price * self.min_natural_stop_pct:
                continue
            risk = max(price * self.min_stop_pct, min(price * self.max_stop_pct, risk))
            sl = round(price - d * risk, 6)
            tgt = round(price + d * 2 * risk, 6)
            tgt2 = round(price + d * 3 * risk, 6)
            tgt3 = round(price + d * 4 * risk, 6)
            tr = {
                "id": self._id, "instrument": key, "direction": direction,
                "source": "setup", "grade": su.get("grade"), "mode": su.get("mode"),
                "level": su.get("level_name"), "score": su.get("points"),
                "instrument_type": vehicle,
                "entry": round(price, 4), "stop_loss": sl, "target1": tgt,
                "target2": tgt2, "target3": tgt3, "risk_init": round(risk, 6), "be_moved": False,
                "rr": 2.0, "lots": tk.get("size_coin"),
                "risk_pct": (tk.get("size") or {}).get("risk_amount"),
                "regime": v.get("regime", {}).get("regime"),
                "opened_at": dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
                "entry_dt": getattr(self, "_cur_dt", None),
                "qualified": bool(su.get("grade") == "A+" or
                                  (su.get("grade") == "A" and su.get("mode") == "continuation")),
                "strength": su.get("strength"), "choch": su.get("choch"),
                "divergence": su.get("divergence"), "state": su.get("state"),
                "v2": su.get("v2"),
                "plan_v": 3,
                "day_vol_usd": v.get("day_vol_usd"),
                "rationale": " · ".join(su.get("reasons", []))[:180], "status": "open",
            }
            self._id += 1
            self.open[okey] = tr

    def force_flatten(self, prices: dict, reason="eod"):
        with self.lock:
            for key, tr in list(self.open.items()):
                px = prices.get(tr["instrument"])
                if px is not None:
                    self._close(key, tr, reason, px)

    # ---- stats + panel ---------------------------------------------------
    def stats(self) -> dict:
        c = self.closed
        n = len(c)
        wins = [t for t in c if t.get("result") == "win"]
        losses = [t for t in c if t.get("result") == "loss"]
        rs = [t.get("r", 0) for t in c]
        gross_r = round(sum(rs), 2)
        win_rate = round(100 * len(wins) / n, 1) if n else 0.0
        avg_r = round(gross_r / n, 2) if n else 0.0
        avg_win = round(sum(t["r"] for t in wins) / len(wins), 2) if wins else 0.0
        avg_loss = round(sum(t["r"] for t in losses) / len(losses), 2) if losses else 0.0
        p = (len(wins) / n) if n else 0
        expectancy = round(p * avg_win + (1 - p) * avg_loss, 2) if n else 0.0
        by_inst = {}
        for t in c:
            b = by_inst.setdefault(t["instrument"], {"n": 0, "w": 0, "r": 0.0})
            b["n"] += 1
            b["w"] += 1 if t.get("result") == "win" else 0
            b["r"] = round(b["r"] + t.get("r", 0), 2)
        by_grade = {}
        for t in c:
            if t.get("source") != "setup":
                continue
            g = t.get("grade", "?")
            b = by_grade.setdefault(g, {"n": 0, "w": 0, "r": 0.0})
            b["n"] += 1
            b["w"] += 1 if t.get("result") == "win" else 0
            b["r"] = round(b["r"] + t.get("r", 0), 2)
        usd = [t.get("pnl_usd_100",
                     round(100.0 * t.get("pnl_pts", 0) / t["entry"], 2) if t.get("entry") else 0.0)
               for t in c]
        usd_total = round(sum(usd), 2)
        usd_avg = round(usd_total / n, 2) if n else 0.0
        usd_q = round(sum(u for u, t in zip(usd, c)
                          if t.get("grade") == "A+" or (t.get("grade") == "A"
                          and t.get("mode") == "continuation")), 2)
        qual = [t for t in c if t.get("source") == "setup" and (
            t.get("grade") == "A+" or (t.get("grade") == "A" and t.get("mode") == "continuation"))]
        qn = len(qual); qw = sum(1 for t in qual if t.get("result") == "win")
        q_stats = {"n": qn, "w": qw, "win_rate": round(100 * qw / qn, 1) if qn else 0.0,
                   "r": round(sum(t.get("r", 0) for t in qual), 2)}
        return {
            "usd_total_100": usd_total, "usd_avg_100": usd_avg, "usd_qualified_100": usd_q,
            "closed": n, "open": len(self.open), "wins": len(wins), "losses": len(losses),
            "win_rate": win_rate, "avg_r": avg_r, "gross_r": gross_r,
            "avg_win_r": avg_win, "avg_loss_r": avg_loss, "expectancy_r": expectancy,
            "by_instrument": by_inst, "by_grade": by_grade,
            "qualified": q_stats,
        }

    def _panel(self) -> dict:
        return {
            "enabled": True,
            "stats": self.stats(),
            "open": sorted(self.open.values(), key=lambda t: t["id"]),
            "recent": list(reversed(self.closed[-12:])),
        }
