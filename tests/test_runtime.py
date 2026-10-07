from types import SimpleNamespace
import pytest

from cst.config import Settings
from cst.engine import Engine
from cst.review import Reviewer
from cst.broker import PaperBroker
from cst.store import Store
from cst.models import StrategyParams
from cst.models import Decision
from cst.engine import refusal_counts
from cst.venues.kalshi import fetch_kalshi
from tests.conftest import make_quote


def test_retrospective_request_supports_default_temperature_models(monkeypatch):
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        assert "temperature" not in kwargs
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='{"summary":"Observed; no fills."}'))])

    monkeypatch.setattr("openai.OpenAI", lambda **_: SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))))
    reviewer = Reviewer("test-key", "gpt-6.1-sol")
    assert reviewer._complete({})["summary"] == "Observed; no fills."
    assert calls[0]["model"] == "gpt-6.1-sol"


def test_failed_review_is_visible_and_does_not_leak_credentials(tmp_path, monkeypatch):
    reviewer = Reviewer("secret-must-not-appear", "test")

    def fail(_payload):
        raise ValueError("secret-must-not-appear")

    monkeypatch.setattr(reviewer, "_complete", fail)
    engine = Engine(Settings(data_dir=str(tmp_path)), fetcher=lambda _: ([], []), reviewer=reviewer)
    state = engine.run_cycle()
    assert state["book"]["equity"] == 1000
    assert state["retrospective"]["source"] == "governor"
    assert "ValueError" in state["retrospective"]["model_error"]
    assert state["errors"]
    assert "secret-must-not-appear" not in str(state)


def test_scan_evidence_survives_restart_and_model_concerns_are_kept(tmp_path, monkeypatch):
    reviewer = Reviewer("test-key", "test")
    monkeypatch.setattr(reviewer, "_complete", lambda _: {"summary": "Need more observations.", "concerns": ["Small sample."]})
    settings = Settings(data_dir=str(tmp_path))
    quote = make_quote()
    engine = Engine(settings, fetcher=lambda _: ([quote], []), reviewer=reviewer)
    engine.run_cycle()
    reopened = Engine(settings, fetcher=lambda _: ([], []))
    state = reopened.snapshot()
    assert state["research"]["scans_recorded"] == 1
    assert state["research"]["observations"] == 1
    assert state["research"]["distinct_contract_sides"] == 1
    scan = state["research"]["recent_scans"][0]
    assert scan["params"]["min_probability"] == 0.90
    assert scan["counts"]["bought"] == 0
    assert state["retrospective"]["actual_bought"] == 0
    assert state["retrospective"]["concerns"] == ["Small sample."]
    assert reviewer.context["phase"] == "before fills"


def test_interrupted_fill_rolls_back_all_accounting(tmp_path, monkeypatch):
    store = Store(tmp_path / "book.sqlite", StrategyParams(), 1000)
    broker = PaperBroker(store)

    def fail(_trade):
        raise RuntimeError("disk write failed")

    monkeypatch.setattr(store, "add_trade", fail)
    with pytest.raises(RuntimeError):
        broker.buy(make_quote(), 1, "learned", "test", 1)
    reopened = Store(store.path, StrategyParams(), 1000)
    assert reopened.cash() == 1000
    assert reopened.fees_paid() == 0
    assert reopened.positions() == []
    assert reopened.trades() == []


def test_settlement_is_recorded_once_and_failure_restores_position(tmp_path, monkeypatch):
    engine = Engine(Settings(data_dir=str(tmp_path)), fetcher=lambda _: ([], []))
    quote = make_quote()
    engine.broker.buy(quote, 1, "learned", "test", 1)
    position = engine.store.positions()[0]
    quote.settled, quote.winner = True, "yes"
    engine.refresher = lambda *_: quote
    original = engine.store.add_settlement

    def fail(_row):
        raise RuntimeError("settlement write failed")

    monkeypatch.setattr(engine.store, "add_settlement", fail)
    cash = engine.store.cash()
    with pytest.raises(RuntimeError):
        engine.mark_open()
    assert engine.store.cash() == cash
    assert len(engine.store.positions()) == 1
    assert engine.store.realized() == 0
    assert len(engine.store.trades()) == 1
    monkeypatch.setattr(engine.store, "add_settlement", original)
    engine.mark_open()
    assert len(engine.store.settlements()) == 1
    assert engine.store.positions() == []
    assert engine.broker.settle(position, True) is None
    assert engine.broker.sell(position, 1, "already closed") is None
    assert engine.store.cash() == pytest.approx(cash + 1)


def test_market_discovery_filters_window_and_excludes_inactive_payloads():
    calls = []
    market = {"ticker": "KXTEST", "status": "active", "yes_bid_dollars": "0.40", "yes_ask_dollars": "0.41",
              "no_bid_dollars": "0.59", "no_ask_dollars": "0.60"}

    class Http:
        def get_json(self, _url, params=None):
            calls.append(params)
            return {"markets": [market, dict(market, ticker="CLOSED", status="closed"), dict(market, ticker="FUTURE", status="inactive")], "cursor": ""}

    quotes, error = fetch_kalshi("https://example.invalid", 8, 200, http=Http(), close_window=(100, 200))
    assert error is None
    assert {q.market_id for q in quotes} == {"KXTEST"}
    assert calls == [{"limit": "200", "mve_filter": "exclude", "min_close_ts": "100", "max_close_ts": "200"}]


def test_refusal_counts_include_contracts_collapsed_in_the_dashboard():
    grouped = Decision(action="skipped", reason_code="fee", title="Event", venue="kalshi", outcome="4 contracts", detail="Fee", group_count=4)
    assert refusal_counts([grouped, grouped]) == {"fee": 8}


def test_read_only_report_reconciles_a_completed_trade(tmp_path):
    from tools.paper_report import report
    store = Store(tmp_path / "book.sqlite", StrategyParams(), 1000)
    broker = PaperBroker(store)
    broker.buy(make_quote(), 1, "learned", "test", 1)
    broker.settle(store.positions()[0], True)
    result = report(store.path)
    assert all(result["ledger_checks"].values())
    assert result["opened_trades"] == 1
    assert result["settlements"] == 1
    assert result["realized_pnl"] == pytest.approx(0.05)
