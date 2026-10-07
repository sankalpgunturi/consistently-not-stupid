"""SQLite book. The paper account, the tape, and the learned knobs live here."""

from __future__ import annotations

import json
import secrets
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from contextlib import contextmanager

from cst.models import BookView, Decision, Position, Settlement, StrategyParams, Trade, utcnow
from cst.strategy import price_bucket
from cst.venues.kalshi import _covers_cutoff, _inclusive_max_ts, settled_favorite, normalize_market_url


def _iso(dt: datetime | None = None) -> str:
    return (dt or utcnow()).astimezone(timezone.utc).isoformat(timespec="seconds")


def _hours_key(hours: float) -> float:
    return round(float(hours), 4)


class Store:
    def __init__(self, path: Path, params: StrategyParams, bankroll: float):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.bankroll = float(bankroll)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self._transaction_depth = 0
        self._init()
        self._seed(params)
        self._ensure_controls()

    def _commit(self) -> None:
        if self._transaction_depth == 0:
            self.conn.commit()

    @contextmanager
    def transaction(self):
        """Commit a fill and all its accounting together, or roll it all back."""
        with self.lock:
            outer = self._transaction_depth == 0
            if outer:
                self.conn.execute("BEGIN IMMEDIATE")
            self._transaction_depth += 1
            try:
                yield
                if outer:
                    self.conn.commit()
            except BaseException:
                if outer:
                    self.conn.rollback()
                raise
            finally:
                self._transaction_depth -= 1

    def _init(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS positions (
                id TEXT PRIMARY KEY,
                venue TEXT, market_id TEXT, event_id TEXT, event_title TEXT,
                title TEXT, outcome TEXT, side TEXT, category TEXT,
                shares REAL, entry_price REAL, cost_basis REAL, fees REAL,
                signal TEXT, reason TEXT, opened_cycle INTEGER, bid REAL,
                end_time TEXT, fee_model TEXT, fee_rate REAL, fee_exponent REAL,
                url TEXT, opened_at TEXT
            );
            CREATE TABLE IF NOT EXISTS trades (
                id TEXT PRIMARY KEY, ts TEXT, venue TEXT, market_id TEXT,
                title TEXT, outcome TEXT, action TEXT, shares REAL, price REAL,
                fee REAL, pnl REAL, cash_after REAL, signal TEXT, reason TEXT, won INTEGER
            );
            CREATE TABLE IF NOT EXISTS decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                cycle INTEGER, payload TEXT
            );
            CREATE TABLE IF NOT EXISTS equity (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT, equity REAL
            );
            CREATE TABLE IF NOT EXISTS retrospectives (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT, payload TEXT
            );
            CREATE TABLE IF NOT EXISTS daily_reviews (day INTEGER PRIMARY KEY, payload TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS retro_cycle ON retrospectives(json_extract(payload, '$.cycle'));
            CREATE INDEX IF NOT EXISTS bought_decisions ON decisions(cycle) WHERE json_extract(payload, '$.action') = 'bought';
            CREATE TABLE IF NOT EXISTS scans (
                cycle INTEGER PRIMARY KEY, ts TEXT NOT NULL, payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS position_quotes (
                id INTEGER PRIMARY KEY, ts TEXT NOT NULL, position_id TEXT NOT NULL, payload TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS position_quote_lookup ON position_quotes(position_id, ts);
            CREATE TABLE IF NOT EXISTS observations (
                cycle INTEGER NOT NULL, quote_key TEXT NOT NULL, payload TEXT NOT NULL,
                PRIMARY KEY (cycle, quote_key)
            );
            CREATE TABLE IF NOT EXISTS near_observations (
                event_id TEXT PRIMARY KEY, ticker TEXT UNIQUE NOT NULL,
                side TEXT NOT NULL, observed_at TEXT NOT NULL,
                expected_at TEXT NOT NULL, minutes_left REAL NOT NULL,
                price REAL NOT NULL, fee REAL NOT NULL, bucket TEXT NOT NULL,
                result TEXT, resolved_at TEXT, checked_at TEXT, payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS stability (
                key TEXT PRIMARY KEY,
                streak INTEGER,
                last_cycle INTEGER
            );
            CREATE TABLE IF NOT EXISTS settlements (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT, bucket TEXT, won INTEGER, price REAL,
                fee_per_share REAL, pnl REAL, market_id TEXT
            );
            CREATE TABLE IF NOT EXISTS audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT, actor TEXT, action TEXT,
                old_value TEXT, new_value TEXT, reason TEXT
            );
            CREATE TABLE IF NOT EXISTS venue_markets (
                ticker TEXT PRIMARY KEY,
                result TEXT NOT NULL,
                close_ts REAL NOT NULL,
                volume REAL NOT NULL,
                covered_until REAL NOT NULL,
                shallow_ts REAL NOT NULL,
                exhausted INTEGER NOT NULL,
                resume_cursor TEXT NOT NULL DEFAULT '',
                resume_max_ts INTEGER
            );
            CREATE TABLE IF NOT EXISTS venue_trades (
                ticker TEXT NOT NULL,
                trade_key TEXT NOT NULL,
                traded_at REAL NOT NULL,
                yes_price REAL NOT NULL,
                PRIMARY KEY (ticker, trade_key)
            );
            CREATE TABLE IF NOT EXISTS venue_samples (
                ticker TEXT NOT NULL,
                hours REAL NOT NULL,
                price REAL,
                won INTEGER,
                bucket TEXT,
                PRIMARY KEY (ticker, hours)
            );
            """
        )
        trade_columns = {row[1] for row in self.conn.execute("PRAGMA table_info(trades)")}
        if "ledger" not in trade_columns:
            self.conn.execute("ALTER TABLE trades ADD COLUMN ledger TEXT NOT NULL DEFAULT 'paper'")
        position_columns = {row[1] for row in self.conn.execute("PRAGMA table_info(positions)")}
        if "ledger" not in position_columns:
            self.conn.execute("ALTER TABLE positions ADD COLUMN ledger TEXT NOT NULL DEFAULT 'paper'")
        equity_columns = {row[1] for row in self.conn.execute("PRAGMA table_info(equity)")}
        if "ledger" not in equity_columns:
            self.conn.execute("ALTER TABLE equity ADD COLUMN ledger TEXT NOT NULL DEFAULT 'paper'")
        if "side" not in trade_columns:
            self.conn.execute("ALTER TABLE trades ADD COLUMN side TEXT NOT NULL DEFAULT ''")
            # Recover old picks only from archived positions matching entry time.
            picks = {}
            for row in self.conn.execute("SELECT payload FROM scans"):
                for position in json.loads(row[0]).get("book_before", {}).get("positions", []):
                    picks[(position["venue"], position["market_id"], position.get("opened_at"))] = position["side"]
            held = {}
            for row in self.conn.execute("SELECT * FROM trades ORDER BY ts, rowid").fetchall():
                key = (row["venue"], row["market_id"])
                if row["action"] == "buy":
                    held[key] = picks.get((*key, row["ts"]), "")
                side = held.get(key, "")
                if side:
                    self.conn.execute("UPDATE trades SET side=? WHERE id=?", (side, row["id"]))
                if row["action"] != "buy":
                    held.pop(key, None)
        columns = {row[1] for row in self.conn.execute("PRAGMA table_info(settlements)")}
        if "market_id" not in columns:
            self.conn.execute("ALTER TABLE settlements ADD COLUMN market_id TEXT")
        market_columns = {row[1] for row in self.conn.execute("PRAGMA table_info(venue_markets)")}
        if "resume_cursor" not in market_columns:
            self.conn.execute("ALTER TABLE venue_markets ADD COLUMN resume_cursor TEXT NOT NULL DEFAULT ''")
        if "resume_max_ts" not in market_columns:
            self.conn.execute("ALTER TABLE venue_markets ADD COLUMN resume_max_ts INTEGER")
        self._reconcile_foreign_positions()
        self._migrate_legacy_history()
        self._commit()

    def _reconcile_foreign_positions(self) -> None:
        """Close clips from a venue this desk no longer marks.

        Cash returns the cost basis. The fee already paid stays in the fee
        total. Marking the old model would raise and take the dashboard down.
        """
        rows = self.conn.execute(
            """
            SELECT * FROM positions
            WHERE venue != 'kalshi' OR IFNULL(fee_model, '') != 'kalshi'
            """
        ).fetchall()
        for row in rows:
            cost = float(row["cost_basis"] or 0)
            cash = round(float(self._get("cash") or 0) + cost, 6)
            self._put("cash", cash)
            self.conn.execute(
                """
                INSERT INTO trades (
                    id, ts, venue, market_id, title, outcome, action, shares, price,
                    fee, pnl, cash_after, signal, reason, won
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid.uuid4()),
                    _iso(),
                    row["venue"],
                    row["market_id"],
                    row["title"],
                    row["outcome"],
                    "reconcile",
                    row["shares"],
                    row["entry_price"],
                    0,
                    0,
                    cash,
                    row["signal"] or "",
                    "This venue was removed. The clip was closed at cost and the cash was returned.",
                    None,
                ),
            )
            self.conn.execute("DELETE FROM positions WHERE id = ?", (row["id"],))
            self._audit(
                "migration",
                "drop-venue",
                row["venue"],
                row["market_id"],
                "This venue was removed. The open clip was closed at cost and the cash was returned.",
            )

    def _migrate_legacy_history(self) -> None:
        """Move the old JSON sample onto rows that remember their horizon.

        The previous aggregate is a bucket total with no horizon. Using it
        after a tighter nearest-expiry would admit a two-hour observation at
        three hours. Samples that name their horizon are kept at that horizon.
        An aggregate that does not is dropped.
        """
        if self._get("legacy_history_migrated"):
            return
        raw = self._get("venue_samples")
        migrated = False
        if isinstance(raw, dict):
            for ticker, row in raw.items():
                if isinstance(row, dict) and "hours" in row:
                    self._merge_sample(str(ticker), row)
                    migrated = True
            self._put("venue_samples", {})
        if migrated or not self._has_samples():
            self._put("venue_record", {})
        self._put("legacy_history_migrated", True)

    def _seed(self, params: StrategyParams) -> None:
        with self.lock:
            row = self.conn.execute("SELECT value FROM meta WHERE key = 'cash'").fetchone()
            if row is None:
                self._put("cash", self.bankroll)
                self._put("peak", self.bankroll)
                self._put("realized", 0.0)
                self._put("fees_paid", 0.0)
                self._put("cycle", 0)
                self._put("params", params.to_json())
                self._put("status", "starting")
                self._put("paper_started_at", _iso())
                self._put("csrf", secrets.token_urlsafe(24))
                self._put("blocked", [])
                self._put("operator_pause", False)
                self.conn.execute(
                    "INSERT INTO equity (ts, equity, ledger) VALUES (?, ?, 'paper')",
                    (_iso(), self.bankroll),
                )
                self._commit()

    def _ensure_controls(self) -> None:
        with self.lock:
            if not self._get("paper_started_at"):
                self._put("paper_started_at", _iso())
            if not self._get("csrf"):
                self._put("csrf", secrets.token_urlsafe(24))
            if self._get("blocked") is None:
                self._put("blocked", [])
            if self._get("operator_pause") is None:
                self._put("operator_pause", False)
            self._commit()

    def _put(self, key: str, value) -> None:
        self.conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, json.dumps(value)),
        )

    def _get(self, key: str, default=None):
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        if row is None:
            return default
        return json.loads(row["value"])

    def record_position_quote(self, position_id, quote, reason, depth, execution_block=None):
        payload = {"market_id": quote.market_id, "side": quote.side,
                   "bid": quote.bid, "ask": quote.ask, "bid_size": quote.bid_size,
                   "close": quote.end_time.isoformat() if quote.end_time else None,
                   "tradable": quote.tradable, "stop_trigger": reason, "execution_block": execution_block,
                   "depth": None if depth is None else {"ok": depth.ok, "size": depth.size, "detail": depth.detail}}
        with self.lock:
            self.conn.execute("INSERT INTO position_quotes(ts, position_id, payload) VALUES (?, ?, ?)",
                              (_iso(), position_id, json.dumps(payload)))
            self._commit()

    def params(self) -> StrategyParams:
        with self.lock:
            raw = self._get("params", {})
        return StrategyParams.from_json(raw or {})

    def save_params(self, params: StrategyParams) -> None:
        with self.lock:
            self._put("params", params.to_json())
            self._commit()

    def revise_params(self, revise):
        """Read, revise, and write params under one lock.

        revise(current) returns (updated, notes, applied) and must not touch
        the store. An empty applied map does not write, so a scan that has
        nothing to tighten cannot put an older copy back over an operator click.
        """
        with self.lock:
            current = StrategyParams.from_json(self._get("params") or {})
            updated, notes, applied = revise(current)
            if applied:
                self._put("params", updated.to_json())
                self._commit()
            return updated, notes, applied

    def governor_seen(self) -> int:
        with self.lock:
            return int(self._get("governor_seen", 0) or 0)

    def set_governor_seen(self, count: int) -> None:
        with self.lock:
            self._put("governor_seen", int(count))
            self._commit()

    def trading_mode(self) -> str:
        with self.lock:
            return "live" if self._get("mode") == "live" else "paper"

    def live_budget(self) -> float | None:
        with self.lock:
            amount = self._get("live_budget")
        return float(amount) if amount is not None else None

    def exchange_balance(self) -> float | None:
        with self.lock:
            amount = self._get("live_exchange_balance")
        return float(amount) if amount is not None else None

    def arm_live(self, dollars: int, exchange_balance: float | None) -> None:
        """Switch the working book to the approved dollars. The paper ledger stays stored."""
        with self.lock:
            self._put("mode", "live")
            self._put("live_budget", int(dollars))
            self._put("live_cash", float(dollars))
            self._put("live_peak", float(dollars))
            self._put("live_realized", 0.0)
            self._put("live_fees_paid", 0.0)
            self._put("live_started_at", _iso())
            if exchange_balance is not None:
                self._put("live_exchange_balance", round(float(exchange_balance), 2))
            self._audit(
                "operator",
                "live",
                "paper",
                {"amount": int(dollars)},
                f"The operator approved live trading for ${int(dollars):,}.",
            )
            self._commit()

    def _ledger(self) -> str:
        return "live" if self._get("mode") == "live" else "paper"

    def _account_key(self, name: str) -> str:
        if self._get("mode") != "live":
            return name
        return {"cash": "live_cash", "peak": "live_peak", "realized": "live_realized", "fees_paid": "live_fees_paid"}[name]

    def cash(self) -> float:
        with self.lock:
            if self._get("mode") == "live":
                return float(self._get("live_cash", 0))
            return float(self._get("cash", self.bankroll))

    def set_cash(self, cash: float) -> None:
        with self.lock:
            self._put(self._account_key("cash"), round(float(cash), 6))
            self._commit()

    def add_fee(self, fee: float) -> None:
        with self.lock:
            key = self._account_key("fees_paid")
            self._put(key, round(float(self._get(key, 0)) + fee, 6))
            self._commit()

    def fees_paid(self) -> float:
        with self.lock:
            return float(self._get(self._account_key("fees_paid"), 0))

    def realized(self) -> float:
        with self.lock:
            return float(self._get(self._account_key("realized"), 0))

    def add_realized(self, pnl: float) -> None:
        with self.lock:
            key = self._account_key("realized")
            self._put(key, round(float(self._get(key, 0)) + pnl, 6))
            self._commit()

    def peak(self) -> float:
        with self.lock:
            default = self._get("live_budget", 0) if self._get("mode") == "live" else self.bankroll
            return float(self._get(self._account_key("peak"), default))

    def note_peak(self, equity: float) -> float:
        with self.lock:
            key = self._account_key("peak")
            default = self._get("live_budget", 0) if self._get("mode") == "live" else self.bankroll
            peak = float(self._get(key, default))
            if equity > peak:
                peak = equity
                self._put(key, round(peak, 6))
                self._commit()
            return peak

    def next_cycle(self) -> int:
        with self.lock:
            cycle = int(self._get("cycle", 0)) + 1
            self._put("cycle", cycle)
            self._commit()
            return cycle

    def cycle(self) -> int:
        with self.lock:
            return int(self._get("cycle", 0))

    def set_status(self, status: str, extra: dict | None = None) -> None:
        with self.lock:
            self._put("status", status)
            if extra is not None:
                self._put("cycle_info", extra)
            self._commit()

    def status(self) -> str:
        with self.lock:
            return str(self._get("status", "starting"))

    def cycle_info(self) -> dict:
        with self.lock:
            return self._get("cycle_info", {}) or {}

    def observe(self, cycle: int, keys: list[str]) -> dict[str, int]:
        with self.lock:
            rows = {
                row["key"]: row
                for row in self.conn.execute("SELECT key, streak, last_cycle FROM stability")
            }
            streaks = {}
            for key in set(keys):
                prev = rows.get(key)
                if prev is not None and int(prev["last_cycle"]) == cycle - 1:
                    streak = int(prev["streak"]) + 1
                else:
                    streak = 1
                streaks[key] = streak
                self.conn.execute(
                    """
                    INSERT INTO stability (key, streak, last_cycle) VALUES (?, ?, ?)
                    ON CONFLICT(key) DO UPDATE SET streak = excluded.streak, last_cycle = excluded.last_cycle
                    """,
                    (key, streak, cycle),
                )
            self.conn.execute("DELETE FROM stability WHERE last_cycle < ?", (cycle - 12,))
            self._commit()
            return streaks

    def positions(self) -> list[Position]:
        with self.lock:
            rows = self.conn.execute("SELECT * FROM positions WHERE ledger = ?", (self._ledger(),)).fetchall()
        return [_position(row) for row in rows]

    def save_position(self, position: Position) -> None:
        with self.lock:
            self.conn.execute(
                """
                INSERT OR REPLACE INTO positions (
                    id, venue, market_id, event_id, event_title, title, outcome, side,
                    category, shares, entry_price, cost_basis, fees, signal, reason,
                    opened_cycle, bid, end_time, fee_model, fee_rate, fee_exponent, url, opened_at,
                    ledger
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    position.id, position.venue, position.market_id, position.event_id,
                    position.event_title, position.title, position.outcome, position.side,
                    position.category, position.shares, position.entry_price, position.cost_basis,
                    position.fees, position.signal, position.reason, position.opened_cycle,
                    position.bid, position.end_time, position.fee_model, position.fee_rate,
                    position.fee_exponent, position.url, position.opened_at, self._ledger(),
                ),
            )
            self._commit()

    def delete_position(self, position_id: str) -> None:
        with self.lock:
            self.conn.execute("DELETE FROM positions WHERE id = ?", (position_id,))
            self._commit()

    def add_trade(self, trade: Trade) -> None:
        with self.lock:
            self.conn.execute(
                """
                INSERT INTO trades (
                    id, ts, venue, market_id, title, outcome, action, shares, price,
                    fee, pnl, cash_after, signal, reason, won, side, ledger
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    trade.id, trade.ts, trade.venue, trade.market_id, trade.title,
                    trade.outcome, trade.action, trade.shares, trade.price, trade.fee,
                    trade.pnl, trade.cash_after, trade.signal, trade.reason, trade.won, trade.side,
                    self._ledger(),
                ),
            )
            self._commit()

    def last_close_id(self) -> str | None:
        """Identity of the latest actual exit in the active ledger."""
        with self.lock:
            row = self.conn.execute(
                "SELECT id FROM trades WHERE ledger = ? AND action IN ('sell', 'settle') ORDER BY rowid DESC LIMIT 1",
                (self._ledger(),),
            ).fetchone()
        return row[0] if row else None

    def trades(self, limit: int = 40) -> list[Trade]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT * FROM trades WHERE ledger = ? ORDER BY ts DESC, rowid DESC LIMIT ?",
                (self._ledger(), limit),
            ).fetchall()
        return [_trade(row) for row in rows]

    def realized_curve(self) -> list[dict]:
        """Completed trades only; pnl already includes entry and exit fees."""
        with self.lock:
            rows = self.conn.execute(
                "SELECT id, ts, title, side, pnl FROM trades WHERE ledger = ? AND action IN ('sell', 'settle') ORDER BY ts, rowid",
                (self._ledger(),),
            ).fetchall()
        total = 0.0
        points = []
        for row in rows:
            total += row["pnl"]
            points.append(dict(row, cumulative_pnl=round(total, 6)))
        return points

    def add_equity(self, equity: float, ts: datetime | None = None) -> None:
        with self.lock:
            self.conn.execute(
                "INSERT INTO equity (ts, equity, ledger) VALUES (?, ?, ?)",
                (_iso(ts), round(float(equity), 6), self._ledger()),
            )
            self._commit()

    def equity_curve(self, limit: int = 400) -> list[dict]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT ts, equity FROM equity WHERE ledger = ? ORDER BY id DESC LIMIT ?",
                (self._ledger(), limit),
            ).fetchall()
        return [{"t": row["ts"], "equity": row["equity"]} for row in reversed(rows)]

    def save_decisions(self, cycle: int, decisions: list[Decision]) -> None:
        with self.lock:
            self.conn.execute("DELETE FROM decisions WHERE cycle = ?", (cycle,))
            if decisions:
                self.conn.executemany(
                    "INSERT INTO decisions (cycle, payload) VALUES (?, ?)",
                    [(cycle, json.dumps(item.to_json())) for item in decisions],
                )
            self._commit()

    def decisions(self, limit: int = 80) -> list[dict]:
        with self.lock:
            cycle = int(self._get("cycle", 0))
            rows = self.conn.execute(
                "SELECT payload FROM decisions WHERE cycle = ? ORDER BY id ASC LIMIT ?",
                (cycle, limit),
            ).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def add_settlement(self, row: Settlement) -> None:
        with self.lock:
            self.conn.execute(
                """
                INSERT INTO settlements (ts, bucket, won, price, fee_per_share, pnl, market_id)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _iso(),
                    row.bucket,
                    1 if row.won else 0,
                    row.price,
                    row.fee_per_share,
                    row.pnl,
                    row.market_id or "",
                ),
            )
            self._commit()

    def settlements(self) -> list[Settlement]:
        with self.lock:
            rows = self.conn.execute("SELECT * FROM settlements ORDER BY id ASC").fetchall()
        return [
            Settlement(
                bucket=row["bucket"],
                won=bool(row["won"]),
                price=row["price"],
                fee_per_share=row["fee_per_share"],
                pnl=row["pnl"],
                market_id=str(row["market_id"] or ""),
            )
            for row in rows
        ]

    def settled_market_ids(self) -> set[str]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT market_id FROM settlements WHERE market_id IS NOT NULL AND market_id != ''"
            ).fetchall()
        return {str(row["market_id"]) for row in rows}

    def venue_record(self) -> dict[str, tuple[int, int]]:
        with self.lock:
            raw = self._get("venue_record") or {}
        found: dict[str, tuple[int, int]] = {}
        if not isinstance(raw, dict):
            return found
        for bucket, pair in raw.items():
            if isinstance(pair, (list, tuple)) and len(pair) == 2:
                found[str(bucket)] = (int(pair[0]), int(pair[1]))
        return found

    def set_venue_record(self, counts: dict[str, tuple[int, int]]) -> None:
        payload = {bucket: [int(wins), int(count)] for bucket, (wins, count) in counts.items()}
        with self.lock:
            self._put("venue_record", payload)
            self._commit()

    def venue_checked(self, hours: float) -> set[str]:
        """Tickers whose stored trades already answer this horizon."""
        with self.lock:
            self._score_covered(hours)
            self._commit()
            rows = self.conn.execute(
                "SELECT ticker FROM venue_samples WHERE hours = ?",
                (_hours_key(hours),),
            ).fetchall()
        return {str(row["ticker"]) for row in rows}

    def venue_resume(self, hours: float) -> dict[str, dict]:
        """How to continue a trade read that has not reached this horizon.

        The cursor is the next page of the same query. Without one, the next
        read starts again at the shallow rail so the boundary second is not skipped.
        """
        cutoff_delta = _hours_key(hours) * 3600
        found: dict[str, dict] = {}
        with self.lock:
            rows = self.conn.execute("SELECT * FROM venue_markets").fetchall()
        for row in rows:
            cutoff = float(row["close_ts"]) - cutoff_delta
            if _covers_cutoff(float(row["shallow_ts"]), float(row["covered_until"]), bool(row["exhausted"]), cutoff):
                continue
            cursor = str(row["resume_cursor"] or "")
            max_ts = row["resume_max_ts"]
            if cursor and max_ts is not None:
                found[str(row["ticker"])] = {"cursor": cursor, "max_ts": int(max_ts)}
            else:
                found[str(row["ticker"])] = {"cursor": "", "max_ts": _inclusive_max_ts(float(row["shallow_ts"]))}
        return found

    def add_venue_samples(self, incoming: dict) -> None:
        """Merge trade windows or fixture rows. An empty dict leaves the record alone."""
        if not incoming:
            return
        with self.lock:
            for ticker, row in incoming.items():
                if isinstance(row, dict):
                    self._merge_sample(str(ticker), row)
            hours = float(StrategyParams.from_json(self._get("params") or {}).min_hours_to_expiry)
            self._score_covered(hours)
            self._put("venue_record", self._record_payload(hours))
            self._commit()

    def _merge_sample(self, ticker: str, row: dict) -> None:
        trades = row.get("trades")
        if isinstance(trades, list):
            self._merge_trades(ticker, row, trades)
            return
        if "hours" not in row:
            return
        price = row.get("price")
        won = 1 if row.get("won") else 0
        bucket = str(row.get("bucket") or "")
        if price is None:
            won = 0
            bucket = ""
        elif not bucket:
            bucket = price_bucket(float(price))
        self.conn.execute(
            """
            INSERT INTO venue_samples (ticker, hours, price, won, bucket)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(ticker, hours) DO UPDATE SET
                price = excluded.price, won = excluded.won, bucket = excluded.bucket
            """,
            (ticker, _hours_key(row["hours"]), price, won, bucket),
        )

    def _merge_trades(self, ticker: str, row: dict, trades: list) -> None:
        existing = self.conn.execute("SELECT * FROM venue_markets WHERE ticker = ?", (ticker,)).fetchone()
        shallow = float(row.get("shallow_ts") or 0)
        covered = float(row.get("covered_until") if row.get("covered_until") is not None else shallow)
        exhausted = 1 if row.get("exhausted") else 0
        if existing is not None:
            shallow = max(shallow, float(existing["shallow_ts"]))
            covered = min(covered, float(existing["covered_until"]))
            exhausted = 1 if exhausted or existing["exhausted"] else 0
        reached = bool(row.get("reached")) or bool(exhausted)
        if reached:
            resume_cursor = ""
            resume_max_ts = None
        else:
            resume_cursor = str(row.get("resume_cursor") or "")
            resume_max_ts = row.get("resume_max_ts")
            if resume_max_ts is not None:
                resume_max_ts = int(resume_max_ts)
        self.conn.execute(
            """
            INSERT INTO venue_markets (
                ticker, result, close_ts, volume, covered_until, shallow_ts, exhausted,
                resume_cursor, resume_max_ts
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(ticker) DO UPDATE SET
                result = excluded.result,
                close_ts = excluded.close_ts,
                volume = excluded.volume,
                covered_until = excluded.covered_until,
                shallow_ts = excluded.shallow_ts,
                exhausted = excluded.exhausted,
                resume_cursor = excluded.resume_cursor,
                resume_max_ts = excluded.resume_max_ts
            """,
            (
                ticker,
                str(row.get("result") or ""),
                float(row.get("close_ts") or 0),
                float(row.get("volume") or 0),
                covered,
                shallow,
                exhausted,
                resume_cursor,
                resume_max_ts,
            ),
        )
        for trade in trades:
            if not isinstance(trade, dict) or trade.get("ts") is None or trade.get("price") is None:
                continue
            self.conn.execute(
                """
                INSERT INTO venue_trades (ticker, trade_key, traded_at, yes_price)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(ticker, trade_key) DO UPDATE SET
                    traded_at = excluded.traded_at, yes_price = excluded.yes_price
                """,
                (ticker, str(trade.get("key") or trade["ts"]), float(trade["ts"]), float(trade["price"])),
            )
        if row.get("reached"):
            self.conn.execute("DELETE FROM venue_samples WHERE ticker = ?", (ticker,))

    def _score_covered(self, hours: float) -> None:
        """Fill sample rows for markets whose trades reach this horizon. Caller holds the lock."""
        hours_key = _hours_key(hours)
        markets = self.conn.execute("SELECT * FROM venue_markets").fetchall()
        for market in markets:
            cutoff = float(market["close_ts"]) - hours_key * 3600
            if not _covers_cutoff(
                float(market["shallow_ts"]),
                float(market["covered_until"]),
                bool(market["exhausted"]),
                cutoff,
            ):
                continue
            have = self.conn.execute(
                "SELECT 1 FROM venue_samples WHERE ticker = ? AND hours = ?",
                (market["ticker"], hours_key),
            ).fetchone()
            if have is not None:
                continue
            trades = self.conn.execute(
                "SELECT traded_at, yes_price FROM venue_trades WHERE ticker = ? ORDER BY traded_at",
                (market["ticker"],),
            ).fetchall()
            price = None
            best_at = None
            for trade in trades:
                stamp = float(trade["traded_at"])
                if stamp <= cutoff + 1e-6 and (best_at is None or stamp > best_at):
                    best_at = stamp
                    price = float(trade["yes_price"])
            observed = settled_favorite(
                {"result": market["result"], "volume_fp": market["volume"]},
                price,
            )
            if observed is None:
                stored_price, won, bucket = None, 0, ""
            else:
                stored_price, won_flag = observed
                won = 1 if won_flag else 0
                bucket = price_bucket(stored_price)
            self.conn.execute(
                """
                INSERT INTO venue_samples (ticker, hours, price, won, bucket)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(ticker, hours) DO UPDATE SET
                    price = excluded.price, won = excluded.won, bucket = excluded.bucket
                """,
                (market["ticker"], hours_key, stored_price, won, bucket),
            )

    def _grouped(self, hours: float) -> dict[str, tuple[int, int]]:
        rows = self.conn.execute(
            """
            SELECT bucket, COALESCE(SUM(won), 0), COUNT(*)
            FROM venue_samples
            WHERE hours = ? AND price IS NOT NULL AND bucket != ''
              AND ticker NOT IN (
                  SELECT market_id FROM settlements
                  WHERE market_id IS NOT NULL AND market_id != ''
              )
            GROUP BY bucket
            """,
            (_hours_key(hours),),
        ).fetchall()
        return {str(row[0]): (int(row[1]), int(row[2])) for row in rows}

    def _record_payload(self, hours: float) -> dict[str, list[int]]:
        rows = self.conn.execute(
            """
            SELECT bucket, COALESCE(SUM(won), 0), COUNT(*)
            FROM venue_samples
            WHERE hours = ? AND price IS NOT NULL AND bucket != ''
            GROUP BY bucket
            """,
            (_hours_key(hours),),
        ).fetchall()
        return {str(row[0]): [int(row[1]), int(row[2])] for row in rows}

    def _has_samples(self) -> bool:
        row = self.conn.execute("SELECT 1 FROM venue_samples LIMIT 1").fetchone()
        return row is not None

    def calibration(self) -> dict[str, tuple[int, int]]:
        """Trades scored at the current horizon, plus this desk's own resolutions.

        A ticker we settled is left out of the venue counts and added from our
        settlement. A print counts only when its timestamp clears the horizon.
        An injected bucket record is the fallback when this book has no sample
        rows. An upgraded aggregate with no horizon is not that fallback.
        """
        with self.lock:
            params = StrategyParams.from_json(self._get("params") or {})
            if params.entry_window_minutes > 0:
                rows = self.conn.execute(
                    "SELECT bucket, SUM(CASE WHEN side = result THEN 1 ELSE 0 END), COUNT(*) "
                    "FROM near_observations WHERE result IN ('yes', 'no') AND minutes_left <= ? GROUP BY bucket",
                    (params.entry_window_minutes,),
                ).fetchall()
                return {str(row[0]): (int(row[1]), int(row[2])) for row in rows}
            hours = float(params.min_hours_to_expiry)
            self._score_covered(hours)
            self._commit()
            found: dict[str, list[int]] = {}
            if self._has_samples():
                for bucket, (wins, count) in self._grouped(hours).items():
                    found[bucket] = [wins, count]
            else:
                raw = self._get("venue_record") or {}
                if isinstance(raw, dict):
                    for bucket, pair in raw.items():
                        if isinstance(pair, (list, tuple)) and len(pair) == 2:
                            found[str(bucket)] = [int(pair[0]), int(pair[1])]
            rows = self.conn.execute("SELECT bucket, won FROM settlements ORDER BY id ASC").fetchall()
            for row in rows:
                wins, count = found.get(row["bucket"], [0, 0])
                found[row["bucket"]] = [wins + (1 if row["won"] else 0), count + 1]
            return {bucket: (wins, count) for bucket, (wins, count) in found.items()}

    def add_retro(self, payload: dict) -> None:
        with self.lock:
            self.conn.execute(
                "INSERT INTO retrospectives (ts, payload) VALUES (?, ?)",
                (_iso(), json.dumps(payload)),
            )
            self._commit()

    def save_scan(self, cycle: int, payload: dict, observations: list[dict]) -> None:
        """Keep the evidence needed to reproduce a scan's admissions and refusals."""
        with self.lock:
            self.conn.execute("INSERT OR REPLACE INTO scans VALUES (?, ?, ?)", (cycle, _iso(), json.dumps(payload)))
            self.conn.executemany(
                "INSERT OR REPLACE INTO observations VALUES (?, ?, ?)",
                [(cycle, row["key"], json.dumps(row)) for row in observations],
            )
            self._commit()

    def research_summary(self) -> dict:
        with self.lock:
            scans = self.conn.execute("SELECT COUNT(*) FROM scans").fetchone()[0]
            observations = self.conn.execute("SELECT COUNT(*), COUNT(DISTINCT quote_key) FROM observations").fetchone()
            rows = self.conn.execute("SELECT ts, payload FROM scans ORDER BY cycle DESC LIMIT 12").fetchall()
            near = self.conn.execute("SELECT COUNT(*), SUM(CASE WHEN result IN ('yes','no') THEN 1 ELSE 0 END), SUM(CASE WHEN result IN ('yes','no') AND side != result THEN 1 ELSE 0 END) FROM near_observations").fetchone()
            buckets = self.conn.execute(
                "SELECT bucket, COUNT(*), SUM(side=result), SUM(price+fee) "
                "FROM near_observations WHERE result IN ('yes','no') GROUP BY bucket"
            ).fetchall()
        cost = sum(row[3] for row in buckets)
        payout = sum(row[2] for row in buckets)
        quote_reference = {
            "cost": round(cost, 6), "payout": payout,
            "net_payoff": round(payout-cost, 6) if buckets else None,
            "buckets": {row[0]: {"resolved": row[1], "wins": row[2], "net_payoff": round(row[2]-row[3], 6)} for row in buckets},
            "note": "Research only, not fills or ledger profit. One contract per resolved observation at its saved ask plus fee, held to official outcome. Excludes executable depth, portfolio vetoes, stops, slippage and API costs; events may correlate. Pending observations are excluded.",
        }
        return {"near_resolution": {"observed_events": near[0], "resolved_events": near[1] or 0, "losing_outcomes": near[2] or 0,
                                    "quote_reference": quote_reference}, "scans_recorded": scans, "observations": observations[0], "distinct_contract_sides": observations[1],
                "recent_scans": [dict(json.loads(row["payload"]), ts=row["ts"]) for row in rows]}

    def retros(self, limit: int = 8) -> list[dict]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT ts, payload FROM retrospectives ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        out = []
        for row in rows:
            item = json.loads(row["payload"])
            item["ts"] = row["ts"]
            out.append(item)
        return out

    def latest_model_review(self) -> dict | None:
        with self.lock:
            row = self.conn.execute(
                "SELECT ts, payload FROM retrospectives WHERE json_extract(payload, '$.source') = 'openai' ORDER BY id DESC LIMIT 1"
            ).fetchone()
        return dict(json.loads(row["payload"]), ts=row["ts"]) if row else None

    def trade_reviews(self) -> dict:
        """Link only recorded buys to their own scan review, never the latest review."""
        with self.lock:
            rows = self.conn.execute("""
                SELECT d.payload AS decision, r.payload AS review, r.ts
                FROM decisions d LEFT JOIN retrospectives r
                  ON json_extract(r.payload, '$.cycle') = d.cycle
                WHERE json_extract(d.payload, '$.action') = 'bought'
                ORDER BY d.cycle, d.id
            """).fetchall()
            trades = self.conn.execute("SELECT * FROM trades WHERE action='buy' ORDER BY ts,rowid").fetchall()
        by_key, entries, result = {}, {}, {}
        for row in rows:
            key = json.loads(row['decision']).get('key')
            by_key.setdefault(key, []).append(row)
        for trade in trades:
            key = f"{trade['venue']}:{trade['market_id']}:{trade['side']}"
            entries.setdefault(key, []).append(trade)
        for key, buys in entries.items():
            reviews = by_key.get(key, [])
            if len(buys) != len(reviews):
                continue  # Imported or incomplete history must not borrow a review.
            for trade, row in zip(buys, reviews):
                review = json.loads(row['review']) if row['review'] else {}
                if review.get('source') == 'openai':
                    result[trade['id']] = {'summary': review.get('summary'),
                        'concerns': review.get('concerns', []), 'ts': row['ts'],
                        'context': 'Entry scan review · before execution'}
        return result

    def market_links(self) -> dict:
        """Use URLs archived from venue quotes, including closed positions."""
        with self.lock:
            rows = self.conn.execute("SELECT DISTINCT quote_key, json_extract(payload, '$.url') FROM observations WHERE json_extract(payload, '$.url') IS NOT NULL").fetchall()
            archived = self.conn.execute("SELECT ticker, json_extract(payload, '$.url') FROM near_observations WHERE ticker IN (SELECT market_id FROM trades)").fetchall()
            keys = self.conn.execute("SELECT DISTINCT venue, market_id, side FROM trades").fetchall()
        links = {row[0]: row[1] for row in rows if row[1]}
        fallback = {row[0]: row[1] for row in archived if row[1]}
        return {f'{venue}:{market}:{side}': normalize_market_url(links.get(f'{venue}:{market}:{side}', fallback.get(market)))
                for venue, market, side in keys if links.get(f'{venue}:{market}:{side}', fallback.get(market))}

    def paper_started_at(self) -> str:
        with self.lock:
            return str(self._get("paper_started_at") or "")

    def csrf(self) -> str:
        with self.lock:
            return str(self._get("csrf") or "")

    def blocked(self) -> set[str]:
        with self.lock:
            return set(self._get("blocked") or [])

    def operator_pause(self) -> bool:
        with self.lock:
            return bool(self._get("operator_pause", False))

    def set_pause(self, paused: bool) -> None:
        with self.lock:
            self._put("operator_pause", bool(paused))
            self._commit()

    def block(self, key: str, reason: str) -> None:
        with self.lock:
            blocked = list(self._get("blocked") or [])
            if key not in blocked:
                blocked.append(key)
                self._put("blocked", blocked)
            self._audit("operator", "block", "", key, reason)
            self._commit()

    def append_audit(self, actor: str, action: str, old, new, reason: str) -> None:
        with self.lock:
            self._audit(actor, action, old, new, reason)
            self._commit()

    def _audit(self, actor: str, action: str, old, new, reason: str) -> None:
        self.conn.execute(
            """
            INSERT INTO audit (ts, actor, action, old_value, new_value, reason)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (_iso(), actor, action, json.dumps(old), json.dumps(new), reason),
        )

    def audit(self, limit: int = 12) -> list[dict]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT ts, actor, action, old_value, new_value, reason FROM audit ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            {
                "ts": row["ts"],
                "actor": row["actor"],
                "action": row["action"],
                "old": json.loads(row["old_value"]),
                "new": json.loads(row["new_value"]),
                "reason": row["reason"],
            }
            for row in rows
        ]

    def book(self, streaks: dict[str, int] | None = None) -> BookView:
        positions = self.positions()
        equity = self.mark_equity(positions)
        peak = self.note_peak(equity)
        return BookView(
            equity=equity,
            cash=self.cash(),
            peak=peak,
            deployed=sum(item.cost_basis for item in positions),
            positions=positions,
            streaks=streaks or {},
            calibration=self.calibration(),
            settlements=self.settlements(),
            blocked=self.blocked(),
            operator_pause=self.operator_pause(),
        )

    def mark_equity(self, positions: list[Position] | None = None) -> float:
        positions = self.positions() if positions is None else positions
        return self.cash() + sum(item.mark_value for item in positions)

    def reset(self, params: StrategyParams) -> None:
        """Clear this paper book. The venue trade record is kept."""
        with self.lock:
            for table in ("positions", "trades", "decisions", "equity", "retrospectives", "daily_reviews", "scans", "observations", "stability", "settlements", "audit", "meta"):
                self.conn.execute(f"DELETE FROM {table}")
            self._commit()
        self._seed(params)


def _position(row: sqlite3.Row) -> Position:
    return Position(
        id=row["id"],
        venue=row["venue"],
        market_id=row["market_id"],
        event_id=row["event_id"],
        event_title=row["event_title"],
        title=row["title"],
        outcome=row["outcome"],
        side=row["side"],
        category=row["category"],
        shares=row["shares"],
        entry_price=row["entry_price"],
        cost_basis=row["cost_basis"],
        fees=row["fees"],
        signal=row["signal"],
        reason=row["reason"],
        opened_cycle=row["opened_cycle"],
        bid=row["bid"],
        end_time=row["end_time"],
        fee_model=row["fee_model"],
        fee_rate=row["fee_rate"],
        fee_exponent=row["fee_exponent"],
        url=row["url"] or "",
        opened_at=row["opened_at"] or "",
    )


def _trade(row: sqlite3.Row) -> Trade:
    return Trade(
        id=row["id"],
        ts=row["ts"],
        venue=row["venue"],
        market_id=row["market_id"],
        title=row["title"],
        outcome=row["outcome"],
        action=row["action"],
        shares=row["shares"],
        price=row["price"],
        fee=row["fee"],
        pnl=row["pnl"],
        cash_after=row["cash_after"],
        signal=row["signal"],
        reason=row["reason"],
        won=row["won"],
        side=row["side"],
    )
