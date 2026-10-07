"""Kalshi fee model.

The standard taker fee is ``0.07 × C × P × (1 − P)``. Paper fills round
that up to the next cent as a conservative approximation. Series metadata
supplies the taker multiplier.
"""

from __future__ import annotations

from decimal import Decimal, ROUND_UP

CENT = Decimal("0.01")


def D(value: float | Decimal | str) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def kalshi_taker_fee(contracts: float | Decimal, price: float | Decimal, multiplier: float | Decimal = 1) -> Decimal:
    """Standard Kalshi taker fee, rounded up to the next cent."""
    count = D(contracts)
    px = D(price)
    mult = D(multiplier)
    if count <= 0 or px <= 0 or px >= 1:
        return Decimal("0")
    raw = D("0.07") * mult * count * px * (Decimal("1") - px)
    return raw.quantize(CENT, rounding=ROUND_UP)


def fee_for(model: str, shares: float, price: float, rate: float = 0.07, exponent: float = 1) -> Decimal:
    del exponent
    if model == "kalshi":
        return kalshi_taker_fee(shares, price, D(rate) / D("0.07"))
    raise ValueError(f"unknown fee model {model}")
