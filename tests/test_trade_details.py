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


def test_visible_settings_tighten_through_api_and_persist(tmp_path):
    engine = Engine(Settings(data_dir=str(tmp_path), entry_window_minutes=10), fetcher=lambda _: ([], []))
    with TestClient(create_app(engine, start_loop=False), base_url='http://127.0.0.1') as client:
        state = client.get('/api/state').json()
        headers = {'X-CSRF-Token': state['csrf']}
        for row in state['params']:
            if not row.get('adjustable', True):
                continue
            key = row['key']
            expected = tighten_value(engine.store.params(), key)
            before = getattr(engine.store.params(), key)
            assert client.post('/api/knob', json={'key': key}).status_code == 403
            response = client.post('/api/knob', headers=headers, json={'key': key})
            assert response.status_code == 200
            actual = getattr(engine.store.params(), key)
            assert actual == pytest.approx(expected if expected is not None else before)
            reopened = Store(tmp_path/'book.sqlite', StrategyParams(), 1000)
            assert getattr(reopened.params(), key) == pytest.approx(actual)
            reopened.conn.close()


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
