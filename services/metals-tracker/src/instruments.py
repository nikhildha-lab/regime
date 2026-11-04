"""Resolve tradable instruments for each tracked coin.

Crypto is far simpler than the index/futures/options split of the NSE tracker:
each coin (BTC/ETH/SOL) is a single Hyperliquid PERPETUAL that carries price,
real traded volume, AND open interest in one stream. So there are no tokens, no
expiries and no option chain to resolve — we just confirm the coin exists in the
Hyperliquid universe and record its size precision (szDecimals) and max leverage.
"""
from __future__ import annotations

from dataclasses import dataclass

from .config import Config, InstrumentCfg


@dataclass
class ResolvedInstrument:
    key: str
    cfg: InstrumentCfg
    coin: str
    sz_decimals: int = 4
    max_leverage: int = 20
    source: str = "hl"            # "hl" (Hyperliquid) or "bn" (Binance futures)


def resolve_all(client, cfg: Config, bn_client=None) -> dict[str, ResolvedInstrument]:
    """`client` is the primary source (HLData live, SimBroker sim). When
    `bn_client` (BinanceData) is given, coins missing from the primary universe
    fall back to Binance USD-M futures automatically."""
    universe = {}
    try:
        universe = client.universe()
    except Exception as e:
        print(f"[instruments] universe fetch failed ({e}); using defaults")

    marks = {}
    try:
        marks = {c: float(v.get("markPx") or 0) for c, v in client.contexts().items()}
    except Exception:
        pass

    bn_uni, bn_marks = {}, {}
    if bn_client is not None:
        try:
            bn_uni = bn_client.universe()
        except Exception as e:
            print(f"[instruments] binance universe failed ({e})")
        try:
            bn_marks = {c: float(v.get("markPx") or 0)
                        for c, v in bn_client.contexts().items()}
        except Exception:
            pass

    resolved: dict[str, ResolvedInstrument] = {}
    skipped, via_bn = [], []
    for ins in cfg.instruments:
        source = "hl"
        meta = universe.get(ins.coin)
        mark = marks.get(ins.coin) or 0.0
        if universe and meta is None:
            bn_coin = ins.coin[1:].upper() if ins.coin.startswith("k") else ins.coin
            bn_coin = "1000" + bn_coin if ins.coin.startswith("k") else bn_coin
            if bn_uni and bn_coin in bn_uni:
                source, meta = "bn", bn_uni[bn_coin]
                ins.coin = bn_coin
                mark = bn_marks.get(bn_coin) or 0.0
                via_bn.append(bn_coin)
            else:
                skipped.append(ins.coin)
                continue
        meta = meta or {}
        if ins.tick_size is None:
            ins.tick_size = _nice(mark * 1e-5) if mark else 0.001
        if ins.profile_bin is None:
            ins.profile_bin = _nice(mark * 4e-4) if mark else 1.0
        resolved[ins.key] = ResolvedInstrument(
            key=ins.key, cfg=ins, coin=ins.coin,
            sz_decimals=int(meta.get("szDecimals", 4)),
            max_leverage=int(meta.get("maxLeverage", 20)),
            source=source,
        )
    if via_bn:
        print(f"[instruments] via Binance futures: {', '.join(via_bn)}")
    if skipped:
        print(f"[instruments] not on Hyperliquid OR Binance, skipped: {', '.join(skipped)}")
    return resolved


def _nice(x: float) -> float:
    """Round to 1/2/5 x 10^n — a sensible bin/tick ladder."""
    import math
    if x <= 0:
        return 0.001
    e = math.floor(math.log10(x))
    m = x / 10 ** e
    m = 1 if m < 1.5 else 2 if m < 3.5 else 5 if m < 7.5 else 10
    return round(m * 10 ** e, max(0, -e + 1))
