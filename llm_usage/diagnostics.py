"""Operational diagnostics snapshot and manual backup / selective refresh helpers."""

from pathlib import Path
import threading
import time

from .db.migrations import current_version

AVAILABLE_REFRESH_TARGETS = (
    'all',
    'local',
    'claude-code',
    'codex',
    'antigravity',
    'devin',
    'opencode-go',
)
MANUAL_BACKUP_COOLDOWN_SECONDS = 30
_backup_lock = threading.Lock()


def validate_refresh_targets(raw):
    """Validate optional refresh targets list; returns normalized list or None (= all)."""
    if raw is None:
        return None
    if not isinstance(raw, list) or not raw or len(raw) > len(AVAILABLE_REFRESH_TARGETS):
        raise ValueError('갱신 대상 목록을 확인하세요.')
    valid = set(AVAILABLE_REFRESH_TARGETS)
    cleaned = []
    for item in raw:
        if not isinstance(item, str) or item not in valid:
            raise ValueError('알 수 없는 갱신 대상입니다.')
        if item not in cleaned:
            cleaned.append(item)
    if 'all' in cleaned:
        return None
    return cleaned


def _file_size(path):
    try:
        return Path(path).stat().st_size
    except OSError:
        return 0


def system_diagnostics(store):
    """Return DB/WAL sizes, schema version, backup list, hook drift status, and refresh state.

    Never exposes absolute filesystem paths or credentials.
    """
    db_path = Path(store.path)
    backups_dir = db_path.parent / 'backups'
    backup_files = []
    if backups_dir.is_dir():
        for entry in sorted(backups_dir.glob('usage-*.db.gz'), reverse=True)[:30]:
            try:
                st = entry.stat()
                backup_files.append(dict(name=entry.name, bytes=st.st_size, mtime=st.st_mtime))
            except OSError:
                continue

    with store.connect() as c:
        c.execute('BEGIN')
        schema_ver = current_version(c)
        counts = {}
        for table in ('events', 'usage_hourly', 'limits', 'limit_history'):
            counts[table] = c.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
        latest_backup = store.state(c, 'backup', {})
        hook_row = c.execute("SELECT status, detail, checked FROM sources WHERE name='hook-scripts'").fetchone()
        refresh = dict(
            requested=store.state(c, 'refresh_requested', 0),
            completed=store.state(c, 'refresh_completed', 0),
            request_id=store.state(c, 'refresh_request_id', 0),
            completed_id=store.state(c, 'refresh_completed_id', 0),
            pending_targets=store.state(c, 'refresh_targets'),
        )
        manual_backup_at = store.state(c, 'manual_backup_at', 0)

    hooks = (
        dict(status=hook_row['status'], detail=hook_row['detail'], checked=hook_row['checked'], repair_needed=True)
        if hook_row
        else dict(status='ok', detail='보조 스크립트 버전 일치', checked=None, repair_needed=False)
    )
    hooks['repair_command'] = 'llm-usage install-hooks'

    return dict(
        database=dict(
            bytes=_file_size(db_path),
            wal_bytes=_file_size(str(db_path) + '-wal'),
            schema_version=schema_ver,
            table_counts=counts,
        ),
        backups=dict(
            count=len(backup_files),
            total_bytes=sum(f['bytes'] for f in backup_files),
            latest=latest_backup,
            files=backup_files,
            manual_backup_at=manual_backup_at,
        ),
        hooks=hooks,
        refresh=refresh,
        available_targets=list(AVAILABLE_REFRESH_TARGETS),
    )


def trigger_manual_backup(store):
    """Run an immediate online SQLite backup with a 30s cooldown lock."""
    from .backup import backup_now

    with _backup_lock:
        now = time.time()
        with store.connect() as c:
            last = store.state(c, 'manual_backup_at', 0) or 0
            if now - last < MANUAL_BACKUP_COOLDOWN_SECONDS:
                wait = max(1, int(MANUAL_BACKUP_COOLDOWN_SECONDS - (now - last)))
                return None, wait
        result = backup_now(store.path)
        with store.connect() as c:
            store.save_state(c, 'backup', result)
            store.save_state(c, 'manual_backup_at', time.time())
        return result, 0
