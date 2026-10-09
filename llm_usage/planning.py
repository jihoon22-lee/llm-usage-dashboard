"""Conditional resource decisions, separate from observations and billing actions."""
import math

from .insights import quota_pace, window_seconds

ROUTE_NAMES = {'codex': 'Codex', 'claude-code': 'Claude', 'antigravity': 'Antigravity',
               'opencode-go': 'OpenCode Go', 'devin': 'Devin'}


def window_label(bucket):
    return {'five_hour':'5시간','seven_day':'주간','codex · 300분':'5시간','codex · 10080분':'주간',
            'seven_day_opus':'Opus 주간','seven_day_sonnet':'Sonnet 주간'}.get(bucket,bucket)


def scope_for(row):
    meta = row.get('scope') or {}
    if meta.get('role') in ('common', 'model', 'family', 'unknown'):
        return meta
    route, bucket = row['route'], row['bucket']
    if route == 'codex' and ' · ' in bucket:
        group = bucket.split(' · ')[0]
        return dict(role='common' if group == 'codex' else 'model', group='common' if group == 'codex' else group,
                    label='공통 한도' if group == 'codex' else group)
    if route == 'claude-code':
        if bucket in ('five_hour', 'seven_day'):
            return dict(role='common', group='common', label='공통 한도')
        if bucket in ('seven_day_opus', 'seven_day_sonnet'):
            name = bucket.removeprefix('seven_day_')
            return dict(role='model', group=name, label=name.title())
        return dict(role='unknown', group=bucket, label=row.get('display_name') or bucket)
    if route == 'antigravity' and bucket.startswith(('gemini-', '3p-')):
        family = bucket.split('-')[0]
        # Numbered split buckets represent distinct pools, not duplicate models.
        suffix = bucket.rsplit('-', 1)[-1]
        group = family + ('-' + suffix if suffix.isdigit() else '')
        return dict(role='family', group=group, label=('Gemini' if family == 'gemini' else '3rd party') + (' ' + suffix if suffix.isdigit() else ''))
    return dict(role='common', group='common', label='공통 한도')


def enrich(rows, now):
    for row in rows:
        row['scope'] = scope_for(row)
        readings = row.get('history') or []
        row['paces'] = {}
        for name, lookback, minimum in (('recent', 1800, 15), ('baseline', 10800, 90)):
            pace = quota_pace(readings, now, row['status'], lookback)
            valid = bool(pace and pace[1] >= minimum)
            row['paces'][name] = dict(per_hour=pace[0] if valid else None, observed_minutes=pace[1] if pace else 0,
                                     reason=None if valid else '연속 관측 부족' if row['status'] == 'fresh' else '최신 한도 확인 필요')
        recent, baseline = (row['paces'][k]['per_hour'] for k in ('recent', 'baseline'))
        row['pace_change'] = ('faster' if recent is not None and baseline is not None and recent >= max(2 * baseline, baseline + 2)
                              else 'slower' if recent is not None and baseline is not None and baseline >= max(2 * recent, recent + 2) else None)
        row['capacity'] = None
        row['capacity_reason'] = '계정 전체 한도와 로컬 토큰의 수집 범위가 달라 남은 토큰·요청 수를 환산하지 않습니다.'


def choices(data):
    groups = {}
    for row in data['limits']:
        if row['status'] == 'ended' or row.get('previous_account') or row.get('not_applicable') or row.get('omitted_latest'):
            continue
        scope = scope_for(row)
        groups.setdefault(row['route'], {})[scope['group']] = scope
    result = []
    for route, scopes in groups.items():
        if route == 'claude-code':
            # Null per-model limits mean no such additional window was reported;
            # both families still share five_hour and seven_day.
            scopes.update({k: dict(role='model', group=k, label=k.title()) for k in ('opus', 'sonnet')})
        for group, scope in scopes.items():
            result.append(dict(route=route, model=group, label=scope['label'], service=ROUTE_NAMES.get(route, route)))
    return result


def applicable(data, route, model):
    rows = [r for r in data['limits'] if r['route'] == route and not r.get('previous_account') and not r.get('not_applicable')]
    return [r for r in rows if scope_for(r)['role'] == 'common' or scope_for(r)['group'] == model]


def fallback_resources(data, route, model, blocked, now):
    resources = data.get('resources', {}).get('items', [])
    items = [r for r in resources if r['route'] == route and r.get('active_account') and (not r.get('duplicate_of') or r.get('effective'))
             and r['status'] in ('fresh', 'manual') and r.get('amount') is not None and r['amount'] > 0]
    allowance = next((r for r in resources if r['route'] == route and r.get('allowance') and r.get('active_account')
                      and r['status'] == 'fresh'), None)
    result = []
    for r in items:
        if r.get('allowance') or r['kind'] == 'api_credit' or r.get('scope') == 'api':
            continue
        if r.get('scope') == 'model' and r.get('model', '').lower() != model.lower():
            continue
        entry = dict(id=r['id'], label=r['label'], origin=r['origin'], kind=r['kind'], can_resolve=None,
                     reason='사용 조건 확인 필요', check_url=r.get('check_url'))
        if r.get('enabled') is False:
            entry.update(can_resolve=False, reason='현재 비활성화 또는 제공 대상 아님')
        elif r['kind'] == 'reset':
            grants = r.get('grants')
            if grants is not None:
                valid = [g for g in grants if g.get('amount', 0) > 0 and not g.get('paused')
                         and (not g.get('expires') or g['expires'] > now) and (not g.get('starts') or g['starts'] <= now)]
                covered = any(set(blocked).issubset(set(g.get('targets') or [])) and g.get('usable') is True for g in valid) if blocked else False
                if covered:
                    entry.update(can_resolve=True, reason='제공사 화면에서 사용 후 한도 재확인 필요')
                elif valid:
                    entry['reason'] = '초기화 대상·사용 조건 확인 필요'
                else:
                    entry.update(can_resolve=False, reason='사용 가능한 상세 초기화권 재확인 필요')
            else:
                entry['reason'] = '수동 기록 · 초기화 대상과 현재 사용 가능 여부 확인 필요'
        elif r.get('scope') in ('unknown',None):
            entry['reason'] = '크레딧 적용 범위 확인 필요'
        else:
            conditions = allowance if allowance is not None else r
            cap = conditions.get('spend_remaining')
            if conditions.get('enabled') is False:
                entry.update(can_resolve=False, reason='추가 사용 비활성화')
            elif cap is not None and cap <= 0:
                entry.update(can_resolve=False, reason='추가 사용 지출 한도 도달')
            elif conditions.get('enabled') is True and (conditions.get('spend_unlimited') is True or
                    (cap is not None and cap > 0 and conditions.get('unit') == r.get('unit'))):
                entry.update(can_resolve=True, reason='보유 크레딧 사용 조건 확인됨 · 작업 지속 시간은 별도 확인')
        if r['status'] == 'manual' and entry['can_resolve'] is True:
            entry.update(can_resolve=None, reason='수동 기록 · 제공사에서 현재 조건 확인 필요')
        result.append(entry)
    return result


def decide(data, route, model='common', hours=2, pace='recent', now=None):
    now = now if now is not None else data['now']
    if route not in ROUTE_NAMES or pace not in ('recent', 'baseline') or not isinstance(hours, (float, int)) or isinstance(hours, bool) or not math.isfinite(hours) or not 0.1 <= hours <= 168:
        raise ValueError('서비스·시간·속도 기준을 확인하세요.')
    options = choices(data)
    if not any(o['route'] == route and o['model'] == model for o in options):
        raise ValueError('선택한 모델·한도 묶음을 확인하세요.')
    rows = applicable(data, route, model)
    result = dict(route=route, model=model, hours=hours, pace=pace, state='unknown',
                  reason='판단에 필요한 한도를 확인하는 중입니다.', bottleneck=None, seconds=None,
                  checked=min((r.get('checked') or 0 for r in rows), default=0),
                  applies=[r['bucket'] for r in rows], conditions=[], resources=[])
    if not rows:
        return result
    policy = data.get('quota_policies', {}).get(route) or {}
    stale_policy = not policy.get('checked') or now-policy['checked'] > data.get('stale_seconds', 600)
    fresh = [r for r in rows if r['status'] == 'fresh' and r['remaining'] is not None and r.get('checked') is not None
             and not (r.get('scope') or {}).get('identity_unverified')
             and -120 <= now-r['checked'] <= data.get('stale_seconds', 600) and (not r.get('resets') or r['resets'] > now)]
    exhausted = [r for r in fresh if r['remaining'] <= 0 or (r.get('scope') or {}).get('locked')]
    blocked = [r['bucket'] for r in exhausted]
    if exhausted or (not stale_policy and policy.get('ordinary_allowed') is False):
        result['provider_blocked']=any((r.get('scope') or {}).get('locked') for r in exhausted) or (not stale_policy and policy.get('ordinary_allowed') is False)
        result.update(state='shortage', reason='구독 한도 소진 또는 제공사 사용 제한', seconds=0,
                      bottleneck=exhausted[0]['bucket'] if exhausted else None)
    elif len(fresh) != len(rows):
        result['reason'] = '필요한 한도 일부가 오래되거나 미제공입니다.'
    else:
        # Known providers gate ordinary use on both the session and weekly window.
        required = ('five_hour', 'seven_day') if route == 'claude-code' else ('codex · 300분', 'codex · 10080분') if route == 'codex' else ()
        missing = [b for b in required if not any(r['bucket'] == b for r in rows)]
        if missing:
            result['reason'] = '필수 한도 미확인: ' + ', '.join(map(window_label,missing))
        else:
            deadlines = []; incomplete = []; resets = []
            for r in fresh:
                metric = (r.get('paces') or {}).get(pace) or {}
                speed = metric.get('per_hour')
                if speed is None or speed <= 0 or not r.get('resets'):
                    incomplete.append(r['bucket'])
                    continue
                duration = r['remaining'] / speed * 3600
                if now + duration < r['resets']:
                    deadlines.append((duration, r['bucket']))
                if r['resets'] <= now + hours * 3600:
                    resets.append(r['bucket'])
            earliest = min(deadlines) if deadlines else None
            boundary = min((r['resets']-now for r in fresh if r.get('resets')),default=float('inf'))
            if earliest and earliest[0]<boundary:
                result.update(seconds=earliest[0], bottleneck=earliest[1])
            if earliest and earliest[0] <= hours * 3600 and earliest[0]<boundary:
                result.update(state='shortage', reason='현재 속도가 유지되면 계획 시간 안에 구독 한도가 소진됩니다.')
            elif incomplete:
                result['reason'] = '속도 관측 부족 또는 최근 감소 없음 · 작업 가능 시간을 확정할 수 없습니다.'
            elif resets:
                result.update(state='reset_pending', reason='계획 도중 초기화 예정 · 초기화 이후 잔여량은 재확인이 필요합니다.')
            else:
                result.update(state='room', reason='관측한 속도가 유지되는 조건에서 계획 시간 동안 여유가 있습니다.')
            if incomplete:
                result['conditions'].append('속도 미확인: ' + ', '.join(map(window_label,incomplete)))
            if resets:
                result['conditions'].append('초기화 전후를 나눠 확인: ' + ', '.join(map(window_label,resets)))
    if model == 'common' and any(o['route'] == route and o['model'] != 'common' for o in options):
        result['conditions'].append('공통 한도 기준입니다. 실제 사용할 모델·한도 묶음을 선택하세요.')
        if result['state'] == 'room':
            result.update(state='unknown', reason='모델별 추가 한도가 있습니다. 사용할 모델·한도 묶음을 선택하세요.')
    if any(scope_for(r)['role']=='unknown' for r in rows) and result['state']=='room':
        result.update(state='unknown',reason='선택한 추가 한도의 모델·사용 경로 적용 범위를 확인해야 합니다.',seconds=None)
    result['resources'] = fallback_resources(data, route, model, blocked, now)
    if result.get('provider_blocked'):
        for r in result['resources']:
            if r['can_resolve'] is True:r.update(can_resolve=None,reason='제공사 사용 제한 해제 여부 확인 필요')
    if not stale_policy and policy.get('spending_blocked') is True:
        for r in result['resources']:
            if r['kind'] != 'reset':
                r.update(can_resolve=False, reason='제공사 지출 제한 도달')
    result['conditions'].append('병렬·웹·원격 사용을 포함한 관측 한도 속도이며, 새 작업의 완료를 보장하지 않습니다.')
    return result


def plan(data, route, model='common', hours=2, pace='recent'):
    result = decide(data, route, model, hours, pace)
    options = choices(data)
    alternatives = []
    for option in options:
        if (option['route'], option['model']) == (route, model):
            continue
        candidate = decide(data, option['route'], option['model'], hours, pace)
        if candidate['state'] == 'room':
            alternatives.append(dict(option, seconds=candidate['seconds'], state='room',
                                     valid_until=candidate['checked']+data.get('stale_seconds',600)))
    # Comparable time headroom, never cross-provider percentage ranking.
    alternatives.sort(key=lambda r: (-(r['seconds'] if r['seconds'] is not None else hours*3600), r['route'], r['model']))
    resource_deadlines=[]
    for r in data.get('resources',{}).get('items',[]):
        if r['route']!=route or not r.get('active_account'):continue
        if r.get('checked'):resource_deadlines.append(r['checked']+r.get('ttl',1800))
        if r.get('expires'):resource_deadlines.append(r['expires'])
        resource_deadlines.extend(g['expires'] for g in r.get('grants') or [] if g.get('expires'))
    result.update(alternatives=alternatives[:2], choices=options, now=data['now'],
                  resources_valid_until=min(resource_deadlines,default=data['now']),
                  valid_until=min(((r.get('checked') or 0) + data.get('stale_seconds', 600) for r in applicable(data, route, model)), default=data['now']))
    return result
