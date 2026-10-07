# The Common Sense Trade

A paper trading desk for high-probability contracts on Kalshi and Polymarket. It starts with $1,000 of simulated cash, reads the public books, and buys only when a favorite still makes sense after the venue fee.

The process never places an order. Kalshi and Polymarket keys are not required for the paper book. Public market data is enough.

## The rule

A contract priced at 90¢ is not a gift. If that price is the true chance, buying it loses the fee. The desk treats cash as the default and buys only when all of this is true:

1. Both the bid and the ask are at least 90%. A one-sided print is ignored.
2. The spread is tight, the book has some depth, and the contract closes between 2 hours and 21 days out. A 95¢ "no" on a 2028 nomination locks cash for years and is skipped.
3. If the favorite wins, the payout still clears the venue fee by at least 1¢ a share. Near 99¢, the fee often eats the rest, so those are skipped too.
4. The quote has sat in that band for two scans (about 20 minutes at the default cadence).
5. A long settled record, or a second venue the operator has marked as the same contract, sits above the all-in cost by at least the minimum edge. Similar titles are shown on the tape. They do not fill until a person approves that pair and notes what they checked in the resolution rules. The settled-record rule stays off until a price bucket has 30 resolved paper trades.
6. The clip is the venue minimum (1 Kalshi contract, or Polymarket's minimum size), and a fresh top of book has to show enough size. It also has to fit inside 0.8% of equity.
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

`cst cycle` runs one scan in the terminal. `cst simulate` prints the separate model described below. `cst reset` returns the paper book to the starting cash and starts the paper window over. `cst bench` times the admission rule on a fixed fixture and, if a key is set, one Decisions call. It does not write the book.

Copy `.env.example` to `.env` to change the starting bankroll, the 90% bar, or the 10-minute scan. After the first run, learned knobs live in `data/book.sqlite` and survive a restart. The environment values are the seed, and the reset command restores them.

## What the dashboard is showing

The status line stays **Paper** and **Live unavailable**. There is no control that arms a live order. The strip under the thesis is the paper window: day count, net P&L after fees, resolved trades, hit rate, drawdown, and cash still in open clips.

The funnel is the scan: markets read, quotes at the bar, quotes the fee still leaves a profit on, quotes that held still, quotes with a second price, pairs waiting for you, and clips actually bought. The counts are the most active Polymarket events and the first Kalshi pages, not every contract.

You can pause new buys, block a contract, approve a pair (with a note), close a paper clip when the book has size, and tighten a risk knob by one step. You cannot force a buy the fee rule or the pair rule refused. Those changes are listed under "What changed by hand." Mutating requests send the token from the page's own state.

"Closest look" is the buy, or the favorite that came nearest and was still refused, with the cents written out.

The equity line is the paper account, marked at the bid after an exit fee. A new clip usually shows a small dip equal to the spread and the fee. That dip is real friction, not a bug.

The table at the bottom is a model with a known true chance. It is not the live book.

- Buying every 90% quote, when the quote is right, loses money. The loss is the fee, and one defeat wipes many small wins.
- This desk, in that same world, does not trade. Equity stays at the start.
- This desk, when some second venues are a few cents higher, has a positive average.

## Fees

Polymarket's taker fee is `shares × rate × price × (1 − price)`, using the rate on the market (`feeSchedule`). The published category rates are the fallback. Geopolitics is zero. Makers are not our case: a paper buy crosses the spread and pays the taker schedule.

Kalshi's standard taker fee is `0.07 × contracts × price × (1 − price)`, rounded up to the next cent so the paper fill does not understate the cut. Some series use a different multiplier. That multiplier is not in the public market payload, so the standard schedule is the assumption until a later version reads it per series.

## Keys, later

When you are ready to talk about real orders, the names in `.env.example` are the ones to fill:

- OpenAI: `CST_OPENAI_API_KEY`. Optional. It lets the Decisions call drop a duplicate clip and lets the chat note read the tape. The desk still runs without it. The Decisions model is `gpt-6-luna`. The chat model is `CST_OPENAI_MODEL`.
- Kalshi and Polymarket signing keys are unused. This build does not send orders. Adding that is a later reviewed change, after the paper window has something to look at: hit rate, fees, drawdown, and how many clips actually resolved. A month of flat cash is a successful result if the books never offered a fee-adjusted edge.

## Layout

- `src/cst/fees.py` — venue fee math
- `src/cst/venues/` — public Gamma and Kalshi reads
- `src/cst/text.py` — strict match for "same price", looser match for "same risk"
- `src/cst/strategy.py` — the admission rule
- `src/cst/depth.py` — fresh displayed size, read only
- `src/cst/decisions.py` — one drop-only Decisions call per scan
- `src/cst/review.py` — the governor, and the optional chat note
- `src/cst/broker.py` — paper fills, stops, settlement
- `src/cst/store.py` — the book, the audit, the operator overrides
- `src/cst/simulate.py` — the separate model
- `src/cst/engine.py` — the scan loop
- `dashboard/` — the page served at `/`
- `docs/ARCHITECTURE.md` — where to look
- `docs/GAPS.md` — choices still open
- `AGENTS.md` — how to work in this repo

The scan covers the most active Polymarket events and the first pages of open Kalshi markets, excluding multivariate combos. It is a wide net, not a claim to have read every contract on both venues. The funnel shows the counts.

## References

The fee formulas and the public endpoints follow Kalshi's market-data quick start and Polymarket's current fee schedule and Gamma API. The shape of the desk — a venue adapter, a paper broker, and a risk gate in front of any order — is the usual structure of paper-first prediction-market bots. The code here is original.

A favorite can settle at zero. Sizing and the pause line keep that from being the whole account. They do not repeal it.
