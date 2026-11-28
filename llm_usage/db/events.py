"""Event ingestion, Codex counter rebuilds, and hourly rollup maintenance."""

import time

from .schema import DETAILS, METRICS, NAMED_EFFORT_ROUTES, TOKENS, clean_label, identity, model_effort


class EventsMixin:
    def _ensure_rollup(self):
        """Hourly sums of events, kept by triggers so every write path (upserts, codex
        rebuilds, Antigravity merges, attribution backfills) updates it in the same
        transaction. Aggregations over whole hours read it instead of every event.

        ts is the hour start (KST is a whole-hour offset, so hours, days, weeks and
        months group exactly as on events); advisor subcalls carry no API request."""
        key = ",".join(
            [
                'ts',
                'provider',
                'route',
                'model',
                "IFNULL(project,char(0))",
                "IFNULL(agent_kind,char(0))",
                "IFNULL(account,char(0))",
                "IFNULL(effort,char(0))",
            ]
        )
        match = ' AND '.join(
            [
                'ts=CAST(OLD.ts/3600 AS INTEGER)*3600',
                'provider=OLD.provider',
                'route=OLD.route',
                'model=OLD.model',
                'project IS OLD.project',
                'agent_kind IS OLD.agent_kind',
                'account IS OLD.account',
                'effort IS OLD.effort',
            ]
        )
        add = f"""INSERT INTO usage_hourly VALUES (CAST(NEW.ts/3600 AS INTEGER)*3600,NEW.provider,NEW.route,NEW.model,NEW.project,NEW.agent_kind,
              {','.join('NEW.' + k for k in METRICS)},NEW.requests,NEW.account,NEW.effort) ON CONFLICT({key}) DO UPDATE SET
              {','.join(f'{k}={k}+excluded.{k}' for k in METRICS)},requests=requests+excluded.requests;"""
        remove = f"""UPDATE usage_hourly SET {','.join(f'{k}={k}-OLD.{k}' for k in METRICS)},requests=requests-OLD.requests WHERE {match};
              DELETE FROM usage_hourly WHERE {match} AND requests+advisor_calls<=0;"""
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')  # the web and the collector may start together
            # Version 4 adds the reasoning effort to the key (3 added detail coverage and
            # advisor subcall counts). An older release finds a different
            # version and rebuilds its own table; this one does the same in return.
            if self.state(c, 'rollup_version') == 4:
                return
            for statement in [
                'DROP TABLE IF EXISTS usage_hourly',
                f"""CREATE TABLE usage_hourly (ts INTEGER NOT NULL, provider TEXT NOT NULL, route TEXT NOT NULL, model TEXT NOT NULL,
                    project TEXT, agent_kind TEXT, {','.join(k + ' INTEGER NOT NULL' for k in METRICS)}, requests INTEGER NOT NULL, account TEXT, effort TEXT)""",
                f'CREATE UNIQUE INDEX usage_hourly_key ON usage_hourly({key})',
                'CREATE INDEX usage_hourly_route_ts ON usage_hourly(route,ts)',
                'DROP TRIGGER IF EXISTS events_rollup_insert',
                'DROP TRIGGER IF EXISTS events_rollup_delete',
                'DROP TRIGGER IF EXISTS events_rollup_update',
                f'CREATE TRIGGER events_rollup_insert AFTER INSERT ON events BEGIN {add} END',
                f'CREATE TRIGGER events_rollup_delete AFTER DELETE ON events BEGIN {remove} END',
                f'CREATE TRIGGER events_rollup_update AFTER UPDATE ON events BEGIN {remove} {add} END',
                self.ROLLUP_INSERT,
            ]:
                c.execute(statement)
            self.save_state(c, 'rollup_version', 4)

    ROLLUP_SELECT = (
        'SELECT CAST(ts/3600 AS INTEGER)*3600,provider,route,model,project,agent_kind,'
        + ','.join(f'SUM({k})' for k in METRICS)
        + ',SUM(requests),account,effort FROM events GROUP BY 1,2,3,4,5,6,account,effort'
    )
    ROLLUP_INSERT = 'INSERT INTO usage_hourly ' + ROLLUP_SELECT

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
            fresh = set(map(tuple, c.execute(self.ROLLUP_SELECT)))
            kept = set(map(tuple, c.execute('SELECT * FROM usage_hourly')))
        return fresh == kept

    def event(
        self,
        c,
        key,
        ts,
        provider,
        route,
        model,
        values,
        session=None,
        project=None,
        agent_kind=None,
        account=None,
        parent_id=None,
        requests=1,
        allow_empty=False,
        effort=None,
        speed=None,
        replace_attribution=False,
    ):
        # Streaming snapshots are monotonic maxima per globally stable request ID.
        numbers = [max(0, int(values.get(k) or 0)) for k in TOKENS]
        if not any(numbers) and not allow_empty:
            return
        effort = clean_label(effort) or (model_effort(model) if route in NAMED_EFFORT_ROUTES else None)
        if effort is None and session and route.startswith('codex'):
            turn = c.execute(
                'SELECT effort FROM session_effort WHERE session=? AND ts<=? ORDER BY ts DESC LIMIT 1', (session, ts)
            ).fetchone()
            effort = turn[0] if turn else None
        details = [max(0, int(values.get(k) or 0)) for k in DETAILS]
        # TTL splits are one coherent observation, never independent maxima.
        # Conflicting splits of the same total stay unknown until a newer total.
        conflict = '(excluded.cache_creation=events.cache_creation AND (events.cache_ttl_conflict OR '
        conflict += '(events.cache_ttl_observed AND excluded.cache_ttl_observed AND '
        conflict += '(events.cache_creation_5m<>excluded.cache_creation_5m OR events.cache_creation_1h<>excluded.cache_creation_1h))))'
        newer = '(excluded.cache_creation>events.cache_creation OR (excluded.cache_creation=events.cache_creation AND NOT events.cache_ttl_observed))'
        detail_updates = ','.join(
            f'{k}=CASE WHEN {conflict} THEN 0 WHEN {newer} THEN excluded.{k} ELSE events.{k} END'
            for k in ('cache_creation_5m', 'cache_creation_1h', 'cache_ttl_observed')
        )
        detail_updates += f',cache_creation_unknown=CASE WHEN {conflict} THEN events.cache_creation WHEN {newer} THEN excluded.cache_creation_unknown ELSE events.cache_creation_unknown END'
        detail_updates += f',cache_ttl_conflict=CASE WHEN {conflict} THEN 1 WHEN excluded.cache_creation>events.cache_creation THEN 0 ELSE events.cache_ttl_conflict END'
        detail_updates += ',reasoning_observed=CASE WHEN excluded.output>events.output THEN excluded.reasoning_observed WHEN excluded.output=events.output THEN MAX(events.reasoning_observed,excluded.reasoning_observed) ELSE events.reasoning_observed END'
        detail_updates += ',advisor_calls=MAX(events.advisor_calls,excluded.advisor_calls),requests=excluded.requests'
        # A copy read without an account (local replay, telemetry) keeps a resolved one.
        attribution = ''.join(
            f',{field}=excluded.{field}'
            if replace_attribution
            else f',{field}=COALESCE(excluded.{field},events.{field})'
            for field in ('session', 'project', 'agent_kind', 'effort')
        )
        if replace_attribution:
            attribution += ',ts=excluded.ts'
        columns = (
            'id',
            'ts',
            'provider',
            'route',
            'model',
            *METRICS,
            'session',
            'project',
            'agent_kind',
            'account',
            'parent_id',
            'requests',
            'effort',
            'speed',
        )
        c.execute(
            'INSERT INTO events ('
            + ','.join(columns)
            + ') VALUES ('
            + ','.join('?' for _ in columns)
            + ') ON CONFLICT(id) DO UPDATE SET '
            + ','.join(f'{k}=MAX(events.{k},excluded.{k})' for k in TOKENS)
            + ','
            + detail_updates
            + ',provider=excluded.provider,model=excluded.model,route=excluded.route'
            + attribution
            + ',account=COALESCE(excluded.account,events.account),parent_id=COALESCE(excluded.parent_id,events.parent_id)'
            + ',speed=COALESCE(excluded.speed,events.speed)',
            (
                key,
                ts,
                provider,
                route,
                model or 'unknown',
                *numbers,
                *details,
                session,
                project,
                agent_kind,
                account,
                parent_id,
                requests,
                effort,
                clean_label(speed),
            ),
        )

    def turn_effort(self, c, session, ts, effort):
        """Record the effort a Codex turn was set to. True when it is new."""
        effort = clean_label(effort)
        if not effort or not session or not ts:
            return False
        return bool(c.execute('INSERT OR IGNORE INTO session_effort VALUES (?,?,?)', (session, ts, effort)).rowcount)

    def apply_turn_efforts(self, c, session):
        """Give the session's stored requests the effort of the turn they ran in."""
        latest = '(SELECT effort FROM session_effort s WHERE s.session=events.session AND s.ts<=events.ts ORDER BY s.ts DESC LIMIT 1)'
        changed = c.execute(
            f"UPDATE events SET effort={latest} WHERE session=? AND route LIKE 'codex%' AND effort IS NOT {latest}",
            (session,),
        ).rowcount
        if changed:
            self.bump_data_version(c)

    def codex_point(self, c, session, ts, provider, route, model, usage, last):
        keys = (
            'input_tokens',
            'cached_input_tokens',
            'output_tokens',
            'cache_write_input_tokens',
            'reasoning_output_tokens',
        )
        totals = [max(0, int(usage.get(k) or 0)) for k in keys]
        previous = [max(0, int(last.get(k) or 0)) for k in keys]
        # Timestamp and original cumulative counters survive Windows/WSL copies and replay.
        key = identity('codex-point', session, ts, totals)
        observed = int(
            usage.get('reasoning_output_tokens') is not None and last.get('reasoning_output_tokens') is not None
        )
        inserted = c.execute(
            'INSERT INTO codex_points VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) '
            "ON CONFLICT(id) DO UPDATE SET model=CASE WHEN codex_points.model='unknown' THEN excluded.model ELSE codex_points.model END,"
            "provider=CASE WHEN codex_points.model='unknown' THEN excluded.provider ELSE codex_points.provider END,"
            'reasoning_observed=MAX(codex_points.reasoning_observed,excluded.reasoning_observed) '
            "WHERE (codex_points.model='unknown' AND excluded.model!='unknown') OR excluded.reasoning_observed>codex_points.reasoning_observed",
            (key, identity(session), ts, provider, route, model or 'unknown', *totals, *previous, observed),
        ).rowcount
        return identity(session) if inserted else None

    def discard_codex_point(self, c, session, ts, usage):
        # Remove only this proven inherited observation, preserving other copies/history.
        keys = (
            'input_tokens',
            'cached_input_tokens',
            'output_tokens',
            'cache_write_input_tokens',
            'reasoning_output_tokens',
        )
        totals = [max(0, int(usage.get(k) or 0)) for k in keys]
        key = identity('codex-point', session, ts, totals)
        c.execute('INSERT OR IGNORE INTO inherited_points VALUES (?)', (key,))
        c.execute('DELETE FROM events WHERE id=?', (key,))
        removed = c.execute('DELETE FROM codex_points WHERE id=?', (key,)).rowcount
        return identity(session) if removed else None

    def rebuild_codex(self, c, session, attrs=None):
        # Rebuilding token deltas must not silently migrate historical project labels.
        projects = dict(
            c.execute(
                'SELECT id,project FROM events WHERE id IN (SELECT id FROM codex_points WHERE session=?)', (session,)
            )
        )
        c.execute('DELETE FROM events WHERE id IN (SELECT id FROM codex_points WHERE session=?)', (session,))
        previous = None
        previous_ts = None
        seen = {}
        resets = 0
        partial = False
        zero_baseline = False
        zero_baselines = 0
        ambiguous = 0
        groups = {}
        last_group = None
        for row in c.execute(
            'SELECT * FROM codex_points WHERE session=? ORDER BY ts,input,output,id', (session,)
        ).fetchall():
            current = [row[k] for k in ('input', 'cached', 'output', 'creation', 'reasoning')]
            last = [row[k] for k in ('last_input', 'last_cached', 'last_output', 'last_creation', 'last_reasoning')]
            # Repeated notifications are duplicates only within this counter epoch.
            # A reset may legitimately reach counters seen earlier in the session.
            if current == previous:
                groups[row['id']] = last_group
                continue
            reset = previous is not None and (current[0] < previous[0] or current[2] < previous[2])
            # Two different counters at the same timestamp have no temporal order.
            # A later repeat of that lower observation is not proof of a new epoch.
            if reset and seen.get(tuple(current)) == previous_ts:
                groups[row['id']] = None
                continue
            if previous is None:
                partial = current != last
            if reset:
                resets += 1
            if previous_ts == row['ts']:
                ambiguous += 1
            # A partial copy starts from the known last request, then converges when full history arrives.
            if previous is None or reset:
                delta = last
            else:
                delta = [max(0, a - b) for a, b in zip(current, previous)]
            if not any(last):
                # A zero-request snapshot can restore a different cumulative baseline.
                # It is not evidence of consumption, even when its total jumps upward.
                delta = last
                zero_baseline = True
                zero_baselines += 1
            else:
                if zero_baseline and delta != last:
                    delta = last
                    ambiguous += 1
                zero_baseline = False
            previous = current
            previous_ts = row['ts']
            seen[tuple(current)] = row['ts']
            last_group = row['id'] if any(delta) else None
            groups[row['id']] = last_group
            if not any(delta):
                continue
            inp, cached, out, creation, reasoning = delta
            self.event(
                c,
                row['id'],
                row['ts'],
                row['provider'],
                row['route'],
                row['model'],
                dict(
                    uncached_input=max(0, inp - cached - creation),
                    cached_input=cached,
                    output=out,
                    cache_creation=creation,
                    reasoning=reasoning,
                    reasoning_observed=row['reasoning_observed'],
                ),
                **({**(attrs or {}), 'project': projects[row['id']]} if row['id'] in projects else (attrs or {})),
            )
        from ..codex_usage import project

        project(self, c, session, groups, projects)
        c.execute(
            'INSERT OR REPLACE INTO session_quality VALUES (?,?,?,?,?)',
            (session, resets, int(partial), ambiguous, zero_baselines),
        )
        self.bump_data_version(c)

    def import_error(self, c, source, record, kind):
        c.execute(
            '''INSERT INTO import_errors VALUES (?,?,?,1,?) ON CONFLICT(source,record)
          DO UPDATE SET kind=excluded.kind,attempts=attempts+1,checked=excluded.checked''',
            (source, str(record), kind, time.time()),
        )

    def error_count(self, c, source):
        return c.execute('SELECT COUNT(*) FROM import_errors WHERE source=?', (source,)).fetchone()[0]
