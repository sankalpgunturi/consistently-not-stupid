"""Public JSON reads with a process-wide paced budget and shared 429 backoff."""
from __future__ import annotations

import threading
import time
from urllib.parse import urlsplit

import httpx


class ReadBudget:
    """No bursts: cap at 20 reads/s; halve on throttling, recover gradually.

    20 is the published Basic default-cost equivalent, not a claim about the
    unauthenticated quota. All clients in this process share this budget.
    """
    def __init__(self, rate=20.0, clock=time.monotonic, sleep=time.sleep):
        self.maximum = self.rate = rate
        self.clock, self.sleep = clock, sleep
        self.lock = threading.Lock()
        self.next_at = self.cooldown = 0.0
        self.last_recovery = clock()
        self.requests = self.throttles = 0

    def acquire(self):
        while True:
            with self.lock:
                now = self.clock()
                delay = max(self.next_at, self.cooldown) - now
                if delay <= 0:
                    if now - self.last_recovery >= 60:
                        self.rate = min(self.maximum, self.rate + 1)
                        self.last_recovery = now
                    self.next_at = now + 1 / self.rate
                    self.requests += 1
                    return
            self.sleep(delay)

    def throttled(self, attempt):
        with self.lock:
            self.throttles += 1
            self.rate = max(1.0, self.rate / 2)
            self.last_recovery = self.clock()
            self.cooldown = max(self.cooldown, self.clock() + min(30, 2 ** attempt))


BUDGET = ReadBudget()


class MarketHttp:
    def __init__(self, timeout: float = 20):
        self.client = httpx.Client(timeout=timeout, follow_redirects=True,
            headers={"User-Agent": "consistently-not-stupid/0.1", "Accept": "application/json"})

    def get_json(self, url: str, params: dict | None = None) -> dict | list:
        host = urlsplit(url).hostname or ""
        limited = host == "kalshi.com" or host.endswith(".kalshi.com")
        for attempt in range(3):
            if limited:
                BUDGET.acquire()
            try:
                response = self.client.get(url, params=params)
                response.raise_for_status()
                return response.json()
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                if status == 429 and limited:
                    BUDGET.throttled(attempt)
                if attempt == 2 or (status < 500 and status != 429):
                    raise
                if status != 429 or not limited:
                    time.sleep(0.25 * 2 ** attempt)
            except httpx.TransportError:
                if attempt == 2:
                    raise
                time.sleep(0.25 * 2 ** attempt)
        raise RuntimeError("Read retries exhausted")

    def close(self) -> None:
        self.client.close()
