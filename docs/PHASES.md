# Phases

Living log for The Common Sense Trade. Update this file at the end of each phase. Do not treat it as a design spec; the spec is `docs/ARCHITECTURE.md` once that exists. Gaps the operator should argue with are in `docs/GAPS.md`.

## Intent

A real Kalshi and Polymarket desk. Month one runs on a paper account of $1,000 so we can see if the rule survives fees. Live sending is unavailable; adding and arming it requires a later reviewed change after the paper observation window. The deterministic rule decides. A model may only remove a buy, never add one.

## Phase 1 — Plan

Status: revised after review. See Phase 2.

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

Left open, on purpose, and written in `docs/GAPS.md`: human pair approval, no live wire, the 0.6 drop threshold, tighten-only with a 90% floor, no login, the standard Kalshi fee, a scan that is not the whole venue, stops that can book a small loss, and Polymarket settlement that waits for UMA. None of those are patched by pretending the book has an edge.
