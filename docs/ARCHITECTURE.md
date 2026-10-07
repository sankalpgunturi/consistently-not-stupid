# Architecture

A paper account of $1,000. One process reads public books, decides, and writes a sqlite file. The dashboard is a human view of that file, plus a few tighten-only overrides. Nothing in this process signs or posts an order.

## Loop

Every `scan_interval_seconds` (default 10 minutes) `Engine.run_cycle`:

1. Read Kalshi public markets (first pages, multivariate combos excluded).
2. Remember which quotes stayed inside the probability band. A favorite has to be stable for `min_stable_scans`.
3. `strategy.evaluate` builds proposals. A buy exists only when the fee, horizon, size cap, correlation, and the settled record all pass. The confirming price is the Wilson lower bound of that ask's bucket. The bucket counts Kalshi settled markets whose last trade landed there, plus this desk's own settlements. It has to sit above the all-in cost by `min_edge`, and the bucket needs `min_sample` observations. A high quote with no record stays in cash. Before each fill the scan re-reads the knobs, because a tighten can land while the book is still being fetched.
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
