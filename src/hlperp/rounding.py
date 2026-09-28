"""Hyperliquid tick and lot size rounding.

Rules (per Hyperliquid docs):
  * Sizes carry at most ``szDecimals`` decimals for the asset.
  * Prices carry at most 5 significant figures, and at most
    ``MAX_DECIMALS - szDecimals`` decimals, where ``MAX_DECIMALS`` is 6 for
    perps. Integer prices are always allowed regardless of significant figures.

The exchange rejects non-conforming values, so we normalise client side rather
than discovering the problem as a rejection.
"""

from __future__ import annotations

from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP

MAX_DECIMALS_PERP = 6


def floor_sz(size: float | str, sz_decimals: int) -> float:
    """Truncate a size down to the asset's lot size.

    Truncation, never rounding up: a size rounded up can exceed the balance or
    the intended notional and the exchange rejects the whole order. Hyperliquid's
    own SDK example floors for the same reason.
    """
    d = Decimal(str(size))
    quantum = Decimal(1).scaleb(-sz_decimals)
    return float(d.quantize(quantum, rounding=ROUND_DOWN))


# Sizes are lot-truncated. Kept under the ``round_sz`` name so call sites read as
# "snap to the exchange's lot size"; the semantics are floor, not round-half-up.
round_sz = floor_sz


def round_px(price: float | str, sz_decimals: int) -> float:
    """Round a price to 5 significant figures and the perp decimal ceiling."""
    d = Decimal(str(price))
    if d == d.to_integral_value():
        return float(d.to_integral_value())

    sig_quantum = Decimal(1).scaleb(d.adjusted() - 4)
    d = d.quantize(sig_quantum, rounding=ROUND_HALF_UP)

    max_decimals = MAX_DECIMALS_PERP - sz_decimals
    if max_decimals >= 0:
        d = d.quantize(Decimal(1).scaleb(-max_decimals), rounding=ROUND_HALF_UP)
    return float(d)


def wire(value: float | str) -> str:
    """Format a number for the API: no trailing zeros, no exponent."""
    d = Decimal(str(value)).normalize()
    s = format(d, "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s or "0"
