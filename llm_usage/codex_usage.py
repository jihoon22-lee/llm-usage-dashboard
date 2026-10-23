"""Numeric Codex response evidence and its links to legacy counter observations.

Response IDs identify consumption; cumulative counters remain retained evidence.
Only an explicitly linked legacy observation is replaced in the effective events.
"""
import json
import re
import sqlite3

from .store import identity

FIELDS=('input_tokens','cached_input_tokens','output_tokens','cache_write_input_tokens','reasoning_output_tokens')


def initialize(c):
    c.executescript('''
      CREATE TABLE IF NOT EXISTS codex_responses (
        id TEXT PRIMARY KEY, session TEXT NOT NULL, ts REAL NOT NULL,
        provider TEXT NOT NULL, route TEXT NOT NULL, model TEXT NOT NULL,
        usage TEXT NOT NULL, cumulative TEXT NOT NULL, original_session TEXT NOT NULL,
        project TEXT, agent_kind TEXT, account TEXT, conflict INTEGER NOT NULL DEFAULT 0,
        projected INTEGER NOT NULL DEFAULT 0);
      CREATE INDEX IF NOT EXISTS codex_responses_session ON codex_responses(session,ts);
      CREATE TABLE IF NOT EXISTS codex_response_links (
        response_id TEXT NOT NULL, point_id TEXT NOT NULL, PRIMARY KEY(response_id,point_id));
      CREATE INDEX IF NOT EXISTS codex_response_link_point ON codex_response_links(point_id);
    ''')
    for table in ('codex_points','codex_responses'):
        if 'reasoning_observed' not in {r['name'] for r in c.execute(f'PRAGMA table_info({table})')}:
            try:c.execute(f'ALTER TABLE {table} ADD COLUMN reasoning_observed INTEGER NOT NULL DEFAULT 0')
            except sqlite3.OperationalError as exc:
                if 'duplicate column' not in str(exc):raise


def counters(value):
    if not isinstance(value,dict) or not all(k in value for k in ('input_tokens','output_tokens')):
        raise ValueError('incomplete response usage')
    numbers=tuple(value.get(k,0) for k in FIELDS)
    if not all(type(v) is int and 0<=v<=2**63-1 for v in numbers):
        raise ValueError('invalid response counter')
    inp,cached,out,creation,reasoning=numbers
    if inp<cached+creation or out<reasoning:raise ValueError('inconsistent response components')
    return numbers


def record(store,c,payload,ts,state,provider,route):
    rid=payload.get('response_id')
    if not isinstance(rid,str) or not re.fullmatch(r'[A-Za-z0-9._:/-]{1,200}',rid):
        raise ValueError('invalid response identity')
    nums=counters(payload.get('usage'))
    observed=int(payload['usage'].get('reasoning_output_tokens') is not None)
    total=counters(payload.get('thread_token_usage') or payload.get('usage'))
    key=identity('codex-response',route,rid);session=identity(state['session'])
    old=c.execute('SELECT * FROM codex_responses WHERE id=?',(key,)).fetchone()
    if old:
        # A stable response must not silently change ownership or numeric content.
        if old['session']!=session or tuple(json.loads(old['usage']))!=nums:
            c.execute('UPDATE codex_responses SET conflict=1 WHERE id=?',(key,))
            raise ValueError('conflicting response observation')
        if old['model']=='unknown' and state.get('model'):
            c.execute('UPDATE codex_responses SET model=?,provider=? WHERE id=?',(state['model'],provider,key))
            state['_dirty']=session
        if observed and not old['reasoning_observed']:
            c.execute('UPDATE codex_responses SET reasoning_observed=1 WHERE id=?',(key,));state['_dirty']=session
    else:
        c.execute('INSERT INTO codex_responses VALUES (?,?,?,?,?,?,?,?,?,?,?,?,0,0,?)',
                  (key,session,ts,provider,route,state.get('model') or 'unknown',json.dumps(nums),json.dumps(total),
                   state['session'],state.get('project'),state.get('agent_kind'),state.get('account'),observed))
        state['_dirty']=session
    from .reconciliation import aliases
    aliases(c,route,key,rid)
    pending=state.setdefault('pending_responses',[])
    if key not in pending:pending.append(key)
    return key


def bind_pending(c,state,point_id):
    changed=False
    for rid in state.pop('pending_responses',[]):
        changed=bool(c.execute('INSERT OR IGNORE INTO codex_response_links VALUES (?,?)',(rid,point_id)).rowcount) or changed
    if changed:state['_dirty']=identity(state['session'])


def project(store,c,session,groups,projects):
    """Choose request evidence once, leaving unlinked legacy history intact.

    A late response can match a unique nearby original counter by its exact
    components. Ambiguous overlap is held rather than guessed or double counted.
    """
    responses=c.execute('SELECT * FROM codex_responses WHERE session=? ORDER BY ts,id',(session,)).fetchall()
    if not responses:return
    points=c.execute('SELECT * FROM codex_points WHERE session=? ORDER BY ts,id',(session,)).fetchall()
    point_times={p['id']:p['ts'] for p in points}
    retained_projects=dict(c.execute('SELECT e.id,e.project FROM events e JOIN codex_responses r ON r.id=e.id WHERE r.session=?',(session,)))
    linked={r['id']:set() for r in responses}
    for rid,pid in c.execute('SELECT l.response_id,l.point_id FROM codex_response_links l '
                             'JOIN codex_responses r ON r.id=l.response_id WHERE r.session=?',(session,)):
        if pid in groups:linked[rid].add(pid)
    current_keys=('input','cached','output','creation','reasoning')
    last_keys=tuple('last_'+k for k in current_keys)
    low,high=(points[0]['ts'],points[-1]['ts']) if points else (None,None)
    active=[];covered=set()
    for row in responses:
        ids=linked[row['id']]
        if not ids:
            nums=tuple(json.loads(row['usage']));total=tuple(json.loads(row['cumulative']))
            nearby=[p for p in points if abs(p['ts']-row['ts'])<=2 and
                    (tuple(p[k] for k in current_keys)==total or tuple(p[k] for k in last_keys)==nums)]
            if len(nearby)==1:
                pid=nearby[0]['id'];ids.add(pid)
                c.execute('INSERT OR IGNORE INTO codex_response_links VALUES (?,?)',(row['id'],pid))
        overlap=not ids and low is not None and low-2<=row['ts']<=high+2
        if overlap:
            c.execute('DELETE FROM events WHERE id=?',(row['id'],))
            c.execute('UPDATE codex_responses SET projected=0 WHERE id=?',(row['id'],))
            continue
        # A new response can arrive while the legacy counter repeats an older
        # baseline. That older request is NOT replaced by this later response.
        parents={groups[pid] for pid in ids if groups[pid] and
                 (groups[pid]==pid or row['ts']<=point_times[groups[pid]])}
        covered.update(parents)
        old_projects={projects[p] for p in parents if p in projects}
        project_name=next(iter(old_projects)) if len(old_projects)==1 else retained_projects.get(row['id'],row['project'])
        active.append((row,project_name))
    c.executemany('DELETE FROM events WHERE id=?',[(key,) for key in covered])
    for row,project_name in active:
        inp,cached,out,creation,reasoning=json.loads(row['usage'])
        store.event(c,row['id'],row['ts'],row['provider'],row['route'],row['model'],
                    dict(uncached_input=inp-cached-creation,cached_input=cached,output=out,
                         cache_creation=creation,reasoning=reasoning,reasoning_observed=row['reasoning_observed']),session=row['original_session'],
                    project=project_name,agent_kind=row['agent_kind'],account=row['account'])
        c.execute('UPDATE codex_responses SET projected=1 WHERE id=?',(row['id'],))


def summary(store,c):
    row=c.execute('SELECT COUNT(*),COALESCE(SUM(projected=0),0),COALESCE(SUM(conflict),0) FROM codex_responses').fetchone()
    if not row[0]:return
    store.source_changed(c,'codex-responses','partial' if row[1] or row[2] else 'ok',
                         f'요청별 원본 {row[0]}개 · 누계 연결 대기 {row[1]}개 · 상충 관측 {row[2]}개')
