"""Quota hierarchy display on an isolated synthetic origin with a numeric /api/limits fixture.

The fixture comes from Store.limits() of this checkout, so the API marking and the UI
are checked together. --preview-assets serves this checkout's web assets.
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


def fixture(weekly_checked_ago):
    now = time.time()
    with tempfile.TemporaryDirectory() as folder:
        store = Store(Path(folder)/'usage.db')
        with store.connect() as c:
            store.limit(c, 'antigravity', 'gemini-5h', 53.6, now+4*3600, now-60, 'antigravity-app')
            store.limit(c, 'antigravity', 'gemini-weekly', 99.0, now+6.9*86400, now-60, 'antigravity')
            for ago, remaining in ((1260, 100.0), (1020, 98.0), (780, 96.0), (540, 94.0), (300, 92.0), (60, 90.0)):
                store.limit(c, 'antigravity', '3p-5h', remaining, now+4.5*3600, now-ago, 'antigravity-app')
            store.limit(c, 'antigravity', '3p-weekly', 0.0, now+6*86400, now-weekly_checked_ago, 'antigravity-app')
            store.limit(c, 'claude-code', 'five_hour', 70.0, now+3*3600, now-60, 'claude-oauth')
            store.limit(c, 'claude-code', 'seven_day', 40.0, now+5*86400, now-60, 'claude-oauth')
        return store.limits(now)


def preview(page, limits):
    def serve(route):
        path = urlsplit(route.request.url).path
        if path == '/api/limits':
            route.fulfill(json=limits)
        elif args.preview_assets and path == '/':
            route.fulfill(path=args.preview_assets.resolve()/'index.html', content_type='text/html',headers=CSP_HEADERS)
        elif args.preview_assets and path.startswith('/assets/'):
            name = Path(path).name
            route.fulfill(path=args.preview_assets.resolve()/name,
                          content_type='text/css' if name.endswith('.css') else 'text/javascript')
        else:
            route.fallback()
    page.route(origin+'/**', serve)


blocked = fixture(120)
row = next(r for r in blocked['limits'] if r['bucket'] == '3p-5h')
assert row['remaining'] == 90.0 and row['blocked_by']['bucket'] == '3p-weekly', row
assert row['forecast'], 'fixture must produce a forecast so hiding it is observable'
stale = fixture(3600)
assert next(r for r in stale['limits'] if r['bucket'] == '3p-5h')['blocked_by'] is None

with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page(service_workers='block',viewport={'width': 1440, 'height': 1000})
    fixture_page(page)
    errors = []; page.on('pageerror', lambda error: errors.append(str(error)));watch_csp(page,errors)
    preview(page, blocked)
    assert page.goto(origin+'/').status == 200
    expect(page.locator('#refresh')).to_be_enabled()
    overview = page.locator('[data-quota="antigravity"] .overview-value')
    expect(overview).to_contain_text('5시간: Gemini 53.6% · 3rd party 사용 불가')
    expect(overview).to_contain_text('주간: Gemini 99.0% · 3rd party 0.0%')
    card = page.locator('#quota-antigravity')
    lower = card.locator('.bucket').filter(has_text='3rd party').first
    expect(lower).to_contain_text('90%')
    expect(lower).to_contain_text('사용 불가')
    expect(lower.locator('.badge.low')).to_have_text('주간 소진')
    expect(lower.locator('.quota-blocked-note')).to_contain_text('주간 한도 소진 · 주간 초기화(')
    expect(lower.locator('.forecast')).to_have_count(0)
    expect(card.locator('.quota-window-title')).to_have_text(['5시간', '주간'])
    claude = page.locator('#quota-claude-code')
    expect(claude).not_to_contain_text('사용 불가')
    timeline = page.locator('#reset-timeline')
    expect(timeline).to_contain_text('Antigravity 주간 3rd party 소진 중')
    expect(timeline).not_to_contain_text('Antigravity 5시간 3rd party')
    expect(timeline).to_contain_text('Antigravity 5시간 Gemini')
    page.screenshot(path=str(output/'quota-hierarchy-desktop.png'), full_page=True)

    # An old weekly 0% no longer blocks: the 5h value and its reset are shown as observed.
    page.unroute(origin+'/**'); preview(page, stale)
    page.reload(); expect(page.locator('#refresh')).to_be_enabled()
    expect(overview).to_contain_text('3rd party 90.0%')
    expect(card).not_to_contain_text('사용 불가')
    expect(timeline).to_contain_text('Antigravity 5시간 3rd party')
    expect(timeline).to_contain_text('Antigravity 주간 3rd party 이전 관측 소진')

    mobile = browser.new_page(service_workers='block',viewport={'width': 390, 'height': 844}, is_mobile=True, has_touch=True)
    fixture_page(mobile)
    mobile.on('pageerror', lambda error: errors.append(str(error)));watch_csp(mobile,errors)
    preview(mobile, blocked)
    assert mobile.goto(origin+'/').status == 200
    expect(mobile.locator('#refresh')).to_be_enabled()
    mobile.locator('[data-quota="antigravity"]').click()
    expect(mobile.locator('#quota-antigravity')).to_be_visible()
    expect(mobile.locator('#quota-antigravity .quota-blocked-note')).to_be_visible()
    assert mobile.evaluate('document.documentElement.scrollWidth<=innerWidth')
    mobile.screenshot(path=str(output/'quota-hierarchy-mobile.png'), full_page=True)
    assert not errors, errors
    browser.close()
print('quota hierarchy browser checks passed')
