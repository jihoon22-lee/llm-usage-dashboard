"""The Codex account residual (account day totals minus this PC's records) as one item.

A disposable synthetic store and Flask app serve every request: no provider, account
file, collector, private origin or production database is consulted.
"""
from browser_support import Sock, watch_csp, artifact_directory
import argparse
import json
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright, expect

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from llm_usage import reports  # noqa: E402
from llm_usage.store import Store  # noqa: E402
from llm_usage.webapp import create_app  # noqa: E402

parser = argparse.ArgumentParser()
parser.add_argument('--artifacts', type=Path)
args = parser.parse_args()
output = artifact_directory(args.artifacts)
ORIGIN = 'https://dashboard.test'
RESIDUAL = '(account-residual)'
PRICING = {'claude-test': dict(input=3, cached=.3, output=15), 'gpt-test': dict(input=2, cached=.2, output=8)}
now = time.time()
folder = tempfile.TemporaryDirectory(prefix='llm-account-residual-')
root = Path(folder.name)
store = Store(root/'usage.db')
with store.connect() as c:
    for day in range(15):
        for i, (route, model, project, tokens) in enumerate((('codex', 'gpt-test', 'fixture-project', 7000), ('codex', 'gpt-test', None, 2000),
                                                            ('claude-code', 'claude-test', 'fixture-project', 4000))):
            store.event(c, f'e-{day}-{i}', now-1800-day*86400, 'Anthropic' if route == 'claude-code' else 'OpenAI', route, model,
                        dict(uncached_input=tokens, cached_input=tokens*4, output=tokens//5), session=f'session-{i}', project=project,
                        agent_kind='main')
    # The account's finished UTC days (09:00-09:00 KST) carry more than this PC recorded.
    days = [datetime.fromtimestamp(now-back*86400, timezone.utc).strftime('%Y-%m-%d') for back in range(1, 15)]
    store.account_tokens(c, 'codex', [dict(startDate=d, tokens=300_000) for d in days], now)
    store.source(c, 'codex-account-usage', 'ok', '계정 집계 범위 KST 09:00–다음 날 09:00', now-60)
    store.save_state(c, 'collector', dict(status='idle', checked=now-30))
    store.save_state(c, 'last_local', now-30)
reports.ensure_report(store, now, PRICING)
(root/'config.json').write_text('{}')
config = dict(database=str(root/'usage.db'), origin=ORIGIN, secret_key='fixture-key-'*4, allowed_logins=['fixture'],
              config_file=str(root/'config.json'), subscription_prices={'claude-code': 20, 'codex': 200}, pricing=PRICING)
client = create_app(config).test_client()


def serve(route):
    url = urlsplit(route.request.url)
    assert url.netloc == 'dashboard.test', route.request.url
    headers = {k: v for k, v in route.request.headers.items() if k.lower() in ('content-type', 'x-csrf-token', 'origin')}
    response = client.open(url.path+('?'+url.query if url.query else ''), method=route.request.method, data=route.request.post_data,
                           headers={**headers, 'Tailscale-User-Login': 'fixture', 'Host': 'dashboard.test'},
                           environ_base={'gunicorn.socket': Sock()})
    try:
        assert response.status_code < 400, (route.request.url, response.status_code)
        route.fulfill(status=response.status_code, body=response.get_data(),
                      headers={k: v for k, v in response.headers.items() if k.lower() != 'content-length'})
    finally:
        response.close()


def fits(page):
    return page.evaluate('document.documentElement.scrollWidth<=innerWidth')


with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page(service_workers='block', viewport={'width': 1440, 'height': 1000}, accept_downloads=True)
    page.route('**/*', serve)
    errors = []; page.on('pageerror', lambda e: errors.append(str(e))); watch_csp(page, errors)
    page.goto(ORIGIN+'/')
    expect(page.locator('#updated')).to_contain_text('마지막 갱신')
    page.locator('#tab-usage').click()
    page.select_option('#period', '30d')
    page.select_option('#group', 'model')
    page.select_option('#granularity', 'week')
    # Model table: the residual is the last row, unranked and not a model to narrow to.
    residual_row = page.locator('#rows tr.residual-row')
    expect(residual_row).to_have_count(1)
    expect(residual_row).to_contain_text('로컬 미기록')
    expect(residual_row).to_contain_text('모델 구분 없음')
    expect(residual_row.locator('.row-rank')).to_have_text('—')
    assert residual_row.get_attribute('data-scope') is None
    expect(page.locator('#rows tr').last).to_have_class('residual-row')
    expect(page.locator(f'#rows tr[data-scope="model:{RESIDUAL}"]')).to_have_count(0)
    expect(page.locator('#rows')).not_to_contain_text(RESIDUAL)
    residual_row.click()
    expect(page.locator('#scope')).to_have_value('')
    # The chart keeps it as its own series under its name.
    expect(page.locator('#legend')).to_contain_text('로컬 미기록')
    expect(page.locator('#legend')).not_to_contain_text(RESIDUAL)
    # Ranking: shares of model-attributed use; the residual follows on its own line.
    expect(page.locator('#ranking-caption')).to_contain_text('비중은 모델이 기록된 사용량 기준')
    line = page.locator('#ranking .rank-residual')
    expect(line).to_contain_text('로컬 미기록')
    expect(line).to_contain_text('순위 제외')
    expect(page.locator(f'#ranking [data-scope="model:{RESIDUAL}"]')).to_have_count(0)
    # Token composition: its own muted item, left out of the ring by default.
    toggle = page.locator('#donut-noresidual')
    expect(toggle).to_be_visible()
    expect(toggle).to_have_text('로컬 미기록 제외')
    expect(toggle).to_have_attribute('aria-pressed', 'true')
    key = page.locator('#composition [data-token="residual_tokens"]')
    expect(key).to_have_class('excluded')
    expect(key).to_contain_text('차트 제외')
    expect(page.locator('#composition [data-token="unclassified_tokens"]')).to_have_count(0)
    expect(page.locator('#composition svg.donut')).to_have_attribute('aria-label', '캐시 읽기·로컬 미기록 제외 비중순 토큰 구성 도넛 차트')
    expect(page.locator('#composition svg.donut')).to_contain_text('2개 항목 제외')
    toggle.click()
    expect(toggle).to_have_attribute('aria-pressed', 'false')
    expect(key).not_to_have_class('excluded')
    expect(page.locator('#composition svg.donut')).to_contain_text('캐시 읽기 제외')
    toggle.click()
    # Projects: below the projects, without drill-down or detail; '미분류' stays a project.
    projects = page.locator('#projects')
    expect(projects.locator('tr.residual-row')).to_contain_text('로컬 미기록')
    expect(projects.locator('tr.residual-row .proj-open')).to_have_count(0)
    expect(projects.locator(f'tr[data-scope="project:{RESIDUAL}"]')).to_have_count(0)
    expect(projects.locator('tr[data-scope="project:미분류"]')).to_have_count(1)
    expect(page.locator('#scope optgroup[label="모델"] option', has_text=RESIDUAL)).to_have_count(0)
    # Narrowing to the '미분류' project leaves the residual out of its totals and tables.
    projects.locator('tr[data-scope="project:미분류"]').click()
    expect(page.locator('#scope')).to_have_value('project:미분류')
    expect(page.locator('#cards .stat', has_text='전체 토큰').locator('.stat-value')).to_have_attribute('data-tip', '156,000')
    expect(page.locator('#rows tr.residual-row')).to_have_count(0)
    expect(projects.locator('tr.residual-row')).to_have_count(0)
    page.locator('#scope-chip').click()
    expect(page.locator('#scope')).to_have_value('')
    expect(page.locator('#rows tr.residual-row')).to_have_count(1)
    with page.expect_download() as download:
        page.locator('#csv-export').click()
    models_csv = Path(download.value.path()).read_text(encoding='utf-8-sig')
    assert '로컬 미기록' in models_csv and RESIDUAL not in models_csv, models_csv
    page.screenshot(path=str(output/'residual-usage-1440.png'), full_page=True)
    # Requests and cost: the residual has neither, so no value and no bar.
    page.select_option('#metric', 'requests')
    expect(line).to_contain_text('요청 수 없음')
    expect(line.locator('svg')).to_have_count(0)
    page.select_option('#metric', 'tokens')
    # The weekly report, drawn before the name changes, is drawn again with the new name.
    page.locator('#tab-reports').click()
    expect(page.locator('#reports')).to_contain_text('Codex 로컬 미기록')
    # Settings: the user names it; reports, the status tab and the views follow.
    page.locator('#settings-open').click()
    label = page.locator('#cfg-residual-label')
    expect(label).to_have_attribute('placeholder', '로컬 미기록')
    label.fill('자동 리뷰')
    page.locator('#cfg-subs-save').click()
    expect(page.locator('#cfg-subs-msg')).to_contain_text('저장했습니다')
    assert json.loads((root/'local.json').read_text())['residual_label'] == '자동 리뷰'
    page.screenshot(path=str(output/'residual-settings-1440.png'), full_page=True)
    page.locator('#settings-back').click()
    page.locator('#tab-usage').click()
    expect(residual_row).to_contain_text('자동 리뷰')
    expect(toggle).to_have_text('자동 리뷰 제외')
    expect(page.locator('#legend')).to_contain_text('자동 리뷰')
    page.locator('#tab-reports').click()
    expect(page.locator('#reports')).to_contain_text('Codex 자동 리뷰')
    expect(page.locator('#reports')).to_contain_text('계정 일별 합계가 주 안에 온전히 든 6일 기준')
    expect(page.locator('#reports .report-head').first).to_be_visible()
    page.locator('#tab-status').click()
    expect(page.locator('#account-gap summary')).to_contain_text('Codex 계정 합계 대비 자동 리뷰')
    expect(page.locator('#account-gap th').last).to_have_text('자동 리뷰')
    # Mobile: the residual rows and line fit without page overflow.
    page.set_viewport_size({'width': 390, 'height': 844})
    page.reload()
    page.locator('#tab-usage').click()
    # Folded on phones: open the panels that show the residual.
    for inner in ('#composition', '#ranking', '#rows', '#projects'):
        page.locator(f'details.panel:has({inner}) > summary').click()
    expect(page.locator('#rows tr.residual-row')).to_be_visible()
    expect(page.locator('#rows tr.residual-row')).to_contain_text('자동 리뷰')
    expect(page.locator('#ranking .rank-residual')).to_be_visible()
    expect(page.locator('#donut-noresidual')).to_be_visible()
    expect(page.locator('#projects tr.residual-row')).to_be_visible()
    assert fits(page)
    page.screenshot(path=str(output/'residual-usage-390.png'), full_page=True)
    page.locator('#tab-reports').click()
    expect(page.locator('#reports')).to_contain_text('Codex 자동 리뷰')
    assert fits(page)
    page.screenshot(path=str(output/'residual-reports-390.png'), full_page=True)
    assert not errors, errors
    browser.close()
folder.cleanup()
print('Account residual browser check passed: one named item in models, ranking, composition, projects, CSV, settings, reports and status.')
