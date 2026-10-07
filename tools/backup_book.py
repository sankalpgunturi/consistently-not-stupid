"""Create a consistent local SQLite backup; keep the latest seven days."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import os
import sqlite3


def main():
    os.umask(0o077)
    root = Path(__file__).resolve().parents[1] / 'data'
    backups = root / 'backups'
    backups.mkdir(exist_ok=True)
    now = datetime.now(timezone.utc)
    target = backups / f"server-{now:%Y%m%dT%H%M%SZ}.sqlite"
    temporary = target.with_suffix('.partial')
    with sqlite3.connect(f'file:{root / "book.sqlite"}?mode=ro', uri=True) as source:
        with sqlite3.connect(temporary) as destination:
            source.backup(destination)
            if destination.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                raise RuntimeError('Backup integrity check failed')
    temporary.replace(target)
    cutoff = (now - timedelta(days=7)).timestamp()
    for old in backups.glob('server-*.sqlite'):
        if old.stat().st_mtime < cutoff:
            old.unlink()
    print(f'Backup verified: {target.name}')


if __name__ == '__main__':
    main()
