"""Readability fixes on synthetic data: one color per service across charts, a short default
model table, each reset on its own row, severity-ordered alerts, the blocked-window headline,
separated period-card lines, a capped consumption palette and the report loading state."""
from browser_support import CSP_HEADERS, watch_csp, fixture_page, fixture_json, artifact_directory
import argparse
import re
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


def limits_fixture():
    now = time.time()
    with tempfile.TemporaryDirectory() as folder:
        store = Store(Path(folder)/'usage.db')
        with store.connect() as c:
            # Claude: the 5-hour window is full but held by the exhausted weekly window.
            store.limit(c, 'claude-code', 'five_hour', 100.0, now+3*3600, now-60, 'claude-oauth')
            store.limit(c, 'claude-code', 'seven_day', 0.0, now+6*3600, now-60, 'claude-oauth')
            store.limit(c, 'codex', 'codex · 10080분', 0.0, now+2*86400, now-60, 'codex')
            store.limit(c, 'antigravity', 'gemini-5h', 60.0, now+4*3600, now-60, 'antigravity-app')
            for name in ('claude-oauth', 'codex', 'antigravity-app'):
                store.source(c, name, 'ok', 'synthetic', now-60)
        return store.limits(now)


def preview(page, held_reports=None):
    limits = limits_fixture()

    def serve(route):
        path = urlsplit(route.request.url).path
        if path == '/api/limits':
            route.fulfill(json=limits)
        elif path == '/api/usage':
            data = fixture_json(route.request)
            data['spike'] = dict(hour_start=time.time()-3600, tokens=123_000_000, ratio=1.3, baseline=90_000_000, current=False)
            data['unpriced_models'] = []
            route.fulfill(json=data)
        elif path == '/api/reports' and held_reports is not None:
            held_reports.append(route)  # answered later, so the loading state can be read
        elif args.preview_assets and path == '/':
            route.fulfill(path=args.preview_assets.resolve()/'index.html', content_type='text/html', headers=CSP_HEADERS)
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
    page = browser.new_page(service_workers='block', viewport={'width': 1440, 'height': 1000})
    fixture_page(page)
    page.on('pageerror', lambda error: errors.append(str(error))); watch_csp(page, errors)
    preview(page)
    assert page.goto(origin+'/?view=quota').status == 200
    expect(page.locator('#updated')).to_contain_text('마지막 갱신')

    # A window held by its exhausted parent leads with 사용 불가 over an empty bar.
    bucket = lambda label: page.locator('#quota-claude-code .bucket').filter(has=page.locator('.bucket-label', has_text=re.compile('^'+label+'$'))).first
    held = bucket('5시간')
    expect(held.locator('.remaining')).to_contain_text('사용 불가')
    expect(held.locator('.remaining')).to_contain_text('창 잔여 100%')
    assert held.locator('progress').evaluate('e=>e.value') == 0
    expect(held.locator('.badge.low')).to_have_text('주간 소진')
    # An observed 0% reads 소진 on the card and on the reset timeline alike.
    weekly = bucket('주간')
    expect(weekly.locator('.badge.low')).to_have_text('소진')
    expect(page.locator('#quota-codex .badge.low')).to_have_text('소진')

    # Every reset sits on its own row, with its own marker on the shared 7-day scale.
    items = page.locator('#reset-timeline .reset-item')
    assert items.count() >= 3
    for index in range(items.count()):
        item = items.nth(index)
        expect(item.locator('.reset-dot')).to_have_count(1)
        left = item.locator('.reset-dot').evaluate('d=>parseFloat(d.style.left)')
        assert 0 < left <= 100, left
    codex_row = items.filter(has_text='Codex')
    expect(codex_row.locator('.badge.low')).to_have_text('소진')
    expect(codex_row.locator('.reset-dot.low')).to_have_count(1)
    expect(page.locator('#reset-timeline')).not_to_contain_text('소진 중')
    # The scale's ticks and the rows' tracks start at the same x.
    scale = page.locator('#reset-timeline .reset-axis').bounding_box()
    track = items.first.locator('.reset-track').bounding_box()
    assert abs(scale['x']-track['x']) < 1 and abs(scale['width']-track['width']) < 1, (scale, track)

    # Alerts: exhausted quotas first, the usage spike last, each marked by severity.
    chips = page.locator('#alerts .alert-chip')
    levels = chips.evaluate_all("cs=>cs.map(c=>[...c.classList].find(n=>n.startsWith('sev-')))")
    assert levels and levels[0] == 'sev-critical' and levels[-1] == 'sev-info', levels
    assert levels == sorted(levels, key=['sev-critical', 'sev-warning', 'sev-info'].index), levels
    expect(chips.last).to_contain_text('사용량 급증')
    assert chips.first.evaluate('c=>getComputedStyle(c).backgroundColor') != chips.last.evaluate('c=>getComputedStyle(c).backgroundColor')

    # Usage: each period-card caption is its own line.
    page.locator('#tab-usage').click()
    expect(page.locator('#cards .stat').first).to_be_visible()
    displays = page.locator('#cards .stat small').evaluate_all('els=>els.map(e=>getComputedStyle(e).display)')
    assert displays and set(displays) == {'block'}, displays

    # A model's ranking bar wears its service's color from the trend legend.
    expect(page.locator('#group')).to_have_value('route')
    legend = page.locator('#legend [data-label]').evaluate_all(
        "els=>Object.fromEntries(els.map(e=>[e.dataset.label,e.style.getPropertyValue('--color').trim().toLowerCase()]))")
    ranked = page.evaluate("""()=>[...document.querySelectorAll('#ranking .rank[data-scope]')].map(el=>{
        const model=el.dataset.scope.slice(6),row=lastUsage.rows.find(r=>r.model===model);
        return [familyOf(row.route),el.querySelector('rect').getAttribute('fill').toLowerCase()];})""")
    assert ranked, ranked
    for route, fill in ranked:
        assert legend.get(route) == fill, (route, fill, legend)

    # The model table fits without its token breakdown; 상세 열 is kept on this device.
    panel = page.locator('[data-panel="models"]')
    if panel.get_attribute('open') is None: panel.locator('summary').click()
    expect(page.locator('#model-table th.col-detail').first).to_be_hidden()
    expect(page.locator('#cols-toggle')).to_be_visible()
    assert page.evaluate("()=>{const w=document.querySelector('[data-panel=\"models\"] .table-wrap');return w.scrollWidth<=w.clientWidth+1;}")
    page.locator('#cols-toggle').click()
    expect(page.locator('#model-table th.col-detail').first).to_be_visible()
    page.reload(); expect(page.locator('#updated')).to_contain_text('마지막 갱신')
    if panel.get_attribute('open') is None: panel.locator('summary').click()
    expect(page.locator('#cols-toggle')).to_have_attribute('aria-pressed', 'true')
    expect(page.locator('#model-table th.col-detail').first).to_be_visible()
    page.locator('#cols-toggle').click()
    expect(page.locator('#model-table th.col-detail').first).to_be_hidden()

    # Model changes come first; the total is one line on the per-request records.
    comparison = page.locator('#comparison')
    expect(comparison.locator('.comparison-total')).to_contain_text('세부 요청 기록 합계')
    assert comparison.evaluate("c=>{const k=[...c.children].map(e=>e.className);return k.indexOf('contributions')<k.indexOf('comparison-total');}")

    # Consumption: seven categories keep a series color, the rest share the muted 기타.
    palette = page.evaluate("""()=>{
        const day={attribution:Array.from({length:10},(_,i)=>({thread_source:'source-'+i,value:10-i}))};
        const p=consumptionColors([day],'feature'),kept=p.legend.filter(l=>l!=='기타');
        return {legend:p.legend,kept:kept.map(p.color),folded:p.color('source-9'),muted:mutedColor};}""")
    assert len(palette['legend']) == 8 and palette['legend'][-1] == '기타', palette
    assert len(set(palette['kept'])) == 7 and palette['muted'] not in palette['kept'], palette
    assert palette['folded'] == palette['muted'], palette
    page.screenshot(path=str(output/'readability-desktop.png'), full_page=True)
    page.close()

    # Reports say they are loading while the first answer is pending.
    held_reports = []
    page = browser.new_page(service_workers='block', viewport={'width': 1440, 'height': 1000})
    fixture_page(page)
    page.on('pageerror', lambda error: errors.append(str(error))); watch_csp(page, errors)
    preview(page, held_reports)
    assert page.goto(origin+'/?view=reports').status == 200
    expect(page.locator('#reports')).to_contain_text('불러오는 중')
    expect(page.locator('#monthly')).to_contain_text('불러오는 중')
    for _ in range(50):
        if held_reports: break
        page.wait_for_timeout(100)
    assert held_reports, 'reports request was not held'
    held_reports[0].fallback()
    expect(page.locator('#reports')).not_to_contain_text('불러오는 중')
    page.close()

    # Phones: the rows stay readable without the scale, and nothing scrolls sideways.
    page = browser.new_page(service_workers='block', viewport={'width': 390, 'height': 844}, is_mobile=True, has_touch=True)
    fixture_page(page)
    page.on('pageerror', lambda error: errors.append(str(error))); watch_csp(page, errors)
    preview(page)
    assert page.goto(origin+'/?view=quota').status == 200
    expect(page.locator('#updated')).to_contain_text('마지막 갱신')
    expect(page.locator('#reset-timeline .reset-item').first).to_be_visible()
    expect(page.locator('#reset-timeline .reset-track').first).to_be_hidden()
    expect(page.locator('#alerts-toggle')).to_have_class('sev-critical')
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
    page.screenshot(path=str(output/'readability-mobile.png'), full_page=True)
    assert not errors, errors
    browser.close()
print('Readability passed: service colors across charts, short model table, per-row resets, alert severity, blocked headline, card lines, consumption palette, report loading and 390px.')
