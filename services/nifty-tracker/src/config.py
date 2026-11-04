"""Configuration loading: merges config.yaml with .env secrets."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import time
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent


def _parse_hhmm(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


@dataclass
class InstrumentCfg:
    key: str
    index_symbol: str
    fut_name: str
    opt_name: str
    strike_step: int
    strikes_each_side: int
    tick_size: float
    profile_bin: float


@dataclass
class Config:
    raw: dict[str, Any]
    instruments: list[InstrumentCfg]
    # secrets
    api_key: str = ""
    api_secret: str = ""
    access_token: str = ""
    telegram_token: str = ""
    telegram_chat_id: str = ""
    run_mode: str = "sim"
    broker: str = "kite"          # "kite" | "dhan"
    dhan_client_id: str = ""
    dhan_access_token: str = ""

    # convenience accessors -------------------------------------------------
    @property
    def market_open(self) -> time:
        return _parse_hhmm(self.raw["session"]["market_open"])

    @property
    def market_close(self) -> time:
        return _parse_hhmm(self.raw["session"]["market_close"])

    @property
    def candle_minutes(self) -> int:
        return int(self.raw["session"]["candle_minutes"])

    @property
    def ib_minutes(self) -> int:
        return int(self.raw["session"]["initial_balance_minutes"])

    def a(self, *keys, default=None):
        """Nested lookup into raw config, e.g. cfg.a('analytics','value_area_pct')."""
        node = self.raw
        for k in keys:
            if not isinstance(node, dict) or k not in node:
                return default
            node = node[k]
        return node


def _dhan_env_fallback():
    """Reuse credentials from ~/Documents/dhan/.env (set up separately) when
    .dhan_token does not exist yet. Values are read, never printed."""
    p = __import__("os").environ.get("MP_DHAN_DIR") and Path(__import__("os").environ["MP_DHAN_DIR"]) / ".env" or Path.home() / "Documents" / "dhan" / ".env"
    out = {}
    try:
        for line in p.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    except Exception:
        pass
    return out.get("DHAN_CLIENT_ID", ""), out.get("DHAN_ACCESS_TOKEN", "")


def load_config(path: str | Path | None = None) -> Config:
    load_dotenv(ROOT / ".env")
    cfg_path = Path(path) if path else ROOT / "config.yaml"
    raw = yaml.safe_load(cfg_path.read_text())

    instruments = [InstrumentCfg(**i) for i in raw["instruments"]]

    token = os.getenv("KITE_ACCESS_TOKEN", "").strip()
    api_key = os.getenv("KITE_API_KEY", "").strip()
    token_file = ROOT / ".kite_token"
    if not token and token_file.exists():
        token = token_file.read_text().strip()

    # Reuse an existing ~/Documents/NSE daily Kite login if present (so live
    # streaming and history share one token system with your other tools).
    if not token:
        import json
        nse_tok = Path.home() / "Documents" / "NSE" / "data" / "kite_token.json"
        if nse_tok.exists():
            try:
                d = json.loads(nse_tok.read_text())
                from datetime import date
                if d.get("date") == date.today().isoformat():
                    token = d.get("access_token", "")
                    if not api_key:
                        api_key = d.get("api_key", "")
            except Exception:
                pass

    # Dhan credentials live in .dhan_token (written by `python -m src.dhan_auth`)
    dhan_cid, dhan_tok = "", ""
    dfile = ROOT / ".dhan_token"
    if dfile.exists():
        try:
            import json as _json
            _d = _json.loads(dfile.read_text())
            dhan_cid = str(_d.get("client_id", "")).strip()
            dhan_tok = str(_d.get("access_token", "")).strip()
        except Exception:
            pass
    if not dhan_tok:
        dhan_cid, dhan_tok = _dhan_env_fallback()

    return Config(
        broker=os.getenv("BROKER", str(raw.get("broker", "kite"))).strip().lower(),
        dhan_client_id=dhan_cid,
        dhan_access_token=dhan_tok,
        raw=raw,
        instruments=instruments,
        api_key=api_key,
        api_secret=os.getenv("KITE_API_SECRET", "").strip(),
        access_token=token,
        telegram_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
        telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", "").strip(),
        run_mode=os.getenv("RUN_MODE", raw.get("_run_mode", "sim")).strip().lower(),
    )
