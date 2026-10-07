"""Kalshi public market data.

The trade API returns yes and no bids and asks in dollars. Multivariate
combos are skipped. Event titles are filled in for favorites so the tape
can name the event instead of the ticker.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from cst.models import Quote
from cst.strategy import price_bucket
from cst.venues.http import MarketHttp

log = logging.getLogger("cst.kalshi")

# Trade lookups per scan. The market list is cheap; each early trade is its own read.
TRADE_LOOKUPS = 40

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


def _num(value) -> float | None:
    """None for a missing or unreadable field. A missing bid is not a quoted zero."""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


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
    """Scans stay inside (0, 1). A mark of an open clip may pin at 0 or 1."""
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
    volume = _num(market.get("volume_fp")) or 0.0
    liquidity = _num(market.get("open_interest_fp")) or 0.0
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
        if size is None:
            size = -1.0
        raw_bid_size = market.get(bid_size_key)
        bid_size = _num(raw_bid_size) if raw_bid_size not in (None, "") else -1.0
        if bid_size is None:
            bid_size = -1.0
        if settled and winner and (bid is None or ask is None):
            # A settled payload can omit the book. The winner is the mark.
            bid = 1.0 if winner == side else 0.0
            ask = bid
        elif bid is None or ask is None:
            continue
        elif not settled and not _book_ok(bid, ask, keep_extremes):
            continue
        elif settled and bid <= 0 and ask <= 0:
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
        bid = max(_num(market.get("yes_bid_dollars")) or 0.0, _num(market.get("no_bid_dollars")) or 0.0)
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


def _close_time(market: dict) -> datetime | None:
    return _dt(market.get("close_time") or market.get("expected_expiration_time"))


def yes_price_before_close(market: dict, trades: list, hours: float) -> float | None:
    """Latest yes trade at least ``hours`` before close.

    A trade after that cutoff is the price of a market about to settle, which
    is not the quote the desk is allowed to buy.
    """
    close = _close_time(market)
    if close is None or hours <= 0:
        return None
    cutoff = close - timedelta(hours=hours)
    best: float | None = None
    best_at: datetime | None = None
    for trade in trades:
        if not isinstance(trade, dict):
            continue
        traded_at = _dt(trade.get("created_time"))
        price = _num(trade.get("yes_price_dollars"))
        if traded_at is None or price is None or traded_at > cutoff:
            continue
        if best_at is None or traded_at > best_at:
            best_at = traded_at
            best = price
    return best


def settled_favorite(market: dict, yes_price: float | None) -> tuple[float, bool] | None:
    """Favorite quoted at ``yes_price``, and whether that side won.

    ``yes_price`` is a trade from when time was still left, not the final print.
    A price of 0 or 1 is not a quote. A cheap yes price means the no side was
    the favorite.
    """
    if not isinstance(market, dict) or yes_price is None:
        return None
    if market.get("mve_collection_ticker") or market.get("mve_selected_legs"):
        return None
    result = str(market.get("result") or "").lower()
    if result not in {"yes", "no"}:
        return None
    volume = _num(market.get("volume_fp"))
    if volume is None or volume < 10:
        return None
    yes_price = round(yes_price, 4)
    if 0.90 <= yes_price < 1:
        return yes_price, result == "yes"
    if 0 < yes_price <= 0.10:
        price = round(1 - yes_price, 4)
        if 0.90 <= price < 1:
            return price, result == "no"
    return None


def _sample_row(price: float | None, won: bool, hours: float) -> dict:
    if price is None:
        return {"price": None, "hours": hours}
    return {"price": price, "won": won, "hours": hours, "bucket": price_bucket(price)}


def _needs_trade(market: dict, hours: float) -> bool:
    """True when a pre-close trade could exist. Short markets are not a sample."""
    if market.get("mve_collection_ticker") or market.get("mve_selected_legs"):
        return False
    result = str(market.get("result") or "").lower()
    if result not in {"yes", "no"}:
        return False
    volume = _num(market.get("volume_fp"))
    if volume is None or volume < 10:
        return False
    close = _close_time(market)
    if close is None:
        return False
    opened = _dt(market.get("open_time"))
    if opened is not None and opened > close - timedelta(hours=hours):
        return False
    return True


def fetch_settled_record(
    base_url: str,
    pages: int,
    page_size: int,
    hours: float,
    skip: set[str] | None = None,
    http: MarketHttp | None = None,
) -> tuple[dict[str, dict] | None, str | None]:
    """New per-ticker samples from settled markets.

    The price is the latest trade at least ``hours`` before close. Markets
    with no such trade are stored with a null price so the next scan does not
    read them again. ``None`` for the whole payload means the market list
    failed and the caller should keep the previous sample. Trade lookups are
    capped; the rest wait for a later scan.
    """
    own = http is None
    client = http or MarketHttp()
    root = base_url.rstrip("/")
    seen = skip or set()
    markets: list[dict] = []
    try:
        cursor = ""
        for _ in range(max(pages, 0)):
            params = {"status": "settled", "limit": str(page_size), "mve_filter": "exclude"}
            if cursor:
                params["cursor"] = cursor
            payload = client.get_json(f"{root}/markets", params=params)
            if not isinstance(payload, dict):
                break
            batch = payload.get("markets") or []
            markets.extend(item for item in batch if isinstance(item, dict))
            cursor = payload.get("cursor") or ""
            if not cursor or not batch:
                break
    except Exception as exc:
        log.warning("kalshi settled record failed: %s", exc)
        if own:
            client.close()
        return None, f"Kalshi settled record: {exc}"
    samples: dict[str, dict] = {}
    lookups = 0
    try:
        for market in markets:
            ticker = str(market.get("ticker") or "")
            if not ticker or ticker in seen or ticker in samples:
                continue
            if not _needs_trade(market, hours):
                samples[ticker] = _sample_row(None, False, hours)
                continue
            if lookups >= TRADE_LOOKUPS:
                break
            close = _close_time(market)
            if close is None:
                continue
            cutoff = int((close - timedelta(hours=hours)).timestamp())
            try:
                payload = client.get_json(
                    f"{root}/markets/trades",
                    params={"ticker": ticker, "limit": "1", "max_ts": str(cutoff)},
                )
            except Exception as exc:
                log.warning("kalshi trades %s failed: %s", ticker, exc)
                continue
            lookups += 1
            trades = payload.get("trades") if isinstance(payload, dict) else None
            yes_price = yes_price_before_close(market, trades or [], hours)
            observed = settled_favorite(market, yes_price)
            if observed is None:
                samples[ticker] = _sample_row(None, False, hours)
            else:
                price, won = observed
                samples[ticker] = _sample_row(price, won, hours)
        return samples, None
    finally:
        if own:
            client.close()


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
