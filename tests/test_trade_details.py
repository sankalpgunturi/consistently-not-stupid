import pytest
from fastapi.testclient import TestClient
from cst.api import create_app
from cst.broker import PaperBroker
from cst.config import Settings
from cst.engine import Engine
from cst.models import Decision, StrategyParams
from cst.review import tighten_value
from cst.store import Store
from cst.strategy import tightened_out
from tests.conftest import NOW, make_book, make_params, make_quote
from datetime import timedelta


def test_visible_settings_are_editable_both_directions_and_persist(tmp_path):
    engine = Engine(Settings(data_dir=str(tmp_path)), fetcher=lambda _: ([], []))
    with TestClient(create_app(engine, start_loop=False), base_url='http://127.0.0.1') as client:
        state = client.get('/api/state').json()
        headers = {'X-CSRF-Token': state['csrf']}
        assert [r['key'] for r in state['params']] == ['entry_window_minutes', 'min_probability', 'scan_interval_seconds', 'amount_per_bet', 'stop_loss_cents']
        for row in state['params']:
            key = row['key']
            for value in (row['max'], row['min']):
                body = {'key': key, 'value': value}
                assert client.post('/api/knob', json=body).status_code == 403
                assert client.post('/api/knob', headers={**headers, 'Origin': 'https://foreign.test'}, json=body).status_code == 403
                assert client.post('/api/knob', headers=headers, json=body).status_code == 200
                assert getattr(engine.store.params(), key) == pytest.approx(value)
                reopened = Store(tmp_path/'book.sqlite', StrategyParams(), 1000)
                assert getattr(reopened.params(), key) == pytest.approx(value)
                reopened.conn.close()
            assert client.post('/api/knob', headers=headers, json={'key': key, 'value': row['max']+row['step']}).status_code == 400
        assert client.post('/api/knob', headers=headers, json={'key': 'max_spread', 'value': .05}).status_code == 400


def test_spread_and_deployed_cap_block_entries_after_tightening():
    q = make_quote(bid=.94, ask=.97, end_time=NOW+timedelta(minutes=5), expected_resolution_time=NOW+timedelta(minutes=5))
    p = make_params(entry_window_minutes=10, max_position_fraction=1, max_category_fraction=1)
    b = make_book(streaks={q.key: 2}, equity=2.6, cash=2.6, peak=2.6)
    assert tightened_out(q,p,b,now=NOW) is None
    p.max_spread = tighten_value(p, 'max_spread')
    assert 'spread' in tightened_out(q,p,b,now=NOW)
    p.max_spread = .03
    p.max_deployed_fraction = tighten_value(p,'max_deployed_fraction')
    assert 'exposure' in tightened_out(q,p,b,now=NOW).lower()


def test_trade_review_uses_its_buy_scan_not_latest_review(tmp_path):
    store = Store(tmp_path/'book.sqlite', StrategyParams(),1000)
    q = make_quote()
    trade = PaperBroker(store).buy(q,1,'test','Entry reason',3)
    store.save_decisions(3,[Decision('bought','buy',q.title,q.venue,q.outcome,'Entry reason',key=q.key)])
    store.add_retro({'cycle':3,'source':'openai','summary':'Review for this entry'})
    store.add_retro({'cycle':4,'source':'openai','summary':'Unrelated later review'})
    assert store.trade_reviews()[trade.id]['summary'] == 'Review for this entry'
    other = PaperBroker(store).buy(make_quote(market_id='other',event_id='other'),1,'test','Imported',5)
    assert other.id not in store.trade_reviews()


def test_market_links_repair_archived_event_only_urls(tmp_path):
    from cst.venues.kalshi import normalize_market_url, quotes_from_kalshi_market
    old = 'https://kalshi.com/markets/kxgbpusd15m-26oct071330'
    correct = 'https://kalshi.com/markets/kxgbpusd15m/kxgbpusd15m-26oct071330'
    assert normalize_market_url(old) == correct
    assert normalize_market_url(correct) == correct
    canonical = 'https://kalshi.com/markets/kxgbpusd15m/15minute-gbpusd/kxgbpusd15m-26oct071330'
    assert normalize_market_url(canonical) == canonical
    store = Store(tmp_path/'book.sqlite', StrategyParams(),1000)
    q = make_quote(url=old)
    PaperBroker(store).buy(q,1,'test','Entry',1)
    store.save_scan(1,{},[{'key':q.key,'url':old}])
    assert store.market_links()[q.key] == correct
    assert old in store.conn.execute('SELECT payload FROM observations').fetchone()[0]
    quotes = quotes_from_kalshi_market({'ticker':'KXGBPUSD15M-26OCT071330-30',
        'event_ticker':'KXGBPUSD15M-26OCT071330','yes_bid_dollars':'0.95','yes_ask_dollars':'0.96'})
    assert quotes and all(q.url == correct for q in quotes)


def test_outcome_window_tightens_without_enabling_legacy_mode():
    p = make_params(entry_window_minutes=10)
    q = make_quote(end_time=NOW+timedelta(minutes=9,seconds=30),
                   expected_resolution_time=NOW+timedelta(minutes=9,seconds=30))
    b = make_book(streaks={q.key:2})
    assert tightened_out(q,p,b,now=NOW) is None
    p.entry_window_minutes = tighten_value(p,'entry_window_minutes')
    assert p.entry_window_minutes == 9
    assert 'within the next 9 minutes' in tightened_out(q,p,b,now=NOW)
    p.entry_window_minutes = 1
    assert tighten_value(p,'entry_window_minutes') is None
    p.entry_window_minutes = 0
    assert tighten_value(p,'entry_window_minutes') is None


def test_amount_sizes_whole_contracts_including_fees():
    from cst.strategy import evaluate, shares_for_budget
    from cst.fees import fee_for
    q = make_quote(bid=.80, ask=.82, end_time=NOW+timedelta(minutes=4), expected_resolution_time=NOW+timedelta(minutes=4))
    p = make_params(entry_window_minutes=5, min_probability=.80, amount_per_bet=20)
    b = make_book(streaks={q.key: 2})
    proposal = evaluate([q], p, b, now=NOW).proposals[0]
    n = proposal.shares
    cost = lambda size: size*q.ask+float(fee_for(q.fee_model,size,q.ask,q.fee_rate,q.fee_exponent))
    assert n > 1 and n == int(n)
    assert cost(n) <= 20 < cost(n+1)
    assert tightened_out(q,p,b,now=NOW,shares=n) is None
    p.amount_per_bet = 1
    assert shares_for_budget(q,p) == 1
    assert 'cap' in tightened_out(q,p,b,now=NOW,shares=n)
    p.min_probability = .90
    assert not evaluate([q],p,b,now=NOW).proposals
    p.min_probability = .80
    p.entry_window_minutes = 3
    assert not evaluate([q],p,b,now=NOW).proposals


@pytest.mark.parametrize('stop,bid,depth_ok,closed', [(0,.20,True,False),(10,.81,True,False),(10,.80,True,True),(10,.79,False,False)])
def test_operator_stop_loss_respects_bid_threshold_and_depth(tmp_path,stop,bid,depth_ok,closed):
    from cst.depth import DepthResult
    engine = Engine(Settings(data_dir=str(tmp_path),stop_loss_cents=stop),fetcher=lambda _: ([],[]))
    q = make_quote(bid=.89,ask=.90)
    PaperBroker(engine.store).buy(q,1,'paper_favorite','test',0)
    engine.refresher = lambda *_: make_quote(bid=bid,ask=bid+.01)
    engine.depth = lambda *_: DepthResult(depth_ok, 'test', 'test')
    engine.mark_open()
    assert (not engine.store.positions()) is closed
