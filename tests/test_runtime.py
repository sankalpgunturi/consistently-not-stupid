from types import SimpleNamespace
import pytest

from cst.config import Settings
from cst.engine import Engine
from cst.review import Reviewer
from cst.broker import PaperBroker
from cst.store import Store
from cst.models import StrategyParams
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
