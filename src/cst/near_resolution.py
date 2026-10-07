"""Prospective evidence for the final-minutes thesis, separate from legacy history.

Keep the first structurally eligible quote per event before its result is known.
These observations are research, not fills; their theoretical payout does not
change the paper ledger. Different events can still share underlying risks.
"""
from dataclasses import asdict
from datetime import datetime, timezone
import json

from cst.fees import fee_for
from cst.strategy import _structural, price_bucket, quote_in_band
from cst.venues.http import MarketHttp


def observe_and_resolve(store, settings, quotes, params, now, *, resolve=True, http=None):
    with store.lock:
        for quote in sorted(quotes, key=lambda q: q.key):
            if not quote_in_band(quote, params) or _structural(quote, params, now):
                continue
            expected = quote.entry_deadline
            if expected.tzinfo is None:
                expected = expected.replace(tzinfo=timezone.utc)
            fee = float(fee_for(quote.fee_model, quote.min_shares, quote.ask,
                                quote.fee_rate, quote.fee_exponent)) / quote.min_shares
            store.conn.execute(
                "INSERT OR IGNORE INTO near_observations "
                "(event_id,ticker,side,observed_at,expected_at,minutes_left,price,fee,bucket,payload) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (quote.event_id or quote.market_id, quote.market_id, quote.side, now.isoformat(),
                 expected.isoformat(), (expected-now).total_seconds()/60, quote.ask, fee,
                 price_bucket(quote.ask), json.dumps(asdict(quote), default=lambda v: v.isoformat())),
            )
        store._commit()
        if not resolve:
            return None
        # Sales need an official outcome even if research never sampled them.
        store.conn.execute("""
            INSERT OR IGNORE INTO sale_outcomes(market_id)
            SELECT DISTINCT market_id FROM trades WHERE venue='kalshi' AND action='sell'
        """)
        sold = store.conn.execute(
            "SELECT market_id FROM sale_outcomes WHERE result IS NULL ORDER BY COALESCE(checked_at, '') LIMIT 50"
        ).fetchall()
        store._commit()
        # Rotate unresolved observations so delayed outcomes cannot starve newer ones.
        rows = store.conn.execute(
            "SELECT ticker FROM near_observations WHERE result IS NULL ORDER BY COALESCE(checked_at, '') LIMIT 100"
        ).fetchall()
    tickers = list(dict.fromkeys([row['market_id'] for row in sold] + [row['ticker'] for row in rows]))[:100]
    if not tickers:
        return None
    client = http or MarketHttp()
    try:
        payload = client.get_json(settings.kalshi_base_url.rstrip('/') + '/markets',
                                  params={'tickers': ','.join(tickers), 'limit': '100'})
        if not isinstance(payload, dict) or not isinstance(payload.get('markets'), list):
            return 'Near-resolution evidence: outcome lookup returned no market list.'
        markets = {m.get('ticker'): m for m in payload['markets'] if isinstance(m, dict)}
        with store.lock:
            for ticker in tickers:
                result = str(markets.get(ticker, {}).get('result') or '').lower()
                store.conn.execute(
                    'UPDATE sale_outcomes SET result=?, checked_at=? WHERE market_id=? AND result IS NULL',
                    (result if result in {'yes', 'no'} else None, datetime.now(timezone.utc).isoformat(), ticker),
                )
                store.conn.execute(
                    'UPDATE near_observations SET result=?, resolved_at=?, checked_at=? WHERE ticker=? AND result IS NULL',
                    (result if result in {'yes','no'} else None, datetime.now(timezone.utc).isoformat() if result in {'yes','no'} else None, datetime.now(timezone.utc).isoformat(), ticker),
                )
            store._commit()
        return None
    except Exception as exc:
        return f'Near-resolution evidence: outcome lookup failed ({type(exc).__name__}).'
    finally:
        if http is None:
            client.close()
