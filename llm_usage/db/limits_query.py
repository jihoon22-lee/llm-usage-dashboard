"""Quota limit storage, source status tracking, and limit snapshot queries."""

from datetime import datetime
import json
import re
import time

from ..claude_accounts import account_source, family, is_claude
from .schema import (
    HISTORY_FIELDS,
    KST,
    METRICS,
    QUOTA_PARENTS,
    SUPPORTED_ROUTES,
    USER_ENDED_DETAIL,
    route_events,
    stamp,
)


class LimitsMixin:
    def state(self, c, key, default=None):
        row = c.execute('SELECT data FROM state WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def save_state(self, c, key, data):
        c.execute('INSERT OR REPLACE INTO state VALUES (?,?)', (key, json.dumps(data)))

    def source(self, c, name, status, detail='', checked=None):
        c.execute(
            'INSERT INTO sources VALUES (?,?,?,?) ON CONFLICT(name) DO UPDATE SET '
            'checked=excluded.checked,status=excluded.status,detail=excluded.detail '
            'WHERE excluded.checked>=sources.checked',
            (name, checked or time.time(), status, detail),
        )
        if status == 'ok':
            c.execute(
                '''INSERT INTO source_health VALUES (?,?) ON CONFLICT(name)
              DO UPDATE SET last_success=MAX(last_success,excluded.last_success)''',
                (name, checked or time.time()),
            )
        quota_source = name in (
            'codex',
            'claude-code',
            'claude-oauth',
            'antigravity',
            'antigravity-app',
            'opencode-go',
            'devin',
        ) or name.startswith(('claude-oauth:', 'claude-code:'))
        if quota_source and status in ('error', 'unavailable'):
            c.execute(
                'INSERT OR REPLACE INTO quota_interruptions VALUES (?,?,?)', (name, checked or time.time(), status)
            )

    def source_changed(self, c, name, status, detail='', checked=None):
        """source() for frequent callers: skip the write when nothing would change.

        Without an explicit time only a status or text change is written; the stored
        time then stays the moment the current state began."""
        row = c.execute('SELECT status,detail,checked FROM sources WHERE name=?', (name,)).fetchone()
        if (
            row
            and row['status'] == status
            and row['detail'] == detail
            and (checked is None or row['checked'] == checked)
        ):
            return
        self.source(c, name, status, detail, checked)

    def prune_history(self, c):
        now = time.time()
        if now - self.state(c, 'history_pruned', 0) >= 86400:
            retention = now - self.thresholds['retention_days'] * 86400
            c.execute('DELETE FROM limit_history WHERE checked<?', (retention,))
            c.execute('DELETE FROM quota_interruptions WHERE checked<?', (retention,))
            c.execute('DELETE FROM resource_history WHERE checked<?', (retention,))
            self.save_state(c, 'history_pruned', now)
            # What is older than any prune's cutoff is gone, even if retention_days grows later.
            self.save_state(c, 'history_cutoff', max(retention, self.state(c, 'history_cutoff', 0)))

    def limit(self, c, route, bucket, remaining, resets, checked, source, metadata=None):
        from ..resources import account, number, quota_context, SKEW

        received = time.time()
        if not number(checked) or checked > received + SKEW:
            return False
        resets = stamp(resets)
        owner = account(c, route)
        previous = c.execute('SELECT * FROM limits WHERE route=? AND bucket=?', (route, bucket)).fetchone()
        # Unidentified terminal replays do not replace a current account response.
        canonical = account_source(route) if is_claude(route) else 'codex' if route == 'codex' else None
        prefer_account = bool(
            owner
            and owner['account_key']
            and canonical
            and source != canonical
            and previous
            and previous['source'] == canonical
            and 0 <= checked - previous['checked'] <= self.thresholds['stale_seconds']
        )
        changed = False
        if not prefer_account:
            changed = (
                c.execute(
                    '''INSERT INTO limits VALUES (?,?,?,?,?,?) ON CONFLICT(route,bucket)
        DO UPDATE SET remaining=excluded.remaining,resets=excluded.resets,checked=excluded.checked,
        source=excluded.source WHERE excluded.checked >= limits.checked OR limits.checked>?''',
                    (route, bucket, remaining, resets, checked, source, received + SKEW),
                ).rowcount
                > 0
            )
            if changed:
                meta = dict(metadata or {})
                if (owner and not owner['account_key']) or (canonical and source != canonical):
                    meta['identity_unverified'] = True
                quota_context(c, route, bucket, checked, meta)
        # Local replay must not manufacture a historical stream at migration time.
        # Keep only observations made within the retention window.
        cutoff = time.time() - self.thresholds['retention_days'] * 86400
        if checked >= cutoff:
            c.execute(
                'INSERT OR IGNORE INTO limit_history VALUES (?,?,?,?,?,?)',
                (route, bucket, checked, remaining, resets, source),
            )
        self.prune_history(c)
        return changed

    def limits(self, now=None, ended=None, names=None):
        """Quota rows; `ended` holds the subscriptions the user marked as ended and
        `names` the user's names for Claude accounts."""
        from ..insights import (
            attach_scopes,
            quota_decreases,
            quota_history,
            quota_trends,
            token_total,
            window_seconds,
        )

        now = now or time.time()
        ended = set(ended or ())
        with self.connect() as c:
            c.execute('BEGIN')
            rows = [dict(r) for r in c.execute('SELECT * FROM limits ORDER BY route,bucket')]
            sources = {r['name']: dict(r) for r in c.execute('SELECT * FROM sources')}
            for row in rows:
                row['display_name'] = self.state(c, 'label:' + row['route'] + ':' + row['bucket'], row['bucket'])
            from ..resources import inventory, SKEW

            accounts = {r['route']: dict(r) for r in c.execute('SELECT * FROM resource_accounts')}
            contexts = {(r['route'], r['bucket']): dict(r) for r in c.execute('SELECT * FROM quota_context')}
            from ..claude_accounts import describe

            claude = describe(c, names, ended)
            # Other Claude accounts are quota routes of their own beside the original one.
            routes = list(
                dict.fromkeys(
                    [
                        *SUPPORTED_ROUTES,
                        *(a['route'] for a in claude),
                        *(r['route'] for r in rows if is_claude(r['route'])),
                    ]
                )
            )
            policies = {route: self.state(c, 'quota_policy:' + route, {}) for route in routes}
            snapshots = {route: self.state(c, 'quota_snapshot:' + route, {}) for route in routes}
            resources = inventory(c, now, sources)
            history = quota_history(
                c,
                now,
                selected_sources={(r['route'], r['bucket']): r['source'] for r in rows},
                since_by_route={route: a['since'] for route, a in accounts.items()},
            )
            # A sole non-default home still needs its account label beside the original route.
            labels = (
                {a['route']: a['label'] for a in claude} if any(a['route'] != 'claude-code' for a in claude) else {}
            )
            for route in routes:
                if not any(r['route'] == route for r in rows):
                    rows.append(
                        dict(route=route, bucket='미제공', remaining=None, resets=None, checked=None, source=route)
                    )
            for row in rows:
                checked, resets = row['checked'], row['resets']
                owner = accounts.get(row['route'])
                context = contexts.get((row['route'], row['bucket']), {})
                metadata = json.loads(context.get('data', '{}'))
                # A placeholder for a route with no reading was observed under no account.
                previous_account = bool(
                    owner
                    and checked is not None
                    and (context.get('epoch') != owner['epoch'] or checked < owner['since'])
                )
                row.update(
                    account_epoch=owner['epoch'] if owner else None,
                    identity_verified=bool(owner and owner['account_key']),
                    previous_account=previous_account,
                    scope=metadata,
                )
                src = sources.get(row['source'], {})
                snapshot = snapshots.get(row['route'], {})
                omitted = bool(
                    snapshot.get('source') == row['source']
                    and snapshot.get('checked', 0) >= (checked or 0)
                    and row['bucket'] not in snapshot.get('buckets', [])
                )
                row['omitted_latest'] = omitted
                row['not_applicable'] = omitted and row['bucket'] in snapshot.get('absent', [])
                # The account or plan changed and the new one's response no longer has this
                # window (a plan's 30-day window during a switch, an old model window): it is
                # an observation of the earlier conditions, not a current quota.
                row['retired'] = bool(omitted and previous_account and snapshot.get('checked', 0) >= owner['since'])
                row['family'] = family(row['route'])
                row['account_label'] = labels.get(row['route'])
                account = sources.get(account_source(row['route']), {}) if is_claude(row['route']) else {}
                if account.get('checked', 0) >= (row['checked'] or 0) and account.get('status') in (
                    'error',
                    'unavailable',
                ):
                    src = account
                user_ended = row['route'] in ended
                if (
                    not user_ended
                    and src.get('status') == 'ended'
                    and str(src.get('detail', '')).startswith(USER_ENDED_DETAIL)
                ):
                    # Tracking was switched back on; the next poll replaces this marker.
                    src = {
                        **src,
                        'status': 'unavailable',
                        'detail': '구독 추적 재개 · 다음 수집에서 한도를 다시 확인합니다.',
                    }
                stale = bool(
                    previous_account
                    or (
                        checked
                        and (
                            checked > now + SKEW
                            or now - checked > self.thresholds['stale_seconds']
                            or (resets and resets <= now)
                            or src.get('status') == 'unavailable'
                        )
                    )
                )
                row.update(
                    status='ended'
                    if user_ended
                    else 'error'
                    if src.get('status') == 'error'
                    else 'ended'
                    if src.get('status') == 'ended'
                    else 'unavailable'
                    if omitted or row['remaining'] is None or src.get('status') == 'unavailable'
                    else 'stale'
                    if stale
                    else 'fresh',
                    stale=stale or user_ended,
                    user_ended=user_ended,
                    reset_kst=datetime.fromtimestamp(resets, KST).isoformat() if resets else None,
                    seconds_to_reset=max(0, int(resets - now)) if resets else None,
                    detail=USER_ENDED_DETAIL if user_ended else src.get('detail', ''),
                    last_attempt=src.get('checked'),
                )
                row['history'] = history.get((row['route'], row['bucket']), [])
                if previous_account:
                    row['history'] = []
                row['trends'] = quota_trends(row['history'], now, row['status'])
                row['decreases'] = quota_decreases(row['history'], now)
                row['history_source'] = row['history'][-1]['source'] if row['history'] else None
                row['window'] = None
                row['window_seconds'] = window_seconds(row['bucket'])
                # Per-point route/bucket/source repeat the row; keep what the chart reads.
                row['history'] = [{k: p[k] for k in HISTORY_FIELDS} for p in row['history']]
            # A shorter window cannot be used while the longer window of the same quota is
            # exhausted. Only a current observation blocks: a stale or expired upper value
            # is not carried onto another bucket, and the lower bucket's value is kept.
            by_key = {(r['route'], r['bucket']): r for r in rows}
            for row in rows:
                parent_bucket = QUOTA_PARENTS.get((family(row['route']), row['bucket']))
                if row['route'] == 'codex' and row['bucket'].endswith(' · 300분'):
                    parent_bucket = row['bucket'].removesuffix('300분') + '10080분'
                parent = by_key.get((row['route'], parent_bucket))
                blocked = (
                    parent
                    and parent['status'] == 'fresh'
                    and parent['remaining'] is not None
                    and parent['remaining'] <= 0
                )
                row['blocked_by'] = (
                    {k: parent[k] for k in ('bucket', 'resets', 'seconds_to_reset')} if blocked else None
                )
                # The desktop app reports only the window that currently binds a model family,
                # so the other window keeps its last observation; say why it is not current.
                agy = re.fullmatch(r'(gemini|3p)-(5h|weekly)', row['bucket']) if row['route'] == 'antigravity' else None
                other = agy and by_key.get(('antigravity', f"{agy[1]}-{'weekly' if agy[2] == '5h' else '5h'}"))
                row['note'] = (
                    '설정에서 구독 종료로 표시했습니다. 한도·초기화 조회를 멈췄고 사용 기록은 계속 집계합니다.'
                    if row.get('user_ended')
                    else f"앱이 지금 {'주간' if agy[2] == '5h' else '5시간'} 창을 보고하고 있어 이 창의 현재값은 확인할 수 없습니다. 마지막 관측값입니다."
                    if other
                    and row['status'] != 'fresh'
                    and other['status'] == 'fresh'
                    and other['source'] == 'antigravity-app'
                    else '계정·요금제 변경 전 관측입니다. 변경 후 계정 응답에 이 한도가 없어 현재 한도에서 제외했습니다.'
                    if row.get('retired')
                    else '계정·구독 조건 변경 전 관측입니다.'
                    if row.get('previous_account')
                    else '최근 계정 응답에서 이 한도를 확인하지 못했습니다. 이전 관측값입니다.'
                    if row.get('omitted_latest')
                    else '로컬 관측의 계정을 확인하지 못했습니다. 계정 조회 후 작업 전망을 제공합니다.'
                    if row['scope'].get('identity_unverified')
                    else '원본 초기화 시각을 해석하지 못했습니다. 시간 전망을 보류합니다.'
                    if row['scope'].get('reset_unparsed')
                    else None
                )
            sums = ','.join(f'COALESCE(SUM({k}),0) AS {k}' for k in METRICS) + ',COALESCE(SUM(requests),0) AS requests'
            for row in rows:
                span = window_seconds(row['bucket'])
                if not row['resets'] or not span:
                    continue
                start, end = row['resets'] - span, min(now, row['resets'])
                if end <= start:
                    continue
                where, args = route_events(row['route'])
                r = c.execute(
                    'SELECT ' + sums + ' FROM events WHERE ' + where + ' AND ts>=? AND ts<?', (*args, start, end)
                ).fetchone()
                row['window'] = dict(
                    start=start, end=end, open=end < row['resets'], requests=r['requests'], tokens=token_total(dict(r))
                )
            from ..signals import quota_events

            for row in rows:
                row['events'] = (
                    quota_events(
                        c,
                        row['route'],
                        row['bucket'],
                        row['history_source'] or row['source'],
                        now,
                        since=accounts.get(row['route'], {}).get('since', 0),
                    )
                    if row['checked']
                    else None
                )
        attach_scopes(rows)
        return dict(
            limits=rows,
            sources=list(sources.values()),
            now=now,
            history_days=1,
            claude_accounts=claude,
            resources=resources,
            quota_policies=policies,
            stale_seconds=self.thresholds['stale_seconds'],
            retention_days=self.thresholds['retention_days'],
            low_percent=self.thresholds['low_percent'],
            quota_hide_days=self.thresholds['quota_hide_days'],
            unused_percent=self.thresholds['unused_percent'],
            unused_hours=self.thresholds['unused_hours'],
        )
