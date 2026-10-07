# Architecture

A paper account of $1,000. One process reads public books, decides, and writes a sqlite file. The dashboard is a human view of that file, plus a few tighten-only overrides. Nothing in this process signs or posts an order.

## Loop

At a `scan_interval_seconds` target cadence (default one second), `Engine.run_cycle`:

1. Consume a fresh near-resolution quote batch. Background discovery refreshes the bounded market universe every 30 seconds; a separate thread polls current quotes every second in groups of 100 tickers. Multivariate combos are excluded.
2. Remember which quotes stayed inside the probability band. A favorite has to be stable for `min_stable_scans`.
3. Record eligible research observations and resolve pending outcomes. `strategy.evaluate` proposes stable quoted favorites that pass probability, fee, expected-outcome timing, size and correlation checks. The ten-minute paper experiment does not require historical evidence or a confidence bound. Research outcomes remain available for daily review. Legacy replay retains its evidence gate. Before each fill, re-read the knobs and book and run admission again.
4. If `CST_OPENAI_API_KEY` is set, one `POST /v1/decisions` call (`gpt-6-luna`) may drop a proposed clip. The chat note may also name drops. Unknown ids are ignored.
5. Before a paper buy, `depth.py` reads the Kalshi orderbook. The other side's bids are the ask. Short size, a moved touch, or a failed read skips the fill.
6. `broker.py` debits cash, stores the position, and appends a trade. Marks use the bid minus the exit fee. The mark loop (`mark_interval_seconds`, default 1s) refreshes open positions, settles authoritative results, and holds paper positions until an official result. Price-drop stops apply only in legacy replay; manual close remains available.
7. The governor may tighten one knob. It writes an audit row.

`cst simulate` is a separate model with a known true chance. It does not touch the book. `cst bench` times step 3, and step 4 when a key is set, and also does not touch the book.

## Where a behavior lives

| Question | File |
| --- | --- |
| Is this quote allowed to become a buy? | `src/cst/strategy.py` |
| Are two titles the same price, or merely related? | `src/cst/text.py` |
| What does the venue charge? | `src/cst/fees.py` |
| How is a payload turned into a `Quote`? | `src/cst/venues/kalshi.py` |
| Is the clip actually on the book? | `src/cst/depth.py` |
| May the model remove it? | `src/cst/decisions.py`, `src/cst/review.py` |
| Did cash move? | `src/cst/broker.py` |
| Where is the book, the token, the audit? | `src/cst/store.py` |
| What does the page call? | `src/cst/api.py`, `src/cst/dashboard/` |
| What are the rails and the seed? | `src/cst/models.py` (`RAILS`, `STEPS`), `src/cst/config.py` |

`Quote`, `Proposal`, `Decision`, `Position`, and `BookView` are the records that cross those files. The paper strategy uses quoted favorites; research results do not gate admission.

## Human overrides

The page can pause new buys, block a market key, close a paper clip, and tighten one risk knob. The server computes the tighter value. The client does not send a new number. Close and stop both go through the depth check. Reset is the only way to clear `paper_started_at`.

`POST /api/*` requires header `X-CSRF-Token` equal to the token on `GET /api/state`. A foreign `Origin` is rejected. The server binds to `127.0.0.1`. There is no login and no arm control. The snapshot says `mode: paper` and `live: unavailable`.

## What this is not

No matching engine, no colocation, no second strategy, no per-market model call, no model-originated buys, no live signing. Inventory caps, the pause, executable size, fees and refreshed marks constrain paper execution. The ten-minute limit applies to the expected outcome; quote monitoring targets one second. Model review and API latency can delay actual admission; cycles never overlap or run catch-up bursts.

## Ten-minute paper strategy (October 7 clarification)

New books use `entry_window_minutes=10` and a one-second target between evaluations. A quote needs an explicit future `expected_expiration_time` within that window and a future trading close. Close time alone is not evidence that the outcome is imminent. Discovery prioritizes the next ten minutes, then the next hour and day, with an additional unfiltered open-market allocation. This is bounded discovery, not exhaustive exchange coverage.

Each cycle reads quotes, records eligible research observations, resolves pending observations, evaluates the deterministic rule, and asks the models to veto proposals. Quotes and timing are checked again after model review. Calls remain sequential so no overlapping scans can duplicate a buy; actual cadence includes scan and review duration. Positions are marked between scans.

`near_observations` preserves the first structurally eligible quote per event, its expected outcome time, price, fee, and eventual result. These are research observations, not fills or ledger profits. Event deduplication reduces repetition but does not establish independence across events. Calibration counts only resolved observations from the active entry window. The former two-hour samples remain in their original tables for replay and are not used to admit these bets. Historical evidence does not gate the ten-minute paper experiment; the user explicitly removed that requirement.

The experimental target is nonnegative net portfolio performance per 24 hours, anchored to initial funding. Individual trades may lose. Daily equity change includes fees and unrealized changes; fees are reported separately but never subtracted twice. A negative day triggers review rather than an automatic parameter change. The original $1,000 is the only contribution; proceeds stay available for reinvestment. Neither high prices nor a short horizon guarantees a result. Venue timing estimates may change, and payment may follow the expected outcome.

Daily reports use consecutive 24-hour windows from `paper_started_at`, rather than calendar midnight. `daily_reviews` stores net equity change, realized P&L, the change in unrealized P&L, fees, trading counts, scan totals, errors, and parameter-change evidence. Boundary marks and their ages are retained so downtime cannot masquerade as a precise valuation. The scan reviewer receives pending daily evaluations and saves its review; API failures leave a review pending for retry. Daily results do not reset the account.

The dashboard compares the paper account with a $1,000 SPY benchmark using Yahoo Finance adjusted daily closes. SPY is an S&P 500 ETF proxy. The last completed close at or before funding is the baseline; the latest completed close is displayed with its date. Adjustments include distributions and splits, while investor brokerage fees and taxes are excluded. The cache refreshes at most every 15 minutes with a five-second HTTP timeout; a failed update retains and labels the last value. No 10% assumed return is substituted for observed data. This is a daily-close comparison, not an intraday index feed.

Model token usage, when returned by the provider, is archived with the scan and retrospective for both model endpoints. API billing is external to the simulated Kalshi account; the dashboard's profit is after trading fees, not after model operating costs. Unknown usage or dollar pricing remains unknown rather than zero. Older scans without usage metadata cannot support a full operating-cost calculation.

Market scans do not wait ten minutes for routine commentary. The retrospective runs immediately for proposed buys, new calibration outcomes, changed parameters/errors, or pending daily evaluations. An unchanged empty portfolio gets model review at most once every ten minutes; intervening scans still execute deterministic screening, archive evidence, and display a fresh built-in summary. Deferred reviews cannot reuse old drops, suggestions, errors, or token counts.

Near-term discovery reserves one market-list request per broader window and assigns the remaining requests to the nearest window. With eight requests and four windows, it can read up to five 1,000-market pages nearest-first. If a window runs out of pages early, its unused requests go to later windows. Broader windows retain the configured page size. A warning records when the nearest window still has a cursor after reaching its cap. This remains bounded discovery, not exhaustive exchange coverage.

## Public read budget

All Kalshi `MarketHttp` clients share a paced 20 requests/second ceiling with no burst allowance. This is the published Basic default-cost equivalent (200 tokens/second divided by 10); Kalshi documents these as authenticated limits, so it is not a verified public quota or account tier. A 429 halves the local rate and applies exponential shared backoff. Recovery adds one request/second per quiet minute, up to the ceiling. Other non-retryable 4xx responses fail immediately.

The quote monitor continues during model review. Discovery is not an entry quote: only freshly fetched batches are evaluated, duplicate snapshots cannot count as another stable scan, and failed or stale reads supply no substitute quotes. Two stable scans now mean two fresh observations about a second apart. Research outcome polling runs every 30 seconds. Each scan archives actual duration, feed request count, current cap and throttles. Pre-fill model vetoes, fresh-quote/depth checks and portfolio limits remain in force.

References: https://docs.kalshi.com/getting_started/rate_limits and https://docs.kalshi.com/api-reference/market/get-markets.
