import hashlib
import hmac
import json
import re
import secrets
import socket
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

from flask import Flask, abort, jsonify, render_template, request, send_from_directory, session
from werkzeug.exceptions import HTTPException

from .compress import apply_etag_and_compression
from .config import (
    LOCAL_KEYS,
    atomic_json as atomic_json,
    config_path,
    ended_routes,
    local_path,
    settings,
    update_local,
)
from .store import Store
from .webapp_config import register_config_routes

# Claude account routes are 'claude-code:<hash>'.
ROUTE_RE = re.compile(r'^[a-z0-9_-]{1,40}(:[a-z0-9_-]{1,40})?$')
SESSION_RE = re.compile(r'^[A-Za-z0-9._:/@-]+$')
# Inline style="" attributes are blocked too; the UI sets geometry through the CSSOM.
CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; "
    "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
)
# Stored names like 'swe-2 (max)' carry a space and parentheses.
MODEL_RE = re.compile(r'^[A-Za-z0-9._/() -]{1,100}$')
# A bot token is '<id>:<secret>'; anything else would break the request URL it is sent in.
TELEGRAM_TOKEN_RE = re.compile(r'[0-9]{1,20}:[A-Za-z0-9_-]{1,120}')
# Serializes pricing.json read-modify-write inside this process.
pricing_lock = threading.Lock()


def asset_version(folder):
    """Content hash of the page assets: a deploy changes the URLs, so browsers may
    keep each version's files instead of refetching them on every visit."""
    digest = hashlib.sha256()
    for path in sorted(Path(folder).iterdir()):
        if path.is_file():
            digest.update(path.name.encode() + b'\0' + path.read_bytes())
    return digest.hexdigest()[:12]


def create_app(config=None):
    if config is None:
        config = settings()
        config['config_file'] = str(config_path())
    if config.get('config_file') and 'pricing_file' not in config:
        config['pricing_file'] = str(Path(config['config_file']).parent / 'pricing.json')
    store = Store(config['database'], thresholds=config.get('thresholds'))
    origin = config['origin'].rstrip('/')
    origin_netloc = urlsplit(origin).netloc.lower()
    app = Flask(__name__, template_folder='web', static_folder='web', static_url_path='/assets')
    version = asset_version(app.static_folder)
    from .pricing import load_pricing

    config_lock = threading.RLock()
    base_local = {key: config[key] for key in LOCAL_KEYS if key in config}
    local_sig = [None]

    def sync_config():
        """Reload web-edited keys when local.json changes on disk (external edits or other workers)."""
        if not config.get('config_file') or not config.get('database'):
            return
        lp = local_path(config)
        try:
            st = lp.stat()
            sig = (st.st_mtime_ns, st.st_size, st.st_ino)
        except OSError:
            sig = None
        if sig == local_sig[0]:
            return
        with config_lock:
            try:
                st = lp.stat()
                sig = (st.st_mtime_ns, st.st_size, st.st_ino)
            except OSError:
                sig = None
            if sig == local_sig[0]:
                return
            if sig is None:
                local = {}
            else:
                try:
                    local = json.loads(lp.read_text())
                except (OSError, ValueError):
                    return
                if not isinstance(local, dict):
                    return
            for key in LOCAL_KEYS:
                if key in local:
                    config[key] = local[key]
                elif key in base_local:
                    config[key] = base_local[key]
                else:
                    config.pop(key, None)
            store.thresholds = {**Store.DEFAULT_THRESHOLDS, **(config.get('thresholds') or {})}
            local_sig[0] = sig

    sync_config()

    app.config.update(
        SECRET_KEY=config['secret_key'],
        SESSION_COOKIE_NAME='llm_usage_session',
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SECURE=True,
        SESSION_COOKIE_SAMESITE='Strict',
        PERMANENT_SESSION_LIFETIME=3600,
        MAX_CONTENT_LENGTH=65536,
    )

    @app.before_request
    def authorize():
        if request.path == '/healthz' and request.remote_addr in ('127.0.0.1', '::1'):
            return
        incoming = request.environ.get('gunicorn.socket')
        if getattr(incoming, 'family', None) != socket.AF_UNIX or request.host.lower() not in (
            'localhost',
            origin_netloc,
        ):
            abort(403)
        login = request.headers.get('Tailscale-User-Login')
        if login not in config['allowed_logins']:
            abort(403)
        sync_config()
        if session.get('login') != login:
            session.clear()
            session.update(login=login, csrf=secrets.token_urlsafe(32))
            session.permanent = True
        if request.method not in ('GET', 'HEAD', 'OPTIONS'):
            if (
                request.headers.get('Origin') != origin
                or not request.is_json
                or not hmac.compare_digest(request.headers.get('X-CSRF-Token', '').encode(), session['csrf'].encode())
            ):
                abort(403)

    @app.after_request
    def headers(response):
        # A versioned asset URL never changes content; everything else, data above all, is not stored.
        cached = (
            request.path.startswith('/assets/') and request.args.get('v') == version and response.status_code == 200
        )
        response.headers.update(
            {
                'Cache-Control': 'private, max-age=31536000, immutable' if cached else 'no-store',
                'X-Content-Type-Options': 'nosniff',
                'X-Frame-Options': 'DENY',
                'Referrer-Policy': 'no-referrer',
                'Content-Security-Policy': CSP,
            }
        )
        static_etag = cached or request.path in ('/sw.js', '/manifest.json')
        return apply_etag_and_compression(request, response, cacheable=static_etag)

    @app.errorhandler(HTTPException)
    def http_error(error):
        return jsonify(
            error='본인 Tailscale 계정으로 접속하거나 화면을 새로고침하세요.'
            if error.code == 403
            else error.description
        ), error.code

    @app.get('/healthz')
    def health():
        return jsonify(ok=True)

    @app.get('/')
    def index():
        return render_template('index.html', v=version, refresh_seconds=config.get('refresh_seconds', 300))

    @app.get('/sw.js')
    def service_worker():
        # Served from the root so its scope covers the page (offline copy and notifications).
        return send_from_directory(app.static_folder, 'sw.js', mimetype='text/javascript')

    @app.get('/manifest.json')
    def manifest():
        return send_from_directory(app.static_folder, 'manifest.json', mimetype='application/manifest+json')

    @app.get('/api/bootstrap')
    def bootstrap():
        return jsonify(csrf=session['csrf'], refresh_seconds=config.get('refresh_seconds', 300))

    def payload():
        """The request's JSON, or None. Nesting too deep to parse is a bad request, not a crash."""
        try:
            return request.get_json(silent=True)
        except RecursionError:
            abort(400, description='요청 본문을 확인하세요.')

    def json_object():
        data = payload()
        if not isinstance(data, dict):
            abort(400, description='요청 본문은 JSON 객체여야 합니다.')
        return data

    def unpriced(data, pricing=None):
        """Models used in the period with no rate and not deliberately left unpriced."""
        from .pricing import NO_PUBLIC_PRICE, hidden

        if pricing is None:
            pricing = load_pricing(config)
        return sorted(
            {
                r['model']
                for r in data.get('rows') or []
                if r.get('est_cost') is None
                and r.get('requests')
                and r['model'] not in NO_PUBLIC_PRICE
                and not hidden(r['model'], pricing)
            }
        )

    def refused(what, error, description):
        """Answer 400 for a value error raised below. Bad input and a fault of our own raise the
        same types there, so where it was raised is logged first: one line that names the
        request by `what` (a fixed word), without the message or anything the client sent."""
        import traceback

        frame = traceback.extract_tb(error.__traceback__)[-1]
        app.logger.warning(
            '%s request refused: %s at %s:%d', what, type(error).__name__, Path(frame.filename).name, frame.lineno
        )
        abort(400, description=description)

    def residual_label():
        from .reconciliation import RESIDUAL_LABEL

        return config.get('residual_label') or RESIDUAL_LABEL

    def active_prices():
        """Monthly prices of subscriptions that are not marked ended."""
        ended = ended_routes(config)
        return {
            route: value for route, value in (config.get('subscription_prices') or {}).items() if route not in ended
        }

    @app.get('/api/usage')
    def usage():
        try:
            as_of = float(request.args['as_of']) if 'as_of' in request.args else None
            pricing = load_pricing(config)
            data = store.usage(
                period=request.args.get('period', '7d'),
                start=request.args.get('start'),
                end=request.args.get('end'),
                granularity=request.args.get('granularity', 'day'),
                group=request.args.get('group', 'provider'),
                cumulative=request.args.get('cumulative') == '1',
                pricing=pricing,
                subscriptions=active_prices(),
                compare=request.args.get('compare'),
                scope=request.args.get('scope') or None,
                value_alert_usd=config.get('value_alert_usd'),
                sections=request.args.get('sections', 'all'),
                as_of=as_of,
                names=config.get('claude_account_names'),
                ended=ended_routes(config),
                price_history=config.get('subscription_price_history'),
                project_aliases=config.get('project_aliases'),
            )
            # The residual's name is a display setting, not part of the cached usage body.
            return jsonify(
                {
                    **data,
                    'unpriced_models': unpriced(data, pricing),
                    'project_budgets': config.get('project_budgets') or {},
                    'project_aliases': config.get('project_aliases') or {},
                    'residual_label': residual_label(),
                }
                if 'rows' in data
                else data
            )
        except (ValueError, TypeError, OverflowError) as error:
            refused('usage', error, '기간 또는 집계 단위를 확인하세요.')

    @app.get('/api/overview')
    def overview():
        from .overview import build

        return jsonify(
            build(
                store,
                pricing=load_pricing(config),
                subscriptions=active_prices(),
                ended=ended_routes(config),
                names=config.get('claude_account_names'),
                project_aliases=config.get('project_aliases'),
            )
        )

    @app.get('/api/session')
    def session_detail():
        sid = request.args.get('id', '')
        if not 0 < len(sid) <= 200 or not SESSION_RE.fullmatch(sid):
            abort(400, description='세션 ID를 확인하세요.')
        return jsonify(store.session_detail(sid, load_pricing(config)))

    @app.get('/api/project')
    def project_detail():
        name = request.args.get('name', '')
        if not 0 < len(name) <= 200:
            abort(400, description='프로젝트 이름을 확인하세요.')
        return jsonify(
            store.project_detail(
                name,
                pricing=load_pricing(config),
                budget=(config.get('project_budgets') or {}).get(name),
                project_aliases=config.get('project_aliases'),
            )
        )

    @app.post('/api/simulate')
    def simulate_cost():
        from .simulator import simulate, validate_substitutions

        body = json_object()
        try:
            subs_rules = validate_substitutions(body.get('substitutions'), MODEL_RE)
            raw_subs = body.get('subscriptions')
            if raw_subs is None:
                sub_prices = active_prices()
            elif isinstance(raw_subs, dict) and len(raw_subs) <= 64:
                sub_prices = {}
                for route, val in raw_subs.items():
                    if (
                        not isinstance(route, str)
                        or not ROUTE_RE.fullmatch(route)
                        or isinstance(val, bool)
                        or not isinstance(val, (int, float))
                        or not 0 <= val <= 100000
                    ):
                        raise ValueError('구독료 입력이 올바르지 않습니다.')
                    sub_prices[route] = float(val)
            else:
                raise ValueError('구독료 입력이 올바르지 않습니다.')
            return jsonify(
                simulate(
                    store,
                    period=body.get('period') or '30d',
                    start=body.get('start'),
                    end=body.get('end'),
                    scope=body.get('scope') or None,
                    substitutions=subs_rules,
                    pricing=load_pricing(config),
                    subscriptions=sub_prices,
                    project_aliases=config.get('project_aliases'),
                )
            )
        except (ValueError, TypeError, OverflowError) as error:
            refused('simulate', error, '시뮬레이션 조건(기간·모델·비율·구독료)을 확인하세요.')

    @app.get('/api/diagnostics/system')
    def diagnostics_system():
        from .diagnostics import system_diagnostics

        return jsonify(system_diagnostics(store))

    @app.post('/api/diagnostics/backup')
    def diagnostics_backup():
        from .diagnostics import trigger_manual_backup

        result, wait = trigger_manual_backup(store)
        if result is None:
            return jsonify(ok=False, error=f'백업은 {wait}초 뒤에 다시 실행할 수 있습니다.', retry_after=wait), 429
        if not result.get('ok'):
            return jsonify(ok=False, backup=result, error=result.get('error') or '백업 실패'), 500
        return jsonify(ok=True, backup=result)

    @app.get('/api/notify/log')
    def notify_log():
        from .notify import recent_log

        return jsonify(log=recent_log(store))

    @app.get('/api/reports')
    def reports():
        from .monthly import recent as months
        from .overview import week_facts
        from .pricing import load_pricing
        from .reports import recent, this_week

        return jsonify(
            reports=[{**r, 'facts': week_facts(r)} for r in recent(store)],
            this_week=this_week(store, time.time(), load_pricing(config)),
            months=months(store),
        )

    @app.get('/api/limits')
    def limits():
        return jsonify(store.limits(ended=ended_routes(config), names=config.get('claude_account_names')))

    @app.get('/api/codex/activity')
    def codex_activity():
        from .codex_activity import activity

        try:
            return jsonify(
                activity(
                    store,
                    period=request.args.get('period', '7d'),
                    start=request.args.get('start'),
                    end=request.args.get('end'),
                    kind=request.args.get('kind', ''),
                    state=request.args.get('state', ''),
                    search=request.args.get('search', ''),
                    page=request.args.get('page', 1),
                )
            )
        except (ValueError, TypeError, OverflowError) as error:
            refused('codex activity', error, '작업 기간 또는 필터를 확인하세요.')

    @app.get('/api/codex/activity/<key>')
    def codex_activity_detail(key):
        from .codex_activity import task_detail

        try:
            return jsonify(task_detail(store, key))
        except ValueError:
            abort(400, description='작업 ID를 확인하세요.')
        except KeyError:
            abort(404, description='현재 계정에서 수집한 작업이 아닙니다.')

    @app.get('/api/codex/consumption')
    def codex_consumption():
        from .codex_activity import consumption

        try:
            return jsonify(consumption(store, days=request.args.get('days', 7)))
        except (ValueError, TypeError, OverflowError):
            abort(400, description='소비 분석 기간은 7일 또는 30일입니다.')

    def manual_resource(rid=None, delete=False):
        from .resources import write_manual, Conflict

        try:
            return jsonify(write_manual(store, payload(), rid, delete))
        except Conflict as error:
            abort(409, description=str(error))
        except KeyError:
            abort(404, description='수동 기록을 찾을 수 없습니다.')
        except (ValueError, TypeError) as error:
            refused('manual resource', error, '입력값·단위·적용 범위·연결 자원을 확인하세요.')

    @app.post('/api/resources/manual')
    def add_resource():
        return manual_resource()

    @app.patch('/api/resources/manual/<rid>')
    def edit_resource(rid):
        return manual_resource(rid)

    @app.delete('/api/resources/manual/<rid>')
    def delete_resource(rid):
        return manual_resource(rid, True)

    @app.get('/api/collection')
    def collection():
        with store.connect() as c:
            return jsonify(
                collector=store.state(c, 'collector', {}),
                backup=store.state(c, 'backup', {}),
                verify=store.state(c, 'verify', {}),
                telemetry=store.state(
                    c, 'telemetry:received', dict(checked=None, accepted=0, pending=0, status='unavailable')
                ),
                requested=store.state(c, 'refresh_requested', 0),
                completed=store.state(c, 'refresh_completed', 0),
                request_id=store.state(c, 'refresh_request_id', 0),
                completed_id=store.state(c, 'refresh_completed_id', 0),
            )

    @app.post('/api/refresh')
    def refresh():
        from .diagnostics import validate_refresh_targets

        body = payload()
        targets = None
        if body is not None:
            if not isinstance(body, dict):
                abort(400, description='요청 본문은 JSON 객체여야 합니다.')
            if 'targets' in body and body['targets'] is not None:
                try:
                    targets = validate_refresh_targets(body['targets'])
                except ValueError as error:
                    refused('refresh', error, '갱신 대상을 확인하세요.')
        with store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            requested = store.state(c, 'refresh_requested', 0)
            request_id = store.state(c, 'refresh_request_id', 0)
            completed_id = store.state(c, 'refresh_completed_id', 0)
            # Coalesce concurrent requests and rate-limit remote polling across sessions/workers.
            if request_id <= completed_id and time.time() - requested >= 10:
                requested = time.time()
                store.save_state(c, 'refresh_requested', requested)
                store.save_state(c, 'refresh_targets', targets)
                request_id += 1
                store.save_state(c, 'refresh_request_id', request_id)
        return jsonify(accepted=True, requested=requested, request_id=request_id, targets=targets or ['all']), 202

    def persist(patch):
        with config_lock:
            try:
                if config.get('config_file'):
                    update_local(patch, config)
                    try:
                        st = local_path(config).stat()
                        local_sig[0] = (st.st_mtime_ns, st.st_size, st.st_ino)
                    except OSError:
                        local_sig[0] = None
                else:
                    config.update(patch(dict(config)) if callable(patch) else patch)
            except (OSError, ValueError):
                abort(500, description='설정을 저장하지 못했습니다. 저장 상태와 권한을 확인하세요.')
            store.thresholds = {**Store.DEFAULT_THRESHOLDS, **(config.get('thresholds') or {})}
            return dict(config)

    register_config_routes(app, config, store, persist, json_object)
    return app
