# Consistently Not Stupid

Paper desk for high-probability Kalshi contracts. The aim is to be consistently not stupid, instead of trying to be very intelligent. Live orders are sent only after the operator approves $1 to $5,000 on the dashboard. The import package and the `cst` command stay `cst`.

## Commands

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
- Loosen a knob, or move `min_probability` below 0.90. Overrides tighten one step inside `RAILS`.
- The ten-minute paper experiment admits quoted favorites without a historical-evidence gate, as explicitly requested by the user. Retain prospective observations for analysis, not admission. Legacy replay keeps its historical rule. Preserve probability, timing, fee, liquidity, stability, exposure and execution checks.
- Treat a similar title as a reason to buy. Twins inside Kalshi are skipped by the correlation check.
- Add a mutating `/api/*` route that skips `X-CSRF-Token`. Reject a foreign `Origin` on those routes and on `/ws` before the socket is accepted.

- The ten-minute paper strategy holds to the official outcome. No automatic price-drop stop-loss exits. Manual close remains available; legacy replay retains its stop rule.
