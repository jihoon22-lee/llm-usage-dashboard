"""Read original numeric usage independently of the ingestion parsers.

Only committed file prefixes / database checkpoints are compared. Appends and
replacement files still awaiting ingestion are reported as pending, and missing
historical snapshots never justify reducing retained usage. No message content
or native account identifiers leave this reader.
"""
import hashlib
import json
import re
import sqlite3
from contextlib import closing
from pathlib import Path

from .claude_accounts import transcript_sources
from .record_files import RecordReadError, compressed, open_record, record_files, unsupported_files
from .sqlite_ro import open_readonly
from .store import identity, stamp

VALUES=('uncached_input','cached_input','output','cache_creation','reasoning')


def _counts():
    return dict(source_files=0, requests=0, matched_requests=0, missing_events=0,
                mismatched_events=0, source_errors=0, unreadable_records=0,
                pending_files=0, pending_records=0, unavailable_sources=0,
                unsupported_files=0, retained_larger=0, detail_inconsistent_records=0,
                source_conflicts=0, detail_conflicts=0)


def _number(value):
    if value is None:return 0
    if type(value) is not int or not 0<=value<=2**63-1:raise ValueError('invalid counter')
    return value


def _prefix(path, checkpoint):
    """Whether the currently available file still contains the committed prefix."""
    stat=path.stat();offset=checkpoint.get('offset',0)
    if compressed(path):
        return all(checkpoint.get(k)==v for k,v in
                   (('size',stat.st_size),('mtime',stat.st_mtime_ns),('inode',stat.st_ino)))
    if checkpoint.get('inode')!=stat.st_ino or stat.st_size<offset:return False
    with path.open('rb') as f:
        f.seek(max(0,offset-64));tail=f.read(min(offset,64))
    if checkpoint.get('tail'):return hashlib.sha256(tail).hexdigest()==checkpoint['tail']
    return not offset or tail.endswith(b'\n')


def _codex(data, state, expected, conflicts):
    kind=data.get('type');payload=data.get('payload') or {};ts=stamp(data.get('timestamp'))
    if kind=='session_meta' and not state.get('session'):
        state.update(session=payload.get('id') or payload.get('session_id'),
                     provider=payload.get('model_provider') or 'openai')
        creator=payload.get('creator_account_id')
        if isinstance(creator,str) and re.fullmatch(r'[A-Za-z0-9-]{1,100}',creator):
            state['account']='codex-account:'+identity(creator)
        if payload.get('forked_from_id'):state['fork']=stamp(payload.get('timestamp') or data.get('timestamp'))
    elif kind=='turn_context':
        if not state.get('fork') or (ts or state['fork'])>=state['fork']:
            state['own_turn']=True
            if payload.get('model'):state['model']=payload['model']
    elif kind=='token_usage_record':
        if not state.get('session') or not ts:return
        if state.get('fork') and (ts<state['fork'] or not state.get('own_turn')):return
        rid=payload.get('response_id');usage=payload.get('usage')
        if not isinstance(rid,str) or not re.fullmatch(r'[A-Za-z0-9._:/-]{1,200}',rid):raise ValueError('invalid identity')
        if not isinstance(usage,dict) or not {'input_tokens','output_tokens'}<=usage.keys():raise ValueError('missing usage')
        numbers=tuple(_number(usage.get(k)) for k in ('input_tokens','cached_input_tokens','output_tokens',
                                                    'cache_write_input_tokens','reasoning_output_tokens'))
        inp,cached,out,creation,reasoning=numbers
        if inp<cached+creation or out<reasoning:raise ValueError('inconsistent components')
        route='codex' if state['provider']=='openai' else 'codex:'+state['provider']
        key=identity('codex-response',route,rid);session=identity(state['session'])
        observed=int(usage.get('reasoning_output_tokens') is not None)
        if key in expected:
            old=expected[key]
            if old['session']!=session or old['numbers']!=numbers:conflicts.add(key)
            else:
                old['observed']=max(old['observed'],observed)
                if state.get('model'):old['models'].add(state['model'])
                if state.get('account'):old['accounts'].add(state['account'])
        else:expected[key]=dict(session=session,numbers=numbers,observed=observed,models={state.get('model') or 'unknown'},
                                accounts={state['account']} if state.get('account') else set())


def _claude_component(usage):
    numbers=tuple(_number(usage.get(k)) for k in ('input_tokens','cache_read_input_tokens',
                                                 'output_tokens','cache_creation_input_tokens'))
    output=usage.get('output_tokens_details') or {}
    raw=output.get('thinking_tokens',usage.get('reasoning_output_tokens'))
    bad=False
    try:
        reasoning=_number(raw);observed=int(raw is not None)
        if reasoning>numbers[2]:raise ValueError('thinking exceeds output')
    except ValueError:reasoning=observed=0;bad=True
    ttl=usage.get('cache_creation') or {};pair=None
    if all(ttl.get(k) is not None for k in ('ephemeral_5m_input_tokens','ephemeral_1h_input_tokens')):
        try:
            pair=tuple(_number(ttl[k]) for k in ('ephemeral_5m_input_tokens','ephemeral_1h_input_tokens'))
            if sum(pair)!=numbers[3]:raise ValueError('cache split differs')
        except ValueError:pair=None;bad=True
    return (*numbers,reasoning),observed,pair,bad


def _claude(data, route, expected, bad_details):
    if data.get('type')!='assistant' or not stamp(data.get('timestamp')):return
    message=data.get('message') or {};usage=message.get('usage');mid=message.get('id')
    if not usage or not mid:return
    iterations=usage.get('iterations') or []
    if not isinstance(iterations,list):raise ValueError('invalid iterations')
    children=[(i,p) for i,p in enumerate(iterations) if isinstance(p,dict) and p.get('type')=='advisor_message']
    parent=identity(route,mid)
    parts=[(parent,usage,message.get('model') or 'unknown',None)]
    parts.extend((identity('claude-advisor',route,mid,i),p,p.get('model') or 'unknown',parent) for i,p in children)
    for key,part,model,owner in parts:
        numbers,observed,pair,bad=_claude_component(part)
        if bad:bad_details.add(key)
        if not any(numbers) and not owner and not children:continue
        session=data.get('sessionId') or data.get('session_id')
        if key not in expected:
            expected[key]=dict(numbers=numbers,observed=observed,pairs={pair} if pair else set(),
                               models={model},sessions={session} if session else set(),parent=owner)
            continue
        old=expected[key];previous=old['numbers']
        if numbers[3]>previous[3]:old['pairs']={pair} if pair else set()
        elif numbers[3]==previous[3] and pair:old['pairs'].add(pair)
        if numbers[2]>previous[2]:old['observed']=observed
        elif numbers[2]==previous[2]:old['observed']=max(old['observed'],observed)
        old['numbers']=tuple(max(a,b) for a,b in zip(previous,numbers));old['models'].add(model)
        if session:old['sessions'].add(session)


def _compare_codex(c, expected, counts):
    counts['requests']=len(expected)
    exists=c.execute("SELECT 1 FROM sqlite_master WHERE name='codex_responses'").fetchone()
    for key,want in expected.items():
        source=c.execute('SELECT * FROM codex_responses WHERE id=?',(key,)).fetchone() if exists else None
        if source is None:counts['missing_events']+=1;continue
        wrong=(tuple(json.loads(source['usage']))!=want['numbers'] or source['session']!=want['session']
               or source['model'] not in want['models'] or source['reasoning_observed']<want['observed'])
        if want['accounts']:wrong=wrong or source['account'] not in want['accounts']
        if not source['projected']:
            counts['pending_records']+=1
            counts['mismatched_events']+=int(wrong)
            continue
        event=c.execute('SELECT * FROM events WHERE id=?',(key,)).fetchone()
        if event is None and any(want['numbers']):counts['missing_events']+=1;continue
        if event:
            inp,cached,out,creation,reasoning=want['numbers']
            wrong=wrong or tuple(event[k] for k in VALUES)!=(inp-cached-creation,cached,out,creation,reasoning)
            wrong=wrong or event['model'] not in want['models'] or event['reasoning_observed']<want['observed']
            wrong=wrong or identity(event['session'])!=want['session']
            if want['accounts']:wrong=wrong or event['account'] not in want['accounts']
        counts['mismatched_events' if wrong else 'matched_requests']+=1


def _compare_claude(c, expected, counts):
    counts['requests']=len(expected);counts['advisor_calls']=sum(bool(v['parent']) for v in expected.values())
    for key,want in expected.items():
        row=c.execute('SELECT * FROM events WHERE id=?',(key,)).fetchone()
        if row is None:counts['missing_events']+=1;continue
        actual=tuple(row[k] for k in VALUES);numbers=want['numbers']
        wrong=any(a<b for a,b in zip(actual,numbers)) or row['model'] not in want['models']
        wrong=wrong or row['parent_id']!=want['parent'] or row['requests']!=int(not want['parent'])
        wrong=wrong or row['advisor_calls']!=int(bool(want['parent']))
        if want['sessions']:wrong=wrong or row['session'] not in want['sessions']
        if want['parent']:
            parent=c.execute('SELECT account,session FROM events WHERE id=?',(want['parent'],)).fetchone()
            wrong=wrong or parent is None or (parent['account'],parent['session'])!=(row['account'],row['session'])
        if actual[2]==numbers[2]:wrong=wrong or row['reasoning_observed']<want['observed']
        wrong=wrong or sum(row[k] for k in ('cache_creation_5m','cache_creation_1h','cache_creation_unknown'))!=actual[3]
        counts['detail_conflicts']+=int(bool(row['cache_ttl_conflict']) or len(want['pairs'])>1)
        if row['cache_ttl_conflict']:
            wrong=wrong or row['cache_creation_5m']!=0 or row['cache_creation_1h']!=0 or row['cache_ttl_observed']!=0
        if actual[3]==numbers[3] and want['pairs']:
            if len(want['pairs'])>1:
                wrong=wrong or not row['cache_ttl_conflict'] or row['cache_creation_unknown']!=actual[3]
            elif not row['cache_ttl_conflict']:
                pair=next(iter(want['pairs']))
                wrong=wrong or (row['cache_creation_5m'],row['cache_creation_1h'])!=pair or not row['cache_ttl_observed']
        counts['retained_larger']+=int(not wrong and actual!=numbers)
        counts['mismatched_events' if wrong else 'matched_requests']+=1


def _opencode(c, sources, checkpoints):
    counts=_counts();expected={}
    for path in sorted({Path(s['path']) for s in sources if s.get('kind')=='opencode'}):
        if not path.exists():counts['unavailable_sources']+=1;continue
        counts['source_files']+=1;checkpoint=checkpoints.get('opencode:'+str(path))
        if checkpoint is None:counts['pending_files']+=1;continue
        try:
            with closing(open_readonly(path)) as source:
                source.execute('BEGIN')
                for mid,updated,raw in source.execute('SELECT id,time_updated,data FROM message'):
                    if updated>checkpoint:counts['pending_records']+=1;continue
                    try:
                        data=json.loads(raw);usage=data.get('tokens')
                        if data.get('role')!='assistant' or not usage:continue
                        cache=usage.get('cache') or {};reason=_number(usage.get('reasoning'))
                        numbers=(_number(usage.get('input')),_number(cache.get('read')),
                                 _number(usage.get('output'))+reason,_number(cache.get('write')),reason)
                        if not any(numbers):continue
                        if usage.get('total') is not None and _number(usage['total'])!=sum(numbers[:4]):
                            counts['detail_inconsistent_records']+=1
                        key=identity('opencode',mid)
                        item=dict(numbers=numbers,models={data.get('modelID') or 'unknown'},observed=int(usage.get('reasoning') is not None))
                        if key in expected:
                            old=expected[key];old['models'].update(item['models'])
                            if numbers[2]>old['numbers'][2]:old['observed']=item['observed']
                            elif numbers[2]==old['numbers'][2]:old['observed']=max(old['observed'],item['observed'])
                            old['numbers']=tuple(max(a,b) for a,b in zip(old['numbers'],numbers))
                        else:expected[key]=item
                    except (ValueError,TypeError,KeyError,AttributeError,OverflowError):counts['unreadable_records']+=1
        except (OSError,sqlite3.Error):counts['source_errors']+=1
    counts['requests']=len(expected)
    for key,want in expected.items():
        row=c.execute('SELECT * FROM events WHERE id=?',(key,)).fetchone()
        if row is None:counts['missing_events']+=1;continue
        actual=tuple(row[k] for k in VALUES)
        wrong=any(a<b for a,b in zip(actual,want['numbers'])) or row['model'] not in want['models']
        if actual[2]==want['numbers'][2]:wrong=wrong or row['reasoning_observed']<want['observed']
        counts['retained_larger']+=int(not wrong and actual!=want['numbers'])
        counts['mismatched_events' if wrong else 'matched_requests']+=1
    return counts


def reconcile_sources(path, cfg):
    result={k:_counts() for k in ('codex_original','claude')}
    native={};claude={};conflicts=set();bad_details=set();visited=set()
    with closing(open_readonly(Path(path))) as c:
        c.row_factory=sqlite3.Row;c.execute('BEGIN')
        checkpoints={k:json.loads(v) for k,v in c.execute("SELECT key,data FROM state WHERE key LIKE 'file:%' OR key LIKE 'opencode:%' OR key='claude_homes_observed'")}
        sources=transcript_sources(cfg,checkpoints.get('claude_homes_observed',[]))
        for source in sources:
            route=source.get('route');root=Path(source['path'])
            if source.get('kind')!='jsonl' or route not in ('codex','claude-code'):continue
            counts=result['codex_original' if route=='codex' else 'claude']
            if not root.exists():counts['unavailable_sources']+=1;continue
            counts['unsupported_files']+=unsupported_files(root)
            for path in record_files(root):
                resolved=path.resolve()
                if resolved in visited:continue
                visited.add(resolved);counts['source_files']+=1
                checkpoint=checkpoints.get('file:'+str(path))
                if not checkpoint:counts['pending_files']+=1;continue
                try:
                    if not _prefix(path,checkpoint):counts['pending_files']+=1;continue
                    offset=checkpoint.get('offset',0);state={}
                    with open_record(path) as f:
                        while f.tell()<offset:
                            raw=f.readline()
                            if not raw or not raw.endswith(b'\n') or f.tell()>offset:raise ValueError('incomplete committed prefix')
                            try:
                                data=json.loads(raw)
                                if route=='codex':_codex(data,state,native,conflicts)
                                else:_claude(data,route,claude,bad_details)
                            except (ValueError,TypeError,KeyError,AttributeError,OverflowError):counts['unreadable_records']+=1
                        if f.read(1):counts['pending_files']+=1
                except (OSError,EOFError,ValueError,RecordReadError):counts['source_errors']+=1
        result['codex_original']['source_conflicts']=len(conflicts)
        result['claude']['detail_inconsistent_records']=len(bad_details)
        _compare_codex(c,native,result['codex_original'])
        _compare_claude(c,claude,result['claude'])
        result['opencode']=_opencode(c,sources,checkpoints)
    return result
