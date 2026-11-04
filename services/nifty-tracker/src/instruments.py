"""Resolve tradable instrument tokens for each tracked index.

For every configured instrument we resolve:
  * the spot index token           (price-action structure)
  * the near-month futures token    (volume + market profile)
  * ATM +/- N option tokens (CE+PE) (OI analysis)

Expiries are resolved DYNAMICALLY from the live instrument dump rather than
hardcoded, because NSE has repeatedly changed weekly-expiry weekdays and
Bank Nifty's weekly options were discontinued. We always pick the nearest
expiry available for each underlying.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from .config import Config, InstrumentCfg


@dataclass
class ResolvedInstrument:
    key: str
    cfg: InstrumentCfg
    spot_token: int
    fut_token: int
    fut_tradingsymbol: str
    expiry: dt.date | None                       # nearest expiry (trading vehicle)
    # strike -> {"CE": token, "PE": token}   (nearest expiry; kept for compatibility)
    option_tokens: dict[float, dict[str, int]] = field(default_factory=dict)
    # expiry(date) -> {strike -> {"CE"/"PE": token}}   (all streamed expiries)
    option_tokens_by_expiry: dict = field(default_factory=dict)
    expiries: list = field(default_factory=list)  # sorted nearest-first
    token_meta: dict[int, dict] = field(default_factory=dict)  # token -> descriptor

    def all_tokens(self) -> list[int]:
        toks = [self.spot_token, self.fut_token]
        obe = self.option_tokens_by_expiry or {self.expiry: self.option_tokens}
        for by_strike in obe.values():
            for legs in by_strike.values():
                toks.extend(legs.values())
        return [t for t in toks if t]


def _nearest_expiry(rows: list[dt.date], today: dt.date) -> dt.date | None:
    future = sorted(e for e in rows if e and e >= today)
    return future[0] if future else (sorted(rows)[-1] if rows else None)


def _nearest_expiries(rows: list[dt.date], today: dt.date, n: int) -> list[dt.date]:
    future = sorted({e for e in rows if e and e >= today})
    return future[:n] if future else (sorted({e for e in rows if e})[-n:] if rows else [])


def resolve_all(kite, cfg: Config, atm_hint: dict[str, float] | None = None
                ) -> dict[str, ResolvedInstrument]:
    """Build ResolvedInstrument map. `kite` may be a real client or a SimBroker
    exposing .instruments('NFO') / .instruments('NSE')."""
    atm_hint = atm_hint or {}
    today = dt.date.today()
    n_exp = max(1, int(cfg.a("chain", "expiries", default=3)))

    try:                                   # 2026-10-23: tell the Dhan client which stocks to load from its scrip master
        from . import dhan_client as _dc
        _dc.STOCKS.update(i.fut_name.upper() for i in cfg.instruments if i.key not in ("NIFTY", "BANKNIFTY"))
    except Exception:
        pass
    nse = kite.instruments("NSE")
    nfo = kite.instruments("NFO")

    # index token lookup
    idx_tokens = {
        r["tradingsymbol"] or r.get("name"): r["instrument_token"]
        for r in nse
        if r.get("segment") == "INDICES" or r.get("instrument_type") == "EQ"
    }
    # Kite lists indices with name in 'tradingsymbol' for INDICES segment
    idx_by_name = {}
    for r in nse:
        if r.get("segment") == "INDICES":
            idx_by_name[r.get("tradingsymbol") or r.get("name")] = r["instrument_token"]

    resolved: dict[str, ResolvedInstrument] = {}

    for ins in cfg.instruments:
      try:
          spot_token = idx_by_name.get(ins.index_symbol) or idx_tokens.get(ins.index_symbol)
          if not spot_token:
              raise RuntimeError(f"Could not find spot token for {ins.index_symbol}")

          fut_rows = [r for r in nfo
                      if r.get("name") == ins.fut_name and r.get("instrument_type") == "FUT"]
          fut_expiries = [_as_date(r["expiry"]) for r in fut_rows]
          fut_exp = _nearest_expiry(fut_expiries, today)
          fut_row = min(
              (r for r in fut_rows if _as_date(r["expiry"]) == fut_exp),
              key=lambda r: r["tradingsymbol"], default=None,
          )
          if not fut_row:
              raise RuntimeError(f"No futures found for {ins.fut_name}")

          opt_rows = [r for r in nfo
                      if r.get("name") == ins.opt_name
                      and r.get("instrument_type") in ("CE", "PE")]
          opt_expiries = [_as_date(r["expiry"]) for r in opt_rows]
          exp_list = _nearest_expiries(opt_expiries, today, n_exp)
          opt_exp = exp_list[0] if exp_list else _nearest_expiry(opt_expiries, today)

          ri = ResolvedInstrument(
              key=ins.key, cfg=ins,
              spot_token=spot_token,
              fut_token=fut_row["instrument_token"],
              fut_tradingsymbol=fut_row["tradingsymbol"],
              expiry=opt_exp,
              expiries=list(exp_list),
          )
          ri.token_meta[spot_token] = {"role": "spot", "symbol": ins.index_symbol}
          ri.token_meta[fut_row["instrument_token"]] = {
              "role": "fut", "symbol": fut_row["tradingsymbol"]}

          # ATM strikes around a hint (or median), from the nearest expiry's grid
          atm = atm_hint.get(ins.key)
          strikes_avail = sorted({float(r["strike"]) for r in opt_rows
                                  if _as_date(r["expiry"]) == opt_exp and r["strike"]})
          if not strikes_avail:
              resolved[ins.key] = ri
              continue
          if atm is None and type(kite).__name__ != "SimBroker":
              # 2026-10-23 fix: centre the option window on the LAST SPOT PRICE, not the middle of the strike list
              # (the old median put the window ~800 pts away from spot -> wrong PCR / walls / max pain)
              now = dt.datetime.now()
              for iv, back in (("15minute", 4), ("day", 10)):
                  try:
                      bars = kite.historical_data(spot_token, now - dt.timedelta(days=back), now, iv)
                      if bars:
                          atm = float(bars[-1]["close"]); break
                  except Exception as e:
                      print(f"[resolve] {ins.key}: spot for ATM via {iv} failed: {e}")
          if atm is None:
              atm = strikes_avail[len(strikes_avail) // 2]
          if not ins.strike_step:                       # stocks: strike interval from the grid near the money
              near = sorted(strikes_avail, key=lambda k: abs(k - atm))[:8]; near.sort()
              steps = sorted(b - a for a, b in zip(near, near[1:]) if b > a)
              ins.strike_step = steps[len(steps) // 2] if steps else 1.0
          if not ins.profile_bin:                       # stocks: ~0.05% of price, at least one tick
              ins.profile_bin = max(ins.tick_size or 0.05, round(atm * 0.0005, 2))
          print(f"[resolve] {ins.key}: option window centred on {atm}")
          atm_round = min(strikes_avail, key=lambda s: abs(s - atm))
          wanted = {atm_round + i * ins.strike_step
                    for i in range(-ins.strikes_each_side, ins.strikes_each_side + 1)}

          # build an option-token book per streamed expiry (same wanted strikes)
          for exp in exp_list:
              by_strike: dict[float, dict[str, int]] = {}
              for r in opt_rows:
                  if _as_date(r["expiry"]) != exp:
                      continue
                  strike = float(r["strike"])
                  if strike not in wanted:
                      continue
                  leg = r["instrument_type"]  # CE / PE
                  by_strike.setdefault(strike, {})[leg] = r["instrument_token"]
                  ri.token_meta[r["instrument_token"]] = {
                      "role": "opt", "leg": leg, "strike": strike,
                      "expiry": exp.isoformat() if exp else None,
                      "symbol": r["tradingsymbol"],
                  }
              if by_strike:
                  ri.option_tokens_by_expiry[exp] = by_strike
          # nearest expiry kept as the default `option_tokens` (trading vehicle)
          ri.option_tokens = ri.option_tokens_by_expiry.get(opt_exp, {})
          resolved[ins.key] = ri
      except Exception as _e:
        if ins.key in ("NIFTY", "BANKNIFTY"):
            raise
        print(f"[resolve] {ins.key}: skipped ({_e})")      # a stock that fails never stops the tracker

    return resolved


def _as_date(v) -> dt.date | None:
    if v in (None, ""):
        return None
    if isinstance(v, dt.datetime):
        return v.date()
    if isinstance(v, dt.date):
        return v
    if isinstance(v, str):
        try:
            return dt.datetime.strptime(v[:10], "%Y-%m-%d").date()
        except ValueError:
            return None
    return None
