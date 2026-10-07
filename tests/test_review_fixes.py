from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from cst.api import create_app
from cst.config import Settings
from cst.engine import Engine
from cst.live import Execution
from tests.conftest import make_quote


def desk(tmp_path):
    return Engine(Settings(data_dir=str(tmp_path), openai_api_key=''), fetcher=lambda _: ([], []))


def test_background_cycle_archives_without_building_dashboard(tmp_path, monkeypatch):
    engine = desk(tmp_path)
    def unexpected(*args, **kwargs):
        pytest.fail('Background cycle must not build a dashboard snapshot')
    monkeypatch.setattr(engine, 'snapshot', unexpected)
    result = engine.run_cycle(include_snapshot=False)
    assert not result['errors']
    assert result['number'] == engine.store.cycle()
    assert engine.store.conn.execute('SELECT COUNT(*) FROM scans').fetchone()[0] == 1


def test_compact_dashboard_keeps_account_history_without_loading_archives(tmp_path, monkeypatch):
    engine = desk(tmp_path)
    engine.broker.buy(make_quote(), 1, 'test', 'test', 1)
    full = engine.snapshot()
    def unexpected(*args, **kwargs):
        pytest.fail('Publisher must not load unused archives')
    for method in ('research_summary', 'retros', 'decisions', 'equity_curve', 'audit'):
        monkeypatch.setattr(engine.store, method, unexpected)
    with TestClient(create_app(engine, start_loop=False), base_url='http://127.0.0.1') as client:
        compact = client.get('/api/state?compact=true').json()
        for key in ('book', 'positions', 'trades', 'params', 'market_links', 'csrf'):
            assert compact[key] == full[key]
        assert client.get('/api/state?compact=true', headers={'Origin': 'https://foreign.test'}).status_code == 403


def test_market_links_use_latest_nonempty_quote_and_active_ledger(tmp_path):
    engine = desk(tmp_path)
    q = make_quote()
    engine.broker.buy(q, 1, 'test', 'test', 1)
    old, new = 'https://kalshi.com/markets/old/event', 'https://kalshi.com/markets/new/event'
    engine.store.save_scan(1, {}, [{'key': q.key, 'url': old}])
    engine.store.save_scan(2, {}, [{'key': q.key, 'url': new}])
    engine.store.save_scan(3, {}, [{'key': q.key, 'url': ''}])
    assert engine.store.market_links() == {q.key: new}
    engine.store.arm_live(10, 10)
    assert engine.store.market_links() == {}


def test_partial_sell_retains_unsold_position_and_basis(tmp_path):
    engine = desk(tmp_path)
    engine.store.arm_live(10, 10)
    engine.broker.buy(make_quote(ask=.8, bid=.79), 2, 'test', 'test', 1)
    held = engine.store.positions()[0]
    initial_basis = held.cost_basis
    engine.trader = SimpleNamespace(sell=lambda *_: Execution(True, '', .6, .5))
    first = engine._execute_sell(held, .6, 'partial stop')
    remaining = engine.store.positions()[0]
    assert first.shares == .5
    assert remaining.shares == 1.5
    assert remaining.cost_basis == pytest.approx(initial_basis * .75)
    assert remaining.fees == pytest.approx(held.fees * .75)
    engine.trader.sell = lambda *_: Execution(True, '', .7, 1.5)
    second = engine._execute_sell(remaining, .7, 'remaining exit')
    assert not engine.store.positions()
    assert engine.store.cash() - 10 == pytest.approx(first.pnl + second.pnl)
    assert engine.store.realized() == pytest.approx(first.pnl + second.pnl)
