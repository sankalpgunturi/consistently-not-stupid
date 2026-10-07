"""Shared records for the paper desk."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


@dataclass(slots=True)
class Quote:
    venue: str
    market_id: str
    event_id: str
    event_title: str
    title: str
    outcome: str
    side: str
    bid: float
    ask: float
    ask_size: float
    volume: float
    liquidity: float
    end_time: datetime | None
    category: str
    fee_model: str
    fee_rate: float
    fee_exponent: float
    rules: str = ""
    url: str = ""
    min_shares: float = 1
    token_id: str = ""
    bid_size: float = -1
    settled: bool = False
    winner: str | None = None
    fee_verified: bool = True
    tradable: bool = True
    expected_resolution_time: datetime | None = None

    @property
    def key(self) -> str:
        return f"{self.venue}:{self.market_id}:{self.side}"

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2

    @property
    def spread(self) -> float:
        return self.ask - self.bid


@dataclass(slots=True)
class StrategyParams:
    # Zero preserves replay compatibility with the former long-horizon strategy.
    entry_window_minutes: float = 0
    min_probability: float = 0.90
    amount_per_bet: float = 1.0
    stop_loss_minutes: int = 0
    exit_probability: float = 0.60
    min_win_profit: float = 0.01
    min_edge: float = 0.01
    max_spread: float = 0.03
    max_days_to_expiry: float = 21
    min_hours_to_expiry: float = 2
    max_position_fraction: float = 0.008
    max_deployed_fraction: float = 0.40
    max_category_fraction: float = 0.15
    max_drawdown: float = 0.05
    stop_gap: float = 0.08
    scan_interval_seconds: int = 600
    mark_interval_seconds: int = 60
    min_stable_scans: int = 2
    correlation_threshold: float = 0.48
    min_sample: int = 30
    max_new_per_cycle: int = 10

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> "StrategyParams":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in raw.items() if k in known})


# Rails the retrospective is allowed to move, and the largest step per scan.
RAILS: dict[str, tuple[float, float]] = {
    "entry_window_minutes": (1, 60),
    "min_probability": (0.80, 0.99),
    "min_win_profit": (0.005, 0.03),
    "min_edge": (0.005, 0.04),
    "max_spread": (0.01, 0.05),
    "max_days_to_expiry": (3, 60),
    "min_hours_to_expiry": (1, 12),
    "max_position_fraction": (0.002, 0.02),
    "max_deployed_fraction": (0.10, 0.50),
    "max_category_fraction": (0.08, 0.30),
    "max_drawdown": (0.02, 0.08),
    "stop_gap": (0.04, 0.15),
    "scan_interval_seconds": (1, 3600),
    "min_stable_scans": (1, 6),
    "correlation_threshold": (0.35, 0.75),
    "min_sample": (20, 100),
    "max_new_per_cycle": (1, 20),
}

STEPS: dict[str, float] = {
    "entry_window_minutes": 1,
    "min_probability": 0.01,
    "min_win_profit": 0.002,
    "min_edge": 0.002,
    "max_spread": 0.005,
    "max_days_to_expiry": 3,
    "min_hours_to_expiry": 1,
    "max_position_fraction": 0.001,
    "max_deployed_fraction": 0.05,
    "max_category_fraction": 0.02,
    "max_drawdown": 0.01,
    "stop_gap": 0.01,
    "scan_interval_seconds": 60,
    "min_stable_scans": 1,
    "correlation_threshold": 0.02,
    "min_sample": 5,
    "max_new_per_cycle": 1,
}

# Moving the knob this way lets more risk in.
LOOSEN_UP = frozenset({
    "entry_window_minutes",
    "max_spread",
    "max_days_to_expiry",
    "max_position_fraction",
    "max_deployed_fraction",
    "max_category_fraction",
    "max_drawdown",
    "stop_gap",
    "max_new_per_cycle",
    # A higher twin bar treats fewer overlaps as the same risk, so more clips get in.
    "correlation_threshold",
})
LOOSEN_DOWN = frozenset({
    "min_probability",
    "min_win_profit",
    "min_edge",
    "min_hours_to_expiry",
    "min_stable_scans",
    "min_sample",
})

PARAM_COPY: dict[str, tuple[str, str]] = {
    "min_probability": ("Entry probability", "Bid and ask both have to clear this."),
    "min_win_profit": ("Profit if it wins", "Cents per share that must remain after the fee."),
    "min_edge": ("Minimum edge", "How far the settled record has to sit above our all-in cost."),
    "max_spread": ("Maximum spread", "Wider books are not a probability, they are a guess."),
    "max_days_to_expiry": ("Furthest expiry", "Days of calendar risk the book will still enter."),
    "min_hours_to_expiry": ("Nearest expiry", "Hours left required before a new buy."),
    "max_position_fraction": ("Per-trade cap", "Largest fraction of equity one clip may use."),
    "max_deployed_fraction": ("Deployed cap", "Fraction of equity allowed out of cash."),
    "max_category_fraction": ("Category cap", "Fraction of equity allowed in one theme."),
    "max_drawdown": ("Pause line", "New buys pause after this fall from the peak."),
    "stop_gap": ("Exit gap", "Sell if the bid falls this far under the price we paid."),
    "scan_interval_seconds": ("Scan every", "Target seconds between current-quote evaluations. Discovery runs separately."),
    "min_stable_scans": ("Stable scans", "Consecutive scans a favorite must stay in band."),
    "correlation_threshold": ("Twin bar", "Overlap that counts as the same risk."),
    "min_sample": ("Record length", "Settled trades in a price bucket before that record can buy."),
    "max_new_per_cycle": ("New buys per scan", "Cap on clips opened in one pass."),
}


@dataclass(slots=True)
class Decision:
    action: str
    reason_code: str
    title: str
    venue: str
    outcome: str
    detail: str
    event_id: str = ""
    event_title: str = ""
    price: float | None = None
    edge: float | None = None
    group_count: int = 1
    key: str = ""

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class Proposal:
    quote: Quote
    shares: float
    edge: float
    signal: str
    detail: str
    confirm: str

    @property
    def key(self) -> str:
        return self.quote.key


@dataclass(slots=True)
class Position:
    id: str
    venue: str
    market_id: str
    event_id: str
    event_title: str
    title: str
    outcome: str
    side: str
    category: str
    shares: float
    entry_price: float
    cost_basis: float
    fees: float
    signal: str
    reason: str
    opened_cycle: int
    bid: float
    end_time: str | None
    fee_model: str
    fee_rate: float
    fee_exponent: float
    url: str = ""
    opened_at: str = ""

    def to_json(self) -> dict[str, Any]:
        mark = self.mark_value
        return {
            "id": self.id,
            "venue": self.venue,
            "market_id": self.market_id,
            "title": self.title,
            "outcome": self.outcome,
            "side": self.side,
            "category": self.category,
            "shares": self.shares,
            "entry_price": self.entry_price,
            "cost_basis": round(self.cost_basis, 4),
            "fees": round(self.fees, 4),
            "bid": self.bid,
            "mark": round(mark, 4),
            "unrealized": round(mark - self.cost_basis, 4),
            "profit_if_win": round(self.shares - self.cost_basis, 4),
            "loss_if_wrong": round(self.cost_basis, 4),
            "signal": self.signal,
            "reason": self.reason,
            "url": self.url,
            "end_time": self.end_time,
            "opened_at": self.opened_at,
        }

    @property
    def mark_value(self) -> float:
        from cst.fees import fee_for

        # A missing book leaves the last bid in place. A quoted zero is worthless.
        if self.bid < 0:
            return self.cost_basis
        if self.bid == 0:
            return 0.0
        exit_fee = float(fee_for(self.fee_model, self.shares, self.bid, self.fee_rate, self.fee_exponent))
        return max(0.0, self.shares * self.bid - exit_fee)


@dataclass(slots=True)
class Trade:
    id: str
    ts: str
    venue: str
    market_id: str
    title: str
    outcome: str
    action: str
    shares: float
    price: float
    fee: float
    pnl: float
    cash_after: float
    signal: str
    reason: str
    won: int | None = None
    side: str = ""

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class Settlement:
    bucket: str
    won: bool
    price: float
    fee_per_share: float
    pnl: float
    market_id: str = ""


@dataclass(slots=True)
class BookView:
    equity: float
    cash: float
    peak: float
    deployed: float
    positions: list[Position] = field(default_factory=list)
    streaks: dict[str, int] = field(default_factory=dict)
    calibration: dict[str, tuple[int, int]] = field(default_factory=dict)
    settlements: list[Settlement] = field(default_factory=list)
    blocked: set[str] = field(default_factory=set)
    operator_pause: bool = False


# Explicit operator controls; automatic reviews retain their tightening rails.
OPERATOR_CONTROLS = {
    "entry_window_minutes": ("Outcome within", 1, 60, 1),
    "min_probability": ("Entry probability", 0.80, 0.99, 0.01),
    "scan_interval_seconds": ("Scan every", 1, 60, 1),
    "amount_per_bet": ("Amount per bet", 1, 100, 1),
    "stop_loss_minutes": ("Stop loss: final minutes", 0, 60, 1),
    "exit_probability": ("Exit below", 0.01, 0.99, 0.01),
}
