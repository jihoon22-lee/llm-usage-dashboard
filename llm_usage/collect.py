"""Incremental, restart-safe local collectors. No transcript text is persisted."""

import fcntl
import hashlib
import json
import os
import re
import socket
import sqlite3
import time
from pathlib import Path

from .config import local_path, with_local
from .limits import codex_limits, poll_limits
from .record_files import RecordReadError, compressed, open_record, record_files, unsupported_files
from .store import Store, identity, project_label, stamp
from .util import log_exception, prefix_bounds
from .collect_backfill import (
    _codex_kind,
    _codex_meta,
    backfill_attribution,
    backfill_devin,
    backfill_jsonl,
    backfill_opencode,
    prepare_normalization,
)
from .collect_sources import (
    DAY,
    HEARTBEAT_SECONDS,
    RETRY_AFTER_FAILURE,
    check_hook_versions,
    collect_antigravity_tokens,
    collect_opencode,
    collect_status,
    prune_stale_file_state,
    report_local_settings,
    status_files,
    status_route,
    write_summaries,
)

__all__ = [
    'DAY',
    'HEARTBEAT_SECONDS',
    'PARSER_VERSION',
    'RECORD_ERRORS',
    'RETRY_AFTER_FAILURE',
    'SQL_RECORD_ERRORS',
    '_codex_kind',
    '_codex_meta',
    'backfill_attribution',
    'backfill_devin',
    'backfill_jsonl',
    'backfill_opencode',
    'changed_files',
    'check_hook_versions',
    'collect_antigravity_tokens',
    'collect_file',
    'collect_local',
    'collect_opencode',
    'collect_status',
    'due',
    'file_parser_version',
    'local_sources',
    'model_provider',
    'parse_line',
    'prepare_normalization',
    'prune_stale_file_state',
    'repair_provider_labels',
    'report_local_settings',
    'retire_legacy_sources',
    'run',
    'sd_notify',
    'status_files',
    'status_route',
    'sync_thresholds',
    'write_summaries',
]

# What one malformed record can raise. A value SQLite cannot bind or store (a list where a
# session id belongs, a NaN time) fails only its own statement, so the record is counted
# as unreadable and the rest of the source is still read.
SQL_RECORD_ERRORS = (sqlite3.IntegrityError, sqlite3.ProgrammingError, sqlite3.InterfaceError, sqlite3.DataError)
# RecursionError: a line nested deeper than the JSON parser follows is one unreadable record
# too, not a reason to stop reading its file and every file after it.
RECORD_ERRORS = (ValueError, TypeError, KeyError, AttributeError, OverflowError, RecursionError, *SQL_RECORD_ERRORS)

# 4 reads the reasoning effort (Codex turn settings, Claude responses) from records read before.
PARSER_VERSION = 4


def model_provider(model):
    m = (model or "").lower()
    for provider, terms in [
        ('Anthropic', ('claude',)),
        ('OpenAI', ('gpt', 'o1', 'o3', 'o4', 'codex')),
        ('Google', ('gemini',)),
        ('Meta', ('muse-spark',)),
        ('Z.ai', ('glm',)),
        ('MiniMax', ('minimax',)),
        ('Moonshot', ('kimi', 'moonshot')),
        ('Alibaba', ('qwen',)),
        ('DeepSeek', ('deepseek',)),
        ('Cognition', ('swe',)),
    ]:
        if any(term in m for term in terms):
            return provider
    return 'Unknown'


def repair_provider_labels(store):
    with store.connect() as c:
        if store.state(c, 'provider-labels-v2'):
            return
        # Stored model IDs remain useful even if the original tool removed its message.
        for table in ('events', 'codex_points'):
            rows = c.execute(
                f"SELECT DISTINCT model,route FROM {table} WHERE provider IN ('Unknown','Other')"
            ).fetchall()
            for row in rows:
                maker = model_provider(row['model'])
                if maker == 'Unknown' and row['route'] == 'codex':
                    maker = 'OpenAI'
                c.execute(
                    f"UPDATE {table} SET provider=? WHERE model=? AND route=? AND provider IN ('Unknown','Other')",
                    (maker, row['model'], row['route']),
                )
        store.save_state(c, 'provider-labels-v2', True)
        store.bump_data_version(c)


# Status rows no current collector writes; left alone they linger as warnings.
RETIRED_SOURCES = ('codex-records',)


def retire_legacy_sources(store):
    with store.connect() as c:
        if store.state(c, 'legacy-sources-v1'):
            return
        marks = ','.join('?' for _ in RETIRED_SOURCES)
        c.execute(f'DELETE FROM sources WHERE name IN ({marks})', RETIRED_SOURCES)
        c.execute(f'DELETE FROM source_health WHERE name IN ({marks})', RETIRED_SOURCES)
        store.save_state(c, 'legacy-sources-v1', True)


def file_parser_version(route):
    # Replay Claude transcripts and Aside copies with the corrected cache and attribution rules.
    return 5 if route in ('claude-code', 'aside') else PARSER_VERSION


def parse_line(store, c, route, d, state):
    if route == 'aside':
        from .aside import ingest_line

        ingest_line(store, c, d, state)
        return
    typ = d.get('type')
    p = d.get('payload', {})
    if route == 'codex':
        if typ == 'session_meta' and not state.get('session'):
            # Fork rollouts embed a second, parent session_meta. The first ID owns this file.
            state['session'] = p.get('id') or p.get('session_id')
            state['provider'] = p.get('model_provider') or 'openai'
            state['project'] = project_label(p.get('cwd'))
            state['agent_kind'] = _codex_kind(p)
            creator = p.get('creator_account_id')
            if isinstance(creator, str) and re.fullmatch(r'[A-Za-z0-9-]{1,100}', creator):
                state['account'] = 'codex-account:' + identity(creator)
            if p.get('forked_from_id'):
                state['fork_started'] = stamp(p.get('timestamp') or d.get('timestamp'))
        elif typ == 'turn_context':
            if (
                not state.get('fork_started')
                or (stamp(d.get('timestamp')) or state['fork_started']) >= state['fork_started']
            ):
                state['has_turn_context'] = True
                if p.get('model'):
                    state['model'] = p['model']
                if state.get('session') and store.turn_effort(
                    c, state['session'], stamp(d.get('timestamp')), p.get('effort') or p.get('reasoning_effort')
                ):
                    state['_effort'] = True
        elif typ == 'token_usage_record':
            ts = stamp(d.get('timestamp'))
            if not state.get('session') or not ts:
                return
            if state.get('fork_started') and (ts < state['fork_started'] or not state.get('has_turn_context')):
                return
            route_name = 'codex' if state.get('provider', 'openai') == 'openai' else 'codex:' + state['provider']
            provider = model_provider(state.get('model', ''))
            if provider == 'Unknown' and state.get('provider') == 'openai':
                provider = 'OpenAI'
            from .codex_usage import record

            record(store, c, p, ts, state, provider, route_name)
        elif typ == 'event_msg' and p.get('type') == 'token_count':
            ts = stamp(d.get('timestamp'))
            if p.get('rate_limits') and ts:
                codex_limits(store, c, p['rate_limits'], ts, 'codex-local')
            usage = (p.get('info') or {}).get('total_token_usage')
            if not usage or not state.get('session') or not ts:
                return
            if state.get('fork_started') and (ts < state['fork_started'] or not state.get('has_turn_context')):
                changed = store.discard_codex_point(c, state['session'], ts, usage)
                if changed:
                    state['_dirty'] = changed
                return
            route_name = 'codex' if state.get('provider', 'openai') == 'openai' else 'codex:' + state['provider']
            provider = model_provider(state.get('model', ''))
            if provider == 'Unknown' and state.get('provider') == 'openai':
                provider = 'OpenAI'
            changed = store.codex_point(
                c,
                state['session'],
                ts,
                provider,
                route_name,
                state.get('model'),
                usage,
                p['info'].get('last_token_usage') or usage,
            )
            if changed:
                state['_dirty'] = changed
            from .codex_usage import bind_pending

            keys = (
                'input_tokens',
                'cached_input_tokens',
                'output_tokens',
                'cache_write_input_tokens',
                'reasoning_output_tokens',
            )
            bind_pending(
                c, state, identity('codex-point', state['session'], ts, [max(0, int(usage.get(k) or 0)) for k in keys])
            )
    elif typ == 'assistant':
        from .claude_usage import ingest_message

        ingest_message(store, c, route, d)


# Bytes just before the read offset identify the consumed prefix (as a digest only).
TAIL_BYTES = 64


def _prefix_changed(path, checkpoint):
    """An in-place rewrite that grows (or keeps) the size reuses the inode; resuming
    from the old offset would then start in the middle of a different line."""
    offset = checkpoint.get('offset', 0)
    if not offset:
        return False
    with path.open('rb') as f:
        f.seek(max(0, offset - TAIL_BYTES))
        tail = f.read(offset - max(0, offset - TAIL_BYTES))
    if checkpoint.get('tail'):
        return hashlib.sha256(tail).hexdigest() != checkpoint['tail']
    return not tail.endswith(b'\n')


def _misaligned_errors(c, path, key):
    """Errors recorded at offsets that are no longer line starts came from reading a
    rewritten file mid-line; such a file is read again from the start once."""
    offsets = [
        int(r[0]) for r in c.execute('SELECT record FROM import_errors WHERE source=?', (key,)) if str(r[0]).isdigit()
    ]
    if not offsets:
        return False
    with path.open('rb') as f:
        for offset in offsets:
            if not offset:
                continue
            f.seek(offset - 1)
            if f.read(1) != b'\n':
                return True
    return False


def collect_file(store, path, route):
    stat = path.stat()
    key = 'file:' + str(path)
    with store.connect() as c:
        checkpoint = store.state(c, key, {})
        replay = checkpoint.get('parser_version', 0) < file_parser_version(route)
        unchanged = (
            checkpoint.get('size') == stat.st_size
            and checkpoint.get('mtime') == stat.st_mtime_ns
            and checkpoint.get('inode') == stat.st_ino
        )
        packed = compressed(path)
        misaligned = False if packed else _misaligned_errors(c, path, key)
        if not replay and unchanged and not misaligned:
            return store.error_count(c, key)
        offset = checkpoint.get('offset', 0)
        state = checkpoint.get('parser', {})
        # A resumed file may not repeat session_meta after a code upgrade.
        project = state.get('project')
        if isinstance(project, str) and ('/' in project or '\\' in project or ':' in project):
            state['project'] = project_label(project)
        rewritten = (
            checkpoint.get('size') == stat.st_size and checkpoint.get('mtime') != stat.st_mtime_ns
        ) or misaligned
        if (
            packed
            or replay
            or rewritten
            or checkpoint.get('inode') != stat.st_ino
            or stat.st_size < offset
            or _prefix_changed(path, checkpoint)
        ):
            # Event IDs deduplicate the lines read again.
            offset = 0
            state = {}
            c.execute('DELETE FROM import_errors WHERE source=?', (key,))
        if route == 'aside':
            # The lines do not name their session: it is the folder the file sits in.
            from .aside import session_id

            state['session'] = session_id(path)
        with open_record(path) as f:
            f.seek(offset)
            while True:
                start = f.tell()
                line = f.readline()
                if not line:
                    break
                if not line.endswith(b'\n'):
                    f.seek(start)
                    break
                try:
                    parse_line(store, c, route, json.loads(line), state)
                except RECORD_ERRORS as exc:
                    store.import_error(c, key, start, type(exc).__name__)
            if state.get('_dirty'):
                store.rebuild_codex(
                    c,
                    state.pop('_dirty'),
                    dict(
                        session=state.get('session'), project=state.get('project'), agent_kind=state.get('agent_kind')
                    ),
                )
            if state.pop('_effort', None):
                store.apply_turn_efforts(c, state['session'])
            end = f.tell()
            f.seek(max(0, end - TAIL_BYTES))
            tail = hashlib.sha256(f.read(end - max(0, end - TAIL_BYTES))).hexdigest()
            store.save_state(
                c,
                key,
                dict(
                    parser_version=file_parser_version(route),
                    offset=end,
                    tail=tail,
                    size=stat.st_size,
                    mtime=stat.st_mtime_ns,
                    inode=stat.st_ino,
                    parser=state,
                ),
            )
            return store.error_count(c, key)


def changed_files(store, root, paths, route=None):
    """The record files that need collect_file(): changed since their checkpoint or
    carrying recorded errors. One query replaces a connection per unchanged file,
    which mattered with thousands of finished session files."""
    prefix = 'file:' + str(root) + '/'
    lo, hi = prefix_bounds(prefix)
    with store.connect() as c:
        saved = {r[0]: r[1] for r in c.execute('SELECT key,data FROM state WHERE key>=? AND key<?', (lo, hi))}
        failing = {
            r[0] for r in c.execute('SELECT DISTINCT source FROM import_errors WHERE source>=? AND source<?', (lo, hi))
        }
    for path in paths:
        key = 'file:' + str(path)
        try:
            stat = path.stat()
        except OSError:
            continue
        try:
            checkpoint = json.loads(saved[key]) if key in saved else {}
        except ValueError:
            checkpoint = {}
        unchanged = (
            checkpoint.get('parser_version', 0) >= file_parser_version(route)
            and checkpoint.get('size') == stat.st_size
            and checkpoint.get('mtime') == stat.st_mtime_ns
            and checkpoint.get('inode') == stat.st_ino
        )
        if unchanged and key not in failing:
            continue
        yield path


def local_sources(store, settings):
    from .claude_accounts import observed_homes, transcript_sources

    from .aside import sources as aside_sources

    with store.connect() as c:
        known = observed_homes(store, c)
    return transcript_sources(settings, known) + aside_sources(settings)


def collect_local(store, settings, *, include_derived=True):
    progress = time.monotonic()

    def heartbeat():
        nonlocal progress
        if time.monotonic() - progress < 5:
            return
        sd_notify('WATCHDOG=1')
        with store.connect() as c:
            current = store.state(c, 'collector', {})
            if current.get('status') == 'collecting':
                store.save_state(c, 'collector', {**current, 'checked': time.time()})
        progress = time.monotonic()

    for source in local_sources(store, settings):
        if source['kind'] in ('antigravity', 'devin'):
            continue
        root = Path(source['path'])
        route = source['route']
        name = source['name']
        try:
            if not root.exists():
                with store.connect() as c:
                    store.source(c, name, 'unavailable', '기록 경로를 사용할 수 없습니다.')
                continue
            if source['kind'] == 'opencode':
                errors = collect_opencode(store, root)
                count = 1
            else:
                paths = record_files(root)
                count = len(paths)
                for path in changed_files(store, root, paths, route):
                    try:
                        collect_file(store, path, route)
                        # The file was read: an earlier whole-file failure (a locked or
                        # briefly unreadable file) is over.
                        with store.connect() as c:
                            c.execute(
                                "DELETE FROM import_errors WHERE source=? AND record='file'", ('file:' + str(path),)
                            )
                    except (OSError, EOFError, ValueError, RecordReadError, *SQL_RECORD_ERRORS) as exc:
                        with store.connect() as c:
                            store.import_error(c, 'file:' + str(path), 'file', type(exc).__name__)
                    # A full parser upgrade can span several minutes. This marks
                    # committed progress, while status remains explicitly collecting.
                    heartbeat()
                # Errors of the files present now, as the per-file counts used to sum.
                current = {'file:' + str(path) for path in paths}
                lo, hi = prefix_bounds('file:' + str(root) + '/')
                with store.connect() as c:
                    errors = sum(
                        n
                        for src_key, n in c.execute(
                            'SELECT source,COUNT(*) FROM import_errors WHERE source>=? AND source<? GROUP BY source',
                            (lo, hi),
                        )
                        if src_key in current
                    )
            lo, hi = prefix_bounds('file:' + str(root) + '/')
            with store.connect() as c:
                # File-level failures are single corrupt lines that never recover;
                # after 7 days archive them as a permanent count so the source can
                # return to 'ok' while the loss stays reported.
                aged = c.execute(
                    'DELETE FROM import_errors WHERE source>=? AND source<? AND checked<?',
                    (lo, hi, time.time() - 7 * 86400),
                ).rowcount
                perm = store.state(c, 'permanent_errors:' + name, 0) + aged
                if aged:
                    store.save_state(c, 'permanent_errors:' + name, perm)
                    if source['kind'] != 'opencode':
                        errors = sum(
                            n
                            for src_key, n in c.execute(
                                'SELECT source,COUNT(*) FROM import_errors WHERE source>=? AND source<? GROUP BY source',
                                (lo, hi),
                            )
                            if src_key in current
                        )
                unsupported = unsupported_files(root) if source['kind'] == 'jsonl' else 0
                detail = f'{count}개 기록 파일 확인 · 해석 실패 {errors}건' + (
                    f' · 누적 영구 실패 {perm}건' if perm else ''
                )
                if unsupported:
                    detail += f' · 미지원 압축 기록 {unsupported}개'
                store.source(c, name, 'partial' if errors or unsupported else 'ok', detail)
            heartbeat()
        except Exception as exc:
            log_exception(f'collect_local {name}', exc)
            with store.connect() as c:
                store.source(c, name, 'error', f'로컬 수집 실패 ({type(exc).__name__})')
    if include_derived:
        from .antigravity import collect_databases

        collect_databases(store, settings, model_provider)
        from .devin import collect_databases as collect_devin

        collect_devin(store, settings, model_provider)
    with store.connect() as c:
        from .codex_usage import summary

        summary(store, c)
        from .claude_usage import summary as claude_summary

        claude_summary(store, c)
        store.save_state(c, 'last_local', time.time())


def sd_notify(message):
    """systemd notification without a dependency; a no-op outside systemd.

    With WatchdogSec set on the unit, a loop that stops pinging (a hang, not only
    an exit) is restarted by systemd."""
    address = os.environ.get('NOTIFY_SOCKET')
    if not address:
        return
    if address.startswith('@'):
        address = '\0' + address[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.connect(address)
            sock.sendall(message.encode())
    except OSError:
        pass


def due(last, ok, now):
    """Daily jobs; a failed run is retried after an hour instead of a day."""
    return now - last >= (DAY if ok else RETRY_AFTER_FAILURE)


def sync_thresholds(store, settings, seen):
    """Apply thresholds edited in the web UI (local.json) without a restart.

    Returns the local.json mtime to pass back as `seen` on the next call.
    """
    try:
        mtime = local_path(settings).stat().st_mtime_ns
    except OSError:
        mtime = None
    if mtime != seen:
        store.thresholds = {**Store.DEFAULT_THRESHOLDS, **(with_local(settings).get('thresholds') or {})}
    return mtime


def run(settings, once=False):
    store = Store(settings['database'], thresholds=settings.get('thresholds'))
    lock = Path(settings['database']).parent / 'collector.lock'
    with lock.open('w') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            if once:
                return
            fcntl.flock(handle, fcntl.LOCK_EX)
        repair_provider_labels(store)
        retire_legacy_sources(store)
        from .claude_usage import prepare_replay

        prepare_replay(store)
        store.rebuild_rollup()
        last_local = last_limits = last_accounts = 0
        with store.connect() as c:
            backup = store.state(c, 'backup', {}) or {}
            verify = store.state(c, 'verify', {}) or {}
        from .verify import VERIFY_FORMAT

        last_backup, backup_ok = backup.get('ts', 0), backup.get('ok', True)
        last_verify, verify_ok = (
            verify.get('ts', 0) if verify.get('format') == VERIFY_FORMAT else 0,
            verify.get('ok', True),
        )
        local_seen = False
        last_beat = 0
        from .notify import collector_started, evaluate as notify_evaluate, record_failure as notify_failure

        try:
            collector_started(store, with_local(settings).get('notify'))
        except Exception as exc:
            log_exception('collector_started', exc)
            notify_failure(store, exc)  # never stops collection
        sd_notify('READY=1')
        while True:
            sd_notify('WATCHDOG=1')
            local_seen = sync_thresholds(store, settings, local_seen)
            # Web-edited keys (ended subscriptions, account names) apply without a restart.
            current_settings = with_local(settings)
            collect_status(store, current_settings)
            # Often enough to see short-lived processes (a commit review lives ~20 s).
            if time.time() - last_accounts >= 5:
                # Which Claude account handles each session: hook markers and live processes.
                from .claude_attribution import refresh as attribute_accounts

                attribute_accounts(store, current_settings)
                last_accounts = time.time()
            with store.connect() as c:
                request = store.state(c, 'refresh_request_id', 0)
                requested_at = store.state(c, 'refresh_requested', 0)
                completed = store.state(c, 'refresh_completed_id', 0)
            now = time.time()
            manual = request > completed
            collecting = once or manual or now - last_local >= 300
            if collecting:
                with store.connect() as c:
                    store.save_state(c, 'collector', dict(status='collecting', started=now, checked=now))
                if prepare_normalization(store, settings):
                    backfill_attribution(store, settings)
                    collect_local(store, settings)
                    from .telemetry import resolve

                    with store.connect() as c:
                        resolve(store, c)
                    from .claude_attribution import refresh as attribute_accounts

                    from .aside import attribute as attribute_aside

                    # Before the resolver: it assigns the requests this marks.
                    attribute_aside(store, current_settings)
                    attribute_accounts(store, current_settings, evidence=False)
                last_local = time.time()
            manual_acked = False
            if once or manual or now - last_limits >= 300:
                fast_manual = manual and not once
                poll_meta = poll_limits(store, current_settings, manual=manual, include_slow=not fast_manual) or {}
                if fast_manual:
                    # Interactive refresh: unblock the web UI as soon as local files and live quotas finish,
                    # then run heavier cloud-session and Codex activity sync immediately after.
                    with store.connect() as c:
                        store.save_state(c, 'refresh_completed_id', request)
                        store.save_state(c, 'refresh_completed', requested_at)
                    manual_acked = True
                    from .limits import poll_slow_sources

                    poll_slow_sources(
                        store,
                        current_settings,
                        claude_accounts=poll_meta.get('claude_accounts'),
                        poll_codex_threads=bool(poll_meta.get('codex_thread_ready')),
                    )
                from .codex_activity_collect import collect as collect_activity

                collect_activity(store, current_settings)
                last_limits = time.time()
                if current_settings.get('statusline_summary'):
                    write_summaries(store, current_settings)
                report_local_settings(store, settings)
                check_hook_versions(store, current_settings)
                if not once:
                    from .pricing import load_pricing
                    from .reconciliation import held

                    current = time.time()
                    local = with_local(settings)
                    # Three steps that fail apart: a broken one is named and never keeps the
                    # other two, or collection, from running.
                    try:
                        from .signals import usage_spike

                        with store.connect() as c:
                            spike = usage_spike(c, current)
                            sources = [dict(r) for r in c.execute('SELECT name,status,detail FROM sources')]
                            month = store.project_month(c, current, load_pricing(local))
                        from .config import ended_routes

                        notify_evaluate(
                            store,
                            local.get('notify'),
                            current,
                            store.limits(current, ended_routes(local), local.get('claude_account_names')),
                            spike,
                            sources,
                            month,
                            local.get('project_budgets'),
                        )
                    except Exception as exc:
                        log_exception('notify_evaluate', exc)
                        notify_failure(store, exc)
                    # The weekly and monthly checks read the same supplements.
                    with held():
                        try:
                            from .reports import ensure_report, summary as report_summary

                            weekly = ensure_report(store, current, load_pricing(local))
                            if weekly:
                                from .notify import deliver

                                deliver(
                                    store,
                                    local.get('notify'),
                                    [
                                        (
                                            'report',
                                            f"주간 리포트 {weekly['start']} ~ {weekly['end']}",
                                            report_summary(
                                                weekly, local.get('residual_label'), local.get('claude_account_names')
                                            ),
                                        )
                                    ],
                                    current,
                                )
                        except Exception as exc:
                            log_exception('weekly report', exc)
                            notify_failure(store, exc, '주간 리포트 생성')
                        try:
                            from .monthly import ensure_months

                            ensure_months(
                                store,
                                current,
                                load_pricing(local),
                                local.get('subscription_prices'),
                                local.get('ended_subscriptions'),
                                local.get('subscription_price_history'),
                            )
                        except Exception as exc:
                            log_exception('monthly report', exc)
                            notify_failure(store, exc, '월 정산 생성')
            sd_notify('WATCHDOG=1')
            if not once and due(last_backup, backup_ok, now):
                from .backup import backup_now

                result = backup_now(settings['database'])
                with store.connect() as c:
                    store.save_state(c, 'backup', result)
                    # Refresh the planner's statistics once a day; cheap when nothing changed.
                    c.execute('PRAGMA optimize')
                prune_stale_file_state(store, current_settings)
                last_backup = time.time()
                backup_ok = bool(result.get('ok'))
            sd_notify('WATCHDOG=1')
            if not once and due(last_verify, verify_ok, now):
                from .verify import failed, reconcile

                try:
                    result = reconcile(settings['database'], settings)
                    sections = {
                        section: {k: v for k, v in counts.items() if k != 'mismatch_samples'}
                        for section, counts in result.items()
                    }
                    # The hourly rollup must equal the events it summarises; rebuild it if not.
                    rollup_ok = store.rollup_matches()
                    if not rollup_ok:
                        store.rebuild_rollup()
                    outcome = dict(
                        ts=time.time(),
                        format=VERIFY_FORMAT,
                        ok=not failed(result) and rollup_ok,
                        rollup=dict(matches=rollup_ok),
                        **sections,
                    )
                except Exception as e:
                    log_exception('daily reconcile', e)
                    from .backup import error_text

                    outcome = dict(ts=time.time(), format=VERIFY_FORMAT, ok=False, error=error_text(e))
                with store.connect() as c:
                    store.save_state(c, 'verify', outcome)
                last_verify = time.time()
                verify_ok = bool(outcome.get('ok'))
            with store.connect() as c:
                # After a pass, and twice a minute while idle: not a write every two seconds.
                if collecting or manual or time.time() - last_beat >= HEARTBEAT_SECONDS:
                    last_beat = time.time()
                    store.save_state(c, 'collector', dict(status='idle', checked=last_beat))
                # A request arriving during these calls stays pending for the next loop.
                if manual and not manual_acked:
                    store.save_state(c, 'refresh_completed_id', request)
                    store.save_state(c, 'refresh_completed', requested_at)
                store.prune_history(c)
            if once:
                return
            time.sleep(2)
