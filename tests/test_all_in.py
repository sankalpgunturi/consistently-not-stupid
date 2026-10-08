from datetime import timedelta
from dataclasses import asdict, replace
from types import SimpleNamespace

import pytest

from cst.config import Settings
from cst.engine import Engine
from cst.fees import fee_for
from cst.strategy import evaluate, shares_for_budget, tightened_out
from tests.conftest import NOW, make_book, make_params, make_quote


def setup(cash=945.19):
    q=make_quote(bid=.80,ask=.82,end_time=NOW+timedelta(minutes=4),expected_resolution_time=NOW+timedelta(minutes=4))
    p=make_params(entry_window_minutes=5,min_probability=.8,all_in=1)
    b=make_book(equity=cash,cash=cash,peak=1000,streaks={q.key:2})
    return q,p,b


@pytest.mark.parametrize('cash',[.5,1,10,945.19,1000])
def test_all_in_maximizes_whole_contracts_after_fees(cash):
    q,p,b=setup(cash)
    n=shares_for_budget(q,p,b.cash)
    cost=lambda count: count*q.ask+float(fee_for(q.fee_model,count,q.ask,q.fee_rate,q.fee_exponent))
    assert cost(n)<=cash+1e-9<cost(n+1)
    result=evaluate([q],p,b,now=NOW)
    if n:
        assert result.proposals[0].shares==n
        assert tightened_out(q,p,b,now=NOW,shares=n) is None
        assert tightened_out(q,p,b,now=NOW,shares=n-1) is not None
    else:
        assert not result.proposals


def test_all_in_only_one_proposal_and_waits_for_open_position():
    q,p,b=setup()
    q2=make_quote(market_id='other',event_id='other',title='A different outcome',category='Other',bid=.85,ask=.87,end_time=q.end_time,expected_resolution_time=q.expected_resolution_time)
    b.streaks[q2.key]=2
    assert len(evaluate([q,q2],p,b,now=NOW).proposals)==1
    assert tightened_out(q,p,b,now=NOW,already_bought=1) is not None
    b.positions=[SimpleNamespace(**asdict(q2), cost_basis=1)]
    assert not evaluate([q],p,b,now=NOW).proposals
    assert tightened_out(q,p,b,now=NOW) is not None


def test_all_in_keeps_depth_and_cash_rechecks_and_is_paper_only():
    q,p,b=setup()
    n=shares_for_budget(q,p,b.cash)
    q.ask_size=n-1
    assert not evaluate([q],p,b,now=NOW).proposals
    q.ask_size=-1
    b.cash-=10
    assert tightened_out(q,p,b,now=NOW,shares=n) is not None
    b.trading_mode='live'
    assert not evaluate([q],p,b,now=NOW).proposals
    assert tightened_out(q,p,b,now=NOW) is not None


def test_all_in_blocks_live_switch_and_operator_can_restore_fixed_size(tmp_path):
    engine=Engine(Settings(data_dir=str(tmp_path),openai_api_key=''),fetcher=lambda _: ([],[]))
    _,error=engine.set_control('all_in',1)
    assert error is None
    _,error=engine.arm_live(10)
    assert 'fixed bet sizing' in error
    assert engine.store.trading_mode()=='paper'
    _,error=engine.set_control('all_in',0)
    assert error is None
    assert engine.store.params().all_in==0


def test_engine_resizes_all_in_at_refreshed_price(tmp_path):
    from cst.depth import DepthResult
    q,p,b=setup(1000)
    engine=Engine(Settings(data_dir=str(tmp_path),openai_api_key='',all_in=1,min_stable_scans=1),fetcher=lambda _: ([q],[]))
    fresh=replace(q,ask=.81)
    engine.refresher=lambda *_: fresh
    checked=[]
    engine.depth=lambda quote,shares,action: checked.append(shares) or DepthResult(True,shares,'covered')
    state=engine.run_cycle()
    expected=shares_for_budget(fresh,engine.store.params(),1000)
    assert state['counts']['bought']==1
    assert state['positions'][0]['shares']==expected
    assert checked[0]==expected
    assert engine.store.cash()<fresh.ask+float(fee_for('kalshi',1,fresh.ask))
