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

from .config import atomic_json, config_path, settings, update_local
from .store import Store

ROUTE_RE=re.compile(r'^[a-z0-9_-]{1,64}$')
SESSION_RE=re.compile(r'^[A-Za-z0-9._:/@-]+$')
# Inline style="" attributes are blocked too; the UI sets geometry through the CSSOM.
CSP=("default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; "
     "frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
# Stored names like 'swe-2 (max)' carry a space and parentheses.
MODEL_RE=re.compile(r'^[A-Za-z0-9._/() -]{1,100}$')
# Serializes pricing.json read-modify-write inside this process.
pricing_lock=threading.Lock()


def asset_version(folder):
    """Content hash of the page assets: a deploy changes the URLs, so browsers may
    keep each version's files instead of refetching them on every visit."""
    digest=hashlib.sha256()
    for path in sorted(Path(folder).iterdir()):
        if path.is_file():digest.update(path.name.encode()+b'\0'+path.read_bytes())
    return digest.hexdigest()[:12]


def create_app(config=None):
    if config is None:
        config=settings();config['config_file']=str(config_path())
    if config.get('config_file') and 'pricing_file' not in config:
        config['pricing_file']=str(Path(config['config_file']).parent/'pricing.json')
    store=Store(config['database'],thresholds=config.get('thresholds'))
    origin=config['origin'].rstrip('/')
    app=Flask(__name__,template_folder='web',static_folder='web',static_url_path='/assets')
    version=asset_version(app.static_folder)
    from .pricing import edited_rates,load_pricing
    app.config.update(SECRET_KEY=config['secret_key'],SESSION_COOKIE_NAME='llm_usage_session',
                      SESSION_COOKIE_HTTPONLY=True,SESSION_COOKIE_SECURE=True,SESSION_COOKIE_SAMESITE='Strict',
                      PERMANENT_SESSION_LIFETIME=3600,MAX_CONTENT_LENGTH=65536)

    @app.before_request
    def authorize():
        if request.path=='/healthz' and request.remote_addr in ('127.0.0.1','::1'):
            return
        incoming=request.environ.get('gunicorn.socket')
        if getattr(incoming,'family',None)!=socket.AF_UNIX or request.host.lower() not in ('localhost',urlsplit(origin).netloc):
            abort(403)
        login=request.headers.get('Tailscale-User-Login')
        if login not in config['allowed_logins']:abort(403)
        if session.get('login')!=login:
            session.clear();session.update(login=login,csrf=secrets.token_urlsafe(32));session.permanent=True
        if request.method not in ('GET','HEAD','OPTIONS'):
            if request.headers.get('Origin')!=origin or not request.is_json or not hmac.compare_digest(request.headers.get('X-CSRF-Token',''),session['csrf']):abort(403)

    @app.after_request
    def headers(response):
        # A versioned asset URL never changes content; everything else, data above all, is not stored.
        cached=request.path.startswith('/assets/') and request.args.get('v')==version and response.status_code==200
        response.headers.update({'Cache-Control':'private, max-age=31536000, immutable' if cached else 'no-store','X-Content-Type-Options':'nosniff','X-Frame-Options':'DENY',
            'Referrer-Policy':'no-referrer','Content-Security-Policy':CSP})
        return response

    @app.errorhandler(HTTPException)
    def http_error(error):
        return jsonify(error='본인 Tailscale 계정으로 접속하거나 화면을 새로고침하세요.' if error.code==403 else error.description),error.code

    @app.get('/healthz')
    def health():return jsonify(ok=True)

    @app.get('/')
    def index():return render_template('index.html',v=version)

    @app.get('/sw.js')
    def service_worker():
        # Served from the root so its scope covers the page (offline copy and notifications).
        return send_from_directory(app.static_folder,'sw.js',mimetype='text/javascript')

    @app.get('/manifest.json')
    def manifest():return send_from_directory(app.static_folder,'manifest.json',mimetype='application/manifest+json')

    @app.get('/api/bootstrap')
    def bootstrap():return jsonify(csrf=session['csrf'],refresh_seconds=config.get('refresh_seconds',300))

    def unpriced(data):
        """Models used in the period with no rate and not deliberately left unpriced."""
        from .pricing import NO_PUBLIC_PRICE,pricing_origins
        origins=pricing_origins(config)
        return sorted({r['model'] for r in data.get('rows') or [] if r.get('est_cost') is None and r.get('requests')
                       and r['model'] not in NO_PUBLIC_PRICE and origins.get(r['model'])!='hidden'})

    @app.get('/api/usage')
    def usage():
        try:
            data=(store.usage(period=request.args.get('period','7d'),start=request.args.get('start'),end=request.args.get('end'),
                            granularity=request.args.get('granularity','day'),group=request.args.get('group','provider'),cumulative=request.args.get('cumulative')=='1',pricing=load_pricing(config),subscriptions=config.get('subscription_prices'),compare=request.args.get('compare'),scope=request.args.get('scope') or None,value_alert_usd=config.get('value_alert_usd'),sections=request.args.get('sections','all')))
            return jsonify({**data,'unpriced_models':unpriced(data),'project_budgets':config.get('project_budgets') or {}} if 'rows' in data else data)
        except (ValueError,TypeError,OverflowError):abort(400,description='기간 또는 집계 단위를 확인하세요.')

    @app.get('/api/session')
    def session_detail():
        sid=request.args.get('id','')
        if not 0<len(sid)<=200 or not SESSION_RE.match(sid):abort(400,description='세션 ID를 확인하세요.')
        return jsonify(store.session_detail(sid,load_pricing(config)))

    @app.get('/api/project')
    def project_detail():
        name=request.args.get('name','')
        if not 0<len(name)<=200:abort(400,description='프로젝트 이름을 확인하세요.')
        return jsonify(store.project_detail(name,pricing=load_pricing(config),budget=(config.get('project_budgets') or {}).get(name)))

    @app.get('/api/notify/log')
    def notify_log():
        from .notify import recent_log
        return jsonify(log=recent_log(store))

    @app.get('/api/reports')
    def reports():
        from .reports import recent
        return jsonify(reports=recent(store))

    @app.get('/api/limits')
    def limits():
        from .planning import choices, plan, decide
        data=store.limits()
        options=choices(data)
        if options:
            try:
                route=request.args.get('route',options[0]['route'])
                model=request.args.get('model','common')
                selected=next((o for o in options if o['route']==route and o['model']==model),None)
                selected=selected or next((o for o in options if o['route']==route),options[0])
                hours=float(request.args.get('hours',2));pace=request.args.get('pace','recent')
                today_hours=float(request.args.get('today_hours',2));week_hours=float(request.args.get('week_hours',10))
                if not .1<=today_hours<=24 or not .1<=week_hours<=168:raise ValueError
                data['planning']=plan(data,selected['route'],selected['model'],hours,pace)
                data['planning']['today']=decide(data,selected['route'],selected['model'],today_hours,pace)
                data['planning']['week']=decide(data,selected['route'],selected['model'],week_hours,pace)
                data['planning']['selection_changed']=(route,model)!=(selected['route'],selected['model'])
            except (ValueError,TypeError,OverflowError):abort(400,description='작업 시간·속도 기준을 확인하세요.')
        return jsonify(data)

    def manual_resource(rid=None,delete=False):
        from .resources import write_manual,Conflict
        try:return jsonify(write_manual(store,request.get_json(silent=True),rid,delete))
        except Conflict as error:abort(409,description=str(error))
        except KeyError:abort(404,description='수동 기록을 찾을 수 없습니다.')
        except (ValueError,TypeError):abort(400,description='입력값·단위·적용 범위·연결 자원을 확인하세요.')

    @app.post('/api/resources/manual')
    def add_resource():return manual_resource()

    @app.patch('/api/resources/manual/<rid>')
    def edit_resource(rid):return manual_resource(rid)

    @app.delete('/api/resources/manual/<rid>')
    def delete_resource(rid):return manual_resource(rid,True)

    @app.get('/api/collection')
    def collection():
        with store.connect() as c:
            return jsonify(collector=store.state(c,'collector',{}),backup=store.state(c,'backup',{}),verify=store.state(c,'verify',{}),
                           requested=store.state(c,'refresh_requested',0),completed=store.state(c,'refresh_completed',0),
                           request_id=store.state(c,'refresh_request_id',0),completed_id=store.state(c,'refresh_completed_id',0))

    @app.post('/api/refresh')
    def refresh():
        with store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            requested=store.state(c,'refresh_requested',0)
            request_id=store.state(c,'refresh_request_id',0);completed_id=store.state(c,'refresh_completed_id',0)
            # Coalesce concurrent requests and rate-limit remote polling across sessions/workers.
            if request_id<=completed_id and time.time()-requested>=10:
                requested=time.time();store.save_state(c,'refresh_requested',requested)
                request_id+=1;store.save_state(c,'refresh_request_id',request_id)
        return jsonify(accepted=True,requested=requested,request_id=request_id),202

    config_lock=threading.RLock()

    def persist(patch):
        with config_lock:
            try:
                if config.get('config_file'):
                    update_local(patch,config)
                else:
                    config.update(patch(dict(config)) if callable(patch) else patch)
            except (OSError,ValueError):
                abort(500,description='설정을 저장하지 못했습니다. 저장 상태와 권한을 확인하세요.')
            store.thresholds.update(config.get('thresholds') or {})
            return dict(config)

    # ~/.config is read-only under ProtectHome; reads may fall back to it but
    # writes always go to the data directory (ReadWritePaths).
    def pricing_write_path():
        if not config.get('config_file'):return None
        return Path(config['database']).parent/'pricing.json'

    def pricing_read_path():
        data=Path(config['database']).parent/'pricing.json'
        if data.exists():return data
        return Path(config['pricing_file'])

    def today():
        from datetime import datetime
        from .store import KST
        return datetime.now(KST).strftime('%Y-%m-%d')

    def number(value,lo,hi):
        return isinstance(value,(int,float)) and not isinstance(value,bool) and lo<=value<=hi

    @app.get('/api/config')
    def get_config():
        from .pricing import BUILTIN,entry_key,pricing_origins,rate_for,upcoming_for
        from .store import SUPPORTED_ROUTES
        from .store import KST
        from datetime import datetime
        pricing=load_pricing(config)
        today=datetime.now(KST).strftime('%Y-%m-%d')
        with store.connect() as c:
            providers={r[0]:r[1] for r in c.execute('SELECT model,MAX(provider) FROM events GROUP BY model ORDER BY model')}
            models=list(providers)
            routes=set(SUPPORTED_ROUTES)|set(config.get('subscription_prices') or {})
            routes.update(r[0] for r in c.execute('SELECT DISTINCT route FROM events'))
        upcoming={m:e for m in {*models,*pricing} if (e:=upcoming_for(m,pricing,today))}
        from .notify import masked
        since=time.time()-30*86400
        with store.connect() as c:
            usage30={r[0]:r[1] for r in c.execute('SELECT model,SUM(uncached_input+cached_input+output+cache_creation) FROM events WHERE ts>=? GROUP BY model',(since,))}
        return jsonify(subscription_routes=sorted(routes),subscription_prices=config.get('subscription_prices') or {},notify=masked(config.get('notify')),
                       project_budgets=config.get('project_budgets') or {},model_usage_30d=usage30,
                       thresholds=store.thresholds,pricing=pricing,refresh_seconds=config.get('refresh_seconds',300),
                       value_alert_usd=config.get('value_alert_usd'),
                       upcoming=upcoming,today=today,pricing_origins=pricing_origins(config),
                       pricing_builtin=sorted(BUILTIN),
                       models=[dict(model=m,provider=providers[m],rate=rate_for(m,pricing,today),key=entry_key(m,pricing)) for m in models])

    @app.post('/api/config/subscription-prices')
    def set_prices():
        prices=(request.get_json(silent=True) or {}).get('prices')
        if not isinstance(prices,dict) or len(prices)>64:abort(400,description='구독료 목록을 확인하세요.')
        clean={}
        for route,value in prices.items():
            if not isinstance(route,str) or not ROUTE_RE.match(route):abort(400,description='경로 이름을 확인하세요.')
            if not number(value,0,100000):abort(400,description='구독료는 0~100000 범위의 숫자여야 합니다.')
            clean[route]=value
        persist({'subscription_prices':clean})
        return jsonify(subscription_prices=clean)

    @app.post('/api/config/pricing')
    def set_pricing():
        body=request.get_json(silent=True) or {}
        model=body.get('model')
        if not isinstance(model,str) or not MODEL_RE.match(model):abort(400,description='모델 이름을 확인하세요.')
        rates={}
        for key in ('input','cached','output','cache_write'):
            if body.get(key) is None:continue
            if not number(body[key],0,10000):abort(400,description='단가는 0~10000 범위의 숫자여야 합니다.')
            rates[key]=body[key]
        if not rates and not body.get('delete') and not body.get('hide'):
            abort(400,description='단가를 입력하세요.')
        from .pricing import BUILTIN,_read_json,entry_key
        # 'delete' removes the user value and falls back to the builtin rate
        # (or unpriced); 'hide' suppresses builtin and lower layers alike.
        # When a read-only lower layer still defines the model, a marker must
        # be written so the fallback/hidden result survives the merge.
        def delete(target,lower_keys):
            if model in lower_keys:target[model]='builtin' if model in BUILTIN else None
            else:target.pop(model,None)
        def effective_entry():
            # Inherited names ('x (high)', dated snapshots) resolve to a parent
            # entry; merging onto it keeps a dated history's earlier rates.
            table=load_pricing(config)
            return table.get(entry_key(model,table))
        path=pricing_write_path()
        with pricing_lock:
            if path:
                lower_keys=set(config.get('pricing') or {})|set(_read_json(config.get('pricing_file')))
                current={}
                read=pricing_read_path()
                if read.exists():
                    try:current=json.loads(read.read_text())
                    except ValueError:abort(500,description='pricing.json을 읽지 못했습니다.')
                if body.get('hide'):current[model]=None
                elif body.get('delete'):delete(current,lower_keys)
                else:current[model]=edited_rates(effective_entry(),rates,today())
                atomic_json(path,current)
            else:
                lower_keys=set(_read_json(config.get('pricing_file') or ''))
                target=config.setdefault('pricing',{})
                if body.get('hide'):target[model]=None
                elif body.get('delete'):delete(target,lower_keys)
                else:target[model]=edited_rates(effective_entry(),rates,today())
        return jsonify(model=model,deleted=bool(body.get('delete')),hidden=bool(body.get('hide')))

    @app.post('/api/config/thresholds')
    def set_thresholds():
        body=request.get_json(silent=True) or {}
        patch={}
        for key,(lo,hi) in {'stale_seconds':(60,86400),'retention_days':(1,365),'low_percent':(1,50),'quota_hide_days':(1,90)}.items():
            if key not in body:continue
            if not number(body[key],lo,hi):abort(400,description=f'{key}는 {lo}~{hi} 범위여야 합니다.')
            patch[key]=body[key]
        saved=persist(lambda current: {'thresholds':{**Store.DEFAULT_THRESHOLDS,**(current.get('thresholds') or {}),**patch}})
        return jsonify(thresholds=saved['thresholds'])

    @app.post('/api/config/refresh')
    def set_refresh():
        value=(request.get_json(silent=True) or {}).get('refresh_seconds')
        if not number(value,30,3600):abort(400,description='갱신 주기는 30~3600초 범위여야 합니다.')
        persist({'refresh_seconds':int(value)})
        return jsonify(refresh_seconds=int(value))

    @app.post('/api/config/project-budgets')
    def set_project_budgets():
        budgets=(request.get_json(silent=True) or {}).get('budgets')
        if not isinstance(budgets,dict) or len(budgets)>200:abort(400,description='프로젝트 예산 목록을 확인하세요.')
        clean={}
        for project,budget in budgets.items():
            if not isinstance(project,str) or not 0<len(project)<=200 or not isinstance(budget,dict):abort(400,description='프로젝트 이름을 확인하세요.')
            tokens,dollars=budget.get('tokens'),budget.get('usd')
            if tokens is not None and not (isinstance(tokens,int) and not isinstance(tokens,bool) and 0<tokens<=10**15):
                abort(400,description='토큰 예산은 양의 정수여야 합니다.')
            if dollars is not None and not number(dollars,0.01,1000000):abort(400,description='USD 예산은 0.01~1000000 범위여야 합니다.')
            if tokens is not None or dollars is not None:clean[project]=dict(tokens=tokens,usd=dollars)
        persist({'project_budgets':clean})
        return jsonify(project_budgets=clean)

    @app.post('/api/config/notify')
    def set_notify():
        from .notify import CHANNEL_KEYS,EVENTS,masked,notification_url
        body=request.get_json(silent=True) or {}
        def change(latest):
            current=dict(latest.get('notify') or {})
            for key in CHANNEL_KEYS:
                if key not in body:continue  # omitted keeps the stored secret
                value=body[key]
                if value in ('',None):current.pop(key,None);continue
                if not isinstance(value,str) or len(value)>500:abort(400,description='알림 채널 값을 확인하세요.')
                if key.endswith('_url'):
                    try:notification_url(value)
                    except ValueError:abort(400,description='알림 주소는 사용자 정보 없는 유효한 HTTPS 주소여야 합니다.')
                current[key]=value.strip()
            if 'events' in body:
                events=body['events']
                if not isinstance(events,dict) or not all(k in EVENTS and isinstance(v,bool) for k,v in events.items()):
                    abort(400,description='알림 종류를 확인하세요.')
                current['events']={**(current.get('events') or {}),**events}
            if 'quiet' in body:
                quiet=body['quiet']
                if quiet is not None and not (isinstance(quiet,list) and len(quiet)==2 and all(isinstance(h,int) and 0<=h<=23 for h in quiet) and quiet[0]!=quiet[1]):
                    abort(400,description='방해 금지 시간은 서로 다른 0~23시 두 개여야 합니다.')
                current['quiet']=quiet
            return {'notify':current}
        saved=persist(change)
        return jsonify(masked(saved['notify']))

    @app.post('/api/notify/test')
    def test_notify():
        from .notify import configured,send
        notify=config.get('notify') or {}
        if not configured(notify):abort(400,description='설정된 알림 채널이 없습니다.')
        errors=send(notify,'LLM Usage 알림 테스트','이 메시지가 보이면 외부 알림이 동작합니다.')
        if errors:
            return jsonify(error='전송 실패: '+', '.join(f'{k} {v}' for k,v in sorted(errors.items())),
                           channels=configured(notify),errors=errors),502
        return jsonify(channels=configured(notify),errors=errors)

    @app.post('/api/config/value-alert')
    def set_value_alert():
        value=(request.get_json(silent=True) or {}).get('value_alert_usd')
        if value is not None and not number(value,0,1000000):abort(400,description='월 환산 알림은 0~1000000 USD 범위이거나 비워 두세요.')
        persist({'value_alert_usd':value})
        return jsonify(value_alert_usd=value)
    return app
