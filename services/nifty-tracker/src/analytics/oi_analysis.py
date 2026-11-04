"""Open-Interest / option-chain analysis around ATM.

From the streamed CE/PE quotes we derive:
  * PCR (OI)            — put OI / call OI (crowding / sentiment)
  * Max pain            — strike minimizing total option writer payout
  * OI support/resistance — strike with the highest put OI (support) and
                            highest call OI (resistance)
  * Buildup map         — per-strike long/short buildup vs OI-open
The OI-open baseline is the first OI seen for each leg today, letting us flag
fresh writing (buildup) vs covering (unwind).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..state import InstrumentState


@dataclass
class OIRead:
    pcr: float | None = None
    pcr_signal: str = "neutral"       # bullish / bearish / neutral
    max_pain: float | None = None
    call_wall: float | None = None    # resistance
    put_wall: float | None = None     # support
    total_call_oi: float = 0.0
    total_put_oi: float = 0.0
    buildup: list[dict] = field(default_factory=list)   # notable strikes (premium+OI)
    chain: list[dict] = field(default_factory=list)      # compact per-strike view
    options_bias: str = "neutral"     # bullish / bearish / neutral (net premium+OI flow)
    bias_detail: str = ""             # short human summary of dominant activity

    def to_dict(self):
        return {
            "pcr": _r(self.pcr, 2), "pcr_signal": self.pcr_signal,
            "max_pain": _r(self.max_pain), "call_wall": _r(self.call_wall),
            "put_wall": _r(self.put_wall),
            "total_call_oi": int(self.total_call_oi),
            "total_put_oi": int(self.total_put_oi),
            "buildup": self.buildup[:6],
            "options_bias": self.options_bias, "bias_detail": self.bias_detail,
            "chain": self.chain,
        }


def _r(x, n=2):
    return None if x is None else round(x, n)


def compact_chain(options: dict, price=None) -> list[dict]:
    """A compact per-strike OI view for one expiry book (for the chain viewer).
    options: {(strike, leg): OptionQuote}. Marks ATM as the strike nearest price."""
    if not options:
        return []
    strikes = sorted({k[0] for k in options})
    call_oi, put_oi, call_open, put_open = {}, {}, {}, {}
    for (s, leg), q in options.items():
        (call_oi if leg == "CE" else put_oi)[s] = q.oi
        (call_open if leg == "CE" else put_open)[s] = q.oi_open or 0
    atm = min(strikes, key=lambda s: abs(s - price)) if price is not None else None
    out = []
    for s in strikes:
        ce, pe = call_oi.get(s, 0), put_oi.get(s, 0)
        ceo, peo = call_open.get(s, 0), put_open.get(s, 0)
        out.append({
            "strike": s, "ce_oi": int(ce), "pe_oi": int(pe),
            "ce_chg": int(ce - ceo) if ceo else 0,
            "pe_chg": int(pe - peo) if peo else 0,
            "atm": bool(atm is not None and abs(s - atm) < 1e-6),
        })
    return out


def _max_pain(strikes, call_oi, put_oi):
    best, best_pay = None, None
    for expiry_px in strikes:
        pay = 0.0
        for s in strikes:
            pay += call_oi.get(s, 0) * max(0.0, expiry_px - s)
            pay += put_oi.get(s, 0) * max(0.0, s - expiry_px)
        if best_pay is None or pay < best_pay:
            best_pay, best = pay, expiry_px
    return best


def analyze(st: InstrumentState, pcr_bull_below: float = 0.7,
            pcr_bear_above: float = 1.3, buildup_pct: float = 5.0) -> OIRead:
    out = OIRead()
    if not st.options:
        return out

    strikes = sorted({k[0] for k in st.options})
    call_oi, put_oi = {}, {}
    call_oi_open, put_oi_open = {}, {}
    for (strike, leg), q in st.options.items():
        (call_oi if leg == "CE" else put_oi)[strike] = q.oi
        (call_oi_open if leg == "CE" else put_oi_open)[strike] = q.oi_open or 0

    out.total_call_oi = sum(call_oi.values())
    out.total_put_oi = sum(put_oi.values())
    if out.total_call_oi > 0:
        out.pcr = out.total_put_oi / out.total_call_oi
        if out.pcr < pcr_bull_below:
            out.pcr_signal = "bullish"
        elif out.pcr > pcr_bear_above:
            out.pcr_signal = "bearish"

    if call_oi:
        out.call_wall = max(call_oi, key=lambda s: call_oi[s])
    if put_oi:
        out.put_wall = max(put_oi, key=lambda s: put_oi[s])
    out.max_pain = _max_pain(strikes, call_oi, put_oi)

    atm = st.atm_strike()

    # Premium + OI classification per strike (vs day-open). Premium direction
    # separates WRITING from BUYING, and COVERING from UNWINDING.
    #   CE: oi+ prem+ = call_long(bull) | oi+ prem- = call_writing(bear)
    #       oi- prem- = call_unwind(bear) | oi- prem+ = call_covering(bull)
    #   PE: oi+ prem+ = put_long(bear)  | oi+ prem- = put_writing(bull)
    #       oi- prem- = put_unwind(bull) | oi- prem+ = put_covering(bear)
    _BULL = {"call_long", "call_covering", "put_writing", "put_unwind"}
    _BEAR = {"call_writing", "call_unwind", "put_long", "put_covering"}
    prem_thr = 3.0    # % premium move to count a direction
    bull_w = bear_w = 0.0
    focus = 6         # ATM +/- N strikes for the aggregate bias
    strikes_sorted = strikes
    if atm is not None:
        strikes_sorted = sorted(strikes, key=lambda s: abs(s - atm))[:2 * focus + 1]
    focus_set = set(strikes_sorted)

    for (strike, leg), q in sorted(st.options.items()):
        if not (q.oi_open and q.oi_open > 0 and q.ltp_open and q.ltp_open > 0):
            continue
        oi_chg = (q.oi - q.oi_open) / q.oi_open * 100
        prem_chg = (q.ltp - q.ltp_open) / q.ltp_open * 100
        if abs(oi_chg) < buildup_pct:
            continue
        oi_up = oi_chg > 0
        prem_up = prem_chg > prem_thr
        prem_dn = prem_chg < -prem_thr
        kind = None
        if leg == "CE":
            if oi_up and prem_up:   kind = "call_long"
            elif oi_up and prem_dn: kind = "call_writing"
            elif not oi_up and prem_dn: kind = "call_unwind"
            elif not oi_up and prem_up: kind = "call_covering"
        else:
            if oi_up and prem_up:   kind = "put_long"
            elif oi_up and prem_dn: kind = "put_writing"
            elif not oi_up and prem_dn: kind = "put_unwind"
            elif not oi_up and prem_up: kind = "put_covering"
        if not kind:
            continue
        entry = {"strike": strike, "leg": leg, "oi_chg_pct": round(oi_chg, 1),
                 "prem_chg_pct": round(prem_chg, 1), "kind": kind}
        out.buildup.append(entry)
        # aggregate bias over the ATM focus window, weighted by |OI change|
        if strike in focus_set:
            w = abs(q.oi - q.oi_open)
            if kind in _BULL:
                bull_w += w
            elif kind in _BEAR:
                bear_w += w
    out.buildup.sort(key=lambda d: -abs(d["oi_chg_pct"]))

    tot = bull_w + bear_w
    if tot > 0:
        share = (bull_w - bear_w) / tot
        if share > 0.2:
            out.options_bias = "bullish"
        elif share < -0.2:
            out.options_bias = "bearish"
        top = out.buildup[:3]
        out.bias_detail = ", ".join(f"{b['kind']} {b['strike']:g}" for b in top)

    for s in strikes:
        ce, pe = call_oi.get(s, 0), put_oi.get(s, 0)
        ceo, peo = call_oi_open.get(s, 0), put_oi_open.get(s, 0)
        out.chain.append({
            "strike": s,
            "ce_oi": int(ce),
            "pe_oi": int(pe),
            "ce_chg": int(ce - ceo) if ceo else 0,   # +buildup / -unwind vs day-open
            "pe_chg": int(pe - peo) if peo else 0,
            "atm": bool(atm is not None and abs(s - atm) < 1e-6),
        })
    return out
