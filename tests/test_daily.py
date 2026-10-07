from datetime import timedelta
import json

from cst.daily import current_day, evaluate_days, save_reviews
from cst.models import StrategyParams
from cst.store import Store
from tests.conftest import NOW


def seeded(tmp_path):
    store=Store(tmp_path/'book.sqlite',StrategyParams(),1000)
    store._put('paper_started_at',NOW.isoformat(timespec='seconds'))
    store.conn.execute('DELETE FROM equity')
    store.conn.execute('INSERT INTO equity(ts,equity) VALUES (?,?)',(NOW.isoformat(timespec='seconds'),1000))
    store.conn.commit()
    return store


def mark(store, hours, value):
    store.conn.execute('INSERT INTO equity(ts,equity) VALUES (?,?)',((NOW+timedelta(hours=hours)).isoformat(timespec='seconds'),value))
    store.conn.commit()


def test_daily_windows_carry_capital_and_include_unrealized_losses(tmp_path):
    store=seeded(tmp_path)
    mark(store,23.99,990)
    assert evaluate_days(store,NOW+timedelta(hours=23)) == []
    reports=evaluate_days(store,NOW+timedelta(hours=24,seconds=5))
    assert len(reports)==1
    day=reports[0]
    assert day['net_pnl']==-10
    assert day['unrealized_change']==-10
    assert day['review_required']
    assert day['review_status']=='pending'
    assert len(evaluate_days(store,NOW+timedelta(hours=25)))==1
    current=current_day(store,995,NOW+timedelta(hours=25))
    assert current['day']==2
    assert current['opening_equity']==990
    assert current['net_pnl']==5
    assert current['capital_change']==-5
    assert current['net_contributions']==1000
    save_reviews(store,reports,'Investigate stale marks and concentration.',['No fills to attribute.'],None)
    assert evaluate_days(store,NOW+timedelta(hours=25))[0]['review_status']=='reviewed'


def test_daily_fees_not_subtracted_twice_and_no_activity_not_a_win(tmp_path):
    store=seeded(tmp_path)
    store.conn.execute("INSERT INTO trades(id,ts,action,fee,pnl) VALUES ('t',?,'buy',.01,0)",((NOW+timedelta(hours=1)).isoformat(timespec='seconds'),))
    store.conn.commit()
    mark(store,23.99,999.99)
    day=evaluate_days(store,NOW+timedelta(hours=24))[0]
    assert day['fees_paid']==.01
    assert day['net_pnl']==-.01
    assert day['buys']==1
    assert day['closed_trades']==0
    assert day['status']=='negative'
    mark(store,47.99,999.99)
    day2=evaluate_days(store,NOW+timedelta(hours=48))[0]
    assert day2['status']=='flat'
    assert day2['buys']==0
    assert day2['closed_trades']==0
    assert day2['opening_equity']==999.99
    assert day2['closing_mark_at'] is not None


def test_failed_daily_review_retries_and_reset_clears_days(tmp_path):
    store=seeded(tmp_path)
    reports=evaluate_days(store,NOW+timedelta(hours=24))
    save_reviews(store,reports,'Unavailable',[],'Timeout')
    assert evaluate_days(store,NOW+timedelta(hours=25))[0]['review_status']=='pending'
    store.reset(StrategyParams())
    assert store.conn.execute('SELECT COUNT(*) FROM daily_reviews').fetchone()[0]==0


def test_rolling_24h_uses_prior_balance_not_original_capital(tmp_path):
    from cst.daily import rolling_day
    store=seeded(tmp_path)
    mark(store,24,2000)
    mark(store,48,2500)
    row=rolling_day(store,2500,NOW+timedelta(hours=48))
    assert row['opening_equity']==2000
    assert row['net_pnl']==500
    assert row['multiple']==1.25
    assert row['partial'] is False
    early=rolling_day(store,1100,NOW+timedelta(hours=12))
    assert early['net_pnl']==100
    assert early['multiple']==1.1
    assert early['partial'] is True
