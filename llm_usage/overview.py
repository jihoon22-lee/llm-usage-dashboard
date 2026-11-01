"""The 개요 tab: today, this week and the last 30 days at a glance, with a few observed facts.

Every period figure comes from Store.usage(), so it equals the usage tab's card for the same
period (request records plus the reconciled server totals). The facts only describe what was
recorded: no forecast, no expected pace and no advice to switch accounts.
"""
from datetime import datetime, timedelta
import time

from .insights import token_total
from .reconciliation import RESIDUAL, held, residual, supplements
from .store import KST, stamp

TOP = 5
FACTS = 6
# A day or a 3-hour block is only worth naming when it stands out this much.
PEAK_DAY_RATIO = 1.5
PEAK_HOURS_SHARE = 25.0
SERVICE_CHANGE_PCT = 25.0
# A service below this share of the week is too small for its change to matter.
SERVICE_SHARE_FLOOR = 10.0


def _day(moment):
    return moment.strftime('%Y-%m-%d')


def _point_total(point):
    return sum(token_total(v) for v in point['values'].values())


def _timeline(usage, now):
    """Tokens per series point; points that start after `now` have not happened (None)."""
    return [None if stamp(p['time']) > now else _point_total(p) for p in usage['series']]


def _prior(usage):
    prior = usage.get('prior')
    if not prior:
        return None
    return dict(mode=prior['mode'], running=prior['running'], complete=prior['complete'],
                current_tokens=prior['current_tokens'], tokens=token_total(prior), cost=prior.get('cost'))


def _period(usage, now):
    cost = usage.get('cost') or {}
    return dict(start=usage['start'], end_exclusive=usage['end_exclusive'], tokens=token_total(usage['totals']),
                requests=usage['totals'].get('requests', 0), cost=cost.get('period'), cost_coverage=cost.get('coverage'),
                prior=_prior(usage), points=_timeline(usage, now))


def _projects(store, start, end):
    """Tokens per project in [start, end), as the usage tab counts a `project:` scope: the
    records plus the server totals wholly inside the range. The account residual is no project."""
    totals = {}
    with store.connect() as c:
        c.execute('BEGIN')
        for name, tokens in c.execute("SELECT COALESCE(project,'미분류'),SUM(uncached_input+cached_input+output+cache_creation) FROM "
                                      + store.table + ' WHERE ts>=? AND ts<? GROUP BY 1', (start, end)):
            totals[name] = totals.get(name, 0) + (tokens or 0)
        for e in supplements(c)[0]:
            if residual(e) or not (start <= e['start'] and e['end'] <= end):
                continue
            name = e['project'] or '미분류'
            totals[name] = totals.get(name, 0) + e['tokens']
    return totals


def _ranked(totals, denominator):
    top = sorted(((k, v) for k, v in totals.items() if v > 0), key=lambda kv: (-kv[1], kv[0]))[:TOP]
    return [dict(name=k, tokens=v, share=v / denominator * 100 if denominator else None) for k, v in top]


def _models(usage):
    """Models over every service, so a row equals the usage tab's `model:` scope."""
    totals, routes = {}, {}
    for row in usage['rows']:
        if row['model'] == RESIDUAL:
            continue
        tokens = token_total(row)
        totals[row['model']] = totals.get(row['model'], 0) + tokens
        # The service that carried most of the model's use names its color.
        if tokens >= routes.get(row['model'], ('', -1))[1]:
            routes[row['model']] = (row['route'], tokens)
    return totals, {model: route for model, (route, _) in routes.items()}


def _recent(usage, now):
    days = [p['time'][:10] for p in usage['series']]
    services = {}
    for i, point in enumerate(usage['series']):
        for label, values in point['values'].items():
            services.setdefault(label, [0] * len(days))[i] += token_total(values)
    per_day = [sum(v[i] for v in services.values()) for i in range(len(days))]
    reconciliation = usage.get('reconciliation') or {}
    return dict(start=usage['start'], end_exclusive=usage['end_exclusive'], days=days,
                tokens=token_total(usage['totals']), unallocated_tokens=reconciliation.get('unallocated_tokens', 0),
                active_days=sum(1 for v in per_day if v > 0),
                services=[dict(route=k, tokens=sum(v), values=v) for k, v in sorted(services.items(), key=lambda kv: -sum(kv[1])) if sum(v)]), per_day


def _month(store):
    """The last settled month's list-price value against what its subscriptions cost."""
    from .monthly import recent
    months = recent(store, 1)
    if not months:
        return None
    month = months[0]
    subs = [s for s in month.get('subscriptions') or [] if s.get('monthly_usd')]
    priced = [s for s in subs if s.get('value') is not None]
    price = sum(s['monthly_usd'] for s in subs)
    value = sum(s['value'] for s in priced) if priced else None
    return dict(month=month['month'], partial=month.get('partial', False), price=price or None, value=value,
                ratio=value / price if value is not None and price else None,
                subscriptions=[dict(route=s['route'], label=s.get('label'), monthly_usd=s['monthly_usd'], value=s.get('value'),
                                    ratio=s.get('ratio'), tokens=s.get('tokens')) for s in subs])


def _window(row):
    """A quota window as the page names it: route, bucket and the labels recorded with it."""
    return {k: row[k] for k in ('route', 'bucket', 'display_name', 'account_label') if row.get(k) is not None}


def _week_facts(report):
    """Quota exhaustion, time spent at 0% and the largest service change of the last full week."""
    if not report:
        return []
    facts = []
    streaks = {(q['route'], q['bucket']): q.get('streak', 0) for q in report.get('quota_use') or []}
    for e in sorted(report.get('exhausted') or [], key=lambda e: -e['count'])[:2]:
        facts.append(dict(kind='exhausted', week=report['start'], **_window(e), count=e['count'],
                          streak=streaks.get((e['route'], e['bucket']), 0)))
    zero = max((q for q in report.get('quota_use') or [] if (q.get('zero_hours') or 0) >= 1), key=lambda q: q['zero_hours'], default=None)
    if zero:
        facts.append(dict(kind='zero_hours', week=report['start'], **_window(zero), hours=zero['zero_hours']))
    total = (report.get('totals') or {}).get('tokens') or 0
    moved = [r for r in report.get('routes') or [] if r.get('change_pct') is not None and total
             and r['tokens'] / total * 100 >= SERVICE_SHARE_FLOOR and abs(r['change_pct']) >= SERVICE_CHANGE_PCT]
    if moved:
        r = max(moved, key=lambda r: abs(r['change_pct']))
        facts.append(dict(kind='service_change', week=report['start'], route=r['route'], change_pct=r['change_pct'], tokens=r['tokens']))
    return facts


def _usage_facts(recent, per_day, heatmap):
    facts = []
    active = [v for v in per_day if v > 0]
    if len(active) >= 7:
        average = sum(active) / len(active)
        peak = max(range(len(per_day)), key=lambda i: per_day[i])
        if average and per_day[peak] / average >= PEAK_DAY_RATIO:
            facts.append(dict(kind='peak_day', day=recent['days'][peak], tokens=per_day[peak], ratio=per_day[peak] / average))
    hours = [0] * 24
    for cell in heatmap or []:
        hours[cell['hour']] += cell['tokens'] or 0
    whole = sum(hours)
    if whole:
        start = max(range(22), key=lambda h: sum(hours[h:h + 3]))
        share = sum(hours[start:start + 3]) / whole * 100
        if share >= PEAK_HOURS_SHARE:
            facts.append(dict(kind='peak_hours', start_hour=start, end_hour=start + 3, share=share))
    return facts


def _subscription_facts(prices, usage, month):
    """Subscriptions with no record in the 30 days, and last month's lowest value against price."""
    facts = []
    tokens = {}
    for row in usage['rows']:
        tokens[row['route']] = tokens.get(row['route'], 0) + token_total(row)
    for part in usage.get('claude_split') or []:
        tokens[part['account']] = part['tokens']
    missing = {gap['route'] for gap in usage.get('unavailable_routes') or []}
    for route, price in sorted((prices or {}).items()):
        if price and not tokens.get(route):
            facts.append(dict(kind='idle_subscription', route=route, monthly_usd=price, collected=route not in missing))
    low = min((s for s in (month or {}).get('subscriptions') or [] if s.get('ratio') is not None and s['ratio'] < 1),
              key=lambda s: s['ratio'], default=None)
    if low:
        facts.append(dict(kind='value_below_price', month=month['month'], route=low['route'], label=low.get('label'),
                          ratio=low['ratio'], value=low['value'], monthly_usd=low['monthly_usd']))
    return facts


def build(store, now=None, *, pricing=None, subscriptions=None, ended=None, names=None):
    """The 개요 response. `subscriptions`: the monthly prices of the subscriptions not ended."""
    from .reports import recent as reports
    now = now or time.time()
    midnight = datetime.fromtimestamp(now, KST).replace(hour=0, minute=0, second=0, microsecond=0)
    monday = midnight - timedelta(days=midnight.weekday())
    span = dict(group='route', pricing=pricing, sections='core', now=now, ended=ended, names=names)
    # One reading of the server totals serves every period below.
    with held():
        today = store.usage(period='today', granularity='hour', **span)
        week = store.usage(period='custom', start=_day(monday), end=_day(monday + timedelta(days=6)), granularity='day', **span)
        last30 = store.usage(period='30d', granularity='day', **span)
        projects = _projects(store, stamp(last30['start']), stamp(last30['end_exclusive']))
    recent, per_day = _recent(last30, now)
    models, model_routes = _models(last30)
    month = _month(store)
    report = (reports(store, 1) or [None])[0]
    facts = _week_facts(report) + _subscription_facts(subscriptions, last30, month) + _usage_facts(recent, per_day, last30.get('heatmap'))
    return dict(generated=now, collected_at=last30.get('collected_at'),
                today=_period(today, now), week=_period(week, now), recent=recent,
                models=[{**m, 'route': model_routes.get(m['name'])} for m in _ranked(models, recent['tokens'])],
                projects=_ranked({k: v for k, v in projects.items() if k != RESIDUAL}, recent['tokens']),
                month=month, facts=facts[:FACTS])
