"""Official quota fields only; never infer reset from a subscription duration."""
import json
import math
import os
import re
import selectors
import socket
import subprocess
import time
import tomllib
import urllib.request
import urllib.error
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path

from .store import stamp,KST

OPENCODE_GO_USAGE_URL='https://opencode.ai/zen/go/v1/usage'


def number(value):
    return isinstance(value,(int,float)) and not isinstance(value,bool) and math.isfinite(value)


def codex_limits(store,c,data,checked,source='codex'):
    from .resources import observe_account, codex_resources, SKEW
    if not isinstance(data,dict):raise ValueError('한도 응답 형식')
    if not number(checked) or checked>time.time()+SKEW:return 0
    if source=='codex':
        ident=data.get('accountId')
        owner=observe_account(c,'codex',(ident,data.get('workspaceId')) if isinstance(ident,str) and ident else None,checked)
        if owner and owner['checked']>checked:return 0
        codex_resources(c,data,checked)
        groups_for_policy=data.get('rateLimitsByLimitId') or {'codex':data.get('rateLimits') or {}}
        store.save_state(c,'quota_policy:codex',dict(checked=checked,
            ordinary_allowed=data.get('ordinaryUsageAllowed') if isinstance(data.get('ordinaryUsageAllowed'),bool) else None,
            spending_blocked=any(row.get('spendControlReached') is True for row in groups_for_policy.values() if isinstance(row,dict))))
    groups = data.get('rateLimitsByLimitId')
    if not groups:
        row = data.get('rateLimits',data)
        groups = {row.get('limitId',row.get('limit_id','codex')):row}
    count=0
    for group,row in groups.items():
        if not isinstance(row,dict):continue
        for window in ('primary','secondary'):
            w=row.get(window)
            if not isinstance(w,dict): continue
            used=w.get('usedPercent',w.get('used_percent'))
            if not number(used):continue
            minutes=w.get('windowDurationMins',w.get('window_minutes'))
            label=f'{group} · {minutes}분' if minutes else f'{group} · {window}'
            title=row.get('limitName',row.get('limit_name')) or ('Codex 공통' if group=='codex' else group)
            duration={300:'5시간',10080:'주간'}.get(minutes,f'{minutes}분' if minutes else window)
            if store.limit(c,'codex',label,max(0,100-used),w.get('resetsAt',w.get('resets_at')),checked,source,
                           dict(role='common' if group=='codex' else 'model',group='common' if group=='codex' else str(group)[:100],
                                label='공통 한도' if group=='codex' else str(title)[:100])):
                store.save_state(c,'label:codex:'+label,str(title)[:100]+' · '+duration)
                count+=1
    return count


def status_limits(store,c,route,data,checked):
    count=0
    if route=='claude-code':
        for key,row in (data.get('rate_limits') or {}).items():
            if not isinstance(row,dict):continue
            used=row.get('used_percentage')
            if number(used):
                store.limit(c,route,key,max(0,100-used),row.get('resets_at'),checked,route)
                count+=1
    elif route=='antigravity':
        # Bucket IDs are the provider's shared quota identity, never copied per model.
        for key,row in (data.get('quota') or {}).items():
            if not isinstance(row,dict):continue
            remaining=row.get('remaining_fraction')
            if number(remaining):
                reset=row.get('reset_time')
                if reset is None and number(row.get('reset_in_seconds')):
                    reset=checked+row['reset_in_seconds']
                store.limit(c,route,key,max(0,min(100,remaining*100)),reset,checked,route)
                count+=1
    return count


def go_limits(store,c,data,checked):
    count=0
    for key,row in data.get('usage',{}).items():
        if key in ('rolling','weekly','monthly') and number(row.get('percent')):
            store.limit(c,'opencode-go',key,max(0,100-row['percent']),row.get('resetsAt'),checked,'opencode-go')
            count+=1
    return count


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,req,fp,code,msg,headers,newurl):
        return None


def claude_limits(store,c,data,checked):
    """Internal account usage response; utilization is already a percentage."""
    count=0
    for bucket in ('five_hour','seven_day','seven_day_opus','seven_day_sonnet','seven_day_oauth_apps','seven_day_cowork','seven_day_omelette','seven_day_overage_included'):
        row=data.get(bucket)
        if not isinstance(row,dict) or not number(row.get('utilization')):continue
        used=row['utilization']
        if not 0<=used<=100:continue
        reset=stamp(row.get('resets_at'))
        if reset is not None and (not number(reset) or reset<=0):continue
        role='common' if bucket in ('five_hour','seven_day') else 'model' if bucket in ('seven_day_opus','seven_day_sonnet') else 'unknown'
        group='common' if role=='common' else bucket.removeprefix('seven_day_') if role=='model' else bucket
        store.limit(c,'claude-code',bucket,100-used,reset,checked,'claude-oauth',
                    dict(role=role,group=group,label='공통 한도' if role=='common' else group.title(),locked=bool(row.get('locked_reason'))))
        count+=1
    from .resources import claude_spend
    claude_spend(c,data,checked)
    return count


DEVIN_CLI_VERSION = '3000.10.27'


def devin_ide(auth_path=None):
    """Report the installed CLI version when its install tree is visible."""
    version = DEVIN_CLI_VERSION
    if auth_path:
        def key(name):
            try:
                return tuple(int(part) for part in name.split('.'))
            except ValueError:
                return ()
        try:
            found = [key(p.name) for p in (Path(auth_path).parent / 'cli' / '_versions').iterdir()]
        except OSError:
            found = []
        found = [k for k in found if k]
        if found:
            version = '.'.join(str(part) for part in max(found))
    return {'ide_name': 'devin', 'ide_version': version,
            'extension_name': 'devin-cli', 'extension_version': version}


def devin_limits(store,c,data,checked):
    """GetUserStatus planStatus — only server-provided remaining-percent fields."""
    plan=(data.get('userStatus') or {}).get('planStatus') or {}
    count=0
    for bucket,remaining,resets in (('daily',plan.get('dailyQuotaRemainingPercent'),plan.get('dailyQuotaResetAtUnix')),
                                  ('weekly',plan.get('weeklyQuotaRemainingPercent'),plan.get('weeklyQuotaResetAtUnix'))):
        if not number(remaining):continue
        try:reset=int(resets) if resets is not None else None
        except (TypeError,ValueError):reset=None
        store.limit(c,'devin',bucket,max(0,min(100,float(remaining))),reset,checked,'devin')
        count+=1
    if count:
        info=plan.get('planInfo') or {}
        try:overage=int(plan.get('overageBalanceMicros') or 0)/1e6 or None
        except (TypeError,ValueError):overage=None
        store.save_state(c,'devin:plan',dict(plan_name=info.get('planName'),billing=info.get('billingStrategy'),
            plan_start=plan.get('planStart'),plan_end=plan.get('planEnd'),overage_usd=overage,
            acu_consumed=plan.get('acuConsumed'),acu_limit=plan.get('acuLimit'),
            prompt_credits=plan.get('availablePromptCredits'),flex_credits=plan.get('availableFlexCredits')))
    return count,plan


def account_poll(store,path,state_key,source,auth_hint,read,record,interval=300):
    """One account-quota read with a persistent cooldown shared by automatic and manual runs.

    read() returns the parsed response (raising PermissionError when the stored login
    cannot be used); record(c,data,checked) stores it and returns (count, detail).
    401/403 and missing credentials pause polling until the credential file changes;
    429 backs off 10/20/40/60 minutes, other failures 1/2/4/8/10 minutes, and a longer
    Retry-After wins."""
    now=time.time()
    try:signature=path.stat().st_mtime_ns
    except OSError:signature=None
    with store.connect() as c:
        state=store.state(c,state_key,{})
    if state.get('auth_blocked') and state.get('credential_mtime')==signature:return
    # A changed credential permits recovery from auth errors, but not from 429.
    if not state.get('auth_blocked') and now<state.get('next_attempt',0):return
    failures=state.get('failures',0)
    try:
        data=read()
        checked=time.time()
        with store.connect() as c:
            n,detail=record(c,data,checked)
            store.source(c,source,'ok' if n else 'unavailable',detail if n else '계정 응답에 해석 가능한 한도 필드가 없습니다.',checked)
            store.save_state(c,state_key,dict(next_attempt=checked+interval,failures=0,credential_mtime=signature))
    except Exception as exc:
        failures+=1;checked=time.time()
        code=exc.code if isinstance(exc,urllib.error.HTTPError) else None
        blocked=code in (401,403) or isinstance(exc,(PermissionError,FileNotFoundError))
        delay=min(3600,600*2**min(failures-1,3)) if code==429 else min(600,60*2**min(failures-1,4))
        if isinstance(exc,urllib.error.HTTPError):
            delay=max(delay,retry_delay(exc.headers.get('Retry-After'),checked))
            exc.close()
        if blocked:detail=auth_hint
        elif code==429:
            try:waiting=datetime.fromtimestamp(checked+delay,KST).strftime('%m/%d %H:%M KST')+' 이후 재시도'
            except (ValueError,OverflowError,OSError):waiting='제공사 지정 대기 시간 적용'
            detail='한도 조회 제한(429) · '+waiting
        else:detail=f'계정 한도 조회 실패 ({"HTTP "+str(code) if code else type(exc).__name__})'
        with store.connect() as c:
            store.source(c,source,'error',detail,checked)
            store.save_state(c,state_key,dict(next_attempt=checked+delay,failures=failures,
                auth_blocked=blocked,credential_mtime=signature))


def poll_devin(store,settings):
    """Devin account quota through the CLI's stored credentials. Internal, undocumented API."""
    path=Path(settings.get('devin_auth') or Path.home()/'.local/share/devin/credentials.toml')
    def read():
        creds=tomllib.loads(path.read_text())
        key=creds.get('windsurf_api_key');base=(creds.get('api_server_url') or '').rstrip('/')
        if not key or not base:raise PermissionError('native authentication required')
        body=json.dumps({'metadata':dict(api_key=key,**devin_ide(str(path)))}).encode()
        request=urllib.request.Request(base+'/exa.seat_management_pb.SeatManagementService/GetUserStatus',data=body,
            headers={'Content-Type':'application/json','Accept':'application/json','User-Agent':'llm-usage/0.1'})
        with urllib.request.build_opener(NoRedirect).open(request,timeout=20) as response:return json.load(response)
    def record(c,data,checked):
        n,plan=devin_limits(store,c,data,checked)
        name=((plan.get('planInfo') or {}).get('planName') or 'Devin')
        store.save_state(c,'label:devin:daily','일간 한도');store.save_state(c,'label:devin:weekly','주간 한도')
        return n,f'{name} · 계정 사용량 조회 · 내부 경로'
    account_poll(store,path,'devin_status_poll','devin','Devin 인증 확인 필요 · Devin CLI에서 로그인 갱신 후 자동 재확인합니다.',read,record)


def retry_delay(value,now):
    try:
        delay=float(value)
        return max(0,delay) if math.isfinite(delay) else 0
    except (TypeError,ValueError):
        try:return max(0,parsedate_to_datetime(value).timestamp()-now)
        except (TypeError,ValueError,OverflowError):return 0


def poll_claude(store,settings):
    """One account read, with persistent cooldown shared by automatic/manual runs."""
    path=Path(settings.get('claude_auth',Path.home()/'.claude/.credentials.json'))
    metadata={}
    def read():
        metadata.update(claude_account(path,settings))
        auth=json.loads(path.read_text()).get('claudeAiOauth',{})
        if not auth.get('accessToken') or (auth.get('expiresAt') and auth['expiresAt']/1000<=time.time()):
            raise PermissionError('native authentication required')
        request=urllib.request.Request('https://api.anthropic.com/api/oauth/usage',headers={
            'Authorization':'Bearer '+auth['accessToken'],'anthropic-beta':'oauth-2025-04-20',
            'Accept':'application/json','User-Agent':'llm-usage/0.1'})
        with urllib.request.build_opener(NoRedirect).open(request,timeout=20) as response:return json.load(response)
    def record(c,data,checked):
        from .resources import observe_account
        observe_account(c,'claude-code',metadata.get('identity'),checked,path.stat().st_mtime_ns)
        return claude_limits(store,c,data,checked),'계정 사용량 조회 · 내부 OAuth 경로'
    account_poll(store,path,'claude_oauth_poll','claude-oauth','Claude 인증 확인 필요 · Claude Code에서 로그인 갱신 후 자동 재확인합니다.',read,record)


def claude_account(path,settings):
    """Read only the native sign-in identity, never a token-derived identity."""
    native=Path(settings['claude_account_file']) if settings.get('claude_account_file') else path.parent.parent/'.claude.json' if path.parent.name=='.claude' else None
    try:
        data=(json.loads(native.read_text()).get('oauthAccount') or {}) if native else {}
        user,org=data.get('accountUuid'),data.get('organizationUuid')
        if not all(isinstance(v,str) and re.fullmatch('[A-Za-z0-9-]{1,100}',v) for v in (user,org)):
            return {}
        return dict(identity=(user,org),organization=org)
    except (OSError,ValueError,TypeError,AttributeError):return {}


def poll_claude_resources(store,settings):
    """Read-only paths in Claude Code 2.1.288, with independent cooldowns.

    Neither endpoint can redeem a reset or change billing. Identities and tokens
    stay in memory; account identity is hashed by the resource store.
    """
    from .resources import observe_account,claude_balance,claude_resets
    path=Path(settings.get('claude_auth',Path.home()/'.claude/.credentials.json'))
    for kind,source,parser in (('credits','claude-credits',claude_balance),('resets','claude-resets',claude_resets)):
        metadata={}
        def read():
            auth=json.loads(path.read_text()).get('claudeAiOauth',{})
            if not auth.get('accessToken') or (auth.get('expiresAt') and auth['expiresAt']/1000<=time.time()):
                raise PermissionError('native authentication required')
            metadata.update(claude_account(path,settings))
            if not metadata.get('identity'):raise PermissionError('native account identity required')
            endpoint=('https://api.anthropic.com/api/oauth/organizations/'+metadata['organization']+'/prepaid/credits' if kind=='credits'
                      else 'https://api.anthropic.com/api/oauth/usage?cedar_ember=1&skip_spend=1')
            request=urllib.request.Request(endpoint,headers={'Authorization':'Bearer '+auth['accessToken'],
                'anthropic-beta':'oauth-2025-04-20','Accept':'application/json','User-Agent':'llm-usage/0.2'})
            with urllib.request.build_opener(NoRedirect).open(request,timeout=10) as response:return json.load(response)
        def record(c,data,checked):
            observe_account(c,'claude-code',metadata.get('identity'),checked,path.stat().st_mtime_ns)
            return parser(c,data,checked),'추가 자원 읽기 전용 조회 · 내부 OAuth 경로'
        account_poll(store,path,source+'_poll',source,'Claude 기본 계정 정보·인증 확인 필요',read,record,interval=900)


def read_codex(binary,timeout=35):
    p=subprocess.Popen([binary,'app-server','--stdio'],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL)
    selector=selectors.DefaultSelector()
    selector.register(p.stdout,selectors.EVENT_READ)
    pending=b''
    def send(obj):
        p.stdin.write(json.dumps(obj).encode()+b'\n');p.stdin.flush()
    try:
        send(dict(id=1,method='initialize',params=dict(clientInfo=dict(name='llm_usage',version='0.1.0'))))
        deadline=time.monotonic()+timeout
        while time.monotonic()<deadline:
            if not selector.select(timeout=1): continue
            # Raw reads: a buffered readline() can keep complete lines in Python's
            # buffer, where select() never reports them again.
            chunk=os.read(p.stdout.fileno(),65536)
            if not chunk:raise RuntimeError('app-server 종료')
            pending+=chunk
            while b'\n' in pending:
                line,pending=pending.split(b'\n',1)
                if not line.strip():continue
                reply=json.loads(line)
                if reply.get('id')==1:
                    if 'error' in reply:raise RuntimeError('app-server 초기화 실패')
                    send(dict(method='initialized'))
                    send(dict(id=2,method='account/rateLimits/read'))
                elif reply.get('id')==2:
                    if 'error' in reply:raise RuntimeError('Codex 한도 조회 실패')
                    return reply['result']
        raise TimeoutError('Codex 한도 응답 시간 초과')
    finally:
        selector.close()
        p.terminate()
        try:p.wait(timeout=5)
        except subprocess.TimeoutExpired:p.kill();p.wait()
        p.stdin.close();p.stdout.close()


# A 5h window resets at most five hours after any observation; allow for clock skew.
AGY_5H_HORIZON=18000+600
# The same original reset jitters by seconds between status-line and app responses.
AGY_RESET_MATCH=300
AGY_SPLIT_BUCKET=re.compile(r'(gemini|3p)-(5h|weekly)-\d+')


def agy_app_limits(store,c,data,checked):
    """Desktop-app GetUserStatus: per-model quotaInfo, stored in the status-line buckets.

    Each model carries one quotaInfo (the window that currently binds) and no window
    length. A reset more than five hours away can only be the weekly window. A nearer
    reset is the weekly one only when it matches the family's known weekly reset;
    otherwise it is the 5h window. Models of a family that report the same
    (fraction, reset) are one shared quota; differing values are stored separately
    with numerical suffixes, never merged. Nothing is written for a bucket the
    response did not describe."""
    c.execute("DELETE FROM limits WHERE route='antigravity' AND bucket LIKE 'app-%'")
    # Bucket labels are derived from the bucket names in the UI.
    c.execute("DELETE FROM state WHERE key LIKE 'label:antigravity:%'")
    configs=(((data.get('userStatus') or {}).get('cascadeModelConfigData') or {}).get('clientModelConfigs')) or []
    weekly={r[0]:r[1] for r in c.execute("SELECT bucket,resets FROM limits WHERE route='antigravity' "
                                           "AND bucket IN ('gemini-weekly','3p-weekly') AND resets>?",(checked,))}
    groups={};skipped=0
    for cfg in configs:
        if not isinstance(cfg,dict):continue
        quota=cfg.get('quotaInfo')
        if not isinstance(quota,dict):continue
        model=str(cfg.get('modelId') or '')
        family='gemini' if model.lower().startswith('gemini') else '3p'
        fraction=quota.get('remainingFraction')
        # QuotaInfo.remaining_fraction is a plain proto3 float without presence in
        # language server 2.19.1, so protojson omits exactly 0.0. Only a quota that
        # still carries its reset is read as exhausted.
        if fraction is None and quota.get('resetTime'):fraction=0.0
        try:reset=stamp(quota.get('resetTime')) if quota.get('resetTime') else None
        except (TypeError,ValueError,AttributeError):reset=None
        if not number(fraction) or not 0<=fraction<=1 or not number(reset) or reset<=checked:
            skipped+=1;continue
        known=weekly.get(family+'-weekly')
        window='weekly' if reset-checked>AGY_5H_HORIZON or (known and abs(reset-known)<=AGY_RESET_MATCH) else '5h'
        groups.setdefault((family,window),{}).setdefault((fraction,reset),[]).append(model)
    written=set();split=False
    for (family,window),values in sorted(groups.items()):
        split=split or len(values)>1
        for n,((fraction,reset),models) in enumerate(sorted(values.items(),key=lambda kv:(-len(kv[1]),kv[0][0]))):
            bucket=f'{family}-{window}'+('' if n==0 else f'-{n+1}')
            store.limit(c,'antigravity',bucket,fraction*100,reset,checked,'antigravity-app')
            written.add(bucket)
    # Numbered buckets only describe one response's split; drop those it no longer has.
    for (bucket,) in c.execute("SELECT bucket FROM limits WHERE route='antigravity' AND source='antigravity-app'").fetchall():
        if AGY_SPLIT_BUCKET.fullmatch(bucket) and bucket not in written:
            c.execute("DELETE FROM limits WHERE route='antigravity' AND bucket=?",(bucket,))
    return len(written),skipped,split


# Last endpoint that answered GetUserStatus, per language-server pid.
_agy_endpoints={}
_LOOPBACK={'tcp':{'0100007F':'127.0.0.1','00000000':'127.0.0.1'},
           'tcp6':{'00000000000000000000000001000000':'::1','00000000000000000000000000000000':'::1'}}


def _listening_ports(pid):
    """Loopback-reachable LISTEN endpoints owned by pid, from /proc (no ss/netstat dependency)."""
    inodes=set()
    try:
        for fd in os.listdir(f'/proc/{pid}/fd'):
            try:target=os.readlink(f'/proc/{pid}/fd/{fd}')
            except OSError:continue
            if target.startswith('socket:['):inodes.add(target[8:-1])
    except OSError:return []
    endpoints=set()
    for table,hosts in _LOOPBACK.items():
        try:
            with open(f'/proc/{pid}/net/{table}') as f:
                for line in list(f)[1:]:
                    parts=line.split()
                    if len(parts)>9 and parts[3]=='0A' and parts[9] in inodes:
                        address,port=parts[1].split(':')
                        if address in hosts:endpoints.add((hosts[address],int(port,16)))
        except OSError:continue
    return sorted(endpoints)


def agy_app_servers():
    """Same-user Antigravity language servers. The CSRF token stays in memory only."""
    if not os.path.isdir('/proc'):return []
    found=[]
    for entry in os.listdir('/proc'):
        if not entry.isdigit():continue
        try:
            if os.stat(f'/proc/{entry}').st_uid!=os.getuid():continue
            with open(f'/proc/{entry}/cmdline','rb') as f:args=f.read().split(b'\0')
        except OSError:continue
        if not args or not args[0].endswith(b'/language_server') or b'antigravity' not in args[0]:continue
        token=None
        for i,arg in enumerate(args):
            if arg==b'--csrf_token' and i+1<len(args):token=args[i+1].decode(errors='replace')
            elif arg.startswith(b'--csrf_token='):token=arg.split(b'=',1)[1].decode(errors='replace')
        if token:found.append((int(entry),token))
    return found


def _tls_ready(host,port,context,timeout):
    try:
        with socket.create_connection((host,port),timeout=timeout) as raw,context.wrap_socket(raw,server_hostname=host):
            return True
    except OSError:return False


def _agy_post(servers,method,timeout=10,probe=2):
    import ssl
    # Loopback only, to a port owned by the server process; its certificate is self-signed.
    context=ssl.create_default_context()
    # Do not inherit a weaker protocol floor from the host OpenSSL configuration.
    context.minimum_version=ssl.TLSVersion.TLSv1_2
    context.check_hostname=False;context.verify_mode=ssl.CERT_NONE
    body=json.dumps({'metadata':{'ideName':'antigravity','extensionName':'antigravity','locale':'en'}}).encode()
    for pid in set(_agy_endpoints)-{pid for pid,_ in servers}:del _agy_endpoints[pid]
    last=None
    for pid,token in servers:
        endpoints=_listening_ports(pid);cached=_agy_endpoints.get(pid)
        if cached in endpoints:endpoints=[cached]+[e for e in endpoints if e!=cached]
        for host,port in endpoints:
            # Other ports of the server (LSP, extension host) may accept and never speak TLS;
            # a short handshake probe keeps them from holding the poll for the full timeout.
            if (host,port)!=cached and not _tls_ready(host,port,context,probe):
                last='TLS 미응답';continue
            request=urllib.request.Request(f'https://{"["+host+"]" if ":" in host else host}:{port}/exa.language_server_pb.LanguageServerService/{method}',
                data=body,headers={'Content-Type':'application/json','Connect-Protocol-Version':'1','X-Codeium-Csrf-Token':token})
            try:
                with urllib.request.build_opener(NoRedirect,urllib.request.HTTPSHandler(context=context)).open(request,timeout=timeout) as response:
                    data=json.load(response)
                _agy_endpoints[pid]=(host,port)
                return data
            except urllib.error.HTTPError as exc:last=f'HTTP {exc.code}';exc.close()
            except (OSError,ValueError) as exc:last=type(exc).__name__
    raise ConnectionError(last or 'no listening port')


def read_agy_app(servers,timeout=10,probe=2):
    return _agy_post(servers,'GetUserStatus',timeout,probe)


def read_agy_quota(servers,timeout=10,probe=2):
    return _agy_post(servers,'RetrieveUserQuotaSummary',timeout,probe)


AGY_QUOTA_BUCKET=re.compile(r'[a-z0-9][a-z0-9-]{0,63}')


def agy_quota_limits(store,c,data,checked):
    """Desktop-app RetrieveUserQuotaSummary: every bucket of every quota family.

    Each group carries all its windows with an explicit bucketId and window
    label, so no reset-horizon guessing is needed for these rows.
    remainingFraction is a plain proto3 float: an omitted value with a reset is
    a real 0 (exhausted). Buckets the response cannot describe are skipped."""
    c.execute("DELETE FROM limits WHERE route='antigravity' AND bucket LIKE 'app-%'")
    c.execute("DELETE FROM state WHERE key LIKE 'label:antigravity:%'")
    payload=data.get('response')
    if not isinstance(payload,dict):payload=data
    groups=payload.get('groups') if isinstance(payload,dict) else None
    written=set();skipped=0
    for group in groups or []:
        if not isinstance(group,dict):continue
        for row in group.get('buckets') or []:
            if not isinstance(row,dict):skipped+=1;continue
            bucket=row.get('bucketId');fraction=row.get('remainingFraction')
            if fraction is None and row.get('resetTime'):fraction=0.0
            try:reset=stamp(row.get('resetTime')) if row.get('resetTime') else None
            except (TypeError,ValueError,AttributeError):reset=None
            if not isinstance(bucket,str) or not AGY_QUOTA_BUCKET.fullmatch(bucket) \
                    or not number(fraction) or not 0<=fraction<=1 or not number(reset) or reset<=checked:
                skipped+=1;continue
            store.limit(c,'antigravity',bucket,fraction*100,reset,checked,'antigravity-app')
            written.add(bucket)
    # Numbered splits exist only when per-model reads disagree; the group-level
    # summary never produces them, so drop leftovers of an earlier read.
    for (bucket,) in c.execute("SELECT bucket FROM limits WHERE route='antigravity' AND source='antigravity-app'").fetchall():
        if AGY_SPLIT_BUCKET.fullmatch(bucket) and bucket not in written:
            c.execute("DELETE FROM limits WHERE route='antigravity' AND bucket=?",(bucket,))
    return len(written),skipped


def poll_antigravity_app(store,settings,servers=None,reader=None,quota_reader=None):
    """Quota as seen by the running desktop app; distinct from status-line receipt.

    RetrieveUserQuotaSummary is tried first because it describes every window of
    every quota family. GetUserStatus — one binding window per model — remains
    the fallback for servers that cannot answer it. Never raises: a failure here
    must not stop the other providers' polling."""
    if settings.get('antigravity_app_quota') is False:return
    checked=time.time()
    try:
        servers=agy_app_servers() if servers is None else servers
        if not servers:
            with store.connect() as c:
                if c.execute("SELECT 1 FROM sources WHERE name='antigravity-app'").fetchone():
                    store.source(c,'antigravity-app','unavailable','데스크톱 앱 language server 미실행 · 앱 실행 중에만 조회됩니다.',checked)
            return
        try:
            summary=(quota_reader or read_agy_quota)(servers)
        except ConnectionError:
            summary=None
        if summary is not None:
            with store.connect() as c:
                n,skipped=agy_quota_limits(store,c,summary,checked)
                if n:
                    notes=['WSL 데스크톱 앱 한도 요약 조회 · 내부 경로 · 응답의 창 레이블 사용']
                    if skipped:notes.append(f'잔여 비율·초기화 시각을 해석할 수 없는 버킷 {skipped}개(미반영)')
                    store.source(c,'antigravity-app','partial' if skipped else 'ok',' · '.join(notes),checked)
                    return
        data=(reader or read_agy_app)(servers)
        with store.connect() as c:
            n,skipped,split=agy_app_limits(store,c,data,checked)
            notes=['WSL 데스크톱 앱 조회 · 내부 경로 · 창 길이는 초기화 시각으로 판단']
            if split:notes.append('같은 계열의 서로 다른 한도를 따로 표시')
            if skipped:notes.append(f'잔여 비율·초기화 시각을 해석할 수 없는 모델 {skipped}개(미반영)')
            store.source(c,'antigravity-app','partial' if n and (split or skipped) else 'ok' if n else 'unavailable',
                         ' · '.join(notes) if n else '앱 응답에 해석 가능한 한도 필드가 없습니다.',checked)
    except Exception as exc:
        # Neither the CSRF token nor the response body is recorded.
        with store.connect() as c:
            store.source(c,'antigravity-app','error',f'데스크톱 앱 한도 조회 실패 ({exc if isinstance(exc,ConnectionError) else type(exc).__name__})',checked)


def poll_limits(store,settings,manual=False):
    poll_claude(store,settings)
    poll_claude_resources(store,settings)
    poll_devin(store,settings)
    poll_antigravity_app(store,settings)
    for route in ('codex','opencode-go'):
        if route=='opencode-go' and not manual:
            with store.connect() as c:
                pause=store.state(c,'opencode_go_poll',{})
            # After an entitlement error the subscription is gone for a while —
            # skip the authenticated request for six hours.
            if time.time()<pause.get('next_attempt',0):continue
        try:
            if route=='codex':
                if not settings.get('codex_binary'):raise FileNotFoundError('codex')
                data=read_codex(settings['codex_binary'])
            else:
                auth=json.loads(Path(settings['opencode_auth']).read_text()).get('opencode-go',{})
                if auth.get('type')!='api' or not auth.get('key'):raise ValueError('구독 인증 미제공')
                req=urllib.request.Request(OPENCODE_GO_USAGE_URL,headers={'Authorization':'Bearer '+auth['key'],'Accept':'application/json','User-Agent':'llm-usage/0.1'})
                # Like the Claude/Devin reads, credentials never follow a redirect.
                with urllib.request.build_opener(NoRedirect).open(req,timeout=20) as response:data=json.load(response)
            with store.connect() as c:
                n=(codex_limits if route=='codex' else go_limits)(store,c,data,time.time())
                store.source(c,route,'ok' if n else 'unavailable','' if n else '원본 한도 필드 미제공')
                if route=='opencode-go':store.save_state(c,'opencode_go_poll',{})
        except Exception as exc:
            # Errors contain no response body, endpoint credentials or command environment.
            detail=f'한도 수집 실패 ({type(exc).__name__})';status='error'
            if route=='codex' and isinstance(exc,FileNotFoundError):
                detail='Codex 실행 파일을 찾지 못했습니다 · 설정의 codex_binary 경로를 확인하세요.'
            if isinstance(exc,urllib.error.HTTPError):
                detail=f'한도 수집 실패 (HTTP {exc.code})'
                try:
                    kind=json.loads(exc.read(4096)).get('error',{}).get('type')
                    if kind=='EntitlementError':status='ended';detail='계정·워크스페이스에 OpenCode Go 구독이 없습니다.'
                    elif kind=='AuthError':detail='제공사 인증을 다시 확인해야 합니다.'
                except (ValueError,AttributeError):pass
                finally:exc.close()
            with store.connect() as c:
                store.source(c,route,status,detail)
                if route=='opencode-go' and status=='ended':
                    store.save_state(c,'opencode_go_poll',dict(next_attempt=time.time()+21600))
