"""Small JSON client with a short retry. Public market data only."""

from __future__ import annotations

import time

import httpx


class MarketHttp:
    def __init__(self, timeout: float = 20):
        self.client = httpx.Client(
            timeout=timeout,
            follow_redirects=True,
            headers={
                "User-Agent": "consistently-not-stupid/0.1",
                "Accept": "application/json",
            },
        )

    def get_json(self, url: str, params: dict | None = None) -> dict | list:
        last: Exception | None = None
        for attempt in range(3):
            try:
                response = self.client.get(url, params=params)
                response.raise_for_status()
                return response.json()
            except Exception as exc:
                last = exc
                time.sleep(0.25 * (attempt + 1))
        assert last is not None
        raise last

    def close(self) -> None:
        self.client.close()
