"""Kalshi public market data.

The trade API returns yes and no bids and asks in dollars. Multivariate
combos are skipped. Event titles are filled in for favorites so the tape
can name the event instead of the ticker.
"""

from __future__ import annotations

import logging
import math
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit, urlunsplit

from cst.models import Quote
from cst.fees import D
from cst.strategy import price_bucket
from cst.venues.http import MarketHttp

log = logging.getLogger("cst.kalshi")


def normalize_market_url(url: str) -> str:
    """Repair our old event-only website links without rewriting archive rows."""
    parts = urlsplit(url)
    path = parts.path.strip('/').split('/')
    if parts.hostname == 'kalshi.com' and len(path) == 2 and path[0] == 'markets' and '-' in path[1]:
        event = path[1].lower()
        parts = parts._replace(path=f"/markets/{event.split('-')[0]}/{event}")
    return urlunsplit(parts)

# Trade lookups per scan. Each attempt counts, including a timeout.
TRADE_LOOKUPS = 40
# One page. Pagination continues until the trades reach the horizon cutoff.
TRADE_PAGE = 100
# Shallowest nearest-expiry the desk can ask for. Reads start here.
RAIL_HOURS = 1.0

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


def quotes_from_kalshi_market(market: dict, event: dict | None = None, keep_extremes: bool = False, series: dict | None = None) -> list[Quote]:
    if market.get("mve_collection_ticker") or market.get("mve_selected_legs"):
        return []
    event = event or {}
    fee_rate = 0.07
    fee_verified = series is None  # Pure-parser fixtures retain the standard schedule.
    if series is not None:
        multiplier = _num(series.get("fee_multiplier"))
        if series.get("fee_type") in {"quadratic", "quadratic_with_maker_fees"} and multiplier is not None and math.isfinite(multiplier) and multiplier >= 0:
            fee_rate = float(D("0.07") * D(multiplier))
            fee_verified = True
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
    url = f"https://kalshi.com/markets/{event_id.split('-')[0].lower()}/{event_id.lower()}"
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
            expected_resolution_time=_dt(market.get("expected_expiration_time")),
            category=category,
            fee_model="kalshi",
            fee_rate=fee_rate,
            fee_exponent=1,
            fee_verified=fee_verified,
            rules=rules,
            url=url,
            min_shares=1,
            tradable=status in {"active", "open"} and not settled,
            settled=bool(settled and winner),
            winner=winner,
        ))
    return quotes


def fetch_kalshi(base_url: str, pages: int, page_size: int, http: MarketHttp | None = None, close_window: tuple[int, int] | None = None, close_windows: list[tuple[int, int] | None] | None = None, priority_page_size: int | None = None, prioritize_nearest: bool = False, metadata_cache: dict | None = None) -> tuple[list[Quote], str | None]:
    own = http is None
    client = http or MarketHttp()
    quotes: list[Quote] = []
    root = base_url.rstrip("/")
    try:
        windows = close_windows or [close_window]
        # Near-resolution scans reserve one request per broader window, then
        # spend the rest nearest-first. Legacy scans retain weighted allocation.
        pattern = list(range(len(windows))) + [0]
        budgets = [0] * len(windows)
        for index in range(max(0, pages)):
            target = (index if index < len(windows) else 0) if prioritize_nearest else pattern[index % len(pattern)]
            budgets[target] += 1
        unique: dict[str, dict] = {}
        for window_index, (window, budget) in enumerate(zip(windows, budgets)):
            cursor = ""
            used = 0
            for _ in range(budget):
                limit = min(1000, max(page_size, priority_page_size or page_size)) if window_index == 0 else page_size
                params = {"status": "open", "limit": str(limit), "mve_filter": "exclude"}
                if window is not None:
                    # Close-time filters cannot be combined with status=open.
                    params.pop("status")
                    params.update(min_close_ts=str(window[0]), max_close_ts=str(window[1]))
                if cursor:
                    params["cursor"] = cursor
                used += 1
                payload = client.get_json(f"{root}/markets", params=params)
                if not isinstance(payload, dict):
                    break
                batch = payload.get("markets") or []
                for item in batch:
                    if isinstance(item, dict) and item.get("ticker") and item.get("status") in {"active", "open"}:
                        unique[item["ticker"]] = item
                cursor = payload.get("cursor") or ""
                if not cursor or not batch:
                    break
            if prioritize_nearest:
                later = len(windows) - window_index - 1
                if later:
                    for spare in range(budget - used):
                        budgets[window_index + 1 + spare % later] += 1
                if window_index == 0 and budget and used == budget and cursor:
                    log.warning("Near-term discovery reached its %s-page limit with more markets available.", budget)
        markets = list(unique.values())
        events = _event_titles(client, root, markets, metadata_cache)
        series, fee_errors = _series_fees(client, root, markets, metadata_cache)
        for market in markets:
            event = events.get(str(market.get("event_ticker") or ""), {})
            ticker = str(market.get("ticker") or "").split("-")[0]
            quotes.extend(quotes_from_kalshi_market(market, event, series=series.get(ticker, {})))
        error = f"Kalshi fee metadata unavailable for {fee_errors} series; affected candidates skipped." if fee_errors else None
        return quotes, error
    except Exception as exc:
        log.warning("kalshi scan failed: %s", exc)
        return quotes, f"Kalshi: {exc}"
    finally:
        if own:
            client.close()


def _metadata(client: MarketHttp, url: str, cache: dict | None):
    """Reuse discovery metadata for 60 seconds; never cache prices or failures."""
    now = time.monotonic()
    if cache is not None:
        for key in list(cache):
            if now - cache[key][0] >= 60:
                del cache[key]
        if url in cache:
            return cache[url][1]
    payload = client.get_json(url)
    if cache is not None and isinstance(payload, dict) and (payload.get("series") or payload.get("event")):
        cache[url] = (time.monotonic(), payload)
    return payload


def _series_fees(client: MarketHttp, root: str, markets: list[dict], cache: dict | None = None) -> tuple[dict[str, dict], int]:
    """One metadata read per series with a favorite; never guess an unknown fee."""
    wanted = sorted({str(m.get("ticker") or "").split("-")[0] for m in markets
                     if max(_num(m.get("yes_bid_dollars")) or 0, _num(m.get("no_bid_dollars")) or 0) >= 0.80})
    found = {}
    errors = 0
    for ticker in wanted:
        try:
            payload = _metadata(client, f"{root}/series/{ticker}", cache)
            row = payload.get("series") if isinstance(payload, dict) else None
            mult = _num(row.get("fee_multiplier")) if isinstance(row, dict) else None
            if not isinstance(row, dict) or row.get("fee_type") not in {"quadratic", "quadratic_with_maker_fees"} or mult is None or not math.isfinite(mult) or mult < 0:
                errors += 1
                continue
            found[ticker] = row
        except Exception:
            errors += 1
    return found, errors


def _event_titles(client: MarketHttp, root: str, markets: list[dict], cache: dict | None = None) -> dict[str, dict]:
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
            payload = _metadata(client, f"{root}/events/{event_id}", cache)
        except Exception:
            continue
        event = payload.get("event") if isinstance(payload, dict) else None
        if isinstance(event, dict):
            found[event_id] = event
    return found


def _close_time(market: dict) -> datetime | None:
    return _dt(market.get("close_time") or market.get("expected_expiration_time"))


def _yes_price(trade: dict) -> float | None:
    """Yes price in dollars. The cents field is the fallback when dollars are absent."""
    dollars = _num(trade.get("yes_price_dollars"))
    if dollars is not None:
        return dollars
    cents = _num(trade.get("yes_price"))
    if cents is None:
        return None
    return cents / 100


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
        price = _yes_price(trade)
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
        return {"price": None, "won": False, "hours": hours, "bucket": ""}
    return {"price": price, "won": won, "hours": hours, "bucket": price_bucket(price)}


def _covers_cutoff(shallow_ts: float, covered_until: float, exhausted: bool, cutoff: float) -> bool:
    """True when stored trades can name the latest print at or before ``cutoff``.

    A walk from ``shallow_ts`` back to ``covered_until`` answers every cutoff
    in between. An exhausted walk answers every earlier cutoff too. A trade
    that only clears two hours does not answer a three-hour cutoff.
    """
    if cutoff > shallow_ts + 1e-6:
        return False
    if exhausted:
        return True
    return covered_until <= cutoff + 1e-6


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


def _inclusive_max_ts(ts: float) -> int:
    """Whole-second max_ts that still includes a trade at ``ts``.

    The trades endpoint takes unix seconds. Ceiling the boundary keeps that
    print in the window. Stepping back a truncated second drops it.
    """
    return int(math.ceil(float(ts) - 1e-6))


def _history_error(failures: int, unreadable: int) -> str | None:
    parts = []
    if failures:
        parts.append(f"{failures} trade lookup{'s' if failures != 1 else ''} failed")
    if unreadable:
        parts.append(f"{unreadable} trade read{'s' if unreadable != 1 else ''} had no price")
    if not parts:
        return None
    return "Kalshi settled record: " + ", and ".join(parts) + "."


def _read_trades(
    client: MarketHttp,
    root: str,
    ticker: str,
    shallow_ts: float,
    deep_ts: float,
    resume: dict | None,
    budget: dict,
) -> dict | None:
    """Page trades until they reach ``deep_ts``, the list ends, or the budget does.

    A later scan continues with the cursor and the same max_ts. A numeric
    cutoff a second earlier would drop the prints in between. A returned dict
    is safe to store. ``None`` means this ticker must be tried again: the call
    failed, or the payload had trades with no readable price.
    ``budget['stop']`` is set when the per-scan cap is used up.
    """
    resume = resume or {}
    cursor = str(resume.get("cursor") or "")
    if cursor and resume.get("max_ts") is not None:
        max_ts = int(resume["max_ts"])
    else:
        cursor = ""
        max_ts = _inclusive_max_ts(shallow_ts)
    collected: list[dict] = []
    oldest: float | None = None
    exhausted = False
    reached = False
    next_cursor = cursor
    next_max_ts = max_ts
    while True:
        if budget["lookups"] >= TRADE_LOOKUPS:
            budget["stop"] = True
            break
        budget["lookups"] += 1
        params = {"ticker": ticker, "limit": str(TRADE_PAGE), "max_ts": str(max_ts)}
        if cursor:
            params["cursor"] = cursor
        try:
            payload = client.get_json(f"{root}/markets/trades", params=params)
        except Exception as exc:
            log.warning("kalshi trades %s failed: %s", ticker, exc)
            budget["failures"] += 1
            return None
        trades = payload.get("trades") if isinstance(payload, dict) else None
        if not isinstance(trades, list):
            budget["failures"] += 1
            return None
        if not trades:
            exhausted = True
            next_cursor = ""
            break
        readable = 0
        page_oldest: float | None = None
        for trade in trades:
            if not isinstance(trade, dict):
                continue
            traded_at = _dt(trade.get("created_time"))
            price = _yes_price(trade)
            if traded_at is None:
                continue
            stamp = traded_at.timestamp()
            if page_oldest is None or stamp < page_oldest:
                page_oldest = stamp
            if price is None:
                continue
            readable += 1
            trade_id = str(trade.get("trade_id") or "")
            collected.append({
                "key": trade_id or f"{stamp:.6f}:{price:.4f}",
                "ts": stamp,
                "price": price,
            })
        if readable == 0:
            budget["unreadable"] += 1
            return None
        if page_oldest is not None:
            oldest = page_oldest if oldest is None else min(oldest, page_oldest)
        if oldest is not None and oldest <= deep_ts + 1e-6:
            reached = True
            next_cursor = ""
            break
        cursor = str(payload.get("cursor") or "") if isinstance(payload, dict) else ""
        if cursor:
            next_cursor = cursor
            next_max_ts = max_ts
            continue
        if len(trades) < TRADE_PAGE:
            exhausted = True
            next_cursor = ""
            break
        if oldest is None:
            budget["unreadable"] += 1
            return None
        # No cursor. The next window still includes the oldest print we have.
        stepped = _inclusive_max_ts(oldest)
        if stepped >= max_ts:
            next_cursor = ""
            next_max_ts = max_ts
            break
        max_ts = stepped
        next_cursor = ""
        next_max_ts = stepped
    if not collected and not exhausted:
        return None
    done = reached or exhausted
    covered_until = oldest if oldest is not None else shallow_ts
    return {
        "trades": collected,
        "shallow_ts": shallow_ts,
        "covered_until": covered_until,
        "exhausted": exhausted,
        "reached": done,
        "resume_cursor": "" if done else next_cursor,
        "resume_max_ts": None if done else next_max_ts,
    }


def fetch_settled_record(
    base_url: str,
    pages: int,
    page_size: int,
    hours: float,
    skip: set[str] | None = None,
    resume: dict[str, dict] | None = None,
    http: MarketHttp | None = None,
) -> tuple[dict[str, dict] | None, str | None]:
    """New per-ticker trade windows from settled markets.

    Reads start at the one-hour rail and page backward until the trades reach
    the current horizon. The caller stores the prints and scores whatever
    horizon is in force. A market that fails a field check is left unstored.
    A null sample is returned only after a lookup that found no trade before
    the cutoff. ``None`` for the whole payload means the market list failed.
    """
    own = http is None
    client = http or MarketHttp()
    root = base_url.rstrip("/")
    seen = skip or set()
    resume = resume or {}
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
    budget = {"lookups": 0, "failures": 0, "unreadable": 0, "stop": False}
    try:
        for market in markets:
            if budget["stop"]:
                break
            ticker = str(market.get("ticker") or "")
            if not ticker or ticker in seen or ticker in samples:
                continue
            if not _needs_trade(market, hours):
                continue
            close = _close_time(market)
            if close is None:
                continue
            close_ts = close.timestamp()
            shallow_ts = (close - timedelta(hours=min(hours, RAIL_HOURS))).timestamp()
            deep_ts = (close - timedelta(hours=hours)).timestamp()
            window = _read_trades(
                client,
                root,
                ticker,
                shallow_ts,
                deep_ts,
                resume.get(ticker),
                budget,
            )
            if window is None or not window["reached"]:
                if window and window["trades"]:
                    samples[ticker] = {
                        "result": str(market.get("result") or "").lower(),
                        "close_ts": close_ts,
                        "volume": _num(market.get("volume_fp")) or 0.0,
                        "hours": hours,
                        "shallow_ts": window["shallow_ts"],
                        "covered_until": window["covered_until"],
                        "exhausted": False,
                        "trades": window["trades"],
                        "reached": False,
                        "resume_cursor": window.get("resume_cursor") or "",
                        "resume_max_ts": window.get("resume_max_ts"),
                    }
                continue
            yes_price = yes_price_before_close(
                market,
                [{"created_time": datetime.fromtimestamp(item["ts"], timezone.utc).isoformat(), "yes_price_dollars": item["price"]} for item in window["trades"]],
                hours,
            )
            observed = settled_favorite(market, yes_price)
            row = _sample_row(None if observed is None else observed[0], False if observed is None else observed[1], hours)
            row.update({
                "result": str(market.get("result") or "").lower(),
                "close_ts": close_ts,
                "volume": _num(market.get("volume_fp")) or 0.0,
                "shallow_ts": window["shallow_ts"],
                "covered_until": window["covered_until"],
                "exhausted": bool(window["exhausted"]),
                "trades": window["trades"],
                "reached": True,
            })
            samples[ticker] = row
        return samples, _history_error(budget["failures"], budget["unreadable"])
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
