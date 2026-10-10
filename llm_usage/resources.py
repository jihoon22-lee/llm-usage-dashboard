"""Account-scoped, allowlisted resource observations. Never stores credentials.

Amounts retain their provider unit. A spending allowance is not a cash balance,
and a reset grant is not extra quota until the provider reports the reset.
"""
import hashlib
import json
import math
import re
import time
import uuid
from datetime import datetime
from decimal import Decimal, InvalidOperation

ROUTES = ('codex', 'claude-code', 'antigravity', 'opencode-go', 'devin')
KINDS = ('usage_credit', 'api_credit', 'reset')
SCOPES = ('subscription', 'cloud', 'api', 'five_hour', 'weekly', 'model', 'unknown')
SOURCE_LINKS = {
    'codex': 'https://chatgpt.com/codex/settings/usage',
    'claude-code': 'https://claude.ai/settings/usage',
}
AUTO_TTL = 1800
MANUAL_TTL = 7 * 86400
SKEW = 120


def key(*parts):
    return hashlib.sha256(json.dumps(parts, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def number(value):
    return isinstance(value, (float, int)) and not isinstance(value, bool) and math.isfinite(value)


def amount(value):
    """Finite nonnegative decimal; strings are used by Codex credit balances."""
    if isinstance(value, bool) or value is None or not isinstance(value, (int, float, str)):
        return None
    try:
        parsed = Decimal(str(value))
        if not parsed.is_finite() or not 0 <= parsed <= 10**12:
            return None
        return float(parsed)
    except (InvalidOperation, ValueError, OverflowError):
        return None


def timestamp(value):
    if value is None:
        return None
    try:
        if number(value):
            result = value / 1000 if value > 10**11 else value
        elif isinstance(value, str):
            dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
            if dt.tzinfo is None:
                return None
            result = dt.timestamp()
        else:
            return None
        return float(result) if 0 < result < 32503680000 else None
    except (ValueError, OverflowError, OSError):
        return None


def initialize(c):
    c.executescript('''
      CREATE TABLE IF NOT EXISTS resource_accounts (
        route TEXT PRIMARY KEY, account_key TEXT, epoch TEXT NOT NULL,
        since REAL NOT NULL, checked REAL NOT NULL, credential_revision TEXT);
      CREATE TABLE IF NOT EXISTS quota_context (
        route TEXT, bucket TEXT, account_key TEXT, epoch TEXT, checked REAL,
        data TEXT NOT NULL, PRIMARY KEY(route,bucket));
      CREATE TABLE IF NOT EXISTS resource_items (
        id TEXT PRIMARY KEY, route TEXT NOT NULL, account_key TEXT,
        pool_key TEXT NOT NULL, kind TEXT NOT NULL, origin TEXT NOT NULL,
        checked REAL NOT NULL, revision INTEGER NOT NULL, data TEXT NOT NULL);
      CREATE INDEX IF NOT EXISTS resources_route ON resource_items(route,origin);
      CREATE TABLE IF NOT EXISTS resource_history (
        id TEXT, checked REAL, amount REAL, spent REAL, period_reset REAL,
        PRIMARY KEY(id,checked));
    ''')


def account(c, route):
    row = c.execute('SELECT * FROM resource_accounts WHERE route=?', (route,)).fetchone()
    return dict(row) if row else None


def observe_account(c, route, identity, checked, credential_revision=None, plan_revision=None):
    """Unknown identity remains unknown; credential changes only break its epoch."""
    if not number(checked) or checked > time.time() + SKEW:
        return account(c, route)
    current = account(c, route)
    if current and checked < current['checked']:
        return current
    ident = key(route, identity) if identity else None
    changed = not current or ident != current['account_key']
    if current and ident is None and credential_revision is not None:
        changed |= str(credential_revision) != current['credential_revision']
    plan_key='quota_plan_revision:'+route
    before=c.execute('SELECT data FROM state WHERE key=?',(plan_key,)).fetchone()
    revision=key(plan_revision) if plan_revision is not None else None
    if revision is not None:
        changed |= bool(before and json.loads(before['data'])!=revision)
        c.execute('INSERT OR REPLACE INTO state VALUES (?,?)',(plan_key,json.dumps(revision)))
    epoch = uuid.uuid4().hex if changed else current['epoch']
    since = checked if changed else current['since']
    c.execute('''INSERT INTO resource_accounts VALUES (?,?,?,?,?,?)
      ON CONFLICT(route) DO UPDATE SET account_key=excluded.account_key,epoch=excluded.epoch,
      since=excluded.since,checked=excluded.checked,credential_revision=excluded.credential_revision''',
      (route, ident, epoch, since, checked, str(credential_revision) if credential_revision is not None else None))
    return account(c, route)


def quota_context(c, route, bucket, checked, metadata=None):
    owner = account(c, route)
    data = metadata or {}
    c.execute('''INSERT INTO quota_context VALUES (?,?,?,?,?,?) ON CONFLICT(route,bucket)
      DO UPDATE SET account_key=excluded.account_key,epoch=excluded.epoch,checked=excluded.checked,data=excluded.data
      WHERE excluded.checked>=quota_context.checked''',
      (route, bucket, owner['account_key'] if owner else None, owner['epoch'] if owner else None,
       checked, json.dumps(data, ensure_ascii=False)))


def save_auto(c, route, pool, kind, checked, data, source):
    if not number(checked) or checked > time.time() + SKEW:
        return None
    owner = account(c, route)
    account_key = owner['account_key'] if owner else None
    # Unknown-account epochs must not identify the next account's resources.
    namespace = account_key or (owner['epoch'] if owner else 'unverified')
    rid = key(route, namespace, pool, kind)
    payload = dict(data, source=source, epoch=owner['epoch'] if owner else None)
    c.execute('''INSERT INTO resource_items VALUES (?,?,?,?,?,?,?,1,?) ON CONFLICT(id)
      DO UPDATE SET checked=excluded.checked,revision=resource_items.revision+1,data=excluded.data
      WHERE excluded.checked>=resource_items.checked''',
      (rid, route, account_key, pool, kind, 'auto', checked, json.dumps(payload, ensure_ascii=False)))
    c.execute('INSERT OR IGNORE INTO resource_history VALUES (?,?,?,?,?)',
              (rid, checked, amount(data.get('amount')), amount(data.get('spent')), timestamp(data.get('period_reset'))))
    return rid


def resource_error(c, route, pool, kind, checked, source, reason='unavailable'):
    """Keep the previous value, but never leave a successful-looking observation."""
    owner = account(c, route)
    namespace = owner['account_key'] or owner['epoch'] if owner else 'unverified'
    rid = key(route, namespace, pool, kind)
    row = c.execute('SELECT data FROM resource_items WHERE id=?', (rid,)).fetchone()
    data = json.loads(row['data']) if row else dict(amount=None, unit='count' if kind == 'reset' else 'credit',
                                                   scope='unknown', label='초기화권' if kind == 'reset' else '사용 크레딧')
    data['observation'] = reason
    data['value_checked'] = data.get('value_checked') or (c.execute('SELECT checked FROM resource_items WHERE id=?', (rid,)).fetchone()[0] if row else None)
    return save_auto(c, route, pool, kind, checked, data, source)


def codex_resources(c, data, checked):
    groups = data.get('rateLimitsByLimitId') or {'codex': data.get('rateLimits') or {}}
    # Credit data is account/workspace-wide, even when repeated in model buckets.
    credit_values = [row['credits'] for row in groups.values() if isinstance(row, dict) and isinstance(row.get('credits'), dict)]
    common = (groups.get('codex') or {}).get('credits')
    credits = common if isinstance(common, dict) else credit_values[0] if credit_values else None
    if credits is not None:
        balance = amount(credits.get('balance'))
        conflict = any(v != credits for v in credit_values)
        save_auto(c, 'codex', 'workspace-credits', 'usage_credit', checked,
                  dict(label='Codex 사용 크레딧', amount=balance, unit='credit', scope='subscription',
                       enabled=credits.get('hasCredits') if isinstance(credits.get('hasCredits'), bool) else None,
                       unlimited=credits.get('unlimited') is True, observation='conflict' if conflict else 'observed',
                       spend_remaining=None, spend_unlimited=None, auto_reload=None,
                       expires=None, expiry_known=False), 'codex')
    else:
        resource_error(c, 'codex', 'workspace-credits', 'usage_credit', checked, 'codex')
    reset = data.get('rateLimitResetCredits')
    if isinstance(reset, dict) and isinstance(reset.get('availableCount'), int) and not isinstance(reset['availableCount'], bool) and reset['availableCount'] >= 0:
        grants = []
        for row in reset.get('credits') or []:
            if not isinstance(row, dict):
                continue
            grants.append(dict(id=key('codex-grant', str(row.get('id', ''))), amount=1,
                               expires=timestamp(row.get('expiresAt')), starts=timestamp(row.get('grantedAt')),
                               scope='unknown', targets=[], usable=None,
                               status=row.get('status') if row.get('status') in ('available', 'used', 'expired') else 'unknown'))
        save_auto(c, 'codex', 'reset-grants', 'reset', checked,
                  dict(label='Codex 초기화권', amount=reset['availableCount'], unit='count', scope='unknown',
                       enabled=None, observation='observed', grants=grants, details_known=isinstance(reset.get('credits'), list),
                       expires=None, expiry_known=False), 'codex')
    else:
        resource_error(c, 'codex', 'reset-grants', 'reset', checked, 'codex')
    return int(credits is not None)+int(isinstance(reset,dict) and type(reset.get('availableCount')) is int and reset['availableCount']>=0)


def money(value):
    """The structured Claude spend response carries its own currency exponent."""
    if not isinstance(value, dict):
        return None, None
    currency = value.get('currency')
    exponent = value.get('exponent')
    raw = amount(value.get('amount_minor'))
    if not isinstance(currency, str) or not re.fullmatch('[A-Z]{3}', currency) or type(exponent) is not int or not 0 <= exponent <= 4 or raw is None:
        return None, None
    return raw / 10**exponent, currency


def claude_spend(c, data, checked):
    extra = data.get('extra_usage')
    spend = data.get('spend')
    if not isinstance(extra, dict) and not isinstance(spend, dict):
        resource_error(c, 'claude-code', 'spending-allowance', 'usage_credit', checked, 'claude-oauth')
        return 0
    extra = extra or {}; spend = spend or {}
    used, currency = money(spend.get('used'))
    limit, limit_currency = money(spend.get('limit'))
    enabled = spend.get('enabled', extra.get('is_enabled'))
    # Legacy extra_usage uses minor units. Convert only currencies whose exponent
    # is known here; unknown currency stays in original minor units.
    if used is None:
        currency = extra.get('currency') if re.fullmatch('[A-Z]{3}', str(extra.get('currency', ''))) else None
        used = amount(extra.get('used_credits'))
        limit = amount(extra.get('monthly_limit'))
        divisor = 100 if currency in ('USD', 'EUR', 'GBP', 'CAD', 'AUD') else 1 if currency in ('JPY', 'KRW') else None
        if divisor:
            used = used / divisor if used is not None else None
            limit = limit / divisor if limit is not None else None
        else:
            currency = 'minor'
    elif limit is not None and limit_currency != currency:
        limit = None
    remaining = max(0, limit - used) if limit is not None and used is not None else None
    balance, balance_currency = money(spend.get('balance'))
    save_auto(c, 'claude-code', 'spending-allowance', 'usage_credit', checked,
              dict(label='Claude 추가 사용 지출 한도', amount=remaining, unit=currency or 'minor', scope='subscription',
                   allowance=True, limit=limit, spent=used, spend_remaining=remaining,
                   spend_unlimited=False if limit is not None else None,
                   enabled=enabled if isinstance(enabled, bool) else None,
                   observation='observed', expires=None, expiry_known=False,
                   # A null limit is not interpreted as unlimited without an explicit flag.
                   period_reset=timestamp(spend.get('resets_at'))), 'claude-oauth')
    if balance is not None:
        save_auto(c, 'claude-code', 'prepaid-credits', 'usage_credit', checked,
                  dict(label='Claude 사용 크레딧', amount=balance, unit=balance_currency, scope='subscription',
                       enabled=None, observation='observed', expires=None, expiry_known=False), 'claude-oauth')
    return 1


def claude_cloud_credits(c, data, checked):
    """Cloud-session promotion from the account usage response, in explicit USD.

    This pool does not fund local Code, chat, Cowork, or API requests. A missing
    promotion is not a zero balance, and its expiry is not a quota reset.
    """
    pool = 'cloud-session-credits'
    row = data.get('iguana_necktie')
    if not isinstance(row, dict):
        if c.execute('SELECT 1 FROM resource_items WHERE route=? AND pool_key=? AND account_key IS ?',
                     ('claude-code', pool, (account(c, 'claude-code') or {}).get('account_key'))).fetchone():
            resource_error(c, 'claude-code', pool, 'usage_credit', checked, 'claude-oauth')
        return 0
    remaining, limit, used = (amount(row.get(k)) for k in ('remaining_dollars', 'limit_dollars', 'used_dollars'))
    conflict = remaining is not None and limit is not None and (remaining > limit or
               (used is not None and not math.isclose(remaining, max(0, limit-used), abs_tol=0.000001)))
    save_auto(c, 'claude-code', pool, 'usage_credit', checked,
              dict(label='Claude 클라우드 전용 크레딧', amount=remaining, unit='USD', scope='cloud',
                   limit=limit, spent=used, enabled=not bool(row['locked_reason']) if 'locked_reason' in row else None,
                   expires=timestamp(row.get('resets_at')), expiry_known=timestamp(row.get('resets_at')) is not None,
                   observation='conflict' if conflict else 'observed' if remaining is not None else 'unavailable'), 'claude-oauth')
    return int(remaining is not None and not conflict)


def claude_balance(c, data, checked):
    raw = amount(data.get('amount'))
    currency = data.get('currency')
    divisor = 100 if currency in ('USD', 'EUR', 'GBP', 'CAD', 'AUD') else 1 if currency in ('JPY', 'KRW') else None
    if raw is None:
        resource_error(c, 'claude-code', 'prepaid-credits', 'usage_credit', checked, 'claude-credits')
        return 0
    auto = data.get('auto_reload_settings')
    unscoped = amount(data.get('amount_without_scoped_credits'))
    restricted = bool(data.get('promo_tranches') or data.get('tranches')) and unscoped is None
    available = unscoped if unscoped is not None and unscoped <= raw else raw
    save_auto(c, 'claude-code', 'prepaid-credits', 'usage_credit', checked,
              dict(label='Claude 사용 크레딧', amount=available / divisor if divisor else available,
                   reported_total=raw / divisor if divisor else raw,
                   unit=currency if divisor else 'minor', scope='unknown' if restricted else 'subscription', enabled=None,
                   auto_reload=auto.get('enabled') if isinstance(auto, dict) and isinstance(auto.get('enabled'), bool) else None,
                   expires=timestamp(data.get('next_expires_at')), expiry_known=bool(data.get('next_expires_at')),
                   expiry_is_partial=True, observation='observed'), 'claude-credits')
    return 1


def claude_resets(c, data, checked):
    reset = data.get('cedar_ember')
    if not isinstance(reset, dict) or not isinstance(reset.get('eligible'), bool):
        resource_error(c, 'claude-code', 'reset-grants', 'reset', checked, 'claude-resets')
        return 0
    if reset.get('ineligible_reason') == 'surface':
        # An unsupported view cannot establish whether the account owns grants.
        resource_error(c, 'claude-code', 'reset-grants', 'reset', checked, 'claude-resets')
        return 0
    grants = []; complete = isinstance(reset.get('grants'), list)
    for row in reset.get('grants') or []:
        if not isinstance(row, dict) or type(row.get('resets_left')) is not int or row['resets_left'] < 0:
            complete = False
            continue
        targets = [v for v in row.get('clears') or [] if isinstance(v, str) and re.fullmatch('[A-Za-z0-9_-]{1,64}', v)]
        grants.append(dict(id=key('claude-grant', str(row.get('id', ''))), amount=row['resets_left'],
                           expires=timestamp(row.get('ends_at')), starts=timestamp(row.get('starts_at')),
                           targets=targets, scope='buckets', usable=row.get('usable_now') if isinstance(row.get('usable_now'), bool) else None,
                           requires_limit=row.get('use_requires_limit') if isinstance(row.get('use_requires_limit'), bool) else None,
                           paused=row.get('paused') is True))
    save_auto(c, 'claude-code', 'reset-grants', 'reset', checked,
              dict(label='Claude 초기화권', amount=sum(g['amount'] for g in grants) if complete else None,
                   unit='count', scope='unknown', enabled=reset['eligible'], observation='observed' if complete else 'unavailable',
                   grants=grants, details_known=complete, expires=None, expiry_known=False,
                   cooldown_until=timestamp(reset.get('cooldown_until'))), 'claude-resets')
    return 1


class Conflict(ValueError):
    pass


def manual_data(body, now):
    if not isinstance(body, dict):
        raise ValueError('자원 내용을 확인하세요.')
    allowed = {'route', 'kind', 'label', 'amount', 'unit', 'scope', 'model', 'expires', 'checked',
               'enabled', 'spend_remaining', 'spend_unlimited', 'auto_reload', 'linked_id', 'revision', 'request_id'}
    if body.keys() - allowed:
        raise ValueError('지원하지 않는 자원 필드입니다.')
    route, kind, scope = body.get('route'), body.get('kind'), body.get('scope')
    if route not in ROUTES or kind not in KINDS or scope not in SCOPES:
        raise ValueError('서비스·자원 종류·적용 범위를 확인하세요.')
    label = body.get('label', '')
    if not isinstance(label, str) or not 1 <= len(label.strip()) <= 80 or any(ord(ch) < 32 for ch in label):
        raise ValueError('자원 이름은 1~80자로 입력하세요.')
    value = amount(body.get('amount'))
    if value is None:
        raise ValueError('잔액·개수는 0 이상의 유한한 숫자여야 합니다.')
    unit = body.get('unit')
    if unit not in ('USD', 'EUR', 'GBP', 'CAD', 'AUD', 'JPY', 'KRW', 'credit', 'count', 'minor'):
        raise ValueError('지원하는 단위를 선택하세요.')
    if kind == 'reset' and (unit != 'count' or value != int(value) or value > 100000):
        raise ValueError('초기화권은 정수 횟수로 입력하세요.')
    if kind != 'reset' and unit == 'count':
        raise ValueError('크레딧의 단위를 확인하세요.')
    if kind == 'api_credit' and scope != 'api':
        raise ValueError('API 크레딧은 API 경로로 기록하세요.')
    if kind == 'reset' and scope == 'api':
        raise ValueError('초기화권 적용 범위를 확인하세요.')
    checked = timestamp(body.get('checked'))
    if checked is None or checked > now + SKEW:
        raise ValueError('마지막 확인 시각은 현재 이전으로 입력하세요.')
    expires = timestamp(body.get('expires'))
    if body.get('expires') is not None and expires is None:
        raise ValueError('만료 시각에 시간대를 포함하세요.')
    model = body.get('model') or ''
    if not isinstance(model, str) or len(model) > 100 or (model and not re.fullmatch('[A-Za-z0-9._/() -]+', model)):
        raise ValueError('모델 이름을 확인하세요.')
    if scope == 'model' and not model:
        raise ValueError('적용 모델을 입력하세요.')
    booleans = {}
    for field in ('enabled', 'spend_unlimited', 'auto_reload'):
        value_bool = body.get(field)
        if value_bool is not None and not isinstance(value_bool, bool):
            raise ValueError('사용 조건은 확인됨·아님·미확인 중 선택하세요.')
        booleans[field] = value_bool
    cap = amount(body.get('spend_remaining'))
    if body.get('spend_remaining') is not None and cap is None:
        raise ValueError('남은 지출 한도를 확인하세요.')
    if cap is not None and booleans['spend_unlimited'] is True:
        raise ValueError('지출 한도와 제한 없음은 동시에 설정할 수 없습니다.')
    linked = body.get('linked_id')
    if linked is not None and (not isinstance(linked, str) or not re.fullmatch('[0-9a-f]{64}', linked)):
        raise ValueError('연결할 자동 자원을 확인하세요.')
    return dict(route=route, kind=kind, checked=checked, data=dict(label=label.strip(), amount=value, unit=unit,
                scope=scope, model=model, expires=expires, expiry_known=expires is not None,
                spend_remaining=cap, linked_id=linked, observation='observed', **booleans))


def write_manual(store, body, rid=None, delete=False):
    now = time.time()
    with store.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        old = c.execute("SELECT * FROM resource_items WHERE id=? AND origin='manual'", (rid,)).fetchone() if rid else None
        if rid and not old:
            raise KeyError(rid)
        if old and (not isinstance(body, dict) or type(body.get('revision')) is not int or body['revision'] != old['revision']):
            raise Conflict('다른 화면에서 변경되었습니다. 최신 목록을 불러온 뒤 입력 내용을 확인하세요.')
        if delete:
            c.execute('DELETE FROM resource_items WHERE id=?', (rid,))
            return dict(deleted=True, id=rid)
        clean = manual_data(body, now)
        request_id = body.get('request_id')
        if request_id is not None and (not isinstance(request_id, str) or not re.fullmatch('[0-9a-f]{32}', request_id)):
            raise ValueError('기록 요청 식별자를 확인하세요.')
        owner = account(c, clean['route'])
        independent = clean['kind'] == 'api_credit'
        binding = None if independent or owner is None else (owner['account_key'],owner['epoch'])
        fingerprint = key(clean,binding)
        if not rid and request_id:
            previous = c.execute("SELECT * FROM resource_items WHERE id=? AND origin='manual'", (request_id,)).fetchone()
            if previous:
                if previous['revision']!=1 or json.loads(previous['data']).get('creation_fingerprint') != fingerprint:
                    raise Conflict('이 저장 요청의 기록이 이미 있습니다. 최신 기록을 확인한 뒤 수정하세요.')
                return dict(id=previous['id'], revision=previous['revision'], replayed=True)
        account_key = owner['account_key'] if owner and not independent else None
        clean['data']['epoch'] = owner['epoch'] if owner and not independent else None
        clean['data']['account_scope'] = 'independent' if independent else 'current'
        clean['data']['creation_fingerprint'] = json.loads(old['data']).get('creation_fingerprint') if old else fingerprint
        linked = clean['data']['linked_id']
        if linked:
            target = c.execute("SELECT * FROM resource_items WHERE id=? AND origin='auto'", (linked,)).fetchone()
            if not target or target['route'] != clean['route'] or target['account_key'] != account_key or target['kind'] != clean['kind']:
                raise ValueError('현재 계정의 같은 종류 자원에만 연결할 수 있습니다.')
            target_data = json.loads(target['data'])
            if target_data.get('epoch') != clean['data']['epoch'] or target_data.get('unit') != clean['data']['unit'] or target_data.get('scope') != clean['data']['scope']:
                raise ValueError('연결할 자원의 계정·단위·적용 범위가 다릅니다.')
        if not old and c.execute("SELECT COUNT(*) FROM resource_items WHERE origin='manual'").fetchone()[0] >= 100:
            raise ValueError('수동 자원은 최대 100개까지 기록할 수 있습니다.')
        rid = rid or request_id or uuid.uuid4().hex
        revision = old['revision'] + 1 if old else 1
        c.execute('''INSERT INTO resource_items VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(id)
          DO UPDATE SET route=excluded.route,account_key=excluded.account_key,pool_key=excluded.pool_key,
          kind=excluded.kind,checked=excluded.checked,revision=excluded.revision,data=excluded.data''',
          (rid, clean['route'], account_key, 'manual:' + rid, clean['kind'], 'manual', clean['checked'], revision,
           json.dumps(clean['data'], ensure_ascii=False)))
        return dict(id=rid, revision=revision)


def inventory(c, now, sources):
    owners = {r['route']: dict(r) for r in c.execute('SELECT * FROM resource_accounts')}
    result = []
    for stored in c.execute('SELECT * FROM resource_items ORDER BY route,origin,id'):
        row = dict(stored); data = json.loads(row.pop('data')); row.update(data)
        owner = owners.get(row['route'])
        independent = row['origin'] == 'manual' and row.get('account_scope') == 'independent'
        active = independent or ((row['account_key'] == owner['account_key'] and row.get('epoch') == owner['epoch']) if owner else row['account_key'] is None)
        row.pop('creation_fingerprint',None)
        row['active_account'] = active
        row['identity_verified'] = row['account_key'] is not None
        row['ttl'] = MANUAL_TTL if row['origin'] == 'manual' else 600 if row.get('source') in ('codex','claude-oauth') else AUTO_TTL
        row['status'] = 'manual' if row['origin'] == 'manual' else 'fresh'
        observed = row.get('value_checked') or row['checked']
        source = sources.get(row.get('source'), {})
        if not active:
            row['status'] = 'previous_account'
        elif observed > now + SKEW or now - observed > row['ttl']:
            row['status'] = 'stale'
        elif row.get('observation') in ('unavailable', 'conflict'):
            row['status'] = row['observation']
        elif source.get('status') in ('error', 'unavailable', 'ended') and source.get('checked', 0) >= row['checked']:
            row['status'] = 'error' if source['status'] == 'error' else 'unavailable'
        elif row.get('expires') and row['expires'] <= now and not row.get('expiry_is_partial'):
            row['status'] = 'expired'
        elif row.get('expires') and row['expires'] <= now:
            row['status'] = 'stale'  # next expiry may cover only part of a balance
        row['check_url'] = SOURCE_LINKS.get(row['route'])
        row['rate_per_hour'] = None
        # Only an explicit cumulative spending counter in the same provider period
        # can establish credit burn. Balance deltas cannot distinguish purchases.
        if row['status'] == 'fresh' and row.get('period_reset') and row['period_reset'] > now:
            history = [dict(p) for p in c.execute('SELECT * FROM resource_history WHERE id=? AND checked>=? AND checked<=? ORDER BY checked',
                                                  (row['id'], max(now-3600,(owner or {}).get('since',0)), now))]
            if len(history) >= 3 and history[-1]['checked'] - history[0]['checked'] >= 900:
                valid = all(p['spent'] is not None and p['period_reset'] == row['period_reset'] for p in history)
                valid &= all(0 < b['checked']-a['checked'] <= 600 and b['spent'] >= a['spent'] for a, b in zip(history, history[1:]))
                if valid:
                    row['rate_per_hour'] = (history[-1]['spent']-history[0]['spent']) * 3600 / (history[-1]['checked']-history[0]['checked'])
        result.append(row)
    auto = {r['id']: r for r in result if r['origin'] == 'auto'}
    for row in result:
        target = auto.get(row.get('linked_id'))
        row['duplicate_of'] = target['id'] if target and target['active_account'] and row['active_account'] else None
        row['effective'] = not row['duplicate_of'] or target['status'] != 'fresh'
        row['conflicts_with_auto'] = bool(target and row['duplicate_of'] and (row.get('amount'), row.get('unit')) != (target.get('amount'), target.get('unit')))
        if row['kind']=='usage_credit' and not row.get('allowance') and row.get('scope')=='subscription':
            allowance=next((r for r in result if r.get('allowance') and r['route']==row['route'] and r['account_key']==row['account_key'] and r['active_account'] and r['status']=='fresh' and r['unit']==row['unit']),None)
            if allowance:
                row['usage_conditions']={k:allowance.get(k) for k in ('enabled','spend_remaining','spend_unlimited','checked','ttl')}
    return dict(items=result, accounts=[dict(route=r, verified=bool(a['account_key']), epoch=a['epoch']) for r, a in owners.items()])
