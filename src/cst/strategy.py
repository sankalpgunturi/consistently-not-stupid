"""Admission rules for a favorite.

The economic law, which the retrospective is not allowed to turn off:

* Pay the ask only when both sides of the book are already at the probability bar.
* If the contract wins, the payout has to clear the venue fee by ``min_win_profit``.
* A market quote alone is not an edge. Buying it, when the quote is right,
  loses the fee. A buy needs a settled record sitting above the all-in cost.
* One clip is the venue minimum. A second copy of the same risk is refused.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from cst.fees import fee_for
from cst.models import BookView, Decision, Proposal, Quote, StrategyParams
from cst.text import related


@dataclass
class Evaluation:
    decisions: list[Decision]
    proposals: list[Proposal]
    counts: dict[str, int]
    focus: Decision | None


@dataclass
class _Row:
    quote: Quote
    code: str
    detail: str
    edge: float | None = None
    proposal: Proposal | None = None
    group: str = ""


def wilson_lower(wins: int, n: int, z: float = 1.96) -> float:
    """Lower bound of a win rate. Zero when the record is empty."""
    if n <= 0:
        return 0.0
    p = wins / n
    z2 = z * z
    denom = 1 + z2 / n
    centre = p + z2 / (2 * n)
    margin = z * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n))
    return max(0.0, (centre - margin) / denom)


def price_bucket(price: float) -> str:
    if price < 0.93:
        return "0.90–0.93"
    if price < 0.96:
        return "0.93–0.96"
    return "0.96–0.99"


def quote_in_band(quote: Quote, params: StrategyParams) -> bool:
    if quote.settled or quote.bid <= 0 or quote.ask <= 0:
        return False
    if quote.ask >= 0.999 or quote.ask < quote.bid:
        return False
    if quote.spread - 1e-9 > params.max_spread:
        return False
    return quote.bid + 1e-12 >= params.min_probability and quote.ask + 1e-12 >= params.min_probability


def _cents(value: float) -> str:
    return f"{value * 100:.1f}¢"


def _hours_left(quote: Quote, now: datetime) -> float | None:
    if quote.end_time is None:
        return None
    end = quote.end_time if quote.end_time.tzinfo else quote.end_time.replace(tzinfo=timezone.utc)
    return (end - now).total_seconds() / 3600


def _fee_ok(quote: Quote, params: StrategyParams) -> tuple[bool, float, float, str]:
    shares = quote.min_shares
    fee = float(fee_for(quote.fee_model, shares, quote.ask, quote.fee_rate, quote.fee_exponent))
    per_share = fee / shares if shares else fee
    profit = 1 - quote.ask - per_share
    if profit + 1e-12 < params.min_win_profit:
        detail = (
            f"Paying {_cents(quote.ask)} plus a {_cents(per_share)} fee leaves {_cents(profit)} "
            f"if the favorite wins. The desk wants at least {_cents(params.min_win_profit)} left."
        )
        return False, per_share, profit, detail
    detail = (
        f"Ask {_cents(quote.ask)}, fee {_cents(per_share)}. "
        f"If it wins, {_cents(profit)} a share is left after the fee."
    )
    return True, per_share, profit, detail


def _structural(quote: Quote, params: StrategyParams, now: datetime) -> tuple[str, str] | None:
    """Return a skip code, or None when the quote clears the structural gates."""
    if quote.ask_size >= 0 and quote.ask_size + 1e-9 < quote.min_shares:
        return "book", "The size on the offer is smaller than the venue minimum."
    if quote.volume < 10 and quote.liquidity < 10:
        return "liquidity", "Almost no contracts have traded, so the quote is not a crowd."
    hours = _hours_left(quote, now)
    if hours is None:
        return "horizon", "There is no close time, so the cash could sit there with no end."
    if hours < params.min_hours_to_expiry:
        return "horizon", "It closes too soon. The desk wants time to see the quote hold still."
    if hours > params.max_days_to_expiry * 24:
        return "horizon", "It is too far away. A high price today is a different bet in a few months."
    ok, _per, _profit, detail = _fee_ok(quote, params)
    if not ok:
        return "fee", detail
    return None


def _learned_signal(quote: Quote, params: StrategyParams, book: BookView) -> Proposal | None:
    wins, n = book.calibration.get(price_bucket(quote.ask), (0, 0))
    if n < params.min_sample:
        return None
    lower = wilson_lower(wins, n)
    _ok, per_share, _profit, _detail = _fee_ok(quote, params)
    edge = lower - quote.ask - per_share
    if edge + 1e-12 < params.min_edge or lower + 1e-12 < params.min_probability:
        return None
    detail = (
        f"In the {price_bucket(quote.ask)} bucket, {wins} of {n} settled favorites won. "
        f"The cautious win rate is {lower:.1%}, against an all-in cost of {_cents(quote.ask + per_share)}."
    )
    return Proposal(
        quote=quote,
        shares=quote.min_shares,
        edge=edge,
        signal="learned",
        detail=detail,
        confirm=f"settled record {lower:.1%}",
    )


def tightened_out(quote: Quote, params: StrategyParams, book: BookView) -> str | None:
    """A sentence when a mid-scan tighten means this quote no longer clears.

    The scan reads the knobs once, then the fetch can take long enough for an
    operator to step them. The fill has to see the new bar and the new edge.
    """
    if not quote_in_band(quote, params):
        return "A knob was tightened during this scan, and the quote is no longer at the bar."
    if _learned_signal(quote, params, book) is None:
        return "A knob was tightened during this scan, and the settled record no longer clears the all-in cost."
    return None


def _clip_fits(quote: Quote, params: StrategyParams, equity: float) -> tuple[bool, float, str]:
    shares = quote.min_shares
    fee = float(fee_for(quote.fee_model, shares, quote.ask, quote.fee_rate, quote.fee_exponent))
    cost = shares * quote.ask + fee
    cap = equity * params.max_position_fraction
    if cost > cap + 1e-9:
        return False, cost, (
            f"The venue minimum costs ${cost:.2f}, and the per-trade cap is ${cap:.2f}."
        )
    return True, cost, ""


def evaluate(quotes: list[Quote], params: StrategyParams, book: BookView, now: datetime | None = None) -> Evaluation:
    now = now or datetime.now(timezone.utc)
    counts = {
        "markets_read": len({(q.venue, q.market_id) for q in quotes}),
        "quotes_read": len(quotes),
        "favorites": 0,
        "fee_ok": 0,
        "stable": 0,
        "confirmed": 0,
        "kept": 0,
        "bought": 0,
    }
    rows: list[_Row] = []
    eligible: list[Quote] = []

    for quote in quotes:
        if not quote_in_band(quote, params):
            continue
        counts["favorites"] += 1
        problem = _structural(quote, params, now)
        if problem is not None:
            code, detail = problem
            rows.append(_Row(quote, code, detail, group=quote.event_id or quote.key))
            continue
        _ok, per_share, profit, fee_detail = _fee_ok(quote, params)
        counts["fee_ok"] += 1
        streak = book.streaks.get(quote.key, 0)
        if streak < params.min_stable_scans:
            need = params.min_stable_scans - streak
            detail = (
                f"Watching. The quote has been in band for {streak} scan{'s' if streak != 1 else ''}. "
                f"It needs {need} more before a buy. {fee_detail}"
            )
            rows.append(_Row(quote, "stability", detail, edge=-per_share, group=quote.event_id or quote.key))
            continue
        counts["stable"] += 1
        eligible.append(quote)

    signals: dict[str, Proposal] = {}
    for quote in eligible:
        if quote.key in signals:
            continue
        learned = _learned_signal(quote, params, book)
        if learned is not None:
            signals[quote.key] = learned

    counts["confirmed"] = len(signals)
    ordered = sorted(signals.values(), key=lambda item: (-item.edge, item.quote.end_time or datetime.max.replace(tzinfo=timezone.utc)))

    drawdown_hit = book.peak > 0 and (book.peak - book.equity) / book.peak >= params.max_drawdown - 1e-12
    paused = book.operator_pause or drawdown_hit
    pause_detail = (
        "New buys are paused by the operator. Exits still run."
        if book.operator_pause
        else "New buys are paused. The book is under its peak by the pause line. Exits still run."
    )
    deployed = book.deployed
    deploy_cap = book.equity * params.max_deployed_fraction
    cat_deployed: dict[str, float] = defaultdict(float)
    for position in book.positions:
        cat_deployed[position.category] += position.cost_basis
    held_keys = {f"{p.venue}:{p.market_id}:{p.side}" for p in book.positions}
    accepted: list[Proposal] = []
    accepted_quotes: list[Quote] = []

    signal_keys = set(signals)
    for quote in eligible:
        if quote.key in signal_keys:
            continue
        _ok, per_share, _profit, fee_detail = _fee_ok(quote, params)
        detail = (
            f"{fee_detail} If this quote is the true chance, the expected result is a loss of "
            f"{_cents(per_share)} a share to the fee. No settled record says it is cheap."
        )
        rows.append(_Row(quote, "edge", detail, edge=-per_share, group=quote.event_id or quote.key))

    proposals: list[Proposal] = []
    for proposal in ordered:
        quote = proposal.quote
        if quote.key in book.blocked:
            rows.append(_Row(quote, "block", "The operator blocked this contract.", edge=proposal.edge))
            continue
        if paused:
            rows.append(_Row(quote, "drawdown", pause_detail, edge=proposal.edge))
            continue
        if quote.key in held_keys:
            rows.append(_Row(quote, "correlation", "This contract is already in the book.", edge=proposal.edge))
            continue
        twin = _twin_of(quote, accepted_quotes, book, params)
        if twin:
            rows.append(_Row(quote, "correlation", twin, edge=proposal.edge))
            continue
        fits, cost, why = _clip_fits(quote, params, book.equity)
        if not fits:
            rows.append(_Row(quote, "size", why, edge=proposal.edge))
            continue
        if deployed + cost > deploy_cap + 1e-9:
            rows.append(_Row(
                quote, "budget",
                f"Buying this would put ${deployed + cost:.2f} to work. The deployed cap is ${deploy_cap:.2f}.",
                edge=proposal.edge,
            ))
            continue
        if cat_deployed[quote.category] + cost > book.equity * params.max_category_fraction + 1e-9:
            rows.append(_Row(
                quote, "budget",
                f"{quote.category} would go past the category cap.",
                edge=proposal.edge,
            ))
            continue
        if len(proposals) >= params.max_new_per_cycle:
            rows.append(_Row(quote, "budget", "This scan already has its full set of new buys.", edge=proposal.edge))
            continue
        if cost > book.cash + 1e-9:
            rows.append(_Row(quote, "budget", "Cash is not enough for the venue minimum.", edge=proposal.edge))
            continue
        proposals.append(proposal)
        accepted_quotes.append(quote)
        deployed += cost
        cat_deployed[quote.category] += cost
        held_keys.add(quote.key)
        rows.append(_Row(quote, "buy", proposal.detail, edge=proposal.edge, proposal=proposal))

    counts["kept"] = len(proposals)
    decisions = _collapse(rows)
    focus = _focus(rows)
    return Evaluation(decisions=decisions, proposals=proposals, counts=counts, focus=focus)


def _twin_of(quote: Quote, accepted: list[Quote], book: BookView, params: StrategyParams) -> str:
    others = [(item, "an order already chosen this scan") for item in accepted]
    others += [(item, "an open trade") for item in book.positions]
    for other, where in others:
        if other.venue == quote.venue and other.event_id and other.event_id == quote.event_id:
            return f"Same event as {where}: {other.title}."
        score = related(
            quote.title, quote.outcome, quote.event_id, quote.venue,
            other.title, other.outcome, other.event_id, other.venue,
        )
        if score + 1e-12 >= params.correlation_threshold:
            return f"Too close to {where}: {other.title}."
    return ""


def _collapse(rows: list[_Row]) -> list[Decision]:
    singles: list[Decision] = []
    buckets: dict[tuple[str, str, str], list[_Row]] = defaultdict(list)
    for row in rows:
        if row.code == "buy" or not row.group:
            singles.append(_decision(row))
            continue
        buckets[(row.quote.venue, row.group, row.code)].append(row)
    grouped: list[Decision] = []
    for (_venue, _group, _code), items in buckets.items():
        if len(items) == 1:
            grouped.append(_decision(items[0]))
            continue
        sample = items[0]
        title = sample.quote.event_title or sample.quote.title
        grouped.append(Decision(
            action="skipped",
            reason_code=sample.code,
            title=title,
            venue=sample.quote.venue,
            outcome=f"{len(items)} contracts",
            detail=sample.detail,
            event_id=sample.quote.event_id,
            event_title=sample.quote.event_title,
            price=sample.quote.ask,
            edge=sample.edge,
            group_count=len(items),
            key=sample.quote.key,
        ))
    order = {
        "buy": 0, "block": 1, "veto": 2, "stability": 3,
        "edge": 4, "fee": 5, "horizon": 6, "correlation": 7, "budget": 8,
        "size": 9, "liquidity": 10, "book": 11, "drawdown": 12, "tightened": 13,
    }
    decisions = singles + grouped
    decisions.sort(key=lambda item: (order.get(item.reason_code, 9), -(item.edge or -1), -item.group_count))
    return decisions[:80]


def _decision(row: _Row) -> Decision:
    action = "bought" if row.code == "buy" else "watching" if row.code == "stability" else "skipped"
    return Decision(
        action=action,
        reason_code=row.code,
        title=row.quote.title,
        venue=row.quote.venue,
        outcome=row.quote.outcome,
        detail=row.detail,
        event_id=row.quote.event_id,
        event_title=row.quote.event_title,
        price=row.quote.ask,
        edge=row.edge,
        key=row.quote.key,
    )


def _focus(rows: list[_Row]) -> Decision | None:
    if not rows:
        return None
    buys = [row for row in rows if row.code == "buy"]
    if buys:
        return _decision(max(buys, key=lambda row: row.edge or 0))
    edges = [row for row in rows if row.code == "edge"]
    if edges:
        return _decision(max(edges, key=lambda row: row.edge if row.edge is not None else -1))
    watches = [row for row in rows if row.code == "stability"]
    if watches:
        return _decision(watches[0])
    fees = [row for row in rows if row.code == "fee"]
    if fees:
        return _decision(fees[0])
    return _decision(rows[0])


def drop_proposals(
    proposals: list[Proposal],
    drop_ids: dict[str, str],
    reason_code: str = "correlation",
) -> tuple[list[Proposal], list[Decision]]:
    kept = []
    dropped = []
    for proposal in proposals:
        reason = drop_ids.get(proposal.key)
        if not reason:
            kept.append(proposal)
            continue
        dropped.append(Decision(
            action="skipped",
            reason_code=reason_code,
            title=proposal.quote.title,
            venue=proposal.quote.venue,
            outcome=proposal.quote.outcome,
            detail=reason,
            event_id=proposal.quote.event_id,
            event_title=proposal.quote.event_title,
            price=proposal.quote.ask,
            edge=proposal.edge,
            key=proposal.key,
        ))
    return kept, dropped


def horizon_end(now: datetime, hours: float) -> datetime:
    return now + timedelta(hours=hours)
