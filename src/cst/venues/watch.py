"""Discover in the background; fetch current near-resolution quotes each tick."""
from __future__ import annotations

import threading
import time
from datetime import datetime, timezone

from cst.venues.http import MarketHttp
from cst.venues.kalshi import quotes_from_kalshi_market, _series_fees


class MarketWatch:
    def __init__(self, discover, interval=30):
        self.discover = discover
        self.interval = interval
        self.lock = threading.Condition()
        self.stop_event = threading.Event()
        self.thread = None
        self.poll_thread = None
        self.latest = ([], ["Quote monitoring is warming up."])
        self.polled_at = 0.0
        self.generation = self.consumed = 0
        self.quotes = []
        self.errors = ["Market discovery is warming up."]
        self.discovered_at = 0.0
        self.client = MarketHttp(timeout=5)
        self.settings = None

    def close(self):
        self.stop_event.set()
        # The quote thread closes its client after the in-flight read completes.

    def _discover(self, settings):
        while not self.stop_event.is_set():
            with self.lock:
                settings = self.settings or settings
            started = time.monotonic()
            try:
                quotes, errors = self.discover(settings)
                with self.lock:
                    self.quotes, self.errors = quotes, errors
                    self.discovered_at = time.monotonic()
            except Exception as exc:
                with self.lock:
                    self.errors = [f"Discovery failed ({type(exc).__name__})."]
            self.stop_event.wait(max(0.1, self.interval - (time.monotonic() - started)))

    def __call__(self, settings):
        with self.lock:
            self.settings = settings
        if self.thread is None:
            self.thread = threading.Thread(target=self._discover, args=(settings,),
                                           name="cst-discovery", daemon=True)
            self.thread.start()
            self.poll_thread = threading.Thread(target=self._monitor, args=(settings,),
                                                name="cst-quotes", daemon=True)
            self.poll_thread.start()
        with self.lock:
            self.lock.wait_for(lambda: self.generation != self.consumed or self.stop_event.is_set(), timeout=1.1)
            if time.monotonic() - self.polled_at > 2:
                return [], ["Waiting for fresh quotes."]
            if self.consumed == self.generation:
                return [], list(self.latest[1])
            self.consumed = self.generation
            return list(self.latest[0]), list(self.latest[1])

    def _monitor(self, settings):
        try:
            while not self.stop_event.is_set():
                with self.lock:
                    settings = self.settings or settings
                started = time.monotonic()
                try:
                    result = self._poll(settings)
                except Exception as exc:
                    result = ([], [f"Quote monitor failed ({type(exc).__name__})."])
                with self.lock:
                    self.latest = result
                    self.polled_at = time.monotonic()
                    self.generation += 1
                    self.lock.notify_all()
                self.stop_event.wait(max(0.05, 1 - (time.monotonic() - started)))
        finally:
            self.client.close()

    def _poll(self, settings):
        with self.lock:
            known, errors, discovered_at = list(self.quotes), list(self.errors), self.discovered_at
        if time.monotonic() - discovered_at > 120:
            return [], errors + ["Discovery unavailable or stale."]
        now = datetime.now(timezone.utc)
        # Include both sides and sub-90% markets so crossing the threshold is seen.
        wanted = {q.market_id: q for q in known if q.entry_deadline and
                  0 < (q.entry_deadline - now).total_seconds() <= settings.entry_window_minutes * 60 + 60}
        tickers = sorted(wanted)
        fresh = []
        fetched_batches = []
        root = settings.kalshi_base_url.rstrip("/")
        for offset in range(0, len(tickers), 100):
            batch = tickers[offset:offset + 100]
            batch_start = len(fresh)
            try:
                cursor = None
                seen = set()
                while True:
                    params = {"tickers": ",".join(batch), "limit": 100}
                    if cursor:
                        params["cursor"] = cursor
                    payload = self.client.get_json(f"{root}/markets", params=params)
                    markets = payload.get("markets", [])
                    # Discovery can first see a market below 90%, before fee lookup.
                    needs_fee = [m for m in markets if m.get("ticker") in wanted and not wanted[m["ticker"]].fee_verified]
                    fees, _ = _series_fees(self.client, root, needs_fee)
                    for market in markets:
                        old = wanted.get(market.get("ticker"))
                        if old is None:
                            continue
                        series = fees.get(old.market_id.split("-")[0], {})
                        if old.fee_verified:
                            series = {"fee_type": "quadratic", "fee_multiplier": old.fee_rate / .07}
                        fresh.extend(quotes_from_kalshi_market(market,
                            event={"title": old.event_title, "category": old.category}, series=series))
                    cursor = payload.get("cursor")
                    if not cursor:
                        break
                    if cursor in seen:
                        raise ValueError("Repeated pagination cursor")
                    seen.add(cursor)
                fetched_batches.append((time.monotonic(), fresh[batch_start:]))
            except Exception as exc:
                errors.append(f"Quote refresh failed ({type(exc).__name__}).")
                # No cached quotes are substituted for a failed read.
        return [q for ts, batch in fetched_batches if time.monotonic() - ts <= 2 for q in batch], errors
