# Architecture

A paper account of $1,000. One process reads public books, decides, and writes a sqlite file. The dashboard is a human view of that file, plus a few tighten-only overrides. Nothing in this process signs or posts an order.

## Loop

Every `scan_interval_seconds` (default 10 minutes) `Engine.run_cycle`:

1. Read Kalshi public markets (first pages, multivariate combos excluded).
2. Remember which quotes stayed inside the probability band. A favorite has to be stable for `min_stable_scans`.
3. `strategy.evaluate` builds proposals. A buy exists only when the fee, horizon, size cap, correlation, and the settled record all pass. The confirming price is the Wilson lower bound of that ask's bucket. The bucket counts Kalshi settled markets whose latest trade at least `min_hours_to_expiry` before close landed there, plus this desk's own settlements. The prints are kept, so a tighter horizon is scored from the same trades when those trades reach the new cutoff. A ticker we have settled is counted from that settlement only. It has to sit above the all-in cost by `min_edge`, and the bucket needs `min_sample` observations. A high quote with no record stays in cash. Before each fill the scan re-reads the knobs and the book, and runs the admission rules again, because a tighten can land while the book is still being fetched.
4. If `CST_OPENAI_API_KEY` is set, one `POST /v1/decisions` call (`gpt-6-luna`) may drop a proposed clip. The chat note may also name drops. Unknown ids are ignored.
5. Before a paper buy, `depth.py` reads the Kalshi orderbook. The other side's bids are the ask. Short size, a moved touch, or a failed read skips the fill.
6. `broker.py` debits cash, stores the position, and appends a trade. Marks use the bid minus the exit fee. A faster loop (`mark_interval_seconds`, default 60s) refreshes open positions, settles authoritative results, and stops a clip whose bid fell `stop_gap` under the entry, if that bid has size.
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

`Quote`, `Proposal`, `Decision`, `Position`, and `BookView` are the records that cross those files. The strategy confirms a buy from the settled record, not from a second venue.

## Human overrides

The page can pause new buys, block a market key, close a paper clip, and tighten one risk knob. The server computes the tighter value. The client does not send a new number. Close and stop both go through the depth check. Reset is the only way to clear `paper_started_at`.

`POST /api/*` requires header `X-CSRF-Token` equal to the token on `GET /api/state`. A foreign `Origin` is rejected. The server binds to `127.0.0.1`. There is no login and no arm control. The snapshot says `mode: paper` and `live: unavailable`.

## What this is not

No matching engine, no colocation, no second strategy, no per-market model call, no model-originated buys, no live signing. Inventory caps, the pause, executable size, the fee, and a faster mark loop than the entry loop are the pieces kept from high-frequency practice. The decision clock stays at 10 minutes.

## Ten-minute paper strategy (October 7 clarification)

New books use `entry_window_minutes=10` and a 60-second delay between scans. A quote needs an explicit future `expected_expiration_time` within that window and a future trading close. Close time alone is not evidence that the outcome is imminent. Discovery prioritizes the next ten minutes, then the next hour and day, with an additional unfiltered open-market allocation. This is bounded discovery, not exhaustive exchange coverage.

Each cycle reads quotes, records eligible research observations, resolves pending observations, evaluates the deterministic rule, and asks the models to veto proposals. Quotes and timing are checked again after model review. Calls remain sequential so no overlapping scans can duplicate a buy; actual cadence includes scan and review duration. Positions are marked between scans.

`near_observations` preserves the first structurally eligible quote per event, its expected outcome time, price, fee, and eventual result. These are research observations, not fills or ledger profits. Event deduplication reduces repetition but does not establish independence across events. Calibration counts only resolved observations from the active entry window. The former two-hour samples remain in their original tables for replay and are not used to admit these bets. Until enough appropriate evidence exists, the desk stays in cash.

The experimental target is nonnegative net portfolio performance per 24 hours, anchored to initial funding. Individual trades may lose. Daily equity change includes fees and unrealized changes; fees are reported separately but never subtracted twice. A negative day triggers review rather than an automatic parameter change. The original $1,000 is the only contribution; proceeds stay available for reinvestment. Neither high prices nor a short horizon guarantees a result. Venue timing estimates may change, and payment may follow the expected outcome.

Daily reports use consecutive 24-hour windows from `paper_started_at`, rather than calendar midnight. `daily_reviews` stores net equity change, realized P&L, the change in unrealized P&L, fees, trading counts, scan totals, errors, and parameter-change evidence. Boundary marks and their ages are retained so downtime cannot masquerade as a precise valuation. The scan reviewer receives pending daily evaluations and saves its review; API failures leave a review pending for retry. Daily results do not reset the account.

The dashboard compares the paper account with a $1,000 SPY benchmark using Yahoo Finance adjusted daily closes. SPY is an S&P 500 ETF proxy. The last completed close at or before funding is the baseline; the latest completed close is displayed with its date. Adjustments include distributions and splits, while investor brokerage fees and taxes are excluded. The cache refreshes at most every 15 minutes with a five-second HTTP timeout; a failed update retains and labels the last value. No 10% assumed return is substituted for observed data. This is a daily-close comparison, not an intraday index feed.

Model token usage, when returned by the provider, is archived with the scan and retrospective for both model endpoints. API billing is external to the simulated Kalshi account; the dashboard's profit is after trading fees, not after model operating costs. Unknown usage or dollar pricing remains unknown rather than zero. Older scans without usage metadata cannot support a full operating-cost calculation.

Market scans do not wait ten minutes for routine commentary. The retrospective runs immediately for proposed buys, open positions, new calibration outcomes, changed parameters/errors, or pending daily evaluations. An unchanged empty portfolio gets model review at most once every ten minutes; intervening scans still execute deterministic screening, archive evidence, and display a fresh built-in summary. Deferred reviews cannot reuse old drops, suggestions, errors, or token counts.
