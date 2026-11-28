"""Devin CLI quota parsing and account polling."""

import json
from pathlib import Path
import tomllib
import urllib.request

from ..util import is_finite_number as number
from .http import NoRedirect, account_poll


DEVIN_CLI_VERSION = '3000.10.27'


def devin_ide(auth_path=None):
    """Report the installed CLI version when its install tree is visible."""
    version = DEVIN_CLI_VERSION
    if auth_path:

        def key(name):
            try:
                return tuple(int(part) for part in name.split('.'))
            except ValueError:
                return ()

        try:
            found = [key(p.name) for p in (Path(auth_path).parent / 'cli' / '_versions').iterdir()]
        except OSError:
            found = []
        found = [k for k in found if k]
        if found:
            version = '.'.join(str(part) for part in max(found))
    return {'ide_name': 'devin', 'ide_version': version, 'extension_name': 'devin-cli', 'extension_version': version}


def devin_limits(store, c, data, checked):
    """GetUserStatus planStatus — only server-provided remaining-percent fields."""
    plan = (data.get('userStatus') or {}).get('planStatus') or {}
    count = 0
    for bucket, remaining, resets in (
        ('daily', plan.get('dailyQuotaRemainingPercent'), plan.get('dailyQuotaResetAtUnix')),
        ('weekly', plan.get('weeklyQuotaRemainingPercent'), plan.get('weeklyQuotaResetAtUnix')),
    ):
        if not number(remaining):
            continue
        try:
            reset = int(resets) if resets is not None else None
        except (TypeError, ValueError):
            reset = None
        store.limit(c, 'devin', bucket, max(0, min(100, float(remaining))), reset, checked, 'devin')
        count += 1
    if count:
        info = plan.get('planInfo') or {}
        try:
            overage = int(plan.get('overageBalanceMicros') or 0) / 1e6 or None
        except (TypeError, ValueError):
            overage = None
        store.save_state(
            c,
            'devin:plan',
            dict(
                plan_name=info.get('planName'),
                billing=info.get('billingStrategy'),
                plan_start=plan.get('planStart'),
                plan_end=plan.get('planEnd'),
                overage_usd=overage,
                acu_consumed=plan.get('acuConsumed'),
                acu_limit=plan.get('acuLimit'),
                prompt_credits=plan.get('availablePromptCredits'),
                flex_credits=plan.get('availableFlexCredits'),
            ),
        )
    return count, plan


def poll_devin(store, settings):
    """Devin account quota through the CLI's stored credentials. Internal, undocumented API."""
    path = Path(settings.get('devin_auth') or Path.home() / '.local/share/devin/credentials.toml')

    def read():
        creds = tomllib.loads(path.read_text())
        key = creds.get('windsurf_api_key')
        base = (creds.get('api_server_url') or '').rstrip('/')
        if not key or not base:
            raise PermissionError('native authentication required')
        body = json.dumps({'metadata': dict(api_key=key, **devin_ide(str(path)))}).encode()
        request = urllib.request.Request(
            base + '/exa.seat_management_pb.SeatManagementService/GetUserStatus',
            data=body,
            headers={'Content-Type': 'application/json', 'Accept': 'application/json', 'User-Agent': 'llm-usage/0.1'},
        )
        with urllib.request.build_opener(NoRedirect).open(request, timeout=20) as response:
            return json.load(response)

    def record(c, data, checked):
        n, plan = devin_limits(store, c, data, checked)
        name = (plan.get('planInfo') or {}).get('planName') or 'Devin'
        store.save_state(c, 'label:devin:daily', '일간 한도')
        store.save_state(c, 'label:devin:weekly', '주간 한도')
        return n, f'{name} · 계정 사용량 조회 · 내부 경로'

    account_poll(
        store,
        path,
        'devin_status_poll',
        'devin',
        'Devin 인증 확인 필요 · Devin CLI에서 로그인 갱신 후 자동 재확인합니다.',
        read,
        record,
    )


# A provider's Retry-After is honoured up to a day: a bogus header must not stop polling for good.
MAX_RETRY_AFTER = 86400
