# Consistently not Stupid

Paper desk for high-probability Kalshi and Polymarket contracts. The aim is to be consistently not stupid, instead of trying to be very intelligent. Live orders are not implemented. The import package and the `cst` command stay `cst`.

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

- Add an order POST, a signing path, or an arm control. Public reads and the CLOB `/book` read are the only venue calls.
- Let a model add a buy. It may only drop one the deterministic rule already proposed.
- Loosen a knob, or move `min_probability` below 0.90. Overrides tighten one step inside `RAILS`.
- Treat a similar title as settlement equivalence. A cross-venue buy needs `approved_pairs`.
- Add a mutating `/api/*` route that skips `X-CSRF-Token`. Reject a foreign `Origin` on those routes and on `/ws` before the socket is accepted.
