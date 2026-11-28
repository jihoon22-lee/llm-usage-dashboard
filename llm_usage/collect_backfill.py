"""One-time normalization upgrades and session/project attribution backfill."""

import json
from pathlib import Path
import sqlite3
import tempfile

from .record_files import RecordReadError, open_record, record_files
from .sqlite_ro import open_readonly
from .store import Store, identity, project_label


def _collect():
    from . import collect

    return collect


def prepare_normalization(store, settings):
    """Never discard retained history before a complete, reviewable replacement exists."""
    with store.connect() as c:
        version = store.state(c, 'normalized_version', 0)
        # A full scan; only a pre-v3 store needs the count.
        retained = (
            c.execute(
                "SELECT COUNT(*) FROM events WHERE route IN ('codex','claude-code') OR route LIKE 'codex:%'"
            ).fetchone()[0]
            if version < 3
            else 0
        )
    if version < 3 and retained:
        with tempfile.TemporaryDirectory(prefix='normalization-', dir=Path(store.path).parent) as folder:
            staged = Store(Path(folder) / 'usage.db', thresholds=settings.get('thresholds'))
            _collect().collect_local(staged, settings, include_derived=False)
            with store.connect() as c:
                c.execute('ATTACH DATABASE ? AS staged', (staged.path,))
                c.execute('BEGIN IMMEDIATE')
                bad = c.execute(
                    "SELECT COUNT(*) FROM staged.sources WHERE status!='ok' AND name!='antigravity-records'"
                ).fetchone()[0]
                # Every retained observation must survive or have explicit proof that
                # it is inherited. Merely finding a partial copy of a session is unsafe.
                missing = c.execute("""SELECT COUNT(*) FROM events e WHERE
                  (e.route IN ('codex','claude-code') OR e.route LIKE 'codex:%')
                  AND NOT EXISTS (SELECT 1 FROM staged.events n WHERE n.id=e.id)
                  AND NOT EXISTS (SELECT 1 FROM staged.codex_points n WHERE n.id=e.id AND e.route!='claude-code')
                  AND NOT EXISTS (SELECT 1 FROM staged.inherited_points n WHERE n.id=e.id AND e.route!='claude-code')""").fetchone()[
                    0
                ]
                missing += c.execute('''SELECT COUNT(*) FROM codex_points p
                  WHERE NOT EXISTS (SELECT 1 FROM staged.codex_points n WHERE n.id=p.id)
                  AND NOT EXISTS (SELECT 1 FROM staged.inherited_points n WHERE n.id=p.id)''').fetchone()[0]
                missing += c.execute('''SELECT COUNT(*) FROM codex_responses p WHERE NOT EXISTS
                  (SELECT 1 FROM staged.codex_responses n WHERE n.id=p.id AND n.session=p.session AND n.usage=p.usage
                   AND (p.account IS NULL OR n.account IS NULL OR p.account=n.account))''').fetchone()[0]
                if bad or missing:
                    store.source(
                        c,
                        'normalization',
                        'error',
                        '원본을 모두 복구할 수 없어 보정을 보류했습니다. 기존 통계를 유지합니다.',
                    )
                    return False
                c.execute('DELETE FROM codex_points')
                c.execute('INSERT INTO codex_points SELECT * FROM staged.codex_points')
                c.execute('''UPDATE staged.codex_responses SET account=
                  (SELECT p.account FROM main.codex_responses p WHERE p.id=staged.codex_responses.id) WHERE account IS NULL''')
                c.execute('DELETE FROM codex_response_links')
                c.execute('DELETE FROM codex_responses')
                c.execute('INSERT INTO codex_responses SELECT * FROM staged.codex_responses')
                c.execute('INSERT INTO codex_response_links SELECT * FROM staged.codex_response_links')
                c.execute('INSERT OR IGNORE INTO inherited_points SELECT * FROM staged.inherited_points')
                c.execute('DELETE FROM session_quality')
                # Replaying files is not new account evidence. Keep proven parent
                # ownership, including for newly recovered advisor children.
                c.execute('''UPDATE staged.events SET account=COALESCE(
                  (SELECT e.account FROM main.events e WHERE e.id=staged.events.id),
                  (SELECT e.account FROM main.events e WHERE e.id=staged.events.parent_id)) WHERE account IS NULL''')
                c.execute("DELETE FROM events WHERE route IN ('codex','claude-code') OR route LIKE 'codex:%'")
                c.execute(
                    "INSERT INTO events SELECT * FROM staged.events WHERE route IN ('codex','claude-code') OR route LIKE 'codex:%'"
                )
                # Native projection and cloud/telemetry deduplication require the
                # same evidence that produced the copied events and checkpoints.
                c.execute('INSERT OR IGNORE INTO usage_aliases SELECT * FROM staged.usage_aliases')
                c.execute('INSERT OR IGNORE INTO usage_links SELECT * FROM staged.usage_links')
                c.execute('INSERT OR IGNORE INTO usage_origins SELECT * FROM staged.usage_origins')
                c.execute("DELETE FROM state WHERE key LIKE 'file:%' OR key LIKE 'permanent_errors:%'")
                c.execute("DELETE FROM import_errors WHERE source LIKE 'file:%'")
                c.execute("INSERT INTO state SELECT * FROM staged.state WHERE key LIKE 'file:%'")
                store.save_state(c, 'normalized_version', 3)
                store.bump_data_version(c)
    with store.connect() as c:
        if version < 4:
            for row in c.execute('SELECT DISTINCT session FROM codex_points').fetchall():
                store.rebuild_codex(c, row['session'])
            store.save_state(c, 'normalized_version', 4)
        store.source(c, 'normalization', 'ok', '카운터 초기화 구간을 구분한 집계')
    return True


def _codex_meta(line):
    try:
        d = json.loads(line)
    except ValueError:
        return None
    return d.get('payload', {}) if d.get('type') == 'session_meta' else None


def _codex_kind(meta):
    source = meta.get('source')
    if meta.get('thread_source') == 'subagent' or (isinstance(source, dict) and source.get('subagent')):
        return 'sub'
    if meta.get('forked_from_id') or meta.get('parent_thread_id'):
        return 'fork'
    return 'main'


def backfill_jsonl(store, path, route):
    """Attribute pre-migration events from one JSONL record file."""
    if route.startswith('codex'):
        meta = None
        with open_record(path) as f:
            for line in f:
                if b'session_meta' not in line:
                    continue
                meta = _codex_meta(line)
                if meta:
                    break
        sid = (meta or {}).get('id') or (meta or {}).get('session_id')
        if not sid:
            return
        with store.connect() as c:
            c.execute(
                '''UPDATE events SET session=?,project=?,agent_kind=? WHERE session IS NULL
              AND id IN (SELECT id FROM codex_points WHERE session=?)''',
                (sid, project_label(meta.get('cwd')), _codex_kind(meta), identity(sid)),
            )
            store.bump_data_version(c)
    elif route == 'claude-code':
        updates = {}
        with open_record(path) as f:
            for line in f:
                if b'"assistant"' not in line:
                    continue
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if d.get('type') != 'assistant':
                    continue
                mid = (d.get('message') or {}).get('id')
                if mid and mid not in updates:
                    updates[mid] = (
                        d.get('sessionId'),
                        project_label(d.get('cwd')),
                        'sub' if d.get('isSidechain') else 'main',
                        identity('claude-code', mid),
                    )
        if updates:
            with store.connect() as c:
                c.executemany(
                    'UPDATE events SET session=?,project=?,agent_kind=? WHERE id=? AND session IS NULL',
                    list(updates.values()),
                )
                store.bump_data_version(c)


def backfill_opencode(store, path):
    src = open_readonly(path)
    try:
        has_session = (
            src.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='session'").fetchone() is not None
        )
        cols = {r[1] for r in src.execute('PRAGMA table_info(message)')}
        rows = src.execute(
            ('SELECT m.id,m.session_id,s.directory,s.parent_id FROM message m LEFT JOIN session s ON s.id=m.session_id')
            if has_session
            else 'SELECT id,' + ('session_id' if 'session_id' in cols else 'NULL') + ',NULL,NULL FROM message'
        ).fetchall()
    finally:
        src.close()
    with store.connect() as c:
        c.executemany(
            'UPDATE events SET session=?,project=?,agent_kind=? WHERE id=? AND session IS NULL',
            [(sid, project_label(d), 'sub' if p else 'main', identity('opencode', mid)) for mid, sid, d, p in rows],
        )
        store.bump_data_version(c)


def backfill_devin(store, settings):
    from .devin import roots, open_db

    for name, path in roots(settings):
        if not path.exists():
            continue
        src, tmp = open_db(path)
        try:
            has_sessions = (
                src.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='sessions'").fetchone() is not None
            )
            dirs = (
                {r['id']: r['working_directory'] for r in src.execute('SELECT id,working_directory FROM sessions')}
                if has_sessions
                else {}
            )
            updates = []
            for row in src.execute('SELECT session_id,chat_message FROM message_nodes'):
                try:
                    d = json.loads(row['chat_message'])
                except (ValueError, TypeError):
                    continue
                if d.get('role') != 'assistant':
                    continue
                rid = ((d.get('metadata') or {}).get('metrics') or {}) and (d.get('metadata') or {}).get('request_id')
                if not rid:
                    continue
                updates.append(
                    (row['session_id'], project_label(dirs.get(row['session_id'])), 'main', identity('devin', rid))
                )
        finally:
            src.close()
            if tmp:
                import shutil

                shutil.rmtree(tmp, ignore_errors=True)
        with store.connect() as c:
            c.executemany('UPDATE events SET session=?,project=?,agent_kind=? WHERE id=? AND session IS NULL', updates)
            store.bump_data_version(c)


def backfill_attribution(store, settings):
    """One-time session/project/agent_kind attribution for pre-migration events."""
    with store.connect() as c:
        if store.state(c, 'backfill_v2'):
            return
        store.source(c, 'session-backfill', 'partial', '세션·프로젝트 귀속 백필 진행 중')
    failures = []
    for source in _collect().local_sources(store, settings):
        kind = source.get('kind')
        route = source.get('route') or ''
        root = Path(source['path'])
        if kind in ('antigravity', 'devin') or not root.exists():
            continue
        try:
            if kind == 'opencode':
                backfill_opencode(store, root)
            elif route == 'claude-code' or route.startswith('codex'):
                for path in record_files(root):
                    backfill_jsonl(store, path, route)
        except (OSError, sqlite3.Error, EOFError, RecordReadError):
            failures.append(source['name'])
    try:
        backfill_devin(store, settings)
    except (OSError, sqlite3.Error):
        failures.append('devin')
    with store.connect() as c:
        if failures:
            # Leave the flag unset so the next collector run retries.
            store.source(
                c, 'session-backfill', 'error', '백필 실패 (' + ','.join(failures) + ') · 다음 수집에서 재시도'
            )
        else:
            store.save_state(c, 'backfill_v2', True)
            store.source(c, 'session-backfill', 'ok', '기존 기록의 세션·프로젝트 귀속 완료')
