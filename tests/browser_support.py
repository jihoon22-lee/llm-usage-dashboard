"""Shared browser-check helpers: serve preview pages with the production CSP and fail on its violations.

A preview that serves index.html without the real headers hides markup the policy blocks
(style="" attributes inserted with innerHTML rendered empty bars in production)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from llm_usage.webapp import CSP  # noqa: E402

CSP_HEADERS = {'Content-Security-Policy': CSP}


def watch_csp(page, errors):
    """Record Content-Security-Policy violations reported to the console as errors."""
    page.on('console', lambda message: errors.append('CSP: ' + message.text)
            if 'Content Security Policy' in message.text else None)

# All browser checks use disposable numeric data. No authentication files, provider
# clients, collector process, private origins or production databases are consulted.
import atexit
import socket
import tempfile
import time
from urllib.parse import urlsplit
from llm_usage.store import Store
from llm_usage.webapp import create_app

ORIGIN = 'https://dashboard.test'
_temp = None
_client = None

class Sock:
    family = socket.AF_UNIX


def fixture_client():
    global _temp, _client
    if _client is not None:
        return _client
    _temp = tempfile.TemporaryDirectory(prefix='llm-browser-fixture-')
    atexit.register(_temp.cleanup)
    folder = Path(_temp.name)
    store = Store(folder/'usage.db')
    now = time.time()
    models = [('codex', 'OpenAI', 'gpt-test'), ('claude-code', 'Anthropic', 'claude-test'),
              ('antigravity', 'Google', 'gemini-3.8-flash')]
    with store.connect() as c:
        for day in range(100):
            for i, (route, provider, model) in enumerate(models):
                store.event(c, f'fixture-{day}-{i}', now-day*86400-1200, provider, route, model,
                            dict(uncached_input=100*(i+1)*(day%7+1), cached_input=400, output=80, cache_creation=50),
                            session=f'fixture-session-{day%3}', project='fixture-project', agent_kind='main')
        for route, _, _ in models:
            store.limit(c, route, 'weekly', 80, now+86400, now-30, route)
            store.source(c, route, 'ok', 'Synthetic browser fixture', now-30)
        for route in ('opencode-go','devin'):
            store.source(c, route, 'ended', 'Synthetic inactive subscription', now-30)
        store.save_state(c, 'collector', dict(status='idle', checked=now-30))
        store.save_state(c, 'last_local', now-30)
    (folder/'config.json').write_text('{}')
    app = create_app(dict(database=str(folder/'usage.db'), origin=ORIGIN, secret_key='fixture-key-'*4,
                          allowed_logins=['fixture'], config_file=str(folder/'config.json'),
                          subscription_prices={r:20 for r, _, _ in models},
                          pricing={m:dict(input=2, cached=.2, output=8, cache_write=2) for _, _, m in models}))
    _client = app.test_client()
    return _client


def fixture_response(request):
    url = urlsplit(request.url)
    assert url.scheme == 'https' and url.netloc == 'dashboard.test', request.url
    headers = {k:v for k,v in request.headers.items() if k.lower() in ('content-type','x-csrf-token','origin')}
    response = fixture_client().open(url.path+('?' + url.query if url.query else ''), method=request.method,
                                    data=request.post_data, headers={**headers,'Tailscale-User-Login':'fixture','Host':'dashboard.test'},
                                    environ_base={'gunicorn.socket':Sock()})
    return response


def fixture_json(request):
    response = fixture_response(request)
    try:
        assert response.status_code == 200, (request.url, response.status_code)
        return response.get_json()
    finally:
        response.close()


def fixture_page(page):
    """Install the final route before test overrides; unexpected network fails closed."""
    def handle(route):
        response = fixture_response(route.request)
        try:
            assert response.status_code < 400, (route.request.url, response.status_code)
            route.fulfill(status=response.status_code, body=response.get_data(),
                          headers={k:v for k,v in response.headers.items() if k.lower()!='content-length'})
        finally:
            response.close()
    page.route('**/*', handle)


def artifact_directory(path=None):
    if path is not None:
        path.mkdir(parents=True, exist_ok=True)
        return path
    folder = tempfile.TemporaryDirectory(prefix='llm-browser-artifacts-')
    atexit.register(folder.cleanup)
    return Path(folder.name)
