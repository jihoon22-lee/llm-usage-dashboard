"""Which Claude account produced each locally recorded Claude Code request.

Accounts in different config homes (zerosub/paseo CLAUDE_CONFIG_DIR) share one
transcript folder through a symlink, and transcripts carry no account. Evidence
points (session, time, hashed account) come from two native sources:

* the llm-usage Claude Code hook (claudehook.py): every SessionStart,
  UserPromptSubmit and Stop of every process, under that process's home;
* Claude Code's own process registry (<home>/sessions/<pid>.json), read while the
  process runs: its start time, and the start of each busy turn.

A request belongs to the account of the latest evidence point of its session at or
before its time. An account switch always starts a new process, and turns are never
moved mid-way, so the producing process left the latest point. Without evidence,
the transcript folder decides: a folder only one account writes is that account's;
in a shared folder, requests before a second account first appeared belong to the
folder's own account. Anything else is 'claude-code:unknown' - never guessed.
"""
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import time

from .claude_accounts import FAMILY, UNKNOWN, home_candidates, inspect, observed_homes, route_for, bind_primary,transcript_sources

# Evidence may be stamped a moment after the request it explains (clock jitter).
TOLERANCE = 2
SESSION = re.compile(r'^[A-Za-z0-9._-]{1,100}$')
KEY = re.compile(r'^[0-9a-f]{64}$')
HOOK_EVENTS = {'SessionStart', 'UserPromptSubmit', 'Stop'}
RETENTION = 120 * 86400


def initialize(c):
    c.executescript('''
      CREATE TABLE IF NOT EXISTS claude_evidence (
        session TEXT NOT NULL, ts REAL NOT NULL, account_key TEXT NOT NULL, kind TEXT NOT NULL,
        PRIMARY KEY(session, ts, kind, account_key));
      CREATE TABLE IF NOT EXISTS claude_dirty (session TEXT PRIMARY KEY, since REAL NOT NULL);
      CREATE INDEX IF NOT EXISTS claude_evidence_request ON claude_evidence(kind,account_key);
    ''')


def account_for(ts, evidence, policy, primary_route):
    """Pure decision for one request. evidence: sorted [(ts, route)]; policy:
    ('single', route) | ('shared', native route, cutover) | None (folder unknown)."""
    latest = None;point_time=None;ambiguous=False
    for point, route in evidence:
        if point > ts + TOLERANCE:
            break
        if point_time==point and latest!=route:ambiguous=True
        elif point_time!=point:ambiguous=False
        latest = route;point_time=point
    if latest:
        return UNKNOWN if ambiguous else latest
    if policy and policy[0] == 'single':
        return policy[1]
    if policy and policy[0] == 'shared':
        return policy[1] if ts < policy[2] else UNKNOWN
    return UNKNOWN


def _finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


# Points this process already stored: the registry repeats them every few seconds.
_stored = set()


def add_evidence(c, points):
    """Insert (session, ts, key, kind); mark sessions whose new points may move requests."""
    dirty = {}
    for session, ts, key, kind in points:
        if not (isinstance(session, str) and SESSION.match(session) and _finite(ts) and isinstance(key, str) and KEY.match(key)):
            continue
        point = (c.execute('PRAGMA database_list').fetchone()[2], session, float(ts), key, kind)
        if point in _stored:
            continue
        if c.execute('INSERT OR IGNORE INTO claude_evidence VALUES (?,?,?,?)', (session, float(ts), key, kind)).rowcount:
            dirty[session] = min(dirty.get(session, ts), ts)
        else:
            # Remember only points already committed, so a rolled-back insert is retried.
            if len(_stored) > 50000:
                _stored.clear()
            _stored.add(point)
    for session, since in dirty.items():
        c.execute('INSERT INTO claude_dirty VALUES (?,?) ON CONFLICT(session) DO UPDATE SET since=MIN(since,excluded.since)',
                  (session, since - TOLERANCE))
    return len(dirty)


def read_hooks(store, inbox):
    """Copy new hook markers from the inbox database (read-only)."""
    path = Path(inbox)/'claude-accounts.db'
    if not path.exists():
        return 0, []
    key = 'claude-hooks:' + str(path)
    with store.connect() as c:
        last = store.state(c, key, 0) or 0
    source = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=2)
    try:
        top = source.execute('SELECT MAX(id) FROM markers').fetchone()[0] or 0
        if top < last:
            last = 0  # the inbox database was recreated
        rows = source.execute('SELECT id,ts,session,event,account,home FROM markers WHERE id>? ORDER BY id LIMIT 20000', (last,)).fetchall()
    finally:
        source.close()
    if not rows:
        return 0, []
    now = time.time()
    points, homes = [], []
    for _, ts, session, event, account, home in rows:
        if event not in HOOK_EVENTS or not _finite(ts) or not 0 < ts <= now + 60:
            continue
        if account:
            points.append((session, ts, account, 'hook:' + event))
        if isinstance(home, str) and home.startswith('/') and len(home) <= 300:
            homes.append(home)
    with store.connect() as c:
        added = add_evidence(c, points)
        store.save_state(c, key, rows[-1][0])
    return added, homes


def registry_points(homes):
    """Evidence from Claude Code's live process registry of each identified home.

    A registry folder shared by homes of different accounts cannot attribute anything."""
    owners = {}
    for home in homes:
        if home['kind'] == 'windows' or not home.get('key'):
            continue
        try:
            folder = os.path.realpath(Path(home['path'])/'sessions')
        except (OSError, ValueError):
            continue
        owners.setdefault(folder, set()).add(home['key'])
    points = []
    for folder, keys in owners.items():
        if len(keys) != 1:
            continue
        key = next(iter(keys))
        try:
            files = sorted(Path(folder).glob('*.json'))
        except OSError:
            continue
        for file in files[:500]:
            try:
                data = json.loads(file.read_text())
            except (OSError, ValueError):
                continue
            if not isinstance(data, dict):
                continue
            session = data.get('sessionId')
            started = data.get('startedAt')
            if _finite(started):
                points.append((session, started/1000, key, 'process:start'))
            changed = data.get('statusUpdatedAt')
            if data.get('status') == 'busy' and _finite(changed):
                points.append((session, changed/1000, key, 'process:busy'))
    return points


def remember_homes(store, c, homes):
    known = observed_homes(store, c)
    merged = list(dict.fromkeys([*known, *homes]))[-20:]
    if merged != known:
        store.save_state(c, 'claude_homes_observed', merged)


def collect_evidence(store, settings):
    """Hook markers from every status inbox and the live process registries."""
    observed = []
    errors = 0
    for inbox in settings.get('status_inboxes', []):
        try:
            _, homes = read_hooks(store, inbox)
            observed += homes
        except (OSError, sqlite3.Error, ValueError, TypeError):
            errors += 1
    with store.connect() as c:
        if observed:
            remember_homes(store, c, [h for h in observed if Path(h).is_absolute()])
        known = observed_homes(store, c)
    homes = [inspect(h) for h in home_candidates(settings, known)]
    with store.connect() as c:
        add_evidence(c, registry_points(homes))
    return errors


def policies(store, settings, homes, primary):
    """Transcript folder -> ('single', route) or ('shared', native route, cutover)."""
    result = {}
    with store.connect() as c:
        roots = [Path(s['path']) for s in transcript_sources(settings,observed_homes(store,c)) if s.get('route') == FAMILY and s.get('kind') == 'jsonl']
        for root in roots:
            try:
                real = os.path.realpath(root)
            except (OSError, ValueError):
                continue
            owners, joined = [], []
            for home in homes:
                link = Path(home['path'])/'projects'
                try:
                    if os.path.realpath(link) != real:
                        continue
                    if link.is_symlink():
                        joined.append(os.lstat(link).st_mtime)
                except (OSError, ValueError):
                    continue
                owners.append(home)
            # A home without a readable identity is a separate (unknown) owner.
            keys = {h['key'] for h in owners}
            native = next((h for h in owners if not (Path(h['path'])/'projects').is_symlink()), owners[0] if owners else None)
            native_route = route_for(native['key'], primary) if native and native['key'] else FAMILY
            state_key = 'claude_cutover:' + str(root)
            stored = store.state(c, state_key)
            # Once shared, a folder stays shared: later requests without evidence
            # may still come from the account that joined it.
            if len(keys) <= 1 and not _finite(stored):
                only = next(iter(keys), None)
                result[str(root)] = ('single', route_for(only, primary) if only else FAMILY)
                continue
            cutover = min([v for v in (stored, *joined) if _finite(v)] or [time.time()])
            if stored != cutover:
                store.save_state(c, state_key, cutover)
            result[str(root)] = ('shared', native_route, cutover)
    return result


def session_roots(c, roots, *, unambiguous=False):
    """Session ID -> transcript root, from the collector's own file checkpoints.

    A repair asks for unambiguous roots: copies in different folders prove no owner.
    """
    found = {}
    for root in roots:
        prefix = 'file:' + root + '/'
        for (key,) in c.execute('SELECT key FROM state WHERE substr(key,1,?)=?', (len(prefix), prefix)):
            path = Path(key[5:])
            session = path.parent.parent.name if path.parent.name == 'subagents' else path.stem
            if SESSION.match(session):
                if session not in found:
                    found[session] = root
                elif unambiguous and found[session] is not None and os.path.realpath(found[session]) != os.path.realpath(root):
                    found[session] = None
    return found


def repair_unbound_folders(c, rules, primary):
    """Repair the old default-route fallback only before any primary account was bound.

    The known owner must name one retained transcript root. Cloud assignments and
    already resolved non-default accounts are not candidates; retained turn evidence
    still takes precedence over a folder. No token or request identity is rewritten.
    """
    updates = []
    for session, root in session_roots(c, list(rules), unambiguous=True).items():
        policy = rules.get(root)
        if not policy or policy[1] in (FAMILY, UNKNOWN):
            continue
        evidence = [(ts, route_for(key, primary)) for ts, key in c.execute(
            'SELECT ts,account_key FROM claude_evidence WHERE session=? ORDER BY ts', (session,))]
        for event_id, ts in c.execute("""SELECT id,ts FROM events WHERE route='claude-code' AND session=? AND account=?
                AND NOT EXISTS (SELECT 1 FROM usage_origins o WHERE o.event_id=events.id AND o.source LIKE 'claude-cloud:%')""",
                (session, FAMILY)):
            target = account_for(ts, evidence, policy, FAMILY)
            if target != FAMILY:
                updates.append((target, event_id))
    c.executemany('UPDATE events SET account=? WHERE id=?', updates)
    return len(updates)


def resolve(store, settings, limit=None):
    """Assign pending requests and re-check sessions with new evidence."""
    return _resolve(store, settings, limit)[0]


def _resolve(store, settings, limit=None):
    known = []
    with store.connect() as c:
        known = observed_homes(store, c)
    homes = [inspect(h) for h in home_candidates(settings, known)]
    with store.connect() as c:
        unbound = False
        if not store.state(c, 'claude_accounts:folder-routes-v1'):
            unbound = store.state(c, 'claude_accounts:folder-repair-unbound')
            if unbound is None:
                unbound = not store.state(c, 'claude_primary_account')
                # Binding a newly signed-in default home commits before resolution.
                # Keep the pre-upgrade condition if a later repair transaction fails.
                store.save_state(c, 'claude_accounts:folder-repair-unbound', unbound)
        primary = bind_primary(store, c, homes)
    accounts = len({h['key'] for h in homes if h['key']})
    with store.connect() as c:
        idle = (store.state(c, 'claude_accounts:cloud-v1') and store.state(c, 'claude_accounts:folder-routes-v1')
                and not c.execute('SELECT 1 FROM claude_dirty LIMIT 1').fetchone()
                and not c.execute("SELECT 1 FROM events WHERE route='claude-code' AND account IS NULL LIMIT 1").fetchone())
    if idle:
        return 0, accounts  # nothing pending: no write transaction every few seconds
    rules = policies(store, settings, homes, primary)
    shared = [p[2] for p in rules.values() if p[0] == 'shared']
    fallback = ('shared', FAMILY, min(shared)) if shared else ('single', FAMILY)
    changed = 0
    with store.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        if not store.state(c, 'claude_accounts:cloud-v1'):
            # Cloud sessions were read with the original account's login only.
            c.execute("""UPDATE events SET account=? WHERE route='claude-code' AND account IS NULL AND id IN
                         (SELECT event_id FROM usage_origins WHERE source LIKE 'claude-cloud:%')""", (FAMILY,))
            store.save_state(c, 'claude_accounts:cloud-v1', True)
        if not store.state(c, 'claude_accounts:folder-routes-v1'):
            changed = repair_unbound_folders(c, rules, primary) if unbound else 0
            store.save_state(c, 'claude_accounts:folder-routes-v1', dict(checked=time.time(), unbound=unbound, repaired=changed))
            c.execute("DELETE FROM state WHERE key='claude_accounts:folder-repair-unbound'")
        roots = session_roots(c, list(rules))
        work = {r[0]: r[1] for r in c.execute('SELECT session,since FROM claude_dirty')}
        for (session,) in c.execute("SELECT DISTINCT session FROM events WHERE route='claude-code' AND account IS NULL"):
            work.setdefault(session, None)
        for session, since in list(work.items())[:limit]:
            routes = {}
            evidence = [];requests={}
            if session is not None:
                for ts, key,kind in c.execute('SELECT ts,account_key,kind FROM claude_evidence WHERE session=? ORDER BY ts', (session,)):
                    routes.setdefault(key, route_for(key, primary) if primary else FAMILY+':'+key[:12])
                    evidence.append((ts, routes[key]))
                    if kind.startswith('telemetry:'):requests.setdefault(kind[len('telemetry:'):],set()).add(routes[key])
            policy = rules.get(roots.get(session)) if session is not None else None
            policy = policy or fallback
            where = "route='claude-code' AND session IS ?"
            args = [session]
            if since is not None:
                where += ' AND (ts>=? OR account IS NULL)'; args.append(since)
            else:
                where += ' AND account IS NULL'
            # A cloud copy was assigned by the account that read it.
            where += " AND NOT EXISTS (SELECT 1 FROM usage_origins o WHERE o.event_id=events.id AND o.source LIKE 'claude-cloud:%')"
            updates = []
            for event_id, ts, current,parent in c.execute(f'SELECT id,ts,account,parent_id FROM events WHERE {where}', args).fetchall():
                direct=requests.get(parent or event_id,set())
                target = next(iter(direct)) if len(direct)==1 else UNKNOWN if direct else account_for(ts, evidence, policy, FAMILY)
                if target != current:
                    updates.append((target, event_id))
            c.executemany('UPDATE events SET account=? WHERE id=?', updates)
            changed += len(updates)
            if session is not None:
                c.execute('DELETE FROM claude_dirty WHERE session=?', (session,))
        if time.time() - (store.state(c, 'claude_evidence:pruned', 0) or 0) >= 3600:
            c.execute('DELETE FROM claude_evidence WHERE ts<?', (time.time() - RETENTION,))
            store.save_state(c, 'claude_evidence:pruned', time.time())
        if changed:
            store.bump_data_version(c)
    return changed, accounts


def summarize(store, c, accounts):
    """Status row of account attribution, shown once a second Claude account exists."""
    if accounts < 2:
        return
    hooks = c.execute("SELECT COUNT(*),MAX(ts) FROM claude_evidence WHERE kind LIKE 'hook:%'").fetchone()
    unknown = c.execute("SELECT COALESCE(SUM(requests),0) FROM events WHERE route='claude-code' AND account=?", (UNKNOWN,)).fetchone()[0]
    recent = c.execute("SELECT COALESCE(SUM(requests),0) FROM events WHERE route='claude-code' AND account=? AND ts>=?",
                       (UNKNOWN, time.time()-86400)).fetchone()[0]
    detail = (f'Claude 계정 {accounts}개 · 훅 기록 {hooks[0]}건' + ('' if hooks[0] else ' (훅 미설치 · 프로세스 기록만 사용)')
              + f' · 계정 미확인 요청 {unknown}건' + (f' (최근 24시간 {recent}건)' if recent else ''))
    store.source_changed(c, 'claude-accounts', 'partial' if recent or not hooks[0] else 'ok', detail)


_summarized = {}


def refresh(store, settings, *, evidence=True):
    """Evidence then resolution; failures leave earlier attributions untouched."""
    try:
        errors = collect_evidence(store, settings) if evidence else 0
        changed, accounts = _resolve(store, settings)
        # The status row needs counting queries; refresh it on change or once a minute.
        if changed or errors or time.time() - _summarized.get(store.path, 0) >= 60:
            with store.connect() as c:
                summarize(store, c, accounts)
                if errors:
                    store.source_changed(c, 'claude-accounts', 'error', '훅 기록 파일을 읽지 못했습니다 · 기존 계정 구분은 유지합니다.')
            _summarized[store.path] = time.time()
        return changed
    except Exception as exc:  # never stops collection
        with store.connect() as c:
            store.source_changed(c, 'claude-accounts', 'error', f'계정 구분 실패 ({type(exc).__name__}) · 기존 계정 구분은 유지합니다.')
        return 0
