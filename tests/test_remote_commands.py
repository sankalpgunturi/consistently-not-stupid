from fastapi.testclient import TestClient
from cst.api import create_app
from cst.config import Settings
from cst.engine import Engine
from tools.publish_dashboard import public_snapshot


def test_public_snapshot_excludes_local_credentials_and_unknown_fields():
    assert public_snapshot({'csrf': 'secret', 'private_key': 'secret', 'audit': ['private'],
                            'book': {'equity': 1000}, 'trades': []}) == {'book': {'equity': 1000}, 'trades': []}


def test_remote_command_replay_cannot_repeat_mutation_even_after_reset(tmp_path):
    engine = Engine(Settings(data_dir=str(tmp_path)), fetcher=lambda _: ([], []))
    app = create_app(engine, start_loop=False)
    with TestClient(app, base_url='http://127.0.0.1') as client:
        headers = {'X-CSRF-Token': engine.store.csrf(), 'X-Command-Id': 'same-command'}
        assert client.post('/api/pause', json={'paused': True}, headers=headers).status_code == 200
        assert engine.store.operator_pause()
        assert client.post('/api/pause', json={'paused': False}, headers=headers).status_code == 202
        assert engine.store.operator_pause()
        engine.reset()
        headers['X-CSRF-Token'] = engine.store.csrf()
        assert client.post('/api/pause', json={'paused': True}, headers=headers).status_code == 202
        assert not engine.store.operator_pause()
        assert client.post('/api/pause', json={'paused': True}, headers={'X-Command-Id': 'no-token'}).status_code == 403


def test_public_snapshot_keeps_full_trade_history_but_not_research_archives():
    history = [{'id': str(i), 'action': 'buy'} for i in range(100)]
    result = public_snapshot({'trades': history, 'research': {'large_archive': 'x' * 100000},
                              'tape': ['internal decisions'], 'latest_model_review': {'summary': 'Reviewed'}})
    assert result['trades'] == history
    assert result['latest_model_review']['summary'] == 'Reviewed'
    assert 'research' not in result and 'tape' not in result
