"""Only numeric usage, opaque identities and collection metadata enter SQLite."""
from collections import OrderedDict
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import functools
from zoneinfo import ZoneInfo
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import sqlite3
import threading
import time

from .claude_accounts import account_source, family, is_claude

KST = ZoneInfo('Asia/Seoul')
TOKENS = ('uncached_input', 'cached_input', 'output', 'cache_creation', 'reasoning')
DETAILS = ('cache_creation_5m','cache_creation_1h','cache_creation_unknown','cache_ttl_observed','cache_ttl_conflict','reasoning_observed','advisor_calls')
METRICS = (*TOKENS,*DETAILS)
SERIES_KEYS = (*METRICS, 'requests', 'cost', 'unpriced')
HISTORY_FIELDS = ('checked', 'remaining', 'resets', 'break_before', 'break_reason')
# Sources fed by a terminal status line rather than by polling.
STATUS_LINE_SOURCES = {'claude-code', 'antigravity', 'antigravity-tokens'}
ONE_TIME_SOURCES = {'session-backfill'}
# Source detail written while a subscription is marked ended in the settings.
USER_ENDED_DETAIL = '설정에서 구독 종료로 표시 · 한도 조회 중지'
SUPPORTED_ROUTES = ('codex','claude-code','antigravity','opencode-go','devin')
# (route, shorter-window bucket) -> the longer window that also gates usage.
QUOTA_PARENTS = {('claude-code', 'five_hour'): 'seven_day', ('devin', 'daily'): 'weekly',
                 ('antigravity', 'gemini-5h'): 'gemini-weekly', ('antigravity', '3p-5h'): '3p-weekly'}
BUCKET_SQL = {
    'hour':'CAST((ts+32400)/3600 AS INTEGER)*3600-32400',
    'day':'CAST((ts+32400)/86400 AS INTEGER)*86400-32400',
    'week':"CAST((ts+32400)/86400 AS INTEGER)*86400-32400-((CAST(strftime('%w',ts,'unixepoch','+9 hours') AS INTEGER)+6)%7)*86400",
    'month':"CAST(strftime('%s',ts,'unixepoch','+9 hours','start of month') AS INTEGER)-32400",
}


def route_events(route):
    """SQL filter for the usage events of a quota route. Claude accounts share route
    'claude-code'; pending (unresolved) events count for the original account."""
    if route=='claude-code':return "route='claude-code' AND (account IS NULL OR account='claude-code')",()
    if is_claude(route):return "route='claude-code' AND account=?",(route,)
    return 'route=?',(route,)


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
            if marker.is_dir() and (marker/'HEAD').is_file():return str(folder)
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
    # unused_*: the pre-reset notice (off unless its notification is turned on) for a window
    # longer than a day with at least unused_percent left within unused_hours of its reset.
    DEFAULT_THRESHOLDS={'stale_seconds':600,'retention_days':30,'low_percent':15,'quota_hide_days':7,'unused_percent':50,'unused_hours':48}

    def __init__(self, path, thresholds=None, rollup=True):
        self.path = str(path)
        # Whole-hour aggregations read usage_hourly; False reads events (equivalence checks).
        self.table='usage_hourly' if rollup else 'events'
        self.rcount='COALESCE(SUM(requests),0) AS requests'
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
            -- Provider-side daily token totals (UTC days). They carry no request,
            -- model or input/output split, so they stay out of events and pricing.
            CREATE TABLE IF NOT EXISTS account_tokens (
              route TEXT, day TEXT, tokens INTEGER NOT NULL, checked REAL NOT NULL, PRIMARY KEY(route,day));
            ''')
            # Additive migration: old releases can still read their original table.
            # Unbound history is retained without assigning it to the active login.
            c.executescript("""
                CREATE TABLE IF NOT EXISTS account_token_buckets (
                  route TEXT, day TEXT, tokens INTEGER NOT NULL, checked REAL NOT NULL,
                  namespace TEXT NOT NULL DEFAULT '', PRIMARY KEY(route,namespace,day));
                INSERT OR IGNORE INTO account_token_buckets
                  SELECT route,day,tokens,checked,'' FROM account_tokens;
            """)
            for column in ('session','project','agent_kind','account','parent_id'):
                if column not in {r['name'] for r in c.execute('PRAGMA table_info(events)')}:
                    # The web and the collector start together after a deploy; the
                    # other process may add the column between the check and here.
                    try:c.execute(f'ALTER TABLE events ADD COLUMN {column} TEXT')
                    except sqlite3.OperationalError as exc:
                        if 'duplicate column' not in str(exc):raise
            added_details=set()
            for column in (*DETAILS,'requests'):
                if column not in {r['name'] for r in c.execute('PRAGMA table_info(events)')}:
                    try:
                        c.execute(f'ALTER TABLE events ADD COLUMN {column} INTEGER NOT NULL DEFAULT '+('1' if column=='requests' else '0'))
                        added_details.add(column)
                    except sqlite3.OperationalError as exc:
                        if 'duplicate column' not in str(exc):raise
            if 'cache_creation_unknown' in added_details:
                c.execute("UPDATE events SET cache_creation_unknown=cache_creation WHERE route='claude-code' AND cache_ttl_observed=0")
            if 'reasoning_observed' in added_details:
                c.execute('UPDATE events SET reasoning_observed=1 WHERE reasoning>0')
            # Claude events whose account is not resolved yet (see claude_attribution).
            c.execute("CREATE INDEX IF NOT EXISTS events_claude_pending ON events(session) WHERE route='claude-code' AND account IS NULL")
            c.execute('CREATE INDEX IF NOT EXISTS events_session ON events(session)')
            c.execute('CREATE INDEX IF NOT EXISTS events_parent ON events(parent_id) WHERE parent_id IS NOT NULL')
            # Quota windows and per-route scopes filter one route over a time range.
            c.execute('CREATE INDEX IF NOT EXISTS events_route_ts ON events(route,ts)')
            from .resources import initialize
            initialize(c)
            from .claude_accounts import initialize as initialize_accounts
            initialize_accounts(c)
            from .claude_attribution import initialize as initialize_attribution
            initialize_attribution(c)
            from .reconciliation import initialize as initialize_usage
            initialize_usage(c)
            from .codex_usage import initialize as initialize_responses
            initialize_responses(c)
            from .codex_activity import initialize as initialize_activity
            initialize_activity(c)
        self._ensure_rollup()
        os.chmod(path, 0o600)

    def _ensure_rollup(self):
        """Hourly sums of events, kept by triggers so every write path (upserts, codex
        rebuilds, Antigravity merges, attribution backfills) updates it in the same
        transaction. Aggregations over whole hours read it instead of every event.

        ts is the hour start (KST is a whole-hour offset, so hours, days, weeks and
        months group exactly as on events); advisor subcalls carry no API request."""
        key=",".join(['ts','provider','route','model',"IFNULL(project,char(0))","IFNULL(agent_kind,char(0))","IFNULL(account,char(0))"])
        match=' AND '.join(['ts=CAST(OLD.ts/3600 AS INTEGER)*3600','provider=OLD.provider','route=OLD.route','model=OLD.model',
                            'project IS OLD.project','agent_kind IS OLD.agent_kind','account IS OLD.account'])
        add=f"""INSERT INTO usage_hourly VALUES (CAST(NEW.ts/3600 AS INTEGER)*3600,NEW.provider,NEW.route,NEW.model,NEW.project,NEW.agent_kind,
              {','.join('NEW.'+k for k in METRICS)},NEW.requests,NEW.account) ON CONFLICT({key}) DO UPDATE SET
              {','.join(f'{k}={k}+excluded.{k}' for k in METRICS)},requests=requests+excluded.requests;"""
        remove=f"""UPDATE usage_hourly SET {','.join(f'{k}={k}-OLD.{k}' for k in METRICS)},requests=requests-OLD.requests WHERE {match};
              DELETE FROM usage_hourly WHERE {match} AND requests+advisor_calls<=0;"""
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')  # the web and the collector may start together
            # Version 3 adds detail coverage and separate advisor subcall counts.
            # An older release finds a different
            # version and rebuilds its own table; this one does the same in return.
            if self.state(c,'rollup_version')==3:return
            for statement in ['DROP TABLE IF EXISTS usage_hourly',
                f"""CREATE TABLE usage_hourly (ts INTEGER NOT NULL, provider TEXT NOT NULL, route TEXT NOT NULL, model TEXT NOT NULL,
                    project TEXT, agent_kind TEXT, {','.join(k+' INTEGER NOT NULL' for k in METRICS)}, requests INTEGER NOT NULL, account TEXT)""",
                f'CREATE UNIQUE INDEX usage_hourly_key ON usage_hourly({key})',
                'CREATE INDEX usage_hourly_route_ts ON usage_hourly(route,ts)',
                'DROP TRIGGER IF EXISTS events_rollup_insert','DROP TRIGGER IF EXISTS events_rollup_delete','DROP TRIGGER IF EXISTS events_rollup_update',
                f'CREATE TRIGGER events_rollup_insert AFTER INSERT ON events BEGIN {add} END',
                f'CREATE TRIGGER events_rollup_delete AFTER DELETE ON events BEGIN {remove} END',
                f'CREATE TRIGGER events_rollup_update AFTER UPDATE ON events BEGIN {remove} {add} END',
                self.ROLLUP_INSERT]:
                c.execute(statement)
            self.save_state(c,'rollup_version',3)

    ROLLUP_SELECT=('SELECT CAST(ts/3600 AS INTEGER)*3600,provider,route,model,project,agent_kind,'
                   +','.join(f'SUM({k})' for k in METRICS)+',SUM(requests),account FROM events GROUP BY 1,2,3,4,5,6,account')
    ROLLUP_INSERT='INSERT INTO usage_hourly '+ROLLUP_SELECT

    def rebuild_rollup(self):
        """Recompute usage_hourly from events in one transaction. The collector does this
        at start: a process running older code (no recursive triggers) may have written
        events while the triggers already existed."""
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            c.execute('DELETE FROM usage_hourly')
            c.execute(self.ROLLUP_INSERT)

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

    def event(self, c, key, ts, provider, route, model, values, session=None, project=None, agent_kind=None, account=None,
              parent_id=None, requests=1, allow_empty=False):
        # Streaming snapshots are monotonic maxima per globally stable request ID.
        numbers = [max(0, int(values.get(k) or 0)) for k in TOKENS]
        if not any(numbers) and not allow_empty:return
        details=[max(0,int(values.get(k) or 0)) for k in DETAILS]
        # TTL splits are one coherent observation, never independent maxima.
        # Conflicting splits of the same total stay unknown until a newer total.
        conflict='(excluded.cache_creation=events.cache_creation AND (events.cache_ttl_conflict OR '
        conflict+='(events.cache_ttl_observed AND excluded.cache_ttl_observed AND '
        conflict+='(events.cache_creation_5m<>excluded.cache_creation_5m OR events.cache_creation_1h<>excluded.cache_creation_1h))))'
        newer='(excluded.cache_creation>events.cache_creation OR (excluded.cache_creation=events.cache_creation AND NOT events.cache_ttl_observed))'
        detail_updates=','.join(f'{k}=CASE WHEN {conflict} THEN 0 WHEN {newer} THEN excluded.{k} ELSE events.{k} END'
                                for k in ('cache_creation_5m','cache_creation_1h','cache_ttl_observed'))
        detail_updates+=f',cache_creation_unknown=CASE WHEN {conflict} THEN events.cache_creation WHEN {newer} THEN excluded.cache_creation_unknown ELSE events.cache_creation_unknown END'
        detail_updates+=f',cache_ttl_conflict=CASE WHEN {conflict} THEN 1 WHEN excluded.cache_creation>events.cache_creation THEN 0 ELSE events.cache_ttl_conflict END'
        detail_updates+=',reasoning_observed=CASE WHEN excluded.output>events.output THEN excluded.reasoning_observed WHEN excluded.output=events.output THEN MAX(events.reasoning_observed,excluded.reasoning_observed) ELSE events.reasoning_observed END'
        detail_updates+=',advisor_calls=MAX(events.advisor_calls,excluded.advisor_calls),requests=excluded.requests'
        # A copy read without an account (local replay, telemetry) keeps a resolved one.
        columns=('id','ts','provider','route','model',*METRICS,'session','project','agent_kind','account','parent_id','requests')
        c.execute('INSERT INTO events ('+','.join(columns)+') VALUES ('+','.join('?' for _ in columns)+') ON CONFLICT(id) DO UPDATE SET '
                  + ','.join(f'{k}=MAX(events.{k},excluded.{k})' for k in TOKENS)
                  + ','+detail_updates
                  + ',provider=excluded.provider,model=excluded.model,route=excluded.route'
                  + ',session=COALESCE(excluded.session,events.session)'
                  + ',project=COALESCE(excluded.project,events.project)'
                  + ',agent_kind=COALESCE(excluded.agent_kind,events.agent_kind)'
                  + ',account=COALESCE(excluded.account,events.account),parent_id=COALESCE(excluded.parent_id,events.parent_id)',
                  (key, ts, provider, route, model or 'unknown', *numbers,*details,session,project,agent_kind,account,parent_id,requests))

    def codex_point(self,c,session,ts,provider,route,model,usage,last):
        keys=('input_tokens','cached_input_tokens','output_tokens','cache_write_input_tokens','reasoning_output_tokens')
        totals=[max(0,int(usage.get(k) or 0)) for k in keys]
        previous=[max(0,int(last.get(k) or 0)) for k in keys]
        # Timestamp and original cumulative counters survive Windows/WSL copies and replay.
        key=identity('codex-point',session,ts,totals)
        observed=int(usage.get('reasoning_output_tokens') is not None and last.get('reasoning_output_tokens') is not None)
        inserted=c.execute('INSERT INTO codex_points VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) '
                           "ON CONFLICT(id) DO UPDATE SET model=CASE WHEN codex_points.model='unknown' THEN excluded.model ELSE codex_points.model END,"
                           "provider=CASE WHEN codex_points.model='unknown' THEN excluded.provider ELSE codex_points.provider END,"
                           'reasoning_observed=MAX(codex_points.reasoning_observed,excluded.reasoning_observed) '
                           "WHERE (codex_points.model='unknown' AND excluded.model!='unknown') OR excluded.reasoning_observed>codex_points.reasoning_observed",
                           (key,identity(session),ts,provider,route,model or 'unknown',*totals,*previous,observed)).rowcount
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
        # Rebuilding token deltas must not silently migrate historical project labels.
        projects=dict(c.execute('SELECT id,project FROM events WHERE id IN (SELECT id FROM codex_points WHERE session=?)',(session,)))
        c.execute('DELETE FROM events WHERE id IN (SELECT id FROM codex_points WHERE session=?)',(session,))
        previous=None;previous_ts=None;seen={};resets=0;partial=False
        zero_baseline=False;zero_baselines=0;ambiguous=0;groups={};last_group=None
        for row in c.execute('SELECT * FROM codex_points WHERE session=? ORDER BY ts,input,output,id',(session,)).fetchall():
            current=[row[k] for k in ('input','cached','output','creation','reasoning')]
            last=[row[k] for k in ('last_input','last_cached','last_output','last_creation','last_reasoning')]
            # Repeated notifications are duplicates only within this counter epoch.
            # A reset may legitimately reach counters seen earlier in the session.
            if current==previous:
                groups[row['id']]=last_group
                continue
            reset=previous is not None and (current[0]<previous[0] or current[2]<previous[2])
            # Two different counters at the same timestamp have no temporal order.
            # A later repeat of that lower observation is not proof of a new epoch.
            if reset and seen.get(tuple(current))==previous_ts:
                groups[row['id']]=None
                continue
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
            last_group=row['id'] if any(delta) else None
            groups[row['id']]=last_group
            if not any(delta):continue
            inp,cached,out,creation,reasoning=delta
            self.event(c,row['id'],row['ts'],row['provider'],row['route'],row['model'],
                       dict(uncached_input=max(0,inp-cached-creation),cached_input=cached,output=out,
                            cache_creation=creation,reasoning=reasoning,reasoning_observed=row['reasoning_observed']),
                       **({**(attrs or {}),'project':projects[row['id']]} if row['id'] in projects else (attrs or {})))
        from .codex_usage import project
        project(self,c,session,groups,projects)
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
        quota_source=name in ('codex','claude-code','claude-oauth','antigravity','antigravity-app','opencode-go','devin') or name.startswith(('claude-oauth:','claude-code:'))
        if quota_source and status in ('error','unavailable'):
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
            c.execute('DELETE FROM resource_history WHERE checked<?',(retention,))
            self.save_state(c,'history_pruned',now)
            # What is older than any prune's cutoff is gone, even if retention_days grows later.
            self.save_state(c,'history_cutoff',max(retention,self.state(c,'history_cutoff',0)))

    def limit(self, c, route, bucket, remaining, resets, checked, source, metadata=None):
        from .resources import account, number, quota_context, SKEW
        received=time.time()
        if not number(checked) or checked>received+SKEW:
            return False
        resets=stamp(resets)
        owner=account(c,route)
        previous=c.execute('SELECT * FROM limits WHERE route=? AND bucket=?',(route,bucket)).fetchone()
        # Unidentified terminal replays do not replace a current account response.
        canonical=account_source(route) if is_claude(route) else 'codex' if route=='codex' else None
        prefer_account=bool(owner and owner['account_key'] and canonical and source!=canonical and previous
                            and previous['source']==canonical and 0<=checked-previous['checked']<=self.thresholds['stale_seconds'])
        changed=False
        if not prefer_account:
            changed=c.execute('''INSERT INTO limits VALUES (?,?,?,?,?,?) ON CONFLICT(route,bucket)
        DO UPDATE SET remaining=excluded.remaining,resets=excluded.resets,checked=excluded.checked,
        source=excluded.source WHERE excluded.checked >= limits.checked OR limits.checked>?''',
                  (route, bucket, remaining, resets, checked, source,received+SKEW)).rowcount>0
            if changed:
                meta=dict(metadata or {})
                if (owner and not owner['account_key']) or (canonical and source!=canonical):meta['identity_unverified']=True
                quota_context(c,route,bucket,checked,meta)
        # Local replay must not manufacture a historical stream at migration time.
        # Keep only observations made within the retention window.
        cutoff=time.time()-self.thresholds['retention_days']*86400
        if checked>=cutoff:
            c.execute('INSERT OR IGNORE INTO limit_history VALUES (?,?,?,?,?,?)',
                      (route,bucket,checked,remaining,resets,source))
        self.prune_history(c)
        return changed

    def limits(self, now=None, ended=None, names=None):
        """Quota rows; `ended` holds the subscriptions the user marked as ended and
        `names` the user's names for Claude accounts."""
        from .insights import quota_history,quota_pace,quota_plan,quota_trends,quota_decreases,quota_forecast,pace_lookback,window_seconds,token_total
        now = now or time.time()
        ended = set(ended or ())
        with self.connect() as c:
            c.execute('BEGIN')
            rows = [dict(r) for r in c.execute('SELECT * FROM limits ORDER BY route,bucket')]
            sources = {r['name']:dict(r) for r in c.execute('SELECT * FROM sources')}
            for row in rows:
                row['display_name']=self.state(c,'label:'+row['route']+':'+row['bucket'],row['bucket'])
            from .resources import inventory, SKEW
            accounts={r['route']:dict(r) for r in c.execute('SELECT * FROM resource_accounts')}
            contexts={(r['route'],r['bucket']):dict(r) for r in c.execute('SELECT * FROM quota_context')}
            from .claude_accounts import describe
            claude=describe(c,names,ended)
            # Other Claude accounts are quota routes of their own beside the original one.
            routes=list(dict.fromkeys([*SUPPORTED_ROUTES,*(a['route'] for a in claude),*(r['route'] for r in rows if is_claude(r['route']))]))
            policies={route:self.state(c,'quota_policy:'+route,{}) for route in routes}
            snapshots={route:self.state(c,'quota_snapshot:'+route,{}) for route in routes}
            resources=inventory(c,now,sources)
            history=quota_history(c,now,selected_sources={(r['route'],r['bucket']):r['source'] for r in rows},
                                  since_by_route={route:a['since'] for route,a in accounts.items()})
        # A sole non-default home still needs its account label beside the original route.
        labels={a['route']:a['label'] for a in claude} if any(a['route']!='claude-code' for a in claude) else {}
        for route in routes:
            if not any(r['route']==route for r in rows):
                rows.append(dict(route=route,bucket='미제공',remaining=None,resets=None,checked=None,source=route))
        for row in rows:
            checked, resets = row['checked'], row['resets']
            owner=accounts.get(row['route'])
            context=contexts.get((row['route'],row['bucket']),{})
            metadata=json.loads(context.get('data','{}'))
            previous_account=bool(owner and (context.get('epoch')!=owner['epoch'] or (checked or 0)<owner['since']))
            row.update(account_epoch=owner['epoch'] if owner else None,identity_verified=bool(owner and owner['account_key']),
                       previous_account=previous_account,scope=metadata)
            src = sources.get(row['source'], {})
            snapshot=snapshots.get(row['route'],{})
            omitted=bool(snapshot.get('source')==row['source'] and snapshot.get('checked',0)>=(checked or 0) and row['bucket'] not in snapshot.get('buckets',[]))
            row['omitted_latest']=omitted
            row['not_applicable']=omitted and row['bucket'] in snapshot.get('absent',[])
            # The account or plan changed and the new one's response no longer has this
            # window (a plan's 30-day window during a switch, an old model window): it is
            # an observation of the earlier conditions, not a current quota.
            row['retired']=bool(omitted and previous_account and snapshot.get('checked',0)>=owner['since'])
            row['family']=family(row['route']);row['account_label']=labels.get(row['route'])
            account=sources.get(account_source(row['route']),{}) if is_claude(row['route']) else {}
            if account.get('checked',0)>=(row['checked'] or 0) and account.get('status') in ('error','unavailable'):
                src=account
            user_ended=row['route'] in ended
            if not user_ended and src.get('status')=='ended' and str(src.get('detail','')).startswith(USER_ENDED_DETAIL):
                # Tracking was switched back on; the next poll replaces this marker.
                src={**src,'status':'unavailable','detail':'구독 추적 재개 · 다음 수집에서 한도를 다시 확인합니다.'}
            stale = bool(previous_account or (checked and (checked>now+SKEW or now-checked > self.thresholds['stale_seconds'] or (resets and resets <= now) or src.get('status')=='unavailable')))
            row.update(status='ended' if user_ended else 'error' if src.get('status')=='error' else 'ended' if src.get('status')=='ended' else 'unavailable' if omitted or row['remaining'] is None or src.get('status')=='unavailable' else 'stale' if stale else 'fresh',
                       stale=stale or user_ended, user_ended=user_ended, reset_kst=datetime.fromtimestamp(resets,KST).isoformat() if resets else None,
                       seconds_to_reset=max(0,int(resets-now)) if resets else None,
                       detail=USER_ENDED_DETAIL if user_ended else src.get('detail',''),last_attempt=src.get('checked'))
            row['history']=history.get((row['route'],row['bucket']),[])
            if previous_account:row['history']=[]
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
            parent_bucket=QUOTA_PARENTS.get((family(row['route']),row['bucket']))
            if row['route']=='codex' and row['bucket'].endswith(' · 300분'):
                parent_bucket=row['bucket'].removesuffix('300분')+'10080분'
            parent=by_key.get((row['route'],parent_bucket))
            blocked=parent and parent['status']=='fresh' and parent['remaining'] is not None and parent['remaining']<=0
            row['blocked_by']={k:parent[k] for k in ('bucket','resets','seconds_to_reset')} if blocked else None
            # The desktop app reports only the window that currently binds a model family,
            # so the other window keeps its last observation; say why it is not current.
            agy=re.fullmatch(r'(gemini|3p)-(5h|weekly)',row['bucket']) if row['route']=='antigravity' else None
            other=agy and by_key.get(('antigravity',f"{agy[1]}-{'weekly' if agy[2]=='5h' else '5h'}"))
            row['note']=('설정에서 구독 종료로 표시했습니다. 한도·초기화 조회를 멈췄고 사용 기록은 계속 집계합니다.' if row.get('user_ended') else
                         f"앱이 지금 {'주간' if agy[2]=='5h' else '5시간'} 창을 보고하고 있어 이 창의 현재값은 확인할 수 없습니다. 마지막 관측값입니다."
                         if other and row['status']!='fresh' and other['status']=='fresh' and other['source']=='antigravity-app' else
                         '계정·요금제 변경 전 관측입니다. 변경 후 계정 응답에 이 한도가 없어 현재 한도에서 제외했습니다.' if row.get('retired') else
                         '계정·구독 조건 변경 전 관측입니다.' if row.get('previous_account') else
                         '최근 계정 응답에서 이 한도를 확인하지 못했습니다. 이전 관측값입니다.' if row.get('omitted_latest') else
                         '로컬 관측의 계정을 확인하지 못했습니다. 계정 조회 후 작업 전망을 제공합니다.' if row['scope'].get('identity_unverified') else
                         '원본 초기화 시각을 해석하지 못했습니다. 시간 전망을 보류합니다.' if row['scope'].get('reset_unparsed') else None)
        sums=','.join(f'COALESCE(SUM({k}),0) AS {k}' for k in METRICS)+',COALESCE(SUM(requests),0) AS requests'
        with self.connect() as c:
            for row in rows:
                span=window_seconds(row['bucket'])
                if not row['resets'] or not span:continue
                start,end=row['resets']-span,min(now,row['resets'])
                if end<=start:continue
                where,args=route_events(row['route'])
                r=c.execute('SELECT '+sums+' FROM events WHERE '+where+' AND ts>=? AND ts<?',(*args,start,end)).fetchone()
                row['window']=dict(start=start,end=end,open=end<row['resets'],requests=r['requests'],
                                   tokens=token_total(dict(r)))
            from .signals import capacity,quota_events
            for row in rows:
                row['events']=quota_events(c,row['route'],row['bucket'],row['history_source'] or row['source'],now,
                                          since=accounts.get(row['route'],{}).get('since',0)) if row['checked'] else None
                row['capacity']=capacity(row)
                row['capacity_history']=[] if row['route'] in accounts else self.capacity_history(c,row,now)
        from .planning import enrich
        enrich(rows,now)
        return dict(limits=rows, sources=list(sources.values()), now=now,history_days=1,claude_accounts=claude,
                    resources=resources,quota_policies=policies,stale_seconds=self.thresholds['stale_seconds'],
                    retention_days=self.thresholds['retention_days'],low_percent=self.thresholds['low_percent'],
                    quota_hide_days=self.thresholds['quota_hide_days'],unused_percent=self.thresholds['unused_percent'],
                    unused_hours=self.thresholds['unused_hours'])

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
                where,args=route_events(row['route'])
                r=c.execute('SELECT COALESCE(SUM(uncached_input+cached_input+output+cache_creation),0),COALESCE(SUM(requests),0) FROM events '
                            'WHERE '+where+' AND ts>=? AND ts<?',(*args,resets-span,resets)).fetchone()
                memo[key]=dict(resets=resets,used_points=used,tokens=r[0],requests=r[1],tokens_per_point=r[0]/used if r[0] else None)
            if memo[key]['tokens_per_point']:result.append(memo[key])
        return result

    # Subscription accounts: one per route, Claude Code split by account (pending = original).
    ACCOUNT_SQL="CASE WHEN route='claude-code' THEN COALESCE(account,'claude-code') ELSE route END"
    GROUPS={'provider':'provider','route':'route','model':'model',
            'project':"COALESCE(project,'미분류')",'agent':"COALESCE(agent_kind,'미분류')",'account':ACCOUNT_SQL}
    SCOPE_KINDS={'route':'route','provider':'provider','model':'model',
                 'project':"COALESCE(project,'미분류')",'account':ACCOUNT_SQL}
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

    def account_tokens(self,c,route,buckets,checked):
        """Store the account's daily totals; a running day's bucket keeps growing."""
        namespace=self.account_namespace(c,route)
        changed=False
        for b in buckets:
            if not isinstance(b,dict):continue
            day,tokens=b.get('startDate'),b.get('tokens')
            if not isinstance(day,str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}',day) or type(tokens) is not int or tokens<0:continue
            try:datetime.strptime(day,'%Y-%m-%d')
            except ValueError:continue
            row=c.execute('SELECT tokens FROM account_token_buckets WHERE route=? AND namespace=? AND day=?',(route,namespace,day)).fetchone()
            if row and row[0]==tokens:continue
            c.execute('INSERT OR REPLACE INTO account_token_buckets VALUES (?,?,?,?,?)',(route,day,tokens,checked,namespace));changed=True
        if changed:self.bump_data_version(c)
        self.save_state(c,'account_tokens_checked:'+route+':'+namespace,checked)

    @staticmethod
    def account_namespace(c,route):
        from .resources import account
        owner=account(c,route)
        return (owner['account_key'] or 'epoch:'+owner['epoch']) if owner else ''

    def account_gap(self,c,route,now,days=14):
        """Account total minus the locally recorded tokens of the same route, per UTC
        day. Computed when read, so it shrinks as local collection catches up. Only
        the plain route counts: codex:<provider> traffic is not on this account."""
        since=(datetime.fromtimestamp(now,timezone.utc)-timedelta(days=days-1)).strftime('%Y-%m-%d')
        rows=c.execute('SELECT day,tokens FROM account_token_buckets WHERE route=? AND namespace=? AND day>=? ORDER BY day',(route,self.account_namespace(c,route),since)).fetchall()
        if not rows:return None
        from .codex_usage import account_filter
        account_sql,account_args=account_filter(self.account_namespace(c,route)) if route=='codex' else ('1=1',())
        local=dict(c.execute("SELECT date(ts,'unixepoch') AS day,SUM(uncached_input+cached_input+output+cache_creation) FROM usage_hourly"
                             ' WHERE route=? AND ts>=? AND '+account_sql+' GROUP BY day',(route,stamp(since+'T00:00:00Z'),*account_args)))
        out=[]
        for day,tokens in rows:
            mine=local.get(day,0)
            out.append(dict(day=day,account=tokens,local=mine,gap=max(0,tokens-mine),over=max(0,mine-tokens)))
        return dict(route=route,days=out,total_gap=sum(d['gap'] for d in out),checked=self.state(c,'account_tokens_checked:'+route+':'+self.account_namespace(c,route)))

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

    def usage(self, period='7d', start=None, end=None, granularity='day', group='provider', cumulative=False, now=None, pricing=None, subscriptions=None, compare=None, scope=None, value_alert_usd=None, sections='all', as_of=None, names=None, ended=None, price_history=None):
        if granularity not in BUCKET_SQL or group not in self.GROUPS or sections not in ('all','core','insights'):
            raise ValueError('집계 단위가 올바르지 않습니다.')
        scope_sql,scope_args='',()
        if scope:
            kind,_,value=scope.partition(':')
            if kind not in self.SCOPE_KINDS or not value:raise ValueError('범위가 올바르지 않습니다.')
            scope_sql=' AND '+self.SCOPE_KINDS[kind]+'=?';scope_args=(value,)
        now = datetime.fromtimestamp(now or time.time(),KST)
        if as_of is not None and (period!='1h' or isinstance(as_of,bool) or not isinstance(as_of,(int,float))
                                  or not math.isfinite(as_of) or not 0<as_of<=now.timestamp()):
            raise ValueError('최근 1시간의 기준 시각이 올바르지 않습니다.')
        # Pin the core and lazy detail requests to one second. Time passing must
        # evict records from this rolling range even when the collector is idle.
        cutoff=int(as_of if as_of is not None else now.timestamp()) if period=='1h' else None
        midnight = now.replace(hour=0,minute=0,second=0,microsecond=0)
        sums=','.join(f'COALESCE(SUM({k}),0) AS {k}' for k in METRICS)
        count='COALESCE(SUM(requests),0) AS requests'
        with self.connect() as c:
            c.execute('BEGIN')
            lifetime=dict(c.execute('SELECT '+sums+','+self.rcount+' FROM '+self.table+' WHERE 1=1'+scope_sql,scope_args).fetchone())
            collected_at=self.state(c,'last_local')
            # Every term that changes the numbers or the response shape is in the
            # key: event writes move lifetime, collection passes move last_local,
            # sum-preserving relabels move data_version.
            key=(period,start,end,granularity,group,cumulative,compare,scope,sections,value_alert_usd,
                 tuple(lifetime.values()),collected_at,self.state(c,'data_version') or 0,
                 midnight.date().isoformat(),cutoff,
                 json.dumps(pricing,sort_keys=True) if pricing else None,
                 json.dumps(subscriptions,sort_keys=True) if subscriptions else None,tuple(sorted(ended or ())),
                 json.dumps(price_history,sort_keys=True) if price_history else None)
            body=self._usage_get(key)
            if body is None:
                body=self._usage_body(c,period,start,end,granularity,group,cumulative,now,midnight,pricing,subscriptions,
                                      compare,scope,scope_sql,scope_args,value_alert_usd,sections,sums,count,lifetime,cutoff,set(ended or ()),
                                      price_history)
                if len(body['series'])<=5000:self._usage_put(key,body)
            body={**body,'prior':self._prior(c,body,period,compare,now,pricing,scope_sql,scope_args,sums,count) if sections!='insights' else None}
            from .reconciliation import apply as reconcile_usage
            body=reconcile_usage(c,body,group,cumulative,scope,pricing=pricing,calendar_start=(midnight-timedelta(days=181)).date().isoformat())
            sources,unavailable_routes=self._usage_live(c,body['rows'],now)
            from .claude_accounts import describe
            claude=describe(c,names)
            from .signals import usage_spike
            spike=usage_spike(c,now.timestamp()) if sections!='insights' else None
            month=self.project_month(c,now,pricing) if sections!='insights' else None
            gap=self.account_gap(c,'codex',now.timestamp()) if sections!='insights' else None
        data={**body,'sources':sources,'unavailable_routes':unavailable_routes,'collected_at':collected_at,'spike':spike,'project_month':month,'account_gap':gap,
              'claude_accounts':claude}
        if sections=='all':return data
        insight_keys={'insights','sessions','sessions_total','sessionless_requests','projects','calendar'}
        if sections=='core':return {k:v for k,v in data.items() if k not in insight_keys}
        shared={'reconciliation','period','start','end_exclusive','timezone','granularity','labels','series','subscriptions','sources','scope','collected_at'}
        return {k:v for k,v in data.items() if k in insight_keys|shared}

    def project_month(self,c,now,pricing=None):
        """Month-to-date (KST calendar month) tokens and estimated cost per project.

        cost is None when no token of the project has a rate; priced_share tells how
        much of the tokens the cost covers."""
        from .insights import token_total
        from .pricing import estimate,rate_for
        now=now if isinstance(now,datetime) else datetime.fromtimestamp(now,KST)
        start=now.replace(day=1,hour=0,minute=0,second=0,microsecond=0)
        sums=','.join(f'COALESCE(SUM({k}),0) AS {k}' for k in METRICS)
        out={}
        for r in c.execute("SELECT COALESCE(project,'미분류') AS project,model,date(ts,'unixepoch','+9 hours') AS day,"+sums+
                           ' FROM '+self.table+' WHERE ts>=? GROUP BY 1,2,3',(start.timestamp(),)):
            row=dict(r);tokens=token_total(row)
            e=out.setdefault(row['project'],dict(tokens=0,cost=0.0,priced_tokens=0))
            e['tokens']+=tokens
            rate=rate_for(row['model'],pricing,row['day']) if pricing else None
            if rate:e['cost']+=estimate(row,rate);e['priced_tokens']+=tokens
        from .reconciliation import supplements
        additions,_=supplements(c)
        residual=0
        for r in additions:
            if r['start']>=start.timestamp() and r['end']<=now.timestamp():
                # The Codex account residual belongs to no project; it is kept apart.
                if r['source']=='codex-account':residual+=r['tokens'];continue
                e=out.setdefault(r['project'] or '미분류',dict(tokens=0,cost=0.0,priced_tokens=0))
                e['tokens']+=r['tokens']
        for e in out.values():
            e['cost']=round(e['cost'],4) if e['priced_tokens'] else None
            e['priced_share']=e.pop('priced_tokens')/e['tokens']*100 if e['tokens'] else None
        return dict(month=start.strftime('%Y-%m'),projects=out,residual_tokens=residual)

    def project_detail(self,project,now=None,pricing=None,days=30,budget=None):
        """One project over the last `days` KST days: models, daily use, busiest sessions,
        and this month so far with a straight-line month-end projection (and the budget
        share when a budget is given). '미분류' is the bucket of records with no project."""
        from .insights import token_total
        from .pricing import estimate,rate_for
        now=now if isinstance(now,datetime) else datetime.fromtimestamp(now or time.time(),KST)
        midnight=now.replace(hour=0,minute=0,second=0,microsecond=0)
        start=midnight-timedelta(days=days-1);month_start=midnight.replace(day=1)
        sums=','.join(f'COALESCE(SUM({k}),0) AS {k}' for k in METRICS)
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
                'SELECT session,MAX(route),SUM(uncached_input+cached_input+output+cache_creation),SUM(requests),MIN(ts),MAX(ts) FROM events'
                +where+' AND session IS NOT NULL GROUP BY session ORDER BY 3 DESC',(project,start.timestamp()))]
        from .reconciliation import supplements
        with self.connect() as c:extra,conflicts=supplements(c)
        by_session={r['session']:r for r in sessions}
        unallocated=[]
        for e in extra:
            # The Codex account residual is listed apart from '미분류' (see reconciliation.RESIDUAL).
            if e['source']=='codex-account' or (e['project'] or '미분류')!=project or e['end']>now.timestamp():continue
            if e['start']>=month_start.timestamp():month['tokens']+=e['tokens']
            if e['start']<start.timestamp():continue
            m=models.setdefault((e['route'],e['model'] or 'unknown'),dict(route=e['route'],model=e['model'] or 'unknown',tokens=0,requests=0,cost=None))
            m['tokens']+=e['tokens']
            begin=datetime.fromtimestamp(e['start'],KST).date().isoformat()
            finish=datetime.fromtimestamp(e['end']-0.000001,KST).date().isoformat()
            if begin==finish:
                d=daily.setdefault(begin,dict(day=begin,tokens=0,requests=0));d['tokens']+=e['tokens']
            else:unallocated.append(dict(start=e['start'],end=e['end'],tokens=e['tokens']))
            if e['session']:
                session=by_session.get(e['session'])
                if session is None:
                    session=by_session[e['session']]=dict(session=e['session'],route=e['route'],tokens=0,requests=0,
                                                          first_ts=e['start'],last_ts=e['end'])
                    sessions.append(session)
                session['tokens']+=e['tokens']
                session['first_ts']=min(session['first_ts'],e['start'])
                session['last_ts']=max(session['last_ts'],e['end'])
        sessions=sorted(sessions,key=lambda r:(-r['tokens'],r['session']))[:10]
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
        return dict(project=project,days=days,start=start.strftime('%Y-%m-%d'),unallocated_intervals=unallocated,
                    models=sorted(models.values(),key=lambda m:-m['tokens']),
                    daily=[daily[k] for k in sorted(daily)],sessions=sessions,month=projection)

    @classmethod
    def session_rows(cls,c,start,end,pricing=None,scope=None,ids=None):
        """Request-only session rows in the selected range, optionally for a bounded ID batch.

        Reconciliation uses the same aggregation to recover local detail for a
        supplemented session that did not make the request-only top 100.
        """
        from .pricing import estimate,rate_for
        where='ts>=? AND ts<? AND session IS NOT NULL';args=[start,end]
        if scope:
            kind,_,value=scope.partition(':')
            where+=' AND '+cls.SCOPE_KINDS[kind]+'=?';args.append(value)
        if ids is not None:
            if not ids:return {}
            where+=' AND session IN ('+','.join('?' for _ in ids)+')';args.extend(ids)
        sums=','.join(f'COALESCE(SUM({k}),0) AS {k}' for k in METRICS)
        sessions={}
        for r in c.execute("SELECT session,model,date(ts,'unixepoch','+9 hours') AS day,"+cls.ACCOUNT_SQL+''' AS account,
                MAX(route) AS route,MAX(project) AS project,MAX(agent_kind) AS kind,MIN(ts) AS first_ts,MAX(ts) AS last_ts,
                COALESCE(SUM(requests),0) AS requests,'''+sums+' FROM events WHERE '+where+' GROUP BY session,model,day,4',args):
            e=sessions.setdefault(r['session'],dict(session=r['session'],route=r['route'],project=r['project'],
                kind=r['kind'],first_ts=r['first_ts'],last_ts=r['last_ts'],requests=0,cost=None,accounts=[],**dict.fromkeys(METRICS,0)))
            if r['route']=='claude-code' and r['account'] not in e['accounts']:e['accounts'].append(r['account'])
            e['first_ts']=min(e['first_ts'],r['first_ts']);e['last_ts']=max(e['last_ts'],r['last_ts'])
            e['requests']+=r['requests']
            for k in METRICS:e[k]+=r[k]
            rate=rate_for(r['model'],pricing,r['day']) if pricing else None
            if rate:e['cost']=(e['cost'] or 0)+estimate(dict(r),rate)
        for e in sessions.values():
            e['accounts'].sort()
            if e['cost'] is not None:e['cost']=round(e['cost'],2)
        return sessions

    def session_detail(self,session,pricing=None):
        """One session's models, hourly activity and cache share; no message content exists here."""
        from .insights import token_total
        from .pricing import estimate,rate_for
        sums=','.join(f'COALESCE(SUM({k}),0) AS {k}' for k in METRICS)
        with self.connect() as c:
            models=[];cost=0.0;rated=False
            for r in c.execute('''SELECT route,model,date(ts,'unixepoch','+9 hours') AS day,MIN(ts) AS first_ts,MAX(ts) AS last_ts,
                COALESCE(SUM(requests),0) AS requests,'''+sums+' FROM events WHERE session=? GROUP BY route,model,day',(session,)):
                row=dict(r);rate=rate_for(row['model'],pricing,row['day']) if pricing else None
                part=estimate(row,rate) if rate else None
                key=next((m for m in models if m['route']==row['route'] and m['model']==row['model']),None)
                if key is None:
                    key=dict(route=row['route'],model=row['model'],first_ts=row['first_ts'],last_ts=row['last_ts'],
                             requests=0,cost=None,**dict.fromkeys(METRICS,0));models.append(key)
                key['first_ts']=min(key['first_ts'],row['first_ts']);key['last_ts']=max(key['last_ts'],row['last_ts'])
                key['requests']+=row['requests']
                for k in METRICS:key[k]+=row[k]
                if part is not None:key['cost']=(key['cost'] or 0)+part;cost+=part;rated=True
            if not models:
                evidence=c.execute('SELECT * FROM usage_evidence WHERE session=? ORDER BY end DESC LIMIT 1',(session,)).fetchone()
                if not evidence:return dict(session=session,models=[],hours=[])
                models=[dict(route=evidence['route'],model=evidence['model'] or 'unknown',first_ts=evidence['start'],last_ts=evidence['end'],requests=0,cost=None,**dict.fromkeys(METRICS,0))]
            meta=c.execute('SELECT MAX(project),MAX(agent_kind) FROM events WHERE session=?',(session,)).fetchone()
            hours=[dict(hour=r[0]*3600,requests=r[1],tokens=r[2]) for r in c.execute(
                'SELECT CAST(ts/3600 AS INTEGER),SUM(requests),SUM(uncached_input+cached_input+output+cache_creation) '
                'FROM events WHERE session=? GROUP BY 1 ORDER BY 1',(session,))]
        totals={k:sum(m[k] for m in models) for k in METRICS}
        inputs=totals['uncached_input']+totals['cached_input']+totals['cache_creation']
        for m in models:m['cost']=round(m['cost'],4) if m['cost'] is not None else None
        data=dict(session=session,project=meta[0],kind=meta[1],first_ts=min(m['first_ts'] for m in models),
                    last_ts=max(m['last_ts'] for m in models),requests=sum(m['requests'] for m in models),totals=totals,
                    cache_share=totals['cached_input']/inputs*100 if inputs else None,cost=round(cost,4) if rated else None,
                    models=sorted(models,key=lambda m:-token_total(m)),hours=hours)
        from .reconciliation import supplements,add
        with self.connect() as c:extra,conflicts=supplements(c)
        data['unallocated_intervals']=[]
        for e in extra:
            if e['session']!=session:continue
            add(data['totals'],e)
            m=next((m for m in data['models'] if m['route']==e['route'] and m['model']==e['model']),None)
            if m is None:
                m=dict(route=e['route'],model=e['model'] or 'unknown',requests=0,cost=None,first_ts=e['start'],last_ts=e['end'],**dict.fromkeys(METRICS,0));data['models'].append(m)
            add(m,e)
            lo=int(e['start']//3600)*3600;hi=int((e['end']-0.000001)//3600)*3600
            if lo==hi:
                h=next((h for h in hours if h['hour']==lo),None)
                if h is None:h=dict(hour=lo,requests=0,tokens=0);hours.append(h)
                h['tokens']+=e['tokens']
            else:data['unallocated_intervals'].append(dict(start=e['start'],end=e['end'],tokens=e['tokens']))
        data['models'].sort(key=lambda m:-token_total(m));hours.sort(key=lambda h:h['hour'])
        inputs=sum(data['totals'][k] for k in ('uncached_input','cached_input','cache_creation'))
        data['cache_share']=data['totals']['cached_input']/inputs*100 if inputs else None
        return data

    def _price_segments(self,c,R,route,parts,pricing,scope_sql,scope_args):
        """Each price span's value of one route's records. Rollup rows are hourly, so an
        hour holding a price change is counted in the span it begins in."""
        from .pricing import estimate,rate_for
        from .insights import token_total
        cases=' '.join(f'WHEN ts<{float(hi)!r} THEN {i}' for i,(_,hi,_) in enumerate(parts[:-1]))
        sums=','.join(f'COALESCE(SUM({k}),0) AS {k}' for k in METRICS)
        result=[dict(start=datetime.fromtimestamp(lo,KST).isoformat(),end=datetime.fromtimestamp(hi,KST).isoformat(),
                     days=(hi-lo)/86400,monthly_usd=usd,cost=None,tokens=0) for lo,hi,usd in parts]
        for r in c.execute(f"SELECT CASE {cases} ELSE {len(parts)-1} END AS part,model,date(ts,'unixepoch','+9 hours') AS day,"+sums+
                           ' FROM '+R+' WHERE route=? AND ts>=? AND ts<?'+scope_sql+' GROUP BY 1,2,3',
                           (route,parts[0][0],parts[-1][1],*scope_args)):
            e=result[r['part']];e['tokens']+=token_total(dict(r))
            rate=rate_for(r['model'],pricing,r['day']) if pricing else None
            if rate:e['cost']=(e['cost'] or 0)+estimate(dict(r),rate)
        return result

    def _prior(self,c,body,period,compare,now,pricing,scope_sql,scope_args,sums,count):
        """The card comparison: the window before (or the chosen comparison) cut at the same
        elapsed time. Worked out on every request, not cached with the body, so the cut
        follows the clock even when no collection pass changes the cache key."""
        from .insights import token_total
        from .pricing import estimate,rate_for
        if period=='all':return None
        a,b=datetime.fromisoformat(body['start']),datetime.fromisoformat(body['end_exclusive'])
        total,first=body['totals'],body['first_record']
        mode=compare if compare in self.COMPARE_LABELS else 'previous'
        shift=self.COMPARE_SHIFTS.get(mode)
        a2=a-timedelta(days=shift) if shift else a-(b-a)
        effective=min(b,now)
        if effective<=a:return None
        stop=a2+(effective-a)
        prior=dict(mode=mode,label=self.COMPARE_LABELS[mode],start=a2.isoformat(),end_exclusive=stop.isoformat(),
                   current_end=effective.isoformat(),running=effective<b,complete=True,
                   current_tokens=token_total(total),requests=0,cost=None,**dict.fromkeys(METRICS,0))
        spent=None
        for r in c.execute('SELECT model,date(ts,\'unixepoch\',\'+9 hours\') AS day,'+sums+','+count+' FROM events WHERE ts>=? AND ts<?'
                           +scope_sql+' GROUP BY model,day',(a2.timestamp(),stop.timestamp(),*scope_args)):
            for k in METRICS:prior[k]+=r[k]
            prior['requests']+=r['requests']
            rate=rate_for(r['model'],pricing,r['day']) if pricing else None
            if rate:spent=(spent or 0)+estimate(dict(r),rate)
        if spent is not None:prior['cost']=round(spent,4)
        # Local records take part only when either side has some; then they must
        # reach back to the earlier window (reconciliation checks its sources too).
        if (token_total(prior) or token_total(total)) and not (first is not None and first<=a2.timestamp()):prior['complete']=False
        return prior

    def _usage_body(self,c,period,start,end,granularity,group,cumulative,now,midnight,pricing,subscriptions,
                    compare,scope,scope_sql,scope_args,value_alert_usd,sections,sums,count,lifetime,cutoff,ended=frozenset(),
                    price_history=None):
        first = c.execute('SELECT MIN(ts) FROM events WHERE 1=1'+scope_sql,scope_args).fetchone()[0]
        if period=='all':
            from .reconciliation import supplements,matches
            extra,_=supplements(c)
            starts=[e['start'] for e in extra if matches(e,scope)]
            if starts:first=min([first] + starts) if first is not None else min(starts)
        if period=='1h':
            b=datetime.fromtimestamp(cutoff,KST)
            a=b-timedelta(hours=1)
        elif period=='custom':
            a = datetime.strptime(start,'%Y-%m-%d').replace(tzinfo=KST)
            b = datetime.strptime(end,'%Y-%m-%d').replace(tzinfo=KST)+timedelta(days=1)
        elif period in ('today','7d','30d','all'):
            a = (datetime.fromtimestamp(first,KST).replace(hour=0,minute=0,second=0,microsecond=0)
                 if first and period=='all' else midnight-timedelta(days={'today':0,'7d':6,'30d':29,'all':0}[period]))
            b = midnight+timedelta(days=1)
        else:
            raise ValueError('기간이 올바르지 않습니다.')
        intervals=calendar_buckets(a,b,granularity)
        # A rolling hour cuts through two hourly rollups. Read original events
        # at both boundaries so charts, models, sessions and totals agree.
        R,rcount=('events',count) if period=='1h' else (self.table,self.rcount)
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
            e = buckets.setdefault((r['bucket'],r['label']),dict(requests=0,cost=0.0,unpriced=0,**dict.fromkeys(METRICS,0)))
            for k in METRICS: e[k]+=r[k]
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
                e=cmp_buckets.setdefault((r['bucket'],r['label']),dict(requests=0,cost=0.0,**dict.fromkeys(METRICS,0)))
                for k in METRICS: e[k]+=r[k]
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
        # The period cards always compare with the window before (or the chosen comparison)
        # cut at the same elapsed time. A window that starts before the first record is
        # not complete, so no change is computed from it.
        session_list=[];sessionless=0;project_list=[];sessions_total=0
        if sections!='core':
            sessions=self.session_rows(c,a.timestamp(),b.timestamp(),pricing,scope)
            sessions_total=len(sessions)
            session_list=sorted(sessions.values(),key=lambda s:-(s['uncached_input']+s['cached_input']+s['output']+s['cache_creation']))[:100]
            sessionless=c.execute('SELECT COALESCE(SUM(requests),0) FROM events WHERE ts>=? AND ts<? AND session IS NULL'+scope_sql,
                                  (a.timestamp(),b.timestamp(),*scope_args)).fetchone()[0]
            projects={}
            project_sessions=dict(c.execute("SELECT COALESCE(NULLIF(project,''),'미분류'),COUNT(DISTINCT session) FROM events WHERE ts>=? AND ts<?"
                +scope_sql+" GROUP BY COALESCE(NULLIF(project,''),'미분류')",(a.timestamp(),b.timestamp(),*scope_args)))
            for r in c.execute('''SELECT project,model,date(ts,'unixepoch','+9 hours') AS day,MAX(ts) AS last_ts,'''+count+','+sums+'''
                FROM events WHERE ts>=? AND ts<?'''+scope_sql+' GROUP BY project,model,day''',(a.timestamp(),b.timestamp(),*scope_args)):
                label=r['project'] or '미분류'
                e=projects.setdefault(label,dict(project=label,sessions=project_sessions[label],last_ts=r['last_ts'],requests=0,cost=0.0,rated=False,
                    **dict.fromkeys(METRICS,0)))
                e['last_ts']=max(e['last_ts'],r['last_ts']);e['requests']+=r['requests']
                for k in METRICS: e[k]+=r[k]
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
            scope_options['account']=[v for v in scope_options['account'] if is_claude(v)]
            if len(scope_options['account'])<2:scope_options.pop('account')
        from .insights import usage_insights,token_total
        insights=None
        if sections!='core':
            insights=usage_insights(c,a,b,now,period,first,total,rows,buckets,scope_sql,scope_args)
            insights['month']=month
        cost={'period':None,'lifetime':None,'coverage':None,'lifetime_coverage':None,'cache_savings':None,'cache_estimated_tokens':0}
        if pricing:
            from .pricing import cache_estimated_tokens
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
                    cost['cache_estimated_tokens']+=cache_estimated_tokens(dict(r),rate)
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
        claude_routes=[r[0] for r in c.execute('SELECT route FROM claude_accounts')]
        # A sole identified non-default home still has its own subscription route.
        separate_claude=any(route!='claude-code' for route in claude_routes)
        # Several Claude accounts: the Claude Code total split by account, 계정 미확인
        # included, so the parts add up to the service (reconciliation adds supplements).
        claude_split=None
        if separate_claude and period!='1h':
            split={}
            for r in c.execute('SELECT '+self.ACCOUNT_SQL+' AS account,model,date(ts,\'unixepoch\',\'+9 hours\') AS day,'+sums+','+rcount+
                               ' FROM '+R+" WHERE route='claude-code' AND ts>=? AND ts<?"+scope_sql+' GROUP BY 1,2,3',
                               (a.timestamp(),b.timestamp(),*scope_args)):
                e=split.setdefault(r['account'],dict(account=r['account'],tokens=0,requests=0,cost=None))
                e['tokens']+=token_total(dict(r));e['requests']+=r['requests']
                rate=rate_for(r['model'],pricing,r['day']) if pricing else None
                if rate:e['cost']=(e['cost'] or 0)+estimate(dict(r),rate)
            claude_split=sorted(split.values(),key=lambda e:(-e['tokens'],e['account']))
        if (subscriptions or separate_claude) and period!='1h':
            by_route={};tokens_by_route={}
            for row in rows:
                tokens_by_route[row['route']]=tokens_by_route.get(row['route'],0)+token_total(row)
                if row.get('est_cost') is not None:
                    by_route[row['route']]=by_route.get(row['route'],0)+row['est_cost']
            if claude_split is not None:
                # Each Claude subscription is valued with its own account's requests.
                by_route={k:v for k,v in by_route.items() if k!='claude-code'}
                tokens_by_route.pop('claude-code',None)
                for e in claude_split:
                    tokens_by_route[e['account']]=e['tokens']
                    if e['cost'] is not None:by_route[e['account']]=e['cost']
            # Scale by the elapsed part of the period: a running week is not seven days yet.
            days=max(1,(min(b,now)-a).total_seconds()/86400)
            prices=dict(subscriptions or {})
            # Several Claude accounts: list each one, priced or not, so a missing price is visible.
            if separate_claude:
                for route in claude_routes:
                    if route not in ended:prices.setdefault(route,None)
            from .subscriptions import MONTH_DAYS,clean_history,segments,weighted
            history=clean_history(price_history)
            lo,hi=a.timestamp(),min(b,now).timestamp()
            for route,monthly in prices.items():
                if monthly is not None and (not isinstance(monthly,(int,float)) or monthly<0):continue
                est=by_route.get(route)
                # The value is set against the prices of the same span: a price changed
                # later does not re-value earlier use.
                parts=segments(monthly,history.get(route),lo,hi) if monthly is not None else []
                # No tokens in the period is not the same as tokens without a list price.
                subs.append(dict(route=route,monthly_usd=monthly,period_cost=est,period_tokens=tokens_by_route.get(route,0),
                                 monthly_equiv=est*MONTH_DAYS/days if est is not None else None,
                                 effective_usd=weighted(parts) if parts else monthly,
                                 price_segments=self._price_segments(c,R,route,parts,pricing,scope_sql,scope_args)
                                 if len(parts)>1 and (claude_split is None or not is_claude(route)) else None))
        labels = sorted({r['label'] for r in buckets})
        lookup = {(r['bucket'],r['label']):r for r in buckets}
        running = {label:dict(requests=0,cost=0.0,unpriced=0,**dict.fromkeys(METRICS,0)) for label in labels}
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
        return dict(period=period,start=a.isoformat(),end_exclusive=b.isoformat(),timezone='Asia/Seoul',totals=total,lifetime=lifetime,
                    granularity=granularity,heatmap=heatmap,cost=cost,
                    rows=rows,series=series,labels=labels,first_record=first,insights=insights,
                    calendar=calendar,subscriptions=subs,claude_split=claude_split,sessions=session_list,sessions_total=sessions_total,sessionless_requests=sessionless,projects=project_list,compare=compare_data,
                    scope=scope,scope_options=scope_options,
                    coverage='로컬·클라우드 요청 기록과 서버 누계를 중복 없이 대조합니다. KST 기준으로 배분 가능한 토큰은 차트에 포함하고, 일자를 확정할 수 없는 계정·세션 누계는 미배분으로 표시합니다. 계정 차이를 dot 사용량으로 단정하지 않습니다. 다른 PC의 사용량은 미수집입니다. Antigravity는 로컬 대화 DB, Devin은 로컬 sessions.db의 요청별 사용량을 집계합니다. 삭제된 기록과 원본 모델명이 없는 요청은 완전히 복구할 수 없습니다. 직접 API 과금은 분석하지 않습니다.',
                    token_note='출력은 추론을 포함하며, 추론 미제공과 실제 0을 수신 건수로 구분합니다. 캐시 생성의 5분·1시간·미확인 값은 생성 합계의 세부 항목입니다. Advisor 토큰은 포함하고 내부 호출 수는 API 요청 수와 따로 표시합니다. 보존 시간이나 해당 단가가 없으면 캐시 생성 기본 단가로 추정합니다.')

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
        # The last success lets the page tell a single failed poll from a lasting failure.
        health=dict(c.execute('SELECT name,last_success FROM source_health').fetchall())
        sources = [dict(r,last_success=health.get(r['name'])) for r in c.execute('SELECT * FROM sources ORDER BY name')]
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
            from .verify import partial
            bad=not verify.get('ok')
            sections=('antigravity','devin','codex_original','claude','opencode')
            misses=sum((verify.get(s) or {}).get('missing_events',0) for s in sections)
            mismatch=verify.get('codex',{}).get('mismatches',0)+sum((verify.get(s) or {}).get('mismatched_events',0) for s in sections)
            errors=sum((verify.get(s) or {}).get(k,0) for s in sections
                       for k in ('source_errors','source_conflicts','alias_errors','output_split_errors'))
            incomplete=partial(verify)
            sources.append(dict(name='원본 대조',checked=verify.get('ts'),
                status='error' if bad else 'stale' if time.time()-verify.get('ts',0)>86400+stale else 'partial' if incomplete else 'ok',
                detail=verify.get('error') or ((f'누락 {misses}건 · 불일치 {mismatch}건 · 오류 {errors}건'
                       +(' · 시간별 집계가 원본과 달라 다시 계산함' if (verify.get('rollup') or {}).get('matches') is False else '')) if bad else
                       '확인 가능한 원본과 일치 · 미수집·해석 불가·과거 원본 부족 또는 세부값 상충이 있어 일부만 확인됨' if incomplete else '원본과 일치')))
        else:
            sources.append(dict(name='원본 대조',checked=None,status='unavailable',detail='아직 실행되지 않음'))
        sources.append(self.storage_row(self.state(c,'last_local')))
        ops_rows={'수집기','일일 백업','원본 대조','데이터베이스'}
        for source in sources:
            # Ops pseudo-rows carry daily cadences; they compute staleness themselves. A
            # finished one-time job (the attribution backfill) keeps its result: its age
            # is not an old value.
            if source['name'] in ops_rows or source['name'] in ONE_TIME_SOURCES:continue
            # Resource reads run every 15 minutes, for every Claude account ('claude-credits:<id>').
            source_stale=max(1800,self.thresholds['stale_seconds']) if source['name'].split(':')[0] in ('claude-credits','claude-resets') else self.thresholds['stale_seconds']
            if source['name']=='codex-consumption-daily':source_stale=max(source_stale,7200)
            elif source['name'] in ('codex-activity','codex-consumption-threads'):source_stale=max(source_stale,1800)
            if source['status']=='ok' and now.timestamp()-source['checked']>source_stale:
                source['status']='stale'
        # A status line reports only while a terminal UI runs it (not the desktop apps
        # or headless workers). After a day without a receipt it is shown as unused,
        # not as a failing source; the quota stays visible through account/app polling.
        for source in sources:
            if ((source['name'] in STATUS_LINE_SOURCES or source['name'].startswith('claude-code:')) and source['status']=='stale' and source.get('checked')
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
