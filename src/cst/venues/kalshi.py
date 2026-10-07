"""Kalshi public market data.

The trade API returns yes and no bids and asks in dollars. Multivariate
combos are skipped. Event titles are filled in for favorites so the tape
can name the event instead of the ticker.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from cst.models import Quote
from cst.venues.http import MarketHttp

log = logging.getLogger("cst.kalshi")

_CATEGORIES = (
    ("KXNBA", "Basketball"),
    ("KXNCAAB", "Basketball"),
    ("KXNCAAF", "Football"),
    ("KXNFL", "Football"),
    ("KXNHL", "Hockey"),
    ("KXMLB", "Baseball"),
    ("KXNBA", "Basketball"),
    ("KXHIGH", "Weather"),
    ("KXTEMP", "Weather"),
    ("KXBTC", "Crypto"),
    ("KXETH", "Crypto"),
    ("KXBTCD", "Crypto"),
    ("KXFED", "Economics"),
    ("KXPRES", "Politics"),
    ("KXELECTION", "Politics"),
)


def _num(value) -> float:
    if value is None or value == "":
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


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


def _category(ticker: str, fallback: str = "Other") -> str:
    upper = ticker.upper()
    for prefix, name in _CATEGORIES:
        if upper.startswith(prefix):
            return name
    return fallback


def _book_ok(bid: float, ask: float, keep_extremes: bool) -> bool:
    """Same rule as the Polymarket parser: scans stay inside (0, 1), marks may pin."""
    if bid < 0 or ask < 0 or ask > 1 or ask < bid:
        return False
    if bid == 0 and ask == 0:
        return False
    if bid == 0 and ask >= 1:
        return False
    if keep_extremes:
        return True
    return bid > 0 and 0 < ask < 1


def quotes_from_kalshi_market(market: dict, event: dict | None = None, keep_extremes: bool = False) -> list[Quote]:
    if market.get("mve_collection_ticker") or market.get("mve_selected_legs"):
        return []
    event = event or {}
    status = str(market.get("status") or "").lower()
    result = str(market.get("result") or "").lower()
    winner = result if result in {"yes", "no"} else None
    settled = winner is not None or status in {"settled", "finalized", "determined"}
    if status not in {"active", "open", "paused", ""} and not settled:
        return []
    title = str(market.get("title") or "")
    event_title = str(event.get("title") or "")
    category = str(event.get("category") or "") or _category(str(market.get("ticker") or ""))
    end = _dt(market.get("close_time") or market.get("expected_expiration_time"))
    rules = str(market.get("rules_primary") or "")[:700]
    ticker = str(market.get("ticker") or "")
    event_id = str(market.get("event_ticker") or event.get("event_ticker") or ticker)
    volume = _num(market.get("volume_fp"))
    liquidity = _num(market.get("open_interest_fp"))
    url = f"https://kalshi.com/markets/{event_id.lower()}"
    sides = (
        ("yes", str(market.get("yes_sub_title") or "Yes"), "yes_bid_dollars", "yes_ask_dollars", "yes_ask_size_fp", "yes_bid_size_fp"),
        ("no", str(market.get("no_sub_title") or "No"), "no_bid_dollars", "no_ask_dollars", "no_ask_size_fp", "no_bid_size_fp"),
    )
    quotes = []
    for side, outcome, bid_key, ask_key, size_key, bid_size_key in sides:
        bid = _num(market.get(bid_key))
        ask = _num(market.get(ask_key))
        raw_size = market.get(size_key)
        size = _num(raw_size) if raw_size not in (None, "") else -1.0
        raw_bid_size = market.get(bid_size_key)
        bid_size = _num(raw_bid_size) if raw_bid_size not in (None, "") else -1.0
        if not settled and not _book_ok(bid, ask, keep_extremes):
            continue
        if settled and bid <= 0 and ask <= 0:
            bid, ask = (1.0, 1.0) if winner == side else (0.0, 0.0)
        quotes.append(Quote(
            venue="kalshi",
            market_id=ticker,
            event_id=event_id,
            event_title=event_title,
            title=title,
            outcome=outcome,
            side=side,
            bid=bid if not (settled and winner) else (1.0 if winner == side else 0.0),
            ask=ask if not (settled and winner) else (1.0 if winner == side else 0.0),
            ask_size=size,
            bid_size=bid_size,
            volume=volume,
            liquidity=liquidity,
            end_time=end,
            category=category,
            fee_model="kalshi",
            fee_rate=0.07,
            fee_exponent=1,
            rules=rules,
            url=url,
            min_shares=1,
            settled=bool(settled and winner),
            winner=winner,
        ))
    return quotes


def fetch_kalshi(base_url: str, pages: int, page_size: int, http: MarketHttp | None = None) -> tuple[list[Quote], str | None]:
    own = http is None
    client = http or MarketHttp()
    quotes: list[Quote] = []
    root = base_url.rstrip("/")
    try:
        cursor = ""
        markets: list[dict] = []
        for _ in range(pages):
            params = {"status": "open", "limit": str(page_size), "mve_filter": "exclude"}
            if cursor:
                params["cursor"] = cursor
            payload = client.get_json(f"{root}/markets", params=params)
            if not isinstance(payload, dict):
                break
            batch = payload.get("markets") or []
            markets.extend(batch)
            cursor = payload.get("cursor") or ""
            if not cursor or not batch:
                break
        events = _event_titles(client, root, markets)
        for market in markets:
            event = events.get(str(market.get("event_ticker") or ""), {})
            quotes.extend(quotes_from_kalshi_market(market, event))
        return quotes, None
    except Exception as exc:
        log.warning("kalshi scan failed: %s", exc)
        return quotes, f"Kalshi: {exc}"
    finally:
        if own:
            client.close()


def _event_titles(client: MarketHttp, root: str, markets: list[dict]) -> dict[str, dict]:
    """Fetch titles only for events that already show a high bid, capped so a scan stays short."""
    wanted: list[str] = []
    seen = set()
    for market in markets:
        bid = max(_num(market.get("yes_bid_dollars")), _num(market.get("no_bid_dollars")))
        event_id = str(market.get("event_ticker") or "")
        if bid < 0.85 or not event_id or event_id in seen:
            continue
        seen.add(event_id)
        wanted.append(event_id)
        if len(wanted) >= 40:
            break
    found: dict[str, dict] = {}
    for event_id in wanted:
        try:
            payload = client.get_json(f"{root}/events/{event_id}")
        except Exception:
            continue
        event = payload.get("event") if isinstance(payload, dict) else None
        if isinstance(event, dict):
            found[event_id] = event
    return found


def fetch_kalshi_ticker(base_url: str, ticker: str, http: MarketHttp | None = None) -> list[Quote]:
    own = http is None
    client = http or MarketHttp()
    try:
        payload = client.get_json(f"{base_url.rstrip('/')}/markets/{ticker}")
        market = payload.get("market") if isinstance(payload, dict) else None
        if not isinstance(market, dict):
            return []
        return quotes_from_kalshi_market(market, keep_extremes=True)
    except Exception as exc:
        log.warning("kalshi market %s failed: %s", ticker, exc)
        return []
    finally:
        if own:
            client.close()
