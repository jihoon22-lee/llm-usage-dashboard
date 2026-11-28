"""Cost & subscription what-if simulator: partial model substitution and sub vs API comparison."""

from datetime import datetime, timedelta
import math
import time

from .aliases import raw_names_for, safe_aliases
from .db.schema import KST, METRICS, calendar_buckets
from .insights import token_total
from .pricing import estimate, rate_for
from .subscriptions import MONTH_DAYS

MAX_SUBSTITUTIONS = 20


def _period_bounds(store, c, period, start, end, now):
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == '1h':
        cutoff = int(now.timestamp())
        b = datetime.fromtimestamp(cutoff, KST)
        return b - timedelta(hours=1), b
    if period == 'today':
        return midnight, midnight + timedelta(days=1)
    if period in ('7d', '14d', '30d', '90d'):
        days = int(period[:-1])
        return midnight - timedelta(days=days - 1), midnight + timedelta(days=1)
    if period == 'month':
        return midnight.replace(day=1), midnight + timedelta(days=1)
    if period == 'all':
        first = c.execute(f'SELECT MIN(ts) FROM {store.table}').fetchone()[0]
        if first is None:
            return midnight, midnight + timedelta(days=1)
        a = datetime.fromtimestamp(first, KST).replace(hour=0, minute=0, second=0, microsecond=0)
        return a, midnight + timedelta(days=1)
    if period == 'custom' and start and end:
        a = datetime.strptime(start, '%Y-%m-%d').replace(tzinfo=KST)
        b = datetime.strptime(end, '%Y-%m-%d').replace(tzinfo=KST) + timedelta(days=1)
        calendar_buckets(a, b, 'day')  # validates end > start and bounded span
        return a, b
    raise ValueError('기간이 올바르지 않습니다.')


def _scope_clause(store, scope, aliases):
    if not scope:
        return '', ()
    kind, _, value = scope.partition(':')
    if kind not in store.SCOPE_KINDS or not value:
        raise ValueError('범위가 올바르지 않습니다.')
    if kind == 'project' and aliases:
        names = raw_names_for(value, aliases)
        if not names:
            return ' AND 1=0', ()
        placeholders = ','.join('?' for _ in names)
        return f' AND {store.SCOPE_KINDS[kind]} IN ({placeholders})', tuple(names)
    return ' AND ' + store.SCOPE_KINDS[kind] + '=?', (value,)


def validate_substitutions(items, model_re):
    """Validate [{from_model, to_model, ratio}] list with per-source total ratio <= 1.0."""
    if items is None:
        return []
    if not isinstance(items, list) or len(items) > MAX_SUBSTITUTIONS:
        raise ValueError('모델 대체 규칙은 최대 20개까지 지정할 수 있습니다.')
    clean = []
    totals = {}
    for entry in items:
        if not isinstance(entry, dict):
            raise ValueError('모델 대체 규칙 형식이 올바르지 않습니다.')
        src = entry.get('from_model')
        dst = entry.get('to_model')
        ratio = entry.get('ratio')
        if (
            not isinstance(src, str)
            or not model_re.fullmatch(src)
            or not isinstance(dst, str)
            or not model_re.fullmatch(dst)
        ):
            raise ValueError('대체 대상 모델 이름을 확인하세요.')
        if (
            isinstance(ratio, bool)
            or not isinstance(ratio, (int, float))
            or not math.isfinite(ratio)
            or not 0.0 <= ratio <= 1.0
        ):
            raise ValueError('대체 비율은 0~1 사이여야 합니다.')
        if ratio == 0 or src == dst:
            continue
        totals[src] = totals.get(src, 0.0) + float(ratio)
        if totals[src] > 1.0001:
            raise ValueError('같은 원본 모델의 대체 비율 합계는 100%를 넘을 수 없습니다.')
        clean.append(dict(from_model=src, to_model=dst, ratio=float(ratio)))
    return clean


def simulate(
    store,
    *,
    period='30d',
    start=None,
    end=None,
    scope=None,
    substitutions=(),
    pricing=None,
    subscriptions=None,
    project_aliases=None,
    now=None,
):
    """Compute baseline vs substituted API cost and compare against monthly subscriptions."""
    aliases = safe_aliases(project_aliases)
    now_dt = datetime.fromtimestamp(now or time.time(), KST)
    sums = ','.join(f'COALESCE(SUM({k}),0) AS {k}' for k in METRICS)
    scope_sql, scope_args = _scope_clause(store, scope, aliases)
    with store.connect() as c:
        c.execute('BEGIN')
        a, b = _period_bounds(store, c, period, start, end, now_dt)
        table = 'events' if period == '1h' else store.table
        rows = [
            dict(r)
            for r in c.execute(
                "SELECT route,model,date(ts,'unixepoch','+9 hours') AS day,"
                + sums
                + ' FROM '
                + table
                + ' WHERE ts>=? AND ts<?'
                + scope_sql
                + ' GROUP BY route,model,day',
                (a.timestamp(), b.timestamp(), *scope_args),
            )
        ]

    elapsed_days = max(1.0, (min(b, now_dt) - a).total_seconds() / 86400.0)
    scale_to_month = MONTH_DAYS / elapsed_days

    by_src = {}
    for sub in substitutions:
        by_src.setdefault(sub['from_model'], []).append(sub)

    baseline_total = 0.0
    simulated_total = 0.0
    priced_tokens = 0
    total_tokens = 0
    unpriced_targets = set()
    by_route = {}
    rule_stats = {
        (s['from_model'], s['to_model']): dict(
            from_model=s['from_model'],
            to_model=s['to_model'],
            ratio=s['ratio'],
            shifted_tokens=0,
            baseline_cost=0.0,
            simulated_cost=0.0,
            target_priced=True,
        )
        for s in substitutions
    }

    for row in rows:
        tokens = token_total(row)
        total_tokens += tokens
        route_entry = by_route.setdefault(
            row['route'], dict(route=row['route'], tokens=0, baseline_cost=0.0, simulated_cost=0.0, priced=False)
        )
        route_entry['tokens'] += tokens
        base_rate = rate_for(row['model'], pricing, row['day']) if pricing else None
        if not base_rate:
            continue
        base_cost = estimate(row, base_rate)
        baseline_total += base_cost
        priced_tokens += tokens
        route_entry['baseline_cost'] += base_cost
        route_entry['priced'] = True

        rules = by_src.get(row['model'])
        if not rules:
            simulated_total += base_cost
            route_entry['simulated_cost'] += base_cost
            continue

        remaining_ratio = 1.0
        row_sim_cost = 0.0
        for rule in rules:
            ratio = min(rule['ratio'], remaining_ratio)
            remaining_ratio = max(0.0, remaining_ratio - ratio)
            dst_rate = rate_for(rule['to_model'], pricing, row['day']) if pricing else None
            stat = rule_stats[(rule['from_model'], rule['to_model'])]
            stat['shifted_tokens'] += round(tokens * ratio)
            part_base = base_cost * ratio
            stat['baseline_cost'] += part_base
            if dst_rate:
                part_sim = estimate(row, dst_rate) * ratio
                stat['simulated_cost'] += part_sim
                row_sim_cost += part_sim
            else:
                unpriced_targets.add(rule['to_model'])
                stat['target_priced'] = False
                stat['simulated_cost'] += part_base
                row_sim_cost += part_base
        row_sim_cost += base_cost * remaining_ratio
        simulated_total += row_sim_cost
        route_entry['simulated_cost'] += row_sim_cost

    baseline_monthly = baseline_total * scale_to_month if priced_tokens else None
    simulated_monthly = simulated_total * scale_to_month if priced_tokens else None
    savings_period = (baseline_total - simulated_total) if priced_tokens else None
    savings_monthly = (baseline_monthly - simulated_monthly) if priced_tokens else None
    savings_pct = (savings_period / baseline_total * 100.0) if priced_tokens and baseline_total > 0 else None

    sub_prices = {
        r: float(v)
        for r, v in (subscriptions or {}).items()
        if isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0
    }
    sub_monthly = sum(sub_prices.values()) if sub_prices else None
    sub_period = sub_monthly * (elapsed_days / MONTH_DAYS) if sub_monthly is not None else None

    def _compare(api_monthly):
        if api_monthly is None or sub_monthly is None:
            return dict(
                api_monthly=round(api_monthly, 2) if api_monthly is not None else None,
                subscription_monthly=round(sub_monthly, 2) if sub_monthly is not None else None,
                delta_monthly=None,
                ratio=None,
                verdict='insufficient_data',
            )
        delta = api_monthly - sub_monthly
        verdict = 'subscription_cheaper' if delta > 0.01 else 'api_cheaper' if delta < -0.01 else 'equal'
        return dict(
            api_monthly=round(api_monthly, 2),
            subscription_monthly=round(sub_monthly, 2),
            delta_monthly=round(delta, 2),
            ratio=round(api_monthly / sub_monthly, 3) if sub_monthly > 0 else None,
            verdict=verdict,
        )

    route_rows = []
    for route in sorted(set(by_route) | set(sub_prices)):
        e = by_route.get(route) or dict(tokens=0, baseline_cost=0.0, simulated_cost=0.0, priced=False)
        route_rows.append(
            dict(
                route=route,
                tokens=e['tokens'],
                subscription_monthly=round(sub_prices[route], 2) if route in sub_prices else None,
                baseline_api_period=round(e['baseline_cost'], 4) if e['priced'] else None,
                simulated_api_period=round(e['simulated_cost'], 4) if e['priced'] else None,
                baseline_api_monthly=round(e['baseline_cost'] * scale_to_month, 2) if e['priced'] else None,
                simulated_api_monthly=round(e['simulated_cost'] * scale_to_month, 2) if e['priced'] else None,
            )
        )

    breakdown = [
        dict(
            from_model=s['from_model'],
            to_model=s['to_model'],
            ratio=s['ratio'],
            shifted_tokens=s['shifted_tokens'],
            baseline_cost=round(s['baseline_cost'], 4),
            simulated_cost=round(s['simulated_cost'], 4),
            delta_cost=round(s['simulated_cost'] - s['baseline_cost'], 4),
            target_priced=s['target_priced'],
        )
        for s in rule_stats.values()
    ]

    return dict(
        period=period,
        start=a.isoformat(),
        end_exclusive=b.isoformat(),
        elapsed_days=round(elapsed_days, 2),
        total_tokens=total_tokens,
        priced_tokens=priced_tokens,
        coverage=round(priced_tokens / total_tokens * 100.0, 2) if total_tokens else None,
        baseline_api_period=round(baseline_total, 4) if priced_tokens else None,
        simulated_api_period=round(simulated_total, 4) if priced_tokens else None,
        baseline_api_monthly=round(baseline_monthly, 2) if baseline_monthly is not None else None,
        simulated_api_monthly=round(simulated_monthly, 2) if simulated_monthly is not None else None,
        savings_period=round(savings_period, 4) if savings_period is not None else None,
        savings_monthly=round(savings_monthly, 2) if savings_monthly is not None else None,
        savings_pct=round(savings_pct, 2) if savings_pct is not None else None,
        subscription_monthly=round(sub_monthly, 2) if sub_monthly is not None else None,
        subscription_period=round(sub_period, 4) if sub_period is not None else None,
        baseline_vs_subscription=_compare(baseline_monthly),
        simulated_vs_subscription=_compare(simulated_monthly),
        by_route=route_rows,
        breakdown=breakdown,
        unpriced_targets=sorted(unpriced_targets),
    )
