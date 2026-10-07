"""Status presentation on an isolated synthetic origin with controlled /api/limits and sources.

Checks a single alert on mobile, the stopped-collector banner, the per-service
"partly old" overview state, the Antigravity window note and the unused status
line. --preview-assets serves this checkout's web assets.
"""
from browser_support import CSP_HEADERS, watch_csp, fixture_page, fixture_json, artifact_directory
import argparse
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright, expect

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from llm_usage.store import Store  # noqa: E402

parser = argparse.ArgumentParser()
parser.add_argument('--preview-assets', type=Path)
parser.add_argument('--artifacts', type=Path)
args = parser.parse_args()
origin = 'https://dashboard.test'
output = artifact_directory(args.artifacts)
output.mkdir(parents=True, exist_ok=True)


def limits_fixture():
    now = time.time()
    with tempfile.TemporaryDirectory() as folder:
        store = Store(Path(folder)/'usage.db')
        with store.connect() as c:
            store.limit(c, 'antigravity', 'gemini-5h', 60.0, now+4*3600, now-60, 'antigravity-app')
            store.limit(c, 'antigravity', '3p-5h', 100.0, now+4*3600, now-60, 'antigravity-app')
            store.limit(c, 'antigravity', '3p-weekly', 0.0, now+5*86400, now-3600, 'antigravity-app')
            store.limit(c, 'codex', 'codex · 10080분', 10.0, now+5*86400, now-60, 'codex')
        return store.limits(now)


def sources(collector_age):
    now = time.time()
    return [dict(name='수집기', status='ok' if collector_age < 600 else 'stale', checked=now-collector_age, detail='idle'),
            dict(name='claude-code', status='idle', checked=now-3*86400,
                 detail='최근 24시간 상태줄 수신 없음 · 터미널(TUI) 실행 중에만 수신 · 한도는 계정/앱 조회로 표시')]


def preview(page, collector_age):
    limits = limits_fixture()

    def serve(route):
        path = urlsplit(route.request.url).path
        if path == '/api/limits':
            route.fulfill(json=limits)
        elif path == '/api/usage':
            data = fixture_json(route.request); data['sources'] = sources(collector_age)
            data['spike'] = None; data['unpriced_models'] = []  # only the fixture quota alerts
            route.fulfill(json=data)
        elif args.preview_assets and path == '/':
            route.fulfill(path=args.preview_assets.resolve()/'index.html', content_type='text/html',headers=CSP_HEADERS)
        elif args.preview_assets and path.startswith('/assets/'):
            name = Path(path).name
            route.fulfill(path=args.preview_assets.resolve()/name,
                          content_type='text/css' if name.endswith('.css') else 'text/javascript')
        else:
            route.fallback()
    page.route(origin+'/**', serve)


with sync_playwright() as p:
    browser = p.chromium.launch()
    errors = []
    for width, mobile in ((1440, False), (390, True)):
        page = browser.new_page(service_workers='block',viewport={'width': width, 'height': 900}, is_mobile=mobile, has_touch=mobile)
        fixture_page(page)
        page.on('pageerror', lambda error: errors.append(str(error)));watch_csp(page,errors)
        preview(page, 30)
        assert page.goto(origin+'/').status == 200
        expect(page.locator('#refresh')).to_be_enabled()
        # One low quota is the only alert; it must be visible on mobile too.
        chips = page.locator('#alerts .alert-chip')
        expect(chips).to_have_count(1)
        expect(chips.first).to_be_visible()
        expect(chips.first).to_contain_text('Codex')
        expect(page.locator('#collector-banner')).to_be_hidden()
        status = page.locator('[data-quota="antigravity"] .overview-status')
        expect(status).to_have_text('최근 확인 · 일부 오래됨')
        if mobile:
            page.locator('[data-quota="antigravity"]').click()
        note = page.locator('#quota-antigravity .quota-note')
        expect(note).to_have_count(1)
        expect(note).to_contain_text('5시간 창을 보고')
        expect(note).to_be_visible()
        page.locator('#tabs [data-view="sources"]').click()
        expect(page.locator('#sources')).to_contain_text('claude-code · 미사용')
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
        page.screenshot(path=str(output/f'status-{width}.png'), full_page=True)
        page.close()
    page = browser.new_page(service_workers='block',viewport={'width': 390, 'height': 900}, is_mobile=True, has_touch=True)
    fixture_page(page)
    page.on('pageerror', lambda error: errors.append(str(error)));watch_csp(page,errors)
    preview(page, 1500)
    assert page.goto(origin+'/').status == 200
    expect(page.locator('#refresh')).to_be_enabled()
    banner = page.locator('#collector-banner')
    expect(banner).to_be_visible()
    expect(banner).to_contain_text('수집기가 25분째 응답하지 않습니다')
    assert not errors, errors
    browser.close()
print('status browser checks passed')
