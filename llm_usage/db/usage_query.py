"""Usage aggregation, period comparison, reconciliation overlay, and cache management."""

from datetime import datetime, timedelta, timezone
import json
import math
import re
import time

from ..claude_accounts import is_claude
from .schema import (
    BUCKET_SQL,
    KST,
    METRICS,
    NAMED_EFFORT_ROUTES,
    PROJECTION_FROM_DAY,
    SERIES_KEYS,
    _EFFORT_SUFFIX,
    calendar_buckets,
    route_events,
    stamp,
)


class UsageMixin:
    # Subscription accounts: one per route, Claude Code split by account (pending = original).
    ACCOUNT_SQL = "CASE WHEN route='claude-code' THEN COALESCE(account,'claude-code') ELSE route END"
    GROUPS = {
        'provider': 'provider',
        'route': 'route',
        'model': 'model',
        'project': "COALESCE(project,'미분류')",
        'agent': "COALESCE(agent_kind,'미분류')",
        'account': ACCOUNT_SQL,
        'effort': "COALESCE(effort,'미기록')",
    }
    SCOPE_KINDS = {
        'route': 'route',
        'provider': 'provider',
        'model': 'model',
        'project': "COALESCE(project,'미분류')",
        'account': ACCOUNT_SQL,
    }
    COMPARE_SHIFTS = {'week': 7, 'month': 28}
    COMPARE_LABELS = {'previous': '직전 동일 기간', 'week': '7일 전 동일 구간', 'month': '28일 전 동일 구간'}

    def _period_free(self, c, lifetime, midnight, pricing, sums, scope_sql='', scope_args=()):
        """Lifetime cost and the 26-week calendar ignore the selected period.

        Reuse them until the data changes: any event write moves the lifetime
        sums, a collection pass bumps last_local, and a sum-preserving relabel
        bumps data_version.
        """
        key = (
            tuple(lifetime.values()),
            self.state(c, 'last_local'),
            midnight.date().isoformat(),
            json.dumps(pricing, sort_keys=True) if pricing else None,
            scope_sql + repr(scope_args),
            self.state(c, 'data_version') or 0,
        )
        memo = self._memo  # one read: another thread may replace it meanwhile
        if memo.get('key') != key:
            from ..insights import token_total
            from ..pricing import estimate, rate_for

            month = None
            if pricing:
                month_start = midnight.replace(day=1)
                days_in_month = ((month_start.replace(day=28) + timedelta(days=4)).replace(day=1) - month_start).days
                by_route = {}
                for r in c.execute(
                    "SELECT route,model,date(ts,'unixepoch','+9 hours') AS day,"
                    + sums
                    + ' FROM '
                    + self.table
                    + ' WHERE ts>=?'
                    + scope_sql
                    + ' GROUP BY route,model,day',
                    (month_start.timestamp(), *scope_args),
                ):
                    rate = rate_for(r['model'], pricing, r['day'])
                    if rate:
                        by_route[r['route']] = by_route.get(r['route'], 0) + estimate(dict(r), rate)
                if by_route:
                    month_total = sum(by_route.values())
                    elapsed = max(1, midnight.day)
                    month = dict(
                        days_elapsed=elapsed,
                        days_in_month=days_in_month,
                        cost_by_route={r: round(v, 4) for r, v in sorted(by_route.items())},
                        total=round(month_total, 4),
                        projected=round(month_total / elapsed * days_in_month, 4)
                        if elapsed >= PROJECTION_FROM_DAY
                        else None,
                        projected_from_day=PROJECTION_FROM_DAY,
                    )
            cal = {}
            for r in c.execute(
                "SELECT date(ts,'unixepoch','+9 hours') AS day,model,"
                + sums
                + ','
                + self.rcount
                + ' FROM '
                + self.table
                + ' WHERE ts>=?'
                + scope_sql
                + ' GROUP BY day,model',
                ((midnight - timedelta(days=181)).timestamp(), *scope_args),
            ):
                e = cal.setdefault(r['day'], dict(day=r['day'], tokens=0, requests=0, cost=0.0, rated=False))
                e['tokens'] += r['uncached_input'] + r['cached_input'] + r['output'] + r['cache_creation']
                e['requests'] += r['requests']
                if pricing:
                    rate = rate_for(r['model'], pricing, r['day'])
                    if rate:
                        e['cost'] += estimate(dict(r), rate)
                        e['rated'] = True
            calendar = [
                dict(
                    day=e['day'],
                    tokens=e['tokens'],
                    requests=e['requests'],
                    cost=round(e['cost'], 4) if e['rated'] else None,
                )
                for _, e in sorted(cal.items())
            ]
            cost = priced = 0
            if pricing:
                for r in c.execute(
                    "SELECT model,date(ts,'unixepoch','+9 hours') AS day,"
                    + sums
                    + ' FROM '
                    + self.table
                    + ' WHERE 1=1'
                    + scope_sql
                    + ' GROUP BY model,day',
                    scope_args,
                ):
                    rate = rate_for(r['model'], pricing, r['day'])
                    if rate:
                        cost += estimate(dict(r), rate)
                        priced += token_total(dict(r))
            memo = dict(key=key, calendar=calendar, cost=cost, priced=priced, month=month)
            self._memo = memo
        return (
            [dict(day) for day in memo['calendar']],
            memo['cost'],
            memo['priced'],
            memo['month'] and dict(memo['month']),
        )

    def account_tokens(self, c, route, buckets, checked):
        """Store the account's daily totals; a running day's bucket keeps growing."""
        namespace = self.account_namespace(c, route)
        changed = False
        for b in buckets:
            if not isinstance(b, dict):
                continue
            day, tokens = b.get('startDate'), b.get('tokens')
            if (
                not isinstance(day, str)
                or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', day)
                or type(tokens) is not int
                or tokens < 0
            ):
                continue
            try:
                datetime.strptime(day, '%Y-%m-%d')
            except ValueError:
                continue
            row = c.execute(
                'SELECT tokens FROM account_token_buckets WHERE route=? AND namespace=? AND day=?',
                (route, namespace, day),
            ).fetchone()
            if row and row[0] == tokens:
                continue
            c.execute(
                'INSERT OR REPLACE INTO account_token_buckets VALUES (?,?,?,?,?)',
                (route, day, tokens, checked, namespace),
            )
            changed = True
        if changed:
            self.bump_data_version(c)
        self.save_state(c, 'account_tokens_checked:' + route + ':' + namespace, checked)

    @staticmethod
    def account_namespace(c, route):
        from ..resources import account

        owner = account(c, route)
        return (owner['account_key'] or 'epoch:' + owner['epoch']) if owner else ''

    def account_gap(self, c, route, now, days=14):
        """Account total minus the locally recorded tokens of the same route, per UTC
        day. Computed when read, so it shrinks as local collection catches up. Only
        the plain route counts: codex:<provider> traffic is not on this account."""
        since = (datetime.fromtimestamp(now, timezone.utc) - timedelta(days=days - 1)).strftime('%Y-%m-%d')
        rows = c.execute(
            'SELECT day,tokens FROM account_token_buckets WHERE route=? AND namespace=? AND day>=? ORDER BY day',
            (route, self.account_namespace(c, route), since),
        ).fetchall()
        if not rows:
            return None
        from ..codex_usage import account_filter

        account_sql, account_args = (
            account_filter(self.account_namespace(c, route)) if route == 'codex' else ('1=1', ())
        )
        local = dict(
            c.execute(
                "SELECT date(ts,'unixepoch') AS day,SUM(uncached_input+cached_input+output+cache_creation) FROM usage_hourly"
                ' WHERE route=? AND ts>=? AND ' + account_sql + ' GROUP BY day',
                (route, stamp(since + 'T00:00:00Z'), *account_args),
            )
        )
        out = []
        for day, tokens in rows:
            mine = local.get(day, 0)
            out.append(dict(day=day, account=tokens, local=mine, gap=max(0, tokens - mine), over=max(0, mine - tokens)))
        return dict(
            route=route,
            days=out,
            total_gap=sum(d['gap'] for d in out),
            checked=self.state(c, 'account_tokens_checked:' + route + ':' + self.account_namespace(c, route)),
        )

    def bump_data_version(self, c):
        """Relabel/attribute writes keep every token sum, so they bump a version."""
        self.save_state(c, 'data_version', (self.state(c, 'data_version') or 0) + 1)

    def _usage_get(self, key):
        with self._usage_lock:
            cache = self._usage_cache
            if key in cache:
                cache.move_to_end(key)
            return cache.get(key)

    def _usage_put(self, key, body):
        with self._usage_lock:
            cache = self._usage_cache
            cache[key] = body
            while len(cache) > 12:
                cache.popitem(last=False)

    def usage(
        self,
        period='7d',
        start=None,
        end=None,
        granularity='day',
        group='provider',
        cumulative=False,
        now=None,
        pricing=None,
        subscriptions=None,
        compare=None,
        scope=None,
        value_alert_usd=None,
        sections='all',
        as_of=None,
        names=None,
        ended=None,
        price_history=None,
        origins=True,
        project_aliases=None,
    ):
        from ..aliases import raw_names_for, safe_aliases

        aliases = safe_aliases(project_aliases)
        if granularity not in BUCKET_SQL or group not in self.GROUPS or sections not in ('all', 'core', 'insights'):
            raise ValueError('집계 단위가 올바르지 않습니다.')
        scope_sql, scope_args = '', ()
        if scope:
            kind, _, value = scope.partition(':')
            if kind not in self.SCOPE_KINDS or not value:
                raise ValueError('범위가 올바르지 않습니다.')
            if kind == 'project' and aliases:
                raw_names = raw_names_for(value, aliases)
                if not raw_names:
                    scope_sql, scope_args = ' AND 1=0', ()
                else:
                    placeholders = ','.join('?' for _ in raw_names)
                    scope_sql = f' AND {self.SCOPE_KINDS[kind]} IN ({placeholders})'
                    scope_args = tuple(raw_names)
            else:
                scope_sql = ' AND ' + self.SCOPE_KINDS[kind] + '=?'
                scope_args = (value,)
        now = datetime.fromtimestamp(now or time.time(), KST)
        if as_of is not None and (
            period != '1h'
            or isinstance(as_of, bool)
            or not isinstance(as_of, (int, float))
            or not math.isfinite(as_of)
            or not 0 < as_of <= now.timestamp()
        ):
            raise ValueError('최근 1시간의 기준 시각이 올바르지 않습니다.')
        # Pin the core and lazy detail requests to one second. Time passing must
        # evict records from this rolling range even when the collector is idle.
        cutoff = int(as_of if as_of is not None else now.timestamp()) if period == '1h' else None
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        sums = ','.join(f'COALESCE(SUM({k}),0) AS {k}' for k in METRICS)
        count = 'COALESCE(SUM(requests),0) AS requests'
        from ..reconciliation import held

        # One read snapshot: the body, the overlay and the month all take the supplements
        # worked out once in it (or in an enclosing block's).
        with held(), self.connect() as c:
            c.execute('BEGIN')
            lifetime = dict(
                c.execute(
                    'SELECT ' + sums + ',' + self.rcount + ' FROM ' + self.table + ' WHERE 1=1' + scope_sql, scope_args
                ).fetchone()
            )
            collected_at = self.state(c, 'last_local')
            # Every term that changes the numbers or the response shape is in the
            # key: event writes move lifetime, collection passes move last_local,
            # sum-preserving relabels move data_version.
            key = (
                period,
                start,
                end,
                granularity,
                group,
                cumulative,
                compare,
                scope,
                sections,
                value_alert_usd,
                tuple(lifetime.values()),
                collected_at,
                self.state(c, 'data_version') or 0,
                midnight.date().isoformat(),
                cutoff,
                json.dumps(pricing, sort_keys=True) if pricing else None,
                json.dumps(subscriptions, sort_keys=True) if subscriptions else None,
                tuple(sorted(ended or ())),
                json.dumps(price_history, sort_keys=True) if price_history else None,
                json.dumps(aliases, sort_keys=True) if aliases else None,
            )
            body = self._usage_get(key)
            if body is None:
                body = self._usage_body(
                    c,
                    period,
                    start,
                    end,
                    granularity,
                    group,
                    cumulative,
                    now,
                    midnight,
                    pricing,
                    subscriptions,
                    compare,
                    scope,
                    scope_sql,
                    scope_args,
                    value_alert_usd,
                    sections,
                    sums,
                    count,
                    lifetime,
                    cutoff,
                    set(ended or ()),
                    price_history,
                    aliases,
                )
                if len(body['series']) <= 5000:
                    self._usage_put(key, body)
            body = {
                **body,
                'prior': self._prior(c, body, period, compare, now, pricing, scope_sql, scope_args, sums, count)
                if sections != 'insights'
                else None,
            }
            from ..reconciliation import apply as reconcile_usage

            # `origins`: the usage tab's split of the records by origin. A caller that only
            # takes figures from the response (the overview, reports) leaves it out.
            body = reconcile_usage(
                c,
                body,
                group,
                cumulative,
                scope,
                pricing=pricing,
                calendar_start=(midnight - timedelta(days=181)).date().isoformat(),
                origins=origins,
                project_aliases=aliases,
            )
            sources, unavailable_routes = self._usage_live(c, body['rows'], now)
            from ..claude_accounts import describe

            claude = describe(c, names)
            from ..signals import usage_spike

            spike = usage_spike(c, now.timestamp()) if sections != 'insights' else None
            month = self.project_month(c, now, pricing, project_aliases=aliases) if sections != 'insights' else None
            gap = self.account_gap(c, 'codex', now.timestamp()) if sections != 'insights' else None
        data = {
            **body,
            'sources': sources,
            'unavailable_routes': unavailable_routes,
            'collected_at': collected_at,
            'spike': spike,
            'project_month': month,
            'account_gap': gap,
            'claude_accounts': claude,
        }
        if sections == 'all':
            return data
        insight_keys = {'insights', 'sessions', 'sessions_total', 'sessionless_requests', 'projects', 'calendar'}
        if sections == 'core':
            return {k: v for k, v in data.items() if k not in insight_keys}
        shared = {
            'reconciliation',
            'period',
            'start',
            'end_exclusive',
            'timezone',
            'granularity',
            'labels',
            'series',
            'subscriptions',
            'sources',
            'scope',
            'collected_at',
        }
        return {k: v for k, v in data.items() if k in insight_keys | shared}

    def project_month(self, c, now, pricing=None, project_aliases=None):
        """Month-to-date (KST calendar month) tokens and estimated cost per project."""
        from .. import store_ops

        return store_ops.project_month(self, c, now, pricing=pricing, project_aliases=project_aliases)

    def project_detail(self, project, now=None, pricing=None, days=30, budget=None, project_aliases=None):
        """One project over the last `days` KST days."""
        from .. import store_ops

        return store_ops.project_detail(
            self,
            project,
            now or time.time(),
            pricing=pricing,
            days=days,
            budget=budget,
            project_aliases=project_aliases,
        )

    @classmethod
    def session_rows(cls, c, start, end, pricing=None, scope=None, ids=None, project_aliases=None):
        """Request-only session rows in the selected range, optionally for a bounded ID batch."""
        from .. import store_ops

        return store_ops.session_rows(
            cls, c, start, end, pricing=pricing, scope=scope, ids=ids, project_aliases=project_aliases
        )

    def session_detail(self, session, pricing=None):
        """One session's models, hourly activity and cache share; no message content exists here."""
        from .. import store_ops

        return store_ops.session_detail(self, session, pricing=pricing)

    def _price_segments(self, c, R, route, parts, pricing, scope_sql, scope_args):
        """Each price span's value of one route's records. Rollup rows are hourly, so an
        hour holding a price change is counted in the span it begins in."""
        from ..pricing import estimate, rate_for
        from ..insights import token_total

        cases = ' '.join(f'WHEN ts<{float(hi)!r} THEN {i}' for i, (_, hi, _) in enumerate(parts[:-1]))
        sums = ','.join(f'COALESCE(SUM({k}),0) AS {k}' for k in METRICS)
        result = [
            dict(
                start=datetime.fromtimestamp(lo, KST).isoformat(),
                end=datetime.fromtimestamp(hi, KST).isoformat(),
                days=(hi - lo) / 86400,
                monthly_usd=usd,
                cost=None,
                tokens=0,
            )
            for lo, hi, usd in parts
        ]
        where, route_args = route_events(route)
        for r in c.execute(
            f"SELECT CASE {cases} ELSE {len(parts) - 1} END AS part,model,date(ts,'unixepoch','+9 hours') AS day,"
            + sums
            + ' FROM '
            + R
            + ' WHERE '
            + where
            + ' AND ts>=? AND ts<?'
            + scope_sql
            + ' GROUP BY 1,2,3',
            (*route_args, parts[0][0], parts[-1][1], *scope_args),
        ):
            e = result[r['part']]
            e['tokens'] += token_total(dict(r))
            rate = rate_for(r['model'], pricing, r['day']) if pricing else None
            if rate:
                e['cost'] = (e['cost'] or 0) + estimate(dict(r), rate)
        return result

    def _prior(self, c, body, period, compare, now, pricing, scope_sql, scope_args, sums, count):
        """The card comparison: the window before (or the chosen comparison) cut at the same
        elapsed time. Worked out on every request, not cached with the body, so the cut
        follows the clock even when no collection pass changes the cache key."""
        from ..insights import token_total
        from ..pricing import estimate, rate_for

        if period == 'all':
            return None
        a, b = datetime.fromisoformat(body['start']), datetime.fromisoformat(body['end_exclusive'])
        total, first = body['totals'], body['first_record']
        mode = compare if compare in self.COMPARE_LABELS else 'previous'
        shift = self.COMPARE_SHIFTS.get(mode)
        a2 = a - timedelta(days=shift) if shift else a - (b - a)
        effective = min(b, now)
        if effective <= a:
            return None
        stop = a2 + (effective - a)
        prior = dict(
            mode=mode,
            label=self.COMPARE_LABELS[mode],
            start=a2.isoformat(),
            end_exclusive=stop.isoformat(),
            current_end=effective.isoformat(),
            running=effective < b,
            complete=True,
            current_tokens=token_total(total),
            requests=0,
            cost=None,
            **dict.fromkeys(METRICS, 0),
        )
        spent = None
        for r in c.execute(
            'SELECT model,date(ts,\'unixepoch\',\'+9 hours\') AS day,'
            + sums
            + ','
            + count
            + ' FROM events WHERE ts>=? AND ts<?'
            + scope_sql
            + ' GROUP BY model,day',
            (a2.timestamp(), stop.timestamp(), *scope_args),
        ):
            for k in METRICS:
                prior[k] += r[k]
            prior['requests'] += r['requests']
            rate = rate_for(r['model'], pricing, r['day']) if pricing else None
            if rate:
                spent = (spent or 0) + estimate(dict(r), rate)
        if spent is not None:
            prior['cost'] = round(spent, 4)
        # Local records take part only when either side has some; then they must
        # reach back to the earlier window (reconciliation checks its sources too).
        if (token_total(prior) or token_total(total)) and not (first is not None and first <= a2.timestamp()):
            prior['complete'] = False
        return prior

    def _usage_body(
        self,
        c,
        period,
        start,
        end,
        granularity,
        group,
        cumulative,
        now,
        midnight,
        pricing,
        subscriptions,
        compare,
        scope,
        scope_sql,
        scope_args,
        value_alert_usd,
        sections,
        sums,
        count,
        lifetime,
        cutoff,
        ended=frozenset(),
        price_history=None,
        project_aliases=None,
    ):
        from ..aliases import resolve_project

        first = c.execute('SELECT MIN(ts) FROM events WHERE 1=1' + scope_sql, scope_args).fetchone()[0]
        if period == 'all':
            from ..reconciliation import supplements, matches

            extra, _ = supplements(c)
            starts = [e['start'] for e in extra if matches(e, scope, project_aliases)]
            if starts:
                first = min([first] + starts) if first is not None else min(starts)
        if period == '1h':
            b = datetime.fromtimestamp(cutoff, KST)
            a = b - timedelta(hours=1)
        elif period == 'custom':
            a = datetime.strptime(start, '%Y-%m-%d').replace(tzinfo=KST)
            b = datetime.strptime(end, '%Y-%m-%d').replace(tzinfo=KST) + timedelta(days=1)
        elif period in ('today', '7d', '30d', 'all'):
            a = (
                datetime.fromtimestamp(first, KST).replace(hour=0, minute=0, second=0, microsecond=0)
                if first and period == 'all'
                else midnight - timedelta(days={'today': 0, '7d': 6, '30d': 29, 'all': 0}[period])
            )
            b = midnight + timedelta(days=1)
        else:
            raise ValueError('기간이 올바르지 않습니다.')
        intervals = calendar_buckets(a, b, granularity)
        # A rolling hour cuts through two hourly rollups. Read original events
        # at both boundaries so charts, models, sessions and totals agree.
        R, rcount = ('events', count) if period == '1h' else (self.table, self.rcount)
        total = dict(
            c.execute(
                'SELECT ' + sums + ',' + rcount + ' FROM ' + R + ' WHERE ts>=? AND ts<?' + scope_sql,
                (a.timestamp(), b.timestamp(), *scope_args),
            ).fetchone()
        )
        rows = [
            dict(r)
            for r in c.execute(
                'SELECT provider,route,model,'
                + rcount
                + ','
                + sums
                + ' FROM '
                + R
                + ' WHERE ts>=? AND ts<?'
                + scope_sql
                + ' GROUP BY provider,route,model ORDER BY SUM(uncached_input+cached_input+output+cache_creation) DESC,provider,route,model',
                (a.timestamp(), b.timestamp(), *scope_args),
            )
        ]
        from ..pricing import estimate, rate_for

        # Chart-only aggregates are dropped from insights responses; skip the scan.
        heatmap = []
        if sections != 'insights':
            cells = {}
            for r in c.execute(
                '''SELECT CAST(strftime('%w',ts,'unixepoch','+9 hours') AS INTEGER) AS dow,
                CAST(strftime('%H',ts,'unixepoch','+9 hours') AS INTEGER) AS hour,
                model,date(ts,'unixepoch','+9 hours') AS day,'''
                + sums
                + ','
                + rcount
                + '''
                FROM '''
                + R
                + ''' WHERE ts>=? AND ts<?'''
                + scope_sql
                + ' GROUP BY dow,hour,model,day',
                (a.timestamp(), b.timestamp(), *scope_args),
            ):
                e = cells.setdefault(
                    (r['dow'], r['hour']),
                    dict(dow=r['dow'], hour=r['hour'], tokens=0, requests=0, cost=0.0, rated=False),
                )
                e['tokens'] += r['uncached_input'] + r['cached_input'] + r['output'] + r['cache_creation']
                e['requests'] += r['requests']
                if pricing:
                    rate = rate_for(r['model'], pricing, r['day'])
                    if rate:
                        e['cost'] += estimate(dict(r), rate)
                        e['rated'] = True
            heatmap = [
                dict(
                    dow=e['dow'],
                    hour=e['hour'],
                    tokens=e['tokens'],
                    requests=e['requests'],
                    cost=round(e['cost'], 4) if e['rated'] else None,
                )
                for _, e in sorted(cells.items())
            ]
        # Calendar weeks start Monday; calendar months are not fixed day counts.
        # Model-level rows let each bucket also carry request counts and a
        # cost estimate (rates are keyed by model, so group by model first).
        from ..pricing import estimate, rate_for

        label_expr = self.GROUPS[group]
        buckets = {}
        from ..insights import token_total

        for r in c.execute(
            'SELECT '
            + BUCKET_SQL[granularity]
            + ' AS bucket,'
            + label_expr
            + ' AS label,model,date(ts,\'unixepoch\',\'+9 hours\') AS day,'
            + sums
            + ','
            + rcount
            + ' FROM '
            + R
            + ' WHERE ts>=? AND ts<?'
            + scope_sql
            + ' GROUP BY bucket,label,model,day',
            (a.timestamp(), b.timestamp(), *scope_args),
        ):
            blabel = (
                resolve_project(r['label'], project_aliases) if group == 'project' and project_aliases else r['label']
            )
            e = buckets.setdefault(
                (r['bucket'], blabel), dict(requests=0, cost=0.0, unpriced=0, **dict.fromkeys(METRICS, 0))
            )
            for k in METRICS:
                e[k] += r[k]
            e['requests'] += r['requests']
            if pricing:
                rate = rate_for(r['model'], pricing, r['day'])
                # Tokens without a rate are counted so the cost view can say "미산정", not $0.
                if rate:
                    e['cost'] += estimate(dict(r), rate)
                else:
                    e['unpriced'] += token_total(dict(r))
        buckets = [dict(bucket=k[0], label=k[1], **v) for k, v in buckets.items()]
        compare_data = None
        if sections != 'insights' and compare in ('previous', 'week', 'month'):
            shift = self.COMPARE_SHIFTS.get(compare)
            a2, b2 = (a - timedelta(days=shift), b - timedelta(days=shift)) if shift else (a - (b - a), a)
            # Bucket shifted timestamps onto the current window's grid so
            # the ghost series always matches the main series 1:1 — weekly
            # and monthly buckets otherwise drift in count and alignment.
            delta = int((a - a2).total_seconds())
            shifted_sql = re.sub(r'\bts\b', f'(ts+{delta})', BUCKET_SQL[granularity])
            cmp_buckets = {}
            for r in c.execute(
                'SELECT '
                + shifted_sql
                + ' AS bucket,'
                + label_expr
                + ' AS label,model,date(ts,\'unixepoch\',\'+9 hours\') AS day,'
                + sums
                + ','
                + rcount
                + ' FROM '
                + R
                + ' WHERE ts>=? AND ts<?'
                + scope_sql
                + ' GROUP BY bucket,label,model,day',
                (a2.timestamp(), b2.timestamp(), *scope_args),
            ):
                clabel = (
                    resolve_project(r['label'], project_aliases)
                    if group == 'project' and project_aliases
                    else r['label']
                )
                e = cmp_buckets.setdefault(
                    (r['bucket'], clabel), dict(requests=0, cost=0.0, **dict.fromkeys(METRICS, 0))
                )
                for k in METRICS:
                    e[k] += r[k]
                e['requests'] += r['requests']
                if pricing:
                    rate = rate_for(r['model'], pricing, r['day'])
                    if rate:
                        e['cost'] += estimate(dict(r), rate)
            cmp_series = []
            cmp_labels = {r['label'] for r in buckets} | {k[1] for k in cmp_buckets}
            for begin, _finish in calendar_buckets(a, b, granularity):
                t = int(begin.timestamp())
                cmp_series.append(
                    dict(
                        time=begin.isoformat(),
                        compared_time=(begin - timedelta(seconds=delta)).isoformat(),
                        values={label: cmp_buckets.get((t, label), {}) for label in cmp_labels},
                    )
                )
            # The running period has only reached now; compare its total with the same
            # elapsed span of the earlier window, not the whole of it.
            effective = min(b, now)
            elapsed = None
            if effective > a:
                stop = a2 + (effective - a)
                elapsed = dict(
                    c.execute(
                        'SELECT ' + sums + ',' + count + ' FROM events WHERE ts>=? AND ts<?' + scope_sql,
                        (a2.timestamp(), stop.timestamp(), *scope_args),
                    ).fetchone()
                )
                elapsed.update(start=a2.isoformat(), end_exclusive=stop.isoformat())
            compare_data = dict(
                mode=compare,
                label=self.COMPARE_LABELS[compare],
                start=a2.isoformat(),
                end_exclusive=b2.isoformat(),
                series=cmp_series,
                elapsed=elapsed,
            )
        # The period cards always compare with the window before (or the chosen comparison)
        # cut at the same elapsed time. A window that starts before the first record is
        # not complete, so no change is computed from it.
        session_list = []
        sessionless = 0
        project_list = []
        sessions_total = 0
        if sections != 'core':
            sessions = self.session_rows(
                c, a.timestamp(), b.timestamp(), pricing, scope, project_aliases=project_aliases
            )
            sessions_total = len(sessions)
            session_list = sorted(
                sessions.values(),
                key=lambda s: -(s['uncached_input'] + s['cached_input'] + s['output'] + s['cache_creation']),
            )[:100]
            sessionless = c.execute(
                'SELECT COALESCE(SUM(requests),0) FROM events WHERE ts>=? AND ts<? AND session IS NULL' + scope_sql,
                (a.timestamp(), b.timestamp(), *scope_args),
            ).fetchone()[0]
            projects = {}
            if project_aliases:
                session_sets = {}
                for raw_proj, sid in c.execute(
                    "SELECT COALESCE(NULLIF(project,''),'미분류'), session FROM events "
                    'WHERE ts>=? AND ts<? AND session IS NOT NULL' + scope_sql + ' GROUP BY 1, 2',
                    (a.timestamp(), b.timestamp(), *scope_args),
                ):
                    session_sets.setdefault(resolve_project(raw_proj, project_aliases), set()).add(sid)
                project_sessions = {k: len(v) for k, v in session_sets.items()}
            else:
                project_sessions = dict(
                    c.execute(
                        "SELECT COALESCE(NULLIF(project,''),'미분류'),COUNT(DISTINCT session) FROM events WHERE ts>=? AND ts<?"
                        + scope_sql
                        + " GROUP BY COALESCE(NULLIF(project,''),'미분류')",
                        (a.timestamp(), b.timestamp(), *scope_args),
                    )
                )
            for r in c.execute(
                '''SELECT project,model,date(ts,'unixepoch','+9 hours') AS day,MAX(ts) AS last_ts,'''
                + count
                + ','
                + sums
                + '''
                FROM events WHERE ts>=? AND ts<?'''
                + scope_sql
                + ' GROUP BY project,model,day'
                '',
                (a.timestamp(), b.timestamp(), *scope_args),
            ):
                label = resolve_project(r['project'], project_aliases)
                e = projects.setdefault(
                    label,
                    dict(
                        project=label,
                        sessions=project_sessions.get(label, 0),
                        last_ts=r['last_ts'],
                        requests=0,
                        cost=0.0,
                        rated=False,
                        **dict.fromkeys(METRICS, 0),
                    ),
                )
                e['last_ts'] = max(e['last_ts'], r['last_ts'])
                e['requests'] += r['requests']
                for k in METRICS:
                    e[k] += r[k]
                if pricing:
                    rate = rate_for(r['model'], pricing, r['day'])
                    if rate:
                        e['cost'] += estimate(dict(r), rate)
                        e['rated'] = True
            project_list = sorted(
                projects.values(),
                key=lambda p: -(p['uncached_input'] + p['cached_input'] + p['output'] + p['cache_creation']),
            )
            for p in project_list:
                p['cost'] = round(p['cost'], 2) if p.pop('rated') else None
        calendar, lifetime_cost, lifetime_priced, month = self._period_free(
            c, lifetime, midnight, pricing, sums, scope_sql, scope_args
        )
        if month is not None:
            month['alert_usd'] = value_alert_usd
        scope_options = {}
        if sections != 'insights':
            scope_options = {
                kind: [
                    r[0]
                    for r in c.execute(
                        'SELECT '
                        + expr
                        + ' AS v,'
                        + rcount.replace(' AS requests', '')
                        + ' AS n FROM '
                        + R
                        + ' GROUP BY v ORDER BY n DESC,v LIMIT 50'
                    )
                ]
                for kind, expr in self.SCOPE_KINDS.items()
            }
            if project_aliases and 'project' in scope_options:
                scope_options['project'] = list(
                    dict.fromkeys(resolve_project(v, project_aliases) for v in scope_options['project'])
                )
            scope_options['account'] = [v for v in scope_options['account'] if is_claude(v)]
            if len(scope_options['account']) < 2:
                scope_options.pop('account')
        from ..insights import usage_insights, token_total

        insights = None
        if sections != 'core':
            insights = usage_insights(c, a, b, now, period, first, total, rows, buckets, scope_sql, scope_args)
            insights['month'] = month
        cost = {
            'period': None,
            'lifetime': None,
            'coverage': None,
            'lifetime_coverage': None,
            'cache_savings': None,
            'cache_estimated_tokens': 0,
            'by_type': None,
        }
        if pricing:
            from ..pricing import COST_PARTS, cache_estimated_tokens, estimate_parts

            priced = 0
            savings = 0.0
            by_type = dict.fromkeys(COST_PARTS, 0.0)
            # Rows are (provider, route, model): a model used through two routes
            # must not show the other route's spend on each row.
            cost_by_row = {}
            for r in c.execute(
                'SELECT provider,route,model,date(ts,\'unixepoch\',\'+9 hours\') AS day,'
                + sums
                + ' FROM '
                + R
                + ' WHERE ts>=? AND ts<?'
                + scope_sql
                + ' GROUP BY provider,route,model,day',
                (a.timestamp(), b.timestamp(), *scope_args),
            ):
                rate = rate_for(r['model'], pricing, r['day'])
                if rate:
                    priced += token_total(dict(r))
                    cost['cache_estimated_tokens'] += cache_estimated_tokens(dict(r), rate)
                    key = (r['provider'], r['route'], r['model'])
                    cost_by_row[key] = cost_by_row.get(key, 0) + estimate(dict(r), rate)
                    for part, value in estimate_parts(dict(r), rate).items():
                        by_type[part] += value
                    # What the cache reads would have cost as uncached input at the same list rate.
                    savings += r['cached_input'] * (rate['input'] - rate.get('cached', rate['input'])) / 1e6
            for row in rows:
                row['est_cost'] = cost_by_row.get((row['provider'], row['route'], row['model']))
            if priced:
                cost['period'] = sum(cost_by_row.values())
                cost['cache_savings'] = round(savings, 4)
                # Where the estimate comes from: the token mix and the cost mix differ widely.
                cost['by_type'] = {part: round(value, 4) for part, value in by_type.items()}
            if token_total(total):
                cost['coverage'] = priced / token_total(total) * 100
            if lifetime_priced:
                cost['lifetime'] = lifetime_cost
                if token_total(lifetime):
                    cost['lifetime_coverage'] = lifetime_priced / token_total(lifetime) * 100
        # The same use by reasoning effort. A token costs the same at any effort; what differs
        # is how many a request uses, so each level reports its volume per request.
        efforts = {}
        # The insights sections do not show it: their request skips the scan.
        for r in (
            c.execute(
                'SELECT route,model,effort,date(ts,\'unixepoch\',\'+9 hours\') AS day,'
                + sums
                + ','
                + rcount
                + ' FROM '
                + R
                + ' WHERE ts>=? AND ts<?'
                + scope_sql
                + ' GROUP BY route,model,effort,day',
                (a.timestamp(), b.timestamp(), *scope_args),
            )
            if sections != 'insights'
            else ()
        ):
            row = dict(r)
            name = _EFFORT_SUFFIX.sub('', row['model']) if row['route'] in NAMED_EFFORT_ROUTES else row['model']
            e = efforts.setdefault(
                (row['route'], name, row['effort']),
                dict(
                    route=row['route'],
                    model=name,
                    effort=row['effort'],
                    requests=0,
                    tokens=0,
                    output=0,
                    reasoning=0,
                    reasoning_observed=0,
                    cost=None,
                ),
            )
            e['requests'] += row['requests']
            e['tokens'] += token_total(row)
            for k in ('output', 'reasoning', 'reasoning_observed'):
                e[k] += row[k]
            rate = rate_for(row['model'], pricing, row['day']) if pricing else None
            if rate:
                e['cost'] = (e['cost'] or 0) + estimate(row, rate)
        efforts = sorted(efforts.values(), key=lambda e: (-e['tokens'], e['route'], e['model'], e['effort'] or ''))
        subs = []
        claude_routes = [r[0] for r in c.execute('SELECT route FROM claude_accounts')]
        # A sole identified non-default home still has its own subscription route.
        separate_claude = any(route != 'claude-code' for route in claude_routes)
        # Several Claude accounts: the Claude Code total split by account, 계정 미확인
        # included, so the parts add up to the service (reconciliation adds supplements).
        claude_split = None
        if separate_claude and period != '1h':
            split = {}
            for r in c.execute(
                'SELECT '
                + self.ACCOUNT_SQL
                + ' AS account,model,date(ts,\'unixepoch\',\'+9 hours\') AS day,'
                + sums
                + ','
                + rcount
                + ' FROM '
                + R
                + " WHERE route='claude-code' AND ts>=? AND ts<?"
                + scope_sql
                + ' GROUP BY 1,2,3',
                (a.timestamp(), b.timestamp(), *scope_args),
            ):
                e = split.setdefault(r['account'], dict(account=r['account'], tokens=0, requests=0, cost=None))
                e['tokens'] += token_total(dict(r))
                e['requests'] += r['requests']
                rate = rate_for(r['model'], pricing, r['day']) if pricing else None
                if rate:
                    e['cost'] = (e['cost'] or 0) + estimate(dict(r), rate)
            claude_split = sorted(split.values(), key=lambda e: (-e['tokens'], e['account']))
        if (subscriptions or separate_claude) and period != '1h':
            by_route = {}
            tokens_by_route = {}
            for row in rows:
                tokens_by_route[row['route']] = tokens_by_route.get(row['route'], 0) + token_total(row)
                if row.get('est_cost') is not None:
                    by_route[row['route']] = by_route.get(row['route'], 0) + row['est_cost']
            if claude_split is not None:
                # Each Claude subscription is valued with its own account's requests.
                by_route = {k: v for k, v in by_route.items() if k != 'claude-code'}
                tokens_by_route.pop('claude-code', None)
                for e in claude_split:
                    tokens_by_route[e['account']] = e['tokens']
                    if e['cost'] is not None:
                        by_route[e['account']] = e['cost']
            # Scale by the elapsed part of the period: a running week is not seven days yet.
            days = max(1, (min(b, now) - a).total_seconds() / 86400)
            prices = dict(subscriptions or {})
            # Several Claude accounts: list each one, priced or not, so a missing price is visible.
            if separate_claude:
                for route in claude_routes:
                    if route not in ended:
                        prices.setdefault(route, None)
            from ..subscriptions import MONTH_DAYS, clean_history, segments, weighted

            history = clean_history(price_history)
            lo, hi = a.timestamp(), min(b, now).timestamp()
            for route, monthly in prices.items():
                if monthly is not None and (not isinstance(monthly, (int, float)) or monthly < 0):
                    continue
                est = by_route.get(route)
                # The value is set against the prices of the same span: a price changed
                # later does not re-value earlier use.
                parts = segments(monthly, history.get(route), lo, hi) if monthly is not None else []
                # No tokens in the period is not the same as tokens without a list price.
                subs.append(
                    dict(
                        route=route,
                        monthly_usd=monthly,
                        period_cost=est,
                        period_tokens=tokens_by_route.get(route, 0),
                        monthly_equiv=est * MONTH_DAYS / days if est is not None else None,
                        effective_usd=weighted(parts) if parts else monthly,
                        price_segments=self._price_segments(c, R, route, parts, pricing, scope_sql, scope_args)
                        if len(parts) > 1
                        else None,
                    )
                )
        labels = sorted({r['label'] for r in buckets})
        lookup = {(r['bucket'], r['label']): r for r in buckets}
        running = {label: dict(requests=0, cost=0.0, unpriced=0, **dict.fromkeys(METRICS, 0)) for label in labels}
        series = []
        for begin, finish in intervals:
            t = int(begin.timestamp())
            values = {}
            for label in labels:
                entry = lookup.get((t, label), {})
                for key in SERIES_KEYS:
                    running[label][key] += entry.get(key, 0)
                values[label] = dict(running[label]) if cumulative else {k: entry.get(k, 0) for k in SERIES_KEYS}
            series.append(
                dict(
                    time=begin.isoformat(),
                    values=values,
                    range_start=max(a, begin).isoformat(),
                    range_end_exclusive=min(b, finish).isoformat(),
                    partial=begin < a or finish > b,
                )
            )
        return dict(
            period=period,
            start=a.isoformat(),
            end_exclusive=b.isoformat(),
            timezone='Asia/Seoul',
            totals=total,
            lifetime=lifetime,
            granularity=granularity,
            heatmap=heatmap,
            cost=cost,
            rows=rows,
            series=series,
            labels=labels,
            first_record=first,
            insights=insights,
            efforts=efforts,
            calendar=calendar,
            subscriptions=subs,
            claude_split=claude_split,
            sessions=session_list,
            sessions_total=sessions_total,
            sessionless_requests=sessionless,
            projects=project_list,
            compare=compare_data,
            scope=scope,
            scope_options=scope_options,
            coverage='로컬·클라우드 요청 기록과 서버 누계를 중복 없이 대조합니다. KST 기준으로 배분 가능한 토큰은 차트에 포함하고, 일자를 확정할 수 없는 계정·세션 누계는 미배분으로 표시합니다. 계정 차이를 dot 사용량으로 단정하지 않습니다. 다른 PC의 사용량은 미수집입니다. Antigravity는 로컬 대화 DB, Devin은 로컬 sessions.db의 요청별 사용량을 집계합니다. 삭제된 기록과 원본 모델명이 없는 요청은 완전히 복구할 수 없습니다. 직접 API 과금은 분석하지 않습니다.',
            token_note='출력은 추론을 포함하며, 추론 미제공과 실제 0을 수신 건수로 구분합니다. 캐시 생성의 5분·1시간·미확인 값은 생성 합계의 세부 항목입니다. Advisor 토큰은 포함하고 내부 호출 수는 API 요청 수와 따로 표시합니다. 보존 시간이나 해당 단가가 없으면 캐시 생성 기본 단가로 추정합니다.',
        )

    def storage_row(self, checked=None):
        """Database, WAL and backup sizes, so growth is visible before it is a problem."""
        from .. import store_ops

        return store_ops.storage_row(self, checked=checked)

    def _usage_live(self, c, rows, now):
        """Source freshness and availability change with wall time, so they are
        computed on every call instead of being cached with the body."""
        from .. import store_ops

        return store_ops.usage_live(self, c, rows, now)
