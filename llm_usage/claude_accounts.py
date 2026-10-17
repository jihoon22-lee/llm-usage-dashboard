"""Claude Code accounts by config home (CLAUDE_CONFIG_DIR). Opaque keys only.

One Claude Code service, several subscription accounts. Each account has an
internal route: 'claude-code' stays bound to the account the database already
knew; every other account is 'claude-code:<12 hex of its key>'. Limits, resources
and sources are stored per account route; usage events keep route 'claude-code'
and carry the account route in events.account.

Only accountUuid/organizationUuid (hashed), subscriptionType, rateLimitTier and
the token expiry time are read. Emails, names and tokens are never stored.
"""
import json
import math
import os
from pathlib import Path
import re
import time

FAMILY = 'claude-code'
UNKNOWN = 'claude-code:unknown'
# Folder name patterns only; anything else in the home list is ignored.
SAFE_NAME = re.compile(r'^[A-Za-z0-9._-]{1,80}$')


def is_claude(route):
    return route == FAMILY or (isinstance(route, str) and route.startswith(FAMILY + ':'))


def family(route):
    return FAMILY if is_claude(route) else route


def suffix(route):
    """'' for the original account, ':<hash>' for another account route."""
    return route[len(FAMILY):] if is_claude(route) else ''


def account_source(route):
    """The account-poll source that is canonical for a quota route."""
    return 'claude-oauth' + suffix(route) if is_claude(route) else route


def account_key(identity):
    """Same key resources.observe_account stores for route 'claude-code'."""
    from .resources import key
    return key(FAMILY, identity)


def route_for(key, primary):
    return FAMILY if key == primary else FAMILY + ':' + key[:12]


def initialize(c):
    c.execute('''CREATE TABLE IF NOT EXISTS claude_accounts (
        route TEXT PRIMARY KEY, account_key TEXT, first_seen REAL NOT NULL,
        last_seen REAL NOT NULL, data TEXT NOT NULL)''')


def plan_label(plan, tier):
    tier = tier if isinstance(tier, str) else ''
    multiple = re.search(r'max_(\d{1,3})x', tier)
    if plan == 'max' or multiple:
        return 'Max ' + multiple.group(1) + 'x' if multiple else 'Max'
    names = {'pro': 'Pro', 'free': 'Free', 'team': 'Team', 'enterprise': 'Enterprise'}
    if plan in names:
        return names[plan]
    return plan.title() if isinstance(plan, str) and re.fullmatch('[a-z_]{1,20}', plan) else None


def default_home(settings):
    credentials = Path(settings.get('claude_auth') or Path.home()/'.claude/.credentials.json')
    if settings.get('claude_account_file'):
        account_file = Path(settings['claude_account_file'])
    elif credentials.parent.name == '.claude':
        account_file = credentials.parent.parent/'.claude.json'
    else:
        account_file = None
    return dict(path=str(credentials.parent), credentials=credentials, account_file=account_file, kind='default',
                name='~/.claude' if credentials.parent.name == '.claude' else credentials.parent.name)


def home_candidates(settings, observed=()):
    """Default home first, then configured, zerosub-managed, hook-observed and Windows homes."""
    found = [default_home(settings)]
    folders = [(Path(p), 'configured') for p in settings.get('claude_homes') or [] if isinstance(p, str)]
    if settings.get('claude_discover_homes', True):
        base = Path(settings.get('paseo_home') or Path.home()/'.paseo')/'zerosub'/'homes'
        try:
            folders += [(p, 'zerosub') for p in sorted(base.glob('claude-*'))]
        except OSError:
            pass
    # Homes reported by the hook are accepted only inside this user's home.
    folders += [(Path(p), 'observed') for p in observed if isinstance(p, str) and Path(p).is_relative_to(Path.home())]
    for folder, kind in folders:
        if folder.is_absolute() and SAFE_NAME.match(folder.name):
            found.append(dict(path=str(folder), credentials=folder/'.credentials.json',
                              account_file=folder/'.claude.json', kind=kind, name=folder.name))
    # A Windows home's own login identifies its transcripts; its token is never read.
    for value in settings.get('homes') or []:
        home = Path(value) if isinstance(value, str) else None
        if home and re.match(r'^/mnt/[A-Za-z]/', str(home)):
            found.append(dict(path=str(home/'.claude'), credentials=None, account_file=home/'.claude.json',
                              kind='windows', name='Windows'))
    unique = {}
    for home in found:
        try:
            real = os.path.realpath(home['path'])
        except (OSError, ValueError):
            continue
        unique.setdefault(real, home)
    return list(unique.values())


_identities = {}


def identity_of(account_file):
    """(accountUuid, organizationUuid) of the native sign-in, or None. The large
    account file is parsed again only when it changes."""
    if not account_file:
        return None
    try:
        stat = os.stat(account_file)
        signature = (str(account_file), stat.st_mtime_ns, stat.st_size)
        if signature in _identities:
            return _identities[signature]
        data = json.loads(Path(account_file).read_text()).get('oauthAccount') or {}
        user, org = data.get('accountUuid'), data.get('organizationUuid')
    except (OSError, ValueError, TypeError, AttributeError):
        return None
    found = (user, org) if all(isinstance(v, str) and re.fullmatch('[A-Za-z0-9-]{1,100}', v) for v in (user, org)) else None
    if len(_identities) > 64:
        _identities.clear()
    _identities[signature] = found
    return found


def inspect(home):
    """Identity key, plan and token expiry of one home. The token itself is dropped."""
    info = dict(home, key=None, plan=None, tier=None, expires=None, has_token=False)
    identity = identity_of(home.get('account_file'))
    if identity:
        info['key'] = account_key(identity)
    if home.get('credentials'):
        try:
            auth = json.loads(Path(home['credentials']).read_text()).get('claudeAiOauth') or {}
        except (OSError, ValueError, TypeError, AttributeError):
            auth = {}
        if isinstance(auth, dict):
            info['plan'] = auth.get('subscriptionType') if isinstance(auth.get('subscriptionType'), str) else None
            info['tier'] = auth.get('rateLimitTier') if isinstance(auth.get('rateLimitTier'), str) else None
            expires = auth.get('expiresAt')
            if isinstance(expires, (int, float)) and not isinstance(expires, bool) and math.isfinite(expires):
                info['expires'] = expires/1000
            info['has_token'] = bool(auth.get('accessToken'))
    return info


def bind_primary(store, c, homes):
    """The account that keeps route 'claude-code': the one the database already
    identified, else the default home's current login."""
    primary = store.state(c, 'claude_primary_account')
    if primary:
        return primary
    from .resources import account
    owner = account(c, FAMILY)
    primary = owner['account_key'] if owner and owner['account_key'] else next(
        (h['key'] for h in homes if h['kind'] == 'default' and h['key']), None)
    if primary:
        store.save_state(c, 'claude_primary_account', primary)
    return primary


def observed_homes(store, c):
    value = store.state(c, 'claude_homes_observed', [])
    return [p for p in value if isinstance(p, str)] if isinstance(value, list) else []


def accounts(store, settings, now=None):
    """Poll contexts per account, freshest valid token first; updates the registry.

    The original account route is always present so an install without any
    readable login still reports why its quota is missing."""
    now = now or time.time()
    with store.connect() as c:
        observed = observed_homes(store, c)
    homes = [inspect(h) for h in home_candidates(settings, observed)]
    contexts = []
    with store.connect() as c:
        primary = bind_primary(store, c, homes)
        groups = {}
        for home in homes:
            if home['key']:
                groups.setdefault(home['key'], []).append(home)
        default = next(h for h in homes if h['kind'] == 'default')
        for key, members in groups.items():
            route = route_for(key, primary)
            pollable = [h for h in members if h['kind'] != 'windows' and h.get('credentials') and h['has_token']]
            best = max(pollable, key=lambda h: ((h['expires'] or 0) > now, h['kind'] == 'default', h['expires'] or 0), default=None)
            plan = next(((h['plan'], h['tier']) for h in ([best] if best else []) + members if h['plan'] or h['tier']), (None, None))
            contexts.append(dict(route=route, key=key, home=best, homes=members, plan=plan[0], tier=plan[1]))
            previous = c.execute('SELECT first_seen FROM claude_accounts WHERE route=?', (route,)).fetchone()
            data = dict(primary=route == FAMILY, plan=plan[0], tier=plan[1], plan_label=plan_label(*plan),
                        homes=[dict(kind=h['kind'], name=h['name']) for h in members],
                        polled=dict(kind=best['kind'], name=best['name']) if best else None,
                        token_valid=bool(best and best['expires'] and best['expires'] > now))
            c.execute('INSERT OR REPLACE INTO claude_accounts VALUES (?,?,?,?,?)',
                      (route, key, previous[0] if previous else now, now, json.dumps(data, ensure_ascii=False)))
        if not default['key'] and not any(ctx['route'] == FAMILY for ctx in contexts):
            # Legacy behaviour for a default home without a readable identity. A default
            # home signed in to another account is that account's route only.
            contexts.insert(0, dict(route=FAMILY, key=None, home=default, homes=[default], plan=default['plan'], tier=default['tier']))
    contexts.sort(key=lambda ctx: (ctx['route'] != FAMILY, ctx['route']))
    return contexts


def registry(c):
    rows = []
    for row in c.execute('SELECT * FROM claude_accounts ORDER BY route'):
        try:
            data = json.loads(row['data'])
        except ValueError:
            data = {}
        rows.append(dict(route=row['route'], account_key=row['account_key'], first_seen=row['first_seen'],
                         last_seen=row['last_seen'], **data))
    rows.sort(key=lambda r: (r['route'] != FAMILY, r['first_seen'], r['route']))
    return rows


def labels(rows, names=None):
    """User names first, else the plan; duplicates get a short distinguishing suffix."""
    names = names if isinstance(names, dict) else {}
    result = {}
    for row in rows:
        name = names.get(row['route'])
        name = name.strip() if isinstance(name, str) and name.strip() else None
        result[row['route']] = name or row.get('plan_label') or '계정'
    counts = {}
    for value in result.values():
        counts[value] = counts.get(value, 0) + 1
    for route, value in list(result.items()):
        if counts[value] > 1:
            result[route] = value + (' · 기본' if route == FAMILY else ' · ' + route[-4:])
    return result


def describe(c, names=None, ended=()):
    """Registry rows with display labels for the API (no keys)."""
    names = names if isinstance(names, dict) else {}
    rows = registry(c)
    named = labels(rows, names)
    out = []
    for row in rows:
        custom = names.get(row['route'])
        out.append(dict(route=row['route'], label=named[row['route']], plan_label=row.get('plan_label'),
                        primary=row['route'] == FAMILY, homes=row.get('homes') or [], polled=row.get('polled'),
                        token_valid=row.get('token_valid'), first_seen=row['first_seen'], last_seen=row['last_seen'],
                        ended=row['route'] in set(ended or ()), custom_name=custom.strip() if isinstance(custom, str) and custom.strip() else None))
    return out
