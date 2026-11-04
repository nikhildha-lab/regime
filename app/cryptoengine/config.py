"""Configuration loading: merges config.yaml with .env secrets (crypto engine inside Market Pulse).

Crypto tracker — data comes from Hyperliquid's PUBLIC market-data API, so no
exchange keys are required to run (live or sim). The only optional secrets are
Telegram alert credentials.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import time
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

# 2026-11-01: engine merged into Market Pulse (app/cryptoengine, from Documents/crypto-tracker/src).
# ROOT = the engine's DATA folder (config.yaml, paper/, datasets/, snapshot.json), not the code folder.
ROOT = Path(os.getenv("CRYPTO_ENGINE_ROOT") or Path(__file__).resolve().parents[2] / "data_store" / "crypto_engine")


def _parse_hhmm(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


@dataclass
class InstrumentCfg:
    key: str
    coin: str                 # Hyperliquid perp symbol
    tick_size: float | None = None      # auto-derived from mark price when omitted
    profile_bin: float | None = None    # auto-derived from mark price when omitted
    source: str | None = None           # "bn" = force Binance USD-M futures (2026-11-01: BTC / ETH / SOL)


@dataclass
class Config:
    raw: dict[str, Any]
    instruments: list[InstrumentCfg]
    # secrets / runtime
    telegram_token: str = ""
    telegram_chat_id: str = ""
    run_mode: str = "sim"

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

    @property
    def info_url(self) -> str:
        return str(self.a("hyperliquid", "info_url",
                          default="https://api.hyperliquid.xyz/info"))

    @property
    def ws_url(self) -> str:
        return str(self.a("hyperliquid", "ws_url",
                          default="wss://api.hyperliquid.xyz/ws"))

    def a(self, *keys, default=None):
        """Nested lookup into raw config, e.g. cfg.a('analytics','value_area_pct')."""
        node = self.raw
        for k in keys:
            if not isinstance(node, dict) or k not in node:
                return default
            node = node[k]
        return node


def load_config(path: str | Path | None = None) -> Config:
    load_dotenv(ROOT / ".env")
    cfg_path = Path(path) if path else ROOT / "config.yaml"
    raw = yaml.safe_load(cfg_path.read_text())

    instruments = [InstrumentCfg(**i) for i in raw["instruments"]]

    return Config(
        raw=raw,
        instruments=instruments,
        telegram_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
        telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", "").strip(),
        run_mode=os.getenv("RUN_MODE", raw.get("_run_mode", "sim")).strip().lower(),
    )
