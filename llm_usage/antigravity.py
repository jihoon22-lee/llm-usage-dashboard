"""Read local Antigravity request statistics without retaining conversation content.

Wire field numbers were checked against the descriptors in CLI 1.1.28 and
against desktop-app (language server 2.19.1) databases, which share the schema.
Only steps.metadata.model_usage is counted: generator usage can aggregate several
requests (including retries). Generator metadata supplies model names only.
"""
from collections import defaultdict
from contextlib import closing
import json
from pathlib import Path
import re
import sqlite3

from .sqlite_ro import open_readonly
from .store import TOKENS, identity

# 2: requests carry their conversation as an opaque session identity.
PARSER_VERSION=2
# APIProvider descriptors identify the maker even when a model uses an unnamed placeholder.
API_MAKERS={24:'Google',26:'Anthropic',31:'OpenAI'}


class InvalidProtobuf(ValueError):pass
class MissingRequestId(ValueError):pass
class MissingTimestamp(ValueError):pass
class InvalidUsage(ValueError):pass
class PendingUsage(ValueError):pass


def varint(raw,offset):
    value=0
    for shift in range(0,70,7):
        if offset>=len(raw):raise InvalidProtobuf('truncated varint')
        byte=raw[offset];offset+=1
        if shift==63 and byte>1:raise InvalidProtobuf('oversized varint')
        value|=(byte&127)<<shift
        if byte<128:return value,offset
    raise InvalidProtobuf('unterminated varint')


def message(raw,wanted):
    """Skip all unneeded fields, particularly prompts, tool arguments and headers."""
    if not isinstance(raw,(bytes,memoryview)):raise InvalidProtobuf('invalid message')
    if len(raw)>128*1024*1024:raise InvalidProtobuf('oversized message')
    raw=memoryview(raw);offset=0;result={}
    while offset<len(raw):
        tag,offset=varint(raw,offset);field,wire=tag>>3,tag&7
        if not 0<field<2**29:raise InvalidProtobuf('invalid field')
        if wire==0:value,offset=varint(raw,offset)
        elif wire in (1,2,5):
            if wire==2:size,offset=varint(raw,offset)
            else:size=8 if wire==1 else 4
            end=offset+size
            if end>len(raw):raise InvalidProtobuf('truncated field')
            value=raw[offset:end];offset=end
        else:raise InvalidProtobuf('unsupported wire type')
        if field in wanted:
            if wanted[field]!=wire:raise InvalidProtobuf('unexpected field type')
            result[field]=value
    return result


def timestamp(raw):
    if raw is None:return None
    values=message(raw,{1:0,2:0});seconds=values.get(1,0);nanos=values.get(2,0)
    if not 0<seconds<=253402300799 or not 0<=nanos<10**9:raise MissingTimestamp('invalid timestamp')
    return seconds+nanos/10**9


def model_name(raw):
    if raw is None:return None
    if len(raw)>160:return None
    try:value=bytes(raw).decode('utf-8')
    except UnicodeDecodeError:return None
    return value if re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:/+-]{0,159}',value) else None


def usage_ids(usage):
    # Multiple original IDs can refer to one response. Retain only opaque hashes.
    result=[]
    for field,kind in ((11,'response'),(12,'provider-message'),(7,'message')):
        raw=usage.get(field)
        if raw and len(raw)<=1024:
            result.append(identity('antigravity',usage.get(6,0),kind,bytes(raw).hex()))
    return result


def generator_model(raw):
    generator=message(raw,{1:2})
    chat=message(generator.get(1,b''),{3:0,4:2,19:2,22:2})
    usage=message(chat.get(4,b''),{1:0,6:0,7:2,11:2,12:2})
    name=model_name(chat.get(19)) or model_name(chat.get(22))
    return usage.get(1,chat.get(3,0)),name,usage_ids(usage)


def step_request(raw,models,model_aliases,enum_models):
    step=message(raw,{1:2,8:2,9:2,11:0,20:2,24:2,32:2})
    if not step.get(9):return None
    usage=message(step[9],{k:0 for k in (1,2,3,4,5,6,9,10)}|{k:2 for k in (7,11,12)})
    if not usage:return None
    if not any(k in usage for k in (2,3,4,5)) and not step.get(8):raise PendingUsage('usage counters pending')
    aliases=usage_ids(usage)
    if not aliases:raise MissingRequestId('no original request identity')
    ts=timestamp(step.get(1)) or timestamp(step.get(32))
    if ts is None:raise MissingTimestamp('no original timestamp')
    nums={key:usage.get(field,0) for field,key in ((2,'uncached_input'),(5,'cached_input'),(3,'output'),
            (4,'cache_creation'),(9,'reasoning'),(10,'response_output'))}
    if any(not 0<=value<=2**63-1 for value in nums.values()):raise InvalidUsage('invalid token counter')
    if nums['reasoning']>nums['output']:raise InvalidUsage('reasoning exceeds output')
    # Scalar protobuf zero is valid. A supplied response split must agree with output.
    if 10 in usage and nums['output']!=nums['reasoning']+nums['response_output']:
        raise InvalidUsage('inconsistent output split')
    enum=usage.get(1,step.get(11,0));name=None
    for alias in aliases:
        if alias in model_aliases and model_aliases[alias][0]==enum:name=model_aliases[alias][1];break
    source=message(step.get(20,b''),{3:0})
    gen=models.get(source.get(3,0))
    if name is None and gen and gen[0]==enum:name=gen[1]
    if name is None and len(enum_models.get(enum,()))==1:name=next(iter(enum_models[enum]))
    if name is None:
        info=message(step.get(24,b''),{1:0,8:2,12:2})
        if info.get(1)==enum:name=model_name(info.get(12)) or model_name(info.get(8))
    return aliases,dict(ts=ts,model=name or 'unknown',model_enum=enum,api_provider=usage.get(6,0),
                        completed=timestamp(step.get(8)) is not None,**nums)


def save_request(store,c,aliases,record):
    """Merge ID aliases and select one coherent snapshot; never sum copies."""
    marks=','.join('?' for _ in aliases)
    keys={r[0] for r in c.execute('SELECT request_id FROM antigravity_request_aliases WHERE alias IN ('+marks+')',aliases)}
    key=min(keys|set(aliases))
    previous=[];conflict=False
    for old in keys:
        row=c.execute('SELECT data,conflict FROM antigravity_requests WHERE id=?',(old,)).fetchone()
        if row:previous.append(json.loads(row['data']));conflict=conflict or bool(row['conflict'])
    candidates=previous+[record]
    def rank(row):
        return (row['completed'],row['output'],sum(row[k] for k in ('uncached_input','cached_input','cache_creation')),
                tuple(row[k] for k in TOKENS),row['model']!='unknown',row['model'])
    best=dict(max(candidates,key=rank))
    for old in previous:
        if old['completed']==record['completed']:
            left=tuple(old[k] for k in TOKENS);right=tuple(record[k] for k in TOKENS)
            if not (all(a<=b for a,b in zip(left,right)) or all(a>=b for a,b in zip(left,right))):conflict=True
    best['ts']=min(row['ts'] for row in candidates)
    # The conversation of any copy names the session; copies share the conversation file name.
    best['session']=best.get('session') or next((row['session'] for row in candidates if row.get('session')),None)
    if best['model']=='unknown':
        known=[row for row in candidates if row['model']!='unknown' and row['model_enum']==best['model_enum']]
        if known:best['model']=known[0]['model'];best['provider']=known[0]['provider']
    if best['provider']=='Unknown':best['provider']=API_MAKERS.get(best['api_provider'],'Unknown')
    for old in keys-{key}:
        c.execute('UPDATE antigravity_request_aliases SET request_id=? WHERE request_id=?',(key,old))
        c.execute('DELETE FROM antigravity_requests WHERE id=?',(old,))
        c.execute("DELETE FROM events WHERE id=? AND route='antigravity'",(old,))
    c.executemany('INSERT INTO antigravity_request_aliases VALUES (?,?) ON CONFLICT(alias) DO UPDATE SET request_id=excluded.request_id',
                  [(alias,key) for alias in aliases])
    c.execute('INSERT OR REPLACE INTO antigravity_requests VALUES (?,?,?,?,?)',
              (key,best['ts'],best['model'],json.dumps(best,sort_keys=True),int(conflict)))
    # Unlike component-wise MAX, this retains a real snapshot and its output split.
    # Explicit columns keep the nullable session/project/agent_kind fields NULL.
    # Explicit columns keep the project NULL (the conversation DB records no workspace).
    c.execute('INSERT OR REPLACE INTO events (id,ts,provider,route,model,uncached_input,cached_input,output,cache_creation,reasoning,session,agent_kind) '
              'VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
              (key,best['ts'],best['provider'],'antigravity',best['model'],*(best[k] for k in TOKENS),
               best.get('session'),'main' if best.get('session') else None))


def signature(path):
    values=[]
    for file in (path,Path(str(path)+'-wal')):
        try:
            stat=file.stat();values.append([stat.st_ino,stat.st_size,stat.st_mtime_ns])
        except FileNotFoundError:values.append(None)
    return values


def collect_database(store,path,provider_for):
    key='antigravity-db:'+str(path);before=signature(path)
    with store.connect() as c:
        state=store.state(c,key,{})
        if state.get('version')==PARSER_VERSION and state.get('signature')==before:
            return state['stats']
        models={};model_aliases={};enum_models=defaultdict(set);stats=dict(requests=0,unknown_models=0,errors=0)
        # Each conversation is its own database file named by the conversation ID; keep only a hash.
        session='agy-'+identity('antigravity-conversation',Path(path).stem)[:24]
        c.execute('DELETE FROM import_errors WHERE source=?',(key,))
        with closing(open_readonly(path)) as src:
            src.execute('BEGIN')
            for idx,raw in src.execute('SELECT idx,data FROM gen_metadata'):
                try:
                    enum,name,aliases=generator_model(raw)
                    if name:
                        models[idx]=(enum,name);enum_models[enum].add(name)
                        for alias in aliases:model_aliases[alias]=(enum,name)
                except (ValueError,TypeError,OverflowError):
                    store.import_error(c,key,'model:'+str(idx),'InvalidModelMetadata');stats['errors']+=1
            for idx,raw in src.execute('SELECT idx,metadata FROM steps WHERE metadata IS NOT NULL'):
                try:
                    parsed=step_request(raw,models,model_aliases,enum_models)
                    if parsed is None:continue
                    aliases,record=parsed;record['provider']=provider_for(record['model'])
                    record['session']=session
                    if record['provider']=='Unknown':record['provider']=API_MAKERS.get(record['api_provider'],'Unknown')
                    save_request(store,c,aliases,record)
                    stats['requests']+=1;stats['unknown_models']+=record['model']=='unknown'
                except (ValueError,TypeError,OverflowError) as exc:
                    store.import_error(c,key,'step:'+str(idx),type(exc).__name__);stats['errors']+=1
        # A WAL write during the snapshot invalidates this checkpoint next time.
        store.save_state(c,key,dict(version=PARSER_VERSION,signature=before,stats=stats))
        return stats


APP_CONVERSATIONS='.gemini/antigravity/conversations'
CLI_CONVERSATIONS='.gemini/antigravity-cli/conversations'


def roots(settings):
    """Yield (name, path, optional). The CLI and the desktop app (2.x, including its
    WSL remote server) write the same conversation schema to different folders."""
    found=[];seen=set();platforms=defaultdict(int)
    for source in settings.get('sources',[]):
        if source.get('kind')=='antigravity':found.append((source['name'],Path(source['path']),False))
    for home in settings.get('homes',[]):
        platform='Windows' if str(home).startswith('/mnt/') else 'WSL'
        platforms[platform]+=1
        suffix=' '+str(platforms[platform]) if platforms[platform]>1 else ''
        found.append((platform+' Antigravity DB'+suffix,Path(home)/CLI_CONVERSATIONS,False))
        # Homes that never ran the desktop app should not degrade the summary.
        found.append((platform+' Antigravity 앱 DB'+suffix,Path(home)/APP_CONVERSATIONS,True))
    for name,path,optional in found:
        canonical=str(path.resolve())
        if canonical not in seen:seen.add(canonical);yield name,path,optional


def collect_databases(store,settings,provider_for):
    files=0;failed=0;missing=0;parse_errors=0
    for name,root,optional in roots(settings):
        try:
            paths=sorted(root.glob('*.db')) if root.is_dir() else ([root] if root.is_file() else [])
            if not paths:
                if optional:
                    with store.connect() as c:
                        if c.execute('SELECT 1 FROM sources WHERE name=?',(name,)).fetchone():
                            store.source(c,name,'unavailable','Antigravity 앱 대화 DB가 없습니다.')
                    continue
                missing+=1
                with store.connect() as c:store.source(c,name,'unavailable','Antigravity 대화 DB가 없습니다.')
                continue
            good=bad=errors=0
            for path in paths:
                try:
                    stats=collect_database(store,path,provider_for);good+=1;errors+=stats['errors']
                except (OSError,sqlite3.Error,ValueError):bad+=1
            files+=good;failed+=bad;parse_errors+=errors
            with store.connect() as c:
                store.source(c,name,'partial' if bad or errors else 'ok',
                    f'{good}개 DB 확인 · 파일 읽기 실패 {bad}개 · 해석 실패 {errors}건')
        except OSError:
            failed+=1
            with store.connect() as c:store.source(c,name,'error','Antigravity 기록 경로 읽기 실패')
    with store.connect() as c:
        total=c.execute("SELECT COUNT(*),COALESCE(SUM(model='unknown'),0),COALESCE(SUM(conflict),0),COALESCE(SUM(json_extract(data,'$.completed')=0),0) FROM antigravity_requests").fetchone()
        status=('partial' if failed or parse_errors or missing or total[2] or total[3] else 'ok') if files else 'error' if failed else 'unavailable'
        detail=(f'{files}개 DB 확인 · 보존한 고유 요청 {total[0]}개 · 모델 미확인 {total[1]}개 · 상충 관측 {total[2]}개 · 진행 중 요청 {total[3]}개'
                f' · 읽기 실패 {failed}개 · 해석 실패 {parse_errors}건 · 빈/없는 경로 {missing}개')
        store.source(c,'antigravity-records',status,detail)
