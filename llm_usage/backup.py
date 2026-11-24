"""Daily online backup of the usage database and editable config files.

Runs inside the collector loop; writes go to the data directory which is the
service's only ReadWritePaths location. SQLite's online backup API is safe
against WAL writers, so collection does not pause.
"""

from datetime import datetime
import gzip
import os
import shutil
import sqlite3
import time
from contextlib import closing
from pathlib import Path

from .store import KST

KEEP_DAILY = 7
KEEP_WEEKLY = 4  # additionally retain Monday snapshots for this many weeks


class BackupCheckFailed(RuntimeError):
    pass


def error_text(exc):
    """What failed, as the status page may show it: the error's type and, for a file
    error, the system's reason. The message itself can carry paths and is left out."""
    if isinstance(exc, OSError) and exc.strerror:
        return f'{type(exc).__name__}: {exc.strerror}'
    return type(exc).__name__


def backup_now(database):
    database = Path(database)
    backups = database.parent / 'backups'
    backups.mkdir(parents=True, exist_ok=True, mode=0o700)
    stamp = datetime.now(KST).strftime('%Y%m%d')
    target = backups / f'usage-{stamp}.db.gz'
    tmp = backups / f'usage-{stamp}.db.tmp'
    # Compress to a temporary name and rename so an interrupted run never
    # leaves a partial file at the final .gz path.
    gz_tmp = backups / f'{target.name}.tmp'
    partial = [tmp, gz_tmp]
    try:
        with (
            closing(sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True)) as src,
            closing(sqlite3.connect(tmp)) as dst,
        ):
            src.backup(dst)
            ok = dst.execute('PRAGMA quick_check').fetchone()[0] == 'ok'
        if not ok:
            raise BackupCheckFailed('quick_check failed')
        with tmp.open('rb') as raw, gzip.open(gz_tmp, 'wb') as out:
            shutil.copyfileobj(raw, out, 1024 * 1024)
        os.replace(gz_tmp, target)
        for name in ('pricing.json', 'local.json'):
            side = database.parent / name
            if side.exists():
                side_tmp = backups / f'{name}-{stamp}.tmp'
                partial.append(side_tmp)
                side_tmp.write_bytes(side.read_bytes())
                os.replace(side_tmp, backups / f'{name}-{stamp}')
        _prune(backups)
        return dict(ts=time.time(), file=target.name, size=target.stat().st_size, ok=True)
    except Exception as e:
        return dict(ts=time.time(), ok=False, error=error_text(e))
    finally:
        for path in partial:
            path.unlink(missing_ok=True)


def _prune(backups):
    now = time.time()
    cutoff_daily = now - KEEP_DAILY * 86400
    cutoff_weekly = now - KEEP_WEEKLY * 7 * 86400
    for path in sorted(backups.glob('usage-*.db.gz')):
        try:
            day_str = path.stem.split('-', 1)[1].removesuffix('.db')
            day = datetime.strptime(day_str, '%Y%m%d').replace(tzinfo=KST)
        except (ValueError, IndexError):
            continue
        ts = day.timestamp()
        if ts > cutoff_daily or (ts > cutoff_weekly and day.weekday() == 0):
            continue
        path.unlink()
        for name in ('pricing.json', 'local.json'):
            (backups / f'{name}-{day_str}').unlink(missing_ok=True)
