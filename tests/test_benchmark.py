from datetime import datetime, timedelta, timezone

from cst.benchmark import build_benchmark, refresh_benchmark, snapshot
from cst.models import StrategyParams
from cst.store import Store


def data():
    return {'chart':{'result':[{'meta':{'currency':'USD','symbol':'SPY'},
        'timestamp':[int(datetime(2026,10,d,13,30,tzinfo=timezone.utc).timestamp()) for d in (5,6,7)],
        'indicators':{'adjclose':[{'adjclose':[100,110,120]}]}}]}}


def test_benchmark_uses_same_capital_and_excludes_incomplete_day():
    start=datetime(2026,10,6,9,tzinfo=timezone.utc)
    now=datetime(2026,10,7,15,tzinfo=timezone.utc)
    row=build_benchmark(data(),start,now,1000)
    assert row['value']==1100
    assert row['multiple']==1.1
    assert row['net_pnl']==100
    assert row['baseline_at'].startswith('2026-10-05')
    assert row['as_of'].startswith('2026-10-06')


def test_benchmark_failure_keeps_last_observed_value_and_cache(tmp_path):
    store=Store(tmp_path/'book.sqlite',StrategyParams(),1000)
    store._put('paper_started_at','2026-10-06T09:00:00+00:00');store.conn.commit()
    now=datetime(2026,10,7,15,tzinfo=timezone.utc)
    refresh_benchmark(store,now,fetch=lambda _:data())
    def failure(_):
        raise TimeoutError()
    cached=refresh_benchmark(store,now+timedelta(minutes=1),fetch=failure)
    assert cached['error'] is None
    stale=refresh_benchmark(store,now+timedelta(minutes=16),fetch=failure)
    assert stale['value']==1100
    assert stale['error']=='TimeoutError'
    assert snapshot(store,1150)['ahead_by']==50
