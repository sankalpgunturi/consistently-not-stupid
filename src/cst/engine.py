"""One scan: read both books, admit a handful of clips, mark the rest, write the note."""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta, timezone

from cst.broker import PaperBroker
from cst.config import Settings
from cst.decisions import veto_proposals
from cst.depth import live_depth
from cst.models import PARAM_COPY, RAILS, Decision, Quote, StrategyParams
from cst.review import Reviewer, govern, heuristic_summary, heuristic_updates, merge_suggestions, tighten_value
from cst.simulate import run_report
from cst.store import Store
from cst.strategy import drop_proposals, evaluate, price_bucket, quote_in_band
from cst.venues.kalshi import fetch_kalshi, fetch_kalshi_ticker
from cst.venues.polymarket import fetch_polymarket, fetch_polymarket_market

log = logging.getLogger("cst.engine")


def default_fetch(settings: Settings) -> tuple[list[Quote], list[str]]:
    quotes: list[Quote] = []
    errors: list[str] = []
    poly, err = fetch_polymarket(settings.polymarket_gamma_url, settings.polymarket_pages, settings.polymarket_page_size)
    quotes.extend(poly)
    if err:
        errors.append(err)
    kalshi, err = fetch_kalshi(settings.kalshi_base_url, settings.kalshi_pages, settings.kalshi_page_size)
    quotes.extend(kalshi)
    if err:
        errors.append(err)
    return quotes, errors


class Engine:
    def __init__(self, settings: Settings, store: Store | None = None, fetcher=None, reviewer: Reviewer | None = None, depth=None, decider=None):
        self.settings = settings
        self.store = store or Store(settings.db_path, settings.seed_params(), float(settings.bankroll))
        self.broker = PaperBroker(self.store)
        self.fetcher = fetcher or default_fetch
        self.reviewer = reviewer or Reviewer(settings.openai_api_key, settings.openai_model)
        self.depth = depth
        self.decider = decider
        self.refresher = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._next_scan = datetime.now(timezone.utc)
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
        while not self._stop.is_set():
            try:
                self.run_cycle()
            except Exception:
                log.exception("scan failed")
                self.store.set_status("error", {"error": "The last scan failed. The book was not changed by a fill it did not record."})
            params = self.store.params()
            self._next_scan = datetime.now(timezone.utc) + timedelta(seconds=params.scan_interval_seconds)
            self.store.set_status(self.store.status() if self.store.status() != "scanning" else "watching", self.store.cycle_info() | {"next_scan_at": self._next_scan.isoformat(timespec="seconds")})
            deadline = time.time() + params.scan_interval_seconds
            while time.time() < deadline and not self._stop.is_set():
                if self._wake.wait(timeout=min(params.mark_interval_seconds, max(0.1, deadline - time.time()))):
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
            self.store.set_status("scanning")
            started = time.time()
            params = self.store.params()
            quotes, errors = self.fetcher(self.settings)
            cycle = self.store.next_cycle()
            streaks = self.store.observe(cycle, [q.key for q in quotes if quote_in_band(q, params)])
            book = self.store.book(streaks)
            result = evaluate(quotes, params, book)
            veto = self._veto(result.proposals)
            kept, vetoed = drop_proposals(result.proposals, veto, reason_code="veto")
            drops, suggestions, model_summary = self.reviewer.review(params, kept, result.counts, book.settlements)
            kept, dropped_decisions = drop_proposals(kept, drops)
            decisions = [item for item in result.decisions if item.action != "bought"]
            decisions = vetoed + dropped_decisions + decisions
            bought = 0
            filled: set[str] = set()
            for proposal in kept:
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
            self.mark_open(locked=True)
            settlements = self.store.settlements()
            heuristic = heuristic_updates(params, settlements)
            merged = merge_suggestions(heuristic, suggestions)
            updated, notes, applied = govern(params, merged, set(heuristic), settlements)
            if applied:
                self.store.save_params(updated)
                self.store.append_audit(
                    "governor",
                    "tighten",
                    {key: getattr(params, key) for key in applied},
                    applied,
                    "Governor tightened after the settled record.",
                )
            summary = model_summary or heuristic_summary(result.counts, bought)
            if model_summary and not self.reviewer.enabled:
                summary = model_summary
            retro = {
                "summary": summary,
                "applied": applied,
                "notes": notes,
                "source": "openai" if self.reviewer.enabled and (model_summary or suggestions or drops) else "governor",
            }
            self.store.add_retro(retro)
            result.counts["bought"] = bought
            self.store.save_decisions(cycle, decisions[:80])
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
            self._next_scan = datetime.fromisoformat(info["next_scan_at"])
            paused = self._paused(updated)
            self.store.set_status("paused" if paused else "watching", info)
            return self.snapshot()
        finally:
            self._lock.release()

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
                    trade = self.broker.settle(position, won)
                    self._record_settlement(position, won, trade.pnl)
                    continue
                if quote.bid > 0:
                    self.broker.mark(position, quote.bid)
                if position.opened_cycle == self.store.cycle():
                    continue
                if quote.bid > 0 and quote.bid <= position.entry_price - params.stop_gap:
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
        ))

    def _refresh(self, venue: str, market_id: str, side: str) -> Quote | None:
        if self.refresher is not None:
            return self.refresher(venue, market_id, side)
        if venue == "polymarket":
            quotes = fetch_polymarket_market(self.settings.polymarket_gamma_url, market_id)
        elif venue == "kalshi":
            quotes = fetch_kalshi_ticker(self.settings.kalshi_base_url, market_id)
        else:
            return None
        for quote in quotes:
            if quote.side == side:
                return quote
        return quotes[0] if quotes else None

    def check_depth(self, quote: Quote, shares: float, action: str):
        if self.depth is not None:
            return self.depth(quote, shares, action)
        return live_depth(self.settings, quote, shares, action)

    def _veto(self, proposals) -> dict[str, str]:
        if self.decider is not None:
            return self.decider(proposals)
        return veto_proposals(proposals, self.settings.openai_api_key)

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

    def approve_pair(self, pair_id: str, note: str) -> dict:
        self.store.approve_pair(pair_id, note)
        return self.snapshot()

    def tighten(self, key: str) -> tuple[dict, str | None]:
        params = self.store.params()
        new = tighten_value(params, key)
        if new is None:
            return self.snapshot(), "That knob cannot be tightened."
        old = getattr(params, key)
        data = params.to_json()
        data[key] = new
        self.store.save_params(StrategyParams.from_json(data))
        self.store.append_audit("operator", "tighten", {key: old}, {key: new}, "The operator tightened one step.")
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
        retros = self.store.retros(6)
        return {
            "name": "Consistently not Stupid",
            "mode": "paper",
            "live": "unavailable",
            "csrf": self.store.csrf(),
            "paper_started_at": started,
            "paper_age_days": round(age_days, 2),
            "operator_pause": operator_pause,
            "approved_pairs": sorted(self.store.approved_pairs()),
            "status": "paused" if paused and self.store.status() != "scanning" else self.store.status(),
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
