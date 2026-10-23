"""Claude Code cloud sessions (claude.ai/code, environment_kind anthropic_cloud).

Their transcripts live only on Anthropic's servers, so no local record file ever
sees them. The same read-only endpoints `claude --teleport` uses return the
transcript events; only the usage numbers of assistant messages are kept, the
conversation text is parsed in memory and dropped. Remote Control ("bridge")
sessions run on this PC and are already read from ~/.claude/projects.
"""
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from email.utils import parsedate_to_datetime
from pathlib import Path

from .store import identity, project_label, stamp

API='https://api.anthropic.com'
SESSIONS_BETA='ccr-byoc-2025-07-29'
MAX_PAGES=100
PARSER_VERSION=4


class IncompletePages(ValueError):
    def __init__(self,message,cursor=None):
        super().__init__(message)
        self.cursor=cursor


def _backoff(store,exc,pause,key='claude-cloud:retry'):
    if not isinstance(exc,urllib.error.HTTPError) or exc.code not in (401,403,429):return
    delay=min(3600,300*2**min(pause.get('failures',0),4))
    retry=exc.headers.get('Retry-After') if exc.headers else None
    if retry:
        try:delay=max(delay,int(retry) if retry.isdigit() else parsedate_to_datetime(retry).timestamp()-time.time())
        except (ValueError,TypeError,OverflowError):pass
    with store.connect() as c:store.save_state(c,key,dict(next_attempt=time.time()+delay,failures=pause.get('failures',0)+1))


def _fetch(url,headers):
    from .limits import NoRedirect
    request=urllib.request.Request(url,headers=headers)
    # Like the other account reads, the bearer token never follows a redirect.
    with urllib.request.build_opener(NoRedirect).open(request,timeout=30) as response:
        return json.load(response)


def _project(item):
    for source in (item.get('session_context') or {}).get('sources') or []:
        # The list API flattens the repository ({type, url}); session reads nest it.
        url=source.get('url') if source.get('type')=='git_repository' else (source.get('git_repository') or {}).get('url')
        if isinstance(url,str) and url:
            return project_label(urllib.parse.urlparse(url).path.rstrip('/').removesuffix('.git'))
    return None


def _sessions(fetch,headers):
    query='limit=100';found=[];seen=set()
    for _ in range(MAX_PAGES):
        page=fetch(f'{API}/v1/sessions?{query}',{**headers,'anthropic-beta':SESSIONS_BETA})
        found+=page.get('data') or []
        if not page.get('has_more'):break
        if not page.get('last_id'):raise IncompletePages('missing cursor')
        if page['last_id'] in seen:raise IncompletePages('repeated cursor')
        seen.add(page['last_id'])
        query='limit=100&after_id='+urllib.parse.quote(page['last_id'])
    else:raise IncompletePages('session page limit')
    return [s for s in found if s.get('environment_kind')=='anthropic_cloud' and s.get('id')]


def _pages(fetch,headers,item,endpoint,start_cursor=None,page_limit=None):
    url=f"{API}/v1/code/sessions/{urllib.parse.quote(item['id'])}/{endpoint}?limit="+('1000' if endpoint=='teleport-events' else '100&sort_order=desc')
    cursor=start_cursor;seen={cursor} if cursor else set()
    for _ in range(page_limit or MAX_PAGES):
        page=fetch(url+('&cursor='+urllib.parse.quote(cursor) if cursor else ''),headers)
        yield from page.get('data') or []
        cursor=page.get('next_cursor')
        if not cursor:return
        if cursor in seen:raise IncompletePages('repeated cursor')
        seen.add(cursor)
    raise IncompletePages('event page limit',cursor)


def _numeric_entries(entries):
    """Drop conversation content before staging a network response in memory."""
    for e in entries:
        d=e.get('payload') or {}
        if d.get('type') not in ('assistant','result'):continue
        payload={k:d[k] for k in ('type','sessionId','session_id','timestamp','isSidechain','parent_tool_use_id','modelUsage','requestId','request_id','uuid') if k in d}
        if d.get('message'):
            payload['message']={k:d['message'][k] for k in ('id','model','usage') if k in d['message']}
        yield dict(created_at=e.get('created_at'),payload=payload)


def _batch(fetch,headers,item,cursor=None,endpoint='events'):
    entries=[]
    try:entries.extend(_numeric_entries(_pages(fetch,headers,item,endpoint,cursor,20)))
    except IncompletePages as exc:
        if exc.cursor:return entries,exc.cursor
        raise
    return entries,None


def _ingest(store,c,fetch,headers,item,model_provider,endpoint='teleport-events',account_key=None,entries=None,account='claude-code'):
    from .reconciliation import aliases,link,snapshot,number
    project=_project(item);count=0
    entries=entries if entries is not None else list(_numeric_entries(_pages(fetch,headers,item,endpoint)))
    session_ids={d.get('sessionId') or d.get('session_id') for e in entries if (d:=e.get('payload') or {}).get('type')=='assistant'}-{None}
    for entry in entries:
        d=(entry or {}).get('payload') or {}
        if d.get('type')=='result' and d.get('modelUsage'):
            if d.get('parent_tool_use_id'):continue  # already included in the parent's modelUsage
            sid=d.get('session_id') or d.get('sessionId') or (next(iter(session_ids)) if len(session_ids)==1 else item['id'] if not session_ids else None)
            if sid is None:continue  # no provable scope to subtract the detailed records
            start=stamp(item.get('created_at'));end=stamp(entry.get('created_at') or d.get('timestamp'))
            if not start or not end:continue
            for model,u in d['modelUsage'].items():
                parts=dict(uncached_input=u.get('inputTokens'),cached_input=u.get('cacheReadInputTokens'),
                           cache_creation=u.get('cacheCreationInputTokens'),output=u.get('outputTokens'))
                if not all(number(v) for v in parts.values()):continue
                total_tokens=sum(parts.values())
                if number(u.get('thinkingTokens')):parts['reasoning']=u['thinkingTokens']
                snapshot(store,c,source='claude-cloud',scope_id='claude:'+sid,route='claude-code',
                         provider=model_provider(model),model=model,start=start,end=end+0.000001,
                         tokens=total_tokens,parts=parts,session=sid,project=project,
                         account_key=account_key,checked=time.time())
            continue
        if d.get('type')!='assistant':continue
        sid=d.get('sessionId') or d.get('session_id') or item['id']
        # Same identity as a local transcript line, so a teleported copy merges.
        from .claude_usage import ingest_message
        count+=ingest_message(store,c,'claude-code',d,session=sid,project=project,account=account,
                              source='claude-cloud:'+endpoint,finality='provisional' if endpoint=='events' else 'final')
    from .claude_usage import summary
    summary(store,c)
    return count


def _rekey(store,c):
    """Cloud totals once stored without the key of the login that read them (the teleport
    read did not pass it) take the key their session proves: that of its keyed totals, else
    of the one account its cloud-read records name. A copy of a keyed total goes, so one
    result counts once. Unproven ones stay with the original account, which read every
    cloud session before accounts were told apart."""
    rows=c.execute("SELECT * FROM usage_evidence WHERE source='claude-cloud' AND account_key IS NULL").fetchall()
    if not rows:return
    prefix='claude_cloud_account:';readers={}
    for name,data in c.execute('SELECT key,data FROM state WHERE substr(key,1,?)=?',(len(prefix),prefix)):
        readers.setdefault(json.loads(data),set()).add(name[len(prefix):])
    changed=False
    for r in rows:
        scope=(r['source'],r['scope_id'])
        if not c.execute('SELECT 1 FROM usage_evidence WHERE source=? AND scope_id=? AND model IS ? AND end=? AND account_key IS NOT NULL',
                         (*scope,r['model'],r['end'])).fetchone():
            keys={k for k, in c.execute('SELECT DISTINCT account_key FROM usage_evidence WHERE source=? AND scope_id=? AND account_key IS NOT NULL',scope)}
            if not keys:
                # A cloud copy keeps the account that read it; a local copy's is attributed.
                routes={a for a, in c.execute("""SELECT DISTINCT account FROM events WHERE id IN (SELECT event_id FROM usage_links WHERE scope_id=?)
                                                 AND id IN (SELECT event_id FROM usage_origins WHERE source LIKE 'claude-cloud:%')""",(r['scope_id'],))}
                keys=readers.get(routes.pop(),set()) if len(routes)==1 else set()
            if len(keys)!=1:continue
            key,=keys
            c.execute('INSERT INTO usage_evidence SELECT ?,source,scope_id,route,provider,model,session,project,?,start,end,checked,tokens,parts'
                      ' FROM usage_evidence WHERE id=?',(identity(*scope,r['model'],r['end'],key),key,r['id']))
        c.execute('DELETE FROM usage_evidence WHERE id=?',(r['id'],))
        changed=True
    if changed:
        store.bump_data_version(c)
        store.save_state(c,'usage_evidence_version',(store.state(c,'usage_evidence_version') or 0)+1)


def collect_claude_cloud(store,settings,fetch=None,account=None):
    """Cloud sessions of one Claude account (the original account without a context)."""
    from .claude_accounts import suffix
    from .collect import model_provider
    from .limits import claude_account,claude_headers,claude_target
    fetch=fetch or _fetch
    route,path,account_file=claude_target(settings,account);tag=suffix(route)
    source,supplement,retry='claude-cloud'+tag,'claude-cloud-supplement'+tag,'claude-cloud:retry'+tag
    try:
        auth=json.loads(path.read_text()).get('claudeAiOauth',{})
        organization=claude_account(path,settings,account_file).get('organization')
        if not auth.get('accessToken') or not organization or (auth.get('expiresAt') and auth['expiresAt']/1000<=time.time()):
            raise PermissionError('native authentication required')
    except (OSError,ValueError,AttributeError,PermissionError):
        with store.connect() as c:
            store.source(c,source,'error','Claude 인증 확인 필요 · Claude Code에서 로그인 갱신 후 자동 재확인합니다.')
        return
    headers={**claude_headers(auth['accessToken']),'anthropic-version':'2023-06-01','x-organization-uuid':organization}
    account_key=identity('claude-account',organization)
    with store.connect() as c:
        pause=store.state(c,retry,{})
        # Account-level cloud totals (usage_evidence) name their account route.
        store.save_state(c,'claude_cloud_account:'+account_key,route)
        _rekey(store,c)
    if time.time()<pause.get('next_attempt',0):return
    try:sessions=_sessions(fetch,headers)
    except Exception as exc:
        # Errors carry no response body or credentials.
        _backoff(store,exc,pause,retry)
        if isinstance(exc,urllib.error.HTTPError):exc.close()
        with store.connect() as c:store.source(c,source,'error',f'클라우드 세션 목록 조회 실패 ({type(exc).__name__})')
        return
    failed=missing=backfilling=unavailable=0;errors=[]
    for item in sessions:
        key='claude-cloud:'+item['id']
        with store.connect() as c:
            seen=store.state(c,key)
        # A cursor only proves that the same parser read the preceding pages.
        # On an upgrade, replay both streams so new fields are recovered there too.
        if not isinstance(seen,dict) or seen.get('parser')!=PARSER_VERSION:seen={}
        state=seen if isinstance(seen,dict) else {}
        supplement_retry=state.get('supplement_retry_at',state.get('checked',0)+86400)
        supplement_paused=bool(state.get('supplement_unavailable') and state.get('updated')==item.get('updated_at')
                               and state.get('parser')==PARSER_VERSION and time.time()<supplement_retry)
        supplement_due=state.get('supplement_unavailable') and not supplement_paused
        if isinstance(seen,dict) and not seen.get('cursor') and not seen.get('teleport_cursor') and seen.get('parser')==PARSER_VERSION and seen.get('updated')==item.get('updated_at') and time.time()-seen.get('checked',0)<86400:
            # Final snapshots can arrive after updated_at. Recheck recently active sessions.
            if seen.get('missing') or (not supplement_due and time.time()-(stamp(item.get('updated_at')) or 0)>900):
                if seen.get('missing'):missing+=1
                if supplement_paused:unavailable+=1
                continue
        try:
            endpoint='teleport-events'
            supplement_state=dict(supplement_unavailable=True,supplement_retry_at=supplement_retry) if supplement_paused else {}
            scan_updated=state.get('scan_updated',item.get('updated_at')) if state.get('cursor') or state.get('teleport_cursor') else item.get('updated_at')
            entries,teleport_cursor=_batch(fetch,headers,item,state.get('teleport_cursor'),'teleport-events')
            with store.connect() as c:
                _ingest(store,c,fetch,headers,item,model_provider,account_key=account_key,entries=entries,account=route)
                if teleport_cursor:
                    store.save_state(c,key,dict(updated=scan_updated,scan_updated=scan_updated,
                        teleport_cursor=teleport_cursor,cursor=state.get('cursor'),parser=PARSER_VERSION,checked=time.time(),**supplement_state))
            if teleport_cursor:
                if supplement_paused:unavailable+=1
                backfilling+=1
                continue
            if supplement_paused:
                unavailable+=1
                with store.connect() as c:
                    store.save_state(c,key,dict(updated=scan_updated,parser=PARSER_VERSION,checked=time.time(),**supplement_state))
                continue
            # Preserve the established transcript path even when the supplemental
            # endpoint fails. Only commit its checkpoint after a full page walk.
            endpoint='events'
            cursor=seen.get('cursor') if isinstance(seen,dict) else None
            entries,cursor=_batch(fetch,headers,item,cursor)
            with store.connect() as c:
                _ingest(store,c,fetch,headers,item,model_provider,'events',account_key,entries,account=route)
                store.save_state(c,key,dict(updated=scan_updated,scan_updated=scan_updated,cursor=cursor,parser=PARSER_VERSION,checked=time.time()))
            if cursor:backfilling+=1
        except urllib.error.HTTPError as exc:
            _backoff(store,exc,pause,retry)
            exc.close()
            if exc.code==404 and endpoint=='events':
                unavailable+=1
                with store.connect() as c:
                    store.save_state(c,key,dict(updated=scan_updated,supplement_unavailable=True,
                        supplement_retry_at=time.time()+86400,parser=PARSER_VERSION,checked=time.time()))
                continue
            if exc.code!=404:
                failed+=1
                if exc.code in (401,403,429):
                    break
                continue
            # Like `claude --teleport`: a session without a transcript is not retried until it changes.
            missing+=1
            with store.connect() as c:store.save_state(c,key,dict(updated=item.get('updated_at'),missing=True,parser=PARSER_VERSION,checked=time.time()))
        except Exception as exc:failed+=1;errors.append(type(exc).__name__)
    detail=(f'클라우드 세션 {len(sessions)}개 확인 · 원격 기록 API(읽기 전용)'+(f' · 기록 없음 {missing}개' if missing else '')
            +(f' · 읽기 실패 {failed}개, 다음 수집에서 재시도' if failed else '')
            +(f' · 과거 이벤트 이어 읽기 {backfilling}개' if backfilling else '')
            +(' · '+','.join(sorted(set(errors))) if errors else ''))
    with store.connect() as c:
        if not failed:store.save_state(c,retry,{})
        store.source(c,source,'partial' if failed or backfilling else 'ok',detail)
        store.source(c,supplement,
                     'unavailable' if not sessions or unavailable+missing==len(sessions) else 'partial' if unavailable or missing or failed or backfilling else 'ok',
                     f'추가 누계·상세 API · 미제공 {unavailable}세션 · 기존 transcript 수집과 별도'
                     +(' · 세션 변경 또는 24시간 뒤 재확인' if unavailable else ''))
