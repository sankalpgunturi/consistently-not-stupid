"""Cached SPY adjusted-close benchmark, normalized to the initial paper capital.

SPY is an investable S&P 500 proxy, not the index itself. Adjusted daily closes
include distributions/splits. Quotes are supplied by Yahoo Finance. No trading
credentials or orders are involved. Never replace missing data with 10% growth.
"""
from datetime import datetime, timedelta, timezone, time
from zoneinfo import ZoneInfo
import math

import httpx

SOURCE = 'https://finance.yahoo.com/quote/SPY/history/'


def build_benchmark(payload, started, now, capital):
    result=payload['chart']['result'][0]
    if result.get('meta',{}).get('currency') != 'USD':
        raise ValueError('Benchmark currency must be USD')
    prices=result['indicators']['adjclose'][0]['adjclose']
    zone=ZoneInfo('America/New_York')
    rows=[]
    for stamp,price in zip(result['timestamp'],prices):
        if price is None or not math.isfinite(float(price)) or price<=0:
            continue
        date=datetime.fromtimestamp(stamp,zone).date()
        # Daily timestamps denote session open. Only use completed daily bars.
        close=datetime.combine(date,time(16),zone).astimezone(timezone.utc)
        if close<=now:
            rows.append((close,float(price)))
    rows.sort()
    baseline=[r for r in rows if r[0]<=started]
    if not baseline or not rows:
        raise ValueError('No completed benchmark close at the funding boundary')
    base=baseline[-1]
    latest=rows[-1]
    value=capital*latest[1]/base[1]
    return {'symbol':'SPY','label':'S&P 500 · SPY','source':SOURCE,
            'basis':'Adjusted daily close; distributions reinvested. Previous close is the funding baseline. No brokerage fees or taxes modeled.',
            'baseline_at':base[0].isoformat(), 'as_of':latest[0].isoformat(),
            'baseline_price':base[1], 'latest_price':latest[1],
            'value':round(value,4),'net_pnl':round(value-capital,4),
            'multiple':latest[1]/base[1], 'updated_at':now.isoformat(),'error':None}


def refresh_benchmark(store, now=None, *, fetch=None):
    now=now or datetime.now(timezone.utc)
    with store.lock:
        previous=store._get('benchmark',{})
        attempt=store._get('benchmark_attempt')
        if attempt and (now-datetime.fromisoformat(attempt)).total_seconds()<900:
            return previous
        store._put('benchmark_attempt',now.isoformat())
        store._commit()
        started=datetime.fromisoformat(store._get('paper_started_at'))
        first=store.conn.execute('SELECT equity FROM equity ORDER BY id LIMIT 1').fetchone()
        capital=float(first[0]) if first else store.bankroll
    try:
        params={'period1':int((started-timedelta(days=7)).timestamp()),'period2':int(now.timestamp()),'interval':'1d','events':'div,splits'}
        if fetch is None:
            response=httpx.get('https://query1.finance.yahoo.com/v8/finance/chart/SPY',params=params,
                               headers={'User-Agent':'Mozilla/5.0'},timeout=5)
            response.raise_for_status()
            payload=response.json()
        else:
            payload=fetch(params)
        record=build_benchmark(payload,started,now,capital)
    except Exception as exc:
        record={**previous,'error':type(exc).__name__,'source':SOURCE,'label':'S&P 500 · SPY'}
    with store.lock:
        store._put('benchmark',record)
        store._commit()
    return record


def snapshot(store, equity):
    with store.lock:
        record=dict(store._get('benchmark',{}))
    if record.get('value') is not None:
        record['ahead_by']=round(equity-record['value'],4)
    return record
