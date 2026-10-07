from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from cst.config import Settings
from cst.engine import Engine
from cst.near_resolution import observe_and_resolve
from tools.publish_dashboard import public_snapshot
from tests.conftest import make_quote


@pytest.mark.parametrize('side,result,verdict', [('yes','no','good'),('no','yes','good'),('yes','yes','bad'),('no','no','bad'),('yes','','pending')])
def test_sold_trade_review_uses_official_outcome_without_changing_account(tmp_path, side, result, verdict):
    engine = Engine(Settings(data_dir=str(tmp_path), openai_api_key=''), fetcher=lambda _: ([], []))
    q = make_quote(side=side, ask=.85, bid=.84)
    engine.broker.buy(q, 1, 'test', 'entry', 1)
    trade = engine.broker.sell(engine.store.positions()[0], .50, 'Stop-loss')
    before = (engine.store.cash(), engine.store.realized(), len(engine.store.trades()))
    assert engine.store.sale_reviews()[trade.id]['verdict'] == 'pending'
    calls = []
    def get_json(url, params):
        calls.append(params)
        return {'markets': [{'ticker': q.market_id, 'result': result, 'yes_bid_dollars': '1.00'}]}
    error = observe_and_resolve(engine.store, engine.settings, [], engine.store.params(),
        datetime.now(timezone.utc), http=SimpleNamespace(get_json=get_json))
    assert error is None
    assert q.market_id in calls[0]['tickers']
    review = engine.store.sale_reviews()[trade.id]
    assert review['verdict'] == verdict
    assert review['sale_return'] == .48
    if verdict == 'good':
        assert review['advantage'] == .48
    elif verdict == 'bad':
        assert review['advantage'] == -.52
    else:
        assert review['advantage'] is None
    assert (engine.store.cash(), engine.store.realized(), len(engine.store.trades())) == before
    assert public_snapshot(engine.snapshot(compact=True))['sale_reviews'][trade.id] == review
    engine.store.arm_live(10, 10)
    assert engine.store.sale_reviews() == {}


def test_sale_review_retries_missing_result(tmp_path):
    engine = Engine(Settings(data_dir=str(tmp_path), openai_api_key=''), fetcher=lambda _: ([], []))
    q = make_quote()
    engine.broker.buy(q, 1, 'test', 'entry', 1)
    trade = engine.broker.sell(engine.store.positions()[0], .5, 'exit')
    for result, verdict in [('', 'pending'), ('no', 'good')]:
        http = SimpleNamespace(get_json=lambda *a, **kw: {'markets': [{'ticker': q.market_id, 'result': result}]})
        observe_and_resolve(engine.store, engine.settings, [], engine.store.params(), datetime.now(timezone.utc), http=http)
        assert engine.store.sale_reviews()[trade.id]['verdict'] == verdict
