"""Black-Scholes greeks + implied vol, for the option trade ticket.

The app streams live option LTPs, so instead of assuming an IV we back it out of
the actual market price (bisection), then compute delta / theta / gamma / vega at
that IV. Good enough for a trade ticket; it is not an exchange-grade pricer, and
very close to expiry (T -> 0) greeks get twitchy — we floor T to keep them sane.
"""
from __future__ import annotations

import math

SQRT2PI = math.sqrt(2 * math.pi)


def _nd(x):      # standard normal pdf
    return math.exp(-0.5 * x * x) / SQRT2PI


def _Nd(x):      # standard normal cdf
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def _d1d2(S, K, r, T, sigma):
    if S <= 0 or K <= 0 or T <= 0 or sigma <= 0:
        return None, None
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    return d1, d2


def bs_price(S, K, r, T, sigma, typ="CE"):
    d1, d2 = _d1d2(S, K, r, T, sigma)
    if d1 is None:
        # intrinsic fallback
        return max(0.0, (S - K) if typ == "CE" else (K - S))
    if typ == "CE":
        return S * _Nd(d1) - K * math.exp(-r * T) * _Nd(d2)
    return K * math.exp(-r * T) * _Nd(-d2) - S * _Nd(-d1)


def implied_vol(price, S, K, r, T, typ="CE"):
    """Invert BS for sigma via bisection. None if the price is below intrinsic."""
    if price is None or price <= 0 or T <= 0:
        return None
    intrinsic = max(0.0, (S - K) if typ == "CE" else (K - S))
    if price < intrinsic - 1e-6:
        return None
    lo, hi = 1e-4, 5.0
    plo = bs_price(S, K, r, T, lo, typ)
    phi = bs_price(S, K, r, T, hi, typ)
    if not (plo <= price <= phi):
        # price outside solvable band (deep ITM / rounding) — clamp
        return None
    for _ in range(60):
        mid = 0.5 * (lo + hi)
        pm = bs_price(S, K, r, T, mid, typ)
        if abs(pm - price) < 1e-4:
            return mid
        if pm < price:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


def greeks(S, K, r, T, sigma, typ="CE"):
    """Return delta, theta (per day), gamma, vega (per 1 vol pt) at sigma."""
    d1, d2 = _d1d2(S, K, r, T, sigma)
    if d1 is None:
        delta = (1.0 if S > K else 0.0) if typ == "CE" else (-1.0 if S < K else 0.0)
        return {"delta": delta, "theta": 0.0, "gamma": 0.0, "vega": 0.0}
    pdf = _nd(d1)
    gamma = pdf / (S * sigma * math.sqrt(T))
    vega = S * pdf * math.sqrt(T) / 100.0            # per 1% vol
    if typ == "CE":
        delta = _Nd(d1)
        theta = (-S * pdf * sigma / (2 * math.sqrt(T))
                 - r * K * math.exp(-r * T) * _Nd(d2)) / 365.0
    else:
        delta = _Nd(d1) - 1.0
        theta = (-S * pdf * sigma / (2 * math.sqrt(T))
                 + r * K * math.exp(-r * T) * _Nd(-d2)) / 365.0
    return {"delta": delta, "theta": theta, "gamma": gamma, "vega": vega}


def time_to_expiry(dte, hours_left=None):
    """Years to expiry. Floors intraday so greeks don't blow up on expiry day.
    dte = calendar days to expiry; hours_left = trading hours left today (opt)."""
    if dte is None:
        dte = 1
    frac_today = (hours_left / 6.25) if hours_left is not None else 0.3
    T = (max(dte, 0) + max(0.0, min(1.0, frac_today))) / 365.0
    return max(T, 0.5 / 365.0)
