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
            assert row['next_value'] == pytest.approx(expected) if expected is not None else row['next_value'] is None
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
