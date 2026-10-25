"""Monthly subscription prices over time.

`subscription_prices` holds each subscription's current monthly price. A price change
that applies from a moment on keeps the earlier price in `subscription_price_history`
as {route: [{'until': epoch seconds, 'usd': price}, ...]}: each entry's price applied
until its `until`, after the previous entry's. The earliest entry also covers the time
before it, and with no history the current price covers all time, as before.

Subscription value divides list-price value by the price paid over the same span, so a
later price never re-values earlier use. Real billing (proration, credits, the next
renewal) can differ; the span's price is a time-weighted approximation.
"""
import math

MONTH_DAYS=30.44
MODES=('now','at','correct','all')


def _number(value):
    return isinstance(value,(int,float)) and not isinstance(value,bool) and math.isfinite(value)


def clean_history(raw):
    """Valid entries only, oldest first; a broken file never breaks the value analysis."""
    result={}
    for route,entries in (raw if isinstance(raw,dict) else {}).items():
        if not isinstance(route,str) or not isinstance(entries,list):continue
        kept=sorted(({'until':float(e['until']),'usd':e['usd']} for e in entries
                     if isinstance(e,dict) and _number(e.get('until')) and e['until']>0
                     and _number(e.get('usd')) and e['usd']>=0),key=lambda e:e['until'])
        if kept:result[route]=kept
    return result


def segments(current,entries,start,end):
    """[(start, end, monthly price)] covering [start, end); empty for an empty span."""
    if end<=start:return []
    bounds=[];low=-math.inf
    for e in entries or []:
        bounds.append((low,e['until'],e['usd']));low=e['until']
    bounds.append((low,math.inf,current))
    return [(max(lo,start),min(hi,end),usd) for lo,hi,usd in bounds if max(lo,start)<min(hi,end)]


def weighted(parts):
    """Time-weighted monthly price of the parts, None when any part has no price."""
    span=sum(hi-lo for lo,hi,_ in parts)
    if not parts or span<=0 or any(usd is None for _,_,usd in parts):return None
    return sum(usd*(hi-lo) for lo,hi,usd in parts)/span


def change(entries,current,new,mode,at,now):
    """History of one route after its price becomes `new`.

    'now'/'at': `new` applies from that moment and earlier prices stay as they were;
    'correct': the current price was wrong, earlier changes stay; 'all': `new` for all
    time. An unchanged price keeps its history whatever the mode. Clearing a price (None)
    drops the history, as nothing is valued then."""
    entries=list(entries or [])
    if new is None:return []
    if current==new:return entries
    if mode=='all':return []
    if mode=='correct':return entries
    since=now if mode=='now' else at
    kept=[e for e in entries if e['until']<=since]
    covering=next((e for e in entries if e['until']>since),None)
    previous=covering['usd'] if covering else current
    # The span being replaced may already have ended at that moment (zero length).
    if previous is not None and not (kept and kept[-1]['until']>=since):kept.append({'until':since,'usd':previous})
    # Adjacent equal prices are one span; a last span at the new price merges into it.
    merged=[]
    for e in kept:
        if merged and merged[-1]['usd']==e['usd']:merged[-1]=e
        else:merged.append(e)
    while merged and merged[-1]['usd']==new:merged.pop()
    return merged
