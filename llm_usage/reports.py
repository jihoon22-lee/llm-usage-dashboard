"""Weekly reports built by the collector from stored usage and quota history.

A report covers one finished Monday-Sunday week in KST and is kept as state
'report:<monday>' so it is built once and survives restarts.
"""
from datetime import datetime, timedelta
import json

from .store import KST

KEEP=12


def last_week(now):
    """(monday, next monday) of the most recent finished week, as KST datetimes."""
    today=datetime.fromtimestamp(now,KST).replace(hour=0,minute=0,second=0,microsecond=0)
    this_monday=today-timedelta(days=today.weekday())
    return this_monday-timedelta(days=7),this_monday


def _totals(usage):
    from .insights import token_total
    return dict(tokens=token_total(usage['totals']),requests=usage['totals'].get('requests',0),cost=(usage.get('cost') or {}).get('period'))


def exhaustions(c,start,end):
    """Quota exhaustions (remaining reaching 0 from above) observed in [start, end),
    per route and bucket, in each bucket's own source stream."""
    counts={}
    # One bucket is often observed by two streams (account poll and local status line,
    # 'codex' and 'codex-local'); each sees the same exhaustion. Count one stream per
    # bucket, preferring the account poll as quota_history does, else the busiest one.
    streams={}
    for route,bucket,source,n in c.execute('SELECT route,bucket,source,COUNT(*) FROM limit_history WHERE checked>=? AND checked<? '
                                           'GROUP BY route,bucket,source',(start,end)).fetchall():
        streams.setdefault((route,bucket),[]).append((source,n))
    chosen=[]
    for (route,bucket),items in streams.items():
        account='claude-oauth' if route=='claude-code' else route
        source=account if any(s==account for s,_ in items) else max(items,key=lambda item:(item[1],item[0]))[0]
        chosen.append((route,bucket,source))
    for route,bucket,source in chosen:
        previous=c.execute('SELECT remaining FROM limit_history WHERE route=? AND bucket=? AND source=? AND checked<? ORDER BY checked DESC LIMIT 1',
                           (route,bucket,source,start)).fetchone()
        before=previous[0] if previous else None
        for (remaining,) in c.execute('SELECT remaining FROM limit_history WHERE route=? AND bucket=? AND source=? AND checked>=? AND checked<? ORDER BY checked',
                                      (route,bucket,source,start,end)):
            if remaining is None:continue
            if before is not None and remaining<=0<before:counts[(route,bucket)]=counts.get((route,bucket),0)+1
            before=remaining
    # The provider's own label (Codex 'Title · 주간') where one was recorded.
    def label(route,bucket):
        row=c.execute('SELECT data FROM state WHERE key=?',('label:'+route+':'+bucket,)).fetchone()
        return json.loads(row[0]) if row else None
    return [dict(route=r,bucket=b,count=n,display_name=label(r,b)) for (r,b),n in sorted(counts.items())]


def build(store,now,pricing=None):
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
    top=sorted(models['rows'],key=lambda r:-token_total(r))[:5]
    totals,prior=_totals(current),_totals(previous)
    with store.connect() as c:
        exhausted=exhaustions(c,monday.timestamp(),next_monday.timestamp())
    busiest=max(current['series'],key=lambda p:sum(token_total(v) for v in p['values'].values()),default=None)
    return dict(week=day(monday),start=day(monday),end=day(next_monday-timedelta(days=1)),built=now,
                totals=totals,previous=prior,
                change_pct=(totals['tokens']-prior['tokens'])/prior['tokens']*100 if prior['tokens'] else None,
                routes=sorted(routes.values(),key=lambda e:-e['tokens']),
                models=[dict(model=r['model'],route=r['route'],tokens=token_total(r),cost=r.get('est_cost')) for r in top],
                exhausted=exhausted,busiest_day=busiest['time'][:10] if busiest and totals['tokens'] else None)


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
    monday,_=last_week(now)
    recount_exhaustions(store,now)
    with store.connect() as c:
        first=c.execute('SELECT MIN(ts) FROM events').fetchone()[0]
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


def recent(store,limit=KEEP):
    with store.connect() as c:
        return [store.state(c,r[0]) for r in c.execute("SELECT key FROM state WHERE key LIKE 'report:%' ORDER BY key DESC LIMIT ?",(limit,)).fetchall()]


def summary(report):
    """Plain-text body for an external notification."""
    from .notify import SERVICE,_label
    tokens=lambda v:f'{v/1e6:,.1f}M'
    lines=[f"{report['start']} ~ {report['end']} · {tokens(report['totals']['tokens'])} 토큰 · {report['totals']['requests']:,}회"
           +(f" · 전주 대비 {report['change_pct']:+.0f}%" if report['change_pct'] is not None else '')]
    if report['totals']['cost'] is not None:lines.append(f"공개 단가 환산 ${report['totals']['cost']:,.0f}")
    lines+= [f"{SERVICE.get(r['route'],r['route'])} {tokens(r['tokens'])}"+(f" ({r['change_pct']:+.0f}%)" if r['change_pct'] is not None else '') for r in report['routes'][:5]]
    if report['exhausted']:lines.append('한도 소진 '+', '.join(f"{_label(e)} {e['count']}회" for e in report['exhausted']))
    return '\n'.join(lines)
