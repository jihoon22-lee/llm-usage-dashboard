"""Shared HTTP redirect guard, retry-delay parsing, status-line limits, and polling helpers."""

from datetime import datetime
from email.utils import parsedate_to_datetime
import math
import re
import time
import urllib.error
import urllib.request

from ..store import KST
from ..util import is_finite_number as number, log_exception

MAX_RETRY_AFTER = 86400
AGY_BASE_BUCKETS = frozenset({'gemini-5h', 'gemini-weekly', '3p-5h', '3p-weekly'})
AGY_POOL_BUCKET = re.compile(r'(gemini|3p)-[a-z0-9-]+-(5h|weekly)(?:-\d+)?')


class AccountMetadataMissing(RuntimeError):
    """A missing native account profile is retryable without rotating credentials."""


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def retry_delay(value, now):
    try:
        delay = float(value)
        return max(0, delay) if math.isfinite(delay) else 0
    except (TypeError, ValueError):
        try:
            return max(0, parsedate_to_datetime(value).timestamp() - now)
        except (TypeError, ValueError, OverflowError):
            return 0


def status_limits(store, c, route, data, checked):
    from ..claude_accounts import is_claude

    count = 0
    if is_claude(route):
        for key, row in (data.get('rate_limits') or {}).items():
            if not isinstance(row, dict):
                continue
            used = row.get('used_percentage')
            if number(used):
                store.limit(c, route, key, max(0, 100 - used), row.get('resets_at'), checked, route)
                count += 1
    elif route == 'antigravity':
        # Bucket IDs are the provider's shared quota identity, never copied per model.
        written = set()
        for key, row in (data.get('quota') or {}).items():
            if not isinstance(row, dict):
                continue
            remaining = row.get('remaining_fraction')
            if number(remaining):
                reset = row.get('reset_time')
                if reset is None and number(row.get('reset_in_seconds')):
                    reset = checked + row['reset_in_seconds']
                if store.limit(c, route, key, max(0, min(100, remaining * 100)), reset, checked, route):
                    written.add(key)
                count += 1
        if AGY_BASE_BUCKETS <= written:
            prev = store.state(c, 'quota_snapshot:antigravity')
            prev_checked = prev.get('checked', 0) if isinstance(prev, dict) else 0
            if not number(prev_checked) or prev_checked <= checked:
                store.save_state(
                    c,
                    'quota_snapshot:antigravity',
                    dict(checked=checked, source=route, buckets=sorted(written)),
                )
            for bucket, row_checked in c.execute(
                "SELECT bucket,checked FROM limits WHERE route='antigravity'"
            ).fetchall():
                if (
                    bucket not in written
                    and AGY_POOL_BUCKET.fullmatch(bucket)
                    and (not number(row_checked) or row_checked < checked)
                ):
                    c.execute("DELETE FROM limits WHERE route='antigravity' AND bucket=?", (bucket,))
    return count


def account_poll(store, path, state_key, source, auth_hint, read, record, interval=300):
    """One account-quota read with a persistent cooldown shared by automatic and manual runs.

    read() returns the parsed response (raising PermissionError when the stored login
    cannot be used); record(c,data,checked) stores it and returns (count, detail).
    401/403 and missing credentials pause polling until the credential file changes;
    429 backs off 10/20/40/60 minutes, other failures 1/2/4/8/10 minutes, and a longer
    Retry-After wins."""
    now = time.time()
    try:
        signature = path.stat().st_mtime_ns
    except OSError:
        signature = None
    with store.connect() as c:
        state = store.state(c, state_key, {})
    if state.get('auth_blocked') and state.get('credential_mtime') == signature:
        return
    # A changed credential permits recovery from auth errors, but not from 429.
    if not state.get('auth_blocked') and now < state.get('next_attempt', 0):
        return
    failures = state.get('failures', 0)
    try:
        data = read()
        checked = time.time()
        with store.connect() as c:
            n, detail = record(c, data, checked)
            store.source(
                c,
                source,
                'ok' if n else 'unavailable',
                detail if n else '계정 응답에 해석 가능한 한도 필드가 없습니다.',
                checked,
            )
            store.save_state(
                c, state_key, dict(next_attempt=checked + interval, failures=0, credential_mtime=signature)
            )
    except Exception as exc:
        log_exception(f'account poll {source}', exc)
        failures += 1
        checked = time.time()
        code = exc.code if isinstance(exc, urllib.error.HTTPError) else None
        blocked = code in (401, 403) or isinstance(exc, (PermissionError, FileNotFoundError))
        delay = min(3600, 600 * 2 ** min(failures - 1, 3)) if code == 429 else min(600, 60 * 2 ** min(failures - 1, 4))
        if isinstance(exc, urllib.error.HTTPError):
            delay = max(delay, min(MAX_RETRY_AFTER, retry_delay(exc.headers.get('Retry-After'), checked)))
            exc.close()
        if blocked:
            detail = auth_hint
        elif isinstance(exc, AccountMetadataMissing):
            detail = '기본 로그인 계정·조직 정보 확인 필요 · 기본 도구에서 계정 정보를 갱신하세요.'
        elif code == 429:
            try:
                waiting = datetime.fromtimestamp(checked + delay, KST).strftime('%m/%d %H:%M KST') + ' 이후 재시도'
            except (ValueError, OverflowError, OSError):
                waiting = '제공사 지정 대기 시간 적용'
            detail = '한도 조회 제한(429) · ' + waiting
        else:
            detail = f'계정 한도 조회 실패 ({"HTTP " + str(code) if code else type(exc).__name__})'
        with store.connect() as c:
            store.source(c, source, 'error', detail, checked)
            store.save_state(
                c,
                state_key,
                dict(next_attempt=checked + delay, failures=failures, auth_blocked=blocked, credential_mtime=signature),
            )


def mark_ended(store, *names):
    """A subscription marked ended in the settings is not polled; its existing sources
    say why (an earlier ok or error row must not stay live)."""
    from ..store import USER_ENDED_DETAIL

    with store.connect() as c:
        for name in names:
            if c.execute('SELECT 1 FROM sources WHERE name=?', (name,)).fetchone():
                store.source_changed(c, name, 'ended', USER_ENDED_DETAIL)
