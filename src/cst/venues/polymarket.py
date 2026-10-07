"""Polymarket Gamma market data.

Prices on the Gamma payload are the yes-token book. The no token is the
complement: no bid = 1 − yes ask, no ask = 1 − yes bid. Fees come from
``feeSchedule`` on the market.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from cst.models import Quote
from cst.venues.http import MarketHttp

log = logging.getLogger("cst.polymarket")

_DEFAULT_RATES = {
    "crypto": 0.07,
    "sports": 0.05,
    "finance": 0.04,
    "politics": 0.04,
    "economics": 0.05,
    "culture": 0.05,
    "weather": 0.05,
    "other": 0.05,
    "mentions": 0.04,
    "tech": 0.04,
    "geopolitics": 0.0,
}


def _loads(value, fallback):
    if value is None:
        return fallback
    if isinstance(value, (list, dict)):
        return value
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return fallback


def _dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _num(value) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _book_ok(bid: float, ask: float, keep_extremes: bool) -> bool:
    """Admission rejects a book pinned at 0 or 1. A mark of an open clip must keep it.

    0/0 and 0/1 are an empty print, not a price. A 0.99/1.00 favorite, or a
    collapsed 0.00/0.02 book, is a real touch.
    """
    if bid < 0 or ask < 0 or ask > 1 or ask < bid:
        return False
    if bid == 0 and ask == 0:
        return False
    if bid == 0 and ask >= 1:
        return False
    if keep_extremes:
        return True
    return bid > 0 and 0 < ask < 1


def quotes_from_polymarket_market(market: dict, event: dict | None = None, keep_extremes: bool = False) -> list[Quote]:
    event = event or {}
    if market.get("archived"):
        return []
    outcomes = _loads(market.get("outcomes"), [])
    prices = _loads(market.get("outcomePrices"), [])
    if not isinstance(outcomes, list) or len(outcomes) < 2:
        return []
    yes_bid = _num(market.get("bestBid"))
    yes_ask = _num(market.get("bestAsk"))
    end = _dt(market.get("endDate") or event.get("endDate"))
    fee_type = str(market.get("feeType") or "").replace("_fees", "") or "other"
    schedule = market.get("feeSchedule") or {}
    if not isinstance(schedule, dict):
        schedule = {}
    rate = _num(schedule.get("rate"))
    if rate is None:
        rate = _DEFAULT_RATES.get(fee_type, 0.05)
    exponent = _num(schedule.get("exponent")) or 1.0
    tags = event.get("tags") or []
    category = fee_type.title() if fee_type else "Other"
    if isinstance(tags, list) and tags and isinstance(tags[0], dict) and tags[0].get("label"):
        category = str(tags[0]["label"])
    liquidity = _num(market.get("liquidityNum"))
    if liquidity is None:
        liquidity = _num(market.get("liquidity"))
    if liquidity is None:
        liquidity = _num(event.get("liquidity")) or 0.0
    volume = _num(market.get("volumeNum"))
    if volume is None:
        volume = _num(market.get("volume")) or 0.0
    min_shares = _num(market.get("orderMinSize")) or 5.0
    tokens = _loads(market.get("clobTokenIds"), [])
    yes_token = str(tokens[0]) if isinstance(tokens, list) and tokens else ""
    no_token = str(tokens[1]) if isinstance(tokens, list) and len(tokens) > 1 else ""
    rules = str(event.get("description") or market.get("description") or "")[:700]
    slug = event.get("slug") or market.get("slug") or ""
    url = f"https://polymarket.com/event/{slug}" if slug else "https://polymarket.com"
    event_id = str(event.get("id") or market.get("id") or "")
    event_title = str(event.get("title") or market.get("question") or "")
    title = str(market.get("question") or event_title)
    market_id = str(market.get("id") or market.get("conditionId") or "")
    closed = bool(market.get("closed"))
    # A live price near 0 or 1 is not a resolution. UMA has to say resolved.
    winner = _winner(prices) if _uma_resolved(market) else None
    books = _books(yes_bid, yes_ask, str(outcomes[0]), str(outcomes[1]), winner)
    quotes = []
    # A closed market is still parsed so an open paper position can settle.
    live = not closed and market.get("active") is not False and market.get("acceptingOrders") is not False
    for side, outcome, bid, ask in books:
        if bid is None or ask is None:
            continue
        if not live and not (closed and winner):
            continue
        if live and not _book_ok(bid, ask, keep_extremes):
            continue
        quotes.append(Quote(
            venue="polymarket",
            market_id=market_id,
            event_id=event_id,
            event_title=event_title,
            title=title,
            outcome=outcome,
            side=side,
            bid=bid,
            ask=ask,
            ask_size=-1,
            volume=volume,
            liquidity=liquidity or 0.0,
            end_time=end,
            category=category,
            fee_model="polymarket",
            fee_rate=rate,
            fee_exponent=exponent,
            rules=rules,
            url=url,
            min_shares=min_shares,
            token_id=yes_token if side == "yes" else no_token,
            settled=bool(winner),
            winner=winner,
        ))
    return quotes


def _uma_resolved(market: dict) -> bool:
    if not market.get("closed"):
        return False
    return str(market.get("umaResolutionStatus") or "").lower() == "resolved"


def _winner(prices) -> str | None:
    if not isinstance(prices, list) or len(prices) < 2:
        return None
    try:
        yes = float(prices[0])
        no = float(prices[1])
    except (TypeError, ValueError):
        return None
    if yes >= 0.99 and no <= 0.01:
        return "yes"
    if no >= 0.99 and yes <= 0.01:
        return "no"
    return None


def _books(yes_bid, yes_ask, yes_name, no_name, winner: str | None):
    # The second outcome is the complement of the first token.
    if winner and (yes_bid is None or yes_ask is None):
        if winner == "yes":
            return [("yes", yes_name, 1.0, 1.0), ("no", no_name, 0.0, 0.0)]
        return [("yes", yes_name, 0.0, 0.0), ("no", no_name, 1.0, 1.0)]
    if yes_bid is None or yes_ask is None:
        return []
    return [
        ("yes", yes_name, yes_bid, yes_ask),
        ("no", no_name, 1 - yes_ask, 1 - yes_bid),
    ]


def fetch_polymarket(base_url: str, pages: int, page_size: int, http: MarketHttp | None = None) -> tuple[list[Quote], str | None]:
    own = http is None
    client = http or MarketHttp()
    quotes: list[Quote] = []
    try:
        for page in range(pages):
            payload = client.get_json(
                f"{base_url.rstrip('/')}/events",
                params={
                    "closed": "false",
                    "active": "true",
                    "limit": str(page_size),
                    "offset": str(page * page_size),
                    "order": "volume24hr",
                    "ascending": "false",
                },
            )
            events = payload if isinstance(payload, list) else []
            if not events:
                break
            for event in events:
                for market in event.get("markets") or []:
                    quotes.extend(quotes_from_polymarket_market(market, event))
        return quotes, None
    except Exception as exc:
        log.warning("polymarket scan failed: %s", exc)
        return quotes, f"Polymarket: {exc}"
    finally:
        if own:
            client.close()


def fetch_polymarket_market(base_url: str, market_id: str, http: MarketHttp | None = None) -> list[Quote]:
    own = http is None
    client = http or MarketHttp()
    try:
        payload = client.get_json(f"{base_url.rstrip('/')}/markets/{market_id}")
        market = payload[0] if isinstance(payload, list) else payload
        if not isinstance(market, dict):
            return []
        events = market.get("events") or []
        event = events[0] if events else {}
        return quotes_from_polymarket_market(market, event, keep_extremes=True)
    except Exception as exc:
        log.warning("polymarket market %s failed: %s", market_id, exc)
        return []
    finally:
        if own:
            client.close()
