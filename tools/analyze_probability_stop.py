"""Approximate stop exits from archived position marks, not executable order books."""
import argparse
from datetime import datetime
import json
import sqlite3
from cst.fees import fee_for


def analyze(path, day):
    connection = sqlite3.connect(f'file:{path}?mode=ro', uri=True)
    connection.row_factory = sqlite3.Row
    settlements = connection.execute("SELECT * FROM trades WHERE ledger='paper' AND action='settle' AND substr(ts,1,10)=?", (day,)).fetchall()
    closed = {(r['market_id'], r['side']): dict(r) for r in settlements}
    hits = {1: {}, 2: {}}
    actual = sum(r['pnl'] for r in settlements)
    for row in connection.execute('SELECT ts,payload FROM scans ORDER BY cycle'):
        scan = json.loads(row['payload'])
        now = datetime.fromisoformat(scan.get('evaluated_at') or row['ts'])
        for position in scan.get('book_before', {}).get('positions', []):
            key = (position['market_id'], position['side'])
            if key not in closed or not position.get('end_time'):
                continue
            remaining = (datetime.fromisoformat(position['end_time'])-now).total_seconds()
            for minutes in hits:
                if key in hits[minutes] or not 0 < remaining <= minutes*60 or not 0 < position['bid'] < .60:
                    continue
                fee = float(fee_for(position['fee_model'], position['shares'], position['bid'], position['fee_rate'], position['fee_exponent']))
                proceeds = max(0, position['shares']*position['bid']-fee)
                hits[minutes][key] = dict(market=key[0], side=key[1], snapshot_at=now.isoformat(), bid=position['bid'],
                    hypothetical_pnl=round(proceeds-position['cost_basis'],6), actual_pnl=closed[key]['pnl'], won=bool(closed[key]['won']))
    connection.close()
    return dict(day_utc=day, completed=len(closed), actual_pnl=round(actual,6),
        limitations=['Stored marks may predate their scan timestamp.', 'Missing observations and depth prevent verification of fills.',
                     'Uses earliest observed trigger, not a complete tick replay.', 'One day is insufficient to validate a strategy.'],
        scenarios=[dict(final_minutes=minutes, exit_below=.60, triggers=list(rows.values()),
                        hypothetical_total_pnl=round(actual+sum(r['hypothetical_pnl']-r['actual_pnl'] for r in rows.values()),6))
                   for minutes,rows in hits.items()])


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', default='data/book.sqlite')
    parser.add_argument('--day', required=True)
    args=parser.parse_args()
    print(json.dumps(analyze(args.db,args.day),indent=2))
