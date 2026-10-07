"""One scan: read the Kalshi book, admit a handful of clips, mark the rest, write the note."""

from __future__ import annotations

import logging
import json
import hashlib
import threading
import time
from collections import Counter
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cst.broker import PaperBroker
from cst.benchmark import refresh_benchmark, snapshot as benchmark_snapshot
from cst.config import Settings
from cst.decisions import veto_proposals
from cst.depth import DepthResult, live_depth
from cst.daily import current_day, evaluate_days, save_reviews, rolling_day
from cst.models import PARAM_COPY, RAILS, Decision, Quote, StrategyParams
from cst.review import Reviewer, govern, heuristic_summary, heuristic_updates, merge_suggestions, tighten_value
from cst.simulate import run_report
from cst.store import Store
from cst.strategy import drop_proposals, evaluate, price_bucket, quote_in_band, tightened_out, wilson_lower
from cst.venues.kalshi import fetch_kalshi, fetch_kalshi_ticker, fetch_settled_record

log = logging.getLogger("cst.engine")


def refusal_counts(decisions: list[Decision]) -> dict[str, int]:
    """Count contracts, including grouped display rows, rather than UI rows."""
    counts: Counter = Counter()
    for item in decisions:
        if item.action != "bought":
            counts[item.reason_code] += max(1, item.group_count)
    return dict(counts)


def discovery_windows(now: float, min_hours: float, max_days: float) -> list[tuple[int, int]]:
    lower = int(now + min_hours * 3600)
    upper = int(now + max_days * 86400)
    boundaries = [int(now + day * 86400) for day in (1, 7) if lower < now + day * 86400 < upper]
    windows = []
    for boundary in boundaries + [upper]:
        if boundary > lower:
            windows.append((lower, boundary))
            # Overlap one second; discovery deduplicates tickers at boundaries.
            lower = boundary - 1
    return windows


def default_fetch(settings: Settings) -> tuple[list[Quote], list[str]]:
    now = datetime.now(timezone.utc).timestamp()
    windows = discovery_windows(now, settings.min_hours_to_expiry, settings.max_days_to_expiry)
    if settings.entry_window_minutes > 0:
        # Close-time is a discovery hint only; entry requires expected resolution.
        windows = [(int(now), int(now + 600)), (int(now + 600), int(now + 3600)),
                   (int(now + 3600), int(now + 86400)), None]
    quotes, err = fetch_kalshi(settings.kalshi_base_url, settings.kalshi_pages, settings.kalshi_page_size, close_windows=windows, priority_page_size=1000 if settings.entry_window_minutes > 0 else None)
    return quotes, [err] if err else []


def default_history(
    settings: Settings,
    skip: set[str],
    hours: float,
    resume: dict[str, dict] | None = None,
) -> tuple[dict[str, dict] | None, str | None]:
    return fetch_settled_record(
        settings.kalshi_base_url,
        settings.kalshi_pages,
        settings.kalshi_page_size,
        hours,
        skip,
        resume,
    )


def quote_on_side(quotes: list[Quote], side: str) -> Quote | None:
    """The quote for this side, or nothing. The other side's price is not a mark."""
    for quote in quotes:
        if quote.side == side:
            return quote
    return None


def _shown_status(raw: str, paused: bool) -> str:
    if raw in {"scanning", "error"}:
        return raw
    if paused:
        return "paused"
    return raw


class Engine:
    def __init__(self, settings: Settings, store: Store | None = None, fetcher=None, reviewer: Reviewer | None = None, depth=None, decider=None, history=None):
        self.settings = settings
        source = Path(__file__).resolve().parent
        digest = hashlib.sha256()
        for path in sorted(source.rglob("*.py")):
            digest.update(str(path.relative_to(source)).encode())
            digest.update(path.read_bytes())
        self.source_sha256 = digest.hexdigest()
        self.store = store or Store(settings.db_path, settings.seed_params(), float(settings.bankroll))
        self.broker = PaperBroker(self.store)
        self.fetcher = fetcher or default_fetch
        # A test that injects quotes should not also call the public settled feed.
        # Production leaves both empty and seeds the record from Kalshi.
        if history is not None:
            self.history = history
        elif fetcher is None:
            self.history = default_history
        else:
            self.history = None
        self.reviewer = reviewer or Reviewer(settings.openai_api_key, settings.openai_model)
        self.depth = depth
        self.decider = decider
        self.refresher = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._next_scan = datetime.now(timezone.utc)
        saved_next = self.store.cycle_info().get("next_scan_at")
        if saved_next:
            try:
                self._next_scan = datetime.fromisoformat(saved_next)
            except (TypeError, ValueError):
                pass
        self._report: dict | None = None
        self._sim_started = False
        self._report_lock = threading.Lock()

    def start(self) -> None:
        self.kick_simulation()
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="cst-scan", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=2)

    def request_scan(self) -> None:
        self._wake.set()

    def kick_simulation(self) -> None:
        if self._sim_started:
            return
        self._sim_started = True
        threading.Thread(target=self._build_simulation, name="cst-sim", daemon=True).start()

    def _build_simulation(self) -> None:
        report = run_report()
        with self._report_lock:
            self._report = report

    def simulation(self) -> dict | None:
        self.kick_simulation()
        with self._report_lock:
            return self._report

    def _loop(self) -> None:
        # Restarting to load a fix must not manufacture another stable scan.
        self._wait_for_scan()
        while not self._stop.is_set():
            try:
                self.run_cycle()
            except Exception:
                log.exception("scan failed")
                info = dict(self.store.cycle_info())
                info["errors"] = ["The last scan failed. The book was not changed by a fill it did not record."]
                self.store.set_status("error", info)
            params = self.store.params()
            self._next_scan = datetime.now(timezone.utc) + timedelta(seconds=params.scan_interval_seconds)
            self.store.set_status(self.store.status() if self.store.status() != "scanning" else "watching", self.store.cycle_info() | {"next_scan_at": self._next_scan.isoformat(timespec="seconds")})
            self._wait_for_scan()

    def _wait_for_scan(self) -> None:
        delay = max(0, (self._next_scan - datetime.now(timezone.utc)).total_seconds())
        deadline = time.monotonic() + delay
        while time.monotonic() < deadline and not self._stop.is_set():
            interval = self.store.params().mark_interval_seconds
            if self._wake.wait(timeout=min(interval, max(0.1, deadline - time.monotonic()))):
                self._wake.clear()
                break
            try:
                self.mark_open()
            except Exception:
                log.exception("mark failed")

    def run_cycle(self) -> dict:
        if not self._lock.acquire(blocking=False):
            return {"status": "busy"}
        try:
            try:
                return self._run_locked()
            except Exception:
                log.exception("scan failed")
                info = dict(self.store.cycle_info())
                info["errors"] = ["The last scan failed. The book was not changed by a fill it did not record."]
                info["finished_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
                self.store.set_status("error", info)
                return self.snapshot()
        finally:
            self._lock.release()

    def _run_locked(self) -> dict:
        self.store.set_status("scanning")
        started = time.time()
        params = self.store.params()
        if self.fetcher is default_fetch:
            refresh_benchmark(self.store)
        fetch_settings = self.settings.model_copy(update={"entry_window_minutes": params.entry_window_minutes})
        quotes, errors = self.fetcher(fetch_settings)
        retrieved_at = datetime.now(timezone.utc)
        if params.entry_window_minutes > 0:
            from cst.near_resolution import observe_and_resolve
            history_error = observe_and_resolve(self.store, self.settings, quotes, params, retrieved_at,
                                               resolve=self.history is not None)
        else:
            history_error = self._refresh_history()
        if history_error:
            errors.append(history_error)
        cycle = self.store.next_cycle()
        streaks = self.store.observe(cycle, [q.key for q in quotes if quote_in_band(q, params)])
        book = self.store.book(streaks)
        evaluated_at = datetime.now(timezone.utc)
        days = evaluate_days(self.store, evaluated_at)
        result = evaluate(quotes, params, book, now=evaluated_at)
        self._veto_usage = None
        veto = self._veto(result.proposals)
        kept, vetoed = drop_proposals(result.proposals, veto, reason_code="veto")
        if isinstance(self.reviewer, Reviewer):
            self.reviewer.context = {
                "phase": "before fills",
                "equity": book.equity, "cash": book.cash,
                "positions": [item.to_json() for item in book.positions],
                "recent_trades": [item.to_json() for item in self.store.trades(20)],
                "calibration": book.calibration,
                "strategy_thesis": "90%+ favorites, outcome expected within ten minutes, small unrelated bets; net portfolio performance after fees should be nonnegative over each 24-hour period, with a fixed $1,000 contribution and proceeds available for reinvestment. Individual losses are allowed; long-term capital preservation is a target, not a guarantee.",
                "daily_evaluations": days,
                "daily_review_instruction": "Review each pending 24-hour evaluation, especially negative days. Distinguish execution bugs, fees, correlated exposure, miscalibration and ordinary variance. Examine archived day evidence before suggesting changes; no automatic loosening or capital top-ups. A flat day without trades does not validate the strategy.",
                "near_resolution_evidence": self.store.research_summary()["near_resolution"],
                "calibration_source": "Prospective first eligible quote per event in the entry window; independent from old two-hour history. Different events may still correlate." if params.entry_window_minutes > 0 else "Legacy pre-close trade history",
                "venue_errors": errors,
                "refusals": refusal_counts(result.decisions),
                "refusal_counts_are_disjoint": True,
                "stage_definitions": {
                    "favorites": "Quotes meeting bid, ask, and spread rules.",
                    "fee_ok": "Favorites passing fee, size, liquidity, and horizon checks.",
                    "stable": "fee_ok quotes observed for the required consecutive scans.",
                    "confirmed": "Stable quotes whose historical sample and lower confidence bound pass.",
                    "kept": "Confirmed quotes selected by portfolio and correlation limits before model vetoes.",
                    "edge": "Historical lower confidence bound minus ask and fee; null if not evaluated or sample too small.",
                },
                "sample_refusals": [item.to_json() for item in result.decisions if item.action != "bought"][:20],
                "agent_reviews": [row["reason"] for row in self.store.audit(12) if row["actor"] == "codex"][:3],
            }
        review_due, review_signature = self._review_due(params, result.proposals, book, days, errors, evaluated_at)
        if review_due:
            drops, suggestions, model_summary = self.reviewer.review(params, kept, result.counts, book.settlements)
            if isinstance(self.reviewer, Reviewer) and self.reviewer.enabled and not self.reviewer.last_error:
                with self.store.lock:
                    self.store._put("last_model_review", {"at": datetime.now(timezone.utc).isoformat(), "signature": review_signature})
                    self.store._commit()
        else:
            drops, suggestions, model_summary = {}, {}, ""
            self.reviewer.last_error = None
            self.reviewer.last_usage = None
            self.reviewer.concerns = []
        if getattr(self.reviewer, "last_error", None):
            errors.append(self.reviewer.last_error)
        save_reviews(self.store, days, model_summary or "Model review unavailable; deterministic daily metrics saved.",
                     getattr(self.reviewer, "concerns", []),
                     getattr(self.reviewer, "last_error", None) or (None if model_summary else "No model summary"))
        kept, dropped_decisions = drop_proposals(kept, drops)
        decisions = [item for item in result.decisions if item.action != "bought"]
        decisions = vetoed + dropped_decisions + decisions
        bought = 0
        filled: set[str] = set()
        for proposal in kept:
            if params.entry_window_minutes > 0:
                refreshed = self._refresh(proposal.quote.venue, proposal.quote.market_id, proposal.quote.side)
                if refreshed is None:
                    decisions.append(Decision("skipped", "book", proposal.quote.title,
                        proposal.quote.venue, proposal.quote.outcome, "Could not refresh the quote after model review.", key=proposal.key))
                    continue
                proposal.quote = refreshed
            depth = self.check_depth(proposal.quote, proposal.shares, "buy")
            if not depth.ok:
                decisions.append(Decision(
                    action="skipped",
                    reason_code="book",
                    title=proposal.quote.title,
                    venue=proposal.quote.venue,
                    outcome=proposal.quote.outcome,
                    detail=depth.detail,
                    price=proposal.quote.ask,
                    edge=proposal.edge,
                    key=proposal.key,
                ))
                continue
            # Pause and block can arrive while the model or the book read is in flight.
            refusal = self._operator_refuses(proposal)
            if refusal is not None:
                decisions.append(refusal)
                continue
            fresh = self.store.params()
            live = self.store.book(streaks)
            why = tightened_out(proposal.quote, fresh, live, already_bought=bought)
            if why:
                decisions.append(Decision(
                    action="skipped",
                    reason_code="tightened",
                    title=proposal.quote.title,
                    venue=proposal.quote.venue,
                    outcome=proposal.quote.outcome,
                    detail=why,
                    price=proposal.quote.ask,
                    edge=proposal.edge,
                    key=proposal.key,
                ))
                continue
            trade = self.broker.buy(proposal.quote, proposal.shares, proposal.signal, proposal.detail, cycle)
            if trade is None:
                decisions.append(Decision(
                    action="skipped",
                    reason_code="budget",
                    title=proposal.quote.title,
                    venue=proposal.quote.venue,
                    outcome=proposal.quote.outcome,
                    detail="Cash was short of the venue minimum by the time this clip was reached.",
                    price=proposal.quote.ask,
                    edge=proposal.edge,
                    key=proposal.key,
                ))
                continue
            bought += 1
            filled.add(proposal.key)
        buys = [item for item in result.decisions if item.action == "bought" and item.key in filled]
        decisions = buys + decisions
        if result.focus and result.focus.action == "bought" and result.focus.key not in filled:
            result.focus = next((item for item in decisions if item.key == result.focus.key), None)
        self.mark_open(locked=True)
        settlements = self.store.settlements()
        seen = self.store.governor_seen()
        prior: dict[str, float] = {}

        def revise(current):
            # Model suggestions use the same settlement gate as the heuristic.
            # An unchanged book does not ratchet, even when the model asks for 0.99.
            if len(settlements) <= seen:
                return current, [], {}
            heuristic = heuristic_updates(current, settlements, seen)
            merged = merge_suggestions(heuristic, suggestions)
            revised, notes, applied_now = govern(current, merged, set(heuristic), settlements)
            prior.update({key: getattr(current, key) for key in applied_now})
            return revised, notes, applied_now

        updated, notes, applied = self.store.revise_params(revise)
        if applied:
            self.store.set_governor_seen(len(settlements))
            self.store.append_audit(
                "governor",
                "tighten",
                prior,
                applied,
                "Governor tightened after the settled record.",
            )
        summary = model_summary or heuristic_summary(result.counts, bought)
        retro = {
            "summary": summary,
            "applied": applied,
            "notes": notes,
            "source": "openai" if self.reviewer.enabled and (model_summary or suggestions or drops) else "governor",
            "concerns": getattr(self.reviewer, "concerns", []),
            "model_error": getattr(self.reviewer, "last_error", None),
            "model_review_deferred": not review_due,
            "cycle": cycle,
            "actual_bought": bought,
            "proposed_updates": suggestions,
            "model_usage": {
                "retrospective": {"model": getattr(self.reviewer, "model", ""), "usage": getattr(self.reviewer, "last_usage", None)},
                "correlation": {"model": "gpt-6-luna", "usage": self._veto_usage},
                "cost_usd": None,
                "note": "Provider token usage where available. API billing is separate from the paper trading ledger; missing usage or pricing is not zero cost.",
            },
            "settlements_seen": len(settlements),
        }
        retro["notes"].insert(0, f"Execution: {bought} paper buys; {len(settlements)} settlements recorded. Model commentary is a pre-fill review.")
        self.store.add_retro(retro)
        result.counts["bought"] = bought
        self.store.save_decisions(cycle, decisions)
        equity = self.store.mark_equity()
        self.store.note_peak(equity)
        self.store.add_equity(equity)
        info = {
            "number": cycle,
            "duration_seconds": round(time.time() - started, 2),
            "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "counts": result.counts,
            "errors": errors,
            "focus": result.focus.to_json() if result.focus else None,
            "next_scan_at": (datetime.now(timezone.utc) + timedelta(seconds=updated.scan_interval_seconds)).isoformat(timespec="seconds"),
        }
        observations = []
        for quote in quotes:
            if max(quote.bid, quote.ask) < params.min_probability:
                continue
            row = asdict(quote)
            row["key"] = quote.key
            row["end_time"] = quote.end_time.isoformat() if quote.end_time else None
            row["expected_resolution_time"] = quote.expected_resolution_time.isoformat() if quote.expected_resolution_time else None
            observations.append(row)
        self.store.save_scan(cycle, {
            **info, "params": params.to_json(), "params_after": updated.to_json(),
            "source_sha256": self.source_sha256,
            "model_usage": retro["model_usage"],
            "quotes_retrieved_at": retrieved_at.isoformat(), "evaluated_at": evaluated_at.isoformat(),
            "book_before": {
                "equity": book.equity, "cash": book.cash, "peak": book.peak, "deployed": book.deployed,
                "positions": [asdict(item) for item in book.positions],
                "streaks": book.streaks, "blocked": sorted(book.blocked), "operator_pause": book.operator_pause,
            },
            "equity": round(equity, 4), "cash": self.store.cash(),
            "calibration": book.calibration,
            "refusals": refusal_counts(decisions),
        }, observations)
        log.info("Scan %s: equity $%.2f, favorites %s, bought %s, errors %s", cycle, equity, result.counts.get("favorites", 0), bought, len(errors))
        self._next_scan = datetime.fromisoformat(info["next_scan_at"])
        paused = self._paused(updated)
        self.store.set_status("paused" if paused else "watching", info)
        return self.snapshot()

    def mark_open(self, locked: bool = False) -> None:
        if not locked and not self._lock.acquire(blocking=False):
            return
        try:
            params = self.store.params()
            for position in self.store.positions():
                quote = self._refresh(position.venue, position.market_id, position.side)
                if quote is None:
                    continue
                if quote.settled and quote.winner in {"yes", "no"}:
                    won = quote.winner == position.side
                    with self.store.transaction():
                        trade = self.broker.settle(position, won)
                        if trade is not None:
                            self._record_settlement(position, won, trade.pnl)
                    continue
                self.broker.mark(position, quote.bid)
                if 0 <= quote.bid <= position.entry_price - params.stop_gap:
                    depth = self.check_depth(quote, position.shares, "sell")
                    if not depth.ok:
                        continue
                    self.broker.sell(
                        position,
                        quote.bid,
                        f"The bid fell to {quote.bid:.2f}, {params.stop_gap * 100:.0f}¢ under the price we paid. The reason for the trade is gone.",
                    )
            equity = self.store.mark_equity()
            self.store.note_peak(equity)
            self.store.add_equity(equity)
        finally:
            if not locked:
                self._lock.release()

    def _record_settlement(self, position, won: bool, pnl: float) -> None:
        from cst.models import Settlement

        fee_per = position.fees / position.shares if position.shares else 0
        self.store.add_settlement(Settlement(
            bucket=price_bucket(position.entry_price),
            won=won,
            price=position.entry_price,
            fee_per_share=fee_per,
            pnl=pnl,
            market_id=position.market_id,
        ))

    def _review_due(self, params, proposals, book, days, errors, now):
        """Review exposure immediately; rate-limit unchanged empty-book commentary."""
        signature = json.dumps({"params": params.to_json(), "calibration": book.calibration,
                                "settlements": len(book.settlements), "errors": errors}, sort_keys=True)
        if not isinstance(self.reviewer, Reviewer) or proposals or book.positions:
            return True, signature
        if any(row.get("review_status") == "pending" for row in days):
            return True, signature
        with self.store.lock:
            last = self.store._get("last_model_review", {})
        if last.get("signature") != signature:
            return True, signature
        try:
            elapsed = (now - datetime.fromisoformat(last["at"])).total_seconds()
        except (KeyError, ValueError, TypeError):
            return True, signature
        return elapsed >= 600, signature

    def _refresh_history(self) -> str | None:
        if self.history is None:
            return None
        hours = float(self.store.params().min_hours_to_expiry)
        skip = self.store.venue_checked(hours) | self.store.settled_market_ids()
        resume = self.store.venue_resume(hours)
        record, err = self.history(self.settings, skip, hours, resume)
        if record:
            self.store.add_venue_samples(record)
        if err or record is None:
            return err or "Kalshi settled record: no sample."
        return None

    def _refresh(self, venue: str, market_id: str, side: str) -> Quote | None:
        if self.refresher is not None:
            return self.refresher(venue, market_id, side)
        if venue == "kalshi":
            quotes = fetch_kalshi_ticker(self.settings.kalshi_base_url, market_id)
        else:
            return None
        return quote_on_side(quotes, side)

    def _operator_refuses(self, proposal) -> Decision | None:
        quote = proposal.quote
        if self.store.operator_pause():
            detail = "New buys are paused by the operator. Exits still run."
            code = "drawdown"
        elif quote.key in self.store.blocked():
            detail = "The operator blocked this contract."
            code = "block"
        else:
            return None
        return Decision(
            action="skipped",
            reason_code=code,
            title=quote.title,
            venue=quote.venue,
            outcome=quote.outcome,
            detail=detail,
            price=quote.ask,
            edge=proposal.edge,
            key=quote.key,
        )

    def check_depth(self, quote: Quote, shares: float, action: str):
        if not quote.tradable:
            return DepthResult(False, 0, "The market is not open for trading.")
        if self.depth is not None:
            return self.depth(quote, shares, action)
        return live_depth(self.settings, quote, shares, action)

    def _veto(self, proposals) -> dict[str, str]:
        if self.decider is not None:
            return self.decider(proposals)
        return veto_proposals(proposals, self.settings.openai_api_key, usage_sink=lambda usage: setattr(self, "_veto_usage", usage))

    def set_pause(self, paused: bool) -> dict:
        old = self.store.operator_pause()
        self.store.set_pause(paused)
        self.store.append_audit(
            "operator",
            "pause",
            old,
            bool(paused),
            "New buys paused by the operator." if paused else "The operator resumed new buys.",
        )
        return self.snapshot()

    def block_market(self, key: str) -> dict:
        self.store.block(key, "The operator blocked this contract.")
        return self.snapshot()

    def tighten(self, key: str) -> tuple[dict, str | None]:
        change: dict[str, float] = {}

        def revise(current):
            new = tighten_value(current, key)
            if new is None:
                return current, [], {}
            data = current.to_json()
            change["old"] = data[key]
            change["new"] = new
            data[key] = new
            return StrategyParams.from_json(data), [], {key: new}

        _updated, _notes, applied = self.store.revise_params(revise)
        if not applied:
            return self.snapshot(), "That knob cannot be tightened."
        reason = "The operator tightened one step."
        if key == "min_hours_to_expiry":
            reason = (
                "The operator tightened nearest expiry. Stored trades are scored again at the new horizon. "
                "A trade counts only when it is at least that far before close. Markets that do not reach it are read again."
            )
        self.store.append_audit(
            "operator",
            "tighten",
            {key: change["old"]},
            {key: change["new"]},
            reason,
        )
        return self.snapshot(), None

    def close_position(self, position_id: str) -> tuple[bool, str]:
        with self._lock:
            position = next((item for item in self.store.positions() if item.id == position_id), None)
            if position is None:
                return False, "That trade is not open."
            quote = self._refresh(position.venue, position.market_id, position.side)
            if quote is None or quote.bid <= 0:
                return False, "The bid could not be read, so the clip stayed open."
            depth = self.check_depth(quote, position.shares, "sell")
            if not depth.ok:
                return False, depth.detail
            self.broker.sell(position, quote.bid, "Closed by the operator.")
            self.store.append_audit("operator", "close", position_id, "closed", "Manual paper close.")
            return True, ""

    def _paused(self, params: StrategyParams) -> bool:
        if self.store.operator_pause():
            return True
        book = self.store.book()
        if book.peak <= 0:
            return False
        return (book.peak - book.equity) / book.peak >= params.max_drawdown - 1e-12

    def snapshot(self) -> dict:
        params = self.store.params()
        positions = self.store.positions()
        equity = self.store.mark_equity(positions)
        peak = self.store.peak()
        drawdown = (peak - equity) / peak if peak else 0
        info = self.store.cycle_info()
        counts = info.get("counts") or {}
        drawdown_pause = peak > 0 and drawdown >= params.max_drawdown - 1e-12
        operator_pause = self.store.operator_pause()
        paused = drawdown_pause or operator_pause
        started = self.store.paper_started_at()
        age_days = 0.0
        if started:
            opened = datetime.fromisoformat(started)
            if opened.tzinfo is None:
                opened = opened.replace(tzinfo=timezone.utc)
            age_days = max(0.0, (datetime.now(timezone.utc) - opened).total_seconds() / 86400)
        settlements = self.store.settlements()
        resolved = len(settlements)
        hits = sum(1 for row in settlements if row.won)
        unrealized = sum(item.mark_value - item.cost_basis for item in positions)
        param_rows = []
        for key, (label, help_text) in PARAM_COPY.items():
            if params.entry_window_minutes > 0 and key in {"min_hours_to_expiry", "max_days_to_expiry"}:
                continue
            lo, hi = RAILS[key]
            param_rows.append({
                "key": key,
                "label": label,
                "help": help_text,
                "value": getattr(params, key),
                "min": lo,
                "max": hi,
                "adjustable": key != "scan_interval_seconds",
            })
        if params.entry_window_minutes > 0:
            param_rows.insert(0, {"key": "entry_window_minutes", "label": "Outcome within",
                "help": "Minutes until the venue expects the outcome; payout may come later.",
                "value": params.entry_window_minutes, "min": params.entry_window_minutes,
                "max": params.entry_window_minutes, "adjustable": False})
        retros = self.store.retros(6)
        research = self.store.research_summary()
        research["calibration"] = [
            {"bucket": bucket, "wins": wins, "samples": count, "lower_bound": wilson_lower(wins, count)}
            for bucket, (wins, count) in sorted(self.store.calibration().items())
        ]
        return {
            "daily": current_day(self.store, equity),
            "last_24h": rolling_day(self.store, equity),
            "benchmark": benchmark_snapshot(self.store, equity),
            "name": "Consistently Not Stupid",
            "mode": "paper",
            "live": "unavailable",
            "csrf": self.store.csrf(),
            "paper_started_at": started,
            "paper_age_days": round(age_days, 2),
            "operator_pause": operator_pause,
            "status": _shown_status(self.store.status(), paused),
            "server_time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "next_scan_at": info.get("next_scan_at") or self._next_scan.isoformat(timespec="seconds"),
            "book": {
                "start": float(self.settings.bankroll),
                "equity": round(equity, 2),
                "cash": round(self.store.cash(), 2),
                "deployed": round(sum(item.cost_basis for item in positions), 2),
                "realized": round(self.store.realized(), 2),
                "unrealized": round(sum(item.mark_value - item.cost_basis for item in positions), 2),
                "fees_paid": round(self.store.fees_paid(), 4),
                "peak": round(peak, 2),
                "drawdown": round(drawdown, 4),
                "entries_paused": paused,
                "drawdown_pause": drawdown_pause,
            },
            "evidence": {
                "pnl_after_fees": round(self.store.realized() + unrealized, 2),
                "resolved_count": resolved,
                "hit_rate": round(hits / resolved, 4) if resolved else None,
                "drawdown": round(drawdown, 4),
                "unresolved_cost": round(sum(item.cost_basis for item in positions), 2),
                "errors": info.get("errors") or [],
            },
            "audit": self.store.audit(12),
            "counts": counts,
            "tape": self.store.decisions(80),
            "focus": info.get("focus"),
            "positions": [item.to_json() for item in positions],
            "trades": [item.to_json() for item in self.store.trades(30)],
            "equity_curve": self.store.equity_curve(),
            "params": param_rows,
            "retrospective": retros[0] if retros else None,
            "retrospective_history": retros,
            "research": research,
            "cycle": {
                "number": info.get("number", self.store.cycle()),
                "duration_seconds": info.get("duration_seconds"),
                "finished_at": info.get("finished_at"),
            },
            "errors": info.get("errors") or [],
            "llm": {"enabled": self.reviewer.enabled, "model": self.reviewer.model if self.reviewer.enabled else ""},
            "simulation": self.simulation(),
        }

    def reset(self) -> dict:
        with self._lock:
            self.store.reset(self.settings.seed_params())
            self.store.set_status("watching", {"counts": {}, "errors": [], "focus": None, "number": 0})
            return self.snapshot()
