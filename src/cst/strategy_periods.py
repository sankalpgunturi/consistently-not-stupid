"""Attribute trades to recorded entry settings, never today's settings."""
import json
from datetime import datetime

KEYS = ('pick_underdog', 'min_probability', 'entry_window_minutes', 'all_in',
        'amount_per_bet', 'stop_loss_minutes', 'exit_probability', 'scan_interval_seconds')


def build_periods(trades, scans):
    candidates = {}
    for row in scans:
        data = json.loads(row['payload'])
        params = data.get('params')
        if not params:
            continue
        candidates.setdefault(row['key'], []).append((data, row['ts']))
    periods, mapping, pending = [], {}, {}
    previous = None
    for trade in trades:
        key = f"{trade['venue']}:{trade['market_id']}:{trade['side']}"
        if trade['action'] == 'buy':
            entry = datetime.fromisoformat(trade['ts'])
            matches = []
            for scan, timestamp in candidates.get(key, []):
                finish = datetime.fromisoformat(scan.get('finished_at') or timestamp)
                elapsed = (finish - entry).total_seconds()
                if -2 <= elapsed <= float(scan.get('duration_seconds', 0)) + 2:
                    matches.append((abs(elapsed), scan['params']))
            params = min(matches, key=lambda item: item[0])[1] if matches else None
            settings = {k: params.get(k) for k in KEYS} if params else None
            # Disabled controls do not create artificial strategy changes.
            if settings:
                if settings['all_in']:
                    settings['amount_per_bet'] = None
                if not settings['stop_loss_minutes']:
                    settings['exit_probability'] = None
            signature = json.dumps(settings, sort_keys=True)
            if not periods or signature != previous:
                periods.append({'id': f"strategy-{trade['id']}", 'number': len(periods)+1,
                                'started_at': trade['ts'], 'settings': settings})
                previous = signature
            pending[key] = periods[-1]['id']
        if key in pending:
            mapping[trade['id']] = pending[key]
    return {'periods': periods, 'trades': mapping}
