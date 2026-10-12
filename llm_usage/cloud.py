"""Claude Code cloud sessions (claude.ai/code, environment_kind anthropic_cloud).

Their transcripts live only on Anthropic's servers, so no local record file ever
sees them. The same read-only endpoints `claude --teleport` uses return the
transcript events; only the usage numbers of assistant messages are kept, the
conversation text is parsed in memory and dropped. Remote Control ("bridge")
sessions run on this PC and are already read from ~/.claude/projects.
"""
import json
import time
import urllib.parse
import urllib.request
from pathlib import Path

from .store import identity, project_label, stamp

API='https://api.anthropic.com'
SESSIONS_BETA='ccr-byoc-2025-07-29'
MAX_PAGES=100


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
    query='limit=100';found=[]
    for _ in range(MAX_PAGES):
        page=fetch(f'{API}/v1/sessions?{query}',{**headers,'anthropic-beta':SESSIONS_BETA})
        found+=page.get('data') or []
        if not page.get('has_more') or not page.get('last_id'):break
        query='limit=100&after_id='+urllib.parse.quote(page['last_id'])
    return [s for s in found if s.get('environment_kind')=='anthropic_cloud' and s.get('id')]


def _ingest(store,c,fetch,headers,item,model_provider):
    url=f"{API}/v1/code/sessions/{urllib.parse.quote(item['id'])}/teleport-events?limit=1000"
    cursor=None;project=_project(item);count=0
    for _ in range(MAX_PAGES):
        page=fetch(url+('&cursor='+urllib.parse.quote(cursor) if cursor else ''),headers)
        for entry in page.get('data') or []:
            d=(entry or {}).get('payload') or {}
            if d.get('type')!='assistant':continue
            m=d.get('message') or {};u=m.get('usage');ts=stamp(d.get('timestamp'))
            if not u or not m.get('id') or not ts:continue
            # Same identity as a local transcript line, so a teleported copy merges.
            store.event(c,identity('claude-code',m['id']),ts,model_provider(m.get('model','')),'claude-code',m.get('model'),dict(
                uncached_input=u.get('input_tokens',0),cached_input=u.get('cache_read_input_tokens',0),
                output=u.get('output_tokens',0),cache_creation=u.get('cache_creation_input_tokens',0)),
                session=d.get('sessionId') or item['id'],project=project,agent_kind='sub' if d.get('isSidechain') else 'main')
            count+=1
        cursor=page.get('next_cursor')
        if not cursor:break
    return count


def collect_claude_cloud(store,settings,fetch=None):
    from .collect import model_provider
    from .limits import claude_account,claude_headers
    fetch=fetch or _fetch
    path=Path(settings.get('claude_auth',Path.home()/'.claude/.credentials.json'))
    try:
        auth=json.loads(path.read_text()).get('claudeAiOauth',{})
        organization=claude_account(path,settings).get('organization')
        if not auth.get('accessToken') or not organization or (auth.get('expiresAt') and auth['expiresAt']/1000<=time.time()):
            raise PermissionError('native authentication required')
    except (OSError,ValueError,AttributeError,PermissionError):
        with store.connect() as c:
            store.source(c,'claude-cloud','error','Claude 인증 확인 필요 · Claude Code에서 로그인 갱신 후 자동 재확인합니다.')
        return
    headers={**claude_headers(auth['accessToken']),'anthropic-version':'2023-06-01','x-organization-uuid':organization}
    try:sessions=_sessions(fetch,headers)
    except Exception as exc:
        # Errors carry no response body or credentials.
        with store.connect() as c:store.source(c,'claude-cloud','error',f'클라우드 세션 목록 조회 실패 ({type(exc).__name__})')
        return
    failed=0
    for item in sessions:
        key='claude-cloud:'+item['id']
        with store.connect() as c:
            if store.state(c,key)==item.get('updated_at'):continue
        try:
            with store.connect() as c:
                _ingest(store,c,fetch,headers,item,model_provider)
                store.save_state(c,key,item.get('updated_at'))
        except Exception:failed+=1
    detail=f'클라우드 세션 {len(sessions)}개 확인 · 원격 기록 API(읽기 전용)'+(f' · 읽기 실패 {failed}개, 다음 수집에서 재시도' if failed else '')
    with store.connect() as c:store.source(c,'claude-cloud','partial' if failed else 'ok',detail)
