"""Official quota fields only; never infer reset from a subscription duration."""

import json
from pathlib import Path
import shutil  # noqa: F401 - tests patch llm_usage.limits.shutil.which
import socket  # noqa: F401 - tests patch llm_usage.limits.socket.create_connection
import subprocess  # noqa: F401 - tests patch llm_usage.limits.subprocess.run
import time
import urllib.error
import urllib.request

from .cloud import collect_claude_cloud
from .providers.antigravity import (
    _agy_endpoints,
    _agy_post,
    _listening_ports,
    _tls_ready,
    agy_app_limits,
    agy_app_servers,
    agy_quota_limits,
    poll_antigravity_app,
    read_agy_app,
    read_agy_quota,
)
from .providers.claude import (
    CLAUDE_SOURCES,
    claude_account,
    claude_cli_version,
    claude_contexts,
    claude_headers,
    claude_limits,
    claude_target,
    poll_claude,
    poll_claude_resources,
)
from .providers.codex import (
    _codex_account_metadata,
    codex_limits,
    codex_usage_buckets,
    poll_codex_thread,
    read_codex,
    record_codex_account,
)
from .providers.devin import devin_ide, devin_limits, poll_devin
from .providers.http import (
    MAX_RETRY_AFTER,
    AccountMetadataMissing,
    NoRedirect,
    account_poll,
    mark_ended,
    retry_delay,
    status_limits,
)
from .providers.opencode import OPENCODE_GO_USAGE_URL, go_limits
from .util import log_exception

__all__ = [
    'AccountMetadataMissing',
    'CLAUDE_SOURCES',
    'MAX_RETRY_AFTER',
    'NoRedirect',
    'OPENCODE_GO_USAGE_URL',
    '_agy_endpoints',
    '_agy_post',
    '_codex_account_metadata',
    '_listening_ports',
    '_tls_ready',
    'account_poll',
    'agy_app_limits',
    'agy_app_servers',
    'agy_quota_limits',
    'claude_account',
    'claude_cli_version',
    'claude_contexts',
    'claude_headers',
    'claude_limits',
    'claude_target',
    'codex_limits',
    'codex_usage_buckets',
    'collect_claude_cloud',
    'devin_ide',
    'devin_limits',
    'go_limits',
    'mark_ended',
    'poll_antigravity_app',
    'poll_claude',
    'poll_claude_resources',
    'poll_codex_thread',
    'poll_devin',
    'poll_limits',
    'poll_slow_sources',
    'read_agy_app',
    'read_agy_quota',
    'read_codex',
    'record_codex_account',
    'retry_delay',
    'status_limits',
]


def _poll_claude_account_fast(store, settings, account):
    poll_claude(store, settings, account)
    poll_claude_resources(store, settings, account)


def _poll_claude_account_cloud(store, settings, account):
    from .claude_accounts import suffix

    route = (account or {}).get('route', 'claude-code')
    tag = suffix(route)
    try:
        collect_claude_cloud(store, settings, account=account)
    except Exception as exc:  # never stops the other reads
        log_exception(f'claude-cloud{tag}', exc)
        with store.connect() as c:
            store.source(c, 'claude-cloud' + tag, 'error', f'클라우드 세션 수집 실패 ({type(exc).__name__})')


def _poll_codex_or_opencode(store, settings, route, manual=False):
    if route == 'opencode-go' and not manual:
        with store.connect() as c:
            pause = store.state(c, 'opencode_go_poll', {})
        # After an entitlement error the subscription is gone for a while —
        # skip the authenticated request for six hours.
        if time.time() < pause.get('next_attempt', 0):
            return False
    account = {}
    data = None
    poll_error = None
    try:
        if route == 'codex':
            if not settings.get('codex_binary'):
                raise FileNotFoundError('codex')
            account = {'account/usage/read': None, 'account/read': None}
            data = read_codex(
                settings['codex_binary'], extra=account, extra_params={'account/read': {'refreshToken': False}}
            )
        else:
            auth = json.loads(Path(settings['opencode_auth']).read_text()).get('opencode-go', {})
            if auth.get('type') != 'api' or not auth.get('key'):
                raise ValueError('구독 인증 미제공')
            req = urllib.request.Request(
                OPENCODE_GO_USAGE_URL,
                headers={
                    'Authorization': 'Bearer ' + auth['key'],
                    'Accept': 'application/json',
                    'User-Agent': 'llm-usage/0.1',
                },
            )
            # Like the Claude/Devin reads, credentials never follow a redirect.
            with urllib.request.build_opener(NoRedirect).open(req, timeout=20) as response:
                data = json.load(response)
        with store.connect() as c:
            n = (
                codex_limits(store, c, data, time.time(), account_profile=account.get('account/read'))
                if route == 'codex'
                else go_limits(store, c, data, time.time())
            )
            store.source(c, route, 'ok' if n else 'unavailable', '' if n else '원본 한도 필드 미제공')
            if route == 'opencode-go':
                store.save_state(c, 'opencode_go_poll', {})
    except Exception as exc:
        log_exception(f'poll_limits {route}', exc)
        # Errors contain no response body, endpoint credentials or command environment.
        poll_error = type(exc).__name__
        detail = f'한도 수집 실패 ({type(exc).__name__})'
        status = 'error'
        if route == 'codex' and isinstance(exc, FileNotFoundError):
            detail = 'Codex 실행 파일을 찾지 못했습니다 · 설정의 codex_binary 경로를 확인하세요.'
        if isinstance(exc, urllib.error.HTTPError):
            detail = f'한도 수집 실패 (HTTP {exc.code})'
            try:
                kind = json.loads(exc.read(4096)).get('error', {}).get('type')
                if kind == 'EntitlementError':
                    status = 'ended'
                    detail = '계정·워크스페이스에 OpenCode Go 구독이 없습니다.'
                elif kind == 'AuthError':
                    detail = '제공사 인증을 다시 확인해야 합니다.'
            except (ValueError, AttributeError):
                pass
            finally:
                exc.close()
        with store.connect() as c:
            store.source(c, route, status, detail)
            if route == 'opencode-go' and status == 'ended':
                store.save_state(c, 'opencode_go_poll', dict(next_attempt=time.time() + 21600))
    finally:
        if route == 'codex':
            # A quota error must neither discard a received usage response nor
            # attach it to the previous login. Read-only account metadata comes
            # from the same app-server and is kept only as a hashed identity.
            try:
                from .resources import observe_account

                ident, revision = _codex_account_metadata(data, account.get('account/read'))
                usable_usage = codex_usage_buckets(account.get('account/usage/read')) is not None
                observed = ident is not None or revision is not None or usable_usage
                # Silence is not a new login. Preserve the last account and
                # receipt time on a complete failure; fresh unbound usage still
                # gets an unknown epoch rather than the previous account.
                if observed:
                    with store.connect() as c:
                        observe_account(
                            c,
                            'codex',
                            ident,
                            time.time(),
                            credential_revision=revision,
                            retain_matching_identity=True,
                        )
                record_codex_account(store, account.get('account/usage/read'))
                if poll_error and not observed:
                    with store.connect() as c:
                        store.source(
                            c,
                            'codex-account-usage',
                            'error',
                            f'계정 토큰 조회 실패 ({poll_error}) · 이전 수신값·시각 유지',
                        )
            except Exception as exc:
                log_exception('codex-account-usage', exc)
                with store.connect() as c:
                    store.source(c, 'codex-account-usage', 'error', f'계정 토큰 합계 기록 실패 ({type(exc).__name__})')
    return route == 'codex' and account.get('account/usage/read') is not None


def poll_slow_sources(store, settings, *, claude_accounts=None, poll_codex_threads=False):
    """Run heavier cloud-session and Codex thread probes concurrently without blocking fast quota refresh."""
    from concurrent.futures import ThreadPoolExecutor

    from .config import ended_routes

    ended = ended_routes(settings)
    if claude_accounts is None:
        claude_accounts = [
            account
            for account in claude_contexts(store, settings)
            if not (account is not None and not account.get('home'))
            and (account or {}).get('route', 'claude-code') not in ended
        ]
    tasks = [lambda a=account: _poll_claude_account_cloud(store, settings, a) for account in claude_accounts]
    if poll_codex_threads and 'codex' not in ended:
        tasks.append(lambda: poll_codex_thread(store, settings))
    if not tasks:
        return
    if len(tasks) == 1:
        tasks[0]()
        return
    with ThreadPoolExecutor(max_workers=min(4, len(tasks))) as pool:
        futures = [pool.submit(fn) for fn in tasks]
        for fut in futures:
            try:
                fut.result()
            except Exception as exc:
                log_exception('poll_slow_sources', exc)


def poll_limits(store, settings, manual=False, include_slow=True):
    from concurrent.futures import ThreadPoolExecutor

    from .claude_accounts import is_claude, suffix
    from .config import ended_routes

    ended = ended_routes(settings)
    # Ended Claude accounts, discoverable now or not: their sources (and status line) end.
    for route in sorted(r for r in ended if is_claude(r)):
        mark_ended(store, route, *(name + suffix(route) for name in CLAUDE_SOURCES))
    active_claude = []
    for account in claude_contexts(store, settings):
        # An account seen only through a home whose login is never read (Windows).
        if account is not None and not account.get('home'):
            continue
        route = (account or {}).get('route', 'claude-code')
        if route in ended:
            continue
        active_claude.append(account)

    tasks = []
    if active_claude:

        def _poll_all_claude():
            for account in active_claude:
                _poll_claude_account_fast(store, settings, account)

        tasks.append(('claude', _poll_all_claude))
    if 'devin' in ended:
        mark_ended(store, 'devin')
    else:
        tasks.append(('devin', lambda: poll_devin(store, settings)))
    if 'antigravity' in ended:
        mark_ended(store, 'antigravity-app')
    else:
        tasks.append(('antigravity', lambda: poll_antigravity_app(store, settings)))
    for route in ('codex', 'opencode-go'):
        if route in ended:
            mark_ended(store, route, *(('codex-account-usage', 'codex-thread-usage') if route == 'codex' else ()))
        else:
            tasks.append((route, lambda r=route: _poll_codex_or_opencode(store, settings, r, manual=manual)))

    codex_thread_ready = False
    if tasks:
        with ThreadPoolExecutor(max_workers=min(6, len(tasks))) as pool:
            submitted = [(name, pool.submit(fn)) for name, fn in tasks]
            for name, fut in submitted:
                try:
                    result = fut.result()
                    if name == 'codex' and result:
                        codex_thread_ready = True
                except Exception as exc:
                    log_exception(f'poll_limits {name}', exc)
    if include_slow:
        poll_slow_sources(store, settings, claude_accounts=active_claude, poll_codex_threads=codex_thread_ready)
    return dict(claude_accounts=active_claude, codex_thread_ready=codex_thread_ready)
