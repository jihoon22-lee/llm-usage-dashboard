"""Incremental, restart-safe local collectors. No transcript text is persisted."""
import fcntl
import hashlib
import json
import os
import socket
import sqlite3
import time
import tempfile
from pathlib import Path

from .config import local_path, with_local
from .limits import codex_limits, poll_limits, status_limits
from .sqlite_ro import open_readonly
from .store import Store, identity, project_label, stamp


def model_provider(model):
    m=(model or "").lower()
    for provider,terms in [('Anthropic',('claude',)),('OpenAI',('gpt','o1','o3','o4','codex')),('Google',('gemini',)),('Meta',('muse-spark',)),('Z.ai',('glm',)),('MiniMax',('minimax',)),('Moonshot',('kimi','moonshot')),('Alibaba',('qwen',)),('DeepSeek',('deepseek',)),('Cognition',('swe',))]:
        if any(term in m for term in terms):return provider
    return 'Unknown'


def repair_provider_labels(store):
    with store.connect() as c:
        if store.state(c,'provider-labels-v2'):return
        # Stored model IDs remain useful even if the original tool removed its message.
        for table in ('events','codex_points'):
            rows=c.execute(f"SELECT DISTINCT model,route FROM {table} WHERE provider IN ('Unknown','Other')").fetchall()
            for row in rows:
                maker=model_provider(row['model'])
                if maker=='Unknown' and row['route']=='codex':maker='OpenAI'
                c.execute(f"UPDATE {table} SET provider=? WHERE model=? AND route=? AND provider IN ('Unknown','Other')",
                          (maker,row['model'],row['route']))
        store.save_state(c,'provider-labels-v2',True)
        store.bump_data_version(c)


# Status rows no current collector writes; left alone they linger as warnings.
RETIRED_SOURCES=('codex-records',)


def retire_legacy_sources(store):
    with store.connect() as c:
        if store.state(c,'legacy-sources-v1'):return
        marks=','.join('?' for _ in RETIRED_SOURCES)
        c.execute(f'DELETE FROM sources WHERE name IN ({marks})',RETIRED_SOURCES)
        c.execute(f'DELETE FROM source_health WHERE name IN ({marks})',RETIRED_SOURCES)
        store.save_state(c,'legacy-sources-v1',True)


def parse_line(store,c,route,d,state):
    typ=d.get('type');p=d.get('payload',{})
    if route=='codex':
        if typ=='session_meta' and not state.get('session'):
            # Fork rollouts embed a second, parent session_meta. The first ID owns this file.
            state['session']=p.get('id',p.get('session_id'))
            state['provider']=p.get('model_provider') or 'openai'
            state['project']=project_label(p.get('cwd'))
            state['agent_kind']=_codex_kind(p)
            if p.get('forked_from_id'):
                state['fork_started']=stamp(p.get('timestamp') or d.get('timestamp'))
        elif typ=='turn_context':
            if not state.get('fork_started') or (stamp(d.get('timestamp')) or state['fork_started'])>=state['fork_started']:
                state['has_turn_context']=True
                if p.get('model'):state['model']=p['model']
        elif typ=='event_msg' and p.get('type')=='token_count':
            ts=stamp(d.get('timestamp'))
            if p.get('rate_limits') and ts:codex_limits(store,c,p['rate_limits'],ts,'codex-local')
            usage=(p.get('info') or {}).get('total_token_usage')
            if not usage or not state.get('session') or not ts:return
            if state.get('fork_started') and (ts < state['fork_started'] or not state.get('has_turn_context')):
                changed=store.discard_codex_point(c,state['session'],ts,usage)
                if changed:state['_dirty']=changed
                return
            route_name='codex' if state.get('provider','openai')=='openai' else 'codex:'+state['provider']
            provider=model_provider(state.get('model',''))
            if provider=='Unknown' and state.get('provider')=='openai':provider='OpenAI'
            changed=store.codex_point(c,state['session'],ts,provider,route_name,
                                     state.get('model'),usage,p['info'].get('last_token_usage') or usage)
            if changed:state['_dirty']=changed
    elif typ=='assistant':
        m=d.get('message',{});u=m.get('usage');ts=stamp(d.get('timestamp'))
        if not u or not m.get('id') or not ts:return
        key=identity(route,m['id'])
        store.event(c,key,ts,model_provider(m.get('model','')),route,m.get('model'),dict(
            uncached_input=u.get('input_tokens',0),cached_input=u.get('cache_read_input_tokens',0),
            output=u.get('output_tokens',0),cache_creation=u.get('cache_creation_input_tokens',0),reasoning=u.get('reasoning_output_tokens',0)),
            session=d.get('sessionId'),project=project_label(d.get('cwd')),
            agent_kind='sub' if d.get('isSidechain') else 'main')


# Bytes just before the read offset identify the consumed prefix (as a digest only).
TAIL_BYTES=64


def _prefix_changed(path,checkpoint):
    """An in-place rewrite that grows (or keeps) the size reuses the inode; resuming
    from the old offset would then start in the middle of a different line."""
    offset=checkpoint.get('offset',0)
    if not offset:return False
    with path.open('rb') as f:
        f.seek(max(0,offset-TAIL_BYTES));tail=f.read(offset-max(0,offset-TAIL_BYTES))
    if checkpoint.get('tail'):return hashlib.sha256(tail).hexdigest()!=checkpoint['tail']
    return not tail.endswith(b'\n')


def _misaligned_errors(c,path,key):
    """Errors recorded at offsets that are no longer line starts came from reading a
    rewritten file mid-line; such a file is read again from the start once."""
    offsets=[int(r[0]) for r in c.execute('SELECT record FROM import_errors WHERE source=?',(key,)) if str(r[0]).isdigit()]
    if not offsets:return False
    with path.open('rb') as f:
        for offset in offsets:
            if not offset:continue
            f.seek(offset-1)
            if f.read(1)!=b'\n':return True
    return False


def collect_file(store,path,route):
    stat=path.stat();key='file:'+str(path)
    with store.connect() as c:
        checkpoint=store.state(c,key,{})
        repair_fork=(route=='codex' and checkpoint.get('parser',{}).get('fork_started') and checkpoint.get('parser_version',0)<1)
        unchanged=checkpoint.get('size')==stat.st_size and checkpoint.get('mtime')==stat.st_mtime_ns and checkpoint.get('inode')==stat.st_ino
        misaligned=_misaligned_errors(c,path,key)
        if not repair_fork and unchanged and not misaligned:
            return store.error_count(c,key)
        offset=checkpoint.get('offset',0);state=checkpoint.get('parser',{})
        rewritten=(checkpoint.get('size')==stat.st_size and checkpoint.get('mtime')!=stat.st_mtime_ns) or misaligned
        if (repair_fork or rewritten or checkpoint.get('inode')!=stat.st_ino or stat.st_size<offset
                or _prefix_changed(path,checkpoint)):
            # Event IDs deduplicate the lines read again.
            offset=0;state={}
            c.execute('DELETE FROM import_errors WHERE source=?',(key,))
        with path.open('rb') as f:
            f.seek(offset)
            while True:
                start=f.tell();line=f.readline()
                if not line:break
                if not line.endswith(b'\n'):
                    f.seek(start);break
                try:parse_line(store,c,route,json.loads(line),state)
                except (ValueError,TypeError,KeyError,AttributeError,OverflowError) as exc:
                    store.import_error(c,key,start,type(exc).__name__)
            if state.get('_dirty'):
                store.rebuild_codex(c,state.pop('_dirty'),
                                    dict(session=state.get('session'),project=state.get('project'),
                                         agent_kind=state.get('agent_kind')))
            end=f.tell();f.seek(max(0,end-TAIL_BYTES));tail=hashlib.sha256(f.read(end-max(0,end-TAIL_BYTES))).hexdigest()
            store.save_state(c,key,dict(parser_version=1,offset=end,tail=tail,size=stat.st_size,mtime=stat.st_mtime_ns,inode=stat.st_ino,parser=state))
            return store.error_count(c,key)

def changed_files(store,root,paths):
    """The record files that need collect_file(): changed since their checkpoint or
    carrying recorded errors. One query replaces a connection per unchanged file,
    which mattered with thousands of finished session files."""
    prefix='file:'+str(root)+'/'
    with store.connect() as c:
        saved={r[0]:r[1] for r in c.execute('SELECT key,data FROM state WHERE substr(key,1,?)=?',(len(prefix),prefix))}
        failing={r[0] for r in c.execute('SELECT DISTINCT source FROM import_errors WHERE substr(source,1,?)=?',(len(prefix),prefix))}
    for path in paths:
        key='file:'+str(path)
        try:stat=path.stat()
        except OSError:continue
        try:checkpoint=json.loads(saved[key]) if key in saved else {}
        except ValueError:checkpoint={}
        unchanged=(checkpoint.get('parser_version',0)>=1 and checkpoint.get('size')==stat.st_size
                   and checkpoint.get('mtime')==stat.st_mtime_ns and checkpoint.get('inode')==stat.st_ino)
        if unchanged and key not in failing:continue
        yield path


def collect_opencode(store,path):
    src=open_readonly(path)
    try:
        with store.connect() as c:
            key='opencode:'+str(path)
            checkpoint=store.state(c,key,0)
            newest=checkpoint
            cols={r[1] for r in src.execute('PRAGMA table_info(message)')}
            has_session='session_id' in cols and src.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='session'").fetchone() is not None
            sel=('SELECT m.id,m.time_created,m.time_updated,m.data,m.session_id,s.directory,s.parent_id '
                 'FROM message m LEFT JOIN session s ON s.id=m.session_id' if has_session else
                 'SELECT id,time_created,time_updated,data,'+('session_id' if 'session_id' in cols else 'NULL')+',NULL,NULL FROM message')
            def ingest(row):
                mid,created,updated,raw,sid,directory,parent_id=row
                try:
                    d=json.loads(raw)
                    if d.get('role')=='assistant' and d.get('tokens'):
                        t=d['tokens'];cache=t.get('cache',{});model=d.get('modelID','unknown')
                        # OpenCode output excludes reasoning, input excludes cache read/write.
                        store.event(c,identity('opencode',mid),created/1000,model_provider(model),d.get('providerID','opencode'),model,
                                    dict(uncached_input=t.get('input',0),cached_input=cache.get('read',0),cache_creation=cache.get('write',0),
                                         output=t.get('output',0)+t.get('reasoning',0),reasoning=t.get('reasoning',0)),
                                    session=sid,project=project_label(directory),
                                    agent_kind='sub' if parent_id else 'main')
                    c.execute('DELETE FROM import_errors WHERE source=? AND record=?',(key,mid))
                except (ValueError,TypeError,KeyError,AttributeError,OverflowError) as exc:
                    store.import_error(c,key,mid,type(exc).__name__)
            # Retry a bounded batch even if a repaired row retains its old time_updated.
            retries=c.execute('SELECT record FROM import_errors WHERE source=? ORDER BY checked LIMIT 200',(key,)).fetchall()
            for failed in retries:
                row=src.execute(sel+' WHERE m.id=?' if has_session else sel+' WHERE id=?',(failed['record'],)).fetchone()
                if row and row[2]<checkpoint:ingest(row)
                elif row is None:
                    # Rotate missing IDs too, so they cannot starve later retries.
                    store.import_error(c,key,failed['record'],'MissingRecord')
            for row in src.execute(sel+(' WHERE m.time_updated>=? ORDER BY m.time_updated' if has_session else ' WHERE time_updated>=? ORDER BY time_updated'),(checkpoint,)):
                newest=max(newest,row[2]);ingest(row)
            store.save_state(c,key,newest)
            return store.error_count(c,key)
    finally:src.close()


def collect_local(store,settings,*,include_derived=True):
    for source in settings['sources']:
        if source['kind'] in ('antigravity','devin'):continue
        root=Path(source['path']);route=source['route'];name=source['name']
        try:
            if not root.exists():
                with store.connect() as c:store.source(c,name,'unavailable','기록 경로를 사용할 수 없습니다.')
                continue
            if source['kind']=='opencode':errors=collect_opencode(store,root);count=1
            else:
                paths=sorted(root.rglob('*.jsonl'))
                count=len(paths)
                errors=sum(collect_file(store,path,route) for path in changed_files(store,root,paths))
                # Errors of the files present now, as the per-file counts used to sum.
                current={'file:'+str(path) for path in paths}
                with store.connect() as c:
                    errors=sum(n for source,n in c.execute('SELECT source,COUNT(*) FROM import_errors WHERE substr(source,1,?)=? GROUP BY source',
                               (len('file:'+str(root)+'/'),'file:'+str(root)+'/')) if source in current)
            with store.connect() as c:
                # File-level failures are single corrupt lines that never recover;
                # after 7 days archive them as a permanent count so the source can
                # return to 'ok' while the loss stays reported.
                aged=c.execute("DELETE FROM import_errors WHERE source LIKE ? AND checked<?",
                               ('file:'+str(root)+'/%',time.time()-7*86400)).rowcount
                perm=store.state(c,'permanent_errors:'+name,0)+aged
                if aged:
                    store.save_state(c,'permanent_errors:'+name,perm)
                    errors=c.execute('SELECT COUNT(*) FROM import_errors WHERE source LIKE ?',
                                     ('file:'+str(root)+'/%',)).fetchone()[0]
                detail=f'{count}개 기록 파일 확인 · 해석 실패 {errors}건'+(f' · 누적 영구 실패 {perm}건' if perm else '')
                store.source(c,name,'partial' if errors else 'ok',detail)
        except Exception as exc:
            with store.connect() as c:store.source(c,name,'error',f'로컬 수집 실패 ({type(exc).__name__})')
    if include_derived:
        from .antigravity import collect_databases
        collect_databases(store,settings,model_provider)
        from .devin import collect_databases as collect_devin
        collect_devin(store,settings,model_provider)
    with store.connect() as c:store.save_state(c,'last_local',time.time())


def collect_antigravity_tokens(store,settings):
    from .statusline import validate_observation,observation_key
    errors=0
    for folder in settings.get('status_inboxes',[]):
        path=Path(folder)/'antigravity-tokens.db'
        if not path.exists():continue
        key='agy-tokens:'+str(path)
        try:
            with store.connect() as c:
                stat=path.stat()
                signature=[stat.st_ino,stat.st_size,stat.st_mtime_ns]
                if store.state(c,key)==signature:continue
                source=sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=2)
                try:
                    for first,last,raw in source.execute('SELECT first_seen,last_seen,data FROM observations ORDER BY last_seen'):
                        if not all(isinstance(v,(int,float)) and 0<v<=time.time()+60 for v in (first,last)) or first>last:
                            raise ValueError('invalid observation time')
                        value=validate_observation(json.loads(raw))
                        c.execute('INSERT INTO agy_token_observations VALUES (?,?,?,?) ON CONFLICT(id) DO UPDATE SET '
                                  'first_seen=MIN(first_seen,excluded.first_seen),last_seen=MAX(last_seen,excluded.last_seen)',
                                  (observation_key(value),first,last,json.dumps(value,sort_keys=True)))
                    store.save_state(c,key,signature)
                finally:source.close()
        except (OSError,sqlite3.Error,ValueError,TypeError):errors+=1
    # This runs on the 2-second loop: prune hourly and rewrite the source row only
    # when its status or text changes, so an idle loop does not write the database.
    with store.connect() as c:
        if time.time()-(store.state(c,'agy-tokens:pruned',0) or 0)>=3600:
            c.execute('DELETE FROM agy_token_observations WHERE last_seen<?',(time.time()-30*86400,))
            store.save_state(c,'agy-tokens:pruned',time.time())
        row=c.execute('SELECT data,last_seen FROM agy_token_observations ORDER BY last_seen DESC LIMIT 1').fetchone()
        if errors:store.source_changed(c,'antigravity-tokens','error','토큰 관측 파일 읽기 실패 · 기존 관측은 유지합니다.')
        elif row:
            value=json.loads(row['data'])
            counters={k:v for k,v in value['totals'].items() if k!='context_window_size'}|value['current']
            numeric=sum(type(v) is int for v in counters.values())
            integrated=c.execute('SELECT 1 FROM antigravity_requests LIMIT 1').fetchone() is not None
            detail=(f'토큰 숫자 {numeric}개 필드 수신 · '+('소비량은 대화 DB에서 별도 집계' if integrated else '소비량 합계 검증 대기') if numeric else
                    '상태줄 수신 · 토큰 숫자 미제공 ('+value['context_state']+')')
            if time.time()-row['last_seen']>600:detail+=' · 오래된 관측'
            store.source_changed(c,'antigravity-tokens',('ok' if integrated else 'partial') if numeric else 'unavailable',detail,row['last_seen'])
        else:store.source_changed(c,'antigravity-tokens','unavailable','토큰 관측 연결됨 · 다음 Antigravity 상태줄 수신 대기')


def collect_status(store,settings):
    collect_antigravity_tokens(store,settings)
    for folder in settings.get('status_inboxes',[]):
        for route in ('claude-code','antigravity'):
            path=Path(folder)/(route+'.json')
            if not path.exists():continue
            try:
                data=json.loads(path.read_text())
                with store.connect() as c:
                    checked=data['checked']
                    if checked<=store.state(c,'status:'+str(path),0):continue
                    count=status_limits(store,c,route,data['data'],checked)
                    store.source(c,route,'ok' if count else 'unavailable','' if count else '최근 상태줄 이벤트에 한도 필드가 없습니다.',checked)
                    store.save_state(c,'status:'+str(path),checked)
            except (OSError,ValueError,KeyError,TypeError,AttributeError):
                with store.connect() as c:store.source(c,route,'error','상태줄 이벤트를 읽지 못했습니다.')


def prepare_normalization(store,settings):
    """Never discard retained history before a complete, reviewable replacement exists."""
    with store.connect() as c:
        version=store.state(c,'normalized_version',0)
        # A full scan; only a pre-v3 store needs the count.
        retained=c.execute("SELECT COUNT(*) FROM events WHERE route IN ('codex','claude-code') OR route LIKE 'codex:%'").fetchone()[0] if version<3 else 0
    if version<3 and retained:
        with tempfile.TemporaryDirectory(prefix='normalization-',dir=Path(store.path).parent) as folder:
            staged=Store(Path(folder)/'usage.db',thresholds=settings.get('thresholds'))
            collect_local(staged,settings,include_derived=False)
            with store.connect() as c:
                c.execute('ATTACH DATABASE ? AS staged',(staged.path,))
                c.execute('BEGIN IMMEDIATE')
                bad=c.execute("SELECT COUNT(*) FROM staged.sources WHERE status!='ok' AND name!='antigravity-records'").fetchone()[0]
                # Every retained observation must survive or have explicit proof that
                # it is inherited. Merely finding a partial copy of a session is unsafe.
                missing=c.execute("""SELECT COUNT(*) FROM events e WHERE
                  (e.route IN ('codex','claude-code') OR e.route LIKE 'codex:%')
                  AND NOT EXISTS (SELECT 1 FROM staged.events n WHERE n.id=e.id)
                  AND NOT EXISTS (SELECT 1 FROM staged.codex_points n WHERE n.id=e.id AND e.route!='claude-code')
                  AND NOT EXISTS (SELECT 1 FROM staged.inherited_points n WHERE n.id=e.id AND e.route!='claude-code')""").fetchone()[0]
                missing+=c.execute('''SELECT COUNT(*) FROM codex_points p
                  WHERE NOT EXISTS (SELECT 1 FROM staged.codex_points n WHERE n.id=p.id)
                  AND NOT EXISTS (SELECT 1 FROM staged.inherited_points n WHERE n.id=p.id)''').fetchone()[0]
                if bad or missing:
                    store.source(c,'normalization','error','원본을 모두 복구할 수 없어 보정을 보류했습니다. 기존 통계를 유지합니다.')
                    return False
                c.execute('DELETE FROM codex_points')
                c.execute('INSERT INTO codex_points SELECT * FROM staged.codex_points')
                c.execute('INSERT OR IGNORE INTO inherited_points SELECT * FROM staged.inherited_points')
                c.execute('DELETE FROM session_quality')
                c.execute("DELETE FROM events WHERE route IN ('codex','claude-code') OR route LIKE 'codex:%'")
                c.execute("INSERT INTO events SELECT * FROM staged.events WHERE route IN ('codex','claude-code') OR route LIKE 'codex:%'")
                c.execute("DELETE FROM state WHERE key LIKE 'file:%' OR key LIKE 'permanent_errors:%'")
                c.execute("DELETE FROM import_errors WHERE source LIKE 'file:%'")
                c.execute("INSERT INTO state SELECT * FROM staged.state WHERE key LIKE 'file:%'")
                store.save_state(c,'normalized_version',3)
                store.bump_data_version(c)
    with store.connect() as c:
        if version<4:
            for row in c.execute('SELECT DISTINCT session FROM codex_points').fetchall():store.rebuild_codex(c,row['session'])
            store.save_state(c,'normalized_version',4)
        store.source(c,'normalization','ok','카운터 초기화 구간을 구분한 집계')
    return True


def _codex_meta(line):
    try:
        d=json.loads(line)
    except ValueError:
        return None
    return d.get('payload',{}) if d.get('type')=='session_meta' else None


def _codex_kind(meta):
    source=meta.get('source')
    if meta.get('thread_source')=='subagent' or (isinstance(source,dict) and source.get('subagent')):return 'sub'
    if meta.get('forked_from_id') or meta.get('parent_thread_id'):return 'fork'
    return 'main'


def backfill_jsonl(store,path,route):
    """Attribute pre-migration events from one JSONL record file."""
    if route.startswith('codex'):
        meta=None
        with path.open('rb') as f:
            for line in f:
                if b'session_meta' not in line:continue
                meta=_codex_meta(line)
                if meta:break
        sid=(meta or {}).get('id') or (meta or {}).get('session_id')
        if not sid:return
        with store.connect() as c:
            c.execute('''UPDATE events SET session=?,project=?,agent_kind=? WHERE session IS NULL
              AND id IN (SELECT id FROM codex_points WHERE session=?)''',
                      (sid,project_label(meta.get('cwd')),_codex_kind(meta),identity(sid)))
            store.bump_data_version(c)
    elif route=='claude-code':
        updates={}
        with path.open('rb') as f:
            for line in f:
                if b'"assistant"' not in line:continue
                try:d=json.loads(line)
                except ValueError:continue
                if d.get('type')!='assistant':continue
                mid=(d.get('message') or {}).get('id')
                if mid and mid not in updates:
                    updates[mid]=(d.get('sessionId'),project_label(d.get('cwd')),
                                  'sub' if d.get('isSidechain') else 'main',identity('claude-code',mid))
        if updates:
            with store.connect() as c:
                c.executemany('UPDATE events SET session=?,project=?,agent_kind=? WHERE id=? AND session IS NULL',
                              list(updates.values()))
                store.bump_data_version(c)


def backfill_opencode(store,path):
    src=open_readonly(path)
    try:
        has_session=src.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='session'").fetchone() is not None
        cols={r[1] for r in src.execute('PRAGMA table_info(message)')}
        rows=src.execute(('SELECT m.id,m.session_id,s.directory,s.parent_id FROM message m '
                          'LEFT JOIN session s ON s.id=m.session_id') if has_session else
                         'SELECT id,'+('session_id' if 'session_id' in cols else 'NULL')+',NULL,NULL FROM message').fetchall()
    finally:src.close()
    with store.connect() as c:
        c.executemany('UPDATE events SET session=?,project=?,agent_kind=? WHERE id=? AND session IS NULL',
                      [(sid,project_label(d),'sub' if p else 'main',identity('opencode',mid))
                       for mid,sid,d,p in rows])
        store.bump_data_version(c)


def backfill_devin(store,settings):
    from .devin import roots, open_db
    for name,path in roots(settings):
        if not path.exists():continue
        src,tmp=open_db(path)
        try:
            has_sessions=src.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='sessions'").fetchone() is not None
            dirs={r['id']:r['working_directory'] for r in src.execute('SELECT id,working_directory FROM sessions')} if has_sessions else {}
            updates=[]
            for row in src.execute('SELECT session_id,chat_message FROM message_nodes'):
                try:d=json.loads(row['chat_message'])
                except (ValueError,TypeError):continue
                if d.get('role')!='assistant':continue
                rid=((d.get('metadata') or {}).get('metrics') or {}) and (d.get('metadata') or {}).get('request_id')
                if not rid:continue
                updates.append((row['session_id'],project_label(dirs.get(row['session_id'])),'main',
                                identity('devin',rid)))
        finally:
            src.close()
            if tmp:
                import shutil;shutil.rmtree(tmp,ignore_errors=True)
        with store.connect() as c:
            c.executemany('UPDATE events SET session=?,project=?,agent_kind=? WHERE id=? AND session IS NULL',updates)
            store.bump_data_version(c)


def backfill_attribution(store,settings):
    """One-time session/project/agent_kind attribution for pre-migration events."""
    with store.connect() as c:
        if store.state(c,'backfill_v2'):return
        store.source(c,'session-backfill','partial','세션·프로젝트 귀속 백필 진행 중')
    failures=[]
    for source in settings.get('sources',[]):
        kind=source.get('kind');route=source.get('route') or '';root=Path(source['path'])
        if kind in ('antigravity','devin') or not root.exists():continue
        try:
            if kind=='opencode':backfill_opencode(store,root)
            elif route=='claude-code' or route.startswith('codex'):
                for path in root.rglob('*.jsonl'):backfill_jsonl(store,path,route)
        except (OSError,sqlite3.Error):
            failures.append(source['name'])
    try:backfill_devin(store,settings)
    except (OSError,sqlite3.Error):failures.append('devin')
    with store.connect() as c:
        if failures:
            # Leave the flag unset so the next collector run retries.
            store.source(c,'session-backfill','error','백필 실패 ('+','.join(failures)+') · 다음 수집에서 재시도')
        else:
            store.save_state(c,'backfill_v2',True)
            store.source(c,'session-backfill','ok','기존 기록의 세션·프로젝트 귀속 완료')


def write_summaries(store,settings):
    """Opt-in (statusline_summary: true): the one-line quota summary the status-line
    wrapper appends below the tool's own line. Inboxes the service cannot write
    (Windows homes under the read-only sandbox) are skipped."""
    from .notify import brief_line
    line=brief_line(store.limits())
    for folder in settings.get('status_inboxes',[]):
        try:
            path=Path(folder);tmp=path/'summary.txt.tmp'
            tmp.write_text(line+'\n');os.replace(tmp,path/'summary.txt')
        except OSError:pass


DAY=86400
RETRY_AFTER_FAILURE=3600


def sd_notify(message):
    """systemd notification without a dependency; a no-op outside systemd.

    With WatchdogSec set on the unit, a loop that stops pinging (a hang, not only
    an exit) is restarted by systemd."""
    address=os.environ.get('NOTIFY_SOCKET')
    if not address:return
    if address.startswith('@'):address='\0'+address[1:]
    try:
        with socket.socket(socket.AF_UNIX,socket.SOCK_DGRAM) as sock:
            sock.connect(address);sock.sendall(message.encode())
    except OSError:pass


def due(last,ok,now):
    """Daily jobs; a failed run is retried after an hour instead of a day."""
    return now-last>=(DAY if ok else RETRY_AFTER_FAILURE)


def sync_thresholds(store,settings,seen):
    """Apply thresholds edited in the web UI (local.json) without a restart.

    Returns the local.json mtime to pass back as `seen` on the next call.
    """
    try:mtime=local_path(settings).stat().st_mtime_ns
    except OSError:mtime=None
    if mtime!=seen:
        store.thresholds={**Store.DEFAULT_THRESHOLDS,**(with_local(settings).get('thresholds') or {})}
    return mtime


def run(settings,once=False):
    store=Store(settings['database'],thresholds=settings.get('thresholds'))
    lock=Path(settings['database']).parent/'collector.lock'
    with lock.open('w') as handle:
        try:fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            if once:return
            fcntl.flock(handle,fcntl.LOCK_EX)
        repair_provider_labels(store)
        retire_legacy_sources(store)
        store.rebuild_rollup()
        last_local=last_limits=0
        with store.connect() as c:
            backup=store.state(c,'backup',{}) or {};verify=store.state(c,'verify',{}) or {}
        last_backup,backup_ok=backup.get('ts',0),backup.get('ok',True)
        last_verify,verify_ok=verify.get('ts',0),verify.get('ok',True)
        local_seen=False
        from .notify import collector_started,evaluate as notify_evaluate,record_failure as notify_failure
        try:collector_started(store,with_local(settings).get('notify'))
        except Exception as exc:notify_failure(store,exc)  # never stops collection
        sd_notify('READY=1')
        while True:
            sd_notify('WATCHDOG=1')
            local_seen=sync_thresholds(store,settings,local_seen)
            collect_status(store,settings)
            with store.connect() as c:
                request=store.state(c,'refresh_request_id',0)
                requested_at=store.state(c,'refresh_requested',0)
                completed=store.state(c,'refresh_completed_id',0)
            now=time.time()
            manual=request>completed
            if once or manual or now-last_local>=300:
                with store.connect() as c:store.save_state(c,'collector',dict(status='collecting',started=now))
                if prepare_normalization(store,settings):
                    backfill_attribution(store,settings)
                    collect_local(store,settings)
                last_local=time.time()
            if once or manual or now-last_limits>=300:
                poll_limits(store,settings,manual=manual);last_limits=time.time()
                if with_local(settings).get('statusline_summary'):write_summaries(store,settings)
                if not once:
                    try:
                        from .signals import usage_spike
                        current=time.time()
                        from .pricing import load_pricing
                        local=with_local(settings)
                        with store.connect() as c:
                            spike=usage_spike(c,current)
                            sources=[dict(r) for r in c.execute('SELECT name,status,detail FROM sources')]
                            month=store.project_month(c,current,load_pricing(local))
                        notify_evaluate(store,local.get('notify'),current,store.limits(current),spike,sources,month,local.get('project_budgets'))
                        from .reports import ensure_report,summary as report_summary
                        weekly=ensure_report(store,current,load_pricing(local))
                        if weekly:
                            from .notify import deliver
                            deliver(store,local.get('notify'),[('report',f"주간 리포트 {weekly['start']} ~ {weekly['end']}",report_summary(weekly))],current)
                    except Exception as exc:notify_failure(store,exc)  # never stops collection
            sd_notify('WATCHDOG=1')
            if not once and due(last_backup,backup_ok,now):
                from .backup import backup_now
                result=backup_now(settings['database'])
                with store.connect() as c:
                    store.save_state(c,'backup',result)
                    # Refresh the planner's statistics once a day; cheap when nothing changed.
                    c.execute('PRAGMA optimize')
                last_backup=time.time();backup_ok=bool(result.get('ok'))
            sd_notify('WATCHDOG=1')
            if not once and due(last_verify,verify_ok,now):
                from .verify import failed,reconcile
                try:
                    result=reconcile(settings['database'],settings)
                    sections={section:{k:v for k,v in counts.items() if k!='mismatch_samples'}
                              for section,counts in result.items()}
                    # The hourly rollup must equal the events it summarises; rebuild it if not.
                    rollup_ok=store.rollup_matches()
                    if not rollup_ok:store.rebuild_rollup()
                    outcome=dict(ts=time.time(),ok=not failed(result) and rollup_ok,rollup=dict(matches=rollup_ok),**sections)
                except Exception as e:outcome=dict(ts=time.time(),ok=False,error=str(e))
                with store.connect() as c:store.save_state(c,'verify',outcome)
                last_verify=time.time();verify_ok=bool(outcome.get('ok'))
            with store.connect() as c:
                store.save_state(c,'collector',dict(status='idle',checked=time.time()))
                # A request arriving during these calls stays pending for the next loop.
                if manual:
                    store.save_state(c,'refresh_completed_id',request)
                    store.save_state(c,'refresh_completed',requested_at)
                store.prune_history(c)
            if once:return
            time.sleep(2)
