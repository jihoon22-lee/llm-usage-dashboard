"""Interval-aware usage evidence. Provider aggregates never become fake requests.

Only an interval wholly inside a selected range contributes to its total. It
enters a chart bucket only when the whole interval fits there as well.
"""
import copy
import json
import math
from datetime import datetime, timezone

from .store import TOKENS, KST, identity, stamp

PARTS = TOKENS[:4]
# The Codex account residual (account day total minus this PC's records) has no model,
# project, session or token parts. Under model and project grouping it gets this key,
# which the dashboard shows under the user's name for it, instead of 'unknown'/'미분류';
# every total still includes it. The '/' keeps it apart from every project name (a
# directory basename, see store.project_label).
RESIDUAL = '/account-residual'
# Its name until the user sets one in the subscription settings (e.g. '자동 리뷰').
RESIDUAL_LABEL = '로컬 미기록'


def initialize(c):
    c.executescript('''
        CREATE TABLE IF NOT EXISTS usage_evidence (
          id TEXT PRIMARY KEY, source TEXT NOT NULL, scope_id TEXT NOT NULL,
          route TEXT NOT NULL, provider TEXT NOT NULL, model TEXT, session TEXT,
          project TEXT, account_key TEXT, start REAL NOT NULL, end REAL NOT NULL,
          checked REAL NOT NULL, tokens INTEGER NOT NULL, parts TEXT);
        CREATE INDEX IF NOT EXISTS usage_evidence_scope ON usage_evidence(source,scope_id,model,end);
        CREATE TABLE IF NOT EXISTS usage_links (
          scope_id TEXT NOT NULL, event_id TEXT NOT NULL,
          PRIMARY KEY(scope_id,event_id));
        CREATE TABLE IF NOT EXISTS usage_origins (
          event_id TEXT NOT NULL, source TEXT NOT NULL, quality TEXT NOT NULL,
          PRIMARY KEY(event_id,source));
        CREATE TABLE IF NOT EXISTS usage_aliases (
          route TEXT, alias TEXT, event_id TEXT, PRIMARY KEY(route,alias));
        CREATE TABLE IF NOT EXISTS telemetry_pending (
          id TEXT PRIMARY KEY, route TEXT, ts REAL, session TEXT, model TEXT,
          alias TEXT, parts TEXT);
    ''')


def number(value):
    return type(value) is int and 0 <= value <= 2**63-1


def snapshot(store, c, *, source, scope_id, route, provider, model, start, end,
             tokens, parts=None, session=None, project=None, account_key=None, checked=None):
    if not number(tokens) or not all(isinstance(v,(float,int)) and math.isfinite(v) for v in (start,end)) or end <= start:
        raise ValueError('invalid usage interval')
    if parts is not None:
        if not all(number(parts.get(k)) for k in PARTS):raise ValueError('incomplete token breakdown')
        if sum(parts[k] for k in PARTS) != tokens:raise ValueError('token breakdown mismatch')
        parts={k:parts[k] for k in TOKENS if k in parts and number(parts[k])}
    key=identity(source,scope_id,model,end,account_key)
    values=(key,source,scope_id,route,provider,model,session,project,account_key,start,end,checked or end,tokens,
            json.dumps(parts,sort_keys=True) if parts is not None else None)
    previous=c.execute('SELECT * FROM usage_evidence WHERE id=?',(key,)).fetchone()
    if previous is not None and previous['tokens']==tokens and previous['parts']==values[-1]:
        # Preserve the earliest receipt proving this unchanged count. Merely
        # polling after KST midnight must not erase an already known day bound.
        values=values[:11]+(min(previous['checked'],values[11]),)+values[12:]
    c.execute('INSERT OR REPLACE INTO usage_evidence VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',values)
    if previous is None or tuple(previous)[:11]+tuple(previous)[12:] != values[:11]+values[12:]:
        store.bump_data_version(c)
        store.save_state(c,'usage_evidence_version',(store.state(c,'usage_evidence_version') or 0)+1)


def link(c, scope, event_id, source, quality='final'):
    c.execute('INSERT OR IGNORE INTO usage_links VALUES (?,?)',(scope,event_id))
    c.execute('INSERT OR REPLACE INTO usage_origins VALUES (?,?,?)',(event_id,source,quality))


def aliases(c, route, event_id, *values):
    for v in values:
        if isinstance(v,str) and 0<len(v)<=200:
            c.execute('INSERT OR IGNORE INTO usage_aliases VALUES (?,?,?)',(route,identity(v),event_id))


def _raw(c, row, begin, end):
    if row['source']=='codex-account':
        # Historical local files have no account identity: this comparison is
        # deliberately labelled account attribution unverified in the response.
        where='route=? AND ts>=? AND ts<?';args=(row['route'],begin,end)
    else:
        where='id IN (SELECT event_id FROM usage_links WHERE scope_id=?) AND ts>=? AND ts<?'
        args=(row['scope_id'],begin,end)
    if row['model']:
        where+=' AND model=?';args+= (row['model'],)
    return dict(c.execute('SELECT '+','.join('COALESCE(SUM('+k+'),0) AS '+k for k in TOKENS)+
                          ' FROM events WHERE '+where,args).fetchone())


def _evidence(c):
    """Aggregates to reconcile: other sources as stored; Codex account days of the current
    account, the latest revision of each (UTC) day, plus older account buckets."""
    rows=[dict(r) for r in c.execute('SELECT * FROM usage_evidence ORDER BY source,scope_id,model,end')]
    from .store import Store
    namespace=Store.account_namespace(c,'codex')
    rows=[r for r in rows if r['source']!='codex-account' or (r['account_key'] or '')==namespace]
    daily={}
    for r in rows:
        if r['source']=='codex-account':
            key=(r['route'],r['start'])
            if key not in daily or (r['checked'],bool(r['account_key']))>(daily[key]['checked'],bool(daily[key]['account_key'])):daily[key]=r
    rows=[r for r in rows if r['source']!='codex-account']+list(daily.values())
    # Upgrade old account buckets without rewriting or attributing their history.
    existing={datetime.fromtimestamp(r['start'],timezone.utc).strftime('%Y-%m-%d') for r in rows if r['source']=='codex-account'}
    for r in c.execute('SELECT * FROM account_token_buckets WHERE route=? AND namespace=?',('codex',namespace)):
        scope='codex-day:'+r['day']
        if r['day'] in existing:continue
        start=stamp(r['day']+'T00:00:00Z')
        rows.append(dict(id=scope,source='codex-account',scope_id=scope,route=r['route'],provider='OpenAI',
                         model=None,session=None,project=None,account_key=namespace,start=start,end=start+86400,
                         checked=r['checked'],tokens=r['tokens'],parts=None))
    return rows


def account_days(c):
    """The Codex account's day totals (09:00–09:00 KST days), as reconciled."""
    return [r for r in _evidence(c) if r['source']=='codex-account']


def supplements(c):
    rows=_evidence(c)
    groups={}
    for r in rows:groups.setdefault((r['source'],r['scope_id'],r['model'],r['account_key']),[]).append(r)
    out=[];conflicts=[]
    for group in groups.values():
        previous=None
        for r in sorted(group,key=lambda x:x['end']):
            running=False
            if r['source']=='codex-account':
                # A received running-day count cannot contain future activity.
                # This is an upper bound, not a claim that work happened at poll
                # time. Revisions still replace the SAME original daily bucket.
                running=r['checked']<r['end']
                r={**r,'end':min(r['end'],r['checked'])}
                if r['end']<=r['start']:continue
            parts=json.loads(r['parts']) if r['parts'] else None
            begin=previous['end'] if previous else r['start']
            amount=r['tokens']-(previous['tokens'] if previous else 0)
            old=json.loads(previous['parts']) if previous and previous['parts'] else None
            delta={k:parts[k]-(old[k] if old else 0) for k in PARTS} if parts else None
            if amount<0 or (delta and any(v<0 for v in delta.values())):
                conflicts.append(dict(source=r['source'],scope_id=r['scope_id'],route=r['route'],provider=r['provider'],model=r['model'],project=r['project'],start=begin,end=r['end'],reason='counter_decreased',checked=r['checked']))
                previous=r;continue
            raw=_raw(c,r,begin,r['end'])
            missing=amount-sum(raw[k] for k in PARTS)
            detail={k:delta[k]-raw[k] for k in PARTS} if delta else None
            if missing<0 or (detail and any(v<0 for v in detail.values())):
                conflicts.append(dict(source=r['source'],scope_id=r['scope_id'],route=r['route'],provider=r['provider'],model=r['model'],project=r['project'],start=begin,end=r['end'],reason='records_exceed_aggregate',
                                      excess=max(0,-missing),checked=r['checked']))
                previous=r;continue
            if detail is not None and parts is not None and 'reasoning' in parts:
                reasoning=parts['reasoning']-(old.get('reasoning',0) if old else 0)-raw['reasoning']
                if reasoning>=0:detail['reasoning']=reasoning
            if missing or (detail and detail.get('reasoning')):
                out.append(dict(source=r['source'],scope_id=r['scope_id'],route=r['route'],provider=r['provider'],
                                model=r['model'],session=r['session'],project=r['project'],start=begin,end=r['end'],
                                tokens=missing,parts=detail,checked=r['checked'],account_verified=bool(r['account_key']) and not r['account_key'].startswith('epoch:'),
                                account=_account(c,r),running=running))
            previous=r
    # Thread/telemetry detail for Codex must not be added on top of its account
    # residual; account totals cover the same route. Keep the narrower evidence
    # as diagnostics until account-bound overlap is provable.
    account_days=[x for x in out if x['source']=='codex-account']
    out=[x for x in out if x['route']!='codex' or x['source']=='codex-account' or
         not any(a['start']<x['end'] and x['start']<a['end'] for a in account_days)]
    return out,conflicts


def _account(c,row):
    """Subscription account of an aggregate: the Claude account whose login read it."""
    if row['route']!='claude-code':return row['route']
    found=c.execute('SELECT data FROM state WHERE key=?',('claude_cloud_account:'+str(row['account_key']),)).fetchone() if row['account_key'] else None
    return json.loads(found[0]) if found else 'claude-code'


def matches(row,scope):
    if not scope:return True
    kind,_,value=scope.partition(':')
    # The Codex account residual is no model and no project: not 'unknown' or '미분류'.
    if row.get('source')=='codex-account' and kind in ('model','project'):return False
    return (row.get(kind) or ('unknown' if kind=='model' else '미분류' if kind=='project' else None))==value


def total(row):
    return row.get('total_tokens',sum(row.get(k,0) or 0 for k in PARTS))


def add(row, evidence):
    before=total(row)
    row.setdefault('recorded_tokens',before)
    row['supplemental_tokens']=row.get('supplemental_tokens',0)+evidence['tokens']
    row['total_tokens']=before+evidence['tokens']
    row['unpriced']=row.get('unpriced',0)+evidence['tokens']
    if evidence['parts']:
        for k in TOKENS:row[k]=row.get(k,0)+evidence['parts'].get(k,0)
    else:row['unclassified_tokens']=row.get('unclassified_tokens',0)+evidence['tokens']
    if evidence['source']=='codex-account':row['residual_tokens']=row.get('residual_tokens',0)+evidence['tokens']


def residual(e):
    return e['source']=='codex-account'


def series_label(e, group):
    if residual(e) and group in ('model','project'):return RESIDUAL
    if group=='agent':return '미귀속'
    return e.get(group) or ('unknown' if group=='model' else '미분류' if group=='project' else '미귀속')


def apply(c, body, group='route', cumulative=False, scope=None, *, calendar_start):
    """Overlay a COPY of the cached request-only response; never mutate the cache."""
    data=copy.deepcopy(body)
    entries,conflicts=supplements(c)
    # Where each aggregate source's evidence begins (any scope): a comparison needs it
    # to reach back as far as the local records.
    first_seen={}
    for e in entries:first_seen[e['source']]=min(first_seen.get(e['source'],math.inf),e['start'])
    entries=[e for e in entries if matches(e,scope)]
    a,b=stamp(data['start']),stamp(data['end_exclusive'])
    conflicts=[e for e in conflicts if matches(e,scope) and e['start']<b and a<e['end']]
    if not entries and not conflicts:return data
    accepted=[];unallocated=[];overlapping=[]
    for e in entries:
        if e['end']<=a or e['start']>=b:continue
        public={**e,'start':datetime.fromtimestamp(e['start'],KST).isoformat(),
                'end_exclusive':datetime.fromtimestamp(e['end'],KST).isoformat()}
        public.pop('end');public.pop('parts')
        # A subscription whose period usage is only supplemented (a Codex account residual,
        # Claude cloud sessions) was still used; an aggregate that crosses the period edge
        # leaves 'no usage' unknown (None) rather than 0.
        sub=next((s for s in data.get('subscriptions') or [] if s['route']==(e.get('account') or e['route'])),None)
        if not (a<=e['start'] and e['end']<=b):
            overlapping.append(public)
            if sub is not None and not sub.get('period_tokens'):sub['period_tokens']=None
            continue
        if sub is not None and sub.get('period_tokens') is not None:sub['period_tokens']+=e['tokens']
        accepted.append(e);add(data['totals'],e)
        label=series_label(e,group)
        located=False
        for p in data['series']:
            lo=stamp(p.get('range_start',p['time']));hi=stamp(p.get('range_end_exclusive',data['end_exclusive']))
            if lo<=e['start'] and e['end']<=hi:
                located=True
                add(p['values'].setdefault(label,dict.fromkeys((*TOKENS,'requests','cost','unpriced'),0)),e)
                if label not in data['labels']:data['labels'].append(label)
                if cumulative:
                    for later in data['series']:
                        if stamp(later['time'])>stamp(p['time']):
                            add(later['values'].setdefault(label,dict.fromkeys((*TOKENS,'requests','cost','unpriced'),0)),e)
                break
        if not located:unallocated.append(public)
        model=RESIDUAL if residual(e) else e['model'] or 'unknown'
        row=next((r for r in data['rows'] if r['route']==e['route'] and r['model']==model),None)
        if row is None:
            row=dict(provider=e['provider'],route=e['route'],model=model,requests=0,
                     est_cost=None,**dict.fromkeys(TOKENS,0));data['rows'].append(row)
        add(row,e)
        project=RESIDUAL if residual(e) else e['project'] or '미분류'
        for collection,key,value in [('sessions','session',e['session']),('projects','project',project)]:
            if key=='session' and not value:continue
            target=next((r for r in data.get(collection,[]) if r.get(key)==value),None)
            if target is None and collection in data:
                target={key:value,'route':e['route'],'project':project,'kind':'unattributed',
                        'requests':0,'sessions':0,'cost':None,'first_ts':e['start'],'last_ts':e['end'],**dict.fromkeys(TOKENS,0)}
                data[collection].append(target)
                if collection=='sessions':data['sessions_total']=data.get('sessions_total',0)+1
            if target is not None:add(target,e)
        for kind in ('route','provider','model','project','account'):
            # The residual is not an unknown model or an unassigned project to filter by.
            if residual(e) and kind in ('model','project'):continue
            value=e.get(kind) or ('unknown' if kind=='model' else '미분류' if kind=='project' else None)
            if kind=='account' and value and not value.startswith('claude-code'):continue
            options=data.get('scope_options',{}).get(kind)
            if options is not None and value and value not in options:options.append(value)
    # The card comparison: on both sides, aggregates that ended by the same elapsed time.
    # A running account day is in the period total but not set against a whole one.
    prior=data.get('prior')
    if prior:
        pa,pb,cut=stamp(prior['start']),stamp(prior['end_exclusive']),stamp(prior['current_end'])
        counted=set()
        for e in entries:
            if e.get('running'):continue
            if pa<=e['start'] and e['end']<=pb:add(prior,e);counted.add(e['source'])
            if a<=e['start'] and e['end']<=cut:prior['current_tokens']+=e['tokens'];counted.add(e['source'])
        # A source counted on either side must already have covered the earlier window;
        # otherwise its share is missing there, not zero.
        if any(first_seen[source]>pa for source in counted):prior['complete']=False
    for e in entries:add(data['lifetime'],e)
    for e in entries:
        begin=datetime.fromtimestamp(e['start'],KST)
        finish=datetime.fromtimestamp(e['end']-0.000001,KST)
        if begin.date()==finish.date() and 'calendar' in data and begin.date().isoformat()>=calendar_start:
            day=begin.date().isoformat()
            cell=next((x for x in data['calendar'] if x['day']==day),None)
            if cell is None:
                cell=dict(day=day,tokens=0,requests=0,cost=None);data['calendar'].append(cell)
            cell['tokens']+=e['tokens']
        if e in accepted and begin.date()==finish.date() and begin.hour==finish.hour:
            dow=(begin.weekday()+1)%7
            cell=next((x for x in data.get('heatmap',[]) if x['dow']==dow and x['hour']==begin.hour),None)
            if cell is None:
                cell=dict(dow=dow,hour=begin.hour,tokens=0,requests=0,cost=None);data.setdefault('heatmap',[]).append(cell)
            cell['tokens']+=e['tokens']
    if 'calendar' in data:data['calendar'].sort(key=lambda x:x['day'])
    for name in ('totals','lifetime'):
        value=data[name];value.setdefault('total_tokens',total(value));value.setdefault('recorded_tokens',value['total_tokens'])
        value.setdefault('supplemental_tokens',0);value.setdefault('unclassified_tokens',0);value.setdefault('residual_tokens',0)
    data['reconciliation']=dict(recorded_tokens=data['totals']['recorded_tokens'],
        supplemental_tokens=sum(e['tokens'] for e in accepted),residual_tokens=sum(e['tokens'] for e in accepted if residual(e)),
        unallocated_tokens=sum(e['tokens'] for e in unallocated),unallocated_intervals=unallocated,
        overlapping_intervals=overlapping,conflicts=conflicts,
        cost_basis='세부 요청 기록 중 단가가 확인된 부분',requests_basis='수집한 고유 요청',
        sources=sorted({e['source'] for e in accepted}))
    where='e.ts>=? AND e.ts<?';args=[a,b]
    if scope:
        kind,_,value=scope.partition(':')
        if kind in ('route','provider','model','project','account'):
            column=("COALESCE(e.project,'미분류')" if kind=='project' else
                    "CASE WHEN e.route='claude-code' THEN COALESCE(e.account,'claude-code') ELSE e.route END" if kind=='account' else 'e.'+kind)
            where+=' AND '+column+'=?';args.append(value)
    origins=c.execute("SELECT CASE WHEN EXISTS(SELECT 1 FROM usage_origins o WHERE o.event_id=e.id AND o.source LIKE 'claude-cloud:%') THEN 'cloud' "
                      "WHEN EXISTS(SELECT 1 FROM usage_origins o WHERE o.event_id=e.id AND o.source='telemetry') THEN 'telemetry' ELSE 'local' END origin,"
                      'SUM(e.uncached_input+e.cached_input+e.output+e.cache_creation) tokens FROM events e WHERE '+where+' GROUP BY 1',args)
    data['reconciliation']['origin_tokens']={r['origin']:r['tokens'] for r in origins}
    comparison=data.get('compare')
    if comparison:
        ca,cb=stamp(comparison['start']),stamp(comparison['end_exclusive'])
        comparison['unallocated_tokens']=0
        shift=a-ca
        for e in entries:
            elapsed=comparison.get('elapsed')
            if elapsed and stamp(elapsed['start'])<=e['start'] and e['end']<=stamp(elapsed['end_exclusive']):add(elapsed,e)
            if not (ca<=e['start'] and e['end']<=cb):continue
            placed=False
            label=series_label(e,group)
            for p,grid in zip(comparison.get('series',[]),data['series']):
                lo=stamp(grid.get('range_start',grid['time']))-shift
                hi=stamp(grid.get('range_end_exclusive',data['end_exclusive']))-shift
                if lo<=e['start'] and e['end']<=hi:
                    add(p['values'].setdefault(label,dict.fromkeys((*TOKENS,'requests','cost','unpriced'),0)),e)
                    if label not in data['labels']:data['labels'].append(label)
                    placed=True;break
            if not placed:comparison['unallocated_tokens']+=e['tokens']
    for key,which in [('coverage','totals'),('lifetime_coverage','lifetime')]:
        if data.get('cost',{}).get(key) is not None:
            r=data[which];data['cost'][key]*=r['recorded_tokens']/r['total_tokens'] if r['total_tokens'] else 1
    return data
