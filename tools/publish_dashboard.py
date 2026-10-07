"""Send a sanitized paper snapshot to the shared Cloudflare dashboard."""
import json
import logging
from pathlib import Path
import time

import httpx

ROOT = Path(__file__).resolve().parents[1]
PUBLIC_FIELDS = {'status', 'operator_pause', 'evidence', 'book', 'last_24h',
    'counts', 'positions', 'trades', 'realized_curve', 'cycle', 'params',
    'next_scan_at', 'errors', 'llm', 'latest_model_review', 'trade_reviews', 'market_links',
    'mode', 'live', 'live_budget', 'live_exchange_balance'}


def public_snapshot(state):
    # Never transmit the local CSRF token, audit payloads or future private fields.
    return {key: value for key, value in state.items() if key in PUBLIC_FIELDS}


def main():
    config = json.loads((ROOT / 'data/cloudflare-publisher.json').read_text())
    with httpx.Client(timeout=10) as client:
        while True:
            started = time.monotonic()
            try:
                local = client.get('http://127.0.0.1:8000/api/state')
                local.raise_for_status()
                result = client.post(config['url'].rstrip('/') + '/internal/snapshot',
                    headers={'Authorization': 'Bearer ' + config['token']},
                    json=public_snapshot(local.json()))
                result.raise_for_status()
                authorization = {'Authorization': 'Bearer ' + config['token']}
                response = client.get(config['url'].rstrip('/') + '/internal/commands', headers=authorization)
                response.raise_for_status()
                for command in response.json().get('commands', []):
                    if command['action'] not in {'pause', 'scan', 'reset', 'block', 'knob', 'close', 'live'}:
                        continue
                    current = client.get('http://127.0.0.1:8000/api/state')
                    current.raise_for_status()
                    execution = client.post('http://127.0.0.1:8000/api/' + command['action'],
                        headers={'X-CSRF-Token': current.json()['csrf'], 'X-Command-Id': command['id']},
                        json=command['body'], timeout=60)
                    detail = None
                    if not execution.is_success:
                        try:
                            detail = execution.json().get('error')
                        except Exception:
                            detail = None
                        if not isinstance(detail, str) or not detail.strip():
                            detail = 'Command failed; try again.'
                        detail = detail[:300]
                    ack = client.post(config['url'].rstrip('/') + '/internal/ack', headers=authorization,
                        json={'id': command['id'], 'action': command['action'], 'ok': execution.is_success,
                              'error': detail})
                    ack.raise_for_status()
            except Exception as exc:
                # Do not log request headers or payloads.
                logging.warning('Dashboard publication failed (%s)', type(exc).__name__)
            time.sleep(max(.1, 5 - (time.monotonic() - started)))


if __name__ == '__main__':
    main()
