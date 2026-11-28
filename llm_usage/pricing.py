"""Optional public per-model pricing for cost estimation.

Only models with an exact name or dated-snapshot prefix match are priced;
unpriced models report no estimate instead of guessing. Users extend or
override rates in ~/.config/llm-usage/pricing.json or the 'pricing' config map.
"""

import importlib.resources
import json
from pathlib import Path
import re
import threading

from .util import is_finite_number

# USD per 1M tokens. cache_write=None falls back to the input rate.
# Provider-internal or unidentified models with no public token price: costs stay
# unestimated by design, so they are not reported as missing a rate.
# big-pickle is OpenCode's free stealth model; it has no paid listing to convert at.
NO_PUBLIC_PRICE = frozenset({'big-pickle', 'compactor', 'codex-auto-review', 'gpt-reserve', 'unknown'})


def _load_builtin():
    """Load the builtin public pricing table from llm_usage/data/builtin_pricing.json."""
    try:
        text = importlib.resources.files('llm_usage').joinpath('data/builtin_pricing.json').read_text(encoding='utf-8')
    except (FileNotFoundError, ModuleNotFoundError, TypeError):
        text = (Path(__file__).resolve().parent / 'data' / 'builtin_pricing.json').read_text(encoding='utf-8')
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError('builtin_pricing.json must be a JSON object')
    return data


BUILTIN = _load_builtin()


def _layer_paths(settings):
    paths = [Path(settings.get('pricing_file') or Path.home() / '.config/llm-usage/pricing.json')]
    if settings.get('database'):
        paths.append(Path(settings['database']).parent / 'pricing.json')
    return paths


def _mtime(path):
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return None


# load_pricing() runs on every request; reuse the merged table until a layer changes.
_cache_lock = threading.RLock()
_table_cache = {}


def pricing_layers(settings):
    """Pricing sources in override order: builtin table first, then the
    settings map, the read-only config dir file, and the web-edited data dir
    file last."""
    layers = [
        BUILTIN,
        settings.get('pricing') or {},
        _read_json(settings.get('pricing_file') or Path.home() / '.config/llm-usage/pricing.json'),
    ]
    if settings.get('database'):
        # Web edits are written next to the database; it wins over the read-only config dir.
        layers.append(_read_json(Path(settings['database']).parent / 'pricing.json'))
    return layers


def load_pricing(settings):
    """The merged rate table. The same object is returned until a layer changes, so
    callers must treat it as read-only. None entries explicitly block inheritance;
    they also participate in usage caches and saved-report revision checks."""
    key = (
        json.dumps(settings.get('pricing') or {}, sort_keys=True),
        tuple((str(p), _mtime(p)) for p in _layer_paths(settings)),
    )
    with _cache_lock:
        cached = _table_cache.get('entry')
        if cached and cached[0] == key:
            return cached[1]
        table = _merge(settings)
        _table_cache['entry'] = (key, table)
        return table


def _merge(settings):
    table = {}
    for source in pricing_layers(settings):
        for model, rates in source.items():
            if rates == 'builtin':
                # Restore marker: drop lower-layer overrides, keep the builtin.
                if model in BUILTIN:
                    table[model] = BUILTIN[model]
                else:
                    table[model] = None
            elif rates is None:
                table[model] = None  # retain the barrier to parent fallback
            elif _valid(rates):
                table[model] = _plain(rates)
            elif isinstance(rates, list):
                seq = sorted((_plain(e) for e in rates if _valid(e)), key=lambda e: e.get('since', ''))
                if seq:
                    table[model] = seq
    return table


def pricing_origins(settings):
    """Classify every decided model for the settings UI.

    Values: 'builtin' (effective entry comes from the builtin table),
    'user' (a writable or read-only user layer supplies it), 'hidden'
    (a tombstone suppresses all lower values)."""
    decision = {}
    for index, layer in enumerate(pricing_layers(settings)):
        for model, value in layer.items():
            # As in _merge: an entry that cannot be used decides nothing, so the row is not
            # labelled with a layer whose rates are not the ones applied.
            usable = value is None or value == 'builtin' or _valid(value)
            if usable or (isinstance(value, list) and any(_valid(e) for e in value)):
                decision[model] = (index, value)
    origins = {}
    for model, (index, value) in decision.items():
        if value is None:
            origins[model] = 'hidden'
        elif value == 'builtin':
            origins[model] = 'builtin' if model in BUILTIN else 'hidden'
        else:
            origins[model] = 'builtin' if index == 0 else 'user'
    return origins


RATE_KEYS = ('input', 'cached', 'output', 'cache_write', 'cache_write_5m', 'cache_write_1h')


def _number(value):
    return is_finite_number(value) and value >= 0


def _valid(rates):
    """Numeric input and output rates, and a number for every other rate that is set. A
    hand-written null ('cache_write': null) means unset; text or a negative makes the entry unusable."""
    return (
        isinstance(rates, dict)
        and _number(rates.get('input'))
        and _number(rates.get('output'))
        and all(_number(rates[key]) for key in RATE_KEYS if rates.get(key) is not None)
        # A dated entry's start is a 'YYYY-MM-DD' text; a number or null there cannot be ordered.
        and ('since' not in rates or isinstance(rates['since'], str))
    )


def _plain(rates):
    """The entry without unset (null) rates, so every rate left in it can be multiplied."""
    if all(rates.get(key) is not None for key in RATE_KEYS if key in rates):
        return rates
    return {key: value for key, value in rates.items() if key not in RATE_KEYS or value is not None}


def _read_json(path):
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


# A snapshot suffix is a date ('-20250514', '-2026-01-01') or a non-numeric
# variant ('-low', '-contributor-free'). Any other numeric-leading suffix is a newer
# version ('claude-opus-4-5' is not a 'claude-opus-4' snapshot) and stays unpriced.
DATE_SUFFIX = re.compile(r'(?:\d{8}|\d{4}-\d{2}-\d{2})(?:-|$)')
VERSION_SUFFIX = re.compile(r'\d')


# A suffix that names another size or tier is a different model with its own price
# ('gpt-5-nano' is not a 'gpt-5' snapshot, 'gemini-2.5-flash-lite' not a flash one).
TIER_WORDS = frozenset(
    {
        'nano',
        'mini',
        'micro',
        'lite',
        'small',
        'large',
        'pro',
        'flash',
        'ultra',
        'plus',
        'turbo',
        'haiku',
        'sonnet',
        'opus',
        'instant',
        'chat',
        'image',
        'audio',
        'realtime',
        'search',
        'embedding',
    }
)


def _snapshot_of(name, key):
    if not name.startswith(key + '-'):
        return False
    suffix = name[len(key) + 1 :]
    if DATE_SUFFIX.match(suffix):
        return True
    return not VERSION_SUFFIX.match(suffix) and not any(part.lower() in TIER_WORDS for part in suffix.split('-'))


def _entry_for(model, pricing):
    key = entry_key(model, pricing)
    return pricing.get(key) if key else None


# Resolved keys for the most recent table object: rate_for() runs per (model, day) row.
_key_cache = {'table': None, 'keys': {}}


def entry_key(model, pricing):
    """Pricing-table key a stored model resolves to, or None."""
    if not model or not pricing:
        return None
    with _cache_lock:
        cache = _key_cache
        if cache['table'] is not pricing:
            cache = {'table': pricing, 'keys': {}}
            globals()['_key_cache'] = cache
        if model in cache['keys']:
            return cache['keys'][model]
        key = _resolve(model, pricing)
        cache['keys'][model] = key
        return key


def _resolve(model, pricing):
    if model in pricing:
        return model
    base = model.split(' (', 1)[0]  # 'swe-2 (max)' prices as 'swe-2'
    if base in pricing:
        return base
    matches = [key for key in pricing if _snapshot_of(base, key)]
    return max(matches, key=len) if matches else None


def _at(entry, day):
    # entry is a rate dict or a dated list sorted by 'since' (YYYY-MM-DD).
    if not isinstance(entry, list):
        return entry
    if day is None:
        return entry[-1]
    chosen = entry[0]
    for e in entry:
        if e.get('since', '') <= day:
            chosen = e
        else:
            break
    return chosen


def rate_for(model, pricing, day=None):
    """day: 'YYYY-MM-DD' (KST). None resolves to the latest known rate."""
    return _at(_entry_for(model, pricing), day)


def hidden(model, pricing):
    """The exact model or the parent it would inherit was deliberately suppressed."""
    key = entry_key(model, pricing)
    return key is not None and pricing[key] is None


def upcoming_for(model, pricing, today):
    """First dated rate entry taking effect after today, if any."""
    entry = _entry_for(model, pricing)
    if not isinstance(entry, list):
        return None
    for e in entry:
        if e.get('since', '') > today:
            return e
    return None


def edited_rates(entry, rates, today, remove=()):
    """Merge a web edit into the effective entry for one model.

    A dated series gains an entry taking effect today (KST); earlier days keep
    their rates. A flat rate (or a deleted/new model) stays flat, applying to
    every day as before.
    """
    if isinstance(entry, list):
        current = {k: v for k, v in (_at(entry, today) or {}).items() if k != 'since' and k not in remove}
        change = {**current, **rates, 'since': today}
        return sorted([e for e in entry if e.get('since') != today] + [change], key=lambda e: e.get('since', ''))
    return {**{k: v for k, v in (entry if isinstance(entry, dict) else {}).items() if k not in remove}, **rates}


def estimate(values, rates):
    default = rates.get('cache_write', rates['input'])
    five = values.get('cache_creation_5m', 0)
    hour = values.get('cache_creation_1h', 0)
    unspecified = max(0, values.get('cache_creation', 0) - five - hour)
    return (
        values.get('uncached_input', 0) * rates['input']
        + values.get('cached_input', 0) * rates.get('cached', rates['input'])
        + values.get('output', 0) * rates['output']
        + unspecified * default
        + five * rates.get('cache_write_5m', default)
        + hour * rates.get('cache_write_1h', default)
    ) / 1e6


COST_PARTS = ('uncached_input', 'cached_input', 'output', 'cache_creation')


def estimate_parts(values, rates):
    """estimate() split by token type; the parts add up to the estimate."""
    read = values.get('uncached_input', 0) * rates['input'] / 1e6
    cached = values.get('cached_input', 0) * rates.get('cached', rates['input']) / 1e6
    output = values.get('output', 0) * rates['output'] / 1e6
    return dict(
        uncached_input=read,
        cached_input=cached,
        output=output,
        cache_creation=max(0.0, estimate(values, rates) - read - cached - output),
    )


def cache_estimated_tokens(values, rates):
    """Claude writes priced by a fallback because their TTL or hourly rate is unknown."""
    return values.get('cache_creation_unknown', 0) + (
        values.get('cache_creation_1h', 0) if 'cache_write_1h' not in rates else 0
    )
