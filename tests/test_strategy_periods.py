import json

from cst.strategy_periods import build_periods


def trade(id, time, market='a', action='buy'):
    return dict(id=id, ts=f'2026-10-08T12:{time}:00+00:00', venue='kalshi',market_id=market,side='yes',action=action)


def scan(time, market='a', **params):
    return dict(key=f'kalshi:{market}:yes', ts=f'2026-10-08T12:{time}:01+00:00',payload=json.dumps(dict(duration_seconds=2,params=dict(min_probability=.8,entry_window_minutes=5,**params))))


def test_batches_follow_entry_settings_not_settlement_time():
    trades=[trade('a','00'),trade('b','01','b'),trade('c','02','c'),trade('sold','03','a','sell'),trade('settled','04','b','settle')]
    r=build_periods(trades,[scan('00',all_in=0),scan('01','b',all_in=0),scan('02','c',all_in=1)])
    assert len(r['periods'])==2
    assert r['trades']['a']==r['trades']['b']==r['trades']['sold']==r['trades']['settled']
    assert r['trades']['c']!=r['trades']['a']


def test_missing_evidence_is_unknown_and_disabled_knobs_do_not_split():
    r=build_periods([trade('a','00'),trade('b','01','b'),trade('c','02','c')],[scan('01','b',all_in=1,amount_per_bet=1,stop_loss_minutes=0,exit_probability=.6),scan('02','c',all_in=1,amount_per_bet=2,stop_loss_minutes=0,exit_probability=.5)])
    assert len(r['periods'])==2
    assert r['periods'][0]['settings'] is None
    assert r['trades']['b']==r['trades']['c']


def test_return_to_previous_strategy_creates_a_new_period():
    r=build_periods([trade('a','00'),trade('b','01','b'),trade('c','02','c')],[scan('00',pick_underdog=0),scan('01','b',pick_underdog=1),scan('02','c',pick_underdog=0)])
    assert len(r['periods'])==3
