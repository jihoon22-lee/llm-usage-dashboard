"""Only numeric usage, opaque identities and collection metadata enter SQLite."""
from collections import OrderedDict
from contextlib import contextmanager
from datetime import datetime, timedelta
import functools
from zoneinfo import ZoneInfo
import hashlib
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import sqlite3
import threading
import time

KST = ZoneInfo('Asia/Seoul')
TOKENS = ('uncached_input', 'cached_input', 'output', 'cache_creation', 'reasoning')
SERIES_KEYS = (*TOKENS, 'requests', 'cost', 'unpriced')
HISTORY_FIELDS = ('checked', 'remaining', 'resets', 'break_before', 'break_reason')
# Sources fed by a terminal status line rather than by polling.
STATUS_LINE_SOURCES = {'claude-code', 'antigravity', 'antigravity-tokens'}
# (route, shorter-window bucket) -> the longer window that also gates usage.
QUOTA_PARENTS = {('claude-code', 'five_hour'): 'seven_day', ('devin', 'daily'): 'weekly',
                 ('antigravity', 'gemini-5h'): 'gemini-weekly', ('antigravity', '3p-5h'): '3p-weekly'}
BUCKET_SQL = {
    'hour':'CAST((ts+32400)/3600 AS INTEGER)*3600-32400',
    'day':'CAST((ts+32400)/86400 AS INTEGER)*86400-32400',
    'week':"CAST((ts+32400)/86400 AS INTEGER)*86400-32400-((CAST(strftime('%w',ts,'unixepoch','+9 hours') AS INTEGER)+6)%7)*86400",
    'month':"CAST(strftime('%s',ts,'unixepoch','+9 hours','start of month') AS INTEGER)-32400",
}


def calendar_buckets(start,end,granularity):
    if end<=start:raise ValueError('시작·종료일이 올바르지 않습니다.')
    cursor=start.replace(minute=0,second=0,microsecond=0)
    if granularity!='hour':cursor=cursor.replace(hour=0)
    if granularity=='week':cursor-=timedelta(days=cursor.weekday())
    elif granularity=='month':cursor=cursor.replace(day=1)
    result=[]
    while cursor<end:
        if len(result)>=50000:raise ValueError('기간이 너무 깁니다.')
        if granularity=='month':
            following=cursor.replace(year=cursor.year+1,month=1) if cursor.month==12 else cursor.replace(month=cursor.month+1)
        else:following=cursor+timedelta(seconds={'hour':3600,'day':86400,'week':604800}[granularity])
        result.append((cursor,following));cursor=following
    return result


def stamp(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return value / 1000 if value > 1e11 else float(value)
    return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()


def identity(*parts):
    return hashlib.sha256(json.dumps(parts, sort_keys=True).encode()).hexdigest()


GENERIC_FOLDERS=('','projects','home','mnt','~')


@functools.lru_cache(maxsize=4096)
def repository_root(cwd):
    """Main repository folder for a path inside a git checkout or linked worktree.

    A linked worktree's .git file points at <main>/.git/worktrees/<name>, so its
    work is attributed to <main>. A Claude Code agent worktree that has already
    been removed is recognised by its <repo>/.claude/worktrees/<name> path."""
    text=str(cwd).rstrip('/')
    if '/.claude/worktrees/' in text:return text.split('/.claude/worktrees/',1)[0]
    # Only absolute POSIX paths are looked up; others (E:\\x) would resolve against our cwd.
    if not text.startswith('/'):return None
    path=Path(text)
    try:
        for folder in (path,*path.parents):
            marker=folder/'.git'
            if marker.is_dir():return str(folder)
            if marker.is_file():
                line=marker.read_text(errors='replace').strip()
                gitdir=Path(line[7:].strip()) if line.startswith('gitdir:') else None
                if gitdir and not gitdir.is_absolute():gitdir=folder/gitdir
                if gitdir and gitdir.parent.name=='worktrees' and gitdir.parent.parent.name=='.git':
                    return str(gitdir.parent.parent.parent)
                return str(folder)
    except OSError:pass
    return None


def project_label(cwd):
    """Store a basename only; existing database labels are deliberately not migrated."""
    if not cwd:return None
    text=str(cwd)
    windows='\\' in text or bool(re.match(r'^[A-Za-z]:',text)) or text.startswith('//')
    if windows:
        path=PureWindowsPath(text)
        # Historical agent worktree paths can be grouped without touching a Windows disk.
        normalized=path.as_posix()
        if '/.claude/worktrees/' in normalized:path=PureWindowsPath(normalized.split('/.claude/worktrees/',1)[0])
        name=path.name
    else:
        root=repository_root(text)
        name=PurePosixPath(root or text).name
        if root and name in GENERIC_FOLDERS:name=PurePosixPath(text).name
    return name if name not in ('','.','..','~') else None



class Store:
    DEFAULT_THRESHOLDS={'stale_seconds':600,'retention_days':30,'low_percent':15,'quota_hide_days':7}

    def __init__(self, path, thresholds=None, rollup=True):
        self.path = str(path)
        # Whole-hour aggregations read usage_hourly; False reads events (equivalence checks).
        self.table='usage_hourly' if rollup else 'events'
        self.rcount='COALESCE(SUM(requests),0) AS requests' if rollup else 'COUNT(*) AS requests'
        self.thresholds={**self.DEFAULT_THRESHOLDS,**(thresholds or {})}
        self._memo={}
        self._usage_cache=OrderedDict()
        # gunicorn serves requests on several threads that share this Store.
        self._usage_lock=threading.Lock()
        Path(path).parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.connect() as c:
            c.executescript('''
            CREATE TABLE IF NOT EXISTS events (
              id TEXT PRIMARY KEY, ts REAL NOT NULL, provider TEXT NOT NULL,
              route TEXT NOT NULL, model TEXT NOT NULL,
              uncached_input INTEGER NOT NULL, cached_input INTEGER NOT NULL,
              output INTEGER NOT NULL, cache_creation INTEGER NOT NULL, reasoning INTEGER NOT NULL,
              session TEXT, project TEXT, agent_kind TEXT);
            CREATE INDEX IF NOT EXISTS events_time ON events(ts);
            CREATE TABLE IF NOT EXISTS codex_points (
              id TEXT PRIMARY KEY, session TEXT NOT NULL, ts REAL NOT NULL,
              provider TEXT NOT NULL, route TEXT NOT NULL, model TEXT NOT NULL,
              input INTEGER, cached INTEGER, output INTEGER, creation INTEGER, reasoning INTEGER,
              last_input INTEGER, last_cached INTEGER, last_output INTEGER, last_creation INTEGER, last_reasoning INTEGER);
            CREATE INDEX IF NOT EXISTS codex_session ON codex_points(session,ts);
            CREATE TABLE IF NOT EXISTS antigravity_requests (
              id TEXT PRIMARY KEY, ts REAL NOT NULL, model TEXT NOT NULL, data TEXT NOT NULL, conflict INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS antigravity_request_aliases (alias TEXT PRIMARY KEY, request_id TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS antigravity_alias_request ON antigravity_request_aliases(request_id);
            CREATE TABLE IF NOT EXISTS agy_token_observations (
              id TEXT PRIMARY KEY, first_seen REAL, last_seen REAL, data TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS agy_token_observations_time ON agy_token_observations(last_seen);
            CREATE TABLE IF NOT EXISTS inherited_points (id TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS limits (
              route TEXT, bucket TEXT, remaining REAL, resets REAL, checked REAL,
              source TEXT, PRIMARY KEY(route,bucket));
            CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS sources (name TEXT PRIMARY KEY, checked REAL,
              status TEXT, detail TEXT);
            CREATE TABLE IF NOT EXISTS source_health (name TEXT PRIMARY KEY, last_success REAL);
            CREATE TABLE IF NOT EXISTS import_errors (
              source TEXT, record TEXT, kind TEXT, attempts INTEGER, checked REAL,
              PRIMARY KEY(source,record));
            CREATE TABLE IF NOT EXISTS session_quality (
              session TEXT PRIMARY KEY, resets INTEGER, partial INTEGER,
              ambiguous INTEGER, zero_baselines INTEGER);
            CREATE TABLE IF NOT EXISTS limit_history (
              route TEXT, bucket TEXT, checked REAL, remaining REAL, resets REAL, source TEXT,
              PRIMARY KEY(route,bucket,checked,source));
            CREATE INDEX IF NOT EXISTS limit_history_time ON limit_history(checked);
            CREATE TABLE IF NOT EXISTS quota_interruptions (
              source TEXT, checked REAL, status TEXT, PRIMARY KEY(source,checked));
            CREATE INDEX IF NOT EXISTS quota_interruptions_time ON quota_interruptions(checked);
            ''')
            for column in ('session','project','agent_kind'):
                if column not in {r['name'] for r in c.execute('PRAGMA table_info(events)')}:
                    c.execute(f'ALTER TABLE events ADD COLUMN {column} TEXT')
            c.execute('CREATE INDEX IF NOT EXISTS events_session ON events(session)')
            # Quota windows and per-route scopes filter one route over a time range.
            c.execute('CREATE INDEX IF NOT EXISTS events_route_ts ON events(route,ts)')
        self._ensure_rollup()
        os.chmod(path, 0o600)

    def _ensure_rollup(self):
        """Hourly sums of events, kept by triggers so every write path (upserts, codex
        rebuilds, Antigravity merges, attribution backfills) updates it in the same
        transaction. Aggregations over whole hours read it instead of every event.

        ts is the hour start (KST is a whole-hour offset, so hours, days, weeks and
        months group exactly as on events); requests is the event count."""
        key=",".join(['ts','provider','route','model',"IFNULL(project,char(0))","IFNULL(agent_kind,char(0))"])
        match=' AND '.join(['ts=CAST(OLD.ts/3600 AS INTEGER)*3600','provider=OLD.provider','route=OLD.route','model=OLD.model',
                            'project IS OLD.project','agent_kind IS OLD.agent_kind'])
        add=f"""INSERT INTO usage_hourly VALUES (CAST(NEW.ts/3600 AS INTEGER)*3600,NEW.provider,NEW.route,NEW.model,NEW.project,NEW.agent_kind,
              {','.join('NEW.'+k for k in TOKENS)},1) ON CONFLICT({key}) DO UPDATE SET
              {','.join(f'{k}={k}+excluded.{k}' for k in TOKENS)},requests=requests+1;"""
        remove=f"""UPDATE usage_hourly SET {','.join(f'{k}={k}-OLD.{k}' for k in TOKENS)},requests=requests-1 WHERE {match};
              DELETE FROM usage_hourly WHERE {match} AND requests<=0;"""
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')  # the web and the collector may start together
            if self.state(c,'rollup_version')==1:return
            for statement in ['DROP TABLE IF EXISTS usage_hourly',
                f"""CREATE TABLE usage_hourly (ts INTEGER NOT NULL, provider TEXT NOT NULL, route TEXT NOT NULL, model TEXT NOT NULL,
                    project TEXT, agent_kind TEXT, {','.join(k+' INTEGER NOT NULL' for k in TOKENS)}, requests INTEGER NOT NULL)""",
                f'CREATE UNIQUE INDEX usage_hourly_key ON usage_hourly({key})',
                'CREATE INDEX usage_hourly_route_ts ON usage_hourly(route,ts)',
                'DROP TRIGGER IF EXISTS events_rollup_insert','DROP TRIGGER IF EXISTS events_rollup_delete','DROP TRIGGER IF EXISTS events_rollup_update',
                f'CREATE TRIGGER events_rollup_insert AFTER INSERT ON events BEGIN {add} END',
                f'CREATE TRIGGER events_rollup_delete AFTER DELETE ON events BEGIN {remove} END',
                f'CREATE TRIGGER events_rollup_update AFTER UPDATE ON events BEGIN {remove} {add} END',
                f"""INSERT INTO usage_hourly SELECT CAST(ts/3600 AS INTEGER)*3600,provider,route,model,project,agent_kind,
                    {','.join(f'SUM({k})' for k in TOKENS)},COUNT(*) FROM events GROUP BY 1,2,3,4,5,6"""]:
                c.execute(statement)
            self.save_state(c,'rollup_version',1)

    ROLLUP_SELECT=('SELECT CAST(ts/3600 AS INTEGER)*3600,provider,route,model,project,agent_kind,'
                   +','.join(f'SUM({k})' for k in TOKENS)+',COUNT(*) FROM events GROUP BY 1,2,3,4,5,6')

    def rebuild_rollup(self):
        """Recompute usage_hourly from events in one transaction. The collector does this
        at start: a process running older code (no recursive triggers) may have written
        events while the triggers already existed."""
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            c.execute('DELETE FROM usage_hourly')
            c.execute('INSERT INTO usage_hourly '+self.ROLLUP_SELECT)

    def rollup_matches(self):
        """True when usage_hourly equals a fresh aggregation of events (daily self-check)."""
        with self.connect() as c:
            c.execute('BEGIN')
            fresh=set(map(tuple,c.execute(self.ROLLUP_SELECT)))
            kept=set(map(tuple,c.execute('SELECT * FROM usage_hourly')))
        return fresh==kept

    @contextmanager
    def connect(self):
        c = sqlite3.connect(self.path, timeout=15)
        c.row_factory = sqlite3.Row
        c.execute('PRAGMA journal_mode=WAL')
        # REPLACE deletes the conflicting events row; only with recursive triggers does
        # that deletion run the rollup's delete trigger (otherwise it would count twice).
        c.execute('PRAGMA recursive_triggers=ON')
        try:
            with c:
                yield c
        finally:
            c.close()

    def event(self, c, key, ts, provider, route, model, values, session=None, project=None, agent_kind=None):
        # Streaming snapshots are monotonic maxima per globally stable request ID.
        numbers = [max(0, int(values.get(k) or 0)) for k in TOKENS]
        if not any(numbers):return
        c.execute('INSERT INTO events (id,ts,provider,route,model,uncached_input,cached_input,output,cache_creation,reasoning,session,project,agent_kind)'
                  ' VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET '
                  + ','.join(f'{k}=MAX(events.{k},excluded.{k})' for k in TOKENS)
                  + ',provider=excluded.provider,model=excluded.model,route=excluded.route'
                  + ',session=COALESCE(excluded.session,events.session)'
                  + ',project=COALESCE(excluded.project,events.project)'
                  + ',agent_kind=COALESCE(excluded.agent_kind,events.agent_kind)',
                  (key, ts, provider, route, model or 'unknown', *numbers, session, project, agent_kind))

    def codex_point(self,c,session,ts,provider,route,model,usage,last):
        keys=('input_tokens','cached_input_tokens','output_tokens','cache_write_input_tokens','reasoning_output_tokens')
        totals=[max(0,int(usage.get(k) or 0)) for k in keys]
        previous=[max(0,int(last.get(k) or 0)) for k in keys]
        # Timestamp and original cumulative counters survive Windows/WSL copies and replay.
        key=identity('codex-point',session,ts,totals)
        inserted=c.execute('INSERT INTO codex_points VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) '
                           'ON CONFLICT(id) DO UPDATE SET model=excluded.model,provider=excluded.provider '
                           "WHERE codex_points.model='unknown' AND excluded.model!='unknown'",
                           (key,identity(session),ts,provider,route,model or 'unknown',*totals,*previous)).rowcount
        return identity(session) if inserted else None

    def discard_codex_point(self,c,session,ts,usage):
        # Remove only this proven inherited observation, preserving other copies/history.
        keys=('input_tokens','cached_input_tokens','output_tokens','cache_write_input_tokens','reasoning_output_tokens')
        totals=[max(0,int(usage.get(k) or 0)) for k in keys]
        key=identity('codex-point',session,ts,totals)
        c.execute('INSERT OR IGNORE INTO inherited_points VALUES (?)',(key,))
        c.execute('DELETE FROM events WHERE id=?',(key,))
        removed=c.execute('DELETE FROM codex_points WHERE id=?',(key,)).rowcount
        return identity(session) if removed else None

    def rebuild_codex(self,c,session,attrs=None):
        c.execute('DELETE FROM events WHERE id IN (SELECT id FROM codex_points WHERE session=?)',(session,))
        previous=None;previous_ts=None;seen={};resets=0;partial=False
        zero_baseline=False;zero_baselines=0;ambiguous=0
        for row in c.execute('SELECT * FROM codex_points WHERE session=? ORDER BY ts,input,output,id',(session,)).fetchall():
            current=[row[k] for k in ('input','cached','output','creation','reasoning')]
            last=[row[k] for k in ('last_input','last_cached','last_output','last_creation','last_reasoning')]
            # Repeated notifications are duplicates only within this counter epoch.
            # A reset may legitimately reach counters seen earlier in the session.
            if current==previous:continue
            reset=previous is not None and (current[0]<previous[0] or current[2]<previous[2])
            # Two different counters at the same timestamp have no temporal order.
            # A later repeat of that lower observation is not proof of a new epoch.
            if reset and seen.get(tuple(current))==previous_ts:continue
            if previous is None:partial=current!=last
            if reset:resets+=1
            if previous_ts==row['ts']:ambiguous+=1
            # A partial copy starts from the known last request, then converges when full history arrives.
            if previous is None or reset:
                delta=last
            else:delta=[max(0,a-b) for a,b in zip(current,previous)]
            if not any(last):
                # A zero-request snapshot can restore a different cumulative baseline.
                # It is not evidence of consumption, even when its total jumps upward.
                delta=last;zero_baseline=True;zero_baselines+=1
            else:
                if zero_baseline and delta!=last:
                    delta=last;ambiguous+=1
                zero_baseline=False
            previous=current;previous_ts=row['ts'];seen[tuple(current)]=row['ts']
            if not any(delta):continue
            inp,cached,out,creation,reasoning=delta
            self.event(c,row['id'],row['ts'],row['provider'],row['route'],row['model'],
                       dict(uncached_input=max(0,inp-cached-creation),cached_input=cached,output=out,
                            cache_creation=creation,reasoning=reasoning),**(attrs or {}))
        c.execute('INSERT OR REPLACE INTO session_quality VALUES (?,?,?,?,?)',(session,resets,int(partial),ambiguous,zero_baselines))
        self.bump_data_version(c)

    def import_error(self,c,source,record,kind):
        c.execute('''INSERT INTO import_errors VALUES (?,?,?,1,?) ON CONFLICT(source,record)
          DO UPDATE SET kind=excluded.kind,attempts=attempts+1,checked=excluded.checked''',
          (source,str(record),kind,time.time()))

    def error_count(self,c,source):
        return c.execute('SELECT COUNT(*) FROM import_errors WHERE source=?',(source,)).fetchone()[0]

    def state(self, c, key, default=None):
        row = c.execute('SELECT data FROM state WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def save_state(self, c, key, data):
        c.execute('INSERT OR REPLACE INTO state VALUES (?,?)', (key, json.dumps(data)))

    def source(self, c, name, status, detail='', checked=None):
        c.execute('INSERT INTO sources VALUES (?,?,?,?) ON CONFLICT(name) DO UPDATE SET '
                  'checked=excluded.checked,status=excluded.status,detail=excluded.detail '
                  'WHERE excluded.checked>=sources.checked',
                  (name, checked or time.time(), status, detail))
        if status=='ok':
            c.execute('''INSERT INTO source_health VALUES (?,?) ON CONFLICT(name)
              DO UPDATE SET last_success=MAX(last_success,excluded.last_success)''',(name,checked or time.time()))
        if name in ('codex','claude-code','claude-oauth','antigravity','antigravity-app','opencode-go','devin') and status in ('error','unavailable'):
            c.execute('INSERT OR REPLACE INTO quota_interruptions VALUES (?,?,?)',(name,checked or time.time(),status))

    def source_changed(self,c,name,status,detail='',checked=None):
        """source() for frequent callers: skip the write when nothing would change.

        Without an explicit time only a status or text change is written; the stored
        time then stays the moment the current state began."""
        row=c.execute('SELECT status,detail,checked FROM sources WHERE name=?',(name,)).fetchone()
        if row and row['status']==status and row['detail']==detail and (checked is None or row['checked']==checked):return
        self.source(c,name,status,detail,checked)

    def prune_history(self,c):
        now=time.time()
        if now-self.state(c,'history_pruned',0)>=86400:
            retention=now-self.thresholds['retention_days']*86400
            c.execute('DELETE FROM limit_history WHERE checked<?',(retention,))
            c.execute('DELETE FROM quota_interruptions WHERE checked<?',(retention,))
            self.save_state(c,'history_pruned',now)

    def limit(self, c, route, bucket, remaining, resets, checked, source):
        resets=stamp(resets)
        c.execute('''INSERT INTO limits VALUES (?,?,?,?,?,?) ON CONFLICT(route,bucket)
        DO UPDATE SET remaining=excluded.remaining,resets=excluded.resets,checked=excluded.checked,
        source=excluded.source WHERE excluded.checked >= limits.checked''',
                  (route, bucket, remaining, resets, checked, source))
        # Local replay must not manufacture a historical stream at migration time.
        # Keep only observations made within the retention window.
        cutoff=time.time()-self.thresholds['retention_days']*86400
        if checked>=cutoff:
            c.execute('INSERT OR IGNORE INTO limit_history VALUES (?,?,?,?,?,?)',
                      (route,bucket,checked,remaining,resets,source))
        self.prune_history(c)

    def limits(self, now=None):
        from .insights import quota_history,quota_pace,quota_plan,quota_trends,quota_decreases,quota_forecast,pace_lookback,window_seconds,token_total
        now = now or time.time()
        with self.connect() as c:
            c.execute('BEGIN')
            rows = [dict(r) for r in c.execute('SELECT * FROM limits ORDER BY route,bucket')]
            sources = {r['name']:dict(r) for r in c.execute('SELECT * FROM sources')}
            for row in rows:
                row['display_name']=self.state(c,'label:'+row['route']+':'+row['bucket'],row['bucket'])
            history=quota_history(c,now)
        routes = ('codex','claude-code','antigravity','opencode-go','devin')
        for route in routes:
            if not any(r['route']==route for r in rows):
                rows.append(dict(route=route,bucket='미제공',remaining=None,resets=None,checked=None,source=route))
        for row in rows:
            checked, resets = row['checked'], row['resets']
            src = sources.get(row['source'], {})
            account=sources.get('claude-oauth',{}) if row['route']=='claude-code' else {}
            if account.get('checked',0)>=(row['checked'] or 0) and account.get('status') in ('error','unavailable'):
                src=account
            stale = bool(checked and (now-checked > self.thresholds['stale_seconds'] or (resets and resets <= now) or src.get('status')=='unavailable'))
            row.update(status='error' if src.get('status')=='error' else 'ended' if src.get('status')=='ended' else 'unavailable' if row['remaining'] is None or src.get('status')=='unavailable' else 'stale' if stale else 'fresh',
                       stale=stale, reset_kst=datetime.fromtimestamp(resets,KST).isoformat() if resets else None,
                       seconds_to_reset=max(0,int(resets-now)) if resets else None,
                       detail=src.get('detail',''),last_attempt=src.get('checked'))
            row['history']=history.get((row['route'],row['bucket']),[])
            pace=quota_pace(row['history'],now,row['status'],pace_lookback(row['bucket'])[0])
            row['pace_per_hour']=pace[0] if pace else None
            row['pace_minutes']=pace[1] if pace else None
            row['trends']=quota_trends(row['history'],now,row['status'])
            row['decreases']=quota_decreases(row['history'],now)
            row['history_source']=row['history'][-1]['source'] if row['history'] else None
            row['forecast']=quota_forecast(row,now)
            row['plan']=quota_plan(row,now)
            row['window']=None
            row['window_seconds']=window_seconds(row['bucket'])
            # Per-point route/bucket/source repeat the row; keep what the chart reads.
            row['history']=[{k:p[k] for k in HISTORY_FIELDS} for p in row['history']]
        # A shorter window cannot be used while the longer window of the same quota is
        # exhausted. Only a current observation blocks: a stale or expired upper value
        # is not carried onto another bucket, and the lower bucket's value is kept.
        by_key={(r['route'],r['bucket']):r for r in rows}
        for row in rows:
            parent=by_key.get((row['route'],QUOTA_PARENTS.get((row['route'],row['bucket']))))
            blocked=parent and parent['status']=='fresh' and parent['remaining'] is not None and parent['remaining']<=0
            row['blocked_by']={k:parent[k] for k in ('bucket','resets','seconds_to_reset')} if blocked else None
            # The desktop app reports only the window that currently binds a model family,
            # so the other window keeps its last observation; say why it is not current.
            agy=re.fullmatch(r'(gemini|3p)-(5h|weekly)',row['bucket']) if row['route']=='antigravity' else None
            other=agy and by_key.get(('antigravity',f"{agy[1]}-{'weekly' if agy[2]=='5h' else '5h'}"))
            row['note']=(f"앱이 지금 {'주간' if agy[2]=='5h' else '5시간'} 창을 보고하고 있어 이 창의 현재값은 확인할 수 없습니다. 마지막 관측값입니다."
                         if other and row['status']!='fresh' and other['status']=='fresh' and other['source']=='antigravity-app' else None)
        sums=','.join(f'COALESCE(SUM({k}),0) AS {k}' for k in TOKENS)+',COUNT(*) AS requests'
        with self.connect() as c:
            for row in rows:
                span=window_seconds(row['bucket'])
                if not row['resets'] or not span:continue
                start,end=row['resets']-span,min(now,row['resets'])
                if end<=start:continue
                r=c.execute('SELECT '+sums+' FROM events WHERE route=? AND ts>=? AND ts<?',(row['route'],start,end)).fetchone()
                row['window']=dict(start=start,end=end,open=end<row['resets'],requests=r['requests'],
                                   tokens=token_total(dict(r)))
            from .signals import capacity,quota_events
            for row in rows:
                row['events']=quota_events(c,row['route'],row['bucket'],row['history_source'] or row['source'],now) if row['checked'] else None
                row['capacity']=capacity(row)
                row['capacity_history']=self.capacity_history(c,row,now)
        return dict(limits=rows, sources=list(sources.values()), now=now,history_days=1,
                    retention_days=self.thresholds['retention_days'],low_percent=self.thresholds['low_percent'],
                    quota_hide_days=self.thresholds['quota_hide_days'])

    def capacity_history(self,c,row,now,keep=8,min_used=5):
        """Tokens one percentage point bought in recent finished windows of this bucket.

        A window is identified by its original reset (jitter of seconds merged); its use
        is 100 minus the lowest remaining observed before that reset, and its tokens are
        this route's events inside [reset - window length, reset). Windows that used
        under min_used points are left out. A drop against earlier windows can mean the
        provider changed the quota; it is an observation, not a published limit."""
        span=row.get('window_seconds');source=row.get('history_source') or row.get('source')
        if not span or not row.get('checked'):return []
        memo=self.__dict__.setdefault('_capacity_memo',{})
        if len(memo)>5000:memo.clear()
        epochs=[]
        for resets,remaining in c.execute('SELECT resets,MIN(remaining) FROM limit_history WHERE route=? AND bucket=? AND source=? '
                                          'AND resets IS NOT NULL AND resets<=? AND resets>=? GROUP BY CAST(resets/60 AS INTEGER) ORDER BY resets',
                                          (row['route'],row['bucket'],source,now,now-30*86400)):
            if epochs and resets-epochs[-1][0]<=120:epochs[-1][1]=min(epochs[-1][1],remaining)
            else:epochs.append([resets,remaining])
        result=[]
        for resets,lowest in epochs[-keep:]:
            used=100-(lowest if lowest is not None else 100)
            if used<min_used:continue
            key=(row['route'],row['bucket'],int(resets//60),round(used,3))
            if key not in memo:
                r=c.execute('SELECT COALESCE(SUM(uncached_input+cached_input+output+cache_creation),0),COUNT(*) FROM events '
                            'WHERE route=? AND ts>=? AND ts<?',(row['route'],resets-span,resets)).fetchone()
                memo[key]=dict(resets=resets,used_points=used,tokens=r[0],requests=r[1],tokens_per_point=r[0]/used if r[0] else None)
            if memo[key]['tokens_per_point']:result.append(memo[key])
        return result

    GROUPS={'provider':'provider','route':'route','model':'model',
            'project':"COALESCE(project,'미분류')",'agent':"COALESCE(agent_kind,'미분류')"}
    SCOPE_KINDS={'route':'route','provider':'provider','model':'model',
                 'project':"COALESCE(project,'미분류')"}
    COMPARE_SHIFTS={'week':7,'month':28}
    COMPARE_LABELS={'previous':'직전 동일 기간','week':'7일 전 동일 구간','month':'28일 전 동일 구간'}

    def _period_free(self,c,lifetime,midnight,pricing,sums,scope_sql='',scope_args=()):
        """Lifetime cost and the 26-week calendar ignore the selected period.

        Reuse them until the data changes: any event write moves the lifetime
        sums, a collection pass bumps last_local, and a sum-preserving relabel
        bumps data_version.
        """
        key=(tuple(lifetime.values()),self.state(c,'last_local'),midnight.date().isoformat(),
             json.dumps(pricing,sort_keys=True) if pricing else None,scope_sql+repr(scope_args),
             self.state(c,'data_version') or 0)
        memo=self._memo  # one read: another thread may replace it meanwhile
        if memo.get('key')!=key:
            from .insights import token_total
            from .pricing import estimate,rate_for
            month=None
            if pricing:
                month_start=midnight.replace(day=1)
                days_in_month=((month_start.replace(day=28)+timedelta(days=4)).replace(day=1)-month_start).days
                by_route={}
                for r in c.execute("SELECT route,model,date(ts,'unixepoch','+9 hours') AS day,"+sums+
                                   ' FROM '+self.table+' WHERE ts>=?'+scope_sql+' GROUP BY route,model,day',
                                   (month_start.timestamp(),*scope_args)):
                    rate=rate_for(r['model'],pricing,r['day'])
                    if rate:by_route[r['route']]=by_route.get(r['route'],0)+estimate(dict(r),rate)
                if by_route:
                    month_total=sum(by_route.values());elapsed=max(1,midnight.day)
                    month=dict(days_elapsed=elapsed,days_in_month=days_in_month,
                               cost_by_route={r:round(v,4) for r,v in sorted(by_route.items())},
                               total=round(month_total,4),projected=round(month_total/elapsed*days_in_month,4))
            cal={}
            for r in c.execute("SELECT date(ts,'unixepoch','+9 hours') AS day,model,"+sums+','+self.rcount+
                               ' FROM '+self.table+' WHERE ts>=?'+scope_sql+' GROUP BY day,model',
                               ((midnight-timedelta(days=181)).timestamp(),*scope_args)):
                e=cal.setdefault(r['day'],dict(day=r['day'],tokens=0,requests=0,cost=0.0,rated=False))
                e['tokens']+=r['uncached_input']+r['cached_input']+r['output']+r['cache_creation']
                e['requests']+=r['requests']
                if pricing:
                    rate=rate_for(r['model'],pricing,r['day'])
                    if rate:e['cost']+=estimate(dict(r),rate);e['rated']=True
            calendar=[dict(day=e['day'],tokens=e['tokens'],requests=e['requests'],cost=round(e['cost'],4) if e['rated'] else None) for _,e in sorted(cal.items())]
            cost=priced=0
            if pricing:
                for r in c.execute("SELECT model,date(ts,'unixepoch','+9 hours') AS day,"+sums+' FROM '+self.table+' WHERE 1=1'+scope_sql+' GROUP BY model,day',scope_args):
                    rate=rate_for(r['model'],pricing,r['day'])
                    if rate:cost+=estimate(dict(r),rate);priced+=token_total(dict(r))
            memo=dict(key=key,calendar=calendar,cost=cost,priced=priced,month=month);self._memo=memo
        return [dict(day) for day in memo['calendar']],memo['cost'],memo['priced'],memo['month'] and dict(memo['month'])

    def bump_data_version(self,c):
        """Relabel/attribute writes keep every token sum, so they bump a version."""
        self.save_state(c,'data_version',(self.state(c,'data_version') or 0)+1)

    def _usage_get(self,key):
        with self._usage_lock:
            cache=self._usage_cache
            if key in cache:cache.move_to_end(key)
            return cache.get(key)

    def _usage_put(self,key,body):
        with self._usage_lock:
            cache=self._usage_cache;cache[key]=body
            while len(cache)>12:cache.popitem(last=False)

    def usage(self, period='7d', start=None, end=None, granularity='day', group='provider', cumulative=False, now=None, pricing=None, subscriptions=None, compare=None, scope=None, value_alert_usd=None, sections='all'):
        if granularity not in BUCKET_SQL or group not in self.GROUPS or sections not in ('all','core','insights'):
            raise ValueError('집계 단위가 올바르지 않습니다.')
        scope_sql,scope_args='',()
        if scope:
            kind,_,value=scope.partition(':')
            if kind not in self.SCOPE_KINDS or not value:raise ValueError('범위가 올바르지 않습니다.')
            scope_sql=' AND '+self.SCOPE_KINDS[kind]+'=?';scope_args=(value,)
        now = datetime.fromtimestamp(now or time.time(),KST)
        midnight = now.replace(hour=0,minute=0,second=0,microsecond=0)
        sums=','.join(f'COALESCE(SUM({k}),0) AS {k}' for k in TOKENS)
        count='COUNT(*) AS requests'
        with self.connect() as c:
            c.execute('BEGIN')
            lifetime=dict(c.execute('SELECT '+sums+','+self.rcount+' FROM '+self.table+' WHERE 1=1'+scope_sql,scope_args).fetchone())
            collected_at=self.state(c,'last_local')
            # Every term that changes the numbers or the response shape is in the
            # key: event writes move lifetime, collection passes move last_local,
            # sum-preserving relabels move data_version.
            key=(period,start,end,granularity,group,cumulative,compare,scope,sections,value_alert_usd,
                 tuple(lifetime.values()),collected_at,self.state(c,'data_version') or 0,
                 midnight.date().isoformat(),
                 json.dumps(pricing,sort_keys=True) if pricing else None,
                 json.dumps(subscriptions,sort_keys=True) if subscriptions else None)
            body=self._usage_get(key)
            if body is None:
                body=self._usage_body(c,period,start,end,granularity,group,cumulative,now,midnight,pricing,subscriptions,
                                      compare,scope,scope_sql,scope_args,value_alert_usd,sections,sums,count,lifetime)
                if len(body['series'])<=5000:self._usage_put(key,body)
            sources,unavailable_routes=self._usage_live(c,body['rows'],now)
            from .signals import usage_spike
            spike=usage_spike(c,now.timestamp()) if sections!='insights' else None
            month=self.project_month(c,now,pricing) if sections!='insights' else None
        data={**body,'sources':sources,'unavailable_routes':unavailable_routes,'collected_at':collected_at,'spike':spike,'project_month':month}
        if sections=='all':return data
        insight_keys={'insights','sessions','sessions_total','sessionless_requests','projects','calendar'}
        if sections=='core':return {k:v for k,v in data.items() if k not in insight_keys}
        shared={'start','end_exclusive','timezone','granularity','labels','series','subscriptions','sources','scope','collected_at'}
        return {k:v for k,v in data.items() if k in insight_keys|shared}

    def project_month(self,c,now,pricing=None):
        """Month-to-date (KST calendar month) tokens and estimated cost per project.

        cost is None when no token of the project has a rate; priced_share tells how
        much of the tokens the cost covers."""
        from .insights import token_total
        from .pricing import estimate,rate_for
        now=now if isinstance(now,datetime) else datetime.fromtimestamp(now,KST)
        start=now.replace(day=1,hour=0,minute=0,second=0,microsecond=0)
        sums=','.join(f'COALESCE(SUM({k}),0) AS {k}' for k in TOKENS)
        out={}
        for r in c.execute("SELECT COALESCE(project,'미분류') AS project,model,date(ts,'unixepoch','+9 hours') AS day,"+sums+
                           ' FROM '+self.table+' WHERE ts>=? GROUP BY 1,2,3',(start.timestamp(),)):
            row=dict(r);tokens=token_total(row)
            e=out.setdefault(row['project'],dict(tokens=0,cost=0.0,priced_tokens=0))
            e['tokens']+=tokens
            rate=rate_for(row['model'],pricing,row['day']) if pricing else None
            if rate:e['cost']+=estimate(row,rate);e['priced_tokens']+=tokens
        for e in out.values():
            e['cost']=round(e['cost'],4) if e['priced_tokens'] else None
            e['priced_share']=e.pop('priced_tokens')/e['tokens']*100 if e['tokens'] else None
        return dict(month=start.strftime('%Y-%m'),projects=out)

    def project_detail(self,project,now=None,pricing=None,days=30,budget=None):
        """One project over the last `days` KST days: models, daily use, busiest sessions,
        and this month so far with a straight-line month-end projection (and the budget
        share when a budget is given). '미분류' is the bucket of records with no project."""
        from .insights import token_total
        from .pricing import estimate,rate_for
        now=now if isinstance(now,datetime) else datetime.fromtimestamp(now or time.time(),KST)
        midnight=now.replace(hour=0,minute=0,second=0,microsecond=0)
        start=midnight-timedelta(days=days-1);month_start=midnight.replace(day=1)
        sums=','.join(f'COALESCE(SUM({k}),0) AS {k}' for k in TOKENS)
        where=" WHERE COALESCE(project,'미분류')=? AND ts>=?"
        models={};daily={};month=dict(tokens=0,cost=0.0,priced=0)
        with self.connect() as c:
            for r in c.execute("SELECT route,model,date(ts,'unixepoch','+9 hours') AS day,"+sums+','+self.rcount+
                               ' FROM '+self.table+where+' GROUP BY route,model,day',(project,min(start,month_start).timestamp())):
                row=dict(r);tokens=token_total(row);rate=rate_for(row['model'],pricing,row['day']) if pricing else None
                part=estimate(row,rate) if rate else None
                if row['day']>=month_start.strftime('%Y-%m-%d'):
                    month['tokens']+=tokens
                    if part is not None:month['cost']+=part;month['priced']+=tokens
                if row['day']<start.strftime('%Y-%m-%d'):continue
                m=models.setdefault((row['route'],row['model']),dict(route=row['route'],model=row['model'],tokens=0,requests=0,cost=None))
                m['tokens']+=tokens;m['requests']+=row['requests']
                if part is not None:m['cost']=(m['cost'] or 0)+part
                d=daily.setdefault(row['day'],dict(day=row['day'],tokens=0,requests=0));d['tokens']+=tokens;d['requests']+=row['requests']
            sessions=[dict(session=r[0],route=r[1],tokens=r[2],requests=r[3],first_ts=r[4],last_ts=r[5]) for r in c.execute(
                'SELECT session,MAX(route),SUM(uncached_input+cached_input+output+cache_creation),COUNT(*),MIN(ts),MAX(ts) FROM events'
                +where+' AND session IS NOT NULL GROUP BY session ORDER BY 3 DESC LIMIT 10',(project,start.timestamp()))]
        elapsed=now.day;days_in_month=((month_start.replace(day=28)+timedelta(days=4)).replace(day=1)-month_start).days
        cost=month['cost'] if month['priced'] else None
        projection=dict(month=month_start.strftime('%Y-%m'),days_elapsed=elapsed,days_in_month=days_in_month,
                        tokens=month['tokens'],cost=round(cost,4) if cost is not None else None,
                        projected_tokens=month['tokens']/elapsed*days_in_month,
                        projected_cost=round(cost/elapsed*days_in_month,4) if cost is not None else None,
                        priced_share=month['priced']/month['tokens']*100 if month['tokens'] else None)
        if budget:
            from .notify import budget_level
            _,projection['budget_ratio']=budget_level(dict(tokens=month['tokens'],cost=cost),budget)
            _,projection['projected_budget_ratio']=budget_level(dict(tokens=projection['projected_tokens'],cost=projection['projected_cost']),budget)
            projection['budget']=budget
        for m in models.values():m['cost']=round(m['cost'],4) if m['cost'] is not None else None
        return dict(project=project,days=days,start=start.strftime('%Y-%m-%d'),
                    models=sorted(models.values(),key=lambda m:-m['tokens']),
                    daily=[daily[k] for k in sorted(daily)],sessions=sessions,month=projection)

    def session_detail(self,session,pricing=None):
        """One session's models, hourly activity and cache share; no message content exists here."""
        from .insights import token_total
        from .pricing import estimate,rate_for
        sums=','.join(f'COALESCE(SUM({k}),0) AS {k}' for k in TOKENS)
        with self.connect() as c:
            models=[];cost=0.0;rated=False
            for r in c.execute('''SELECT route,model,date(ts,'unixepoch','+9 hours') AS day,MIN(ts) AS first_ts,MAX(ts) AS last_ts,
                COUNT(*) AS requests,'''+sums+' FROM events WHERE session=? GROUP BY route,model,day',(session,)):
                row=dict(r);rate=rate_for(row['model'],pricing,row['day']) if pricing else None
                part=estimate(row,rate) if rate else None
                key=next((m for m in models if m['route']==row['route'] and m['model']==row['model']),None)
                if key is None:
                    key=dict(route=row['route'],model=row['model'],first_ts=row['first_ts'],last_ts=row['last_ts'],
                             requests=0,cost=None,**dict.fromkeys(TOKENS,0));models.append(key)
                key['first_ts']=min(key['first_ts'],row['first_ts']);key['last_ts']=max(key['last_ts'],row['last_ts'])
                key['requests']+=row['requests']
                for k in TOKENS:key[k]+=row[k]
                if part is not None:key['cost']=(key['cost'] or 0)+part;cost+=part;rated=True
            if not models:return dict(session=session,models=[],hours=[])
            meta=c.execute('SELECT MAX(project),MAX(agent_kind) FROM events WHERE session=?',(session,)).fetchone()
            hours=[dict(hour=r[0]*3600,requests=r[1],tokens=r[2]) for r in c.execute(
                'SELECT CAST(ts/3600 AS INTEGER),COUNT(*),SUM(uncached_input+cached_input+output+cache_creation) '
                'FROM events WHERE session=? GROUP BY 1 ORDER BY 1',(session,))]
        totals={k:sum(m[k] for m in models) for k in TOKENS}
        inputs=totals['uncached_input']+totals['cached_input']+totals['cache_creation']
        for m in models:m['cost']=round(m['cost'],4) if m['cost'] is not None else None
        return dict(session=session,project=meta[0],kind=meta[1],first_ts=min(m['first_ts'] for m in models),
                    last_ts=max(m['last_ts'] for m in models),requests=sum(m['requests'] for m in models),totals=totals,
                    cache_share=totals['cached_input']/inputs*100 if inputs else None,cost=round(cost,4) if rated else None,
                    models=sorted(models,key=lambda m:-token_total(m)),hours=hours)

    def _usage_body(self,c,period,start,end,granularity,group,cumulative,now,midnight,pricing,subscriptions,
                    compare,scope,scope_sql,scope_args,value_alert_usd,sections,sums,count,lifetime):
        first = c.execute('SELECT MIN(ts) FROM events WHERE 1=1'+scope_sql,scope_args).fetchone()[0]
        if period=='custom':
            a = datetime.strptime(start,'%Y-%m-%d').replace(tzinfo=KST)
            b = datetime.strptime(end,'%Y-%m-%d').replace(tzinfo=KST)+timedelta(days=1)
        elif period in ('today','7d','30d','all'):
            a = (datetime.fromtimestamp(first,KST).replace(hour=0,minute=0,second=0,microsecond=0)
                 if first and period=='all' else midnight-timedelta(days={'today':0,'7d':6,'30d':29,'all':0}[period]))
            b = midnight+timedelta(days=1)
        else:
            raise ValueError('기간이 올바르지 않습니다.')
        intervals=calendar_buckets(a,b,granularity)
        R,rcount=self.table,self.rcount
        total = dict(c.execute('SELECT '+sums+','+rcount+' FROM '+R+' WHERE ts>=? AND ts<?'+scope_sql,(a.timestamp(),b.timestamp(),*scope_args)).fetchone())
        rows = [dict(r) for r in c.execute('SELECT provider,route,model,'+rcount+','+sums+' FROM '+R+' WHERE ts>=? AND ts<?'+scope_sql+' GROUP BY provider,route,model ORDER BY SUM(uncached_input+cached_input+output+cache_creation) DESC,provider,route,model',(a.timestamp(),b.timestamp(),*scope_args))]
        from .pricing import estimate,rate_for
        # Chart-only aggregates are dropped from insights responses; skip the scan.
        heatmap=[]
        if sections!='insights':
            cells={}
            for r in c.execute('''SELECT CAST(strftime('%w',ts,'unixepoch','+9 hours') AS INTEGER) AS dow,
                CAST(strftime('%H',ts,'unixepoch','+9 hours') AS INTEGER) AS hour,
                model,date(ts,'unixepoch','+9 hours') AS day,'''+sums+','+rcount+'''
                FROM '''+R+''' WHERE ts>=? AND ts<?'''+scope_sql+' GROUP BY dow,hour,model,day',(a.timestamp(),b.timestamp(),*scope_args)):
                e=cells.setdefault((r['dow'],r['hour']),dict(dow=r['dow'],hour=r['hour'],tokens=0,requests=0,cost=0.0,rated=False))
                e['tokens']+=r['uncached_input']+r['cached_input']+r['output']+r['cache_creation']
                e['requests']+=r['requests']
                if pricing:
                    rate=rate_for(r['model'],pricing,r['day'])
                    if rate:e['cost']+=estimate(dict(r),rate);e['rated']=True
            heatmap=[dict(dow=e['dow'],hour=e['hour'],tokens=e['tokens'],requests=e['requests'],cost=round(e['cost'],4) if e['rated'] else None) for _,e in sorted(cells.items())]
        # Calendar weeks start Monday; calendar months are not fixed day counts.
        # Model-level rows let each bucket also carry request counts and a
        # cost estimate (rates are keyed by model, so group by model first).
        from .pricing import estimate,rate_for
        label_expr=self.GROUPS[group]
        buckets = {}
        from .insights import token_total
        for r in c.execute('SELECT '+BUCKET_SQL[granularity]+' AS bucket,'+label_expr+' AS label,model,date(ts,\'unixepoch\',\'+9 hours\') AS day,'+sums+','+rcount+' FROM '+R+' WHERE ts>=? AND ts<?'+scope_sql+' GROUP BY bucket,label,model,day',(a.timestamp(),b.timestamp(),*scope_args)):
            e = buckets.setdefault((r['bucket'],r['label']),dict(requests=0,cost=0.0,unpriced=0,**dict.fromkeys(TOKENS,0)))
            for k in TOKENS: e[k]+=r[k]
            e['requests']+=r['requests']
            if pricing:
                rate=rate_for(r['model'],pricing,r['day'])
                # Tokens without a rate are counted so the cost view can say "미산정", not $0.
                if rate:e['cost']+=estimate(dict(r),rate)
                else:e['unpriced']+=token_total(dict(r))
        buckets = [dict(bucket=k[0],label=k[1],**v) for k,v in buckets.items()]
        compare_data=None
        if sections!='insights' and compare in ('previous','week','month'):
            shift=self.COMPARE_SHIFTS.get(compare)
            a2,b2=(a-timedelta(days=shift),b-timedelta(days=shift)) if shift else (a-(b-a),a)
            # Bucket shifted timestamps onto the current window's grid so
            # the ghost series always matches the main series 1:1 — weekly
            # and monthly buckets otherwise drift in count and alignment.
            delta=int((a-a2).total_seconds())
            shifted_sql=re.sub(r'\bts\b',f'(ts+{delta})',BUCKET_SQL[granularity])
            cmp_buckets={}
            for r in c.execute('SELECT '+shifted_sql+' AS bucket,'+label_expr+' AS label,model,date(ts,\'unixepoch\',\'+9 hours\') AS day,'+sums+','+rcount+' FROM '+R+' WHERE ts>=? AND ts<?'+scope_sql+' GROUP BY bucket,label,model,day',(a2.timestamp(),b2.timestamp(),*scope_args)):
                e=cmp_buckets.setdefault((r['bucket'],r['label']),dict(requests=0,cost=0.0,**dict.fromkeys(TOKENS,0)))
                for k in TOKENS: e[k]+=r[k]
                e['requests']+=r['requests']
                if pricing:
                    rate=rate_for(r['model'],pricing,r['day'])
                    if rate:e['cost']+=estimate(dict(r),rate)
            cmp_series=[]
            cmp_labels={r['label'] for r in buckets}|{k[1] for k in cmp_buckets}
            for begin,_finish in calendar_buckets(a,b,granularity):
                t=int(begin.timestamp())
                cmp_series.append(dict(time=begin.isoformat(),
                    compared_time=(begin-timedelta(seconds=delta)).isoformat(),
                    values={label:cmp_buckets.get((t,label),{}) for label in cmp_labels}))
            # The running period has only reached now; compare its total with the same
            # elapsed span of the earlier window, not the whole of it.
            effective=min(b,now)
            elapsed=None
            if effective>a:
                stop=a2+(effective-a)
                elapsed=dict(c.execute('SELECT '+sums+','+count+' FROM events WHERE ts>=? AND ts<?'+scope_sql,
                                       (a2.timestamp(),stop.timestamp(),*scope_args)).fetchone())
                elapsed.update(start=a2.isoformat(),end_exclusive=stop.isoformat())
            compare_data=dict(mode=compare,label=self.COMPARE_LABELS[compare],
                              start=a2.isoformat(),end_exclusive=b2.isoformat(),series=cmp_series,elapsed=elapsed)
        session_list=[];sessionless=0;project_list=[];sessions_total=0
        if sections!='core':
            sessions={}
            for r in c.execute('''SELECT session,model,date(ts,'unixepoch','+9 hours') AS day,MAX(route) AS route,MAX(project) AS project,
                MAX(agent_kind) AS kind,MIN(ts) AS first_ts,MAX(ts) AS last_ts,'''+count+','+sums+'''
                FROM events WHERE ts>=? AND ts<? AND session IS NOT NULL'''+scope_sql+' GROUP BY session,model,day''',
                (a.timestamp(),b.timestamp(),*scope_args)):
                e=sessions.setdefault(r['session'],dict(session=r['session'],route=r['route'],project=r['project'],
                    kind=r['kind'],first_ts=r['first_ts'],last_ts=r['last_ts'],requests=0,cost=0.0,rated=False,
                    **dict.fromkeys(TOKENS,0)))
                e['first_ts']=min(e['first_ts'],r['first_ts']);e['last_ts']=max(e['last_ts'],r['last_ts'])
                e['requests']+=r['requests']
                for k in TOKENS: e[k]+=r[k]
                if pricing:
                    rate=rate_for(r['model'],pricing,r['day'])
                    if rate:e['cost']+=estimate(dict(r),rate);e['rated']=True
            sessions_total=len(sessions)
            session_list=sorted(sessions.values(),key=lambda s:-(s['uncached_input']+s['cached_input']+s['output']+s['cache_creation']))[:100]
            for s in session_list:s['cost']=round(s['cost'],2) if s.pop('rated') else None
            sessionless=c.execute('SELECT COUNT(*) FROM events WHERE ts>=? AND ts<? AND session IS NULL'+scope_sql,
                                  (a.timestamp(),b.timestamp(),*scope_args)).fetchone()[0]
            projects={}
            project_sessions=dict(c.execute("SELECT COALESCE(NULLIF(project,''),'미분류'),COUNT(DISTINCT session) FROM events WHERE ts>=? AND ts<?"
                +scope_sql+" GROUP BY COALESCE(NULLIF(project,''),'미분류')",(a.timestamp(),b.timestamp(),*scope_args)))
            for r in c.execute('''SELECT project,model,date(ts,'unixepoch','+9 hours') AS day,MAX(ts) AS last_ts,'''+count+','+sums+'''
                FROM events WHERE ts>=? AND ts<?'''+scope_sql+' GROUP BY project,model,day''',(a.timestamp(),b.timestamp(),*scope_args)):
                label=r['project'] or '미분류'
                e=projects.setdefault(label,dict(project=label,sessions=project_sessions[label],last_ts=r['last_ts'],requests=0,cost=0.0,rated=False,
                    **dict.fromkeys(TOKENS,0)))
                e['last_ts']=max(e['last_ts'],r['last_ts']);e['requests']+=r['requests']
                for k in TOKENS: e[k]+=r[k]
                if pricing:
                    rate=rate_for(r['model'],pricing,r['day'])
                    if rate:e['cost']+=estimate(dict(r),rate);e['rated']=True
            project_list=sorted(projects.values(),key=lambda p:-(p['uncached_input']+p['cached_input']+p['output']+p['cache_creation']))
            for p in project_list:p['cost']=round(p['cost'],2) if p.pop('rated') else None
        calendar,lifetime_cost,lifetime_priced,month=self._period_free(c,lifetime,midnight,pricing,sums,scope_sql,scope_args)
        if month is not None:month['alert_usd']=value_alert_usd
        scope_options={}
        if sections!='insights':
            scope_options={kind:[r[0] for r in c.execute('SELECT '+expr+' AS v,'+rcount.replace(' AS requests','')+' AS n FROM '+R+' GROUP BY v ORDER BY n DESC,v LIMIT 50')]
                           for kind,expr in self.SCOPE_KINDS.items()}
        from .insights import usage_insights,token_total
        insights=None
        if sections!='core':
            insights=usage_insights(c,a,b,now,period,first,total,rows,buckets,scope_sql,scope_args)
            insights['month']=month
        cost={'period':None,'lifetime':None,'coverage':None,'lifetime_coverage':None,'cache_savings':None}
        if pricing:
            priced=0;savings=0.0
            # Rows are (provider, route, model): a model used through two routes
            # must not show the other route's spend on each row.
            cost_by_row={}
            for r in c.execute('SELECT provider,route,model,date(ts,\'unixepoch\',\'+9 hours\') AS day,'+sums+
                               ' FROM '+R+' WHERE ts>=? AND ts<?'+scope_sql+' GROUP BY provider,route,model,day',
                               (a.timestamp(),b.timestamp(),*scope_args)):
                rate=rate_for(r['model'],pricing,r['day'])
                if rate:
                    priced+=token_total(dict(r))
                    key=(r['provider'],r['route'],r['model'])
                    cost_by_row[key]=cost_by_row.get(key,0)+estimate(dict(r),rate)
                    # What the cache reads would have cost as uncached input at the same list rate.
                    savings+=r['cached_input']*(rate['input']-rate.get('cached',rate['input']))/1e6
            for row in rows:row['est_cost']=cost_by_row.get((row['provider'],row['route'],row['model']))
            if priced:cost['period']=sum(cost_by_row.values());cost['cache_savings']=round(savings,4)
            if token_total(total):cost['coverage']=priced/token_total(total)*100
            if lifetime_priced:
                cost['lifetime']=lifetime_cost
                if token_total(lifetime):cost['lifetime_coverage']=lifetime_priced/token_total(lifetime)*100
        subs=[]
        if subscriptions:
            by_route={}
            for row in rows:
                if row.get('est_cost') is not None:
                    by_route[row['route']]=by_route.get(row['route'],0)+row['est_cost']
            # Scale by the elapsed part of the period: a running week is not seven days yet.
            days=max(1,(min(b,now)-a).total_seconds()/86400)
            for route,monthly in subscriptions.items():
                if not isinstance(monthly,(int,float)) or monthly<0:continue
                est=by_route.get(route)
                subs.append(dict(route=route,monthly_usd=monthly,period_cost=est,
                                 monthly_equiv=est*30.44/days if est is not None else None))
        labels = sorted({r['label'] for r in buckets})
        lookup = {(r['bucket'],r['label']):r for r in buckets}
        running = {label:dict(requests=0,cost=0.0,unpriced=0,**dict.fromkeys(TOKENS,0)) for label in labels}
        series = []
        for begin,finish in intervals:
            t=int(begin.timestamp())
            values = {}
            for label in labels:
                entry = lookup.get((t,label),{})
                for key in SERIES_KEYS:
                    running[label][key] += entry.get(key,0)
                values[label] = dict(running[label]) if cumulative else {k:entry.get(k,0) for k in SERIES_KEYS}
            series.append(dict(time=begin.isoformat(),values=values,range_start=max(a,begin).isoformat(),
                range_end_exclusive=min(b,finish).isoformat(),partial=begin<a or finish>b))
        return dict(start=a.isoformat(),end_exclusive=b.isoformat(),timezone='Asia/Seoul',totals=total,lifetime=lifetime,
                    granularity=granularity,heatmap=heatmap,cost=cost,
                    rows=rows,series=series,labels=labels,first_record=first,insights=insights,
                    calendar=calendar,subscriptions=subs,sessions=session_list,sessions_total=sessions_total,sessionless_requests=sessionless,projects=project_list,compare=compare_data,
                    scope=scope,scope_options=scope_options,
                    coverage='로컬에서 확보한 세션만 집계합니다. 원격 작업은 실행한 PC의 기록 기준이며, 클라우드·다른 PC의 사용량은 미수집입니다. Antigravity는 로컬 대화 DB, Devin은 로컬 sessions.db의 요청별 사용량을 집계합니다. 삭제된 기록과 원본 모델명이 없는 요청은 완전히 복구할 수 없습니다. 직접 API 과금은 분석하지 않습니다.',
                    token_note='출력(Output)은 추론(reasoning)을 포함합니다. 캐시 생성(Cache creation)은 별도 입력으로 총 토큰에 한 번만 포함합니다. 추론은 출력의 부분집합이거나 원본 미제공(0)입니다.')

    def storage_row(self,checked=None):
        """Database, WAL and backup sizes, so growth is visible before it is a problem."""
        def size(path):
            try:return Path(path).stat().st_size
            except OSError:return 0
        database=size(self.path);wal=size(self.path+'-wal')
        backups=sorted(Path(self.path).parent.glob('backups/usage-*.db.gz'))
        kept=sum(size(p) for p in backups)
        return dict(name='데이터베이스',checked=checked,status='ok',
                    detail=f'DB {database/1e6:.0f}MB · WAL {wal/1e6:.0f}MB · 백업 {len(backups)}개 {kept/1e6:.0f}MB')

    def _usage_live(self,c,rows,now):
        """Source freshness and availability change with wall time, so they are
        computed on every call instead of being cached with the body."""
        sources = [dict(r) for r in c.execute('SELECT * FROM sources ORDER BY name')]
        stale=self.thresholds['stale_seconds']
        collector=self.state(c,'collector',{})
        sources.append(dict(name='수집기',checked=collector.get('checked'),
            status='ok' if collector.get('checked') and time.time()-collector['checked']<=stale else 'stale' if collector.get('checked') else 'unavailable',
            detail=collector.get('status') or '기록 없음'))
        backup=self.state(c,'backup',{})
        if backup:
            fresh=time.time()-backup.get('ts',0)<=86400+stale
            sources.append(dict(name='일일 백업',checked=backup.get('ts'),
                status='error' if not backup.get('ok') else 'ok' if fresh else 'stale',
                detail=backup.get('error') or f"{backup.get('file','')} · {backup.get('size',0)/1e6:.0f}MB"))
        else:
            sources.append(dict(name='일일 백업',checked=None,status='unavailable',detail='아직 실행되지 않음'))
        verify=self.state(c,'verify',{})
        if verify:
            bad=not verify.get('ok')
            misses=sum((verify.get(s) or {}).get('missing_events',0) for s in ('antigravity','devin'))
            mismatch=verify.get('codex',{}).get('mismatches',0)+sum((verify.get(s) or {}).get('mismatched_events',0) for s in ('antigravity','devin'))
            errors=sum((verify.get(s) or {}).get('source_errors',0) for s in ('antigravity','devin'))
            sources.append(dict(name='원본 대조',checked=verify.get('ts'),
                status='error' if bad else 'ok' if time.time()-verify.get('ts',0)<=86400+stale else 'stale',
                detail=verify.get('error') or ((f'누락 {misses}건 · 불일치 {mismatch}건 · 오류 {errors}건'
                       +(' · 시간별 집계가 원본과 달라 다시 계산함' if (verify.get('rollup') or {}).get('matches') is False else '')) if bad else '원본과 일치')))
        else:
            sources.append(dict(name='원본 대조',checked=None,status='unavailable',detail='아직 실행되지 않음'))
        sources.append(self.storage_row(self.state(c,'last_local')))
        ops_rows={'수집기','일일 백업','원본 대조','데이터베이스'}
        for source in sources:
            # Ops pseudo-rows carry daily cadences; they compute staleness themselves.
            if source['name'] in ops_rows:continue
            if source['status']=='ok' and now.timestamp()-source['checked']>self.thresholds['stale_seconds']:
                source['status']='stale'
        # A status line reports only while a terminal UI runs it (not the desktop apps
        # or headless workers). After a day without a receipt it is shown as unused,
        # not as a failing source; the quota stays visible through account/app polling.
        for source in sources:
            if (source['name'] in STATUS_LINE_SOURCES and source['status']=='stale' and source.get('checked')
                    and now.timestamp()-source['checked']>86400):
                source.update(status='idle',detail=' · '.join(filter(None,[source.get('detail'),
                    '최근 24시간 상태줄 수신 없음 · 터미널(TUI) 실행 중에만 수신 · 한도는 계정/앱 조회로 표시'])))
        unavailable_routes=[dict(route=source['name'].removesuffix('-records'),detail=source['detail'])
                            for source in sources if source['name'].endswith('-records') and source['status']=='unavailable'
                            and not any(row['route']==source['name'].removesuffix('-records') for row in rows)]
        observation=next((s for s in sources if s['name']=='antigravity-tokens'),None)
        for gap in unavailable_routes:
            if gap['route']=='antigravity' and observation and observation['status']=='partial':
                gap.update(observed=True,observation_checked=observation['checked'],
                           observation_stale=now.timestamp()-observation['checked']>self.thresholds['stale_seconds'])
        return sources,unavailable_routes
