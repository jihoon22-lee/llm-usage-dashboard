"""Notifications sent by the collector, so they arrive while no page is open.

Channels are configured in local.json under "notify" (ntfy topic URL, a
Discord/Slack-compatible webhook URL, or a Telegram bot token and chat). Their
values are secrets: they are never written to the statistics DB, logs or API
responses. Only transitions are sent (a quota becoming low, not every poll).
"""
import ipaddress
import json
import re
import time
import urllib.error
import urllib.request
from datetime import datetime
from urllib.parse import urlsplit

from .store import KST

CHANNEL_KEYS=('ntfy_url','webhook_url','telegram_token','telegram_chat')
EVENTS={'low':'잔여 적음','exhausted':'소진','recovered':'초기화 후 회복','spike':'사용량 급증',
        'collector':'수집기 재시작','source':'수집 실패 지속','budget':'프로젝트 예산','report':'주간 리포트'}
SOURCE_ERROR_MINUTES=30
COLLECTOR_GAP_MINUTES=10
STATE_KEY='notify:state'
SOURCE='외부 알림'


def notification_url(value):
    """Parse a secret HTTPS endpoint without ever echoing it in a validation error."""
    try:
        if not isinstance(value,str) or not value or any(ch.isspace() or ord(ch)<32 or ord(ch)==127 for ch in value) or '\\' in value:
            raise ValueError
        parts=urlsplit(value)
        if parts.scheme!='https' or not parts.netloc or any(ch in parts.netloc for ch in '@%'):
            raise ValueError
        host=parts.hostname
        if not host or parts.netloc.endswith(':') or (parts.port is not None and not 1<=parts.port<=65535):raise ValueError
        if ':' in host:
            # Older urlsplit accepts trailing text after a closing IPv6 bracket.
            # Validate the entire authority before exposing it as a masked hint.
            if not re.fullmatch(r'\[[0-9A-Fa-f:.]+\](?::[0-9]+)?',parts.netloc):raise ValueError
            ipaddress.IPv6Address(host)
        else:
            if not re.fullmatch(r'[^:\[\]]+(?::[0-9]+)?',parts.netloc):raise ValueError
            ascii_host=host.encode('idna').decode('ascii').removesuffix('.')
            if len(ascii_host)>253 or not all(re.fullmatch(r'[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?',label) for label in ascii_host.split('.')):
                raise ValueError
        return parts
    except (ValueError,UnicodeError):
        raise ValueError('알림 주소는 사용자 정보 없는 유효한 HTTPS 주소여야 합니다.') from None


def configured(notify):
    notify=notify or {}
    return [name for name,keys in (('ntfy',('ntfy_url',)),('webhook',('webhook_url',)),('telegram',('telegram_token','telegram_chat')))
            if all(notify.get(k) for k in keys)]


def masked(notify):
    """What the settings page may show: which channels are set, never their values."""
    notify=notify or {}
    def hint(url):
        try:return notification_url(url).netloc
        except ValueError:return '설정됨'
    return dict(channels={'ntfy':hint(notify['ntfy_url']) if notify.get('ntfy_url') else None,
                          'webhook':hint(notify['webhook_url']) if notify.get('webhook_url') else None,
                          'telegram':'설정됨' if notify.get('telegram_token') and notify.get('telegram_chat') else None},
                events={k:(notify.get('events') or {}).get(k,True) for k in EVENTS},
                quiet=notify.get('quiet'))


def _post(url,payload,timeout):
    data=json.dumps(payload,ensure_ascii=False).encode()
    request=urllib.request.Request(url,data=data,headers={'Content-Type':'application/json'})
    with urllib.request.urlopen(request,timeout=timeout) as response:response.read(256)


def send(notify,title,body,timeout=10):
    """Send to every configured channel. Returns {channel: error text} for failures;
    error text names only the status or exception type, never a URL or token."""
    errors={}
    for channel in configured(notify):
        try:
            if channel=='ntfy':
                parts=notification_url(notify['ntfy_url']);topic=parts.path.strip('/')
                if not topic or '/' in topic:raise ValueError('topic')
                _post(f'{parts.scheme}://{parts.netloc}',dict(topic=topic,title=title,message=body,tags=['bar_chart']),timeout)
            elif channel=='webhook':
                notification_url(notify['webhook_url'])
                text=f'**{title}**\n{body}'
                _post(notify['webhook_url'],dict(content=text,text=text),timeout)
            else:
                _post(f"https://api.telegram.org/bot{notify['telegram_token']}/sendMessage",
                      dict(chat_id=notify['telegram_chat'],text=f'{title}\n{body}'),timeout)
        except urllib.error.HTTPError as exc:errors[channel]=f'HTTP {exc.code}';exc.close()
        except (OSError,ValueError) as exc:errors[channel]=type(exc).__name__
    return errors


def quiet_now(notify,now):
    quiet=(notify or {}).get('quiet')
    if not quiet or len(quiet)!=2:return False
    start,end=quiet;hour=datetime.fromtimestamp(now,KST).hour
    return start<=hour<end if start<end else hour>=start or hour<end


SERVICE={'codex':'Codex','claude-code':'Claude','antigravity':'Antigravity','opencode-go':'OpenCode Go','devin':'Devin'}
WINDOWS={'five_hour':'5시간','seven_day':'주간','seven_day_opus':'Opus 주간','seven_day_sonnet':'Sonnet 주간',
         'seven_day_oauth_apps':'앱 주간','rolling':'5시간','daily':'일간','weekly':'주간','monthly':'월간'}


def _label(row):
    """The same names the dashboard shows (bucketName/quotaLabel in web/format.js)."""
    bucket=row['bucket'];service=SERVICE.get(row['route'],row['route'])
    agy=re.fullmatch(r'(gemini|3p)-(5h|weekly)(?:-(\d+))?',bucket) if row['route']=='antigravity' else None
    if agy:
        return f"{service} {'5시간' if agy[2]=='5h' else '주간'} {'3rd party' if agy[1]=='3p' else 'Gemini'}"+(f' {agy[3]}' if agy[3] else '')
    name=WINDOWS.get(bucket) or row.get('display_name') or bucket
    return name if name.startswith(service) else f'{service} {name}'


def quota_messages(rows,previous,low_percent):
    """(messages, new state) from quota rows; the first sight of a bucket only records it."""
    state={};messages=[]
    for row in rows:
        if row.get('status')!='fresh' or row.get('remaining') is None or row.get('blocked_by'):continue
        key=f"{row['route']}:{row['bucket']}";remaining=row['remaining']
        now_state=dict(zero=remaining<=0,low=remaining<=low_percent,resets=row.get('resets'))
        state[key]=now_state;before=previous.get(key)
        if before is None:continue
        label=_label(row)
        if now_state['zero'] and not before['zero']:
            messages.append(('exhausted',f'{label} 소진',f"잔여 0% · 초기화 {_when(row.get('resets'))}"))
        elif now_state['low'] and not before['low']:
            messages.append(('low',f'{label} 잔여 {remaining:.1f}%',f"잔여 적음 기준 {low_percent}% 이하 · 초기화 {_when(row.get('resets'))}"))
        elif before['low'] and not now_state['low'] and before.get('resets')!=now_state['resets']:
            messages.append(('recovered',f'{label} 회복',f'초기화 후 잔여 {remaining:.1f}%'))
    # Buckets that are not current right now keep their last state.
    return messages,{**previous,**state}


def brief_line(limits):
    """One terminal line of current quota values, e.g. for a status line or a prompt.

    Only current (fresh) observations of the shared windows are listed; a window whose
    longer window is exhausted reads 사용 불가. Nothing current says so instead of 0%."""
    parts=[]
    for row in limits.get('limits') or []:
        if row.get('status')!='fresh' or row.get('remaining') is None:continue
        if row['route']=='codex' and ' · ' in row['bucket'] and not row['bucket'].startswith('codex ·'):continue
        value='사용 불가' if row.get('blocked_by') else f"{row['remaining']:.0f}%"
        parts.append(f'{_label(row)} {value}')
    return ' · '.join(parts) if parts else '한도 최신값 없음'


def _when(ts):
    return datetime.fromtimestamp(ts,KST).strftime('%m-%d %H:%M KST') if ts else '미제공'


def budget_level(usage,budget):
    """(level 0/80/100, ratio) of month-to-date usage against a token and/or USD budget."""
    ratios=[]
    if budget.get('tokens'):ratios.append(usage.get('tokens',0)/budget['tokens'])
    if budget.get('usd') and usage.get('cost') is not None:ratios.append(usage['cost']/budget['usd'])
    ratio=max(ratios,default=0)
    return (100 if ratio>=1 else 80 if ratio>=.8 else 0),ratio


def budget_messages(month,budgets,previous):
    """Crossing 80% or 100% of a project's monthly budget, once per level per month."""
    state=previous if previous.get('month')==month['month'] else dict(month=month['month'],levels={})
    messages=[]
    for project,budget in (budgets or {}).items():
        level,ratio=budget_level(month['projects'].get(project,{}),budget)
        if level>state['levels'].get(project,0):
            messages.append(('budget',f'{project} 월 예산 {"초과" if level==100 else "80% 도달"}',f"{month['month']} 사용 {ratio*100:.0f}%"))
        state['levels'][project]=max(level,state['levels'].get(project,0))
    return messages,state


def evaluate(store,notify,now=None,limits=None,spike=None,sources=None,month=None,budgets=None):
    """Compare with the last evaluation, send what changed, and remember it."""
    now=now or time.time()
    with store.connect() as c:previous=store.state(c,STATE_KEY,{}) or {}
    messages=[]
    if limits is not None:
        quota,previous_quota=quota_messages(limits['limits'],previous.get('quota',{}),limits.get('low_percent',15))
        messages+=quota;previous['quota']=previous_quota
    if month is not None:
        budget,previous['budget']=budget_messages(month,budgets,previous.get('budget') or {})
        messages+=budget
    if spike and spike.get('hour_start')!=previous.get('spike_hour'):
        previous['spike_hour']=spike['hour_start']
        messages.append(('spike','사용량 급증',f"{_when(spike['hour_start'])}부터 1시간 {spike['tokens']/1e6:.1f}M 토큰 · 평소 상위 5% 기준의 {spike['ratio']:.1f}배"))
    if sources is not None:
        failing=previous.get('failing',{})
        for source in sources:
            name=source['name']
            if name==SOURCE:continue  # its own failures are already the source status
            if source.get('status')!='error':failing.pop(name,None);continue
            since=failing.setdefault(name,dict(since=now,sent=False))
            if not since['sent'] and now-since['since']>=SOURCE_ERROR_MINUTES*60:
                since['sent']=True
                messages.append(('source',f'{name} 수집 실패 지속',f"{SOURCE_ERROR_MINUTES}분 넘게 실패 · {source.get('detail') or ''}".strip(' ·')))
        previous['failing']=failing
    with store.connect() as c:store.save_state(c,STATE_KEY,previous)
    return deliver(store,notify,messages,now)


OUTBOX_KEY='notify:outbox'
LOG_KEY='notify:log'
LOG_KEEP=50
# Held or failed messages are retried until they expire; a weekly report stays useful longer.
OUTBOX_TTL={'report':7*86400}
DEFAULT_TTL=24*3600
MAX_ATTEMPTS=6
# More held messages than this are sent as one digest when quiet hours end.
DIGEST_OVER=3


def _log(c,store,entries):
    log=(store.state(c,LOG_KEY,[]) or [])+entries
    store.save_state(c,LOG_KEY,log[-LOG_KEEP:])


def recent_log(store):
    """Newest first: what was sent, held for quiet hours, retried or dropped."""
    with store.connect() as c:return list(reversed(store.state(c,LOG_KEY,[]) or []))


def deliver(store,notify,messages,now):
    """Send what the settings enable; hold it during quiet hours or after a failed send.

    Held messages wait in an outbox (persisted state) and go out on the first evaluation
    outside quiet hours; several are combined into one digest. A message whose every
    channel failed is retried each evaluation until it expires. The outcome is recorded
    as the '외부 알림' source and in a short history, without any channel secret."""
    enabled=(notify or {}).get('events') or {}
    wanted=[dict(kind=k,title=t,body=b,ts=now,attempts=0) for k,t,b in messages if enabled.get(k,True)]
    if not configured(notify):return []
    with store.connect() as c:
        outbox=[m for m in (store.state(c,OUTBOX_KEY,[]) or []) if enabled.get(m['kind'],True)]
        fresh=[m for m in outbox if now-m['ts']<=OUTBOX_TTL.get(m['kind'],DEFAULT_TTL) and m['attempts']<MAX_ATTEMPTS]
        expired=[m for m in outbox if m not in fresh]
        log=[dict(ts=now,kind=m['kind'],title=m['title'],status='expired') for m in expired]
        if quiet_now(notify,now):
            log+=[dict(ts=now,kind=m['kind'],title=m['title'],status='held') for m in wanted]
            store.save_state(c,OUTBOX_KEY,fresh+wanted);_log(c,store,log)
            return []
        store.save_state(c,OUTBOX_KEY,[]);_log(c,store,log)
    queue=fresh+wanted
    if not queue:return []
    held=[m for m in fresh if m['attempts']==0]
    if len(held)>DIGEST_OVER:
        digest=dict(kind='digest',title=f'방해 금지 동안 알림 {len(held)}건',
                    body='\n'.join(f"{_when(m['ts'])} {m['title']}" for m in held),ts=now,attempts=0,parts=held)
        queue=[digest]+[m for m in queue if m not in held]
    failures={};sent=[];retry=[];log=[]
    for message in queue:
        # A held or retried message says when it happened: the situation may have changed since.
        late='parts' not in message and message['ts']<now
        errors=send(notify,message['title'],message['body']+(f" · {_when(message['ts'])} 발생" if late else ''))
        failures.update(errors)
        parts=message.get('parts') or [message]
        if errors and len(errors)==len(configured(notify)):
            for part in parts:retry.append({**part,'attempts':part['attempts']+1})
            log.append(dict(ts=now,kind=message['kind'],title=message['title'],status='failed',
                            error=' · '.join(f'{k} {v}' for k,v in sorted(errors.items()))))
        else:
            sent+=[(m['kind'],m['title'],m['body']) for m in parts]
            log.append(dict(ts=now,kind=message['kind'],title=message['title'],
                            status='resent' if message['attempts'] or message['ts']<now else 'sent',
                            channels=[ch for ch in configured(notify) if ch not in errors]))
    with store.connect() as c:
        # Another evaluation may not run in between, but keep anything already queued.
        store.save_state(c,OUTBOX_KEY,(store.state(c,OUTBOX_KEY,[]) or [])+retry)
        _log(c,store,log)
        store.source(c,SOURCE,'error' if failures else 'ok',
                     ' · '.join(f'{k} {v}' for k,v in sorted(failures.items())) if failures else
                     f'{len(sent)}건 전송'+(f' · 재시도 대기 {len(retry)}건' if retry else ''),now)
    return sent


def record_failure(store,exc):
    """An exception in evaluation is shown, not swallowed; collection continues."""
    with store.connect() as c:store.source(c,SOURCE,'error',f'알림 판단 실패 ({type(exc).__name__})')


def collector_started(store,notify,now=None):
    """Called once at collector start, before the first heartbeat is written."""
    now=now or time.time()
    with store.connect() as c:last=(store.state(c,'collector',{}) or {}).get('checked')
    if last and now-last>=COLLECTOR_GAP_MINUTES*60:
        minutes=round((now-last)/60)
        return deliver(store,notify,[('collector','수집기 재시작',f'약 {minutes}분 동안 수집이 멈춘 뒤 다시 시작했습니다. 그 사이 한도 관측은 비어 있습니다.')],now)
    return []
