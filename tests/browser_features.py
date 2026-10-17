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
              exhausted=[dict(route='claude-code', bucket='five_hour', count=2)], busiest_day='2026-09-24',
              accounts=[dict(account='claude-code', label='Pro', tokens=4e8, cost=None),
                        dict(account='claude-code:unknown', label='계정 미확인', tokens=1e8, cost=None)],
              projects=[dict(project='fixture-project', tokens=2e9, cost=500.0), dict(project='미분류', tokens=1e9, cost=None)],
              quota_use=[dict(route='codex', bucket='codex · 10080분', display_name='Codex 공통 · 주간', observed_hours=150.5, zero_hours=0,
                              resets_observed=1, before_reset=dict(avg=41.0, min=41.0, max=41.0), exhausted=0, streak=0),
                         dict(route='claude-code', bucket='five_hour', observed_hours=120, zero_hours=3.5, resets_observed=18,
                              before_reset=dict(avg=22.4, min=0.0, max=80.0), exhausted=2, streak=3),
                         dict(route='claude-code', bucket='seven_day', observed_hours=0.5, zero_hours=0, resets_observed=0,
                              before_reset=None, exhausted=0, streak=0)])

# This week so far: last week up to the same point (Wednesday 12:00 KST) is the comparison.
THIS_WEEK = dict(start='2026-09-28', end='2026-10-04', tokens=1.5e9, requests=2000, cost=400.0,
                 prior=dict(mode='previous', label='직전 기간', start='2026-09-21T00:00:00+09:00', end_exclusive='2026-09-23T12:00:00+09:00',
                            current_end='2026-09-30T12:00:00+09:00', running=True, complete=True, current_tokens=1.5e9, requests=1800,
                            cost=300.0, uncached_input=1.2e9, cached_input=0, output=0, cache_creation=0),
                 accounts=None, routes=[dict(route='codex', tokens=1e9), dict(route='claude-code', tokens=5e8)])

# Settled months: September whole, August partial with its quota history already pruned.
MONTHS = [dict(month='2026-09', start='2026-09-01', end='2026-09-30', built=now, partial=False, records_from=None,
               totals=dict(tokens=5.2e9, requests=40000, cost=4100.0, residual_tokens=3e8),
               subscriptions=[dict(route='codex', monthly_usd=200, value=3600.0, tokens=4.5e9, ratio=18.0),
                              dict(route='claude-code', monthly_usd=100, value=500.0, tokens=7e8, ratio=5.0),
                              dict(route='devin', monthly_usd=None, value=20.0, tokens=1e8, ratio=None)],
               models=[dict(model='gpt-fixture', route='codex', tokens=4.5e9, cost=3600.0)],
               projects=[dict(project='fixture-project', tokens=3e9, cost=2000.0)], accounts=None, residual=None,
               quota_use=[dict(route='codex', bucket='codex · 10080분', display_name='Codex 공통 · 주간', observed_hours=600, zero_hours=12.5,
                               resets_observed=4, before_reset=dict(avg=10.0, min=0.0, max=30.0), exhausted=2)],
               quota_from=1788222600),
          dict(month='2026-08', start='2026-08-01', end='2026-08-31', built=now, partial=True, records_from='2026-08-20',
               totals=dict(tokens=1e8, requests=900, cost=100.0, residual_tokens=0),
               subscriptions=[dict(route='codex', monthly_usd=200, value=100.0, tokens=1e8, ratio=0.5)],
               models=[], projects=[], accounts=None, residual=None, quota_use=None, quota_from=None)]


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
            route.fulfill(json=dict(reports=[REPORT], this_week=THIS_WEEK, months=MONTHS))
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
    # Sources: failures and values to check first with a hint, what is not collected
    # listed in a neutral color, normal sources (internal work inside) folded.
    page.locator('#tabs [data-view="status"]').click()
    expect(page.locator('#quality .q-ok, #quality .q-warn').first).to_be_visible()
    assert page.locator('.source-summary > span').all_text_contents()==['실패 1','확인 필요 1','미제공 0','미사용 1','정상 3']
    action = page.locator('#sources .source-action')
    expect(action.locator('h4')).to_have_text('조치 필요 2개')
    assert action.locator('.source strong').all_text_contents()==['codex','WSL .claude/projects']
    expect(action.locator('.source').first).to_be_visible()
    expect(action).to_contain_text('다음 주기에 다시 시도')
    quiet = page.locator('#sources .source-quiet')
    expect(quiet.locator('li').first).to_be_visible()
    expect(quiet.locator('li').first).to_contain_text('claude-code · 미사용')
    assert quiet.locator('.src-bad,.src-warn').count()==0
    normal = page.locator('#sources details[data-fold="ok"]')
    assert normal.get_attribute('open') is None
    expect(normal.locator(':scope > summary')).to_contain_text('정상 3개')
    expect(normal.locator('.source').first).to_be_hidden()
    normal.locator(':scope > summary').click()
    internal = normal.locator('details[data-fold="internal"]')
    expect(internal).to_contain_text('normalization')
    assert internal.get_attribute('open') is None
    # An opened fold stays open through the periodic re-render.
    page.evaluate('renderSources(lastUsage.sources)')
    assert page.locator('#sources details[data-fold="ok"]').get_attribute('open') is not None
    expect(page.locator('#sources-panel .coverage-fold')).to_contain_text('수집 범위 설명')
    assert page.locator('#sources-panel .coverage-fold').get_attribute('open') is None
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
    # Long session IDs keep their last characters so neighbours stay distinguishable.
    expect(page.locator('#sessions code').first).to_have_text('fixture-…sion')
    expect(page.locator('#sessions code').first).to_have_attribute('data-tip', 'fixture-session')
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
    # This week so far leads, against last week at the same point; then the finished week
    # with Claude Code by account (stored names without account settings) and top projects.
    week = reports.locator('.this-week')
    expect(week).to_contain_text('이번 주 2026-09-28 ~ 2026-10-04 · 진행 중 · 09. 30. 12:00 기준')
    expect(week).to_contain_text('지난주 같은 시점 대비 +25.0%')
    expect(week).to_contain_text('서비스별: Codex 1B · Claude 500M')
    expect(week.locator('.hint')).to_have_count(1)
    assert reports.locator(':scope > *').first.get_attribute('class') == 'this-week'
    expect(reports).to_contain_text('Claude 계정별: Pro 400M · 계정 미확인 100M')
    expect(reports).to_contain_text('상위 프로젝트: fixture-project 2B · 미분류 1B')
    # Quota use: observed facts per window, the tightest first; nothing unobserved reads as 0.
    use = reports.locator('.quota-use tbody tr')
    expect(use).to_have_count(3)
    expect(use.first).to_contain_text('Claude 5시간')
    expect(use.first).to_contain_text('3주 연속 소진')
    expect(use.first).to_contain_text('3.5시간')
    expect(use.first).to_contain_text('평균 22.4% · 최소 0.0% · 18회')
    expect(use.nth(1)).to_contain_text('Codex 공통 · 주간')
    expect(use.nth(1)).to_contain_text('41.0% · 1회')
    expect(use.nth(2)).to_contain_text('미관측')
    expect(use.nth(2)).to_contain_text('30분')
    # Older reports without the field keep their exhaustion line; a week whose history was
    # pruned before it was built says so.
    page.evaluate("renderReports([{...%s,quota_use:undefined}])" % json.dumps(REPORT))
    expect(reports).to_contain_text('한도 소진: Claude 5시간 2회')
    page.evaluate("renderReports([{...%s,quota_use:null,exhausted:null}])" % json.dumps(REPORT))
    expect(reports).to_contain_text('보존 기간이 지나')
    page.evaluate("renderReports(%s)" % json.dumps([REPORT]))
    expect(week).to_contain_text('지난주 같은 시점 대비 +25.0%')
    # A week begun inside the stored records cannot be compared; nothing yet this week says so.
    page.evaluate("renderReports(%s,%s)" % (json.dumps([REPORT]), json.dumps({**THIS_WEEK, 'tokens': 0, 'requests': 0, 'routes': [],
                                                                                 'prior': {**THIS_WEEK['prior'], 'complete': False}})))
    expect(week).to_contain_text('지난주 같은 시점 기록 부족')
    expect(week).to_contain_text('이번 주에는 아직 사용 기록이 없습니다.')
    page.evaluate("renderReports(%s,%s)" % (json.dumps([REPORT]), json.dumps(THIS_WEEK)))
    # Monthly settlements: the last month in full, each subscription against the price it
    # was settled with; the month-by-month table; older months folded.
    monthly = page.locator('#monthly')
    expect(monthly).to_contain_text('2026-09 · 09-01 ~ 09-30 · 5.2B 토큰 · 40,000회 · 공개 단가 환산 $4,100')
    expect(monthly.locator(':scope > .sub-row')).to_have_count(3)
    expect(monthly.locator(':scope > .sub-row .value-bar')).to_have_count(2)
    codex = monthly.locator(':scope > .sub-row', has_text='OpenAI / Codex')
    expect(codex).to_contain_text('환산 $3,600 · 월 구독료 $200 · 4.5B 토큰')
    expect(codex).to_contain_text('18.0배')
    expect(monthly).to_contain_text('환산 $20 · 월 구독료 미설정 · 100M 토큰')
    expect(monthly).to_contain_text('Codex 토큰에는 이 PC에 기록이 없는')
    expect(monthly).to_contain_text('09. 01. 09:30부터 보존된 이력')
    expect(monthly.locator(':scope > .table-wrap .quota-use tbody tr')).to_have_count(1)
    trend = monthly.locator('.month-trend tbody tr')
    expect(trend).to_have_count(2)
    expect(trend.first).to_contain_text('Claude 5.0배 · Codex 18.0배')
    expect(trend.nth(1)).to_contain_text('2026-08 부분')
    older = monthly.locator('.report-older')
    expect(older.locator(':scope > summary')).to_have_text('이전 결산 1개')
    older.locator(':scope > summary').click()
    older.locator('details > summary').click()
    expect(older).to_contain_text('부분 월(기록 08-20부터)')
    expect(older).to_contain_text('이 달의 한도 이력은 보존 기간이 지나')
    # Account names that arrive after the settlements were drawn redraw them with the names.
    page.evaluate("window.shownAccounts=claudeAccounts.list;setClaudeAccounts([{route:'claude-code',label:'Pro'},"
                  "{route:'claude-code:abababababab',label:'두 번째'}])&&redrawReports()")
    expect(monthly.locator(':scope > .sub-row', has_text='Anthropic / Claude Code · Pro')).to_have_count(1)
    page.evaluate("setClaudeAccounts(window.shownAccounts)&&redrawReports()")
    expect(monthly.locator(':scope > .sub-row', has_text='Anthropic / Claude Code · Pro')).to_have_count(0)
    page.evaluate("renderMonths([])")
    expect(monthly).to_contain_text('첫 결산은 수집기가 다음 달 첫 수집 주기에')
    page.evaluate("renderMonths(%s)" % json.dumps(MONTHS))
    # Settings: secrets are write-only; save, clear and test go through the API only.
    page.locator('#settings-open').click()
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
    # The weekly report tables fit a phone: short service names, no request or observed column.
    expect(mobile.locator('#reports .quota-use th.col-req')).to_be_hidden()
    table = mobile.locator('#reports .report-table').first
    assert table.locator('tbody td').first.inner_text() == 'Codex', table.locator('tbody td').first.inner_text()
    expect(table.locator('th.col-req')).to_be_hidden()
    assert mobile.evaluate("[...document.querySelectorAll('#reports .table-wrap')].every(w=>w.scrollWidth<=w.clientWidth)")
    assert mobile.evaluate('document.documentElement.scrollWidth<=innerWidth')
    mobile.screenshot(path=str(output/'features-mobile.png'), full_page=True)
    assert not [p for p in posts if p[0] not in ('/api/config/notify', '/api/notify/test', '/api/refresh', '/api/config/project-budgets')], posts
    assert not errors, errors
    mobile.unroute_all(behavior='ignoreErrors')
    browser.close()
print('feature browser checks passed')
