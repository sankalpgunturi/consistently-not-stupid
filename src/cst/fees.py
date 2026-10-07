"""Venue fee models.

Polymarket's published taker fee is ``C × rate × p × (1 − p)``, with the
rate taken from the market. When the market publishes an exponent, the
variance term is raised to that power. Exponent 1 is the published table.

Kalshi's standard taker fee is ``0.07 × C × P × (1 − P)``. Paper fills
round that up to the next cent so the desk never understates the cut.
Some series use a different multiplier; those are not in the public market
payload, so the standard schedule is the assumption.
"""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP, ROUND_UP

CENT = Decimal("0.01")
POLY_TICK = Decimal("0.00001")


def D(value: float | Decimal | str) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def polymarket_taker_fee(shares: float | Decimal, price: float | Decimal, rate: float | Decimal, exponent: float | Decimal = 1) -> Decimal:
    """Taker fee in USDC, rounded to 5 decimal places. Dust below 0.00001 is zero."""
    shares_d = D(shares)
    price_d = D(price)
    rate_d = D(rate)
    exp = D(exponent)
    if shares_d <= 0 or rate_d <= 0 or price_d <= 0 or price_d >= 1:
        return Decimal("0")
    variance = price_d * (Decimal("1") - price_d)
    raw = shares_d * rate_d * (variance ** exp)
    rounded = raw.quantize(POLY_TICK, rounding=ROUND_HALF_UP)
    if rounded < POLY_TICK:
        return Decimal("0")
    return rounded


def kalshi_taker_fee(contracts: float | Decimal, price: float | Decimal, multiplier: float | Decimal = 1) -> Decimal:
    """Standard Kalshi taker fee, rounded up to the next cent."""
    count = D(contracts)
    px = D(price)
    mult = D(multiplier)
    if count <= 0 or px <= 0 or px >= 1:
        return Decimal("0")
    raw = D("0.07") * mult * count * px * (Decimal("1") - px)
    return raw.quantize(CENT, rounding=ROUND_UP)


def fee_for(model: str, shares: float, price: float, rate: float = 0, exponent: float = 1) -> Decimal:
    if model == "kalshi":
        return kalshi_taker_fee(shares, price)
    if model == "polymarket":
        return polymarket_taker_fee(shares, price, rate, exponent)
    raise ValueError(f"unknown fee model {model}")
