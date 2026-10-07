"""Fresh top-of-book size for a paper fill.

A buy needs displayed asks at or better than the limit. A sell needs displayed
bids at or better than the limit. If the book cannot be read, the size is
short, or the touch ran away, the clip stays unfilled. Nothing here posts an order.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from cst.models import Quote
from cst.venues.http import MarketHttp

log = logging.getLogger("cst.depth")


@dataclass(slots=True)
class DepthResult:
    ok: bool
    size: float
    detail: str


def _num(value) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _fail(detail: str) -> DepthResult:
    return DepthResult(False, 0.0, detail)


def _cover(available: float, shares: float, action: str) -> DepthResult:
    if available + 1e-9 < shares:
        noun = "offer" if action == "buy" else "bid"
        return _fail(f"The size on the {noun} is {available:g}, short of {shares:g}.")
    return DepthResult(True, available, "The displayed size covers the clip.")


def _bid_pairs(raw, dollars: bool) -> list[tuple[float, float]]:
    found: list[tuple[float, float]] = []
    if not isinstance(raw, list):
        return found
    for level in raw:
        if not isinstance(level, (list, tuple)) or len(level) < 2:
            continue
        price = _num(level[0])
        size = _num(level[1])
        if price is None or size is None:
            continue
        if not dollars:
            price = price / 100.0
        found.append((price, size))
    return found


def judge_kalshi_orderbook(payload: dict | list | None, side: str, action: str, limit: float, shares: float) -> DepthResult:
    """Kalshi publishes bids. A yes ask is one minus a no bid, and the size sits on that bid."""
    if not isinstance(payload, dict):
        return _fail("The book could not be read, so the clip was not filled.")
    book = payload.get("orderbook_fp") or payload.get("orderbook") or payload
    if not isinstance(book, dict):
        return _fail("The book could not be read, so the clip was not filled.")
    if "yes_dollars" in book or "no_dollars" in book:
        yes = _bid_pairs(book.get("yes_dollars"), dollars=True)
        no = _bid_pairs(book.get("no_dollars"), dollars=True)
    elif "yes" in book or "no" in book:
        yes = _bid_pairs(book.get("yes"), dollars=False)
        no = _bid_pairs(book.get("no"), dollars=False)
    else:
        return _fail("The book could not be read, so the clip was not filled.")
    same = yes if side == "yes" else no
    other = no if side == "yes" else yes
    if action == "sell":
        if not same:
            return _fail("The book had no size at the price we needed.")
        best = max(price for price, _size in same)
        if best + 1e-6 < limit:
            return _fail("The bid moved away from the price we were willing to sell.")
        available = sum(size for price, size in same if price + 1e-9 >= limit)
        return _cover(available, shares, action)
    # Buying this side lifts the other side's bids. Those bids are asks at 1 - price.
    if not other:
        return _fail("The book had no size at the price we needed.")
    asks = [(1.0 - price, size) for price, size in other]
    best = min(price for price, _size in asks)
    if best > limit + 1e-6:
        return _fail("The ask moved away from the price we were willing to pay.")
    available = sum(size for price, size in asks if price <= limit + 1e-9)
    return _cover(available, shares, action)


def live_depth(settings, quote: Quote, shares: float, action: str) -> DepthResult:
    limit = quote.ask if action == "buy" else quote.bid
    client = MarketHttp()
    try:
        if quote.venue == "kalshi":
            payload = client.get_json(
                f"{settings.kalshi_base_url.rstrip('/')}/markets/{quote.market_id}/orderbook",
            )
            return judge_kalshi_orderbook(payload, quote.side, action, limit, shares)
        return _fail("The book could not be read, so the clip was not filled.")
    except Exception as exc:
        log.warning("depth read failed for %s: %s", quote.key, exc)
        return _fail("The book could not be read, so the clip was not filled.")
    finally:
        client.close()
