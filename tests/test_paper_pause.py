from datetime import timedelta

import pytest

from cst.config import Settings
from cst.engine import Engine
from cst.strategy import evaluate, tightened_out
from tests.conftest import NOW, make_book, make_params, make_quote


@pytest.mark.parametrize('mode,paused', [('paper',False),('live',True)])
def test_drawdown_does_not_pause_paper_but_still_pauses_live(mode, paused):
    q=make_quote(bid=.18,ask=.20,end_time=NOW+timedelta(minutes=4),expected_resolution_time=NOW+timedelta(minutes=4))
    p=make_params(entry_window_minutes=5,min_probability=.8,pick_underdog=1)
    b=make_book(equity=945,cash=945,peak=1000,streaks={q.key:2},trading_mode=mode)
    assert bool(evaluate([q],p,b,now=NOW).proposals) is not paused
    assert (tightened_out(q,p,b,now=NOW) is not None) is paused
    b.operator_pause=True
    assert not evaluate([q],p,b,now=NOW).proposals
    assert tightened_out(q,p,b,now=NOW) is not None


def test_runtime_status_and_manual_pause_after_paper_loss(tmp_path):
    engine=Engine(Settings(data_dir=str(tmp_path),openai_api_key=''),fetcher=lambda _: ([],[]))
    engine.store.set_cash(945)
    engine.store.note_peak(1000)
    p=engine.store.params()
    assert not engine._paused(p)
    assert engine.store.book().trading_mode=='paper'
    state=engine.snapshot(compact=True)
    assert not state['book']['drawdown_pause']
    assert not state['book']['entries_paused']
    assert state['book']['equity']==945
    engine.store.set_pause(True)
    assert engine._paused(p)
    assert engine.snapshot(compact=True)['book']['entries_paused']
