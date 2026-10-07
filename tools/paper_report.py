"""Read the paper ledger for a retrospective without interrupting the runner."""

import argparse
import json
from pathlib import Path
import sqlite3


def report(path: Path) -> dict:
    with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN")

        def meta(key, default=None):
            row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
            return json.loads(row[0]) if row else default

        first = conn.execute("SELECT equity FROM equity ORDER BY id LIMIT 1").fetchone()
        start = first[0] if first else None
        trades = [dict(row) for row in conn.execute("SELECT * FROM trades ORDER BY ts, rowid")]
        closed = [row for row in trades if row["action"] in {"sell", "settle"}]
        realized = sum(row["pnl"] for row in closed)
        fees = sum(row["fee"] for row in trades)
        cash = meta("cash")
        expected_cash = start
        if start is not None:
            for trade in trades:
                value = trade["shares"] * trade["price"]
                expected_cash += -value - trade["fee"] if trade["action"] == "buy" else value - trade["fee"]
        latest = conn.execute("SELECT ts, equity FROM equity ORDER BY id DESC LIMIT 1").fetchone()
        reviews = [dict(ts=row["ts"], **json.loads(row["payload"])) for row in conn.execute("SELECT ts,payload FROM retrospectives ORDER BY id DESC LIMIT 12")]
        scans = [json.loads(row[0]) for row in conn.execute("SELECT payload FROM scans ORDER BY cycle DESC LIMIT 12")]
        return {
            "mode": "paper",
            "operating_costs_included": False,
            "model_usage_note": "Provider usage is archived with each scan where available; unpriced API costs are not deducted from the trading ledger.",
            "started_at": meta("paper_started_at"),
            "starting_cash": start,
            "cash": cash,
            "last_mark": dict(latest) if latest else None,
            "realized_pnl": realized,
            "fees": fees,
            "opened_trades": sum(row["action"] == "buy" for row in trades),
            "closed_trades": len(closed),
            "settlements": sum(row["action"] == "settle" for row in trades),
            "stopped_or_manually_closed": sum(row["action"] == "sell" for row in trades),
            "ledger_checks": {
                "cash_matches_trades": expected_cash is not None and abs(expected_cash - cash) < 1e-5,
                "fees_match_trades": abs(fees - meta("fees_paid", 0)) < 1e-5,
                "realized_matches_trades": abs(realized - meta("realized", 0)) < 1e-5,
            },
            "open_positions": [dict(row) for row in conn.execute("SELECT * FROM positions")],
            "recent_trades": trades[-50:],
            "latest_scans": scans,
            "latest_reviews": reviews,
            "daily_evaluations": [json.loads(row[0]) for row in conn.execute("SELECT payload FROM daily_reviews ORDER BY day DESC LIMIT 30")] if conn.execute("SELECT 1 FROM sqlite_master WHERE name='daily_reviews'").fetchone() else [],
            "changes": [dict(row) for row in conn.execute("SELECT * FROM audit ORDER BY id DESC LIMIT 30")],
        }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=Path(__file__).resolve().parents[1] / "data/book.sqlite")
    args = parser.parse_args()
    print(json.dumps(report(args.db), indent=2))
