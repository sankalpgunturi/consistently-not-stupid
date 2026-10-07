"""Send a sanitized paper snapshot to the shared Cloudflare dashboard."""
import json
import logging
from pathlib import Path
import time

import httpx

ROOT = Path(__file__).resolve().parents[1]
PUBLIC_FIELDS = {'status', 'operator_pause', 'paper_age_days', 'evidence', 'book',
    'daily', 'last_24h', 'benchmark', 'counts', 'tape', 'positions', 'trades',
    'realized_curve', 'focus', 'retrospective', 'cycle', 'params', 'research',
    'next_scan_at', 'errors', 'llm', 'latest_model_review'}


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
                    if command['action'] not in {'pause', 'scan', 'reset', 'block', 'knob', 'close'}:
                        continue
                    current = client.get('http://127.0.0.1:8000/api/state')
                    current.raise_for_status()
                    execution = client.post('http://127.0.0.1:8000/api/' + command['action'],
                        headers={'X-CSRF-Token': current.json()['csrf'], 'X-Command-Id': command['id']},
                        json=command['body'], timeout=60)
                    ack = client.post(config['url'].rstrip('/') + '/internal/ack', headers=authorization,
                        json={'id': command['id'], 'action': command['action'], 'ok': execution.is_success,
                              'error': None if execution.is_success else 'Command failed; try again.'})
                    ack.raise_for_status()
            except Exception as exc:
                # Do not log request headers or payloads.
                logging.warning('Dashboard publication failed (%s)', type(exc).__name__)
            time.sleep(max(.1, 5 - (time.monotonic() - started)))


if __name__ == '__main__':
    main()
