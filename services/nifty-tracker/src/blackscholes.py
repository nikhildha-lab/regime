"""Minimal Black-Scholes option pricer for the vehicle-comparison backtest.

We can't fetch expired option premiums, so we price ATM options off the
underlying move: entry premium at the entry spot/time, exit premium at the exit
spot/time (same strike, same IV, less time-to-expiry). This captures the three
things that make options behave differently from futures — delta, gamma
(convexity), and theta (time decay) — without needing historical option data.

Assumptions (configurable in config.yaml -> vehicles): per-index IV, risk-free
rate, and expiry convention (weekly = nearest Thursday, monthly = last Thursday).
No bid/ask, no IV smile, no vol-crush modelling — a first-order comparison.
"""
from __future__ import annotations

import datetime as dt
from math import erf, exp, log, sqrt


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + erf(x / sqrt(2.0)))


def bs_price(S: float, K: float, T: float, r: float, sigma: float, typ: str) -> float:
    """European option price. typ in {'CE','PE'}. T in years."""
    typ = typ.upper()
    if T <= 0 or sigma <= 0 or S <= 0:
        return max(0.0, S - K) if typ == "CE" else max(0.0, K - S)
    d1 = (log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * sqrt(T))
    d2 = d1 - sigma * sqrt(T)
    if typ == "CE":
        return S * _norm_cdf(d1) - K * exp(-r * T) * _norm_cdf(d2)
    return K * exp(-r * T) * _norm_cdf(-d2) - S * _norm_cdf(-d1)


def next_thursday(d: dt.date) -> dt.date:
    # Thursday = weekday 3; if d is Thursday, expiry is same day
    return d + dt.timedelta(days=(3 - d.weekday()) % 7)


def last_thursday_of_month(d: dt.date) -> dt.date:
    # last day of month, walk back to Thursday
    if d.month == 12:
        nxt = dt.date(d.year + 1, 1, 1)
    else:
        nxt = dt.date(d.year, d.month + 1, 1)
    last = nxt - dt.timedelta(days=1)
    return last - dt.timedelta(days=(last.weekday() - 3) % 7)


def expiry_for(trade_date: dt.date, mode: str) -> dt.date:
    """Nearest expiry date for the given convention."""
    if mode == "monthly":
        e = last_thursday_of_month(trade_date)
        if e < trade_date:
            nm = dt.date(trade_date.year + (trade_date.month == 12),
                         1 if trade_date.month == 12 else trade_date.month + 1, 1)
            e = last_thursday_of_month(nm)
        return e
    return next_thursday(trade_date)   # weekly


def years_to_expiry(now: dt.datetime, expiry_date: dt.date) -> float:
    """Time to 15:30 IST on expiry, in years (>=0), including intraday fraction."""
    exp_dt = dt.datetime.combine(expiry_date, dt.time(15, 30))
    secs = (exp_dt - now).total_seconds()
    return max(0.0, secs) / (365.0 * 24 * 3600)
