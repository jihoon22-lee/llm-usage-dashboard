"""Sessions of Aside, a browser whose built-in agents call the models directly.

Aside signs in with the user's ChatGPT (Codex) and Claude subscriptions and keeps its own
session files under `<home>/.aside/u/<profile>/sessions/<session>/messages.jsonl`. Those
requests spend the same subscription quotas but never reach the Codex or Claude Code record
folders, so they are read here and stored under the subscription's own route.

Only numbers leave a line: the model, the token counts, the effort, the time and an identity
for the request. The conversation, tool arguments and results in the same file are not kept.
The format is Aside's own and undocumented (checked against Aside 1.26.1115).
"""

from pathlib import Path
import re

from .claude_usage import counter
from .store import clean_label, identity

# Aside's provider -> the route of the subscription it spends.
ROUTES = {'openai-codex': 'codex', 'claude-code': 'claude-code'}
ORIGIN = 'aside'
# An Aside session works in the browser, not in a folder: its requests are listed under one
# project of this name, so the share used through Aside can be seen and filtered.
PROJECT = 'Aside'
SAFE = re.compile(r'[A-Za-z0-9._:/() -]{1,200}')


def roots(settings):
    """[(source name, sessions folder)] of every Aside profile in the selected homes, and
    of explicit sources of kind 'aside'."""
    found = [(s['name'], Path(s['path'])) for s in settings.get('sources') or [] if s.get('kind') == 'aside']
    counts = {}
    for home in settings.get('homes') or []:
        platform = 'Windows' if str(home).startswith('/mnt/') else 'WSL'
        try:
            profiles = sorted(p for p in (Path(home) / '.aside' / 'u').iterdir() if (p / 'sessions').is_dir())
        except OSError:
            continue
        for profile in profiles:
            counts[platform] = counts.get(platform, 0) + 1
            found.append(
                (f'{platform} Aside' + (f' {counts[platform]}' if counts[platform] > 1 else ''), profile / 'sessions')
            )
    seen = set()
    result = []
    for name, path in found:
        real = str(path.resolve())
        if real not in seen:
            seen.add(real)
            result.append((name, path))
    return result


def sources(settings):
    """The roots as record-file sources for the local collector."""
    return [dict(name=name, route='aside', kind='jsonl', path=str(path)) for name, path in roots(settings)]


def session_id(path):
    """An opaque session identity from the session folder's name."""
    return 'aside-' + identity('aside-session', Path(path).parent.name)[:24]


def ingest_line(store, c, d, state):
    """Store one assistant response of a session file. Returns 1 when it was a request of a
    subscription this dashboard tracks, 0 for every other line."""
    from .collect import model_provider

    if d.get('role') != 'assistant' or not isinstance(d.get('usage'), dict):
        return 0
    route = ROUTES.get(d.get('provider'))
    if route is None:
        return 0  # a model reached another way (an API key, Aside's own): not a tracked subscription
    usage = d['usage']
    stamp = d.get('timestamp')
    if type(stamp) not in (int, float) or not 0 < stamp < 4_102_444_800_000:
        raise ValueError('invalid response time')
    model = d.get('model')
    if not isinstance(model, str) or not SAFE.fullmatch(model):
        raise ValueError('invalid model name')
    values = dict(
        uncached_input=counter(usage.get('input')),
        cached_input=counter(usage.get('cacheRead')),
        output=counter(usage.get('output')),
        cache_creation=counter(usage.get('cacheWrite')),
    )
    if 'reasoning' in usage and usage['reasoning'] is not None:
        values.update(reasoning=counter(usage['reasoning']), reasoning_observed=1)
        if values['reasoning'] > values['output']:
            raise ValueError('reasoning exceeds output')
    # The one-hour share of the cache writes, where the provider states it.
    hour = usage.get('cacheWrite1h')
    if hour is not None and counter(hour) <= values['cache_creation']:
        values.update(
            cache_creation_1h=counter(hour),
            cache_creation_5m=values['cache_creation'] - counter(hour),
            cache_ttl_observed=1,
        )
    else:
        values['cache_creation_unknown'] = values['cache_creation']
    # A response is replayed in the file after a compaction or a branch: its ID counts it once.
    # A line without one (a response cut short) is its session's line at that time.
    rid = d.get('responseId')
    if rid is not None and (not isinstance(rid, str) or not SAFE.fullmatch(rid)):
        raise ValueError('invalid response identity')
    session = state['session']
    # A Claude message keeps the identity a Claude Code transcript gives it, so a copy read
    # there merges. Codex responses stay apart from the records rebuilt from Codex's own files.
    if rid and route == 'claude-code':
        key = identity(route, rid)
    else:
        key = identity(ORIGIN, route, rid or f'{session}:{stamp}')
    store.event(
        c,
        key,
        stamp / 1000,
        model_provider(model),
        route,
        model,
        values,
        session=session,
        project=PROJECT,
        agent_kind='main',
        effort=clean_label(d.get('thinkingLevel')),
    )
    if c.execute('SELECT 1 FROM events WHERE id=?', (key,)).fetchone():
        c.execute('INSERT OR REPLACE INTO usage_origins VALUES (?,?,?)', (key, ORIGIN, 'final'))
    return 1
