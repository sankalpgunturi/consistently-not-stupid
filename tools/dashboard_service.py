"""Keep publishing to the permanent dashboard URL across login/restarts."""
import argparse
import os
from pathlib import Path
import plistlib
import subprocess

ROOT = Path(__file__).resolve().parents[1]
LABEL = 'com.consistently-not-stupid.dashboard'
DOMAIN = f'gui/{os.getuid()}'
TARGET = f'{DOMAIN}/{LABEL}'
PLIST = Path.home() / 'Library/LaunchAgents' / f'{LABEL}.plist'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['install', 'restart', 'stop', 'status'])
    command = parser.parse_args().command
    if command == 'install':
        if not (ROOT / 'data/cloudflare-publisher.json').is_file():
            raise SystemExit('Configure data/cloudflare-publisher.json first.')
        if subprocess.run(['launchctl', 'print', TARGET], capture_output=True).returncode == 0:
            raise SystemExit('Already installed; use restart.')
        logs = ROOT / 'data/logs'
        logs.mkdir(parents=True, exist_ok=True)
        PLIST.parent.mkdir(parents=True, exist_ok=True)
        PLIST.write_bytes(plistlib.dumps({
            'Label': LABEL, 'ProgramArguments': [str(ROOT / '.venv/bin/python'), str(ROOT / 'tools/publish_dashboard.py')],
            'WorkingDirectory': str(ROOT), 'RunAtLoad': True, 'KeepAlive': True,
            'ThrottleInterval': 15, 'StandardOutPath': str(logs / 'dashboard.log'),
            'StandardErrorPath': str(logs / 'dashboard.log')}))
        subprocess.run(['launchctl', 'bootstrap', DOMAIN, str(PLIST)], check=True)
    elif command == 'restart':
        subprocess.run(['launchctl', 'kickstart', '-k', TARGET], check=True)
    elif command == 'stop':
        subprocess.run(['launchctl', 'bootout', TARGET], check=True)
    else:
        subprocess.run(['launchctl', 'print', TARGET], check=True)


if __name__ == '__main__':
    main()
