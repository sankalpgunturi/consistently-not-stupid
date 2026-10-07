"""Command line for the paper desk."""

from __future__ import annotations

import argparse
import json
import logging
from datetime import datetime, timedelta, timezone

import uvicorn

from cst.api import create_app
from cst.config import load_settings
from cst.decisions import veto_proposals
from cst.engine import Engine
from cst.models import BookView
from cst.simulate import run_report
from cst.strategy import evaluate, pair_id


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    parser = argparse.ArgumentParser(prog="cst", description="Consistently Not Stupid")
    sub = parser.add_subparsers(dest="cmd", required=True)

    serve = sub.add_parser("serve", help="Run the paper desk and the dashboard")
    serve.add_argument("--host", default=None)
    serve.add_argument("--port", type=int, default=None)

    sub.add_parser("cycle", help="Read both books once and update the paper account")
    sub.add_parser("simulate", help="Print the model of the rule")
    sub.add_parser("reset", help="Wipe the paper book back to the starting cash")
    sub.add_parser("bench", help="Time evaluate, and one Decisions call if a key is set. Does not touch the book.")

    args = parser.parse_args()
    settings = load_settings()

    if args.cmd == "simulate":
        print(json.dumps(run_report(), indent=2))
        return

    if args.cmd == "bench":
        _bench(settings)
        return

    engine = Engine(settings)
    if args.cmd == "reset":
        engine.reset()
        print(f"Paper book reset to ${float(settings.bankroll):,.2f}.")
        return
    if args.cmd == "cycle":
        state = engine.run_cycle()
        book = state["book"]
        counts = state.get("counts") or {}
        print(
            f"Scan {state['cycle'].get('number')}  "
            f"equity ${book['equity']:,.2f}  "
            f"favorites {counts.get('favorites', 0)}  "
            f"bought {counts.get('bought', 0)}"
        )
        focus = state.get("focus")
        if focus:
            print(focus.get("detail") or focus.get("title"))
        return

    host = args.host or settings.host
    port = args.port or settings.port
    uvicorn.run(create_app(engine, bind_host=host), host=host, port=port, log_level="info")


def _bench(settings) -> None:
    """Print measured seconds. A missing key skips the model call. The book is not opened."""
    import time

    from cst.models import Quote

    now = datetime(2026, 10, 7, 12, tzinfo=timezone.utc)
    cheap = Quote(
        venue="polymarket",
        market_id="bench-poly",
        event_id="bench-poly",
        event_title="Bitcoin on October 7",
        title="Will the price of Bitcoin be above $82,000 on October 7?",
        outcome="Yes",
        side="yes",
        bid=0.93,
        ask=0.94,
        ask_size=20,
        volume=10_000,
        liquidity=20_000,
        end_time=now + timedelta(hours=20),
        category="Crypto",
        fee_model="polymarket",
        fee_rate=0.04,
        fee_exponent=1,
        min_shares=5,
        token_id="bench-token",
    )
    rich = Quote(
        venue="kalshi",
        market_id="KXBTCD-BENCH",
        event_id="KXBTCD-BENCH",
        event_title="Bitcoin on October 7",
        title="Will the price of Bitcoin be above $82,000 on October 7?",
        outcome="Yes",
        side="yes",
        bid=0.97,
        ask=0.98,
        ask_size=20,
        volume=100,
        liquidity=100,
        end_time=now + timedelta(hours=20),
        category="Crypto",
        fee_model="kalshi",
        fee_rate=0.07,
        fee_exponent=1,
        min_shares=1,
    )
    params = settings.seed_params()
    params.min_stable_scans = 1
    book = BookView(
        equity=1000,
        cash=1000,
        peak=1000,
        deployed=0,
        streaks={cheap.key: 2, rich.key: 2},
        approved_pairs={pair_id(cheap.key, rich.key)},
    )
    started = time.perf_counter()
    result = evaluate([cheap, rich], params, book, now=now)
    evaluate_seconds = time.perf_counter() - started
    print(f"evaluate_seconds {evaluate_seconds:.6f}")
    print(f"evaluate_proposals {len(result.proposals)}")
    if not settings.openai_api_key.strip():
        print("decisions_seconds skipped")
        return
    started = time.perf_counter()
    veto_proposals(result.proposals, settings.openai_api_key)
    print(f"decisions_seconds {time.perf_counter() - started:.6f}")


if __name__ == "__main__":
    main()
