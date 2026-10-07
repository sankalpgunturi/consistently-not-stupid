from datetime import timedelta, datetime, timezone

import pytest

from cst.config import Settings
from cst.models import StrategyParams
from cst.near_resolution import observe_and_resolve
from cst.store import Store
from cst.strategy import evaluate, tightened_out
from tests.conftest import NOW, make_quote, make_book, make_params


def test_new_books_target_five_minutes():
    params = Settings().seed_params()
    assert params.entry_window_minutes == 5
    assert params.min_probability == .8
    assert params.scan_interval_seconds == 1


@pytest.mark.parametrize('minutes,close_minutes,accepted', [(10,10,True),(5,60,True),(10.01,60,False),(0,60,False),(-1,60,False),(5,0,False),(None,5,False)])
def test_expected_outcome_and_open_trading_both_required(minutes,close_minutes,accepted):
    q = make_quote(end_time=NOW+timedelta(minutes=close_minutes), expected_resolution_time=None if minutes is None else NOW+timedelta(minutes=minutes))
    p = make_params(entry_window_minutes=10)
    b = make_book(streaks={q.key:2}, calibration={'0.93–0.96': (100,100)})
    result = evaluate([q],p,b,now=NOW)
    assert bool(result.proposals) == accepted
    if not accepted:
        assert result.decisions[0].reason_code == 'horizon'
    else:
        assert tightened_out(q,p,b,now=NOW+timedelta(minutes=11)) is not None


def test_prospective_evidence_keeps_first_event_quote_and_losses(tmp_path):
    p=StrategyParams(entry_window_minutes=10)
    store=Store(tmp_path/'book.sqlite',p,1000)
    store.set_venue_record({'0.93–0.96':(100,100)})
    assert store.calibration() == {}  # Legacy evidence cannot admit a new-regime buy.
    q=make_quote(expected_resolution_time=NOW+timedelta(minutes=5))
    twin=make_quote(market_id='m2',expected_resolution_time=NOW+timedelta(minutes=5))
    settings=Settings()
    observe_and_resolve(store,settings,[q,twin],p,NOW,resolve=False)
    observe_and_resolve(store,settings,[q],p,NOW+timedelta(minutes=1),resolve=False)
    assert store.conn.execute('SELECT COUNT(*) FROM near_observations').fetchone()[0] == 1
    class Http:
        def get_json(self,url,params):
            return {'markets':[{'ticker':'m1','result':'no'}]}
    observe_and_resolve(store,settings,[],p,NOW+timedelta(minutes=6),http=Http())
    assert store.calibration() == {'0.93–0.96':(0,1)}
    assert store.cash() == 1000
    assert store.research_summary()['near_resolution']['losing_outcomes'] == 1
    row=store.conn.execute('SELECT * FROM near_observations').fetchone()
    assert row['minutes_left'] == 5
    assert row['price'] == .94


def test_unknown_or_failed_outcomes_do_not_become_wins(tmp_path):
    p=StrategyParams(entry_window_minutes=10)
    store=Store(tmp_path/'book.sqlite',p,1000)
    q=make_quote(expected_resolution_time=NOW+timedelta(minutes=5))
    class Http:
        def get_json(self,*args,**kwargs):
            return {'markets':[{'ticker':'m1','result':''}]}
    observe_and_resolve(store,Settings(),[q],p,NOW,http=Http())
    assert store.calibration() == {}
    row=store.conn.execute('SELECT * FROM near_observations').fetchone()
    assert row['resolved_at'] is None
    assert row['checked_at'] is not None
    assert store.research_summary()['near_resolution']['quote_reference']['net_payoff'] is None


def test_research_payoff_includes_losses_and_fees_but_not_pending_or_cash(tmp_path):
    p = StrategyParams(entry_window_minutes=10)
    store = Store(tmp_path/'book.sqlite', p, 1000)
    quotes = [make_quote(market_id=f'm{i}', event_id=f'e{i}', bid=price-.01, ask=price,
                         expected_resolution_time=NOW+timedelta(minutes=5))
              for i, price in enumerate((.91, .94, .97))]
    class Http:
        def get_json(self, *args, **kwargs):
            return {'markets': [{'ticker': 'm0', 'result': 'no'},
                                {'ticker': 'm1', 'result': 'yes'},
                                {'ticker': 'm2', 'result': ''}]}
    observe_and_resolve(store, Settings(), quotes, p, NOW, http=Http())
    summary = store.research_summary()['near_resolution']
    assert summary['observed_events'] == 3
    assert summary['resolved_events'] == 2
    reference = summary['quote_reference']
    assert reference['cost'] == 1.87
    assert reference['payout'] == 1
    assert reference['net_payoff'] == -.87
    assert reference['buckets']['0.90–0.93']['net_payoff'] == -.92
    assert reference['buckets']['0.93–0.96']['net_payoff'] == .05
    assert '0.96–0.99' not in reference['buckets']
    assert store.cash() == 1000
    assert store.trades() == []


def test_near_engine_records_without_crediting_research_or_using_old_history(tmp_path):
    from cst.engine import Engine
    now=datetime.now(timezone.utc)
    q=make_quote(expected_resolution_time=now+timedelta(minutes=5),end_time=now+timedelta(minutes=6))
    engine=Engine(Settings(data_dir=str(tmp_path), entry_window_minutes=10),fetcher=lambda _:([q],[]))
    engine.store.set_venue_record({'0.93–0.96':(100,100)})
    engine.decider=lambda _:{}
    state=engine.run_cycle()
    assert state['errors'] == []
    assert state['counts']['bought'] == 0
    assert state['book']['cash'] == 1000
    assert state['research']['near_resolution']['observed_events'] == 1
    assert state['research']['calibration'] == []
    assert state['research']['recent_scans'][0]['params']['entry_window_minutes'] == 10


def test_near_engine_refreshes_quote_after_model_review(tmp_path):
    from cst.engine import Engine
    from cst.depth import DepthResult
    now=datetime.now(timezone.utc)
    q=make_quote(expected_resolution_time=now+timedelta(minutes=5),end_time=now+timedelta(minutes=6))
    p=StrategyParams(entry_window_minutes=10,min_stable_scans=1)
    store=Store(tmp_path/'book.sqlite',p,1000)
    # Independent synthetic events for an admission-path fixture, not live evidence.
    for i in range(100):
        other=make_quote(market_id=f'h{i}',event_id=f'e{i}',expected_resolution_time=now+timedelta(minutes=5))
        observe_and_resolve(store,Settings(),[other],p,now,resolve=False)
    store.conn.execute("UPDATE near_observations SET result='yes'")
    store.conn.commit()
    engine=Engine(Settings(data_dir=str(tmp_path),entry_window_minutes=10),store=store,fetcher=lambda _:([q],[]))
    engine.decider=lambda _:{}
    engine.depth=lambda _q,shares,_action:DepthResult(True,shares,'covered')
    engine.refresher=lambda *_:make_quote(expected_resolution_time=now-timedelta(seconds=1),end_time=now+timedelta(minutes=6))
    state=engine.run_cycle()
    assert state['counts']['confirmed'] == 1
    assert state['counts']['bought'] == 0
    assert any(row['reason_code']=='recheck' for row in state['tape'])


@pytest.mark.parametrize('status,tradable',[('active',True),('open',True),('paused',False),('',False)])
def test_market_status_survives_parser_and_blocks_paused_fills(tmp_path,status,tradable):
    from cst.venues.kalshi import quotes_from_kalshi_market
    from cst.engine import Engine
    from cst.depth import DepthResult
    market={'ticker':'TEST','event_ticker':'E','status':status,
            'yes_bid_dollars':'.93','yes_ask_dollars':'.94',
            'volume_fp':'100','open_interest_fp':'100',
            'close_time':(NOW+timedelta(minutes=6)).isoformat(),
            'expected_expiration_time':(NOW+timedelta(minutes=5)).isoformat()}
    quote=quotes_from_kalshi_market(market)[0]
    assert quote.tradable is tradable
    p=make_params(entry_window_minutes=10)
    b=make_book(streaks={quote.key:2},calibration={'0.93–0.96':(100,100)})
    assert bool(evaluate([quote],p,b,now=NOW).proposals) is tradable
    engine=Engine(Settings(data_dir=str(tmp_path)),fetcher=lambda _: ([],[]))
    engine.depth=lambda _q,n,_action:DepthResult(True,n,'size present')
    assert engine.check_depth(quote,1,'buy').ok is tradable
    assert engine.check_depth(quote,1,'sell').ok is tradable


@pytest.mark.parametrize("record", [{}, {"0.93–0.96": (0, 100)}, {"0.93–0.96": (1, 1)}])
def test_paper_entries_and_fill_recheck_do_not_require_history(record):
    q = make_quote(end_time=NOW+timedelta(minutes=5), expected_resolution_time=NOW+timedelta(minutes=5))
    p = make_params(entry_window_minutes=10)
    b = make_book(streaks={q.key:2}, calibration=record)
    result = evaluate([q], p, b, now=NOW)
    assert len(result.proposals) == 1
    assert result.proposals[0].signal == "paper_favorite"
    assert result.proposals[0].edge == 0  # No claimed statistical edge.
    assert tightened_out(q, p, b, now=NOW) is None
    b.operator_pause = True
    assert tightened_out(q, p, b, now=NOW) is not None


@pytest.mark.parametrize("winner", ["yes", "no"])
def test_paper_holds_through_price_drop_then_settles(tmp_path, winner):
    from cst.engine import Engine
    from cst.broker import PaperBroker
    engine = Engine(Settings(data_dir=str(tmp_path), entry_window_minutes=10), fetcher=lambda _: ([], []))
    q = make_quote()
    PaperBroker(engine.store).buy(q, 1, "paper_favorite", "test", 0)
    cash = engine.store.cash()
    engine.refresher = lambda *_: make_quote(bid=0.01, ask=0.02)
    engine.mark_open()
    assert len(engine.store.positions()) == 1
    assert engine.store.cash() == cash
    assert [t.action for t in engine.store.trades()] == ["buy"]
    engine.refresher = lambda *_: make_quote(settled=True, winner=winner)
    engine.mark_open()
    assert not engine.store.positions()
    assert engine.store.trades()[0].action == "settle"
    assert bool(engine.store.trades()[0].won) == (q.side == winner)


def test_fill_explanation_uses_refreshed_price_and_fee(tmp_path):
    from cst.engine import Engine
    from cst.depth import DepthResult
    now = datetime.now(timezone.utc)
    q = make_quote(bid=.94, ask=.95, end_time=now+timedelta(minutes=5),
                   expected_resolution_time=now+timedelta(minutes=5))
    engine = Engine(Settings(data_dir=str(tmp_path), min_stable_scans=1),
                    fetcher=lambda _: ([q], []), decider=lambda _: {})
    engine.depth = lambda _q, shares, _action: DepthResult(True, shares, 'covered')
    engine.refresher = lambda *_: make_quote(bid=.96, ask=.97,
        end_time=q.end_time, expected_resolution_time=q.expected_resolution_time)
    state = engine.run_cycle()
    assert state['counts']['bought'] == 1
    trade = engine.store.trades()[0]
    assert trade.price == .97
    assert trade.fee == .01
    assert 'Ask 97.0¢' in trade.reason
    assert '2.0¢ a share' in trade.reason
    assert engine.store.positions()[0].reason == trade.reason


def test_final_check_explains_quote_move_without_claiming_rule_change():
    q = make_quote(bid=.89, ask=.91, end_time=NOW+timedelta(minutes=5),
                   expected_resolution_time=NOW+timedelta(minutes=5))
    why = tightened_out(q, make_params(entry_window_minutes=10), make_book(streaks={q.key: 2}), now=NOW)
    assert 'bid 89.0%' in why and 'ask 91.0%' in why
    assert 'tightened' not in why
