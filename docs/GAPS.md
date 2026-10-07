# Gaps

Choices filled in so the desk could run. Argue with these after the flight. None of them are a promise that the book makes money.

- **The confirming price is the settled record, and a new book stays in cash.** A buy needs the Wilson lower bound of that price bucket to clear the ask, the Kalshi fee, and the minimum edge, with at least 30 resolved paper trades in the bucket. A perfect 30-for-30 record is still about 89%, under a 90¢ all-in cost, so the gate does not open. The record is built from this desk's own settlements. Do not buy the quote alone, and do not lower the sample, to get the first trade started.
- **Live sending does not exist.** There is no arm button and no order POST. Adding signed orders, acknowledgements, reconciliation, and a kill path is a later reviewed change. Thirty days of paper is an observation window, not evidence the book won.
- **The Decisions drop threshold is 0.6.** A clip is removed only when the matching answer is `drop` and the probability on `drop` is at least 0.6. Below that, or on any bad body, the deterministic set stands. The model cannot add a market.
- **Overrides only tighten, and the floor is 90%.** One step per click, inside the rails. The automatic governor also moves one step, and only after a settlement that was not already counted. `min_probability` cannot fall below 0.90. The scan clock is not a risk knob.
- **No login.** The desk requires a CSRF token. `Origin` and `Host` have to be localhost or the host passed to `--host`, including on the state read and the snapshot socket. A wildcard bind such as `0.0.0.0` trusts only loopback names. That is not an account system.
- **Kalshi fees use the standard 0.07 schedule, rounded up to the next cent.** Some series use another multiplier. It is not in the public market payload, so those fills may overstate or understate the cut.
- **The scan is not the whole venue.** It is the first pages of open Kalshi markets, and event titles are fetched only for high bids, capped. The funnel says so.
- **A stop can book a small loss.** If the bid falls 8¢ under the entry and the bid has size, the paper clip sells. Marks themselves use the bid minus the exit fee, so a new buy usually shows a dip equal to the spread and the fee.
- **Entries are slower than marks.** New buys are considered every 10 minutes. Open clips are marked about once a minute. A quote can move between scans.
- **Settlement waits for Kalshi `result`.** A clip settles when `result` is `yes` or `no`. A price near 0 or 1 does not settle it. If the flag is missing, the position stays open.
- **A person cannot force a buy** the fee rule, the horizon, the depth check, or the settled record refused.
- **Paper size is the displayed touch.** Kalshi publishes bids, so the other side's bids are the ask. An unfamiliar payload is a refusal, not a fill.
