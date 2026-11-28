"""Claude Code OAuth quota, credit, and reset-grant polling."""

from functools import lru_cache
import json
from pathlib import Path
import re
import shutil
import subprocess
import time
import urllib.request

from ..util import is_finite_number as number, log_exception
from .http import AccountMetadataMissing, NoRedirect, account_poll

CLAUDE_SOURCES = ('claude-oauth', 'claude-credits', 'claude-resets', 'claude-cloud', 'claude-cloud-supplement')


def claude_limits(store, c, data, checked, route='claude-code'):
    """Internal account usage response; utilization is already a percentage."""
    from ..claude_accounts import account_source
    from ..resources import SKEW, timestamp

    source = account_source(route)
    if not number(checked) or checked > time.time() + SKEW:
        return 0
    count = 0
    observed = []
    optional = (
        'seven_day_opus',
        'seven_day_sonnet',
        'seven_day_oauth_apps',
        'seven_day_cowork',
        'seven_day_omelette',
        'seven_day_overage_included',
    )
    for bucket in ('five_hour', 'seven_day', *optional):
        row = data.get(bucket)
        if not isinstance(row, dict) or not number(row.get('utilization')):
            continue
        used = row['utilization']
        if not 0 <= used <= 100:
            continue
        reset = timestamp(row.get('resets_at'))
        role = (
            'common'
            if bucket in ('five_hour', 'seven_day')
            else 'model'
            if bucket in ('seven_day_opus', 'seven_day_sonnet')
            else 'unknown'
        )
        group = 'common' if role == 'common' else bucket.removeprefix('seven_day_') if role == 'model' else bucket
        label = {
            'seven_day_oauth_apps': '앱 주간',
            'seven_day_cowork': 'Cowork 주간',
            'seven_day_omelette': '추가 주간 한도',
            'seven_day_overage_included': '추가 포함량 주간',
        }.get(bucket, '공통 한도' if role == 'common' else group.title())
        store.limit(
            c,
            route,
            bucket,
            100 - used,
            reset,
            checked,
            source,
            dict(
                role=role,
                group=group,
                label=label,
                locked=bool(row.get('locked_reason')),
                reset_unparsed=row.get('resets_at') is not None and reset is None,
            ),
        )
        if role == 'unknown':
            store.save_state(c, 'label:' + route + ':' + bucket, label)
        count += 1
        observed.append(bucket)
    # Per-model (or per-surface) weekly limits, e.g. a Max plan's Fable allowance,
    # arrive only in the `limits` list; percent is the used share of that limit.
    for row in data.get('limits') if isinstance(data.get('limits'), list) else []:
        if (
            not isinstance(row, dict)
            or row.get('kind') != 'weekly_scoped'
            or not number(row.get('percent'))
            or not 0 <= row['percent'] <= 100
        ):
            continue
        scope = row.get('scope') if isinstance(row.get('scope'), dict) else {}
        model, surface = (scope.get(k) if isinstance(scope.get(k), dict) else {} for k in ('model', 'surface'))
        name = model.get('display_name') or surface.get('display_name')
        if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9 ._-]{0,39}', name):
            continue
        bucket = 'seven_day_' + re.sub(r'[^a-z0-9]+', '_', name.lower()).strip('_')
        if bucket in observed:
            continue  # the top-level field already reported this window
        reset = timestamp(row.get('resets_at'))
        store.limit(
            c,
            route,
            bucket,
            100 - row['percent'],
            reset,
            checked,
            source,
            dict(
                role='model' if model.get('display_name') else 'unknown',
                group=bucket.removeprefix('seven_day_'),
                label=name,
                locked=False,
                reset_unparsed=row.get('resets_at') is not None and reset is None,
            ),
        )
        store.save_state(c, 'label:' + route + ':' + bucket, name + ' 주간')
        count += 1
        observed.append(bucket)
    from ..resources import claude_spend, claude_cloud_credits

    store.save_state(
        c,
        'quota_snapshot:' + route,
        dict(
            checked=checked,
            source=source,
            buckets=observed,
            absent=[b for b in optional if b in data and data[b] is None],
        ),
    )
    return count + claude_spend(c, data, checked, route) + claude_cloud_credits(c, data, checked, route)


@lru_cache(maxsize=4)
def claude_cli_version(binary, revision):
    """Use the installed native version, never run a session or refresh login."""
    path = Path(binary)
    if re.fullmatch(r'\d+\.\d+\.\d+', path.name):
        return path.name
    try:
        result = subprocess.run([binary, '--version'], capture_output=True, text=True, timeout=3, check=True)
        match = re.match(r'(\d+\.\d+\.\d+)\b', result.stdout.strip())
        return match.group(1) if match else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def claude_headers(token):
    # The usage endpoint gates reset visibility by the native client surface.
    # Without this header it returns eligible=false, reason=surface and no grants.
    version = None
    try:
        binary = Path(shutil.which('claude') or Path.home() / '.local/bin/claude').resolve()
        version = claude_cli_version(str(binary), binary.stat().st_mtime_ns)
    except OSError:
        pass
    agent = f'claude-cli/{version} (external, cli)' if version else 'llm-usage/0.2'
    return {
        'Authorization': 'Bearer ' + token,
        'anthropic-beta': 'oauth-2025-04-20',
        'Accept': 'application/json',
        'User-Agent': agent,
        'anthropic-client-platform': 'claude_code_cli',
        'x-app': 'cli',
    }


def claude_target(settings, account=None):
    """(route, credentials, account file) of one Claude account read. Without an
    account context this is the original single account of the default home."""
    if account and account.get('home'):
        home = account['home']
        return account['route'], Path(home['credentials']), home.get('account_file')
    path = Path(settings.get('claude_auth', Path.home() / '.claude/.credentials.json'))
    return (account or {}).get('route', 'claude-code'), path, None


def poll_claude(store, settings, account=None):
    """One account read, with persistent cooldown shared by automatic/manual runs."""
    from ..claude_accounts import suffix

    route, path, account_file = claude_target(settings, account)
    tag = suffix(route)
    metadata = {}

    def read():
        metadata.update(claude_account(path, settings, account_file))
        auth = json.loads(path.read_text()).get('claudeAiOauth', {})
        metadata['plan_revision'] = (
            [auth.get('subscriptionType'), auth.get('rateLimitTier')]
            if auth.get('subscriptionType') or auth.get('rateLimitTier')
            else None
        )
        if not auth.get('accessToken') or (auth.get('expiresAt') and auth['expiresAt'] / 1000 <= time.time()):
            raise PermissionError('native authentication required')
        request = urllib.request.Request(
            'https://api.anthropic.com/api/oauth/usage', headers=claude_headers(auth['accessToken'])
        )
        with urllib.request.build_opener(NoRedirect).open(request, timeout=20) as response:
            return json.load(response)

    def record(c, data, checked):
        from ..resources import observe_account

        observe_account(
            c, route, metadata.get('identity'), checked, path.stat().st_mtime_ns, metadata.get('plan_revision')
        )
        return claude_limits(store, c, data, checked, route), '계정 사용량 조회 · 내부 OAuth 경로'

    account_poll(
        store,
        path,
        'claude_oauth_poll' + tag,
        'claude-oauth' + tag,
        'Claude 인증 확인 필요 · Claude Code에서 로그인 갱신 후 자동 재확인합니다.',
        read,
        record,
    )


def claude_account(path, settings, account_file=None):
    """Read only the native sign-in identity, never a token-derived identity."""
    native = (
        Path(account_file)
        if account_file
        else Path(settings['claude_account_file'])
        if settings.get('claude_account_file')
        else path.parent.parent / '.claude.json'
        if path.parent.name == '.claude'
        else None
    )
    try:
        data = (json.loads(native.read_text()).get('oauthAccount') or {}) if native else {}
        user, org = data.get('accountUuid'), data.get('organizationUuid')
        if not all(isinstance(v, str) and re.fullmatch('[A-Za-z0-9-]{1,100}', v) for v in (user, org)):
            return {}
        return dict(identity=(user, org), organization=org)
    except (OSError, ValueError, TypeError, AttributeError):
        return {}


def poll_claude_resources(store, settings, account=None):
    """Read-only paths in Claude Code 2.1.288, with independent cooldowns.

    Neither endpoint can redeem a reset or change billing. Identities and tokens
    stay in memory; account identity is hashed by the resource store.
    """
    from ..claude_accounts import suffix
    from ..resources import observe_account, claude_balance, claude_resets

    route, path, account_file = claude_target(settings, account)
    tag = suffix(route)
    for kind, source, parser in (
        ('credits', 'claude-credits' + tag, claude_balance),
        ('resets', 'claude-resets' + tag, claude_resets),
    ):
        metadata = {}

        def read():
            auth = json.loads(path.read_text()).get('claudeAiOauth', {})
            if not auth.get('accessToken') or (auth.get('expiresAt') and auth['expiresAt'] / 1000 <= time.time()):
                raise PermissionError('native authentication required')
            metadata.update(claude_account(path, settings, account_file))
            if not metadata.get('identity'):
                raise AccountMetadataMissing('native account identity required')
            endpoint = (
                'https://api.anthropic.com/api/oauth/organizations/' + metadata['organization'] + '/prepaid/credits'
                if kind == 'credits'
                else 'https://api.anthropic.com/api/oauth/usage?cedar_ember=1&skip_spend=1'
            )
            request = urllib.request.Request(endpoint, headers=claude_headers(auth['accessToken']))
            with urllib.request.build_opener(NoRedirect).open(request, timeout=10) as response:
                return json.load(response)

        def record(c, data, checked):
            observe_account(c, route, metadata.get('identity'), checked, path.stat().st_mtime_ns)
            return parser(c, data, checked, route), '추가 자원 읽기 전용 조회 · 내부 OAuth 경로'

        account_poll(
            store, path, source + '_poll', source, 'Claude 기본 계정 정보·인증 확인 필요', read, record, interval=900
        )


def claude_contexts(store, settings):
    """Accounts to poll; discovery errors fall back to the original single account."""
    from ..claude_accounts import accounts

    try:
        return accounts(store, settings)
    except Exception as exc:  # an unreadable home must not stop quota polling
        log_exception('claude_contexts', exc)
        return [None]
