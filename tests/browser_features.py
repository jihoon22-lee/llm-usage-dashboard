"""Portable Chromium regression checks using disposable synthetic data and local assets."""
from browser_support import CSP_HEADERS, watch_csp, fixture_page, fixture_json, artifact_directory
import argparse
import json
import time
from pathlib import Path
from urllib.parse import urlsplit, parse_qs

from playwright.sync_api import sync_playwright, expect

parser = argparse.ArgumentParser()
parser.add_argument('--preview-assets', type=Path)
parser.add_argument('--artifacts', type=Path)
args = parser.parse_args()
origin = 'https://dashboard.test'
output = artifact_directory(args.artifacts)
output.mkdir(parents=True, exist_ok=True)
now = time.time()
SOURCES = [dict(name='수집기', status='ok', checked=now-20, detail='idle'),
           dict(name='codex', status='error', checked=now-60, detail='HTTP 500'),
           dict(name='WSL .claude/projects', status='partial', checked=now-60, detail='12개 기록 파일 확인 · 해석 실패 1건'),
           dict(name='claude-code', status='idle', checked=now-3*86400, detail='최근 24시간 상태줄 수신 없음'),
           dict(name='normalization', status='ok', checked=now-60, detail='카운터 초기화 구간을 구분한 집계'),
           dict(name='devin', status='ok', checked=now-60, detail='계정 사용량 조회')]
SESSION = dict(session='fixture-session', project='fixture-project', kind='main', first_ts=now-7200, last_ts=now-600, requests=12,
               totals=dict(uncached_input=10, cached_input=900, output=50, cache_creation=40, reasoning=0), cache_share=95.2, cost=1.25,
               models=[dict(route='codex', model='gpt-fixture', requests=12, uncached_input=10, cached_input=900, output=50, cache_creation=40,
                            reasoning=0, first_ts=now-7200, last_ts=now-600, cost=1.25)],
               hours=[dict(hour=int(now//3600-2)*3600, requests=5, tokens=500), dict(hour=int(now//3600-1)*3600, requests=7, tokens=500)])
posts = []
REPORT = dict(week='2026-09-21', start='2026-09-21', end='2026-09-27', built=now, change_pct=12.5,
              totals=dict(tokens=3.2e9, requests=4100, cost=812.5), previous=dict(tokens=2.84e9, requests=3900, cost=700),
              routes=[dict(route='codex', tokens=2.5e9, requests=3000, cost=700.0, change_pct=20.0)],
              models=[dict(model='gpt-fixture', route='codex', tokens=2.5e9, cost=700.0)],
              exhausted=[dict(route='claude-code', bucket='five_hour', count=2)], busiest_day='2026-09-24')


def preview(page):
    def serve(route):
        request = route.request; path = urlsplit(request.url).path
        if request.method == 'POST':
            posts.append((path, json.loads(request.post_data or '{}')))
            if path == '/api/config/notify':
                route.fulfill(json=dict(channels=dict(ntfy='ntfy.sh', webhook='hooks.example', telegram=None),
                                        events=dict(low=True, exhausted=True, recovered=True, spike=False, collector=True, source=True), quiet=[23, 7]))
            elif path == '/api/notify/test':
                route.fulfill(json=dict(channels=['ntfy', 'webhook'], errors={}))
            elif path == '/api/config/project-budgets':
                route.fulfill(json=dict(project_budgets=json.loads(request.post_data)['budgets']))
            else:
                route.fulfill(status=202, json=dict(accepted=True, requested=1, request_id=0))
            return
        if path == '/api/usage':
            data = fixture_json(route.request)
            if parse_qs(urlsplit(request.url).query).get('sections') != ['insights']:
                data['sources'] = SOURCES
                data['spike'] = dict(hour_start=int(now//3600)*3600, tokens=60e6, baseline=12e6, ratio=5.0, current=True)
                data['unpriced_models'] = ['brand-new-model']
            data['project_budgets'] = {'fixture-project': dict(tokens=1000, usd=None)}
            data['project_month'] = dict(month='2026-10', projects={'fixture-project': dict(tokens=900, cost=None, priced_share=0)})
            data['projects'] = [dict(project='fixture-project', sessions=1, last_ts=now-600, requests=12, uncached_input=10,
                                     cached_input=900, output=50, cache_creation=40, cost=1.25)]
            data.setdefault('sessions', [])
            data['sessions'] = [dict(session='fixture-session', route='codex', project='fixture-project', kind='main', first_ts=now-7200,
                                     last_ts=now-600, requests=12, uncached_input=10, cached_input=900, output=50, cache_creation=40, cost=1.25)]
            data['sessions_total'] = 1
            route.fulfill(json=data)
        elif path == '/api/reports':
            route.fulfill(json=dict(reports=[REPORT]))
        elif path == '/api/session':
            route.fulfill(json=SESSION)
        elif path == '/api/config':
            data = fixture_json(route.request)
            data['project_budgets'] = {'fixture-project': dict(tokens=1000, usd=None)}
            data['notify'] = dict(channels=dict(ntfy='ntfy.sh', webhook=None, telegram=None),
                                  events=dict(low=True, exhausted=True, recovered=True, spike=True, collector=True, source=True), quiet=None)
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
    page = browser.new_page(service_workers='block',viewport={'width': 1440, 'height': 1000})
    fixture_page(page)
    page.on('pageerror', lambda error: errors.append(str(error)));watch_csp(page,errors)
    preview(page)
    assert page.goto(origin+'/').status == 200
    expect(page.locator('#refresh')).to_be_enabled()
    page.evaluate("$('auto').checked=false;$('auto').dispatchEvent(new Event('change'))")
    alerts = page.locator('#alerts')
    expect(alerts).to_contain_text('사용량 급증')
    expect(alerts).to_contain_text('단가 미등록 모델 1개 · brand-new-model')
    expect(alerts).to_contain_text('fixture-project 월 예산 90%')
    # Sources: summary, problem-first groups, internal work folded, a hint per status.
    page.locator('#tabs [data-view="status"]').click()
    expect(page.locator('#quality .q-ok, #quality .q-warn').first).to_be_visible()
    expect(page.locator('.source-summary')).to_contain_text('정상 3')
    expect(page.locator('.source-summary')).to_contain_text('확인 필요 1')
    expect(page.locator('.source-summary')).to_contain_text('실패·미수집 1')
    expect(page.locator('.source-summary')).to_contain_text('미사용 1')
    expect(page.locator('#sources .source-group > h4').first).to_have_text('Codex')
    internal = page.locator('#sources details.source-group')
    expect(internal).to_contain_text('내부 작업')
    assert internal.get_attribute('open') is None
    expect(page.locator('#sources')).to_contain_text('다음 주기에 다시 시도')
    expect(page.locator('#sources')).to_contain_text('claude-code · 미사용')
    # Usage: the bucket containing now is marked, and numbers can be shortened.
    page.locator('#tabs [data-view="usage"]').click()
    expect(page.locator('#chart .chart-current-label')).to_have_text('진행 중')
    first_total = page.locator('#rows tr').first.locator('.total-cell')
    exact = first_total.inner_text()
    page.locator('#num-compact').click()
    expect(page.locator('#num-compact')).to_have_attribute('aria-pressed', 'true')
    assert first_total.inner_text() != exact and first_total.locator('[data-tip]').count() == 1
    page.locator('#num-compact').click()
    expect(first_total).to_have_text(exact)
    # A session opens its detail and closes again; project budgets show in the projects table.
    page.locator('.sess-open').first.click()
    detail = page.locator('.sess-detail-row')
    expect(detail).to_contain_text('fixture-project')
    expect(detail).to_contain_text('gpt-fixture')
    expect(detail.locator('.sess-hours rect')).to_have_count(2)
    page.locator('.sess-open').first.click()
    expect(detail).to_have_count(0)
    expect(page.locator('#projects')).to_contain_text('이번 달 예산 90% · 900/1K 토큰')
    page.locator('#tabs [data-view="reports"]').click()
    reports = page.locator('#reports')
    expect(reports).to_contain_text('2026-09-21 ~ 2026-09-27')
    expect(reports).to_contain_text('전주 대비 +13%')
    expect(reports).to_contain_text('한도 소진: Claude 5시간 2회')
    # Settings: secrets are write-only; save, clear and test go through the API only.
    page.locator('#tabs [data-view="settings"]').click()
    expect(page.locator('#ntfy-state')).to_have_text('설정됨 · ntfy.sh')
    expect(page.locator('#notify-events input')).to_have_count(8)
    budget = page.locator('#cfg-budgets [data-budget="fixture-project"]')
    expect(budget.locator('[data-budget-key="tokens"]')).to_have_value('0.001')
    budget.locator('[data-budget-key="usd"]').fill('50')
    page.locator('#budgets-save').click()
    expect(page.locator('#budgets-msg')).to_contain_text('예산을 저장했습니다')
    path, body = posts[-1]
    assert path == '/api/config/project-budgets' and body['budgets']['fixture-project'] == {'tokens': 1000, 'usd': 50}, body
    page.locator('#webhook-url').fill('https://hooks.example/abc')
    page.locator('#notify-events input[data-notify-event="spike"]').uncheck()
    page.locator('#quiet-start').fill('23'); page.locator('#quiet-end').fill('7')
    page.locator('#notify-save').click()
    expect(page.locator('#notify-msg')).to_contain_text('저장했습니다.')
    path, body = posts[-1]
    assert path == '/api/config/notify' and body['webhook_url'] == 'https://hooks.example/abc' and 'ntfy_url' not in body, body
    assert body['events']['spike'] is False and body['quiet'] == [23, 7], body
    expect(page.locator('#webhook-state')).to_have_text('설정됨 · hooks.example')
    expect(page.locator('#webhook-url')).to_have_value('')
    page.locator('[data-notify-clear="ntfy_url"]').click()
    expect(page.locator('#notify-msg')).to_have_text('지웠습니다.')
    assert posts[-1] == ('/api/config/notify', {'ntfy_url': ''}), posts[-1]
    page.locator('#notify-test').click()
    expect(page.locator('#notify-msg')).to_contain_text('테스트를 보냈습니다: ntfy, webhook')
    # The unpriced-model chip lands on the pricing table filtered to unset rates.
    page.locator('#tabs [data-view="quota"]').click()
    page.locator('.alert-chip', has_text='단가 미등록').click()
    expect(page.locator('#price-unset')).to_have_attribute('aria-pressed', 'true')
    page.screenshot(path=str(output/'features-desktop.png'), full_page=True)
    page.unroute_all(behavior='ignoreErrors'); page.close()
    # Phones: tabs are fixed to the bottom edge and nothing scrolls sideways.
    mobile = browser.new_page(service_workers='block',viewport={'width': 390, 'height': 844}, is_mobile=True, has_touch=True)
    fixture_page(mobile)
    mobile.on('pageerror', lambda error: errors.append(str(error)));watch_csp(mobile,errors)
    preview(mobile)
    assert mobile.goto(origin+'/').status == 200
    expect(mobile.locator('#refresh')).to_be_enabled()
    box = mobile.locator('#tabs').bounding_box()
    assert abs(box['y']+box['height']-844) <= 1, box
    mobile.locator('#tabs [data-view="reports"]').click()
    expect(mobile.locator('#view-reports')).to_be_visible()
    assert mobile.evaluate('document.documentElement.scrollWidth<=innerWidth')
    mobile.screenshot(path=str(output/'features-mobile.png'), full_page=True)
    assert not [p for p in posts if p[0] not in ('/api/config/notify', '/api/notify/test', '/api/refresh', '/api/config/project-budgets')], posts
    assert not errors, errors
    mobile.unroute_all(behavior='ignoreErrors')
    browser.close()
print('feature browser checks passed')
