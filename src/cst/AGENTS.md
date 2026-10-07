# Package

The scan in `engine.py` reads quotes, calls `strategy.evaluate`, optionally drops clips, checks `depth`, then asks `broker.py` to fill. `broker.py` never calls a venue. In paper mode that fill is simulated. After the operator approves a live amount, `live.py` signs the order first and the broker records only a fill Kalshi actually made.

- `strategy.py` is the only place a buy is born. Tests in `tests/test_desk.py` lock the fee, settled-record, and horizon gates. The default strategy requires an open market with `expected_expiration_time` in the next ten minutes. Prospective first eligible quotes, at most one per event, are research only; historical sample size and confidence bounds must not block paper entries. Legacy history remains separate. Legacy replay mode (`entry_window_minutes=0`) retains the old pre-close record. A fill re-checks every admission rule on the current knobs and the current book.
- `venues/` parses public Kalshi payloads. A clip settles when Kalshi `result` is `yes` or `no`, not when the price sits near 0 or 1.
- `depth.py` and `decisions.py` fail closed for a fill and fail open for the veto. An outage does not create a buy and does not skip the fee rule.
- `store.py` owns the sqlite book, the CSRF token, and the append-only audit.

Do not post an order from anywhere except `live.py`, and do not post one before the operator has approved a live amount.

- The short-window strategy holds to the official outcome when stop loss is Off (the default). If the operator enables it, a bid drop of the selected cents below entry triggers a depth-checked exit. Manual close remains available; legacy replay retains its stop rule.
