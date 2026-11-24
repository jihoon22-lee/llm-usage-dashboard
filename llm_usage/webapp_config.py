"""Configuration, pricing, budget and notification routes for the web app."""

import fcntl
import json
import os
import time
from datetime import datetime
from pathlib import Path

from flask import abort, jsonify

from .config import ended_routes
from .pricing import edited_rates, load_pricing
from .store import KST, Store


def _today():
    return datetime.now(KST).strftime('%Y-%m-%d')


def _number(value, lo, hi):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and lo <= value <= hi


def register_config_routes(app, config, store, persist, json_object):
    """Attach /api/config* and /api/notify/test routes to `app`."""
    from . import webapp as _webapp

    # ~/.config is read-only under ProtectHome; reads may fall back to it but
    # writes always go to the data directory (ReadWritePaths).
    def pricing_write_path():
        if not config.get('config_file'):
            return None
        return Path(config['database']).parent / 'pricing.json'

    @app.get('/api/config')
    def get_config():
        from .claude_accounts import describe
        from .notify import masked
        from .pricing import BUILTIN, entry_key, pricing_origins, rate_for, upcoming_for
        from .store import SUPPORTED_ROUTES
        from .subscriptions import clean_history

        pricing = load_pricing(config)
        today = _today()
        since = time.time() - 30 * 86400
        since_ts = int(since // 3600) * 3600 if store.table == 'usage_hourly' else since
        with store.connect() as c:
            c.execute('BEGIN')
            providers = {
                r[0]: r[1]
                for r in c.execute(f'SELECT model,MAX(provider) FROM {store.table} GROUP BY model ORDER BY model')
            }
            models = list(providers)
            routes = set(SUPPORTED_ROUTES) | set(config.get('subscription_prices') or {})
            routes.update(r[0] for r in c.execute(f'SELECT DISTINCT route FROM {store.table}'))
            claude = describe(c, config.get('claude_account_names'), ended_routes(config))
            # Several Claude accounts: each is its own subscription row.
            routes.update(a['route'] for a in claude)
            usage30 = {
                r[0]: r[1]
                for r in c.execute(
                    f'SELECT model,SUM(uncached_input+cached_input+output+cache_creation) FROM {store.table} WHERE ts>=? GROUP BY model',
                    (since_ts,),
                )
            }
        upcoming = {m: e for m in {*models, *pricing} if (e := upcoming_for(m, pricing, today))}
        return jsonify(
            subscription_routes=sorted(routes),
            subscription_prices=config.get('subscription_prices') or {},
            subscription_price_history=clean_history(config.get('subscription_price_history')),
            ended_subscriptions=config.get('ended_subscriptions') or {},
            claude_accounts=claude,
            claude_account_names=config.get('claude_account_names') or {},
            notify=masked(config.get('notify')),
            residual_label=config.get('residual_label'),
            project_budgets=config.get('project_budgets') or {},
            model_usage_30d=usage30,
            thresholds=store.thresholds,
            pricing={m: r for m, r in pricing.items() if r is not None},
            refresh_seconds=config.get('refresh_seconds', 300),
            value_alert_usd=config.get('value_alert_usd'),
            upcoming=upcoming,
            today=today,
            pricing_origins=pricing_origins(config),
            pricing_builtin=sorted(BUILTIN),
            models=[
                dict(model=m, provider=providers[m], rate=rate_for(m, pricing, today), key=entry_key(m, pricing))
                for m in models
            ],
        )

    @app.post('/api/config/subscription-prices')
    def set_prices():
        body = json_object()
        prices = body.get('prices')
        if not isinstance(prices, dict) or len(prices) > 64:
            abort(400, description='구독료 목록을 확인하세요.')
        clean = {}
        for route, value in prices.items():
            if not isinstance(route, str) or not _webapp.ROUTE_RE.fullmatch(route):
                abort(400, description='경로 이름을 확인하세요.')
            if not _number(value, 0, 100000):
                abort(400, description='구독료는 0~100000 범위의 숫자여야 합니다.')
            clean[route] = value
        # When a changed price applies: from now (a plan change, the default), from a
        # given moment, as a correction of the current price, or for all time.
        from .subscriptions import MODES, change as price_change, clean_history

        mode = body.get('effective', 'now')
        at = body.get('effective_at')
        if mode not in MODES:
            abort(400, description='적용 시점을 확인하세요.')
        now = time.time()
        if mode != 'at':
            at = None
        elif not _number(at, 1, now + 60):
            abort(400, description='적용 시각은 지금 이전이어야 합니다.')

        def prices_with_history(current):
            before = current.get('subscription_prices') or {}
            history = clean_history(current.get('subscription_price_history'))
            after = {
                route: price_change(
                    history.get(route), before.get(route), clean.get(route), mode, min(at or now, now), now
                )
                for route in set(history) | set(clean)
            }
            return {
                'subscription_prices': clean,
                'subscription_price_history': {r: e for r, e in after.items() if e},
                **extra,
            }

        extra = {}
        names = body.get('names')
        if names is not None:
            # Display names of Claude accounts; emails are never stored or shown.
            if (
                not isinstance(names, dict)
                or len(names) > 16
                or not all(
                    isinstance(r, str)
                    and _webapp.ROUTE_RE.fullmatch(r)
                    and r.startswith('claude-code')
                    and isinstance(v, str)
                    and len(v.strip()) <= 40
                    and not any(ord(ch) < 32 or ord(ch) == 127 for ch in v)
                    for r, v in names.items()
                )
            ):
                abort(400, description='계정 이름은 40자 이하로 입력하세요.')
            extra['claude_account_names'] = {r: v.strip() for r, v in names.items() if v.strip()}
        label = body.get('residual_label')
        if label is not None:
            # The name of the Codex account residual (e.g. '자동 리뷰'); empty restores the default.
            if (
                not isinstance(label, str)
                or len(label.strip()) > 20
                or any(ord(ch) < 32 or ord(ch) == 127 for ch in label)
            ):
                abort(400, description='계정 합계 차이 이름은 20자 이하로 입력하세요.')
            extra['residual_label'] = label.strip() or None
        ended = body.get('ended')
        if ended is not None:
            # Omitted keeps the stored state; a list replaces it and keeps the original dates.
            if (
                not isinstance(ended, list)
                or len(ended) > 64
                or not all(isinstance(r, str) and _webapp.ROUTE_RE.fullmatch(r) for r in ended)
            ):
                abort(400, description='구독 종료 목록을 확인하세요.')

            def change(current):
                before = current.get('ended_subscriptions') or {}
                return {
                    **prices_with_history(current),
                    'ended_subscriptions': {r: before.get(r) or {'since': time.time()} for r in dict.fromkeys(ended)},
                }

            saved = persist(change)
        else:
            saved = persist(prices_with_history)
        return jsonify(
            subscription_prices=clean,
            subscription_price_history=saved.get('subscription_price_history') or {},
            ended_subscriptions=saved.get('ended_subscriptions') or {},
            claude_account_names=saved.get('claude_account_names') or {},
            residual_label=saved.get('residual_label'),
        )

    @app.post('/api/config/pricing')
    def set_pricing():
        body = json_object()
        model = body.get('model')
        if not isinstance(model, str) or not _webapp.MODEL_RE.fullmatch(model):
            abort(400, description='모델 이름을 확인하세요.')
        rates = {}
        cleared = {
            key
            for key in ('cached', 'cache_write', 'cache_write_5m', 'cache_write_1h')
            if key in body and body[key] is None
        }
        for key in ('input', 'cached', 'output', 'cache_write', 'cache_write_5m', 'cache_write_1h'):
            if body.get(key) is None:
                continue
            if not _number(body[key], 0, 10000):
                abort(400, description='단가는 0~10000 범위의 숫자여야 합니다.')
            rates[key] = body[key]
        if not rates and not cleared and not body.get('delete') and not body.get('hide') and not body.get('unhide'):
            abort(400, description='단가를 입력하세요.')
        from .pricing import BUILTIN, _read_json, _valid, entry_key

        # 'delete' removes the user value and falls back to the builtin rate
        # (or unpriced); 'hide' suppresses builtin and lower layers alike.
        # When a read-only lower layer still defines the model, a marker must
        # be written so the fallback/hidden result survives the merge.
        def delete(target, lower_keys):
            if model in lower_keys:
                target[model] = 'builtin' if model in BUILTIN else None
            else:
                target.pop(model, None)

        def unhide(target, lower_keys):
            # Restoring a hidden non-builtin must expose its lower/parent rate;
            # deleting a user rate may intentionally suppress that lower layer.
            if model in BUILTIN:
                delete(target, lower_keys)
            else:
                target.pop(model, None)

        def effective_entry():
            # Inherited names ('x (high)', dated snapshots) resolve to a parent
            # entry; merging onto it keeps a dated history's earlier rates.
            table = load_pricing(config)
            return table.get(entry_key(model, table))

        def edited():
            # A rate without input and output cannot price anything and would be dropped on
            # load: refuse it instead of reporting a save that changes nothing.
            entry = edited_rates(effective_entry(), rates, _today(), remove=cleared)
            if not all(_valid(e) for e in (entry if isinstance(entry, list) else [entry])):
                abort(400, description='입력·출력 단가를 함께 입력하세요.')
            return entry

        path = pricing_write_path()
        with _webapp.pricing_lock:
            if path:
                path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                fd = os.open(path.with_name('pricing.lock'), os.O_CREAT | os.O_RDWR, 0o600)
                with os.fdopen(fd, 'a+') as lock:
                    fcntl.flock(lock, fcntl.LOCK_EX)
                    try:
                        lower = Path(config['pricing_file'])
                        lower_keys = set(config.get('pricing') or {}) | set(
                            _read_json(lower) if lower.resolve() != path.resolve() else {}
                        )
                        # Only the web-edited file is rewritten. The config-dir file stays a lower layer
                        # of its own, so later hand edits to it keep taking effect.
                        current = {}
                        if path.exists():
                            try:
                                current = json.loads(path.read_text())
                            except ValueError:
                                abort(500, description='pricing.json을 읽지 못했습니다.')
                            if not isinstance(current, dict):
                                abort(500, description='pricing.json을 읽지 못했습니다.')
                        if body.get('hide'):
                            current[model] = None
                        elif body.get('unhide'):
                            unhide(current, lower_keys)
                        elif body.get('delete'):
                            delete(current, lower_keys)
                        else:
                            current[model] = edited()
                        _webapp.atomic_json(path, current)
                    finally:
                        fcntl.flock(lock, fcntl.LOCK_UN)
            else:
                lower_keys = set(_read_json(config.get('pricing_file') or ''))
                target = config.setdefault('pricing', {})
                if body.get('hide'):
                    target[model] = None
                elif body.get('unhide'):
                    unhide(target, lower_keys)
                elif body.get('delete'):
                    delete(target, lower_keys)
                else:
                    target[model] = edited()
        return jsonify(model=model, deleted=bool(body.get('delete')), hidden=bool(body.get('hide')))

    @app.post('/api/config/thresholds')
    def set_thresholds():
        body = json_object()
        patch = {}
        for key, (lo, hi) in {
            'stale_seconds': (60, 86400),
            'retention_days': (1, 365),
            'low_percent': (1, 50),
            'quota_hide_days': (1, 90),
            'unused_percent': (10, 100),
            'unused_hours': (1, 72),
        }.items():
            if key not in body:
                continue
            if not _number(body[key], lo, hi):
                abort(400, description=f'{key}는 {lo}~{hi} 범위여야 합니다.')
            patch[key] = body[key]
        saved = persist(
            lambda current: {'thresholds': {**Store.DEFAULT_THRESHOLDS, **(current.get('thresholds') or {}), **patch}}
        )
        return jsonify(thresholds=saved['thresholds'])

    @app.post('/api/config/refresh')
    def set_refresh():
        value = json_object().get('refresh_seconds')
        if not _number(value, 30, 3600):
            abort(400, description='갱신 주기는 30~3600초 범위여야 합니다.')
        persist({'refresh_seconds': int(value)})
        return jsonify(refresh_seconds=int(value))

    @app.post('/api/config/project-budgets')
    def set_project_budgets():
        budgets = json_object().get('budgets')
        if not isinstance(budgets, dict) or len(budgets) > 200:
            abort(400, description='프로젝트 예산 목록을 확인하세요.')
        clean = {}
        for project, budget in budgets.items():
            if not isinstance(project, str) or not 0 < len(project) <= 200 or not isinstance(budget, dict):
                abort(400, description='프로젝트 이름을 확인하세요.')
            tokens, dollars = budget.get('tokens'), budget.get('usd')
            if tokens is not None and not (
                isinstance(tokens, int) and not isinstance(tokens, bool) and 0 < tokens <= 10**15
            ):
                abort(400, description='토큰 예산은 양의 정수여야 합니다.')
            if dollars is not None and not _number(dollars, 0.01, 1000000):
                abort(400, description='USD 예산은 0.01~1000000 범위여야 합니다.')
            if tokens is not None or dollars is not None:
                clean[project] = dict(tokens=tokens, usd=dollars)
        persist({'project_budgets': clean})
        return jsonify(project_budgets=clean)

    @app.post('/api/config/notify')
    def set_notify():
        from .notify import CHANNEL_KEYS, EVENTS, masked, notification_url

        body = json_object()

        def change(latest):
            current = dict(latest.get('notify') or {})
            for key in CHANNEL_KEYS:
                if key not in body:
                    continue  # omitted keeps the stored secret
                value = body[key]
                if value in ('', None):
                    current.pop(key, None)
                    continue
                if not isinstance(value, str) or len(value) > 500:
                    abort(400, description='알림 채널 값을 확인하세요.')
                if key == 'telegram_token' and not _webapp.TELEGRAM_TOKEN_RE.fullmatch(value.strip()):
                    abort(400, description='텔레그램 봇 토큰 형식을 확인하세요.')
                if key.endswith('_url'):
                    try:
                        notification_url(value)
                    except ValueError:
                        abort(400, description='알림 주소는 사용자 정보 없는 유효한 HTTPS 주소여야 합니다.')
                current[key] = value.strip()
            if 'events' in body:
                events = body['events']
                if not isinstance(events, dict) or not all(
                    k in EVENTS and isinstance(v, bool) for k, v in events.items()
                ):
                    abort(400, description='알림 종류를 확인하세요.')
                current['events'] = {**(current.get('events') or {}), **events}
            if 'quiet' in body:
                quiet = body['quiet']
                if quiet is not None and not (
                    isinstance(quiet, list)
                    and len(quiet) == 2
                    and all(isinstance(h, int) and not isinstance(h, bool) and 0 <= h <= 23 for h in quiet)
                    and quiet[0] != quiet[1]
                ):
                    abort(400, description='방해 금지 시간은 서로 다른 0~23시 두 개여야 합니다.')
                current['quiet'] = quiet
            return {'notify': current}

        saved = persist(change)
        return jsonify(masked(saved['notify']))

    @app.post('/api/notify/test')
    def test_notify():
        from .notify import configured, send

        notify = config.get('notify') or {}
        if not configured(notify):
            abort(400, description='설정된 알림 채널이 없습니다.')
        errors = send(notify, 'LLM Usage 알림 테스트', '이 메시지가 보이면 외부 알림이 동작합니다.')
        if errors:
            return jsonify(
                error='전송 실패: ' + ', '.join(f'{k} {v}' for k, v in sorted(errors.items())),
                channels=configured(notify),
                errors=errors,
            ), 502
        return jsonify(channels=configured(notify), errors=errors)

    @app.post('/api/config/value-alert')
    def set_value_alert():
        value = json_object().get('value_alert_usd')
        if value is not None and not _number(value, 0, 1000000):
            abort(400, description='월 환산 알림은 0~1000000 USD 범위이거나 비워 두세요.')
        persist({'value_alert_usd': value})
        return jsonify(value_alert_usd=value)
