"""Derived signals over stored observations: quota events, capacity and usage spikes.

Every value here is computed from recorded observations; nothing is filled in for
a window, hour or bucket that was not observed.
"""
import statistics

EVENT_DAYS=30
SPIKE_DAYS=14
# Hours below this many tokens are never reported as a spike, whatever the baseline.
SPIKE_FLOOR=5_000_000
SPIKE_TOKENS='uncached_input+cached_input+output+cache_creation'


def quota_events(c,route,bucket,source,now,days=EVENT_DAYS,since=0):
    """Exhaustion (remaining reaching 0 from above) and recovery (a new reset with
    more remaining) observed in one source's history of a bucket."""
    rows=c.execute('SELECT checked,remaining,resets FROM limit_history WHERE route=? AND bucket=? AND source=? '
                   'AND checked>=? ORDER BY checked',(route,bucket,source,max(now-days*86400,since))).fetchall()
    events=[];previous=None
    for checked,remaining,resets in rows:
        if remaining is None:continue
        if previous is not None:
            before_remaining,before_resets=previous
            if remaining<=0<before_remaining:events.append(dict(kind='exhausted',ts=checked))
            elif (before_remaining<=0<remaining and resets and before_resets and abs(resets-before_resets)>2):
                events.append(dict(kind='recovered',ts=checked))
        previous=(remaining,resets)
    exhausted=[e for e in events if e['kind']=='exhausted']
    return dict(days=days,exhausted=len(exhausted),last_exhausted=exhausted[-1]['ts'] if exhausted else None,
                recent=events[-8:])


def capacity(row,min_used=5):
    """Tokens and requests one percentage point bought in the current window, and
    what the remaining percentage would buy at that rate. Needs a window that
    started full and has used at least min_used points; an estimate, not a quota."""
    window=row.get('window') or {};remaining=row.get('remaining')
    if row.get('status')!='fresh' or remaining is None or not window.get('open') or not window.get('tokens'):return None
    plan=row.get('plan')
    used=100-remaining
    if used<min_used or plan is None:return None
    per_point=window['tokens']/used
    return dict(tokens_per_point=per_point,remaining_tokens=per_point*remaining,
                remaining_requests=(window.get('requests') or 0)/used*remaining,used_points=used)


def usage_spike(c,now,days=SPIKE_DAYS):
    """The current or last full hour when it is far above this account's usual hours.

    Baseline: the 95th percentile of the non-empty hours in the previous `days`,
    and at least three times their median. Returns None when there is too little
    history to call anything unusual."""
    hour=int(now//3600)
    # usage_hourly (store.py) already holds whole-hour sums.
    totals=dict(c.execute(f'SELECT CAST(ts/3600 AS INTEGER) AS h,SUM({SPIKE_TOKENS}) FROM usage_hourly '
                          'WHERE ts>=? GROUP BY h',((hour-days*24)*3600,)).fetchall())
    history=sorted(v for h,v in totals.items() if h<hour-1 and v>0)
    if len(history)<48:return None
    baseline=max(history[int(len(history)*0.95)],3*statistics.median(history),SPIKE_FLOOR)
    for h in (hour,hour-1):
        tokens=totals.get(h,0)
        if tokens>baseline:
            return dict(hour_start=h*3600,tokens=tokens,baseline=baseline,ratio=tokens/baseline,current=h==hour)
    return None
