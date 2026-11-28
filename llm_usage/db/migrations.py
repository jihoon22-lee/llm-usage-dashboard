"""Explicit sequential SQLite schema migrations tracked via PRAGMA user_version."""

import sqlite3

SCHEMA_VERSION = 4


def current_version(c):
    row = c.execute('PRAGMA user_version').fetchone()
    return int(row[0]) if row else 0


def _v1_base_tables(c):
    c.executescript(
        '''
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
        CREATE TABLE IF NOT EXISTS account_tokens (
          route TEXT, day TEXT, tokens INTEGER NOT NULL, checked REAL NOT NULL, PRIMARY KEY(route,day));
        CREATE TABLE IF NOT EXISTS account_token_buckets (
          route TEXT, day TEXT, tokens INTEGER NOT NULL, checked REAL NOT NULL,
          namespace TEXT NOT NULL DEFAULT '', PRIMARY KEY(route,namespace,day));
        INSERT OR IGNORE INTO account_token_buckets
          SELECT route,day,tokens,checked,'' FROM account_tokens;
        '''
    )


def _v2_event_columns(c):
    from .schema import DETAILS, NAMED_EFFORT_ROUTES, model_effort

    added_text = set()
    existing = {r['name'] for r in c.execute('PRAGMA table_info(events)')}
    for column in ('session', 'project', 'agent_kind', 'account', 'parent_id', 'effort', 'speed'):
        if column not in existing:
            try:
                c.execute(f'ALTER TABLE events ADD COLUMN {column} TEXT')
                added_text.add(column)
            except sqlite3.OperationalError as exc:
                if 'duplicate column' not in str(exc):
                    raise
    c.execute(
        'CREATE TABLE IF NOT EXISTS session_effort (session TEXT NOT NULL, ts REAL NOT NULL, effort TEXT NOT NULL, PRIMARY KEY(session,ts))'
    )
    if 'effort' in added_text:
        marks = ','.join('?' for _ in NAMED_EFFORT_ROUTES)
        for (model,) in c.execute(
            f'SELECT DISTINCT model FROM events WHERE route IN ({marks})', NAMED_EFFORT_ROUTES
        ).fetchall():
            if model_effort(model):
                c.execute(
                    f'UPDATE events SET effort=? WHERE model=? AND route IN ({marks})',
                    (model_effort(model), model, *NAMED_EFFORT_ROUTES),
                )
    added_details = set()
    existing = {r['name'] for r in c.execute('PRAGMA table_info(events)')}
    for column in (*DETAILS, 'requests'):
        if column not in existing:
            try:
                c.execute(
                    f'ALTER TABLE events ADD COLUMN {column} INTEGER NOT NULL DEFAULT '
                    + ('1' if column == 'requests' else '0')
                )
                added_details.add(column)
            except sqlite3.OperationalError as exc:
                if 'duplicate column' not in str(exc):
                    raise
    if 'cache_creation_unknown' in added_details:
        c.execute(
            "UPDATE events SET cache_creation_unknown=cache_creation WHERE route='claude-code' AND cache_ttl_observed=0"
        )
    if 'reasoning_observed' in added_details:
        c.execute('UPDATE events SET reasoning_observed=1 WHERE reasoning>0')


def _v3_event_indexes(c):
    c.execute(
        "CREATE INDEX IF NOT EXISTS events_claude_pending ON events(session) WHERE route='claude-code' AND account IS NULL"
    )
    c.execute('CREATE INDEX IF NOT EXISTS events_session ON events(session)')
    c.execute('CREATE INDEX IF NOT EXISTS events_parent ON events(parent_id) WHERE parent_id IS NOT NULL')
    c.execute('CREATE INDEX IF NOT EXISTS events_route_ts ON events(route,ts)')


def _v4_subsystem_tables(c):
    from ..claude_accounts import initialize as initialize_accounts
    from ..claude_attribution import initialize as initialize_attribution
    from ..codex_activity import initialize as initialize_activity
    from ..codex_usage import initialize as initialize_responses
    from ..reconciliation import initialize as initialize_usage
    from ..resources import initialize as initialize_resources

    initialize_resources(c)
    initialize_accounts(c)
    initialize_attribution(c)
    initialize_usage(c)
    initialize_responses(c)
    initialize_activity(c)


MIGRATIONS = (
    (1, _v1_base_tables),
    (2, _v2_event_columns),
    (3, _v3_event_indexes),
    (4, _v4_subsystem_tables),
)


def migrate(c):
    """Apply pending schema migrations sequentially and stamp PRAGMA user_version.

    Legacy databases (user_version == 0) run migrations 1..N idempotently and are
    upgraded in place without losing existing rows.
    """
    version = current_version(c)
    if version >= SCHEMA_VERSION:
        cols = {r['name'] for r in c.execute('PRAGMA table_info(events)')}
        if 'effort' in cols and 'requests' in cols:
            return version
        version = 1
    for target, fn in MIGRATIONS:
        if version < target:
            fn(c)
            c.execute(f'PRAGMA user_version = {target}')
            version = target
    return version
