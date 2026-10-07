# Package

The scan in `engine.py` reads quotes, calls `strategy.evaluate`, optionally drops clips, checks `depth`, then asks `broker.py` to paper-fill. `broker.py` never calls a venue.

- `strategy.py` is the only place a buy is born. Tests in `tests/test_desk.py` lock the fee, pair, and horizon gates.
- `venues/` parses public payloads. Settlement flags come from Kalshi `result` and Polymarket `umaResolutionStatus`, not from a price near 0 or 1.
- `depth.py` and `decisions.py` fail closed for a fill and fail open for the veto. An outage does not create a buy and does not skip the fee rule.
- `store.py` owns the sqlite book, the CSRF token, and the append-only audit.

Do not add a route or a client method that posts an order.
