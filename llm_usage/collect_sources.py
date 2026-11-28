"""Secondary local collectors: OpenCode SQLite, Antigravity tokens, status inboxes, hook checks, and summaries."""

import json
import os
from pathlib import Path
import re
import sqlite3
import time

from .config import local_problem
from .limits import status_limits
from .sqlite_ro import open_readonly
from .store import identity, project_label
from .util import prefix_bounds


def _collect():
    from . import collect

    return collect


def collect_opencode(store, path):
    src = open_readonly(path)
    try:
        with store.connect() as c:
            key = 'opencode:' + str(path)
            checkpoint = store.state(c, key, 0)
            if not store.state(c, key + ':details-v1'):
                checkpoint = 0
            newest = checkpoint
            cols = {r[1] for r in src.execute('PRAGMA table_info(message)')}
            has_session = (
                'session_id' in cols
                and src.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='session'").fetchone()
                is not None
            )
            sel = (
                'SELECT m.id,m.time_created,m.time_updated,m.data,m.session_id,s.directory,s.parent_id '
                'FROM message m LEFT JOIN session s ON s.id=m.session_id'
                if has_session
                else 'SELECT id,time_created,time_updated,data,'
                + ('session_id' if 'session_id' in cols else 'NULL')
                + ',NULL,NULL FROM message'
            )

            def ingest(row):
                mid, created, updated, raw, sid, directory, parent_id = row
                try:
                    d = json.loads(raw)
                    if d.get('role') == 'assistant' and d.get('tokens'):
                        t = d['tokens']
                        cache = t.get('cache', {})
                        model = d.get('modelID', 'unknown')
                        # OpenCode output excludes reasoning, input excludes cache read/write.
                        store.event(
                            c,
                            identity('opencode', mid),
                            created / 1000,
                            _collect().model_provider(model),
                            d.get('providerID', 'opencode'),
                            model,
                            dict(
                                uncached_input=t.get('input', 0),
                                cached_input=cache.get('read', 0),
                                cache_creation=cache.get('write', 0),
                                output=t.get('output', 0) + t.get('reasoning', 0),
                                reasoning=t.get('reasoning', 0),
                                reasoning_observed=int(t.get('reasoning') is not None),
                            ),
                            session=sid,
                            project=project_label(directory),
                            agent_kind='sub' if parent_id else 'main',
                        )
                    c.execute('DELETE FROM import_errors WHERE source=? AND record=?', (key, mid))
                except _collect().RECORD_ERRORS as exc:
                    store.import_error(c, key, mid, type(exc).__name__)

            # Retry a bounded batch even if a repaired row retains its old time_updated.
            retries = c.execute(
                'SELECT record FROM import_errors WHERE source=? ORDER BY checked LIMIT 200', (key,)
            ).fetchall()
            for failed in retries:
                row = src.execute(
                    sel + ' WHERE m.id=?' if has_session else sel + ' WHERE id=?', (failed['record'],)
                ).fetchone()
                if row and row[2] < checkpoint:
                    ingest(row)
                elif row is None:
                    # Rotate missing IDs too, so they cannot starve later retries.
                    store.import_error(c, key, failed['record'], 'MissingRecord')
            for row in src.execute(
                sel
                + (
                    ' WHERE m.time_updated>=? ORDER BY m.time_updated'
                    if has_session
                    else ' WHERE time_updated>=? ORDER BY time_updated'
                ),
                (checkpoint,),
            ):
                newest = max(newest, row[2])
                ingest(row)
            store.save_state(c, key, newest)
            store.save_state(c, key + ':details-v1', True)
            return store.error_count(c, key)
    finally:
        src.close()


def collect_antigravity_tokens(store, settings):
    from .statusline import validate_observation, observation_key

    errors = 0
    for folder in settings.get('status_inboxes', []):
        path = Path(folder) / 'antigravity-tokens.db'
        if not path.exists():
            continue
        key = 'agy-tokens:' + str(path)
        try:
            with store.connect() as c:
                stat = path.stat()
                signature = [stat.st_ino, stat.st_size, stat.st_mtime_ns]
                if store.state(c, key) == signature:
                    continue
                source = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=2)
                try:
                    for first, last, raw in source.execute(
                        'SELECT first_seen,last_seen,data FROM observations ORDER BY last_seen'
                    ):
                        if (
                            not all(isinstance(v, (int, float)) and 0 < v <= time.time() + 60 for v in (first, last))
                            or first > last
                        ):
                            raise ValueError('invalid observation time')
                        value = validate_observation(json.loads(raw))
                        c.execute(
                            'INSERT INTO agy_token_observations VALUES (?,?,?,?) ON CONFLICT(id) DO UPDATE SET '
                            'first_seen=MIN(first_seen,excluded.first_seen),last_seen=MAX(last_seen,excluded.last_seen)',
                            (observation_key(value), first, last, json.dumps(value, sort_keys=True)),
                        )
                    store.save_state(c, key, signature)
                finally:
                    source.close()
        except (OSError, sqlite3.Error, ValueError, TypeError):
            errors += 1
    # This runs on the 2-second loop: prune hourly and rewrite the source row only
    # when its status or text changes, so an idle loop does not write the database.
    with store.connect() as c:
        if time.time() - (store.state(c, 'agy-tokens:pruned', 0) or 0) >= 3600:
            c.execute('DELETE FROM agy_token_observations WHERE last_seen<?', (time.time() - 30 * 86400,))
            store.save_state(c, 'agy-tokens:pruned', time.time())
        row = c.execute('SELECT data,last_seen FROM agy_token_observations ORDER BY last_seen DESC LIMIT 1').fetchone()
        if errors:
            store.source_changed(c, 'antigravity-tokens', 'error', '토큰 관측 파일 읽기 실패 · 기존 관측은 유지합니다.')
        elif row:
            value = json.loads(row['data'])
            counters = {k: v for k, v in value['totals'].items() if k != 'context_window_size'} | value['current']
            numeric = sum(type(v) is int for v in counters.values())
            integrated = c.execute('SELECT 1 FROM antigravity_requests LIMIT 1').fetchone() is not None
            detail = (
                f'토큰 숫자 {numeric}개 필드 수신 · '
                + ('소비량은 대화 DB에서 별도 집계' if integrated else '소비량 합계 검증 대기')
                if numeric
                else '상태줄 수신 · 토큰 숫자 미제공 (' + value['context_state'] + ')'
            )
            if time.time() - row['last_seen'] > 600:
                detail += ' · 오래된 관측'
            store.source_changed(
                c,
                'antigravity-tokens',
                ('ok' if integrated else 'partial') if numeric else 'unavailable',
                detail,
                row['last_seen'],
            )
        else:
            store.source_changed(
                c, 'antigravity-tokens', 'unavailable', '토큰 관측 연결됨 · 다음 Antigravity 상태줄 수신 대기'
            )


def status_files(folder):
    """(path, base route) of every status-line file; Claude homes other than the
    default write claude-code@<home tag>.json."""
    folder = Path(folder)
    found = [(folder / 'claude-code.json', 'claude-code'), (folder / 'antigravity.json', 'antigravity')]
    try:
        found += [
            (p, 'claude-code')
            for p in sorted(folder.glob('claude-code@*.json'))
            if re.fullmatch(r'claude-code@[0-9a-f]{12}\.json', p.name)
        ]
    except OSError:
        pass
    return [(p, route) for p, route in found if p.exists()]


def status_route(store, c, path, route, data):
    """The quota route a status line reports for; None when its account is unknown."""
    if route != 'claude-code':
        return route
    key = data.get('account')
    if isinstance(key, str) and re.fullmatch('[0-9a-f]{64}', key):
        from .claude_accounts import route_for

        primary = store.state(c, 'claude_primary_account')
        if primary or key in (store.state(c, 'claude_accounts:unbound-keys', []) or []):
            return route_for(key, primary)
        known = c.execute('SELECT route FROM claude_accounts WHERE account_key=?', (key,)).fetchone()
        if known:
            return known[0]
        return 'claude-code' if path.name == 'claude-code.json' else None
    # Older wrappers did not identify the account; only the default home's file is used.
    return 'claude-code' if path.name == 'claude-code.json' else None


def collect_status(store, settings):
    from .config import ended_routes
    from .store import USER_ENDED_DETAIL

    collect_antigravity_tokens(store, settings)
    ended = ended_routes(settings)
    for folder in settings.get('status_inboxes', []):
        for path, base in status_files(folder):
            # A Claude file names its account only inside (see status_route).
            route = None if base == 'claude-code' else base
            try:
                data = json.loads(path.read_text())
                with store.connect() as c:
                    route = status_route(store, c, path, base, data)
                    if route is None:
                        continue
                    # Kept (committed) before the event is read, so a file whose last event was already
                    # read, or whose event fails below, still names its account.
                    if store.state(c, 'status_route:' + str(path)) != route:
                        store.save_state(c, 'status_route:' + str(path), route)
                        c.commit()
                    if route in ended:
                        # Like the polled sources: an old ok/error must not stay live.
                        store.source_changed(c, route, 'ended', USER_ENDED_DETAIL)
                        continue
                    checked = data['checked']
                    if checked <= store.state(c, 'status:' + str(path), 0):
                        continue
                    count = status_limits(store, c, route, data['data'], checked)
                    if count:
                        store.source(c, route, 'ok', '', checked)
                    else:
                        existing = c.execute('SELECT status FROM sources WHERE name=?', (route,)).fetchone()
                        if not existing or existing[0] != 'ok':
                            store.source(
                                c,
                                route,
                                'unavailable',
                                '최근 상태줄 이벤트에 한도 필드가 없습니다.',
                                checked,
                            )
                    store.save_state(c, 'status:' + str(path), checked)
            except (OSError, ValueError, KeyError, TypeError, AttributeError):
                with store.connect() as c:
                    # Unread, a file fails the account it last reported; a file of another home
                    # never read is no account's (the default home's is the original account's).
                    if route is None:
                        route = store.state(c, 'status_route:' + str(path)) or (
                            'claude-code' if path.name == 'claude-code.json' else None
                        )
                    if route is None:
                        continue
                    if route in ended:
                        store.source_changed(c, route, 'ended', USER_ENDED_DETAIL)
                    else:
                        store.source(c, route, 'error', '상태줄 이벤트를 읽지 못했습니다.')


def prune_stale_file_state(store, settings):
    """Drop state checkpoints for deleted files under currently mounted roots."""
    roots = []
    for source in _collect().local_sources(store, settings):
        if source['kind'] in ('antigravity', 'devin', 'opencode'):
            continue
        root = Path(source['path'])
        try:
            if root.is_dir():
                roots.append((root, source.get('route')))
        except OSError:
            continue
    if not roots:
        return 0
    removed = 0
    with store.connect() as c:
        pending_claude = bool(
            c.execute("SELECT 1 FROM events WHERE route='claude-code' AND account IS NULL LIMIT 1").fetchone()
        )
        for root, route in roots:
            if route == 'claude-code' and pending_claude:
                continue
            lo, hi = prefix_bounds('file:' + str(root) + '/')
            stale = []
            for (key,) in c.execute('SELECT key FROM state WHERE key>=? AND key<?', (lo, hi)).fetchall():
                try:
                    if not Path(key[5:]).exists():
                        stale.append((key,))
                except OSError:
                    continue
            if stale:
                c.executemany('DELETE FROM state WHERE key=?', stale)
                removed += len(stale)
    return removed


def check_hook_versions(store, settings):
    """Report a non-blocking hint if copied hook/statusline scripts drift behind the running code."""
    from .claudehook import HOOK_VERSION as CLAUDE_HOOK_VERSION
    from .statusline import HOOK_VERSION as STATUSLINE_VERSION

    raw_dirs = [
        settings.get('status_dir', Path(store.path).parent / 'status'),
        *(settings.get('extra_status_dirs') or []),
        *(settings.get('status_inboxes') or []),
    ]
    dirs = []
    seen_dirs = set()
    for p in raw_dirs:
        if not p:
            continue
        folder = Path(p)
        key = str(folder)
        if key not in seen_dirs:
            seen_dirs.add(key)
            dirs.append(folder)
    mismatched = []
    expected = (('statusline.py', STATUSLINE_VERSION), ('claudehook.py', CLAUDE_HOOK_VERSION))
    for folder in dirs:
        for filename, version in expected:
            script = folder / filename
            try:
                if not script.is_file():
                    continue
                head = script.read_text(errors='ignore')[:4096]
            except OSError:
                continue
            match = re.search(r'^HOOK_VERSION\s*=\s*(\d+)\b', head, re.MULTILINE)
            if not match or int(match.group(1)) != version:
                mismatched.append(filename)
    with store.connect() as c:
        if mismatched:
            names = ', '.join(sorted(set(mismatched)))
            store.source_changed(
                c,
                'hook-scripts',
                'partial',
                f'설치된 보조 스크립트({names}) 버전이 현재 수집기와 다릅니다 · llm-usage install-hooks 재실행을 권장합니다.',
            )
        else:
            c.execute("DELETE FROM sources WHERE name='hook-scripts'")


def write_summaries(store, settings):
    """Opt-in (statusline_summary: true): the one-line quota summary the status-line
    wrapper appends below the tool's own line. Inboxes the service cannot write
    (Windows homes under the read-only sandbox) are skipped."""
    from .config import ended_routes
    from .notify import brief_line

    line = brief_line(store.limits(ended=ended_routes(settings), names=settings.get('claude_account_names')))
    for folder in settings.get('status_inboxes', []):
        try:
            path = Path(folder)
            tmp = path / 'summary.txt.tmp'
            tmp.write_text(line + '\n')
            os.replace(tmp, path / 'summary.txt')
        except OSError:
            pass


DAY = 86400
RETRY_AFTER_FAILURE = 3600
# The idle loop turns every 2 seconds; its heartbeat is written this often. The page calls
# the collector stopped after stale_seconds (60 at the least), a restart notice after 10 minutes.
HEARTBEAT_SECONDS = 30
LOCAL_SETTINGS_SOURCE = '웹 설정'


def report_local_settings(store, settings):
    """Say so when local.json cannot be read: the collector then runs on the base settings."""
    problem = local_problem(settings)
    with store.connect() as c:
        if problem:
            store.source_changed(
                c,
                LOCAL_SETTINGS_SOURCE,
                'error',
                f'local.json을 읽지 못했습니다 ({problem}) · 웹에서 바꾼 알림·구독료·종료 구독 설정이 적용되지 않습니다.',
            )
        elif c.execute('SELECT 1 FROM sources WHERE name=?', (LOCAL_SETTINGS_SOURCE,)).fetchone():
            c.execute('DELETE FROM sources WHERE name=?', (LOCAL_SETTINGS_SOURCE,))
            c.execute('DELETE FROM source_health WHERE name=?', (LOCAL_SETTINGS_SOURCE,))
