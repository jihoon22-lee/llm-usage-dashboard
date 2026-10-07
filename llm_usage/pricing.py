"""Optional public per-model pricing for cost estimation.

Only models with an exact name or dated-snapshot prefix match are priced;
unpriced models report no estimate instead of guessing. Users extend or
override rates in ~/.config/llm-usage/pricing.json or the 'pricing' config map.
"""
import json
from pathlib import Path
import re

# USD per 1M tokens. cache_write=None falls back to the input rate.
# Provider-internal or unidentified models with no public token price: costs stay
# unestimated by design, so they are not reported as missing a rate.
NO_PUBLIC_PRICE=frozenset({'compactor','codex-auto-review','gpt-reserve','unknown'})
BUILTIN={
    'claude-opus-4':dict(input=15,cached=1.5,output=75,cache_write=18.75),
    'claude-sonnet-4':dict(input=3,cached=0.3,output=15,cache_write=3.75),
    'claude-haiku-4':dict(input=1,cached=0.1,output=5,cache_write=1.25),
    'claude-opus-5-5':dict(input=4,cached=0.2,output=20,cache_write=5),
    'gpt-5':dict(input=1.25,cached=0.125,output=10,cache_write=1.25),
    'gpt-5-mini':dict(input=0.25,cached=0.025,output=2,cache_write=0.25),
    'gpt-6-sol':dict(input=2,cached=0.2,output=10,cache_write=2.5),
    # Standard <=272K input, verified 2026-10-03; 6.1 halves Sol's cache-read rate.
    # https://developers.openai.com/api/docs/models/gpt-6.1-sol
    'gpt-6.1-sol':dict(input=2,cached=0.1,output=10,cache_write=2.5),
    'gpt-6-luna':dict(input=0.1,cached=0.01,output=0.5,cache_write=0.125),
    'gemini-2.5-pro':dict(input=1.25,cached=0.31,output=10),
    'gemini-2.5-flash':dict(input=0.3,cached=0.075,output=2.5),
    # Dated history (KST 'since' dates; each applies until the next entry).
    # Only past, announced changes are encoded — promo end dates shift, so
    # future rates are never scheduled here and must be updated manually.
    # GPT-5.6 GA'd 2026-06-26; Sol promo cut 2026-08-22 ("at least through"
    # 2026-11-21 per OpenAI, extendable).
    'gpt-5.6-sol':[dict(since='2026-06-26',input=5,cached=0.5,output=30,cache_write=6.25),
                   dict(since='2026-08-22',input=4,cached=0.4,output=20,cache_write=5)],
    'gpt-5.6-terra':[dict(since='2026-06-26',input=2.5,cached=0.25,output=15,cache_write=3.125),
                     dict(since='2026-07-30',input=2,cached=0.2,output=12,cache_write=2.5)],
    'gpt-5.6-luna':[dict(since='2026-06-26',input=1,cached=0.1,output=6,cache_write=1.25),
                    dict(since='2026-07-30',input=0.2,cached=0.02,output=1.2,cache_write=0.25)],
    # Gemini 3.x Flash introductory rates (announced to end 2026-12-31 but
    # left unscheduled — update manually when Google confirms the new rates).
    'gemini-3.6-flash':dict(input=0.75,cached=0.075,output=3.75),
    'gemini-3.7-flash':dict(input=0.75,cached=0.075,output=3.75),
    'gemini-3.8-flash':dict(input=0.75,cached=0.075,output=3.75),
    # deepseek-v4-flash is the retired V4 API alias; observed events predate
    # the V4.1-Flash handover so the original V4 rates apply.
    'deepseek-v4-flash':dict(input=0.14,cached=0.0028,output=0.28),
}


def _layer_paths(settings):
    paths=[Path(settings.get('pricing_file') or Path.home()/'.config/llm-usage/pricing.json')]
    if settings.get('database'):paths.append(Path(settings['database']).parent/'pricing.json')
    return paths


def _mtime(path):
    try:return path.stat().st_mtime_ns
    except OSError:return None


# load_pricing() runs on every request; reuse the merged table until a layer changes.
_table_cache={}


def pricing_layers(settings):
    """Pricing sources in override order: builtin table first, then the
    settings map, the read-only config dir file, and the web-edited data dir
    file last."""
    layers=[BUILTIN,settings.get('pricing') or {},
            _read_json(settings.get('pricing_file') or Path.home()/'.config/llm-usage/pricing.json')]
    if settings.get('database'):
        # Web edits are written next to the database; it wins over the read-only config dir.
        layers.append(_read_json(Path(settings['database']).parent/'pricing.json'))
    return layers


def load_pricing(settings):
    """The merged rate table. The same object is returned until a layer changes, so
    callers must treat it as read-only."""
    key=(json.dumps(settings.get('pricing') or {},sort_keys=True),
         tuple((str(p),_mtime(p)) for p in _layer_paths(settings)))
    cached=_table_cache.get('entry')
    if cached and cached[0]==key:return cached[1]
    table=_merge(settings)
    _table_cache['entry']=(key,table)
    return table


def _merge(settings):
    table={}
    for source in pricing_layers(settings):
        for model,rates in source.items():
            if rates=='builtin':
                # Restore marker: drop lower-layer overrides, keep the builtin.
                if model in BUILTIN:table[model]=BUILTIN[model]
                else:table.pop(model,None)
            elif rates is None:table.pop(model,None)  # deletion tombstone
            elif _valid(rates):table[model]=rates
            elif isinstance(rates,list):
                seq=sorted((e for e in rates if _valid(e)),key=lambda e:e.get('since',''))
                if seq:table[model]=seq
    return table


def pricing_origins(settings):
    """Classify every decided model for the settings UI.

    Values: 'builtin' (effective entry comes from the builtin table),
    'user' (a writable or read-only user layer supplies it), 'hidden'
    (a tombstone suppresses all lower values)."""
    decision={}
    for index,layer in enumerate(pricing_layers(settings)):
        for model,value in layer.items():decision[model]=(index,value)
    origins={}
    for model,(index,value) in decision.items():
        if value is None:origins[model]='hidden'
        elif value=='builtin':origins[model]='builtin' if model in BUILTIN else 'hidden'
        else:origins[model]='builtin' if index==0 else 'user'
    return origins


def _valid(rates):
    return isinstance(rates,dict) and isinstance(rates.get('input'),(int,float)) and isinstance(rates.get('output'),(int,float))


def _read_json(path):
    try:return json.loads(Path(path).read_text())
    except (OSError,ValueError):return {}


# A snapshot suffix is a date ('-20250514', '-2026-01-01') or a non-numeric
# variant ('-low', '-contributor-free'). Any other numeric-leading suffix is a newer
# version ('claude-opus-4-5' is not a 'claude-opus-4' snapshot) and stays unpriced.
DATE_SUFFIX=re.compile(r'(?:\d{8}|\d{4}-\d{2}-\d{2})(?:-|$)')
VERSION_SUFFIX=re.compile(r'\d')


# A suffix that names another size or tier is a different model with its own price
# ('gpt-5-nano' is not a 'gpt-5' snapshot, 'gemini-2.5-flash-lite' not a flash one).
TIER_WORDS=frozenset({'nano','mini','micro','lite','small','large','pro','flash','ultra','plus','turbo',
                      'haiku','sonnet','opus','instant','chat','image','audio','realtime','search','embedding'})


def _snapshot_of(name,key):
    if not name.startswith(key+'-'):return False
    suffix=name[len(key)+1:]
    if DATE_SUFFIX.match(suffix):return True
    return not VERSION_SUFFIX.match(suffix) and suffix.split('-',1)[0].lower() not in TIER_WORDS


def _entry_for(model,pricing):
    key=entry_key(model,pricing)
    return pricing.get(key) if key else None


# Resolved keys for the most recent table object: rate_for() runs per (model, day) row.
_key_cache={'table':None,'keys':{}}


def entry_key(model,pricing):
    """Pricing-table key a stored model resolves to, or None."""
    if not model or not pricing:return None
    cache=_key_cache
    if cache['table'] is not pricing:
        cache={'table':pricing,'keys':{}};globals()['_key_cache']=cache
    if model in cache['keys']:return cache['keys'][model]
    key=_resolve(model,pricing)
    cache['keys'][model]=key
    return key


def _resolve(model,pricing):
    if model in pricing:return model
    base=model.split(' (',1)[0]  # 'swe-2 (max)' prices as 'swe-2'
    if base in pricing:return base
    matches=[key for key in pricing if _snapshot_of(base,key)]
    return max(matches,key=len) if matches else None


def _at(entry,day):
    # entry is a rate dict or a dated list sorted by 'since' (YYYY-MM-DD).
    if not isinstance(entry,list):return entry
    if day is None:return entry[-1]
    chosen=entry[0]
    for e in entry:
        if e.get('since','')<=day:chosen=e
        else:break
    return chosen


def rate_for(model,pricing,day=None):
    """day: 'YYYY-MM-DD' (KST). None resolves to the latest known rate."""
    return _at(_entry_for(model,pricing),day)


def upcoming_for(model,pricing,today):
    """First dated rate entry taking effect after today, if any."""
    entry=_entry_for(model,pricing)
    if not isinstance(entry,list):return None
    for e in entry:
        if e.get('since','')>today:return e
    return None


def edited_rates(entry,rates,today):
    """Merge a web edit into the effective entry for one model.

    A dated series gains an entry taking effect today (KST); earlier days keep
    their rates. A flat rate (or a deleted/new model) stays flat, applying to
    every day as before.
    """
    if isinstance(entry,list):
        current={k:v for k,v in (_at(entry,today) or {}).items() if k!='since'}
        change={**current,**rates,'since':today}
        return sorted([e for e in entry if e.get('since')!=today]+[change],key=lambda e:e.get('since',''))
    return {**(entry if isinstance(entry,dict) else {}),**rates}


def estimate(values,rates):
    return (values.get('uncached_input',0)*rates['input']
            +values.get('cached_input',0)*rates.get('cached',rates['input'])
            +values.get('output',0)*rates['output']
            +values.get('cache_creation',0)*rates.get('cache_write',rates['input']))/1e6
