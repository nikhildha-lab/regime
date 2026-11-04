"""Alert dispatch: Telegram + macOS desktop notifications, with dedup.

A signal only alerts once per `alert_cooldown_sec` window (keyed by
instrument+direction+title) so a persistent confluence doesn't spam you. Also
writes snapshot.json every cycle for the trading-skill bridge.
"""
from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import requests

from .config import Config, ROOT


class Alerter:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.cooldown = int(cfg.a("signals", "alert_cooldown_sec", default=300))
        # Telegram alerts fire only on QUALIFIED setups (>= this score), a higher
        # bar than the dashboard's min_confluence so your phone isn't spammed.
        self.min_score = int(cfg.a("alerts", "min_score",
                                   default=cfg.a("paper", "min_score", default=3)))
        self.eod_summary = bool(cfg.a("alerts", "eod_summary", default=True))
        self.close_t = str(cfg.a("session", "market_close", default="15:30"))
        self._last: dict[str, float] = {}
        self._eod_sent_for: str | None = None
        self.telegram = bool(cfg.a("alerts", "telegram", default=True)) and \
            bool(cfg.telegram_token and cfg.telegram_chat_id)
        self.desktop = bool(cfg.a("alerts", "desktop", default=True))
        _sj = cfg.a("alerts", "snapshot_json", default="")       # 2026-11-03 audit: off unless configured (no reader; 10 MB a cycle)
        self.snapshot_path = (ROOT / str(_sj)) if _sj else None

    def _due(self, key: str, now: float) -> bool:
        last = self._last.get(key, 0)
        if now - last >= self.cooldown:
            self._last[key] = now
            return True
        return False

    def dispatch(self, snapshot: dict):
        # persist snapshot for the trading-skill bridge
        try:
            if self.snapshot_path is not None:
                self.snapshot_path.write_text(json.dumps(snapshot))
        except Exception as e:  # pragma: no cover
            print(f"[alerts] snapshot write failed: {e}")

        now = time.time()
        for s in snapshot.get("top_signals", []):
            key = f"{s['instrument']}:{s['direction']}:{s['title']}"
            if s["score"] < self.min_score:           # qualified setups only
                continue
            if not self._due(key, now):
                continue
            self._emit(s)

        if self.eod_summary:
            self._maybe_eod(snapshot)

    def _emit(self, s: dict):
        arrow = "🟢 LONG" if s["direction"] == "long" else "🔴 SHORT"
        price = f" @ {s['price']}" if s.get("price") is not None else ""
        lines = [f"{arrow}  {s['instrument']}{price}  (score {s['score']})",
                 s["rationale"]]
        p = s.get("plan")
        if p:
            t2 = f" → {p['target2']}" if p.get("target2") is not None else ""
            lines.append(f"• {p['instrument']}")
            lines.append(f"• Entry {p['entry']} | SL {p['stop_loss']} "
                         f"| T1 {p['target1']}{t2} | R:R {p.get('rr','—')}")
            if p.get("note"):
                lines.append(f"• {p['note']}")
        msg = "\n".join(lines)
        print(f"[ALERT] {msg}")
        if self.telegram:
            self._telegram(msg)
        if self.desktop:
            self._desktop(f"{s['instrument']} {s['direction'].upper()}", s["rationale"][:180])

    def _maybe_eod(self, snapshot: dict):
        """Send one paper-trade performance summary at/after market close per day."""
        insts = snapshot.get("instruments", {})
        sess_time = None
        for v in insts.values():
            cs = v.get("candles") or []
            if cs:
                sess_time = max(sess_time, cs[-1]["t"]) if sess_time else cs[-1]["t"]
        if not sess_time or sess_time < self.close_t:
            return
        day = snapshot.get("generated_at", "")[:10]
        if self._eod_sent_for == day:
            return
        self._eod_sent_for = day
        st = (snapshot.get("paper") or {}).get("stats") or {}
        if not st:
            return
        msg = (f"📊 EOD summary {day}\n"
               f"Paper trades: {st.get('closed',0)} closed | "
               f"win {st.get('win_rate',0)}% | avg {st.get('avg_r',0)}R | "
               f"expectancy {st.get('expectancy_r',0)}R | gross {st.get('gross_r',0)}R")
        print(f"[EOD] {msg}")
        if self.telegram:
            self._telegram(msg)
        if self.desktop:
            self._desktop(f"EOD {day}", msg)

    def _telegram(self, text: str):
        try:
            requests.post(
                f"https://api.telegram.org/bot{self.cfg.telegram_token}/sendMessage",
                json={"chat_id": self.cfg.telegram_chat_id, "text": text},
                timeout=8,
            )
        except Exception as e:  # pragma: no cover
            print(f"[alerts] telegram failed: {e}")

    def _desktop(self, title: str, body: str):
        # macOS native notification; no-op/fails silently elsewhere
        try:
            body = body.replace('"', "'")
            title = title.replace('"', "'")
            subprocess.run(
                ["osascript", "-e",
                 f'display notification "{body}" with title "{title}"'],
                check=False, capture_output=True, timeout=5,
            )
        except Exception:
            pass
