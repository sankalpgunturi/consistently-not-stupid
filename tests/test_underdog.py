from datetime import timedelta

import pytest

from cst.strategy import evaluate, quote_in_band, tightened_out
from tests.conftest import NOW, make_book, make_params, make_quote


@pytest.mark.parametrize('side', ['yes', 'no'])
def test_switch_selects_opposite_side_but_keeps_favorite_threshold(side):
    favorite = make_quote(side=side, bid=.80, ask=.82, end_time=NOW+timedelta(minutes=4), expected_resolution_time=NOW+timedelta(minutes=4))
    underdog = make_quote(side='no' if side == 'yes' else 'yes', bid=.18, ask=.20, end_time=favorite.end_time, expected_resolution_time=favorite.expected_resolution_time)
    p = make_params(entry_window_minutes=5, min_probability=.80, amount_per_bet=1)
    b = make_book(streaks={favorite.key:2, underdog.key:2})
    assert evaluate([favorite,underdog],p,b,now=NOW).proposals[0].quote.side == favorite.side
    p.pick_underdog = 1
    result = evaluate([favorite,underdog],p,b,now=NOW)
    assert len(result.proposals) == 1
    chosen = result.proposals[0]
    assert chosen.quote.side == underdog.side
    assert chosen.signal == 'paper_underdog'
    assert chosen.shares > 1  # Whole contracts within the same $1 all-in budget.
    assert tightened_out(underdog,p,b,now=NOW,shares=chosen.shares) is None
    assert not quote_in_band(favorite,p)
    p.min_probability = .81
    assert not evaluate([favorite,underdog],p,b,now=NOW).proposals
    p.min_probability = .80
    p.pick_underdog = 0
    assert tightened_out(underdog,p,b,now=NOW,shares=chosen.shares) is not None


@pytest.mark.parametrize('change', ['spread','horizon','fee','size','stability','paused'])
def test_underdog_preserves_admission_checks(change):
    p=make_params(entry_window_minutes=5,min_probability=.8,pick_underdog=1)
    q=make_quote(bid=.18,ask=.20,end_time=NOW+timedelta(minutes=4),expected_resolution_time=NOW+timedelta(minutes=4))
    b=make_book(streaks={q.key:2})
    if change == 'spread': q.bid=.10
    if change == 'horizon': q.expected_resolution_time=NOW+timedelta(minutes=6)
    if change == 'fee': q.fee_verified=False
    if change == 'size': q.ask_size=0
    if change == 'stability': b.streaks={}
    if change == 'paused': b.operator_pause=True
    assert not evaluate([q],p,b,now=NOW).proposals


def test_legacy_replay_ignores_underdog_setting():
    p=make_params(entry_window_minutes=0,min_probability=.8,pick_underdog=1)
    assert quote_in_band(make_quote(bid=.9,ask=.91),p)
    assert not quote_in_band(make_quote(bid=.09,ask=.10),p)
