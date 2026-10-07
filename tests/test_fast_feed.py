from datetime import datetime, timedelta, timezone
import time
from types import SimpleNamespace

import httpx
import pytest

from cst.config import Settings
from cst.venues.http import ReadBudget, MarketHttp
from cst.venues.watch import MarketWatch
from tests.conftest import make_quote


def test_budget_paces_shared_reads_and_backs_off():
    now = [0.0]
    def sleep(delay):
        now[0] += delay
    budget = ReadBudget(clock=lambda: now[0], sleep=sleep)
    for _ in range(21):
        budget.acquire()
    assert now[0] == pytest.approx(1)
    budget.throttled(2)
    assert budget.rate == 10
    budget.acquire()
    assert now[0] == pytest.approx(5)
    now[0] += 60
    budget.acquire()
    assert budget.rate == 11


def test_http_retries_throttling_but_not_bad_requests(monkeypatch):
    budget = SimpleNamespace(acquire=lambda: None, throttled=lambda attempt: attempts.append(attempt))
    monkeypatch.setattr('cst.venues.http.BUDGET', budget)
    attempts = []
    codes = iter([429, 200, 400])
    client = MarketHttp()
    client.client.close()
    client.client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(next(codes), json={}, request=request)))
    assert client.get_json('https://external-api.kalshi.com/markets') == {}
    assert attempts == [0]
    with pytest.raises(httpx.HTTPStatusError):
        client.get_json('https://external-api.kalshi.com/markets')
    client.close()


def test_watch_refreshes_below_threshold_and_never_reuses_failed_quotes():
    watch = MarketWatch(lambda _: ([], []))
    watch.client.close()
    q = make_quote()
    q.expected_resolution_time = datetime.now(timezone.utc) + timedelta(minutes=5)
    q.bid, q.ask = .7, .72
    watch.quotes = [q]
    watch.discovered_at = time.monotonic()
    watch.errors = []
    calls = []
    def read(url, params):
        calls.append(params)
        return {'markets': [{'ticker': q.market_id, 'status': 'active',
            'yes_bid_dollars': '.91', 'yes_ask_dollars': '.92',
            'no_bid_dollars': '.08', 'no_ask_dollars': '.09',
            'expected_expiration_time': q.expected_resolution_time.isoformat()}]}
    watch.client = SimpleNamespace(get_json=read)
    quotes, errors = watch._poll(Settings())
    assert not errors
    assert quotes[0].bid == .91
    assert q.market_id in calls[0]['tickers']
    def fail(*a, **kw):
        raise TimeoutError()
    watch.client.get_json = fail
    assert watch._poll(Settings())[0] == []
    watch.discovered_at = time.monotonic() - 121
    assert watch._poll(Settings())[0] == []


def test_watch_only_consumes_each_poll_once_and_rejects_old_data():
    watch = MarketWatch(lambda _: ([], []))
    watch.client.close()
    watch.thread = object()  # No background worker in this deterministic test.
    watch.latest = ([make_quote()], [])
    watch.polled_at = time.monotonic()
    watch.generation = 1
    assert len(watch(Settings())[0]) == 1
    watch.stop_event.set()  # Bypass waiting for a new generation.
    assert watch(Settings())[0] == []
    watch.generation = 2
    watch.polled_at -= 3
    assert watch(Settings())[0] == []
