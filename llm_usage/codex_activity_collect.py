"""Bounded, resumable reads of the Codex client's existing account endpoints."""
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import codex_activity as data
from .config import ended_routes
from .store import identity

BASE='https://chatgpt.com/backend-api/wham/'
FILTERS=('all','archived','code_review','security_review')
MAX_RESPONSE=16*1024*1024


class BudgetExhausted(Exception):
    pass


class Client:
    def __init__(self,headers,budget=30):
        from .limits import NoRedirect
        self.headers=headers;self.deadline=time.monotonic()+budget
        self.opener=urllib.request.build_opener(NoRedirect)

    def __call__(self,path,body=None):
        remaining=self.deadline-time.monotonic()
        if remaining<1:raise BudgetExhausted()
        if not path.startswith(('tasks/','usage/')) or '://' in path or path.startswith('/'):raise ValueError('invalid read path')
        request=urllib.request.Request(BASE+path,headers={**self.headers,'Accept':'application/json','Content-Type':'application/json'},
                                       data=json.dumps(body).encode() if body is not None else None)
        with self.opener.open(request,timeout=min(10,remaining)) as response:
            chunks=[];size=0
            while True:
                if time.monotonic()>=self.deadline:raise BudgetExhausted()
                chunk=response.read1(min(65536,MAX_RESPONSE+1-size))
                if not chunk:break
                chunks.append(chunk);size+=len(chunk)
                if size>MAX_RESPONSE:raise ValueError('response too large')
        return json.loads(b''.join(chunks))


def authentication(settings):
    path=Path(settings.get('codex_auth') or Path(os.environ.get('CODEX_HOME') or Path.home()/'.codex')/'auth.json')
    raw=json.loads(path.read_text());tokens=raw.get('tokens') or {}
    if not isinstance(tokens,dict):raise PermissionError('native login required')
    account=data.text(tokens.get('account_id'),data.ID);token=tokens.get('access_token')
    if not account or not isinstance(token,str) or not token or len(token)>20000:raise PermissionError('native login required')
    return identity('codex-activity-account',account),{'Authorization':'Bearer '+token,'ChatGPT-Account-Id':account},path.stat().st_mtime_ns


def _failure(exc):
    code=exc.code if isinstance(exc,urllib.error.HTTPError) else None
    # Exception strings, URLs and server bodies can contain private data.
    return code,'HTTP '+str(code) if code else type(exc).__name__


def _allowed(store,key,signature,now):
    with store.connect() as c:state=store.state(c,key,{}) or {}
    if state.get('rate_limited') and now<state.get('next_attempt',0):return False
    if state.get('blocked') and state.get('credential_mtime')==signature:return False
    return state.get('credential_mtime')!=signature or now>=state.get('next_attempt',0)


def _failed(store,key,signature,exc,now):
    from .limits import retry_delay
    code,reason=_failure(exc)
    with store.connect() as c:
        old=store.state(c,key,{}) or {};failures=min(10,old.get('failures',0)+1)
        delay=min(3600,300*2**min(failures-1,4))
        if isinstance(exc,urllib.error.HTTPError):
            delay=max(delay,retry_delay((exc.headers or {}).get('Retry-After'),now));exc.close()
        store.save_state(c,key,dict(next_attempt=now+delay,blocked=code in (401,403),rate_limited=code==429,credential_mtime=signature,failures=failures))
    return reason


def _scan(store,account,fetch,kind,page_limit,now):
    key='codex_activity:scan:'+account+':'+kind
    with store.connect() as c:state=store.state(c,key,{}) or {}
    # Always check the head, then resume the deep scan. No ordering assumption
    # permits stopping at an old item, and a failed page never advances the cursor.
    cursor=None;resume=state.get('cursor');seen=set();count=0
    for index in range(page_limit):
        query=urllib.parse.urlencode(dict(limit=20,task_filter=kind,**({'cursor':cursor} if cursor else {})))
        raw=fetch('tasks/list?'+query)
        if not isinstance(raw,dict) or not isinstance(raw.get('items'),list):raise ValueError('invalid task list')
        next_cursor=raw.get('cursor')
        if next_cursor is not None and (not isinstance(next_cursor,str) or not next_cursor or len(next_cursor)>4096):raise ValueError('invalid cursor')
        if next_cursor and (next_cursor==cursor or next_cursor in seen):raise ValueError('repeated cursor')
        oldest=state.get('oldest')
        with store.connect() as c:
            for item in raw['items']:
                data.record_task(c,account,item,now,kind);created=data.timestamp(item.get('created_at'))
                if created:oldest=min(oldest,created) if oldest else created
            # A head read cannot prove the saved deep cursor has been exhausted.
            following=resume if index==0 and resume else next_cursor
            complete=state.get('complete',False) or not following
            state.update(cursor=following,complete=complete,checked=now,oldest=oldest,
                         pages=state.get('pages',0)+1,error=None)
            store.save_state(c,key,state)
        count+=len(raw['items'])
        if next_cursor:seen.add(next_cursor)
        cursor=following
        if not cursor:break
    return count


def _details(store,account,fetch,limit,now):
    with store.connect() as c:
        rows=c.execute('''SELECT * FROM codex_activity_tasks WHERE account=? AND
          (detail_checked IS NULL OR COALESCE(updated,0)>COALESCE(detail_updated,0) OR detail_status='error')
          AND next_detail<=? ORDER BY detail_checked IS NOT NULL,created DESC LIMIT ?''',(account,now,limit)).fetchall()
    for row in rows:
        try:
            raw=fetch('tasks/'+urllib.parse.quote(row['task_id'],safe=''))
            with store.connect() as c:data.record_detail(c,row['id'],raw,now)
        except BudgetExhausted:raise
        except Exception as exc:
            code,reason=_failure(exc)
            if isinstance(exc,urllib.error.HTTPError):exc.close()
            with store.connect() as c:c.execute('''UPDATE codex_activity_tasks SET detail_status='error',
                detail_error=?,next_detail=? WHERE id=?''',(reason,now+300,row['id']))
            if code in (401,403,429):raise


def _activity_summary(store,account,now,budget=False):
    with store.connect() as c:
        total,linked,errors=c.execute('''SELECT COUNT(*),COALESCE(SUM(detail_status='ok'),0),
          COALESCE(SUM(detail_status='error'),0) FROM codex_activity_tasks WHERE account=?''',(account,)).fetchone()
        scans=[store.state(c,'codex_activity:scan:'+account+':'+f,{}) or {} for f in FILTERS]
        complete=sum(s.get('complete') is True for s in scans)
        issue=any(s.get('error') for s in scans)
        partial=budget or complete<len(FILTERS) or linked<total or issue
        store.source(c,'codex-activity','partial' if partial else 'ok',
                     f'작업 {total}개 · 상세 확인 {linked}개 · 상세 실패 {errors}개 · 목록 {complete}/{len(FILTERS)} 경로 끝까지 확인'
                     +(' · 남은 이력은 다음 수집에서 계속합니다.' if partial else ''),now)


def _thread_summary(store,account,now):
    with store.connect() as c:
        ids=[r[0] for r in c.execute('''SELECT DISTINCT r.thread_id FROM codex_activity_runs r
          JOIN codex_activity_tasks t ON t.id=r.task_key WHERE t.account=? AND r.thread_id!='' ''',(account,))]
        rows=data._threads(c,account,ids,now)
        provided=sum(bool(r['payload'] and data.has_values(r['payload'])) for r in rows)
        unavailable=sum(r['status']=='unavailable' for r in rows);pending=sum(r['status']=='pending' for r in rows)
        complete=bool(rows) and all(r['status']=='complete' and r['payload'] and data.has_values(r['payload']) and not r['stale'] for r in rows)
        store.source(c,'codex-consumption-threads','ok' if complete else 'partial' if provided or pending else 'unavailable',
                     f'소비 값 수신 {provided}개 스레드 · 미제공 {unavailable}개 · 조회 대기 {pending}개 · 원본 토큰 아님',now)


def _threads(store,account,fetch,now):
    with store.connect() as c:
        ids=[r[0] for r in c.execute('''SELECT DISTINCT r.thread_id FROM codex_activity_runs r
          JOIN codex_activity_tasks t ON t.id=r.task_key
          LEFT JOIN codex_thread_consumption u ON u.account=t.account AND u.thread_id=r.thread_id
          WHERE t.account=? AND r.thread_id!='' AND (u.thread_id IS NULL OR u.retry_at<=?)
          ORDER BY u.checked IS NOT NULL,u.checked,t.created DESC LIMIT 40''',(account,now))]
    if ids:
        raw=fetch('usage/thread_usage/query_v2',dict(threads=[dict(thread_id=i) for i in ids]))
        with store.connect() as c:
            _,missing=data.record_consumption(c,account,raw,ids,now)
        if missing:raise ValueError('requested thread rows omitted')
    _thread_summary(store,account,now)
    return bool(ids)


def _daily(store,account,fetch,now):
    day=datetime.fromtimestamp(now,timezone.utc).date()
    query=urllib.parse.urlencode(dict(start_date=(day-timedelta(days=29)).isoformat(),end_date=day.isoformat(),group_by='day'))
    raw=fetch('usage/daily-token-usage-breakdown?'+query)
    with store.connect() as c:count,gaps,invalid=data.record_daily(c,account,raw,now)
    try:
        plan=data.plan_metadata(fetch('usage/plan_limit_history?days=30'),now)
    except BudgetExhausted:raise
    except Exception as exc:
        _,reason=_failure(exc)
        if isinstance(exc,urllib.error.HTTPError):exc.close()
        with store.connect() as c:
            old=store.state(c,'codex_activity:plan:'+account,{}) or {}
            store.save_state(c,'codex_activity:plan:'+account,{**old,'error':reason,'last_attempt':now})
            store.source(c,'codex-consumption-daily','partial',f'일별 {count}일 수신 · 플랜 이력 조회 실패 ({reason})',now)
        return
    partial=invalid or gaps or count<30 or plan.get('coverage_complete') is not True or plan.get('approximate') is not False or any(p['accounting_complete'] is not True for p in plan['periods'])
    with store.connect() as c:
        store.save_state(c,'codex_activity:plan:'+account,plan)
        store.source(c,'codex-consumption-daily','partial' if partial else 'ok',
                     f'한도 소비율(%) {count}일 · 분해 합계 차이 {gaps}일 · 해석 불가 {invalid}행'
                     +(' · 제공 범위·근사 표시를 확인하세요.' if partial else ''),now)


def collect(store,settings,fetch=None,now=None,page_limit=3,detail_limit=12):
    now=time.time() if now is None else now
    if not settings.get('codex_binary'):return
    if 'codex' in ended_routes(settings) or settings.get('codex_activity_enabled') is False:
        with store.connect() as c:
            for source in data.SOURCES:store.source(c,source,'ended' if 'codex' in ended_routes(settings) else 'idle','Codex 추가 수집이 중지됐습니다.',now)
        return
    try:account,headers,signature=authentication(settings)
    except Exception as exc:
        with store.connect() as c:
            store.save_state(c,'codex_activity:account',dict(key=None,status='unavailable',checked=now))
            for source in data.SOURCES:store.source(c,source,'unavailable','Codex 기존 CLI 로그인 확인 필요 · 계정 토큰 수집과 별도 경로',now)
        return
    with store.connect() as c:
        old=store.state(c,'codex_activity:account',{}) or {}
        store.save_state(c,'codex_activity:account',dict(key=account,status='active',checked=now))
        if old.get('key')!=account:
            for source in data.SOURCES:
                c.execute('DELETE FROM source_health WHERE name=?',(source,))
                store.source(c,source,'unavailable','현재 계정의 첫 수집 대기',now)
    fetch=fetch or Client(headers)
    # Independent cooldowns prevent a rejected optional source blocking the rest.
    for name,interval,action in (
        ('daily',3600,lambda:_daily(store,account,fetch,now)),
        ('threads',300,lambda:_threads(store,account,fetch,now)),
        ('list',300,lambda:_lists_and_details(store,account,fetch,signature,now,page_limit,detail_limit))):
        key='codex_activity:poll:'+account+':'+name
        if not _allowed(store,key,signature,now):continue
        try:read=action()
        except BudgetExhausted:
            if name=='list':_activity_summary(store,account,now,True)
            break
        except Exception as exc:
            reason=_failed(store,key,signature,exc,now)
            with store.connect() as c:
                source={'daily':'codex-consumption-daily','list':'codex-activity','threads':'codex-consumption-threads'}[name]
                store.source(c,source,'error','Codex 추가 조회 실패 ('+reason+') · 이전 수신값 유지',now)
        else:
            with store.connect() as c:store.save_state(c,key,dict(next_attempt=now+(interval if read is not False else 0),credential_mtime=signature,failures=0))


def _lists_and_details(store,account,fetch,signature,now,page_limit,detail_limit):
    for kind in FILTERS:
        gate='codex_activity:filter:'+account+':'+kind
        if not _allowed(store,gate,signature,now):continue
        try:_scan(store,account,fetch,kind,page_limit,now)
        except BudgetExhausted:raise
        except Exception as exc:
            reset=isinstance(exc,ValueError) or isinstance(exc,urllib.error.HTTPError) and exc.code==400
            reason=_failed(store,gate,signature,exc,now)
            key='codex_activity:scan:'+account+':'+kind
            with store.connect() as c:
                old=store.state(c,key,{}) or {}
                # A rejected or invalid cursor is retried from the head after the
                # cooldown, retaining already received tasks and completeness caveats.
                store.save_state(c,key,{**old,**({'cursor':None} if reset else {}),'error':reason})
        else:
            with store.connect() as c:store.save_state(c,gate,dict(next_attempt=now+300,credential_mtime=signature,failures=0))
    _details(store,account,fetch,detail_limit,now)
    _activity_summary(store,account,now)
