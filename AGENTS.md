# Consistently Not Stupid

Paper desk for high-probability Kalshi contracts. The aim is to be consistently not stupid, instead of trying to be very intelligent. Live orders are sent only after the operator approves $1 to $5,000 on the dashboard. The import package and the `cst` command stay `cst`.

## Commands

Production paper engine: `chitti-vps`, `/opt/cst`, systemd units `cst-paper` and `cst-dashboard`. The authoritative ledger is `/opt/cst/data/book.sqlite` on that server. The Mac ledger is an archived migration copy; its launch agents are disabled. Do not restart it. Shared dashboard: https://consistently-not-stupid.sgunturi.workers.dev. See `cloudflare/README.md` for deployment and recovery.

- Tests: `.venv/bin/pytest`
- Dashboard: `.venv/bin/cst serve` then http://127.0.0.1:8000
- One scan: `.venv/bin/cst cycle`
- Timing only (does not open the book): `.venv/bin/cst bench`

## Look here

- Admission rule: `src/cst/strategy.py`
- Map of the process: `docs/ARCHITECTURE.md`
- Choices still open: `docs/GAPS.md`
- What changed by phase: `docs/PHASES.md`
- Local rules for the package: `src/cst/AGENTS.md`

## Do not

- Send an order while the book is in paper mode, or approve live trading outside $1 to $5,000. Signed orders live in `src/cst/live.py` and use `CST_KALSHI_API_KEY_ID` and `CST_KALSHI_PRIVATE_KEY_PATH`.
- Let a model add a buy. It may only drop one the deterministic rule already proposed.
- Automatic reviews may only tighten one step inside `RAILS`, excluding the operator-selected dashboard controls in short-window mode. Reviews may suggest changes to those controls but must not apply them. The operator may directly set the dashboard controls: outcome window (1–60 minutes), entry probability (80–99%), scan interval (1–60 seconds), amount per bet ($1–$100 including fees), and stop loss (Off or the final 1–60 minutes, with an absolute exit probability of 1–99%). These explicit user controls supersede the former 90% floor and tighten-only UI.
- The ten-minute paper experiment admits quoted favorites without a historical-evidence gate, as explicitly requested by the user. Retain prospective observations for analysis, not admission. Legacy replay keeps its historical rule. Preserve probability, timing, fee, liquidity, stability, exposure and execution checks.
- Treat a similar title as a reason to buy. Twins inside Kalshi are skipped by the correlation check.
- Add a mutating `/api/*` route that skips `X-CSRF-Token`. Reject a foreign `Origin` on those routes and on `/ws` before the socket is accepted.

- The short-window strategy holds to the official outcome when stop loss is Off (the default). If the operator enables it, our side’s bid below the selected absolute probability triggers a depth-checked exit during the selected final minutes before trading close (or expected outcome if earlier). Manual close remains available; legacy replay retains its stop rule.

- User-approved timing: supported fixed-interval price contracts enter relative to the event cutoff at trading close, not the later settlement estimate. Other markets retain estimated-outcome timing; do not assume every close is an event cutoff.
