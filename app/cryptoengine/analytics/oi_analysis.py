"""Perp open-interest / funding / liquidation analysis (crypto replacement for the
NSE option-chain OI panel).

Crypto perps have no option chain, so the classic PCR / max-pain / call-&-put-wall
read doesn't exist. Instead we derive the crowd-positioning picture natively:

  * Open interest   — current OI, and buildup vs the day-open OI. Combined with
                      price direction (see participation.py) this gives the
                      long-buildup / short-buildup / covering / unwinding quadrant.
  * Funding rate    — Hyperliquid charges funding HOURLY. Positive funding = longs
                      pay shorts = the crowd is long. Very positive funding is a
                      CONTRARIAN bearish lean (over-leveraged longs = squeeze fuel);
                      very negative is a contrarian bullish lean.
  * Liquidation     — Hyperliquid has no public liquidation feed, so clusters are
    clusters          ESTIMATED by projecting common leverage tiers (10x/25x/50x)
                      off the current mark. They are MAGNETS (price is drawn toward
                      stacked liquidation liquidity), not barriers — a short-liq
                      cluster above tends to pull price UP into it, and vice-versa.

`call_wall` / `put_wall` are intentionally left None: liquidation clusters are
magnets, not the bounce-walls that option OI provides, so we do NOT feed them into
the "bounce at support" signal path (that would invert the read). They surface as
their own liq-magnet signals instead.
"""
from __future__ import annotations

from dataclasses import dataclass, field

_HOURS_PER_YEAR = 24 * 365


@dataclass
class OIRead:
    oi: float | None = None
    oi_open: float | None = None
    oi_chg_pct: float | None = None
    funding: float | None = None            # current hourly rate (decimal)
    funding_annual_pct: float | None = None
    funding_signal: str = "neutral"         # crowded_long / crowded_short / neutral
    funding_bias: str = "neutral"           # contrarian lean: bullish / bearish / neutral
    liq_clusters: list[dict] = field(default_factory=list)
    liq_up: float | None = None             # nearest short-liq magnet above
    liq_dn: float | None = None             # nearest long-liq magnet below
    net_bias: str = "neutral"               # bullish / bearish / neutral
    bias_detail: str = ""
    # compatibility shims (option walls don't exist for perps -> None on purpose)
    call_wall: float | None = None
    put_wall: float | None = None
    pcr: float | None = None
    pcr_signal: str = "neutral"

    def to_dict(self):
        return {
            "oi": None if self.oi is None else round(self.oi, 2),
            "oi_open": None if self.oi_open is None else round(self.oi_open, 2),
            "oi_chg_pct": _r(self.oi_chg_pct, 1),
            "funding": self.funding,
            "funding_annual_pct": _r(self.funding_annual_pct, 2),
            "funding_signal": self.funding_signal,
            "funding_bias": self.funding_bias,
            "liq_clusters": self.liq_clusters,
            "liq_up": _r(self.liq_up), "liq_dn": _r(self.liq_dn),
            "net_bias": self.net_bias, "bias_detail": self.bias_detail,
        }


def _r(x, n=2):
    return None if x is None else round(x, n)


def _estimate_liq_clusters(price, lev_tiers, cluster_bin_pct):
    """Project liquidation levels off the current mark for each leverage tier.

    A trader long at ~mark with isolated leverage L is liquidated near
    mark*(1 - 1/L); a short near mark*(1 + 1/L). We surface both sides as magnets,
    tagged with leverage and distance. Nearby tiers are merged into one cluster.
    """
    if not price or price <= 0:
        return []
    raw = []
    for L in lev_tiers:
        if L <= 1:
            continue
        raw.append({"price": price * (1 - 1.0 / L), "side": "long", "leverage": L})
        raw.append({"price": price * (1 + 1.0 / L), "side": "short", "leverage": L})
    raw.sort(key=lambda d: d["price"])
    merged = []
    for c in raw:
        if merged and c["side"] == merged[-1]["side"] and \
                abs(c["price"] - merged[-1]["price"]) / price * 100 <= cluster_bin_pct:
            # merge: keep the lower-leverage (closer-in, heavier) label
            merged[-1]["leverage"] = min(merged[-1]["leverage"], c["leverage"])
            merged[-1]["price"] = (merged[-1]["price"] + c["price"]) / 2
        else:
            merged.append(dict(c))
    for c in merged:
        c["price"] = round(c["price"], 2)
        c["dist_pct"] = round((c["price"] - price) / price * 100, 2)
    return merged


def analyze(st, price=None, funding_long_ann=30.0, funding_short_ann=-8.0,
            lev_tiers=(10, 25, 50), cluster_bin_pct=0.4,
            oi_buildup_pct=3.0) -> OIRead:
    out = OIRead()
    price = price or st.mark or st.last_price or None

    # ---- open interest + buildup vs day-open -----------------------------
    out.oi = st.oi or None
    out.oi_open = st.oi_open
    if st.oi and st.oi_open:
        out.oi_chg_pct = (st.oi - st.oi_open) / st.oi_open * 100

    # ---- funding ---------------------------------------------------------
    out.funding = st.funding
    if st.funding is not None:
        out.funding_annual_pct = st.funding * _HOURS_PER_YEAR * 100
        if out.funding_annual_pct >= funding_long_ann:
            out.funding_signal = "crowded_long"
            out.funding_bias = "bearish"           # contrarian
        elif out.funding_annual_pct <= funding_short_ann:
            out.funding_signal = "crowded_short"
            out.funding_bias = "bullish"           # contrarian

    # ---- estimated liquidation magnets -----------------------------------
    if price:
        out.liq_clusters = _estimate_liq_clusters(price, list(lev_tiers),
                                                  cluster_bin_pct)
        ups = [c["price"] for c in out.liq_clusters if c["price"] > price]
        dns = [c["price"] for c in out.liq_clusters if c["price"] < price]
        out.liq_up = min(ups) if ups else None
        out.liq_dn = max(dns) if dns else None

    # ---- net bias (funding contrarian + OI direction) --------------------
    bull = bear = 0.0
    detail = []
    if out.funding_bias == "bullish":
        bull += 1
        detail.append(f"funding {out.funding_annual_pct:.0f}%/yr (shorts crowded)")
    elif out.funding_bias == "bearish":
        bear += 1
        detail.append(f"funding {out.funding_annual_pct:.0f}%/yr (longs crowded)")
    # OI expansion alongside the last move: rising OI = fresh conviction either way;
    # this is a weak tie-breaker only (participation.py does the graded version).
    if out.oi_chg_pct is not None and abs(out.oi_chg_pct) >= oi_buildup_pct:
        detail.append(f"OI {out.oi_chg_pct:+.1f}% vs day-open")

    tot = bull + bear
    if tot > 0:
        share = (bull - bear) / tot
        out.net_bias = "bullish" if share > 0.2 else "bearish" if share < -0.2 else "neutral"
    out.bias_detail = "; ".join(detail)
    return out
