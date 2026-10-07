# Package

The scan in `engine.py` reads quotes, calls `strategy.evaluate`, optionally drops clips, checks `depth`, then asks `broker.py` to paper-fill. `broker.py` never calls a venue.

- `strategy.py` is the only place a buy is born. Tests in `tests/test_desk.py` lock the fee, settled-record, and horizon gates. The default strategy requires an open market with `expected_expiration_time` in the next ten minutes. Its calibration uses prospective first eligible quotes, at most one per event, without mixing in legacy two-hour history or double-counting paper settlements. Legacy replay mode (`entry_window_minutes=0`) retains the old pre-close record. A fill re-checks every admission rule on the current knobs and the current book.
- `venues/` parses public Kalshi payloads. A clip settles when Kalshi `result` is `yes` or `no`, not when the price sits near 0 or 1.
- `depth.py` and `decisions.py` fail closed for a fill and fail open for the veto. An outage does not create a buy and does not skip the fee rule.
- `store.py` owns the sqlite book, the CSRF token, and the append-only audit.

Do not add a route or a client method that posts an order.
