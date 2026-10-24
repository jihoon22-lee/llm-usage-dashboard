"""Codex activity and quota consumption, deliberately outside the token ledger.

Only whitelisted identifiers, timestamps, classifications and numeric observations
survive parsing. Task transcripts, PR bodies and credentials are never persisted.
"""
import json
import math
import re
import time
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit

from .store import KST, identity

SOURCES=('codex-activity', 'codex-consumption-daily', 'codex-consumption-threads')
ID=re.compile(r'[A-Za-z0-9_.:-]{1,180}')
TASK_ID=re.compile(r'task_[A-Za-z0-9_-]{1,160}')
TURN_ID=re.compile(r'[A-Za-z0-9_.:~\-]{1,240}')
UUID=re.compile(r'[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}')
ENUM=re.compile(r'[A-Za-z0-9_.:-]{1,100}')
MODEL=re.compile(r'[A-Za-z0-9_./() :-]{1,100}')
PERCENT_FIELDS=('five_hour_limit_percent','weekly_limit_percent','balance_usage_credits')
KINDS=('cloud','code_review','security_review','unknown')
STATES=('completed','failed','cancelled','running','pending','unknown')


def initialize(c):
    c.executescript('''
      CREATE TABLE IF NOT EXISTS codex_activity_tasks (
        id TEXT PRIMARY KEY, account TEXT NOT NULL, task_id TEXT NOT NULL,
        kind TEXT NOT NULL, created REAL, updated REAL, status TEXT NOT NULL,
        checked REAL NOT NULL, prs TEXT NOT NULL, url TEXT,
        detail_checked REAL, detail_updated REAL, detail_status TEXT,
        detail_error TEXT, next_detail REAL NOT NULL DEFAULT 0);
      CREATE INDEX IF NOT EXISTS codex_activity_account ON codex_activity_tasks(account,created);
      CREATE TABLE IF NOT EXISTS codex_activity_runs (
        task_key TEXT NOT NULL, turn_id TEXT NOT NULL, thread_id TEXT NOT NULL,
        model TEXT, status TEXT NOT NULL, created REAL, parent_thread TEXT,
        checked REAL NOT NULL, PRIMARY KEY(task_key,turn_id,thread_id));
      CREATE INDEX IF NOT EXISTS codex_activity_thread ON codex_activity_runs(thread_id);
      CREATE TABLE IF NOT EXISTS codex_thread_consumption (
        account TEXT NOT NULL, thread_id TEXT NOT NULL, checked REAL NOT NULL,
        data_as_of REAL, status TEXT NOT NULL, payload TEXT NOT NULL,
        previous TEXT, retry_at REAL NOT NULL, PRIMARY KEY(account,thread_id));
      CREATE TABLE IF NOT EXISTS codex_daily_consumption (
        account TEXT NOT NULL, day TEXT NOT NULL, checked REAL NOT NULL,
        payload TEXT NOT NULL, PRIMARY KEY(account,day));
    ''')


def text(value, pattern=ENUM):
    return value if isinstance(value,str) and pattern.fullmatch(value) else None


def timestamp(value):
    if isinstance(value,str):
        try:value=datetime.fromisoformat(value.replace('Z','+00:00')).timestamp() if re.search(r'(Z|[+-]\d\d:\d\d)$',value) else None
        except (ValueError,OverflowError,OSError):return None
    return float(value) if type(value) in (float,int) and math.isfinite(value) and 0<value<253402300800 else None


def decimal(value):
    """Keep decimal strings exact, including an explicitly supplied zero."""
    if isinstance(value,bool) or not isinstance(value,(str,int,float)):return None
    if isinstance(value,str) and (len(value)>100 or not re.fullmatch(r'[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][+-]?\d+)?',value)):return None
    try:n=Decimal(str(value))
    except InvalidOperation:return None
    if not n.is_finite() or n<0 or n>Decimal('1e12') or abs(n.as_tuple().exponent)>100:return None
    return format(n,'f')


def execution_state(value):
    value={'in_progress':'running','queued':'pending','canceled':'cancelled'}.get(value,value)
    return value if value in STATES else 'unknown'


def _pr(raw):
    if not isinstance(raw,dict):return None
    raw=raw.get('pull_request',raw)
    if not isinstance(raw,dict) or not isinstance(raw.get('url'),str):return None
    try:u=urlsplit(raw['url'])
    except ValueError:return None
    m=re.fullmatch(r'/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/pull/([1-9][0-9]*)/?',u.path)
    if u.scheme!='https' or u.netloc!='github.com' or not m or u.query or u.fragment:return None
    return dict(repository=m[1],number=int(m[2]),url='https://github.com/'+m[1]+'/pull/'+m[2])


def task_metadata(raw,kind='unknown'):
    if not isinstance(raw,dict) or not text(raw.get('id'),TASK_ID):raise ValueError('invalid task identity')
    display=raw.get('task_status_display') or {}
    display=display if isinstance(display,dict) else {}
    latest=display.get('latest_turn_status_display') or {}
    latest=latest if isinstance(latest,dict) else {}
    intent=display.get('initial_intent') or latest.get('intent')
    if intent in ('cr','code_review'):kind='code_review'
    elif intent in ('security_review','security_cr'):kind='security_review'
    elif kind in ('all','archived'):kind='cloud'
    if kind not in KINDS:kind='unknown'
    prs={p['url']:p for x in raw.get('pull_requests',[]) if (p:=_pr(x))} if isinstance(raw.get('pull_requests'),list) else {}
    # A task URL is optional. Do not invent a link for an unsupported web route.
    url=raw.get('url');safe_url=None
    if isinstance(url,str) and len(url)<500:
        try:
            u=urlsplit(url)
            if u.scheme=='https' and u.netloc=='chatgpt.com' and not u.query and not u.fragment and re.fullmatch(r'/codex/(?:tasks|task)/[A-Za-z0-9_-]+',u.path):safe_url=url
        except ValueError:pass
    return dict(task_id=raw['id'],kind=kind,created=timestamp(raw.get('created_at')),
                updated=timestamp(raw.get('updated_at')),status=execution_state(latest.get('turn_status')),
                prs=list(prs.values()),url=safe_url)


def record_task(c,account,raw,checked,kind='unknown'):
    r=task_metadata(raw,kind);key=identity('codex-activity',account,r['task_id'])
    old=c.execute('SELECT * FROM codex_activity_tasks WHERE id=?',(key,)).fetchone()
    if old and ((old['updated'] or 0)>(r['updated'] or 0) or old['checked']>checked):return key
    # A sparse list must not erase an established PR or replace a review with a
    # generic archive membership. Actual new metadata takes precedence.
    if old:
        if not r['prs']:r['prs']=json.loads(old['prs'])
        if r['kind'] in ('cloud','unknown') and old['kind'] in ('code_review','security_review'):r['kind']=old['kind']
        r['created']=r['created'] if r['created'] is not None else old['created']
        r['url']=r['url'] or old['url']
    c.execute('''INSERT INTO codex_activity_tasks
      (id,account,task_id,kind,created,updated,status,checked,prs,url) VALUES (?,?,?,?,?,?,?,?,?,?)
      ON CONFLICT(id) DO UPDATE SET kind=excluded.kind,created=excluded.created,
      updated=excluded.updated,status=excluded.status,checked=excluded.checked,prs=excluded.prs,url=excluded.url''',
      (key,account,r['task_id'],r['kind'],r['created'],r['updated'],r['status'],checked,json.dumps(r['prs']),r['url']))
    if r['updated'] is not None:
        c.execute("UPDATE codex_activity_tasks SET next_detail=0 WHERE id=? AND ?>COALESCE(detail_updated,0) AND detail_status='ok'",(key,r['updated']))
    return key


def record_detail(c,task_key,raw,checked):
    if not isinstance(raw,dict) or not any(k in raw for k in ('current_assistant_turn','current_diff_task_turn')):raise ValueError('invalid task detail')
    task=c.execute('SELECT * FROM codex_activity_tasks WHERE id=?',(task_key,)).fetchone()
    if not task:raise ValueError('unknown task')
    if isinstance(raw.get('task'),dict):
        if raw['task'].get('id')!=task['task_id']:raise ValueError('task identity mismatch')
        version=timestamp(raw['task'].get('updated_at'))
        if version is not None and task['updated'] is not None and version<task['updated']:raise ValueError('outdated task detail')
        record_task(c,task['account'],raw['task'],checked,task['kind'])
    queue=[raw.get(k) for k in ('current_assistant_turn','current_diff_task_turn')]
    seen=set();count=0
    while queue:
        turn=queue.pop(0)
        if not isinstance(turn,dict):continue
        tid=text(turn.get('id'),TURN_ID)
        if not tid:continue
        # Both current_* slots can describe the same turn; union the identity
        # evidence, while the primary key makes their consumption count once.
        children=turn.get('child_turns')
        if isinstance(children,list):
            queue.extend(child for child in children if isinstance(child,dict) and text(child.get('id'),TURN_ID) not in seen|{tid})
        seen.add(tid)
        if len(seen)>200:raise ValueError('too many task turns')
        threads={}
        events=turn.get('thread_events') or {}
        events=events.get('events',[]) if isinstance(events,dict) else []
        if not isinstance(events,list):raise ValueError('invalid thread events')
        for event in events:
            if not isinstance(event,dict):continue
            p=event.get('params') or {}
            if not isinstance(p,dict):continue
            if event.get('method')=='thread/started' and isinstance(p.get('thread'),dict):
                t=p['thread'];sid=text(t.get('id'),UUID)
                if sid:threads[sid]=text(t.get('parentThreadId'),UUID) or text(t.get('forkedFromId'),UUID)
            sid=text(p.get('threadId'),UUID)
            if sid:threads.setdefault(sid,None)
        sid=text(turn.get('thread_id'),UUID)
        if sid:threads.setdefault(sid,None)
        worklog=turn.get('worklog') or {}
        messages=worklog.get('messages',[]) if isinstance(worklog,dict) else []
        for msg in messages if isinstance(messages,list) else []:
            meta=msg.get('metadata') if isinstance(msg,dict) else None
            sid=text(meta.get('thread_id'),UUID) if isinstance(meta,dict) else None
            if sid:threads.setdefault(sid,None)
        if threads:c.execute("DELETE FROM codex_activity_runs WHERE task_key=? AND turn_id=? AND thread_id=''",(task_key,tid))
        for sid,parent in (threads or {'':None}).items():
            c.execute('''INSERT INTO codex_activity_runs VALUES (?,?,?,?,?,?,?,?)
              ON CONFLICT(task_key,turn_id,thread_id) DO UPDATE SET
              model=COALESCE(excluded.model,codex_activity_runs.model),status=excluded.status,
              created=COALESCE(excluded.created,codex_activity_runs.created),
              parent_thread=COALESCE(excluded.parent_thread,codex_activity_runs.parent_thread),checked=excluded.checked''',
              (task_key,tid,sid,text(turn.get('model_version'),MODEL),execution_state(turn.get('turn_status')),
               timestamp(turn.get('created_at')),parent,checked))
            count+=1
    if not seen and any(raw.get(k) is not None for k in ('current_assistant_turn','current_diff_task_turn')):raise ValueError('missing turn identity')
    c.execute('''UPDATE codex_activity_tasks SET detail_checked=?,detail_updated=updated,
      detail_status='ok',detail_error=NULL,next_detail=? WHERE id=?''',(checked,checked+3600,task_key))
    return count


def consumption_values(raw):
    values={k:decimal(raw.get(k)) for k in PERCENT_FIELDS}
    return dict(**values,missing_fields=[k for k in PERCENT_FIELDS if k not in raw],
                invalid_fields=[k for k in PERCENT_FIELDS if raw.get(k) is not None and values[k] is None])


def has_values(payload):
    return any(payload.get(k) is not None for k in PERCENT_FIELDS) or any(has_values(g) for g in payload.get('groups',[]))


def record_consumption(c,account,raw,requested,checked):
    if not isinstance(raw,dict) or not isinstance(raw.get('threads'),list):raise ValueError('invalid thread usage')
    requested=set(requested);found=set();as_of=timestamp(raw.get('data_as_of'))
    for item in raw['threads']:
        if not isinstance(item,dict) or item.get('thread_id') not in requested:raise ValueError('unexpected thread identity')
        sid=item['thread_id']
        if sid in found:raise ValueError('duplicate thread observation')
        found.add(sid)
        state=text(item.get('data_status')) or 'unknown'
        groups=item.get('groups',[])
        if not isinstance(groups,list):raise ValueError('invalid model groups')
        clean=[]
        for g in groups:
            if not isinstance(g,dict):raise ValueError('invalid model group')
            clean.append(dict(model=text(g.get('model'),MODEL),reasoning_effort=text(g.get('reasoning_effort')),product_experience=text(g.get('product_experience')),
                              speed=text(g.get('speed')),**consumption_values(g)))
        values=consumption_values(item)
        payload=dict(**values,groups=clean,data_status=state,
                     usage_source=text(item.get('usage_source')),comparison_plan=text(raw.get('comparison_plan')),
                     data_as_of=as_of,checked=checked)
        old=c.execute('SELECT * FROM codex_thread_consumption WHERE account=? AND thread_id=?',(account,sid)).fetchone()
        if old and (old['checked']>checked or (as_of and old['data_as_of'] and as_of<old['data_as_of'])):continue
        previous=None
        if old:
            prior=json.loads(old['payload'])
            previous=json.dumps(prior) if has_values(prior) else old['previous']
        effective='partial' if values['invalid_fields'] or any(g['invalid_fields'] for g in clean) else state
        c.execute('''INSERT INTO codex_thread_consumption VALUES (?,?,?,?,?,?,?,?)
          ON CONFLICT(account,thread_id) DO UPDATE SET checked=excluded.checked,data_as_of=excluded.data_as_of,
          status=excluded.status,payload=excluded.payload,previous=excluded.previous,retry_at=excluded.retry_at''',
          (account,sid,checked,as_of,effective,json.dumps(payload),previous,checked+(3600 if has_values(payload) else 21600)))
    # An omitted requested row is not a successful zero or a fresh unavailable row.
    return len(found),len(requested-found)


def _sum(rows):
    values=[Decimal(r['value']) for r in rows if r.get('value') is not None]
    return format(sum(values,Decimal(0)),'f') if values else None


def record_daily(c,account,raw,checked):
    if not isinstance(raw,dict) or raw.get('units')!='percent' or raw.get('group_by')!='day':raise ValueError('unsupported consumption units')
    if not isinstance(raw.get('data'),list):raise ValueError('invalid daily data')
    seen=set();gaps=0;invalid=0
    for item in raw['data']:
        if not isinstance(item,dict):raise ValueError('invalid day')
        day=item.get('date')
        if not isinstance(day,str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}',day):raise ValueError('invalid day')
        date.fromisoformat(day)
        if day in seen:raise ValueError('duplicate day')
        seen.add(day);attribution=[];models=[];surfaces=[];bad=0
        for key in ('attribution','models'):
            if key in item and not isinstance(item[key],list):raise ValueError('invalid breakdown')
        for a in item.get('attribution',[]):
            if not isinstance(a,dict):bad+=1;continue
            value=decimal(a.get('value'))
            if value is None:bad+=1;continue
            attribution.append({**{k:text(a.get(k),MODEL if k=='model' else ENUM) or 'unknown' for k in ('thread_source','turn_trigger','model','surface')},'value':value})
        for m in item.get('models',[]):
            if not isinstance(m,dict):bad+=1;continue
            value=decimal(m.get('credits'))  # The envelope, not this field name, defines the unit.
            if value is None:bad+=1;continue
            models.append(dict(model=text(m.get('model'),MODEL) or 'unknown',speed=text(m.get('speed')) or 'unknown',value=value))
        surface=item.get('product_surface_usage_values',{})
        if not isinstance(surface,dict):raise ValueError('invalid surfaces')
        for name,v in surface.items():
            value=decimal(v)
            if value is None or not text(name):bad+=1;continue
            surfaces.append(dict(surface=name,value=value))
        totals={k:_sum(v) for k,v in (('attribution',attribution),('models',models),('surfaces',surfaces))}
        values=[Decimal(v) for v in totals.values() if v is not None]
        mismatch=len(values)>1 and max(values)-min(values)>max(Decimal('0.000001'),max(values)*Decimal('0.000001'))
        gaps+=int(mismatch);invalid+=bad
        payload=dict(day=day,units='percent',attribution=attribution,models=models,surfaces=surfaces,
                     totals=totals,mismatch=mismatch,invalid_rows=bad,checked=checked)
        c.execute('''INSERT INTO codex_daily_consumption VALUES (?,?,?,?)
          ON CONFLICT(account,day) DO UPDATE SET checked=excluded.checked,payload=excluded.payload
          WHERE excluded.checked>=codex_daily_consumption.checked''',(account,day,checked,json.dumps(payload)))
    return len(seen),gaps,invalid


def plan_metadata(raw,checked):
    if not isinstance(raw,dict):raise ValueError('invalid plan history')
    periods=raw.get('periods')
    if not isinstance(periods,list):raise ValueError('invalid plan periods')
    return dict(checked=checked,data_as_of=timestamp(raw.get('data_as_of')),coverage_start=timestamp(raw.get('coverage_start')),
                coverage_complete=raw.get('coverage_complete') if type(raw.get('coverage_complete')) is bool else None,
                approximate=raw.get('approximate') if type(raw.get('approximate')) is bool else None,
                boundary_tolerance_seconds=raw.get('boundary_tolerance_seconds') if type(raw.get('boundary_tolerance_seconds')) is int else None,
                periods=[dict(starts_at=timestamp(p.get('starts_at')),ends_at=timestamp(p.get('ends_at')),
                              plan_type=text(p.get('plan_type')),window_minutes=p.get('window_minutes') if type(p.get('window_minutes')) is int else None,
                              accounting_complete=p.get('accounting_complete') if type(p.get('accounting_complete')) is bool else None)
                         for p in periods if isinstance(p,dict)])


def source_info(store,c,now):
    result=[]
    for name in SOURCES:
        row=c.execute('SELECT * FROM sources WHERE name=?',(name,)).fetchone()
        data=dict(row) if row else dict(name=name,status='unavailable',checked=None,detail='아직 수집하지 않았습니다.')
        stale=7200 if name=='codex-consumption-daily' else 1800
        data['stale']=bool(data['checked'] and now-data['checked']>stale)
        if data['stale'] and data['status']=='ok':data['status']='stale'
        result.append(data)
    return result


def _context(store,c,now):
    active=store.state(c,'codex_activity:account',{}) or {}
    account=active.get('key')
    scans={f:store.state(c,'codex_activity:scan:'+str(account)+':'+f,{}) for f in ('all','archived','code_review','security_review')}
    return dict(account_key=account,account_status=active.get('status','unavailable'),sources=source_info(store,c,now),
                coverage={f:{**{k:s.get(k) for k in ('complete','checked','completed_at','oldest','pages','error')},'continuing':bool(s.get('cursor'))} for f,s in scans.items()},now=now)


def _threads(c,account,ids,now):
    result=[]
    for sid in sorted(set(ids)):
        row=c.execute('SELECT * FROM codex_thread_consumption WHERE account=? AND thread_id=?',(account,sid)).fetchone()
        item=dict(thread_id=sid,status='pending',checked=None,payload=None,previous=None,stale=False)
        if row:
            item.update(status=row['status'],checked=row['checked'],payload=json.loads(row['payload']),
                        previous=json.loads(row['previous']) if row['previous'] else None,
                        stale=now-row['checked']>86400 or bool(row['data_as_of'] and now-row['data_as_of']>86400))
        result.append(item)
    return result


def _task(c,row,now,detail=False):
    r={k:row[k] for k in ('id','task_id','kind','created','updated','status','checked','url','detail_status','detail_error','detail_checked')}
    r['prs']=json.loads(row['prs'])
    runs=[dict(x) for x in c.execute('SELECT turn_id,thread_id,model,status,created,parent_thread FROM codex_activity_runs WHERE task_key=? ORDER BY created,turn_id,thread_id',(row['id'],))]
    threads=_threads(c,row['account'],[x['thread_id'] for x in runs if x['thread_id']],now)
    r['models']=sorted({x['model'] for x in runs if x['model']})
    r['run_count']=len({x['turn_id'] for x in runs});r['thread_count']=len(threads)
    states={x['status'] for x in threads}
    r['consumption_status']=('detail_error' if row['detail_status']=='error' else 'pending' if row['detail_status']!='ok' else 'unlinked') if not threads else next(iter(states)) if len(states)==1 else 'mixed'
    r['consumption']=threads[0] if len(threads)==1 else None
    if detail:r.update(runs=runs,threads=threads)
    return r


def activity(store,period='7d',start=None,end=None,kind='',state='',search='',page=1,now=None):
    now=time.time() if now is None else now
    if period not in ('7d','30d','all','custom','unknown') or (kind and kind not in KINDS) or (state and state not in STATES):raise ValueError('invalid activity filter')
    if not isinstance(search,str) or len(search)>100:raise ValueError('invalid search')
    page=int(page)
    if page<1 or page>100000:raise ValueError('invalid page')
    today=datetime.fromtimestamp(now,KST).date();low=high=None
    if period in ('7d','30d'):low=today-timedelta(days=int(period[:-1])-1);high=today+timedelta(days=1)
    elif period=='custom':
        low=date.fromisoformat(start);high=date.fromisoformat(end)+timedelta(days=1)
        if low>=high:raise ValueError('invalid dates')
    with store.connect() as c:
        context=_context(store,c,now);clauses=['account=?'];params=[context['account_key'] or '']
        if low:
            clauses+=['created>=?','created<?'];params += [datetime.combine(d,datetime.min.time(),KST).timestamp() for d in (low,high)]
        if period=='unknown':clauses+=['created IS NULL']
        if kind:clauses+=['kind=?'];params+=[kind]
        if state:clauses+=['status=?'];params+=[state]
        if search:
            pattern='%'+search.replace('\\','\\\\').replace('%','\\%').replace('_','\\_')+'%'
            clauses+=["(prs LIKE ? ESCAPE '\\' OR task_id LIKE ? ESCAPE '\\')"];params += [pattern,pattern]
        where=' AND '.join(clauses)
        total=c.execute('SELECT COUNT(*) FROM codex_activity_tasks WHERE '+where,params).fetchone()[0]
        prs={p['url'] for row in c.execute('SELECT prs FROM codex_activity_tasks WHERE '+where,params) for p in json.loads(row[0])}
        rows=c.execute('SELECT * FROM codex_activity_tasks WHERE '+where+' ORDER BY created DESC,id LIMIT 20 OFFSET ?',(*params,(page-1)*20)).fetchall()
        unknown=c.execute('SELECT COUNT(*) FROM codex_activity_tasks WHERE account=? AND created IS NULL',(context['account_key'] or '',)).fetchone()[0]
        return dict(**context,items=[_task(c,r,now) for r in rows],total=total,pr_count=len(prs),page=page,page_size=20,
                    unknown_dates=unknown,period=period,start=low.isoformat() if low else None,
                    end_exclusive=high.isoformat() if high else None,timezone='Asia/Seoul')


def task_detail(store,key,now=None):
    if not isinstance(key,str) or not re.fullmatch(r'[0-9a-f]{64}',key):raise ValueError('invalid task key')
    now=time.time() if now is None else now
    with store.connect() as c:
        context=_context(store,c,now)
        row=c.execute('SELECT * FROM codex_activity_tasks WHERE id=? AND account=?',(key,context['account_key'])).fetchone()
        if not row:raise KeyError(key)
        return dict(**context,task=_task(c,row,now,True))


def consumption(store,days=7,now=None):
    days=int(days)
    if days not in (7,30):raise ValueError('invalid consumption period')
    now=time.time() if now is None else now
    # These are provider date labels. Do not reinterpret midnight as KST.
    high=datetime.fromtimestamp(now,timezone.utc).date();low=high-timedelta(days=days-1)
    with store.connect() as c:
        context=_context(store,c,now);account=context['account_key']
        rows=[json.loads(r[0]) for r in c.execute('SELECT payload FROM codex_daily_consumption WHERE account=? AND day>=? AND day<=? ORDER BY day',(account,low.isoformat(),high.isoformat()))]
        plan=store.state(c,'codex_activity:plan:'+str(account),{})
        return dict(**context,days=rows,requested_days=days,units='percent',date_basis='provider',
                    start=low.isoformat(),end=high.isoformat(),missing_days=days-len(rows),plan=plan)
