"""SQLite book. The paper account, the tape, and the learned knobs live here."""

from __future__ import annotations

import json
import secrets
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from cst.models import BookView, Decision, Position, Settlement, StrategyParams, Trade, utcnow


def _iso(dt: datetime | None = None) -> str:
    return (dt or utcnow()).astimezone(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: Path, params: StrategyParams, bankroll: float):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.bankroll = float(bankroll)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        self._init()
        self._seed(params)
        self._ensure_controls()

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
            CREATE TABLE IF NOT EXISTS stability (
                key TEXT PRIMARY KEY,
                streak INTEGER,
                last_cycle INTEGER
            );
            CREATE TABLE IF NOT EXISTS settlements (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT, bucket TEXT, won INTEGER, price REAL,
                fee_per_share REAL, pnl REAL
            );
            CREATE TABLE IF NOT EXISTS audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT, actor TEXT, action TEXT,
                old_value TEXT, new_value TEXT, reason TEXT
            );
            """
        )
        self.conn.commit()

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
                self._put("approved_pairs", [])
                self._put("blocked", [])
                self._put("operator_pause", False)
                self.conn.execute("INSERT INTO equity (ts, equity) VALUES (?, ?)", (_iso(), self.bankroll))
                self.conn.commit()

    def _ensure_controls(self) -> None:
        with self.lock:
            if not self._get("paper_started_at"):
                self._put("paper_started_at", _iso())
            if not self._get("csrf"):
                self._put("csrf", secrets.token_urlsafe(24))
            if self._get("approved_pairs") is None:
                self._put("approved_pairs", [])
            if self._get("blocked") is None:
                self._put("blocked", [])
            if self._get("operator_pause") is None:
                self._put("operator_pause", False)
            self.conn.commit()

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

    def params(self) -> StrategyParams:
        with self.lock:
            raw = self._get("params", {})
        return StrategyParams.from_json(raw or {})

    def save_params(self, params: StrategyParams) -> None:
        with self.lock:
            self._put("params", params.to_json())
            self.conn.commit()

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
                self.conn.commit()
            return updated, notes, applied

    def governor_seen(self) -> int:
        with self.lock:
            return int(self._get("governor_seen", 0) or 0)

    def set_governor_seen(self, count: int) -> None:
        with self.lock:
            self._put("governor_seen", int(count))
            self.conn.commit()

    def cash(self) -> float:
        with self.lock:
            return float(self._get("cash", self.bankroll))

    def set_cash(self, cash: float) -> None:
        with self.lock:
            self._put("cash", round(float(cash), 6))
            self.conn.commit()

    def add_fee(self, fee: float) -> None:
        with self.lock:
            self._put("fees_paid", round(float(self._get("fees_paid", 0)) + fee, 6))
            self.conn.commit()

    def fees_paid(self) -> float:
        with self.lock:
            return float(self._get("fees_paid", 0))

    def realized(self) -> float:
        with self.lock:
            return float(self._get("realized", 0))

    def add_realized(self, pnl: float) -> None:
        with self.lock:
            self._put("realized", round(float(self._get("realized", 0)) + pnl, 6))
            self.conn.commit()

    def peak(self) -> float:
        with self.lock:
            return float(self._get("peak", self.bankroll))

    def note_peak(self, equity: float) -> float:
        with self.lock:
            peak = float(self._get("peak", self.bankroll))
            if equity > peak:
                peak = equity
                self._put("peak", round(peak, 6))
                self.conn.commit()
            return peak

    def next_cycle(self) -> int:
        with self.lock:
            cycle = int(self._get("cycle", 0)) + 1
            self._put("cycle", cycle)
            self.conn.commit()
            return cycle

    def cycle(self) -> int:
        with self.lock:
            return int(self._get("cycle", 0))

    def set_status(self, status: str, extra: dict | None = None) -> None:
        with self.lock:
            self._put("status", status)
            if extra is not None:
                self._put("cycle_info", extra)
            self.conn.commit()

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
            self.conn.commit()
            return streaks

    def positions(self) -> list[Position]:
        with self.lock:
            rows = self.conn.execute("SELECT * FROM positions").fetchall()
        return [_position(row) for row in rows]

    def save_position(self, position: Position) -> None:
        with self.lock:
            self.conn.execute(
                """
                INSERT OR REPLACE INTO positions (
                    id, venue, market_id, event_id, event_title, title, outcome, side,
                    category, shares, entry_price, cost_basis, fees, signal, reason,
                    opened_cycle, bid, end_time, fee_model, fee_rate, fee_exponent, url, opened_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    position.id, position.venue, position.market_id, position.event_id,
                    position.event_title, position.title, position.outcome, position.side,
                    position.category, position.shares, position.entry_price, position.cost_basis,
                    position.fees, position.signal, position.reason, position.opened_cycle,
                    position.bid, position.end_time, position.fee_model, position.fee_rate,
                    position.fee_exponent, position.url, position.opened_at,
                ),
            )
            self.conn.commit()

    def delete_position(self, position_id: str) -> None:
        with self.lock:
            self.conn.execute("DELETE FROM positions WHERE id = ?", (position_id,))
            self.conn.commit()

    def add_trade(self, trade: Trade) -> None:
        with self.lock:
            self.conn.execute(
                """
                INSERT INTO trades (
                    id, ts, venue, market_id, title, outcome, action, shares, price,
                    fee, pnl, cash_after, signal, reason, won
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    trade.id, trade.ts, trade.venue, trade.market_id, trade.title,
                    trade.outcome, trade.action, trade.shares, trade.price, trade.fee,
                    trade.pnl, trade.cash_after, trade.signal, trade.reason, trade.won,
                ),
            )
            self.conn.commit()

    def trades(self, limit: int = 40) -> list[Trade]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT * FROM trades ORDER BY ts DESC, rowid DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [_trade(row) for row in rows]

    def add_equity(self, equity: float, ts: datetime | None = None) -> None:
        with self.lock:
            self.conn.execute(
                "INSERT INTO equity (ts, equity) VALUES (?, ?)",
                (_iso(ts), round(float(equity), 6)),
            )
            self.conn.commit()

    def equity_curve(self, limit: int = 400) -> list[dict]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT ts, equity FROM equity ORDER BY id DESC LIMIT ?",
                (limit,),
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
            self.conn.commit()

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
                INSERT INTO settlements (ts, bucket, won, price, fee_per_share, pnl)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (_iso(), row.bucket, 1 if row.won else 0, row.price, row.fee_per_share, row.pnl),
            )
            self.conn.commit()

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
            )
            for row in rows
        ]

    def calibration(self) -> dict[str, tuple[int, int]]:
        found: dict[str, list[int]] = {}
        for row in self.settlements():
            wins, n = found.get(row.bucket, [0, 0])
            found[row.bucket] = [wins + (1 if row.won else 0), n + 1]
        return {bucket: (wins, n) for bucket, (wins, n) in found.items()}

    def add_retro(self, payload: dict) -> None:
        with self.lock:
            self.conn.execute(
                "INSERT INTO retrospectives (ts, payload) VALUES (?, ?)",
                (_iso(), json.dumps(payload)),
            )
            self.conn.commit()

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

    def paper_started_at(self) -> str:
        with self.lock:
            return str(self._get("paper_started_at") or "")

    def csrf(self) -> str:
        with self.lock:
            return str(self._get("csrf") or "")

    def approved_pairs(self) -> set[str]:
        with self.lock:
            return set(self._get("approved_pairs") or [])

    def blocked(self) -> set[str]:
        with self.lock:
            return set(self._get("blocked") or [])

    def operator_pause(self) -> bool:
        with self.lock:
            return bool(self._get("operator_pause", False))

    def set_pause(self, paused: bool) -> None:
        with self.lock:
            self._put("operator_pause", bool(paused))
            self.conn.commit()

    def approve_pair(self, pair_id: str, note: str) -> None:
        with self.lock:
            pairs = list(self._get("approved_pairs") or [])
            if pair_id not in pairs:
                pairs.append(pair_id)
                self._put("approved_pairs", pairs)
            self._audit("operator", "approve_pair", "", pair_id, note)
            self.conn.commit()

    def block(self, key: str, reason: str) -> None:
        with self.lock:
            blocked = list(self._get("blocked") or [])
            if key not in blocked:
                blocked.append(key)
                self._put("blocked", blocked)
            self._audit("operator", "block", "", key, reason)
            self.conn.commit()

    def append_audit(self, actor: str, action: str, old, new, reason: str) -> None:
        with self.lock:
            self._audit(actor, action, old, new, reason)
            self.conn.commit()

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
            approved_pairs=self.approved_pairs(),
            blocked=self.blocked(),
            operator_pause=self.operator_pause(),
        )

    def mark_equity(self, positions: list[Position] | None = None) -> float:
        positions = self.positions() if positions is None else positions
        return self.cash() + sum(item.mark_value for item in positions)

    def reset(self, params: StrategyParams) -> None:
        with self.lock:
            for table in ("positions", "trades", "decisions", "equity", "retrospectives", "stability", "settlements", "audit", "meta"):
                self.conn.execute(f"DELETE FROM {table}")
            self.conn.commit()
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
    )
