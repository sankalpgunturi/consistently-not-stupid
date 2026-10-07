# Consistently Not Stupid

> It is remarkable how much long-term advantage people like us have gotten by trying to be consistently not stupid, instead of trying to be very intelligent.
>
> Charlie Munger

A paper trading desk for high-probability Kalshi contracts. It starts with $1,000 of simulated cash, reads the public book, and buys only when a favorite still makes sense after the Kalshi fee and the settled record. The aim is to avoid the stupid trade, not to invent a clever one.

This month is a paper evaluation of the algorithm, with live trading planned for a later version. The current process places simulated trades only. A Kalshi key is not required for the paper book. Public market data is enough. Finishing the month does not automatically enable real orders.

## The rule

A contract priced at 90¢ is not a gift. If that price is the true chance, buying it loses the fee. The desk treats cash as the default and buys only when all of this is true:

1. Both the bid and the ask are at least 90%. A one-sided print is ignored.
2. The spread is tight, the book has some depth, and the contract closes between 2 hours and 21 days out. A 95¢ "no" on a 2028 nomination locks cash for years and is skipped.
3. If the favorite wins, the payout still clears the venue fee by at least 1¢ a share. Near 99¢, the fee often eats the rest, so those are skipped too.
4. The quote has sat in that band for two scans (about 20 minutes at the default cadence).
5. The settled record for that price bucket sits above the all-in cost (ask plus the Kalshi fee) by at least the minimum edge. The record is Kalshi settled markets whose latest trade, taken at least two hours before close, sat in that bucket, plus this desk's own resolutions. Each market counts once. A bucket needs 30 observations. The cautious win rate is a Wilson lower bound, not the raw hit rate. A high quote by itself is not a buy, and the desk does not take extra clips to manufacture a sample.
6. The clip is one Kalshi contract, and a fresh orderbook has to show enough size. It also has to fit inside 0.8% of equity.
7. The book does not already hold that event, or a close cousin of it. One French election, one Bitcoin ladder, one city temperature.

New buys pause if equity falls 5% from its peak, or if you pause them. If a held favorite's bid drops 8¢ under the price we paid, and the bid still has size, the desk sells. That can book a small loss. It is there so a broken quote does not turn into a total loss of the premium. A stop is not a promise that the account never goes down.

After each scan the desk writes a retrospective. The governor may only tighten a knob, one step, inside the rail. It cannot turn the fee rule off, and it cannot lower the 90% floor. With `CST_OPENAI_API_KEY` set, one Decisions call may drop a proposed clip, and a chat note may suggest drops. Neither can add a buy. A missing key or a bad response leaves the deterministic set.

## Run it

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
pytest
cst serve
```

Open http://127.0.0.1:8000.

For continuous operation on this Mac, run `.venv/bin/python tools/paper_service.py install`. The macOS service starts at login, restarts after a crash, and prevents idle sleep while running. It reads `.env` from this checkout. Keep the Mac powered, awake, and connected; closing a laptop lid or shutting it down can interrupt the run. Use `status`, `restart`, or `stop` in place of `install` to manage the service. A restart loads code changes without resetting the account.

The runner scans every ten minutes and marks open positions every minute. Its log is `data/logs/runner.log`; the account and research record live in `data/book.sqlite`. Scans retain a source-code fingerprint, observation and evaluation times, the book before execution, settings, calibration, counts, refusals, equity, and favorite quotes. Trade records contain fills, fees, exits, and settlements. Retrospectives retain model concerns, proposed and applied changes, and model failures. These records are local and ignored by Git.

Run `.venv/bin/python tools/paper_report.py` for a read-only JSON retrospective with recent scans, reviews, trades, changes, and cash/fee/P&L reconciliation. Fills and settlements update the account in one database transaction; an interrupted write rolls back the whole operation.

`cst cycle` runs one scan in the terminal. `cst simulate` prints the separate model described below. `cst reset` returns the paper book to the starting cash and starts the paper window over. `cst bench` times the admission rule on a fixed fixture and, if a key is set, one Decisions call. It does not write the book.

Copy `.env.example` to `.env` to change the starting bankroll, the 90% bar, or the 10-minute scan. After the first run, learned knobs live in `data/book.sqlite` and survive a restart. The environment values are the seed, and the reset command restores them.

## What the dashboard is showing

The status line stays **Paper** and **Live planned**. There is no control that arms a live order. The strip under the thesis is the paper window: day count, net P&L after fees, resolved trades, hit rate, drawdown, and cash still in open clips. The simulation record shows archived observations and historical sample sizes separately from this account's results. A fill is an open position, not a confirmed win.

Open **Activity & analysis** for scan counts, reviews, and historical evidence. **Settings** contains controls and risk parameters. The scan reads a bounded number of market pages within the configured closing-time window, then checks each active contract against the current rules. It does not cover every contract.

You can pause new buys, block a contract, close a paper clip when the book has size, and tighten a risk knob by one step. You cannot force a buy the fee rule or the settled record refused. Those changes are listed under "What changed by hand." Mutating requests send the token from the page's own state.

"Closest look" is the buy, or the favorite that came nearest and was still refused, with the cents written out.

The equity line is the paper account, marked at the bid after an exit fee. A new clip usually shows a small dip equal to the spread and the fee. That dip is real friction, not a bug.

The table at the bottom is a model with a known true chance. It is not the live book.

- Buying every 90% quote, when the quote is right, loses money. The loss is the fee, and one defeat wipes many small wins.
- This desk, in that same world, does not trade. Equity stays at the start.
- This desk, when a settled record sits above the ask, has a positive average.

## Fees

The scan reads each favorite's series fee multiplier and applies `0.07 × multiplier × contracts × price × (1 − price)`. Unknown fee schedules are skipped. Paper buys cross the spread; fills and exit marks round the fee up to the next cent as a conservative approximation. The recorded entry fee schedule is retained for exit marks. Kalshi's account-specific precision and per-order rounding rebates are not simulated. See the [series metadata](https://docs.kalshi.com/api-reference/market/get-series) and [fee-rounding rules](https://docs.kalshi.com/getting_started/fee_rounding).

## Keys, later

When you are ready to talk about real orders, the names in `.env.example` are the ones to fill:

- OpenAI: `CST_OPENAI_API_KEY`. Optional. It lets the Decisions call drop a duplicate clip and lets the chat note read the tape. The desk still runs without it. The Decisions model is `gpt-6-luna`. The chat model is `CST_OPENAI_MODEL`.
- The Kalshi signing key is unused. This build does not send orders. Adding that is a later reviewed change, after the paper window has something to look at: hit rate, fees, drawdown, and how many clips actually resolved. A month of flat cash is a successful result if the book never offered a fee-adjusted edge.

## Layout

- `src/cst/fees.py` — venue fee math
- `src/cst/venues/` — public Kalshi reads
- `src/cst/text.py` — strict match for "same price", looser match for "same risk"
- `src/cst/strategy.py` — the admission rule
- `src/cst/depth.py` — fresh displayed size, read only
- `src/cst/decisions.py` — one drop-only Decisions call per scan
- `src/cst/review.py` — the governor, and the optional chat note
- `src/cst/broker.py` — paper fills, stops, settlement
- `src/cst/store.py` — the book, the audit, the operator overrides
- `src/cst/simulate.py` — the separate model
- `src/cst/engine.py` — the scan loop
- `src/cst/dashboard/` — the page served at `/`
- `docs/ARCHITECTURE.md` — where to look
- `docs/GAPS.md` — choices still open
- `AGENTS.md` — how to work in this repo

The scan filters by closing time and excludes multivariate combos and inactive contracts. It reads a bounded number of pages. Activity & analysis shows the counts.

## References

The fee formula and the public endpoints follow Kalshi's market-data quick start. The shape of the desk — a venue adapter, a paper broker, and a risk gate in front of any order — is the usual structure of paper-first prediction-market bots. The code here is original.

A favorite can settle at zero. Sizing and the pause line keep that from being the whole account. They do not repeal it.
