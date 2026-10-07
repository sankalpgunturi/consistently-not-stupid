from types import SimpleNamespace
import pytest

from cst.config import Settings
from cst.engine import Engine
from cst.review import Reviewer
from cst.broker import PaperBroker
from cst.store import Store
from cst.models import StrategyParams
from cst.models import Decision
from cst.engine import refusal_counts, discovery_windows
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
    assert len(scan["source_sha256"]) == 64
    assert scan["book_before"]["cash"] == 1000
    assert scan["book_before"]["streaks"][quote.key] == 1
    assert scan["evaluated_at"] >= scan["quotes_retrieved_at"]
    assert scan["counts"]["bought"] == 0
    assert state["retrospective"]["actual_bought"] == 0
    assert state["retrospective"]["concerns"] == ["Small sample."]
    assert reviewer.context["phase"] == "before fills"
    assert reviewer.context["sample_refusals"]
    assert reviewer.context["refusal_counts_are_disjoint"] is True


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


def test_discovery_covers_near_medium_and_far_with_independent_cursors():
    calls = []
    market = {"ticker": "KXTEST", "status": "active", "yes_bid_dollars": "0.40", "yes_ask_dollars": "0.41"}
    windows = [(100, 200), (199, 300), (299, 400)]

    class Http:
        def get_json(self, _url, params=None):
            calls.append(dict(params))
            bucket = params["min_close_ts"]
            page = sum(c["min_close_ts"] == bucket for c in calls)
            assert params.get("cursor") == (f"{bucket}-{page-1}" if page > 1 else None)
            # A shared boundary ticker must appear only once in the result.
            return {"markets": [market, dict(market, ticker=f"M{bucket}-{page}")], "cursor": f"{bucket}-{page}"}

    quotes, error = fetch_kalshi("https://example.invalid", 8, 200, http=Http(), close_windows=windows)
    assert error is None
    assert len(calls) == 8
    assert [sum(c["min_close_ts"] == str(lo) for c in calls) for lo, _ in windows] == [4, 2, 2]
    assert len([q for q in quotes if q.market_id == "KXTEST"]) == 1


def test_discovery_window_boundaries_respect_configured_limits():
    windows = discovery_windows(0, 2, 21)
    assert windows == [(7200, 86400), (86399, 604800), (604799, 1814400)]
    assert discovery_windows(0, 2, 3) == [(7200, 86400), (86399, 259200)]


def test_review_sees_active_timing_and_explicit_side(monkeypatch):
    from datetime import timedelta
    from cst.models import StrategyParams, Proposal
    from tests.conftest import make_quote, NOW
    reviewer = Reviewer('test-key','test')
    seen = {}
    def complete(payload):
        seen.update(payload)
        return {'summary':'No change.'}
    monkeypatch.setattr(reviewer,'_complete',complete)
    q=make_quote(side='no',expected_resolution_time=NOW+timedelta(minutes=5))
    reviewer.review(StrategyParams(entry_window_minutes=10),[Proposal(q,1,.02,'learned','fixture','fixture')],{},[])
    assert seen['entry_timing']['entry_window_minutes']==10
    assert 'min_hours_to_expiry' not in seen['knobs']
    assert 'max_days_to_expiry' not in seen['knobs']
    assert seen['proposals'][0]['side']=='no'
    assert seen['proposals'][0]['expected_resolution_time']==q.expected_resolution_time.isoformat()


def test_review_preserves_provider_usage_without_inventing_cost(monkeypatch):
    usage={'prompt_tokens':120,'completion_tokens':30,'total_tokens':150}
    response=SimpleNamespace(usage=usage, choices=[SimpleNamespace(message=SimpleNamespace(content='{"summary":"Recorded"}'))])
    monkeypatch.setattr('openai.OpenAI',lambda **_:SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **_:response))))
    reviewer=Reviewer('test','test')
    reviewer.review(StrategyParams(),[],{},[])
    assert reviewer.last_usage==usage
    assert 'cost_usd' not in reviewer.last_usage
    reviewer.api_key=''
    reviewer.review(StrategyParams(),[],{},[])
    assert reviewer.last_usage is None


def test_empty_book_reviews_are_throttled_but_new_evidence_is_reviewed(tmp_path, monkeypatch):
    reviewer=Reviewer('test','test')
    calls=[]
    def complete(payload):
        calls.append(payload)
        reviewer.last_usage={'total_tokens':15}
        return {'summary':'No proposed buys.'}
    monkeypatch.setattr(reviewer,'_complete',complete)
    engine=Engine(Settings(data_dir=str(tmp_path)),fetcher=lambda _: ([],[]),reviewer=reviewer)
    first=engine.run_cycle()
    second=engine.run_cycle()
    assert len(calls)==1
    assert not first['retrospective']['model_review_deferred']
    assert second['retrospective']['model_review_deferred']
    assert second['retrospective']['model_usage']['retrospective']['usage'] is None
    from datetime import datetime,timedelta,timezone
    now=datetime.now(timezone.utc)
    params=engine.store.params();book=engine.store.book({})
    assert engine._review_due(params,[],book,[],[],now)[0] is False
    assert engine._review_due(params,[],book,[],[],now+timedelta(minutes=11))[0] is True
    assert engine._review_due(params,[object()],book,[],[],now)[0] is True
    assert engine._review_due(params,[],book,[{'review_status':'pending'}],[],now)[0] is True
    assert engine._review_due(params,[],book,[],['New venue failure'],now)[0] is True
    book.calibration={'0.93–0.96':(1,1)}
    assert engine._review_due(params,[],book,[],[],now)[0] is True


def test_report_keeps_research_payoffs_out_of_paper_ledger(tmp_path):
    from tools.paper_report import report
    from cst.near_resolution import observe_and_resolve
    from tests.conftest import NOW
    from datetime import timedelta
    params=StrategyParams(entry_window_minutes=10)
    store=Store(tmp_path/'book.sqlite',params,1000)
    quotes=[make_quote(market_id=f'm{i}',event_id=f'e{i}',expected_resolution_time=NOW+timedelta(minutes=5)) for i in range(3)]
    observe_and_resolve(store,Settings(),quotes,params,NOW,resolve=False)
    store.conn.execute("UPDATE near_observations SET result='yes' WHERE ticker='m0'")
    store.conn.execute("UPDATE near_observations SET result='no' WHERE ticker='m1'")
    store.conn.commit()
    result=report(store.path);research=result['research_reference']
    assert research['resolved_events']==2
    assert research['pending_events']==1
    assert research['wins']==1 and research['losses']==1
    assert research['quote_reference_payoff']==pytest.approx(-.90)
    assert research['recent_losses'][0]['ticker']=='m1'
    assert result['cash']==1000
    assert result['opened_trades']==0
    assert result['realized_pnl']==0
    assert all(result['ledger_checks'].values())
