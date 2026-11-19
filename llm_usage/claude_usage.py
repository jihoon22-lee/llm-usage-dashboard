"""One numeric usage interpretation for local and cloud Claude transcripts."""

import json

from .store import identity, project_label, stamp


def counter(value):
    if value is None:
        return 0
    if type(value) is not int or not 0 <= value <= 2**63 - 1:
        raise ValueError('invalid usage counter')
    return value


def components(usage):
    if not isinstance(usage, dict):
        raise ValueError('invalid usage object')
    values = {
        name: counter(usage.get(field))
        for name, field in (
            ('uncached_input', 'input_tokens'),
            ('cached_input', 'cache_read_input_tokens'),
            ('output', 'output_tokens'),
            ('cache_creation', 'cache_creation_input_tokens'),
        )
    }
    output = usage.get('output_tokens_details') or {}
    if not isinstance(output, dict):
        raise ValueError('invalid output details')
    reasoning = output.get('thinking_tokens', usage.get('reasoning_output_tokens'))
    try:
        values.update(reasoning=counter(reasoning), reasoning_observed=int(reasoning is not None))
        if values['reasoning'] > values['output']:
            raise ValueError('thinking exceeds output')
    except ValueError:
        values.update(reasoning=0, reasoning_observed=0, _reasoning_error=True)
    ttl = usage.get('cache_creation') or {}
    if not isinstance(ttl, dict):
        raise ValueError('invalid cache details')
    five, hour = (ttl.get(k) for k in ('ephemeral_5m_input_tokens', 'ephemeral_1h_input_tokens'))
    if five is not None and hour is not None:
        try:
            five, hour = counter(five), counter(hour)
            if five + hour != values['cache_creation']:
                five, hour, complete = call_split(usage, values['cache_creation'])
                if not complete:
                    values['_invalid_cache_split'] = (five, hour)
                    raise ValueError('invalid call cache details')
            values.update(cache_creation_5m=five, cache_creation_1h=hour, cache_ttl_observed=1)
        except ValueError:
            values.update(cache_creation_unknown=values['cache_creation'], _cache_error=True)
    else:
        values['cache_creation_unknown'] = values['cache_creation']
    return values


def call_split(usage, total):
    """The cache split of a response that made several model calls (an advisor consulted
    between them): the top-level split then holds only the first call's, while the total
    adds every call. The calls' own splits, added up, must give that total."""
    calls = [p for p in usage.get('iterations') or [] if isinstance(p, dict) and p.get('type') == 'message']
    five = hour = 0
    complete = True
    for part in calls:
        ttl = part.get('cache_creation')
        if not isinstance(ttl, dict):
            raise ValueError('invalid cache details')
        raw_five, raw_hour = ttl.get('ephemeral_5m_input_tokens'), ttl.get('ephemeral_1h_input_tokens')
        call_five, call_hour = counter(raw_five), counter(raw_hour)
        raw_total = part.get('cache_creation_input_tokens')
        complete &= (
            raw_five is not None
            and raw_hour is not None
            and (raw_total is None or type(raw_total) is int and raw_total == call_five + call_hour)
        )
        five += call_five
        hour += call_hour
    if not calls or five + hour != total:
        raise ValueError('cache detail total differs')
    # Even an incomplete split identifies the exact pair that the old parser accepted.
    return five, hour, complete


def prepare_replay(store):
    """Keep the pre-upgrade split apart from independently checked snapshots during replay."""
    with store.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        if store.state(c, 'claude-usage-replay-v2'):
            return
        c.execute(
            '''INSERT INTO claude_cache_replay (id, five, hour)
               SELECT id, cache_creation_5m, cache_creation_1h FROM events
               WHERE route='claude-code' AND cache_creation>0 AND cache_ttl_observed=1'''
        )
        # Old Aside account evidence could be written to a native Claude session.
        c.execute(
            """INSERT INTO claude_dirty SELECT session, MIN(ts) FROM claude_evidence
               WHERE kind='aside' AND session NOT LIKE 'aside-%' GROUP BY session
               ON CONFLICT(session) DO UPDATE SET since=MIN(since,excluded.since)"""
        )
        c.execute("DELETE FROM claude_evidence WHERE kind='aside' AND session NOT LIKE 'aside-%'")
        store.save_state(c, 'claude-usage-replay-v2', True)


def repair_cache_split(c, key, values):
    """Withdraw an old split only when a reread disproves that exact observation."""
    if not (values.get('cache_ttl_observed') or values.get('_invalid_cache_split')):
        return
    row = c.execute(
        '''SELECT r.five, r.hour, r.verified, e.cache_creation
           FROM claude_cache_replay r JOIN events e ON e.id=r.id WHERE r.id=?''',
        (key,),
    ).fetchone()
    if row is None:
        return
    old = (row['five'], row['hour'])
    total = row['cache_creation']
    if total != sum(old):
        c.execute('DELETE FROM claude_cache_replay WHERE id=?', (key,))
        return
    if values['cache_creation'] != total:
        return

    verified = {tuple(pair) for pair in json.loads(row['verified'])}
    if values.get('cache_ttl_observed'):
        verified.add((values.get('cache_creation_5m', 0), values.get('cache_creation_1h', 0)))
    disproved = values.get('_invalid_cache_split') == old
    if disproved or old in verified or len(verified) > 1:
        c.execute('DELETE FROM claude_cache_replay WHERE id=?', (key,))
    else:
        c.execute('UPDATE claude_cache_replay SET verified=? WHERE id=?', (json.dumps(sorted(verified)), key))

    pairs = verified if disproved else verified | {old}
    observed = len(pairs) == 1
    five, hour = next(iter(pairs)) if observed else (0, 0)
    details = (five, hour, total - five - hour, int(observed), int(len(pairs) > 1))
    c.execute(
        '''UPDATE events SET cache_creation_5m=?, cache_creation_1h=?, cache_creation_unknown=?,
                  cache_ttl_observed=?, cache_ttl_conflict=?
           WHERE id=? AND (cache_creation_5m,cache_creation_1h,cache_creation_unknown,cache_ttl_observed,cache_ttl_conflict)
               IS NOT (?,?,?,?,?)''',
        (*details, key, *details),
    )


def detail_status(store, c, key, values):
    for field, observed in (('cache', 'cache_ttl_observed'), ('reasoning', 'reasoning_observed')):
        record = key + ':' + field
        if values.get('_' + field + '_error'):
            store.import_error(c, 'claude-details', record, field + 'ComponentMismatch')
        elif values.get(observed):
            c.execute("DELETE FROM import_errors WHERE source='claude-details' AND record=?", (record,))


def summary(store, c):
    bad = c.execute("SELECT COUNT(*) FROM import_errors WHERE source='claude-details'").fetchone()[0]
    conflict = c.execute("SELECT COALESCE(SUM(cache_ttl_conflict),0) FROM events WHERE route='claude-code'").fetchone()[
        0
    ]
    if bad or conflict:
        store.source_changed(
            c,
            'claude-details',
            'partial',
            f'원본 세부값 불일치 {bad}건 · 캐시 관측 상충 {conflict}건 · 전체 사용량은 보존하고 해당 세부값은 미확인으로 표시합니다.',
        )
    elif c.execute("SELECT 1 FROM sources WHERE name='claude-details'").fetchone():
        store.source_changed(c, 'claude-details', 'ok', '현재 확인된 세부값 불일치가 없습니다.')


def ingest_message(
    store, c, route, data, *, session=None, project=None, account=None, source='local', finality='final'
):
    from .collect import model_provider
    from .reconciliation import aliases, link

    message = data.get('message') or {}
    usage = message.get('usage')
    ts = stamp(data.get('timestamp'))
    if not usage or not message.get('id') or not ts:
        return 0
    key = identity(route, message['id'])
    sid = session or data.get('sessionId') or data.get('session_id')
    project = project if project is not None else project_label(data.get('cwd'))
    kind = 'sub' if data.get('isSidechain') or data.get('parent_tool_use_id') else 'main'
    iterations = usage.get('iterations') or []
    if not isinstance(iterations, list):
        raise ValueError('invalid usage iterations')
    advisors = [
        (i, part)
        for i, part in enumerate(iterations)
        if isinstance(part, dict) and part.get('type') == 'advisor_message'
    ]
    values = components(usage)
    detail_status(store, c, key, values)
    aside_session = c.execute(
        "SELECT session,ts FROM events WHERE id=? AND session LIKE 'aside-%'",
        (key,),
    ).fetchone()
    if aside_session and sid:
        from .claude_attribution import reassign_aside_session

        reassign_aside_session(c, key, aside_session['session'], sid, min(ts, aside_session['ts']))
    # Claude Code writes the request's effort and speed beside the response.
    store.event(
        c,
        key,
        ts,
        model_provider(message.get('model', '')),
        route,
        message.get('model'),
        values,
        session=sid,
        project=project,
        agent_kind=kind,
        account=account,
        allow_empty=bool(advisors or aside_session),
        effort=data.get('effort'),
        speed=usage.get('speed'),
        replace_attribution=bool(aside_session),
    )
    repair_cache_split(c, key, values)
    aliases(c, route, key, message['id'], data.get('requestId'), data.get('request_id'), data.get('uuid'))
    if route == 'claude-code' and sid:
        link(c, 'claude:' + sid, key, source, finality)
    # A historical parent may already have an account even when this replay does not.
    parent = c.execute('SELECT account FROM events WHERE id=?', (key,)).fetchone()
    for index, part in advisors:
        child = identity('claude-advisor', route, message['id'], index)
        values = components(part)
        values['advisor_calls'] = 1
        detail_status(store, c, child, values)
        store.event(
            c,
            child,
            ts,
            model_provider(part.get('model', '')),
            route,
            part.get('model'),
            values,
            session=sid,
            project=project,
            agent_kind=kind,
            account=parent['account'] if parent else account,
            parent_id=key,
            requests=0,
            allow_empty=True,
        )
        repair_cache_split(c, child, values)
        if route == 'claude-code' and sid:
            link(c, 'claude:' + sid, child, source, finality)
    return 1
