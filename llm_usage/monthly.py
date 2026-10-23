"""Monthly subscription settlements built by the collector once a KST month is over.

A settlement covers one finished calendar month in KST and is kept as state
'month:<YYYY-MM>', so it is built once and survives restarts. It holds each
subscription's list-price value against the monthly price set when the settlement was
first built, the month's quota use from the quota history still kept, and the month's
top models, projects and Claude accounts. Nothing is announced.
"""
from datetime import datetime, timedelta
import json

from .store import KST

KEEP=12
# Bumped when what a settlement holds changes, so the stored months are rebuilt once.
# 2: account boundaries also constrain the month's quota exhaustion counts.
MONTH_FORMAT=3


def month_start(now):
    """KST 1st 00:00 of the month holding `now`."""
    return datetime.fromtimestamp(now,KST).replace(day=1,hour=0,minute=0,second=0,microsecond=0)


def shift(start,months):
    """The 1st of the month `months` away from the month starting at `start`."""
    index=start.year*12+start.month-1+months
    return start.replace(year=index//12,month=index%12+1)


def active_prices(start,prices,ended):
    """(monthly prices, routes ended before the month began): a subscription ended during
    or after the month is still settled for it."""
    gone={route for route,info in (ended or {}).items()
          if not isinstance(info,dict) or not isinstance(info.get('since'),(int,float)) or info['since']<start.timestamp()}
    return {route:value for route,value in (prices or {}).items() if route not in gone},gone


def first_record(c):
    """When the records begin: the first request record or received aggregate."""
    from .reconciliation import supplements
    first=c.execute('SELECT MIN(ts) FROM events').fetchone()[0]
    received=[];supplements(c,received)
    starts=[r['start'] for r in received]
    if starts:first=min([first]+starts) if first is not None else min(starts)
    return first


def build(store,start,now,pricing=None,prices=None,ended=None,stored=None):
    """One month's settlement. A rebuild (`stored`) keeps the monthly prices it was first
    built with, and its quota figures when less of the month's quota history is kept now."""
    from .insights import token_total
    from .reconciliation import RESIDUAL,held
    from .claude_accounts import FAMILY,registry
    from .reports import _accounts,_names,_totals,quota_covered_from,quota_rows,residual_share
    end=shift(start,1)
    day=lambda d:d.strftime('%Y-%m-%d')
    if stored is not None:prices,gone={s['route']:s['monthly_usd'] for s in stored.get('subscriptions') or []},set()
    else:prices,gone=active_prices(start,prices,ended)
    span=dict(period='custom',start=day(start),end=day(end-timedelta(days=1)),pricing=pricing,now=now)
    with held():
        usage=store.usage(group='route',sections='core',subscriptions=prices,ended=gone,**span)
        models=store.usage(group='model',sections='core',**span)
        insight=store.usage(group='route',sections='insights',**span)
        with store.connect() as c:
            first=first_record(c)
            accounts=_accounts(c,usage)
            residual=residual_share(c,start.timestamp(),end.timestamp())
            # Quota history is kept for retention_days only: the month is summarized from
            # what is left of it, and says from when.
            quota_from=quota_covered_from(store,c,start.timestamp())
            use=quota_rows(c,quota_from,end.timestamp()) if quota_from<end.timestamp() else None
            _,names=_names(c)
            # A further Claude account first seen after the month did not exist in it.
            later={r['route'] for r in registry(c) if r['route']!=FAMILY and (r['first_seen'] or 0)>=end.timestamp()}
    if stored is not None and stored.get('quota_use') is not None and (use is None or stored['quota_from']<quota_from):
        use,quota_from=stored['quota_use'],stored['quota_from']
    # A subscription with neither a price nor use in the month has nothing to settle. Claude
    # accounts keep their plan name of the moment for when the page has no names yet.
    subs=[dict(route=s['route'],monthly_usd=s['monthly_usd'],value=s['period_cost'],tokens=s['period_tokens'],
               ratio=s['period_cost']/s['monthly_usd'] if s['monthly_usd'] and s['period_cost'] is not None else None,
               **({'label':names[s['route']]} if s['route'] in names else {}))
          for s in usage.get('subscriptions') or [] if (s['monthly_usd'] is not None or s['period_tokens']) and s['route'] not in later
          # A rebuild settles the same subscriptions as the first time.
          and (stored is None or s['route'] in prices)]
    top=sorted((r for r in models['rows'] if r['model']!=RESIDUAL),key=lambda r:-token_total(r))[:5]
    projects=sorted((p for p in insight.get('projects') or [] if p['project']!=RESIDUAL),key=lambda p:-token_total(p))[:5]
    partial=first is not None and first>start.timestamp()
    return dict(month=start.strftime('%Y-%m'),start=day(start),end=day(end-timedelta(days=1)),built=now,
                partial=partial,records_from=day(datetime.fromtimestamp(first,KST)) if partial else None,
                totals=dict(**_totals(usage),residual_tokens=(usage.get('reconciliation') or {}).get('residual_tokens',0)),
                subscriptions=subs,
                models=[dict(model=r['model'],route=r['route'],tokens=token_total(r),cost=r.get('est_cost'),advisor_calls=r.get('advisor_calls',0)) for r in top],
                projects=[dict(project=p['project'],tokens=token_total(p),cost=p.get('cost')) for p in projects],
                accounts=accounts,residual=residual,quota_use=use,quota_from=quota_from if use is not None else None)


def ensure_months(store,now,pricing=None,prices=None,ended=None):
    """Settle the finished months that have none yet, oldest first, within the kept count
    and never before the first record. Late records, received aggregates or rates of the
    last two finished months rebuild those two; a format change rebuilds every month kept.
    Returns the months (re)built; nothing is announced."""
    from .reconciliation import held,supplements
    from .reports import SETTLE_AFTER
    from .store import identity,METRICS
    current=month_start(now-SETTLE_AFTER);last=shift(current,-1)
    with held():
        with store.connect() as c:
            low,high=shift(last,-1).timestamp(),current.timestamp()
            sums=','.join('SUM('+k+')' for k in METRICS)
            # Project and account too: attributing records later keeps every token sum.
            records=[tuple(r) for r in c.execute("SELECT date(ts,'unixepoch','+9 hours'),route,model,project,account,SUM(requests),"+sums+
                                                 ' FROM usage_hourly WHERE ts>=? AND ts<? GROUP BY 1,2,3,4,5 ORDER BY 1,2,3,4,5',(low,high))]
            # Only what lies wholly inside those months can change their settlements; an
            # account day still running across the month's end does not.
            additions=[e for e in supplements(c)[0] if low<=e['start'] and e['end']<=high]
            revision=identity(json.dumps([records,additions,pricing],sort_keys=True))
            changed=revision!=store.state(c,'months:revision')
            reformat=store.state(c,'months:format')!=MONTH_FORMAT
            stored={key[6:]:store.state(c,key) for (key,) in c.execute("SELECT key FROM state WHERE key LIKE 'month:%'")}
            first=first_record(c)
        due=[]
        for back in range(KEEP):
            month=shift(last,-back)
            if first is None or shift(month,1).timestamp()<=first:break
            if month.strftime('%Y-%m') not in stored or reformat or (changed and back<2):due.append(month)
        for month in reversed(due):
            key=month.strftime('%Y-%m')
            settled=build(store,month,now,pricing,prices,ended,stored.get(key))
            with store.connect() as c:store.save_state(c,'month:'+key,settled)
    with store.connect() as c:
        if changed:store.save_state(c,'months:revision',revision)
        if reformat:store.save_state(c,'months:format',MONTH_FORMAT)
        for (old,) in c.execute("SELECT key FROM state WHERE key LIKE 'month:%' ORDER BY key DESC LIMIT -1 OFFSET ?",(KEEP,)).fetchall():
            c.execute('DELETE FROM state WHERE key=?',(old,))
    return [m.strftime('%Y-%m') for m in reversed(due)]


def recent(store,limit=KEEP):
    with store.connect() as c:
        return [store.state(c,r[0]) for r in c.execute("SELECT key FROM state WHERE key LIKE 'month:%' ORDER BY key DESC LIMIT ?",(limit,)).fetchall()]
