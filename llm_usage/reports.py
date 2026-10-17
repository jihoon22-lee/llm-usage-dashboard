"""Weekly reports built by the collector from stored usage and quota history.

A report covers one finished Monday-Sunday week in KST and is kept as state
'report:<monday>' so it is built once and survives restarts.
"""
from datetime import datetime, timedelta
import json

from .store import KST

KEEP=12
# Bumped when what a report holds changes, so the stored weeks are rebuilt once (not
# announced again). 2: the Codex account residual left the top models for its own line.
# 3: Claude Code split by account, and the top projects.
REPORT_FORMAT=3


def last_week(now):
    """(monday, next monday) of the most recent finished week, as KST datetimes."""
    today=datetime.fromtimestamp(now,KST).replace(hour=0,minute=0,second=0,microsecond=0)
    this_monday=today-timedelta(days=today.weekday())
    return this_monday-timedelta(days=7),this_monday


def _totals(usage):
    from .insights import token_total
    return dict(unallocated_tokens=(usage.get('reconciliation') or {}).get('unallocated_tokens',0),supplemental_tokens=(usage.get('reconciliation') or {}).get('supplemental_tokens',0),tokens=token_total(usage['totals']),requests=usage['totals'].get('requests',0),cost=(usage.get('cost') or {}).get('period'))


def _points(c,route,bucket,source,start,end,since=None):
    """One source stream's readings for [start, end), segmented (insights.segment).

    Readings up to 10 minutes outside the week join it, so an interval or a reset at the
    week's edge is seen; observations of a previous account (before `since`) are left out."""
    from .insights import segment
    rows=[dict(r) for r in c.execute('SELECT checked,remaining,resets FROM limit_history WHERE route=? AND bucket=? AND source=? '
                                     'AND checked>=? AND checked<=? ORDER BY checked',
                                     (route,bucket,source,max(start-600,since or 0),end+600))]
    interruptions=[r[0] for r in c.execute('SELECT checked FROM quota_interruptions WHERE source=? AND checked>=? AND checked<=?',
                                           (source,start-600,end+600))]
    points=segment(rows,interruptions)
    # A fetch failure since the previous reading, whichever break reason segment() gives
    # first (a reset or a rise in remaining can come before 'error').
    for previous,point in zip(points,points[1:]):
        point['interrupted']=any(previous['checked']<t<=point['checked'] for t in interruptions)
    return points


def _use(points,start,end):
    """(observed seconds, 0% seconds, remaining before each observed reset) inside [start, end)."""
    observed=zero=0.0;before=[];epoch_reset=None
    for previous,point in zip(points,points[1:]):
        if previous['break_before']:epoch_reset=previous['resets']
        span=point['checked']-previous['checked']
        inside=max(0,min(point['checked'],end)-max(previous['checked'],start))
        # A recorded fetch failure between two readings leaves that interval unobserved.
        if span<=600 and not point['interrupted'] and previous['remaining'] is not None and point['remaining'] is not None:
            observed+=inside
            if previous['remaining']<=0 and point['remaining']<=0:zero+=inside
        # The window's reset time passed between two consecutive readings: whatever the
        # next reading says (a new window, no window yet, the old time), the reset was seen.
        # A new window read up to a minute early (clock skew) counts as well.
        skew=60 if point['break_reason']=='reset' else 0
        if (span<=600 and epoch_reset is not None and start<=epoch_reset<end
                and previous['checked']<epoch_reset<=point['checked']+skew and previous['remaining'] is not None):
            before.append(previous['remaining'])
    return observed,zero,before


def _streams(c,start,end):
    """[(route, bucket, source, points)] observed in [start, end), one source stream per bucket.

    One bucket is often observed by two streams (account poll and local status line,
    'codex' and 'codex-local'); each sees the same exhaustion. Use the stream that observed
    the bucket longest in the week, the account poll on a tie as quota_history prefers it,
    so an account poll that stopped early in the week does not hide the readings after it."""
    from .claude_accounts import account_source
    since={r[0]:r[1] for r in c.execute('SELECT route,since FROM resource_accounts')}
    streams={}
    for route,bucket,source,n in c.execute('SELECT route,bucket,source,COUNT(*) FROM limit_history WHERE checked>=? AND checked<? '
                                           'GROUP BY route,bucket,source',(start,end)).fetchall():
        points=_points(c,route,bucket,source,start,end,since.get(route))
        streams.setdefault((route,bucket),[]).append(((_use(points,start,end)[0],source==account_source(route),n,source),points))
    chosen=[]
    for (route,bucket),items in sorted(streams.items()):
        rank,points=max(items,key=lambda item:item[0])
        chosen.append((route,bucket,rank[3],points))
    return chosen


def _names(c):
    """The provider's own bucket label and, with several Claude accounts, account names."""
    def label(route,bucket):
        row=c.execute('SELECT data FROM state WHERE key=?',('label:'+route+':'+bucket,)).fetchone()
        return json.loads(row[0]) if row else None
    from .claude_accounts import describe
    accounts=describe(c)
    names={a['route']:a['label'] for a in accounts} if len(accounts)>1 else {}
    return label,names


def _accounts(c,usage):
    """Several Claude accounts: Claude Code by account (계정 미확인 included, so the parts
    add up to the service), with the plan names of the moment; the page and the notification
    show the names set in the dashboard. None with one account."""
    from .claude_accounts import UNKNOWN
    _,names=_names(c)
    return [dict(account=e['account'],label='계정 미확인' if e['account']==UNKNOWN else names.get(e['account']),tokens=e['tokens'],cost=e['cost'])
            for e in usage.get('claude_split') or [] if e['tokens']] or None


def exhaustions(c,start,end):
    """Quota exhaustions (remaining reaching 0 from above) observed in [start, end),
    per route and bucket, in each bucket's own source stream."""
    counts={}
    for route,bucket,source,_ in _streams(c,start,end):
        previous=c.execute('SELECT remaining FROM limit_history WHERE route=? AND bucket=? AND source=? AND checked<? ORDER BY checked DESC LIMIT 1',
                           (route,bucket,source,start)).fetchone()
        before=previous[0] if previous else None
        for (remaining,) in c.execute('SELECT remaining FROM limit_history WHERE route=? AND bucket=? AND source=? AND checked>=? AND checked<? ORDER BY checked',
                                      (route,bucket,source,start,end)):
            if remaining is None:continue
            if before is not None and remaining<=0<before:counts[(route,bucket)]=counts.get((route,bucket),0)+1
            before=remaining
    # The provider's own label (Codex 'Title · 주간') where one was recorded; several
    # Claude accounts: each entry names its account ('Claude(Max 5x) 5시간').
    label,names=_names(c)
    return [dict(route=r,bucket=b,count=n,display_name=label(r,b),**({'account_label':names[r]} if r in names else {}))
            for (r,b),n in sorted(counts.items())]


def quota_use(c,start,end):
    """How each quota window was used in [start, end), from observations only.

    observed_hours: time covered by consecutive observations (no gap over 10 minutes and
    no recorded fetch failure between them).
    zero_hours: the part of it where two consecutive observations were both 0%.
    before_reset: the last observed remaining of each window that reset in the week,
    counted only when the reset was seen as it happened: a reading before the window's
    reset time and the next one at most 10 minutes later, after it. A window that reset
    unobserved is not guessed; a reset time that moves while a window is idle, or a grant
    that resets a window early, is not a reset. Windows observed for under a minute are
    left out, as are observations of a previous account (before its current epoch).
    Readings up to 10 minutes outside the week join it, so a reset or an interval at the
    week's edge is seen; time is counted only inside the week."""
    out=[]
    for route,bucket,source,points in _streams(c,start,end):
        observed,zero,before=_use(points,start,end)
        if observed<60 and not before:continue
        out.append(dict(route=route,bucket=bucket,observed_hours=round(observed/3600,2),zero_hours=round(zero/3600,2),
                        resets_observed=len(before),
                        before_reset=dict(avg=round(sum(before)/len(before),1),min=round(min(before),1),max=round(max(before),1)) if before else None))
    label,names=_names(c)
    for row in out:
        row['display_name']=label(row['route'],row['bucket'])
        if row['route'] in names:row['account_label']=names[row['route']]
    return out


def residual_share(c,start,end):
    """The Codex account residual (account day total minus this PC's records) of the
    account days lying wholly inside [start, end), with the account total of those same
    days. Account days run 09:00–09:00 KST, so a Monday–Sunday week covers six of them;
    None when no such day was received."""
    from .reconciliation import account_days,supplements
    days=[r for r in account_days(c) if start<=r['start'] and r['end']<=end]
    if not days:return None
    starts={r['start'] for r in days}
    residual=sum(e['tokens'] for e in supplements(c)[0] if e['source']=='codex-account' and e['start'] in starts)
    return dict(tokens=residual,account_tokens=sum(r['tokens'] for r in days),days=len(days))


def history_cutoff(store,c):
    """Quota history older than this is gone (None: nothing pruned yet). A later, longer
    retention_days does not bring it back."""
    cutoff=store.state(c,'history_cutoff')
    if cutoff is None and store.state(c,'history_pruned'):
        # Pruned before the cutoff was recorded.
        cutoff=store.state(c,'history_pruned')-store.thresholds['retention_days']*86400
    return cutoff


def quota_covered(store,c,start):
    """Whether quota history from `start` on is still kept; a rebuilt older week keeps the
    figures computed back then."""
    cutoff=history_cutoff(store,c)
    return cutoff is None or start>=cutoff


def quota_covered_from(store,c,start):
    """Where the quota history kept from `start` on begins."""
    return max(start,history_cutoff(store,c) or start)


def quota_rows(c,start,end,exhausted=None):
    """Quota use per window in [start, end) with its exhaustion count (exhausted)."""
    if exhausted is None:exhausted=exhaustions(c,start,end)
    use=quota_use(c,start,end)
    counts={(e['route'],e['bucket']):e['count'] for e in exhausted}
    # An exhaustion counted from a reading just before the span can leave a window with no
    # observed interval inside it; it keeps a row (observed 0) so the count is not lost.
    have={(row['route'],row['bucket']) for row in use}
    for e in exhausted:
        if (e['route'],e['bucket']) not in have:
            use.append(dict(route=e['route'],bucket=e['bucket'],observed_hours=0,zero_hours=0,resets_observed=0,before_reset=None,
                            display_name=e.get('display_name'),**({'account_label':e['account_label']} if e.get('account_label') else {})))
    for row in use:row['exhausted']=counts.get((row['route'],row['bucket']),0)
    return use


def streaks(store,c,monday,exhausted):
    """{(route, bucket): consecutive weeks ending with this one that had an exhaustion},
    from the stored reports of the previous weeks; a missing week ends a run."""
    out={}
    for e in exhausted or []:
        key=(e['route'],e['bucket']);run=1
        for back in range(1,KEEP):
            report=store.state(c,'report:'+(monday-timedelta(days=7*back)).strftime('%Y-%m-%d'))
            if not report or not any((x['route'],x['bucket'])==key and x.get('count',0)>0 for x in report.get('exhausted') or []):break
            run+=1
        out[key]=run
    return out


def build(store,now,pricing=None):
    # Every usage view of the week and the residual share read the same supplements.
    from .reconciliation import held
    with held():return _build(store,now,pricing)


def _build(store,now,pricing=None):
    monday,next_monday=last_week(now)
    day=lambda d:d.strftime('%Y-%m-%d')
    span=dict(period='custom',granularity='day',pricing=pricing,sections='core')
    current=store.usage(start=day(monday),end=day(next_monday-timedelta(days=1)),group='route',**span)
    previous=store.usage(start=day(monday-timedelta(days=7)),end=day(monday-timedelta(days=1)),group='route',**span)
    models=store.usage(start=day(monday),end=day(next_monday-timedelta(days=1)),group='model',**span)
    from .insights import token_total
    routes={}
    for row in current['rows']:
        e=routes.setdefault(row['route'],dict(route=row['route'],tokens=0,requests=0,cost=None))
        e['tokens']+=token_total(row);e['requests']+=row.get('requests',0)
        if row.get('est_cost') is not None:e['cost']=(e['cost'] or 0)+row['est_cost']
    before={}
    for row in previous['rows']:before[row['route']]=before.get(row['route'],0)+token_total(row)
    for e in routes.values():
        e['change_pct']=(e['tokens']-before[e['route']])/before[e['route']]*100 if before.get(e['route']) else None
    # The Codex account residual is not a model or a project; it is reported on its own line.
    from .reconciliation import RESIDUAL
    top=sorted((r for r in models['rows'] if r['model']!=RESIDUAL),key=lambda r:-token_total(r))[:5]
    insight=store.usage(start=day(monday),end=day(next_monday-timedelta(days=1)),group='route',**{**span,'sections':'insights'})
    projects=sorted((p for p in insight.get('projects') or [] if p['project']!=RESIDUAL),key=lambda p:-token_total(p))[:5]
    totals,prior=_totals(current),_totals(previous)
    # Quota figures need the week's quota history; once it is pruned they are None
    # (not 0) and ensure_report keeps the values an earlier build computed.
    with store.connect() as c:
        covered=quota_covered(store,c,monday.timestamp())
        exhausted=exhaustions(c,monday.timestamp(),next_monday.timestamp()) if covered else None
        use=quota_rows(c,monday.timestamp(),next_monday.timestamp(),exhausted) if covered else None
        runs=streaks(store,c,monday,exhausted)
        residual=residual_share(c,monday.timestamp(),next_monday.timestamp())
        accounts=_accounts(c,current)
    for row in use or []:row['streak']=runs.get((row['route'],row['bucket']),0)
    busiest=max(current['series'],key=lambda p:sum(token_total(v) for v in p['values'].values()),default=None)
    return dict(week=day(monday),start=day(monday),end=day(next_monday-timedelta(days=1)),built=now,
                totals=totals,previous=prior,
                change_pct=(totals['tokens']-prior['tokens'])/prior['tokens']*100 if prior['tokens'] else None,
                routes=sorted(routes.values(),key=lambda e:-e['tokens']),
                models=[dict(model=r['model'],route=r['route'],tokens=token_total(r),cost=r.get('est_cost')) for r in top],
                accounts=accounts,projects=[dict(project=p['project'],tokens=token_total(p),cost=p.get('cost')) for p in projects],
                exhausted=exhausted,quota_use=use,residual=residual,busiest_day=busiest['time'][:10] if busiest and totals['tokens'] else None)


def recount_exhaustions(store,now):
    """Once: reports built before exhaustions were counted per bucket (not per stream)
    are corrected for the weeks whose quota history is still retained."""
    with store.connect() as c:
        if store.state(c,'reports:exhaustions-v2'):return
        oldest=now-store.thresholds['retention_days']*86400
        for key,in c.execute("SELECT key FROM state WHERE key LIKE 'report:%'").fetchall():
            report=store.state(c,key)
            start=datetime.strptime(report.get('start',''),'%Y-%m-%d').replace(tzinfo=KST) if report.get('start') else None
            if not start or start.timestamp()<oldest:continue
            report['exhausted']=exhaustions(c,start.timestamp(),(start+timedelta(days=7)).timestamp())
            store.save_state(c,key,report)
        store.save_state(c,'reports:exhaustions-v2',True)


def ensure_report(store,now,pricing=None):
    """Build the finished weeks that have no report yet, newest last; returns the
    newest one when it was built now (only that one is announced).

    Weeks missed while the collector was stopped are filled in, within the kept
    count and never before the first recorded event."""
    # The change check and every rebuilt week read the same supplements; only reports
    # are written meanwhile.
    from .reconciliation import held
    with held():return _ensure_report(store,now,pricing)


def _ensure_report(store,now,pricing=None):
    monday,_=last_week(now)
    recount_exhaustions(store,now)
    # Reconcile previously published local reports after a backfill. Do not
    # re-send notifications for a report that already existed.
    with store.connect() as c:
        from .reconciliation import supplements
        from .store import identity,TOKENS
        # Closed-week day/model totals also change when detailed records arrive,
        # and linked records can shrink an aggregate without a new snapshot.
        low=(monday-timedelta(weeks=KEEP)).timestamp();high=(monday+timedelta(days=7)).timestamp()
        sums=','.join('SUM('+k+')' for k in TOKENS)
        records=[tuple(r) for r in c.execute("SELECT date(ts,'unixepoch','+9 hours'),route,model,SUM(requests),"+sums+
            ' FROM usage_hourly WHERE ts>=? AND ts<? GROUP BY 1,2,3 ORDER BY 1,2,3',(low,high))]
        additions,_=supplements(c)
        additions=[e for e in additions if e['start']<high and e['end']>low]
        revision=identity(json.dumps([records,additions,pricing,REPORT_FORMAT],sort_keys=True))
        refresh=revision!=store.state(c,'reports:usage-revision')
        old_weeks=[r[0] for r in c.execute("SELECT key FROM state WHERE key LIKE 'report:%'")] if refresh else []
    # Oldest first, so a week's consecutive-exhaustion count reads the rebuilt week before it.
    for key in sorted(old_weeks):
        week=datetime.strptime(key[7:],'%Y-%m-%d').replace(tzinfo=KST)
        rebuilt=build(store,(week+timedelta(days=7)).timestamp(),pricing);rebuilt['built']=now
        with store.connect() as c:
            old=store.state(c,key) or {}
            for field in ('exhausted','quota_use'):
                if rebuilt.get(field) is None and old.get(field) is not None:rebuilt[field]=old[field]
            store.save_state(c,key,rebuilt)
    if refresh:
        with store.connect() as c:store.save_state(c,'reports:usage-revision',revision)
    with store.connect() as c:
        first=c.execute('SELECT MIN(ts) FROM events').fetchone()[0]
        if additions:first=min([first]+[e['start'] for e in additions]) if first is not None else min(e['start'] for e in additions)
        missing=[]
        for back in range(KEEP):
            week=monday-timedelta(days=7*back)
            if first is None or (week+timedelta(days=7)).timestamp()<=first:break
            if not store.state(c,'report:'+week.strftime('%Y-%m-%d')):missing.append(back)
    report=None
    for back in reversed(missing):
        report=build(store,now-7*86400*back,pricing);report['built']=now
        with store.connect() as c:store.save_state(c,'report:'+report['week'],report)
    with store.connect() as c:
        for (old,) in c.execute("SELECT key FROM state WHERE key LIKE 'report:%' ORDER BY key DESC LIMIT -1 OFFSET ?",(KEEP,)).fetchall():
            c.execute('DELETE FROM state WHERE key=?',(old,))
    return report if missing and missing[0]==0 else None


def this_week(store,now,pricing=None):
    """This week so far (KST Monday 00:00 to now) against last week cut at the same time:
    the period cards' comparison (`prior`), with this week's services and Claude accounts."""
    from .insights import token_total
    from .reconciliation import held
    monday,_=last_week(now);monday+=timedelta(days=7)
    day=lambda d:d.strftime('%Y-%m-%d')
    with held():
        usage=store.usage(period='custom',start=day(monday),end=day(monday+timedelta(days=6)),group='route',pricing=pricing,sections='core',now=now)
    routes={}
    for row in usage['rows']:routes[row['route']]=routes.get(row['route'],0)+token_total(row)
    with store.connect() as c:accounts=_accounts(c,usage)
    return dict(start=day(monday),end=day(monday+timedelta(days=6)),tokens=token_total(usage['totals']),requests=usage['totals'].get('requests',0),
                cost=(usage.get('cost') or {}).get('period'),prior=usage.get('prior'),accounts=accounts,
                routes=[dict(route=route,tokens=tokens) for route,tokens in sorted(routes.items(),key=lambda x:-x[1]) if tokens])


def recent(store,limit=KEEP):
    with store.connect() as c:
        return [store.state(c,r[0]) for r in c.execute("SELECT key FROM state WHERE key LIKE 'report:%' ORDER BY key DESC LIMIT ?",(limit,)).fetchall()]


def summary(report,residual_label=None,names=None):
    """Plain-text body for an external notification. `names`: the Claude account names set
    in the dashboard, over the ones stored at build time."""
    from .notify import SERVICE,_label
    tokens=lambda v:f'{v/1e6:,.1f}M'
    lines=[f"{report['start']} ~ {report['end']} · {tokens(report['totals']['tokens'])} 토큰 · {report['totals']['requests']:,}회"
           +(f" · 전주 대비 {report['change_pct']:+.0f}%" if report['change_pct'] is not None else '')]
    if report['totals']['cost'] is not None:lines.append(f"공개 단가 환산 ${report['totals']['cost']:,.0f}")
    lines+= [f"{SERVICE.get(r['route'],r['route'])} {tokens(r['tokens'])}"+(f" ({r['change_pct']:+.0f}%)" if r['change_pct'] is not None else '') for r in report['routes'][:5]]
    if report.get('accounts'):
        named=lambda a:(names or {}).get(a['account']) or a.get('label') or '계정'
        lines.append('Claude 계정별 '+' · '.join(f"{named(a)} {tokens(a['tokens'])}" for a in report['accounts']))
    if report.get('projects'):lines.append('상위 프로젝트 '+' · '.join(f"{p['project']} {tokens(p['tokens'])}" for p in report['projects'][:3]))
    if report['exhausted']:lines.append('한도 소진 '+', '.join(f"{_label(e)} {e['count']}회"+(f" ({e['streak']}주 연속)" if e.get('streak',0)>1 else '')
                                                         for e in _with_streaks(report)))
    share=report.get('residual')
    if share and share['tokens'] and share['account_tokens']:
        from .reconciliation import RESIDUAL_LABEL
        lines.append(f"Codex {residual_label or RESIDUAL_LABEL} {tokens(share['tokens'])} 토큰 · 계정 합계의 {share['tokens']/share['account_tokens']*100:.0f}% ({share['days']}일 기준)")
    zero=sorted((q for q in report.get('quota_use') or [] if q['zero_hours']>0),key=lambda q:-q['zero_hours'])
    if zero:lines.append('0%로 머문 시간 '+', '.join(f"{_label(q)} {_hours(q['zero_hours'])}" for q in zero[:3]))
    return '\n'.join(lines)


def _with_streaks(report):
    runs={(q['route'],q['bucket']):q.get('streak',0) for q in report.get('quota_use') or []}
    return [{**e,'streak':runs.get((e['route'],e['bucket']),0)} for e in report['exhausted']]


def _hours(value):
    return f'{round(value*60)}분' if value<1 else f'{value:.1f}시간'.replace('.0시간','시간')
