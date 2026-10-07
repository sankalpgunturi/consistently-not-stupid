# Phases

Living log for Consistently Not Stupid. Update this file at the end of each phase. Do not treat it as a design spec; the spec is `docs/ARCHITECTURE.md` once that exists. Gaps the operator should argue with are in `docs/GAPS.md`.

## Intent

A Kalshi paper desk. Polymarket was removed; do not add it back, and do not buy a favorite because the quote is high. Month one runs on a paper account of $1,000 so we can see if the rule survives fees. Live sending is unavailable; adding and arming it requires a later reviewed change after the paper observation window. The deterministic rule decides. A model may only remove a buy, never add one. A buy needs the settled record to clear the all-in cost. That record is the latest trade at least `min_hours_to_expiry` before close, not a bootstrap of unconfirmed clips.

## Phase 1 — Plan

Status: revised after review. See Phase 2. Phase 7 later removed Polymarket and pair approval. The list below is the plan that was executed then, not a reason to put either back.

### What already exists

A paper scan loop, fee math, a 90% book filter, title-similarity cross-venue edge, correlation, a governor that can loosen knobs, a dashboard, and a separate model that shows why buying every favorite loses the fee. No order is sent. Tests pass (22).

### What this pass will add

Keep the deterministic core. The model may only remove a buy.

1. **Cross-venue buys need a human equivalence.** Title similarity is not settlement equivalence. A similar pair is shown on the tape as a candidate. It becomes a buy only after the operator marks that pair as the same contract, including a note that they checked the resolution rules. The learned-record path is unchanged and does not need a pair. Unapproved pairs never fill.

2. **Paper fills have to be executable.** A buy or a manual close fetches the venue top of book (Kalshi orderbook, Polymarket CLOB book) and refuses unless the displayed size covers the clip. Exit fees stay in the fill. Settlement uses Kalshi `result` when it is `yes` or `no`. Polymarket settles only when the market is closed and the venue marks it resolved (UMA status or an equivalent final flag), not from a live price that happens to sit near 0 or 1. If that flag is missing, the position stays open.

3. **Paper month is evidence, not a live switch.** `paper_started_at` is set once and cleared only by `cst reset`. The dashboard shows age, net P&L after fees, resolved count, hit rate, drawdown, unresolved cost, and venue errors. There is no Arm button. The status line is `PAPER` and `LIVE UNAVAILABLE` until a later change adds signed orders, acknowledgements, reconciliation, and a kill path, and that change is reviewed. Thirty days of paper is the observation window, not a claim that the book made money.

4. **Overrides only tighten.** Each knob has a tightening direction. Humans and the governor may move one step that way, inside the rail. The other direction is rejected and tested. `min_probability` cannot fall below 0.90. Every accepted change is appended with the old value, the new value, the time, and the reason. A person can pause new buys, block a market, and close a paper clip. A person cannot force a buy the fee rule or the equivalence rule refused.

5. **Local mutations carry a token.** Mutating routes require header `X-CSRF-Token` matching the token from `GET /api/state`, and they reject a foreign `Origin`. The server still binds to localhost. No accounts.

6. **Decisions API, one call per scan, drop only.** `POST /v1/decisions`, model `gpt-6-luna`, one `choice` question per proposed clip (`keep` or `drop`), answered by clip id. A clip is removed only when the matching answer is `drop`, the probability on `drop` is at least 0.6, and the id is one of the proposed clips. Duplicate, unknown, refused, or malformed answers are ignored. Timeout, missing key, or a bad body leaves the deterministic set. An outage fails open only for this veto. Fee, size, horizon, and equivalence gates still apply. Chat completions remain the after-scan note, and that note cannot loosen a knob.

7. **Benchmark, off the trading path.** `cst bench` times deterministic `evaluate` on a fixed fixture and, if a key is set, one Decisions call. It prints the numbers. It does not invent a latency and it does not change the book.

8. **Agent map.** Short root `AGENTS.md`. `docs/ARCHITECTURE.md`. `docs/GAPS.md` for the choices to revisit after the flight.

9. **Omit.** No matching engine, no colocation, no second strategy, no per-market model call, no model-originated buys, no live signing or order POST, no login. High-frequency practice we keep: inventory caps, a kill switch, executable size, fee-aware edge, and a faster mark loop than the entry loop. The decision clock stays 10 minutes.

### Acceptance

- Tests: unapproved cross-venue pair does not fill; approved pair can; thin book does not fill; Polymarket does not settle on price alone; loosening a knob is rejected; CSRF header is required; Decisions veto cannot add a clip and a bad body does not drop the set; pause and block stick.
- `pytest` passes.
- Dashboard shows `PAPER`, `LIVE UNAVAILABLE`, paper age, the evidence counts, pause, block, close, and pair approval.
- A scan with no key still follows the deterministic set.

## Phase 2 — Plan review

Status: approved. The first pass returned REVISE (cross-venue rules, executable paper fills, a meaningless 30-day live gate, loosening overrides, CSRF, and a live mode with no wire). The list under "What this pass will add" is the revised plan that was approved.

## Phase 3 — Code

Status: coded against that plan. `pytest` is green. Independent review is Phase 4.

## Phase 4 — Code review

Status: approved. The first pass returned REVISE: raising `correlation_threshold` was treated as tightening, but a higher twin bar lets more overlaps through. It now lives in `LOOSEN_UP`, so a human or the governor can only step it down. The second pass approved.

## Phase 5 — Test

Status: done. `pytest` is 32 passed. The dashboard was opened at http://127.0.0.1:8000 against a live public read.

The first browser pass paused, resumed, tightened minimum probability from 90% to 91%, scanned, and blocked a single contract. Cash stayed at $1,000. There is no arm control. Two defects showed up and were fixed before the pass was accepted:

- The page socket 404'd because uvicorn was installed without a WebSocket library. The dependency is now `uvicorn[standard]`, and a test reads one snapshot off `/ws`.
- A collapsed tape row offered Block, but the click only stored one sample key. Grouped rows no longer show that button.

A second pass showed Paper, Live unavailable, and Connected, then pause and resume.

The live scan had favorites and none with a second price, so the Approve pair button was not on the tape. The unit tests cover an unapproved pair and an approved one.

## Phase 6 — Retrospective

Status: done for this pass.

The rule did what it was built to do on a real book: hundreds of quotes sat at the bar, the fee still left a cent on about a hundred of them, and zero were bought, because none had a confirmed second price. Cash stayed at the start. That is not a proof the account cannot lose. It is a proof the desk sits out when the only edge is the fee.

Blind spots that were real, and what was done:

- A missing WebSocket extra made the page look disconnected while the buttons still worked. Fixed and tested.
- Block on a grouped row over-claimed. The button is only on a single contract now.
- Raising the twin bar was classified as tightening. Review caught it. The direction is corrected and tested.

A later pass closed three timing holes. A pause or a block that arrives while the scan is still reading the book is checked again immediately before each fill. A stop can fire on a mark in the same cycle the clip was opened. `/ws` closes a foreign `Origin` before it accepts, so the snapshot and the token stay on localhost.

Left open at the time, and written in `docs/GAPS.md`: no live wire, the 0.6 drop threshold, tighten-only with a 90% floor, no login, the standard Kalshi fee, a scan that is not the whole venue, and stops that can book a small loss. None of those are patched by pretending the book has an edge.

## Phase 7 — Kalshi only

Status: done. Polymarket is out of the repo: the Gamma reader, the CLOB depth check, the fee schedule, the pair-approval route, and the dashboard control. The desk reads Kalshi. A buy still needs the settled record. Cross-venue pair approval was the only other confirmation, and it cannot fire with one venue, so it is gone from the strategy and the page. An old sqlite file may still contain `approved_pairs`; the scan does not read it.

Cold start was real at the end of this phase: the Wilson lower bound of a perfect 30-for-30 record is about 89%, which does not clear a 90¢ ask plus the Kalshi fee, and the only sample was the desk's own fills. Phase 8 seeds that sample from Kalshi instead of leaving the gate shut.

## Phase 8 — A record the desk can actually use

Status: done. The entry sample is the first pages of settled Kalshi markets, plus this desk's own resolutions. A price of 0 or 1 is not a quote. The desk does not buy extra clips to build the sample. The governor and a model suggestion both wait for a new settlement of ours before they tighten. A missing Kalshi bid on a live market is not a quoted zero. A tighten during the fetch applies to that scan's fills. An unbracketed IPv6 bind matches the bracketed Host a browser sends. Pair-approval fields are gone from the snapshot and the decision. Phase 9 replaced the last-print price with the trade taken while time was still left.

## Phase 9 — The sample matches the gate

Status: done. The gate asks whether a favorite quoted with at least `min_hours_to_expiry` still to run usually won. The seed is the latest trade at least that far before close. A final print is not a sample, and a market with no such trade is stored as checked and left out of the counts. Each scan reads at most 40 of those trades; the rest wait. A ticker this desk has settled is counted once, from the desk's own settlement. A settled market whose bid and ask are missing still marks a held clip from `result`. The Wilson gate is unchanged, and a bucket under 30 observations stays in cash.

## Phase 10 — The record survives a tighter horizon

Status: done. Trade prints are stored from the one-hour rail back through the current horizon, and a tighten scores those prints again. A trade counts at the new horizon only when its timestamp still clears it. Pagination walks back until the cutoff, so a single page of recent trades is not treated as coverage. A missing field is not pinned. A failed trade read counts toward the 40-attempt cap and the scan says so. Reset clears the paper book and keeps the venue prints. An older Polymarket clip is closed at cost on open, so the dashboard can still mark the book. A fill re-checks the horizon, the fee, the streak, the caps, and the current record before cash moves.
