"""Incremental Supertrend + a swing paper-trade forward-test stream.

Companion to the 2-yr backtest (15-min flip-to-flip, no buffer, hold overnight).
PAPER ONLY — logs flips and P&L in points to paper/supertrend_trades.jsonl; it
places no orders. Indicator + open position persist across app restarts via
paper/supertrend_state.json, so overnight holds survive the daily stop/start.
Continuity caveat: the stream is only as continuous as the app's uptime —
sessions where the tracker never ran leave a gap in the indicator.
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

SYN_COST_PTS = {"NIFTY": 5.5, "BANKNIFTY": 11.0}   # synthetic-future round trip


class SupertrendState:
    """One instrument's incremental Supertrend; serializable; bar-by-bar push()."""

    def __init__(self, period=10, mult=3.0, d=None):
        self.period = int(period); self.mult = float(mult)
        self.trs = []; self.atr = None
        self.fub = None; self.flb = None
        self.pc = None; self.trend = 0
        if d:
            for k in ("trs", "atr", "fub", "flb", "pc", "trend"):
                if k in d: setattr(self, k, d[k])

    def to_dict(self):
        return {"trs": self.trs, "atr": self.atr, "fub": self.fub,
                "flb": self.flb, "pc": self.pc, "trend": self.trend}

    def push(self, o, h, l, c):
        """Advance one COMPLETED bar. Returns +1/-1 on a flip, else None."""
        tr = h - l if self.pc is None else max(h - l, abs(h - self.pc), abs(l - self.pc))
        if self.atr is None:                      # seeding ATR(period)
            self.trs.append(tr)
            if len(self.trs) == self.period:
                self.atr = sum(self.trs) / self.period
                self.trs = []
            self.pc = c
            return None
        self.atr = (self.atr * (self.period - 1) + tr) / self.period
        hl2 = (h + l) / 2
        ub = hl2 + self.mult * self.atr
        lb = hl2 - self.mult * self.atr
        if self.fub is None or ub < self.fub or self.pc > self.fub: self.fub = ub
        if self.flb is None or lb > self.flb or self.pc < self.flb: self.flb = lb
        prev = self.trend; new = prev
        if prev <= 0 and c > self.fub: new = 1
        elif prev >= 0 and c < self.flb: new = -1
        self.pc = c
        if new != prev and new != 0:
            self.trend = new
            return new
        self.trend = new
        return None


class PaperSTStream:
    """Swing Supertrend paper stream fed from engine snapshots (completed bars)."""

    def __init__(self, cfg, paper_dir, mode="live"):
        pcfg = ((getattr(cfg, "raw", {}) or {}).get("paper") or {}).get("supertrend") or {}
        self.enabled = bool(pcfg.get("enabled", False))
        self.period = int(pcfg.get("period", 10))
        self.mult = float(pcfg.get("mult", 3.0))
        self.cm = int((((getattr(cfg, "raw", {}) or {}).get("session") or {}).get("candle_minutes", 15)))
        self.dir = Path(paper_dir)
        self.mode = str(mode or "live")
        sfx = "" if self.mode == "live" else f".{self.mode}"
        self.state_f = self.dir / f"supertrend_state{sfx}.json"
        self.log_f = self.dir / f"supertrend_trades{sfx}.jsonl"
        self.state = {}
        if self.state_f.exists():
            try: self.state = json.loads(self.state_f.read_text())
            except Exception: self.state = {}
        self.closed = []
        if self.log_f.exists():
            for line in self.log_f.read_text().splitlines():
                line = line.strip()
                if line:
                    try: self.closed.append(json.loads(line))
                    except Exception: pass

    def update(self, insts, cur_dt_iso=None):
        if not self.enabled: return
        now_iso = cur_dt_iso or dt.datetime.now().isoformat()
        day = now_iso[:10]
        dirty = False
        for key, v in (insts or {}).items():
            cands = v.get("candles") or []
            if not cands: continue
            s = self.state.setdefault(key, {})
            st = SupertrendState(self.period, self.mult, s.get("st"))
            last = s.get("last_ts", "")
            pos = s.get("pos")
            for c in cands:
                try:
                    ts = f"{day}T{c['t']}"
                    if ts <= last: continue
                    # process only bars whose end time has passed (completed)
                    bs = dt.datetime.fromisoformat(ts)
                    if (bs + dt.timedelta(minutes=self.cm)).isoformat() > now_iso:
                        break
                    flip = st.push(c["o"], c["h"], c["l"], c["c"])
                except Exception:
                    continue
                last = ts; dirty = True
                if flip is not None:
                    px = c["c"]
                    if pos and pos.get("dir") != flip:
                        self._close(key, pos, px, ts); pos = None
                    if pos is None:
                        pos = {"dir": flip, "entry": px, "entry_ts": ts}
            s["st"] = st.to_dict(); s["last_ts"] = last; s["pos"] = pos
        if dirty:
            try: self.state_f.write_text(json.dumps(self.state))
            except Exception: pass

    def _close(self, key, pos, px, ts):
        pts = pos["dir"] * (px - pos["entry"])
        net = pts - SYN_COST_PTS.get(key, 10.0)
        tr = {"source": "supertrend", "mode": self.mode, "instrument": key,
              "direction": "long" if pos["dir"] > 0 else "short",
              "entry": round(pos["entry"], 2), "exit": round(px, 2),
              "entry_dt": pos["entry_ts"], "exit_dt": ts,
              "pnl_pts": round(pts, 2), "pnl_net_pts": round(net, 2),
              "result": "win" if net > 0 else "loss"}
        self.closed.append(tr)
        try:
            with self.log_f.open("a") as f: f.write(json.dumps(tr) + "\n")
        except Exception: pass

    def panel(self):
        n = len(self.closed)
        net = sum(t.get("pnl_net_pts", 0) for t in self.closed)
        wins = sum(1 for t in self.closed if t.get("result") == "win")
        return {"enabled": self.enabled, "closed": n,
                "win_rate": round(100 * wins / n, 1) if n else 0.0,
                "net_pts": round(net, 1),
                "open": {k: s.get("pos") for k, s in self.state.items() if s.get("pos")},
                "recent": list(reversed(self.closed[-6:]))}
