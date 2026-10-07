"""Paper fills.

Buys pay the ask and the taker fee. Marks use the bid, minus the fee to get
out. A win settles at $1 a share. A loss settles at zero. Nothing here talks
to an exchange.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from cst.fees import fee_for
from cst.models import Position, Quote, Trade
from cst.store import Store


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class PaperBroker:
    def __init__(self, store: Store):
        self.store = store

    def buy(self, quote: Quote, shares: float, signal: str, reason: str, cycle: int) -> Trade | None:
        fee = float(fee_for(quote.fee_model, shares, quote.ask, quote.fee_rate, quote.fee_exponent))
        cost = shares * quote.ask + fee
        cash = self.store.cash()
        if cost > cash + 1e-9:
            return None
        cash = round(cash - cost, 6)
        self.store.set_cash(cash)
        self.store.add_fee(fee)
        position = Position(
            id=str(uuid.uuid4()),
            venue=quote.venue,
            market_id=quote.market_id,
            event_id=quote.event_id,
            event_title=quote.event_title,
            title=quote.title,
            outcome=quote.outcome,
            side=quote.side,
            category=quote.category,
            shares=shares,
            entry_price=quote.ask,
            cost_basis=cost,
            fees=fee,
            signal=signal,
            reason=reason,
            opened_cycle=cycle,
            bid=quote.bid,
            end_time=quote.end_time.isoformat(timespec="seconds") if quote.end_time else None,
            fee_model=quote.fee_model,
            fee_rate=quote.fee_rate,
            fee_exponent=quote.fee_exponent,
            url=quote.url,
            opened_at=_now(),
        )
        self.store.save_position(position)
        trade = Trade(
            id=str(uuid.uuid4()),
            ts=_now(),
            venue=quote.venue,
            market_id=quote.market_id,
            title=quote.title,
            outcome=quote.outcome,
            action="buy",
            shares=shares,
            price=quote.ask,
            fee=fee,
            pnl=0.0,
            cash_after=cash,
            signal=signal,
            reason=reason,
        )
        self.store.add_trade(trade)
        return trade

    def sell(self, position: Position, bid: float, reason: str) -> Trade | None:
        # Zero is a real bid on a pinned book. A missing book never reaches here.
        if bid < 0 or bid > 1:
            return None
        fee = float(fee_for(position.fee_model, position.shares, bid, position.fee_rate, position.fee_exponent))
        proceeds = max(0.0, position.shares * bid - fee)
        pnl = proceeds - position.cost_basis
        cash = round(self.store.cash() + proceeds, 6)
        self.store.set_cash(cash)
        self.store.add_fee(fee)
        self.store.add_realized(pnl)
        self.store.delete_position(position.id)
        trade = Trade(
            id=str(uuid.uuid4()),
            ts=_now(),
            venue=position.venue,
            market_id=position.market_id,
            title=position.title,
            outcome=position.outcome,
            action="sell",
            shares=position.shares,
            price=bid,
            fee=fee,
            pnl=pnl,
            cash_after=cash,
            signal=position.signal,
            reason=reason,
        )
        self.store.add_trade(trade)
        return trade

    def settle(self, position: Position, won: bool) -> Trade:
        proceeds = position.shares * (1.0 if won else 0.0)
        pnl = proceeds - position.cost_basis
        cash = round(self.store.cash() + proceeds, 6)
        self.store.set_cash(cash)
        self.store.add_realized(pnl)
        self.store.delete_position(position.id)
        trade = Trade(
            id=str(uuid.uuid4()),
            ts=_now(),
            venue=position.venue,
            market_id=position.market_id,
            title=position.title,
            outcome=position.outcome,
            action="settle",
            shares=position.shares,
            price=1.0 if won else 0.0,
            fee=0.0,
            pnl=pnl,
            cash_after=cash,
            signal=position.signal,
            reason="Settled in the favorite's favor." if won else "Settled against the favorite.",
            won=1 if won else 0,
        )
        self.store.add_trade(trade)
        return trade

    def mark(self, position: Position, bid: float) -> None:
        position.bid = bid
        self.store.save_position(position)
