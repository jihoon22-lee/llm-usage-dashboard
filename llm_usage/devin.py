"""Devin for Terminal sessions.db — per-request token metrics.

The CLI commits every model response as a message_nodes row whose chat_message
JSON carries metadata.metrics (input/output/cache_read/cache_creation tokens),
request_id and generation_model. One logical request is rewritten several times
as chain snapshots with identical metrics, so request_id is the deduplication
identity. Quota fields are not persisted locally; limits.py polls GetUserStatus.
Conversation text and credentials are never copied out.
"""
import json
import shutil
import sqlite3
import tempfile
from collections import defaultdict
from pathlib import Path

from .store import identity, project_label

DB_RELS = ('.local/share/devin/cli/sessions.db', 'AppData/Roaming/devin/cli/sessions.db')

# generation_model is a compound id: model name plus trailing reasoning level or
# mode token ('swe-2-max', 'swe-1-6-fast', 'claude-opus-5-high'). Split it so the
# stored model reads as the model with its level, e.g. 'swe-2 (max)'.
MODEL_VARIANTS = frozenset({'low', 'medium', 'high', 'max', 'fast'})


def model_name(generation_model):
    base, dash, variant = (generation_model or '').rpartition('-')
    if dash and base and variant in MODEL_VARIANTS:
        return f'{base} ({variant})'
    return generation_model


def open_db(path):
    """Return (connection, tempdir_to_cleanup). mode=ro handles live WAL files on
    native filesystems; drives under /mnt cannot run WAL recovery, so those fall
    back to a private copy (db first, then wal — never the shm wal-index)."""
    try:
        con = sqlite3.connect(f'file:{path}?mode=ro', uri=True, timeout=5)
        try:
            con.execute('SELECT 1 FROM sqlite_master').fetchone()  # forces WAL recovery
            con.row_factory = sqlite3.Row
            return con, None
        except sqlite3.Error:
            con.close()
    except (OSError, sqlite3.Error):
        pass
    tmp = Path(tempfile.mkdtemp(prefix='llm-usage-devin-'))
    target = tmp / 'sessions.db'
    shutil.copyfile(path, target)
    try:
        shutil.copyfile(str(path) + '-wal', str(target) + '-wal')
    except OSError:
        pass
    con = sqlite3.connect(target)
    con.row_factory = sqlite3.Row
    return con, tmp


def checkpoint_of(value):
    """Legacy checkpoints are a bare row_id; newer ones also pin the file identity."""
    return value if isinstance(value, dict) else dict(row_id=value or 0, inode=None)


def file_signature(path):
    """(inode, size, mtime) of the database and its WAL; unchanged means nothing new."""
    values = []
    for file in (Path(path), Path(str(path) + '-wal')):
        try:
            stat = file.stat()
            values.append([stat.st_ino, stat.st_size, stat.st_mtime_ns])
        except OSError:
            values.append(None)
    return values


def collect_database(store, path, provider_for):
    try:
        inode = path.stat().st_ino
    except OSError:
        inode = None
    key = 'devin:' + str(path)
    signature = file_signature(path)
    # Drives under /mnt are read through a private copy of the whole database; skip
    # the copy and the scan when neither the database nor its WAL has changed.
    with store.connect() as c:
        saved = checkpoint_of(store.state(c, key, 0))
        if saved.get('signature') == signature:
            return store.error_count(c, key)
    src, tmp = open_db(path)
    try:
        with store.connect() as c:
            key = 'devin:' + str(path)
            saved = checkpoint_of(store.state(c, key, 0))
            top = src.execute('SELECT COALESCE(MAX(row_id),0) FROM message_nodes').fetchone()[0]
            # A reinstalled CLI recreates sessions.db and restarts row_id. Rescanning
            # is safe because events are keyed by request_id.
            replaced = top < saved['row_id'] or (None not in (saved['inode'], inode) and saved['inode'] != inode)
            if replaced:
                c.execute('DELETE FROM import_errors WHERE source=?', (key,))
            checkpoint = 0 if replaced else saved['row_id']
            newest = checkpoint
            has_sessions = src.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='sessions'").fetchone() is not None
            sel = ('SELECT n.row_id,n.session_id,n.node_id,n.created_at,n.chat_message,s.working_directory '
                   'FROM message_nodes n LEFT JOIN sessions s ON s.id=n.session_id ' if has_sessions else
                   'SELECT row_id,session_id,node_id,created_at,chat_message,NULL AS working_directory FROM message_nodes ')
            for row in src.execute(sel + 'WHERE ' + ('n.' if has_sessions else '') + 'row_id>? ORDER BY ' + ('n.' if has_sessions else '') + 'row_id', (checkpoint,)):
                newest = max(newest, row['row_id'])
                try:
                    d = json.loads(row['chat_message'])
                    if d.get('role') != 'assistant':
                        continue
                    meta = d.get('metadata') or {}
                    metrics = meta.get('metrics')
                    if not isinstance(metrics, dict):
                        continue
                    rid = meta.get('request_id')
                    record = 'node:' + str(row['node_id'])
                    if not rid:
                        store.import_error(c, key, record, 'MissingRequestId')
                        continue
                    model = model_name(meta.get('generation_model'))
                    # Chain snapshots repeat identical metrics; events dedupe by request_id.
                    store.event(c, identity('devin', rid), row['created_at'],
                                provider_for(model or ''), 'devin', model,
                                dict(uncached_input=metrics.get('input_tokens'),
                                     cached_input=metrics.get('cache_read_tokens'),
                                     output=metrics.get('output_tokens'),
                                     cache_creation=metrics.get('cache_creation_tokens'),
                                     reasoning=None),
                                session=row['session_id'], project=project_label(row['working_directory']),
                                agent_kind='main')
                    c.execute('DELETE FROM import_errors WHERE source=? AND record=?', (key, record))
                except (ValueError, TypeError, KeyError, AttributeError, OverflowError) as exc:
                    store.import_error(c, key, 'node:' + str(row['node_id']), type(exc).__name__)
            store.save_state(c, key, dict(row_id=newest, inode=inode, signature=signature))
            return store.error_count(c, key)
    finally:
        src.close()
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)


def roots(settings):
    found = []
    seen = set()
    platforms = defaultdict(int)
    for source in settings.get('sources', []):
        if source.get('kind') == 'devin':
            found.append((source['name'], Path(source['path'])))
    for home in settings.get('homes', []):
        for rel in DB_RELS:
            path = Path(home) / rel
            if not path.exists():
                continue
            platform = 'Windows' if str(path).startswith('/mnt/') else 'WSL'
            platforms[platform] += 1
            suffix = ' ' + str(platforms[platform]) if platforms[platform] > 1 else ''
            found.append((platform + ' Devin' + suffix, path))
    for name, path in found:
        canonical = str(path.resolve())
        if canonical not in seen:
            seen.add(canonical)
            yield name, path


def collect_databases(store, settings, provider_for):
    files = failed = missing = errors = 0
    for name, path in roots(settings):
        if not path.exists():
            missing += 1
            with store.connect() as c:
                store.source(c, name, 'unavailable', 'Devin sessions.db가 없습니다.')
            continue
        try:
            errs = collect_database(store, path, provider_for)
            files += 1
            errors += errs
            with store.connect() as c:
                store.source(c, name, 'partial' if errs else 'ok', f'요청 기록 수집 · 해석 실패 {errs}건')
        except (OSError, sqlite3.Error):
            failed += 1
            with store.connect() as c:
                store.source(c, name, 'error', 'Devin 기록 경로 읽기 실패')
    with store.connect() as c:
        total = c.execute("SELECT COUNT(*) FROM events WHERE route='devin'").fetchone()[0]
        status = ('partial' if failed or errors or missing else 'ok') if files else 'error' if failed else 'unavailable'
        detail = (f'{files}개 DB 확인 · 보존한 고유 요청 {total}개 · 읽기 실패 {failed}개'
                  f' · 해석 실패 {errors}건 · 없는 경로 {missing}개')
        store.source(c, 'devin-records', status, detail)
