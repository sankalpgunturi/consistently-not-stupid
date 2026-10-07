# Gaps

Choices filled in so the desk could run. Argue with these after the flight. None of them are a promise that the book makes money.

- **A person confirms cross-venue pairs.** Title similarity only raises a candidate. Two markets can share a title and still resolve on different rules. The learned-record path does not need a pair.
- **Live sending does not exist.** There is no arm button and no order POST. Adding signed orders, acknowledgements, reconciliation, and a kill path is a later reviewed change. Thirty days of paper is an observation window, not evidence the book won.
- **The Decisions drop threshold is 0.6.** A clip is removed only when the matching answer is `drop` and the probability on `drop` is at least 0.6. Below that, or on any bad body, the deterministic set stands. The model cannot add a market.
- **Overrides only tighten, and the floor is 90%.** One step per click, inside the rails. `min_probability` cannot fall below 0.90. The scan clock is not a risk knob.
- **No login.** The desk requires a CSRF token. When a browser sends `Origin`, it has to be localhost or the same host the request is addressed to, so `--host` can be an interface other than localhost. That is not an account system.
- **Kalshi fees use the standard 0.07 schedule, rounded up to the next cent.** Some series use another multiplier. It is not in the public market payload, so those fills may overstate or understate the cut.
- **The scan is not the whole venue.** Polymarket is the most active events, a few pages. Kalshi is the first pages of open markets, and event titles are fetched only for high bids, capped. The funnel says so.
- **A stop can book a small loss.** If the bid falls 8¢ under the entry and the bid has size, the paper clip sells. Marks themselves use the bid minus the exit fee, so a new buy usually shows a dip equal to the spread and the fee.
- **Entries are slower than marks.** New buys are considered every 10 minutes. Open clips are marked about once a minute. A quote can move between scans.
- **Polymarket settlement waits for UMA.** `closed` and `umaResolutionStatus == "resolved"`, with outcome prices at the extremes. A price near 0 or 1, including `["0","0"]`, does not settle the paper clip. Kalshi settles when `result` is `yes` or `no`.
- **A person cannot force a buy** the fee rule, the horizon, the depth check, or the pair rule refused.
- **Paper size is the displayed touch.** Polymarket sums CLOB size at or better than the limit. Kalshi treats the other side's bids as the ask. An unfamiliar payload is a refusal, not a fill.
