"""Codex App Server quota, account usage, and thread usage polling."""

from datetime import datetime
import json
import os
import re
import selectors
import subprocess
import time

from ..store import stamp
from ..util import is_finite_number as number, log_exception


def _codex_account_metadata(data, profile):
    from ..store import identity

    profile = profile if isinstance(profile, dict) else {}
    routing = profile.get('workspaceRouting')
    routing = routing if isinstance(routing, dict) else {}
    ident = data.get('accountId') if isinstance(data, dict) else None
    if not isinstance(ident, str) or not ident:
        ident = routing.get('chatgptAccountId')
    workspace = data.get('workspaceId') if isinstance(data, dict) else None
    native = profile.get('account')
    native = native if isinstance(native, dict) else {}
    native_type, email = native.get('type'), native.get('email')
    revision = (
        identity(native_type, email)
        if (native_type == 'chatgpt' and isinstance(email, str) and email) or native_type in ('apiKey', 'amazonBedrock')
        else None
    )
    return ((ident, workspace) if isinstance(ident, str) and ident else None), revision


def codex_limits(store, c, data, checked, source='codex', account_profile=None):
    from ..resources import observe_account, account, codex_resources, SKEW, timestamp

    if not isinstance(data, dict):
        raise ValueError('한도 응답 형식')
    if not number(checked) or checked > time.time() + SKEW:
        return 0
    resource_count = 0
    if source == 'codex':
        ident, revision = _codex_account_metadata(data, account_profile)
        groups_for_policy = data.get('rateLimitsByLimitId') or {'codex': data.get('rateLimits') or {}}
        plans = sorted(
            {
                str(row['planType'])
                for row in groups_for_policy.values()
                if isinstance(row, dict) and row.get('planType')
            }
        )
        owner = observe_account(
            c,
            'codex',
            ident,
            checked,
            plan_revision=plans or None,
            credential_revision=revision
            if account_profile is not None
            else (account(c, 'codex') or {}).get('credential_revision'),
            retain_matching_identity=account_profile is not None,
        )
        if owner and owner['checked'] > checked:
            return 0
        from ..codex_usage import bind_account

        bind_account(store, c, ident, owner['account_key'] if owner else None, checked)
        resource_count = codex_resources(c, data, checked)
        store.save_state(
            c,
            'quota_policy:codex',
            dict(
                checked=checked,
                ordinary_allowed=data.get('ordinaryUsageAllowed')
                if isinstance(data.get('ordinaryUsageAllowed'), bool)
                else None,
                spending_blocked=any(
                    row.get('spendControlReached') is True
                    for row in groups_for_policy.values()
                    if isinstance(row, dict)
                ),
            ),
        )
    groups = data.get('rateLimitsByLimitId')
    if not groups:
        row = data.get('rateLimits', data)
        groups = {row.get('limitId', row.get('limit_id', 'codex')): row}
    count = 0
    observed = []
    for group, row in groups.items():
        if not isinstance(row, dict):
            continue
        for window in ('primary', 'secondary'):
            w = row.get(window)
            if not isinstance(w, dict):
                continue
            used = w.get('usedPercent', w.get('used_percent'))
            if not number(used) or used < 0:
                continue
            minutes = w.get('windowDurationMins', w.get('window_minutes'))
            if not number(minutes) or not 0 < minutes <= 1000000 or int(minutes) != minutes:
                minutes = None
            raw_reset = w.get('resetsAt', w.get('resets_at'))
            reset = timestamp(raw_reset)
            label = f'{group} · {minutes}분' if minutes else f'{group} · {window}'
            title = row.get('limitName', row.get('limit_name')) or ('Codex 공통' if group == 'codex' else group)
            duration = {300: '5시간', 10080: '주간'}.get(
                minutes, f'{minutes}분' if minutes else '기본 창' if window == 'primary' else '추가 창'
            )
            if store.limit(
                c,
                'codex',
                label,
                max(0, 100 - used),
                reset,
                checked,
                source,
                dict(
                    role='common' if group == 'codex' else 'model',
                    group='common' if group == 'codex' else str(group)[:100],
                    label='공통 한도' if group == 'codex' else str(title)[:100],
                    locked=bool(row.get('rateLimitReachedType')),
                    reset_unparsed=raw_reset is not None and reset is None,
                ),
            ):
                store.save_state(c, 'label:codex:' + label, str(title)[:100] + ' · ' + duration)
                count += 1
                observed.append(label)
    if source == 'codex':
        store.save_state(c, 'quota_snapshot:codex', dict(checked=checked, source=source, buckets=observed))
    return count + resource_count


def read_codex(binary, timeout=35, extra=None, extra_grace=10, extra_params=None):
    """account/rateLimits/read through a short-lived app-server. `extra` maps further
    read methods to None; their results (or None on error/silence) are filled in from
    the same process, waiting at most `extra_grace` seconds past the rate limits."""
    extra = extra if extra is not None else {}
    ids = {3 + i: method for i, method in enumerate(extra)}
    limits = None
    limits_done = False
    limits_error = None
    p = subprocess.Popen(
        [binary, 'app-server', '--stdio'], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
    )
    selector = selectors.DefaultSelector()
    selector.register(p.stdout, selectors.EVENT_READ)
    pending = b''

    def send(obj):
        p.stdin.write(json.dumps(obj).encode() + b'\n')
        p.stdin.flush()

    try:
        send(dict(id=1, method='initialize', params=dict(clientInfo=dict(name='llm_usage', version='0.1.0'))))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not selector.select(timeout=1):
                continue
            # Raw reads: a buffered readline() can keep complete lines in Python's
            # buffer, where select() never reports them again.
            chunk = os.read(p.stdout.fileno(), 65536)
            if not chunk:
                raise RuntimeError('app-server 종료')
            pending += chunk
            while b'\n' in pending:
                line, pending = pending.split(b'\n', 1)
                if not line.strip():
                    continue
                reply = json.loads(line)
                if reply.get('id') == 1:
                    if 'error' in reply:
                        raise RuntimeError('app-server 초기화 실패')
                    send(dict(method='initialized'))
                    send(dict(id=2, method='account/rateLimits/read'))
                    for rid, method in ids.items():
                        msg = dict(id=rid, method=method)
                        if extra_params and method in extra_params:
                            msg['params'] = extra_params[method]
                        send(msg)
                elif reply.get('id') == 2:
                    limits_done = True
                    if 'error' in reply:
                        limits_error = RuntimeError('Codex 한도 조회 실패')
                    else:
                        limits = reply.get('result')
                    deadline = min(deadline, time.monotonic() + extra_grace)
                elif reply.get('id') in ids:
                    extra[ids.pop(reply['id'])] = reply.get('result') if 'error' not in reply else None
                if limits_done and not ids:
                    if limits_error:
                        raise limits_error
                    return limits
        if limits_error:
            raise limits_error
        if limits is not None:
            return limits
        raise TimeoutError('Codex 한도 응답 시간 초과')
    finally:
        selector.close()
        p.terminate()
        try:
            p.wait(timeout=5)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait()
        p.stdin.close()
        p.stdout.close()


def codex_usage_buckets(usage):
    """None means no usable response; an explicitly empty list is valid."""
    from ..reconciliation import number

    raw = usage.get('dailyUsageBuckets') if isinstance(usage, dict) else None
    if not isinstance(raw, list):
        return None
    buckets = []
    for b in raw:
        if not isinstance(b, dict) or not number(b.get('tokens')):
            continue
        day = b.get('startDate')
        if not isinstance(day, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', day):
            continue
        try:
            datetime.strptime(day, '%Y-%m-%d')
        except ValueError:
            continue
        buckets.append(b)
    return buckets if buckets or not raw else None


def record_codex_account(store, usage):
    """Daily token totals of the whole ChatGPT account, every surface included: the
    only trace of agents that run on OpenAI's side (ChatGPT dot, Codex cloud)."""
    with store.connect() as c:
        buckets = codex_usage_buckets(usage)
        if buckets is None:
            store.source(
                c,
                'codex-account-usage',
                'unavailable',
                '계정 일별 토큰 합계 미제공 · 이전 관측은 수신 시각과 함께 보존합니다.',
            )
            return
        store.account_tokens(c, 'codex', buckets, time.time())
        from ..reconciliation import snapshot

        namespace = store.account_namespace(c, 'codex')
        checked = time.time()
        for b in buckets:
            start = stamp(b['startDate'] + 'T00:00:00Z')
            snapshot(
                store,
                c,
                source='codex-account',
                scope_id='codex-day:' + b['startDate'],
                route='codex',
                provider='OpenAI',
                model=None,
                start=start,
                end=start + 86400,
                tokens=b['tokens'],
                checked=checked,
                account_key=namespace,
            )
        skipped = len(usage['dailyUsageBuckets']) - len(buckets)
        store.source(
            c,
            'codex-account-usage',
            'partial' if skipped else 'ok',
            '계정 집계 범위 KST 09:00–다음 날 09:00 · 기간 합계와 날짜 미배분량 대조'
            + (f' · 잘못된 일별 구간 {skipped}개 제외' if skipped else ''),
        )


def poll_codex_thread(store, settings):
    """Optional native capability probe. A thread total has no per-request time;
    keep it as a diagnostic rather than fabricate a dated event."""
    with store.connect() as c:
        last = store.state(c, 'codex-thread:checked', 0)
        if time.time() - last < 3600:
            return
        row = c.execute(
            "SELECT session FROM events WHERE route='codex' AND session IS NOT NULL ORDER BY ts DESC LIMIT 1"
        ).fetchone()
    if not row:
        return
    try:
        result = {'account/usage/read': None}
        read_codex(
            settings['codex_binary'],
            timeout=12,
            extra=result,
            extra_grace=2,
            extra_params={'account/usage/read': dict(threadId=row[0])},
        )
        usage = (result['account/usage/read'] or {}).get('threadUsage')
        from ..reconciliation import number

        groups = []
        for g in (usage or {}).get('groups') or []:
            if not number(g.get('totalTokens')):
                continue
            model = g.get('model')
            if not isinstance(model, str) or not re.fullmatch(r'[A-Za-z0-9._:/() -]{1,100}', model):
                model = None
            groups.append(
                dict(
                    model=model,
                    tokens=g['totalTokens'],
                    **{
                        k: g[k]
                        for k in ('inputTokens', 'outputTokens', 'cachedInputTokens', 'netNewInputTokens')
                        if number(g.get(k))
                    },
                )
            )
        with store.connect() as c:
            store.save_state(c, 'codex-thread:checked', time.time())
            store.save_state(c, 'codex-thread:usage', dict(checked=time.time(), groups=groups))
            store.source(
                c,
                'codex-thread-usage',
                'partial' if groups else 'unavailable',
                '스레드 모델별 누계 수신 · 발생 시각 미제공, 계정 총량에 중복 합산하지 않음'
                if groups
                else '스레드 상세 사용량 미제공 · 계정 합계 수집 유지',
            )
    except Exception as exc:
        log_exception('codex-thread poll', exc)
        with store.connect() as c:
            store.save_state(c, 'codex-thread:checked', time.time())
            store.source(
                c, 'codex-thread-usage', 'error', f'스레드 누계 조회 실패 ({type(exc).__name__}) · 계정 합계 유지'
            )
