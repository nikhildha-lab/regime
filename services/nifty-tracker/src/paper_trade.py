"""Paper-trading validator — does the signal actually work?

When a *qualified* signal (score >= paper.min_score) fires and has a valid plan,
a paper trade is opened at the current price with the plan's structural stop and
target. Each cycle every open trade is marked against the live/replay price:
  * price hits target  -> WIN   (R = +reward/risk)
  * price hits stop     -> LOSS  (R = -1)
  * session end (last candle >= market_close) -> flat at market (time stop)

Outcomes are measured in **R-multiples on the underlying** (instrument-agnostic,
so the same yardstick works whether you'd have traded futures or options). This
tests the *edge of the signal*, not option pricing — actual option P&L differs
with delta/theta, but if the underlying doesn't reach target the option wouldn't
either.

Closed trades persist to `paper/paper_trades.jsonl` and stats accumulate across
sessions.
"""
from __future__ import annotations

import datetime as dt
import json
import threading
from pathlib import Path

from .config import Config, ROOT


def _parse_hhmm(s):
    try:
        h, m = s.split(":")
        return dt.time(int(h), int(m))
    except Exception:
        return dt.time(15, 30)


class PaperBook:
    def __init__(self, cfg: Config, path=None, load=True):
        self.cfg = cfg
        self.enabled = bool(cfg.a("paper", "enabled", default=True))
        self._mode = str(getattr(cfg, "run_mode", "") or "live")
        self.min_score = int(cfg.a("paper", "min_score", default=3))
        self.max_per_side = int(cfg.a("paper", "max_open_per_side", default=1))
        self.regime_gate = bool(cfg.a("paper", "regime_gate", default=True))
        self.include_setups = bool(cfg.a("paper", "include_setups", default=True))
        self.close_t = _parse_hhmm(cfg.a("session", "market_close", default="15:30"))
        self.no_new_t = _parse_hhmm(cfg.a("paper", "no_new_after", default="15:15"))   # no new paper trades after this (2026-10-22)
        self.path = Path(path) if path else \
            ROOT / str(cfg.a("paper", "file", default="paper/paper_trades.jsonl"))
        if path is None and self._mode != "live":
            # sim/replay runs must never write into the real forward-test log
            self.path = self.path.with_name(
                f"{self.path.stem}.{self._mode}{self.path.suffix}")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Supertrend(10,3) swing forward-test stream — PAPER ONLY, separate log
        try:
            from .analytics.supertrend import PaperSTStream
            self.st_stream = PaperSTStream(cfg, self.path.parent,
                                           mode=self._mode)
        except Exception:
            self.st_stream = None
        self.lock = threading.RLock()
        self.open: dict[str, dict] = {}          # key -> trade
        self.closed: list[dict] = self._load() if load else []
        # open trades survive a tracker restart (2026-10-22): saved to <file>.open.json
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

    def _persist(self, trade: dict):
        trade.setdefault("mode", self._mode)
        with self.path.open("a") as f:
            f.write(json.dumps(trade) + "\n")

    # ---- main cycle ------------------------------------------------------
    def update(self, snapshot: dict, market_dt=None) -> dict:
        if not self.enabled:
            return {"enabled": False}
        # market datetime of this bar (backtest passes it; live falls back to now)
        self._cur_dt = market_dt.isoformat() if hasattr(market_dt, "isoformat") \
            else dt.datetime.now().isoformat()
        insts = snapshot.get("instruments", {})
        prices = {k: v.get("spot") for k, v in insts.items()}
        # session time from the latest candle (works in replay + live)
        sess_time = None
        for v in insts.values():
            cs = v.get("candles") or []
            if cs:
                try:
                    t = dt.datetime.strptime(cs[-1]["t"], "%H:%M").time()
                    sess_time = max(sess_time, t) if sess_time else t
                except Exception:
                    pass
        eod = bool(sess_time and sess_time >= self.close_t)
        self._no_new = bool(sess_time and sess_time >= self.no_new_t)
        if self._mode == "live" and market_dt is None:   # live only (backtests pass market_dt): also use the clock
            now_t = dt.datetime.now().time()
            self._no_new = self._no_new or now_t >= self.no_new_t or now_t < dt.time(9, 15)

        with self.lock:
            self._manage(prices, eod)
            self._open_new(insts, prices)
            self._open_new_setups(insts, prices)
            self._save_open()
            if getattr(self, "st_stream", None):
                try:
                    self.st_stream.update(insts, getattr(self, "_cur_dt", None))
                except Exception:
                    pass
            return self._panel()

    def _manage(self, prices, eod):
        for key, tr in list(self.open.items()):
            px = prices.get(tr["instrument"])
            if px is None:
                continue
            hit = None
            if tr["direction"] == "long":
                if px <= tr["stop_loss"]:
                    hit = ("stop", tr["stop_loss"])
                elif px >= tr["target1"]:
                    hit = ("target", tr["target1"])
            else:
                if px >= tr["stop_loss"]:
                    hit = ("stop", tr["stop_loss"])
                elif px <= tr["target1"]:
                    hit = ("target", tr["target1"])
            if hit is None and eod:
                hit = ("eod", px)
            if hit:
                self._close(key, tr, hit[0], hit[1])

    def _close(self, key, tr, reason, exit_px):
        entry, sl = tr["entry"], tr["stop_loss"]
        risk = abs(entry - sl) or 1e-9
        pnl_pts = (exit_px - entry) if tr["direction"] == "long" else (entry - exit_px)
        tr.update({
            "status": "closed", "exit": round(exit_px, 2), "exit_reason": reason,
            "exit_dt": getattr(self, "_cur_dt", None),
            "pnl_pts": round(pnl_pts, 2), "r": round(pnl_pts / risk, 2),
            # after costs (2026-10-22): ~4 Nifty / 10 Bank Nifty index points round trip (spread + fees + slippage)
            # costs: index points (options) for Nifty / Bank Nifty; stock futures ~0.10% of price round trip (STT 0.05% sell side since 1 Apr 2026 + fees + spread; was 0.06%)
            "r_net": round((pnl_pts - {"NIFTY": 4.0, "BANKNIFTY": 10.0}.get(tr["instrument"], 0.0010 * float(tr.get("entry") or 0))) / risk, 2),
            "result": "win" if pnl_pts > 0 else "loss" if pnl_pts < 0 else "flat",
        })
        self.closed.append(tr)
        self._persist(tr)
        self.open.pop(key, None)

    def _open_new(self, insts, prices):
        if getattr(self, "_no_new", False):
            return
        for key, v in insts.items():
            price = prices.get(key)
            if price is None:
                continue
            open_sides = [t for t in self.open.values() if t["instrument"] == key]
            for sd in v.get("signals", []):
                if sd["score"] < self.min_score:
                    continue
                if self.regime_gate and sd.get("with_regime") is False:
                    continue                         # only trade WITH the regime
                plan = sd.get("plan")
                if not plan or plan.get("stop_loss") is None or plan.get("target1") is None:
                    continue
                okey = f"{key}:{sd['direction']}"
                if okey in self.open:
                    continue
                if len([t for t in open_sides if t["direction"] == sd["direction"]]) >= self.max_per_side:
                    continue
                # sanity: SL/target on correct sides
                if sd["direction"] == "long" and not (plan["stop_loss"] < price < plan["target1"]):
                    continue
                if sd["direction"] == "short" and not (plan["target1"] < price < plan["stop_loss"]):
                    continue
                mp = v.get("market_profile", {})
                tr = {
                    "id": self._id, "instrument": key, "direction": sd["direction"],
                    "source": "signal",
                    "score": sd["score"], "instrument_type": plan["instrument"],
                    "entry": round(price, 2), "stop_loss": plan["stop_loss"],
                    "target1": plan["target1"], "rr": plan.get("rr"),
                    "day_type": mp.get("day_type"), "ib_ext": mp.get("ib_extension"),
                    "with_regime": sd.get("with_regime"),
                    "regime": v.get("regime", {}).get("regime"),
                    "opened_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "entry_dt": getattr(self, "_cur_dt", None),
                    "rationale": sd["rationale"][:160], "status": "open",
                }
                self._id += 1
                self.open[okey] = tr

    def _open_new_setups(self, insts, prices):
        """Log actionable graded setups (A+/A/B/C per profile) as their own paper
        stream, tagged by grade + suggested lots, so a forward-test record builds
        that can be sliced by grade."""
        if not self.include_setups or getattr(self, "_no_new", False):
            return
        for key, v in insts.items():
            price = prices.get(key)
            if price is None:
                continue
            su = v.get("setup")
            if not su or not su.get("actionable"):
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
            same = [t for t in self.open.values() if t["instrument"] == key
                    and t.get("source") == "setup" and t["direction"] == direction]
            if len(same) >= self.max_per_side:
                continue
            tk = v.get("ticket") or {}
            size = tk.get("size") or {}
            vehicle = (f"{tk.get('leg')} {tk.get('strike')}"
                       if tk.get("leg") and tk.get("strike") else "underlying")
            if key not in ("NIFTY", "BANKNIFTY"):
                vehicle = "futures"                # stocks: assume the stock future (Nikhil 2026-10-23); points = underlying move
            tr = {
                "id": self._id, "instrument": key, "direction": direction,
                "source": "setup", "grade": su.get("grade"), "mode": su.get("mode"),
                "level": su.get("level_name"), "score": su.get("points"),
                "instrument_type": vehicle,
                "entry": round(price, 2), "stop_loss": sl, "target1": tgt,
                "rr": su.get("rr"), "lots": size.get("lots"),
                "risk_pct": size.get("risk_pct"),
                "regime": v.get("regime", {}).get("regime"),
                "regime_stance": v.get("regime", {}).get("stance"),
                # context only (no filter): does the trade go WITH the regime? None = no trend (range / stand aside)
                "with_regime": (lambda rg: None if not any(w in rg for w in ("up", "down")) else
                                (("up" in rg) == (direction == "long")))(str(v.get("regime", {}).get("regime", "")).lower()),
                "depth_imb20": (v.get("depth") or {}).get("imb20"),
                "depth_imb20_1m": (v.get("depth") or {}).get("imb20_1m"),
                "opened_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "entry_dt": getattr(self, "_cur_dt", None),
                "strength": su.get("strength"), "choch": su.get("choch"),
                "divergence": su.get("divergence"), "state": su.get("state"),
                "v2": su.get("v2"),
                "rationale": " · ".join(su.get("reasons", []))[:180], "status": "open",
            }
            self._id += 1
            self.open[okey] = tr

    def force_flatten(self, prices: dict, reason="eod"):
        """Close all open trades at given prices (used at end of a backtest day)."""
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
        # expectancy per trade in R
        p = (len(wins) / n) if n else 0
        expectancy = round(p * avg_win + (1 - p) * avg_loss, 2) if n else 0.0
        by_inst = {}
        for t in c:
            b = by_inst.setdefault(t["instrument"], {"n": 0, "w": 0, "r": 0.0})
            b["n"] += 1
            b["w"] += 1 if t.get("result") == "win" else 0
            b["r"] = round(b["r"] + t.get("r", 0), 2)
        # slice the graded-setup stream by grade (does A+ actually beat B?)
        by_grade = {}
        for t in c:
            if t.get("source") != "setup":
                continue
            g = t.get("grade", "?")
            b = by_grade.setdefault(g, {"n": 0, "w": 0, "r": 0.0})
            b["n"] += 1
            b["w"] += 1 if t.get("result") == "win" else 0
            b["r"] = round(b["r"] + t.get("r", 0), 2)
        return {
            "closed": n, "open": len(self.open), "wins": len(wins), "losses": len(losses),
            "win_rate": win_rate, "avg_r": avg_r, "gross_r": gross_r,
            "avg_win_r": avg_win, "avg_loss_r": avg_loss, "expectancy_r": expectancy,
            "by_instrument": by_inst, "by_grade": by_grade,
        }

    def _panel(self) -> dict:
        return {
            "enabled": True,
            "supertrend": (self.st_stream.panel()
                           if getattr(self, "st_stream", None) else None),
            "stats": self.stats(),
            "open": sorted(self.open.values(), key=lambda t: t["id"]),
            "recent": list(reversed(self.closed[-12:])),
        }
