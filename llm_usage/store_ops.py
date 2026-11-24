"""Detail queries and live operational status helpers extracted from Store."""

from datetime import datetime, timedelta
from pathlib import Path


def project_month(store, c, now, pricing=None):
    """Month-to-date (KST calendar month) tokens and estimated cost per project.

    cost is None when no token of the project has a rate; priced_share tells how
    much of the tokens the cost covers."""
    from .insights import token_total
    from .pricing import estimate, rate_for
    from .reconciliation import supplements
    from .store import KST, METRICS

    now = now if isinstance(now, datetime) else datetime.fromtimestamp(now, KST)
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    sums = ','.join(f'COALESCE(SUM({k}),0) AS {k}' for k in METRICS)
    out = {}
    for r in c.execute(
        "SELECT COALESCE(project,'미분류') AS project,model,date(ts,'unixepoch','+9 hours') AS day,"
        + sums
        + ' FROM '
        + store.table
        + ' WHERE ts>=? GROUP BY 1,2,3',
        (start.timestamp(),),
    ):
        row = dict(r)
        tokens = token_total(row)
        e = out.setdefault(row['project'], dict(tokens=0, cost=0.0, priced_tokens=0))
        e['tokens'] += tokens
        rate = rate_for(row['model'], pricing, row['day']) if pricing else None
        if rate:
            e['cost'] += estimate(row, rate)
            e['priced_tokens'] += tokens
    additions, _ = supplements(c)
    residual = 0
    for r in additions:
        if r['start'] >= start.timestamp() and r['end'] <= now.timestamp():
            # The Codex account residual belongs to no project; it is kept apart.
            if r['source'] == 'codex-account':
                residual += r['tokens']
                continue
            e = out.setdefault(r['project'] or '미분류', dict(tokens=0, cost=0.0, priced_tokens=0))
            e['tokens'] += r['tokens']
    for e in out.values():
        e['cost'] = round(e['cost'], 4) if e['priced_tokens'] else None
        e['priced_share'] = e.pop('priced_tokens') / e['tokens'] * 100 if e['tokens'] else None
    return dict(month=start.strftime('%Y-%m'), projects=out, residual_tokens=residual)


def project_detail(store, project, now, pricing=None, days=30, budget=None):
    """One project over the last `days` KST days: models, daily use, busiest sessions,
    and this month so far with a straight-line month-end projection (and the budget
    share when a budget is given). '미분류' is the bucket of records with no project."""
    from .insights import token_total
    from .pricing import estimate, rate_for
    from .reconciliation import supplements
    from .store import KST, METRICS, PROJECTION_FROM_DAY

    now = now if isinstance(now, datetime) else datetime.fromtimestamp(now, KST)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    start = midnight - timedelta(days=days - 1)
    month_start = midnight.replace(day=1)
    sums = ','.join(f'COALESCE(SUM({k}),0) AS {k}' for k in METRICS)
    where = " WHERE COALESCE(project,'미분류')=? AND ts>=?"
    models = {}
    daily = {}
    month = dict(tokens=0, cost=0.0, priced=0)
    with store.connect() as c:
        c.execute('BEGIN')
        for r in c.execute(
            "SELECT route,model,date(ts,'unixepoch','+9 hours') AS day,"
            + sums
            + ','
            + store.rcount
            + ' FROM '
            + store.table
            + where
            + ' GROUP BY route,model,day',
            (project, min(start, month_start).timestamp()),
        ):
            row = dict(r)
            tokens = token_total(row)
            rate = rate_for(row['model'], pricing, row['day']) if pricing else None
            part = estimate(row, rate) if rate else None
            if row['day'] >= month_start.strftime('%Y-%m-%d'):
                month['tokens'] += tokens
                if part is not None:
                    month['cost'] += part
                    month['priced'] += tokens
            if row['day'] < start.strftime('%Y-%m-%d'):
                continue
            m = models.setdefault(
                (row['route'], row['model']),
                dict(route=row['route'], model=row['model'], tokens=0, requests=0, cost=None),
            )
            m['tokens'] += tokens
            m['requests'] += row['requests']
            if part is not None:
                m['cost'] = (m['cost'] or 0) + part
            d = daily.setdefault(row['day'], dict(day=row['day'], tokens=0, requests=0))
            d['tokens'] += tokens
            d['requests'] += row['requests']
        sessions = [
            dict(session=r[0], route=r[1], tokens=r[2], requests=r[3], first_ts=r[4], last_ts=r[5])
            for r in c.execute(
                'SELECT session,MAX(route),SUM(uncached_input+cached_input+output+cache_creation),SUM(requests),MIN(ts),MAX(ts) FROM events'
                + where
                + ' AND session IS NOT NULL GROUP BY session ORDER BY 3 DESC',
                (project, start.timestamp()),
            )
        ]
        extra, _ = supplements(c)
    by_session = {r['session']: r for r in sessions}
    unallocated = []
    for e in extra:
        # The Codex account residual is listed apart from '미분류' (see reconciliation.RESIDUAL).
        if e['source'] == 'codex-account' or (e['project'] or '미분류') != project or e['end'] > now.timestamp():
            continue
        if e['start'] >= month_start.timestamp():
            month['tokens'] += e['tokens']
        if e['start'] < start.timestamp():
            continue
        m = models.setdefault(
            (e['route'], e['model'] or 'unknown'),
            dict(route=e['route'], model=e['model'] or 'unknown', tokens=0, requests=0, cost=None),
        )
        m['tokens'] += e['tokens']
        begin = datetime.fromtimestamp(e['start'], KST).date().isoformat()
        finish = datetime.fromtimestamp(e['end'] - 0.000001, KST).date().isoformat()
        if begin == finish:
            d = daily.setdefault(begin, dict(day=begin, tokens=0, requests=0))
            d['tokens'] += e['tokens']
        else:
            unallocated.append(dict(start=e['start'], end=e['end'], tokens=e['tokens']))
        if e['session']:
            session = by_session.get(e['session'])
            if session is None:
                session = by_session[e['session']] = dict(
                    session=e['session'],
                    route=e['route'],
                    tokens=0,
                    requests=0,
                    first_ts=e['start'],
                    last_ts=e['end'],
                )
                sessions.append(session)
            session['tokens'] += e['tokens']
            session['first_ts'] = min(session['first_ts'], e['start'])
            session['last_ts'] = max(session['last_ts'], e['end'])
    sessions = sorted(sessions, key=lambda r: (-r['tokens'], r['session']))[:10]
    elapsed = now.day
    days_in_month = ((month_start.replace(day=28) + timedelta(days=4)).replace(day=1) - month_start).days
    cost = month['cost'] if month['priced'] else None
    early = elapsed < PROJECTION_FROM_DAY
    projection = dict(
        month=month_start.strftime('%Y-%m'),
        days_elapsed=elapsed,
        days_in_month=days_in_month,
        tokens=month['tokens'],
        cost=round(cost, 4) if cost is not None else None,
        projected_tokens=None if early else month['tokens'] / elapsed * days_in_month,
        projected_cost=round(cost / elapsed * days_in_month, 4) if cost is not None and not early else None,
        projected_from_day=PROJECTION_FROM_DAY,
        priced_share=month['priced'] / month['tokens'] * 100 if month['tokens'] else None,
    )
    if budget:
        from .notify import budget_level

        _, projection['budget_ratio'] = budget_level(dict(tokens=month['tokens'], cost=cost), budget)
        projection['projected_budget_ratio'] = (
            None
            if early
            else budget_level(dict(tokens=projection['projected_tokens'], cost=projection['projected_cost']), budget)[1]
        )
        projection['budget'] = budget
    for m in models.values():
        m['cost'] = round(m['cost'], 4) if m['cost'] is not None else None
    return dict(
        project=project,
        days=days,
        start=start.strftime('%Y-%m-%d'),
        unallocated_intervals=unallocated,
        models=sorted(models.values(), key=lambda m: -m['tokens']),
        daily=[daily[k] for k in sorted(daily)],
        sessions=sessions,
        month=projection,
    )


def session_rows(cls, c, start, end, pricing=None, scope=None, ids=None):
    """Request-only session rows in the selected range, optionally for a bounded ID batch.

    Reconciliation uses the same aggregation to recover local detail for a
    supplemented session that did not make the request-only top 100."""
    from .pricing import estimate, rate_for
    from .store import METRICS

    where = 'ts>=? AND ts<? AND session IS NOT NULL'
    args = [start, end]
    if scope:
        kind, _, value = scope.partition(':')
        where += ' AND ' + cls.SCOPE_KINDS[kind] + '=?'
        args.append(value)
    if ids is not None:
        if not ids:
            return {}
        where += ' AND session IN (' + ','.join('?' for _ in ids) + ')'
        args.extend(ids)
    sums = ','.join(f'COALESCE(SUM({k}),0) AS {k}' for k in METRICS)
    sessions = {}
    for r in c.execute(
        "SELECT session,model,date(ts,'unixepoch','+9 hours') AS day,"
        + cls.ACCOUNT_SQL
        + ''' AS account,
            MAX(route) AS route,MAX(project) AS project,MAX(agent_kind) AS kind,MIN(ts) AS first_ts,MAX(ts) AS last_ts,
            COALESCE(SUM(requests),0) AS requests,'''
        + sums
        + ' FROM events WHERE '
        + where
        + ' GROUP BY session,model,day,4',
        args,
    ):
        e = sessions.setdefault(
            r['session'],
            dict(
                session=r['session'],
                route=r['route'],
                project=r['project'],
                kind=r['kind'],
                first_ts=r['first_ts'],
                last_ts=r['last_ts'],
                requests=0,
                cost=None,
                accounts=[],
                **dict.fromkeys(METRICS, 0),
            ),
        )
        if r['route'] == 'claude-code' and r['account'] not in e['accounts']:
            e['accounts'].append(r['account'])
        e['first_ts'] = min(e['first_ts'], r['first_ts'])
        e['last_ts'] = max(e['last_ts'], r['last_ts'])
        e['requests'] += r['requests']
        for k in METRICS:
            e[k] += r[k]
        rate = rate_for(r['model'], pricing, r['day']) if pricing else None
        if rate:
            e['cost'] = (e['cost'] or 0) + estimate(dict(r), rate)
    for e in sessions.values():
        e['accounts'].sort()
        if e['cost'] is not None:
            e['cost'] = round(e['cost'], 2)
    return sessions


def session_detail(store, session, pricing=None):
    """One session's models, hourly activity and cache share; no message content exists here."""
    from .insights import token_total
    from .pricing import estimate, rate_for
    from .reconciliation import add, supplements
    from .store import METRICS

    sums = ','.join(f'COALESCE(SUM({k}),0) AS {k}' for k in METRICS)
    with store.connect() as c:
        c.execute('BEGIN')
        models = []
        cost = 0.0
        rated = False
        for r in c.execute(
            '''SELECT route,model,date(ts,'unixepoch','+9 hours') AS day,MIN(ts) AS first_ts,MAX(ts) AS last_ts,
            COALESCE(SUM(requests),0) AS requests,'''
            + sums
            + ' FROM events WHERE session=? GROUP BY route,model,day',
            (session,),
        ):
            row = dict(r)
            rate = rate_for(row['model'], pricing, row['day']) if pricing else None
            part = estimate(row, rate) if rate else None
            key = next((m for m in models if m['route'] == row['route'] and m['model'] == row['model']), None)
            if key is None:
                key = dict(
                    route=row['route'],
                    model=row['model'],
                    first_ts=row['first_ts'],
                    last_ts=row['last_ts'],
                    requests=0,
                    cost=None,
                    **dict.fromkeys(METRICS, 0),
                )
                models.append(key)
            key['first_ts'] = min(key['first_ts'], row['first_ts'])
            key['last_ts'] = max(key['last_ts'], row['last_ts'])
            key['requests'] += row['requests']
            for k in METRICS:
                key[k] += row[k]
            if part is not None:
                key['cost'] = (key['cost'] or 0) + part
                cost += part
                rated = True
        if not models:
            evidence = c.execute(
                'SELECT * FROM usage_evidence WHERE session=? ORDER BY end DESC LIMIT 1', (session,)
            ).fetchone()
            if not evidence:
                return dict(session=session, models=[], hours=[])
            models = [
                dict(
                    route=evidence['route'],
                    model=evidence['model'] or 'unknown',
                    first_ts=evidence['start'],
                    last_ts=evidence['end'],
                    requests=0,
                    cost=None,
                    **dict.fromkeys(METRICS, 0),
                )
            ]
        meta = c.execute('SELECT MAX(project),MAX(agent_kind) FROM events WHERE session=?', (session,)).fetchone()
        hours = [
            dict(hour=r[0] * 3600, requests=r[1], tokens=r[2])
            for r in c.execute(
                'SELECT CAST(ts/3600 AS INTEGER),SUM(requests),SUM(uncached_input+cached_input+output+cache_creation) '
                'FROM events WHERE session=? GROUP BY 1 ORDER BY 1',
                (session,),
            )
        ]
        extra, _ = supplements(c)
    totals = {k: sum(m[k] for m in models) for k in METRICS}
    inputs = totals['uncached_input'] + totals['cached_input'] + totals['cache_creation']
    for m in models:
        m['cost'] = round(m['cost'], 4) if m['cost'] is not None else None
    data = dict(
        session=session,
        project=meta[0],
        kind=meta[1],
        first_ts=min(m['first_ts'] for m in models),
        last_ts=max(m['last_ts'] for m in models),
        requests=sum(m['requests'] for m in models),
        totals=totals,
        cache_share=totals['cached_input'] / inputs * 100 if inputs else None,
        cost=round(cost, 4) if rated else None,
        models=sorted(models, key=lambda m: -token_total(m)),
        hours=hours,
    )
    data['unallocated_intervals'] = []
    for e in extra:
        if e['session'] != session:
            continue
        add(data['totals'], e)
        m = next((m for m in data['models'] if m['route'] == e['route'] and m['model'] == e['model']), None)
        if m is None:
            m = dict(
                route=e['route'],
                model=e['model'] or 'unknown',
                requests=0,
                cost=None,
                first_ts=e['start'],
                last_ts=e['end'],
                **dict.fromkeys(METRICS, 0),
            )
            data['models'].append(m)
        add(m, e)
        lo = int(e['start'] // 3600) * 3600
        hi = int((e['end'] - 0.000001) // 3600) * 3600
        if lo == hi:
            h = next((h for h in hours if h['hour'] == lo), None)
            if h is None:
                h = dict(hour=lo, requests=0, tokens=0)
                hours.append(h)
            h['tokens'] += e['tokens']
        else:
            data['unallocated_intervals'].append(dict(start=e['start'], end=e['end'], tokens=e['tokens']))
    data['models'].sort(key=lambda m: -token_total(m))
    hours.sort(key=lambda h: h['hour'])
    inputs = sum(data['totals'][k] for k in ('uncached_input', 'cached_input', 'cache_creation'))
    data['cache_share'] = data['totals']['cached_input'] / inputs * 100 if inputs else None
    return data


def storage_row(store, checked=None):
    """Database, WAL and backup sizes, so growth is visible before it is a problem."""

    def size(path):
        try:
            return Path(path).stat().st_size
        except OSError:
            return 0

    database = size(store.path)
    wal = size(store.path + '-wal')
    backups = sorted(Path(store.path).parent.glob('backups/usage-*.db.gz'))
    kept = sum(size(p) for p in backups)
    return dict(
        name='데이터베이스',
        checked=checked,
        status='ok',
        detail=f'DB {database / 1e6:.0f}MB · WAL {wal / 1e6:.0f}MB · 백업 {len(backups)}개 {kept / 1e6:.0f}MB',
    )


def usage_live(store, c, rows, now):
    """Source freshness and availability change with wall time, so they are
    computed on every call instead of being cached with the body."""
    from .store import ONE_TIME_SOURCES, STATUS_LINE_SOURCES

    now_ts = now.timestamp()
    # The last success lets the page tell a single failed poll from a lasting failure.
    health = dict(c.execute('SELECT name,last_success FROM source_health').fetchall())
    sources = [dict(r, last_success=health.get(r['name'])) for r in c.execute('SELECT * FROM sources ORDER BY name')]
    stale = store.thresholds['stale_seconds']
    collector = store.state(c, 'collector', {})
    sources.append(
        dict(
            name='수집기',
            checked=collector.get('checked'),
            status='ok'
            if collector.get('checked') and now_ts - collector['checked'] <= stale
            else 'stale'
            if collector.get('checked')
            else 'unavailable',
            detail=collector.get('status') or '기록 없음',
        )
    )
    backup = store.state(c, 'backup', {})
    if backup:
        fresh = now_ts - backup.get('ts', 0) <= 86400 + stale
        sources.append(
            dict(
                name='일일 백업',
                checked=backup.get('ts'),
                status='error' if not backup.get('ok') else 'ok' if fresh else 'stale',
                detail=backup.get('error') or f"{backup.get('file', '')} · {backup.get('size', 0) / 1e6:.0f}MB",
            )
        )
    else:
        sources.append(dict(name='일일 백업', checked=None, status='unavailable', detail='아직 실행되지 않음'))
    verify = store.state(c, 'verify', {})
    if verify:
        from .verify import partial

        bad = not verify.get('ok')
        sections = ('antigravity', 'devin', 'codex_original', 'claude', 'opencode', 'aside')
        misses = sum((verify.get(s) or {}).get('missing_events', 0) for s in sections)
        mismatch = verify.get('codex', {}).get('mismatches', 0) + sum(
            (verify.get(s) or {}).get('mismatched_events', 0) for s in sections
        )
        errors = sum(
            (verify.get(s) or {}).get(k, 0)
            for s in sections
            for k in ('source_errors', 'source_conflicts', 'alias_errors', 'output_split_errors')
        )
        incomplete = partial(verify)
        sources.append(
            dict(
                name='원본 대조',
                checked=verify.get('ts'),
                status='error'
                if bad
                else 'stale'
                if now_ts - verify.get('ts', 0) > 86400 + stale
                else 'partial'
                if incomplete
                else 'ok',
                detail=verify.get('error')
                or (
                    (
                        f'누락 {misses}건 · 불일치 {mismatch}건 · 오류 {errors}건'
                        + (
                            ' · 시간별 집계가 원본과 달라 다시 계산함'
                            if (verify.get('rollup') or {}).get('matches') is False
                            else ''
                        )
                    )
                    if bad
                    else '확인 가능한 원본과 일치 · 미수집·해석 불가·과거 원본 부족 또는 세부값 상충이 있어 일부만 확인됨'
                    if incomplete
                    else '원본과 일치'
                ),
            )
        )
    else:
        sources.append(dict(name='원본 대조', checked=None, status='unavailable', detail='아직 실행되지 않음'))
    sources.append(store.storage_row(store.state(c, 'last_local')))
    ops_rows = {'수집기', '일일 백업', '원본 대조', '데이터베이스'}
    for source in sources:
        # Ops pseudo-rows carry daily cadences; they compute staleness themselves. A
        # finished one-time job (the attribution backfill) keeps its result: its age
        # is not an old value.
        if source['name'] in ops_rows or source['name'] in ONE_TIME_SOURCES:
            continue
        # Resource reads run every 15 minutes, for every Claude account ('claude-credits:<id>').
        source_stale = (
            max(1800, store.thresholds['stale_seconds'])
            if source['name'].split(':')[0] in ('claude-credits', 'claude-resets')
            else store.thresholds['stale_seconds']
        )
        if source['name'] == 'codex-consumption-daily':
            source_stale = max(source_stale, 7200)
        elif source['name'] in ('codex-activity', 'codex-consumption-threads'):
            source_stale = max(source_stale, 1800)
        if source['status'] == 'ok' and now_ts - source['checked'] > source_stale:
            source['status'] = 'stale'
    # A status line reports only while a terminal UI runs it (not the desktop apps
    # or headless workers). After a day without a receipt it is shown as unused,
    # not as a failing source; the quota stays visible through account/app polling.
    for source in sources:
        if (
            (source['name'] in STATUS_LINE_SOURCES or source['name'].startswith('claude-code:'))
            and source['status'] == 'stale'
            and source.get('checked')
            and now_ts - source['checked'] > 86400
        ):
            source.update(
                status='idle',
                detail=' · '.join(
                    filter(
                        None,
                        [
                            source.get('detail'),
                            '최근 24시간 상태줄 수신 없음 · 터미널(TUI) 실행 중에만 수신 · 한도는 계정/앱 조회로 표시',
                        ],
                    )
                ),
            )
    unavailable_routes = [
        dict(route=source['name'].removesuffix('-records'), detail=source['detail'])
        for source in sources
        if source['name'].endswith('-records')
        and source['status'] == 'unavailable'
        and not any(row['route'] == source['name'].removesuffix('-records') for row in rows)
    ]
    observation = next((s for s in sources if s['name'] == 'antigravity-tokens'), None)
    for gap in unavailable_routes:
        if gap['route'] == 'antigravity' and observation and observation['status'] == 'partial':
            gap.update(
                observed=True,
                observation_checked=observation['checked'],
                observation_stale=now_ts - observation['checked'] > store.thresholds['stale_seconds'],
            )
    return sources, unavailable_routes
