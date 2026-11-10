"""개요 on synthetic data: the default view, what each subscription can be used for now, period
tiles and ranked rows that open the usage tab on the same number, observed facts, the 30-day
chart, the saved-defaults move to 개요, an offline copy and 390px."""
from browser_support import watch_csp, fixture_page, fixture_json, artifact_directory
import argparse
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright, expect

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from llm_usage.store import Store  # noqa: E402

parser = argparse.ArgumentParser()
parser.add_argument('--artifacts', type=Path)
args = parser.parse_args()
origin = 'https://dashboard.test'
output = artifact_directory(args.artifacts)

# The last full KST week, which 개요 calls 지난주; an older stored week goes by its date.
_today = datetime.now(timezone(timedelta(hours=9))).date()
LAST_WEEK = (_today - timedelta(days=_today.weekday() + 7)).isoformat()
FACTS = [dict(kind='exhausted', week=LAST_WEEK, route='codex', bucket='codex · 10080분', display_name='주간', count=2, streak=3),
         dict(kind='zero_hours', week=LAST_WEEK, route='codex', bucket='codex · 10080분', display_name='주간', hours=12.5),
         dict(kind='service_change', week=LAST_WEEK, route='claude-code', change_pct=150.0, tokens=10),
         dict(kind='idle_subscription', route='devin', monthly_usd=20, collected=False),
         dict(kind='value_below_price', month='2026-10', route='opencode-go', ratio=0.4, value=4, monthly_usd=10),
         dict(kind='peak_hours', start_hour=14, end_hour=17, share=31.2)]
MONTH = dict(month='2026-10', partial=False, price=120, value=600, ratio=5.0, subscriptions=[])


def limits_fixture():
    now = time.time()
    with tempfile.TemporaryDirectory() as folder:
        store = Store(Path(folder)/'usage.db')
        with store.connect() as c:
            # Claude: the 5-hour window is held by the exhausted weekly window.
            store.limit(c, 'claude-code', 'five_hour', 100.0, now+3*3600, now-60, 'claude-oauth')
            store.limit(c, 'claude-code', 'seven_day', 0.0, now+6*3600, now-60, 'claude-oauth')
            store.limit(c, 'codex', 'codex · 10080분', 42.0, now+2*86400, now-60, 'codex')
            # Antigravity: Gemini has room while 3rd party models are out.
            store.limit(c, 'antigravity', 'gemini-5h', 60.0, now+4*3600, now-60, 'antigravity-app')
            store.limit(c, 'antigravity', '3p-5h', 0.0, now+2*3600, now-60, 'antigravity-app')
            for name in ('claude-oauth', 'codex', 'antigravity-app'):
                store.source(c, name, 'ok', 'synthetic', now-60)
        return store.limits(now)


def preview(page, offline=False):
    limits = limits_fixture()

    def serve(route):
        path = urlsplit(route.request.url).path
        if offline and path.startswith('/api/'):
            route.abort()
        elif path == '/api/limits':
            route.fulfill(json=limits)
        elif path == '/api/overview':
            data = fixture_json(route.request)
            # The synthetic records all fall at one time of day; the listed peak hours stand in for theirs.
            data['facts'] = FACTS+[fact for fact in data['facts'] if fact['kind'] != 'peak_hours']
            data['month'] = MONTH
            route.fulfill(json=data)
        else:
            route.fallback()
    page.route(origin+'/**', serve)


def open_page(context, offline=False):
    page = context.new_page()
    fixture_page(page)
    page.on('pageerror', lambda error: errors.append(str(error))); watch_csp(page, errors)
    preview(page, offline)
    return page


def loaded(page):
    expect(page.locator('#updated')).to_contain_text('마지막 갱신')
    expect(page.locator('#ov-tiles .ov-tile').first).to_be_visible()


errors = []
with sync_playwright() as p:
    browser = p.chromium.launch()
    context = browser.new_context(service_workers='block', viewport={'width': 1440, 'height': 1000})
    page = open_page(context)
    assert page.goto(origin+'/').status == 200
    loaded(page)
    # 개요 is the first tab, the default view and a bare URL.
    expect(page.locator('#tab-overview')).to_have_attribute('aria-selected', 'true')
    expect(page.locator('#view-quota')).to_be_hidden()
    assert 'view=' not in page.url, page.url
    # The README image (docs/assets/dashboard.png): the default view as it first opens, down to
    # the end of the chart and facts row, before any row takes focus or the chart a pointer.
    expect(page.locator('#ov-chart .ov-hit')).to_have_count(30)
    expect(page.locator('#ov-facts .ov-fact').nth(5)).to_be_visible()
    bottom = page.evaluate("Math.ceil(Math.max(...[...document.querySelector('#ov-chart-title').closest('.ov-grid').children]"
                           ".map(panel=>panel.getBoundingClientRect().bottom))+scrollY)")
    page.screenshot(path=str(output/'dashboard.png'), full_page=True, clip={'x': 0, 'y': 0, 'width': 1440, 'height': bottom+16})

    # What can be used now: a held window leads with 사용 불가 and the window that holds it;
    # each Antigravity model family has its own row.
    rows = page.locator('#ov-quota .ov-q')
    claude = rows.filter(has_text='Claude')
    expect(claude.locator('.ov-q-value')).to_have_text('사용 불가')
    expect(claude.locator('.ov-q-name small')).to_have_text('주간 한도 소진')
    expect(claude.locator('.ov-q-reset')).to_contain_text('후 초기화')
    assert claude.locator('.ov-q-meter i').evaluate('i=>i.getBoundingClientRect().width') == 0
    expect(rows.filter(has_text='Codex').locator('.ov-q-value')).to_have_text('42%')
    expect(rows.filter(has_text='Antigravity Gemini').locator('.ov-q-value')).to_have_text('60%')
    expect(rows.filter(has_text='Antigravity 3rd party').locator('.ov-q-value')).to_have_text('사용 불가')
    width = rows.filter(has_text='Codex').locator('.ov-q-meter i').evaluate('i=>i.getBoundingClientRect().width/i.parentElement.getBoundingClientRect().width')
    assert abs(width-0.42) < 0.01, width
    # The rows are redrawn as the readings age; a focused row keeps the focus.
    key = rows.nth(1).get_attribute('data-ov-key')
    rows.nth(1).focus()
    moved = page.evaluate("()=>{const before=document.activeElement;ageLimits();return !before.isConnected&&document.activeElement.dataset.ovKey}")
    assert moved == key, (moved, key)

    # Tiles: today and this week against the period before, last month's settled value.
    tiles = page.locator('#ov-tiles .ov-tile')
    expect(tiles).to_have_count(4)
    overview = page.evaluate('lastOverview')
    expect(tiles.nth(0).locator('.stat-value')).to_have_text(page.evaluate('compact(lastOverview.today.tokens)'))
    expect(tiles.nth(1)).to_contain_text('지난주')
    expect(tiles.nth(3)).to_contain_text('10월 구독 가치')
    expect(tiles.nth(3)).to_contain_text('구독료 $120의 5.0배')

    # Observed facts read as sentences, without forecasts or advice.
    facts = page.locator('#ov-facts .ov-fact')
    expect(facts.nth(0)).to_contain_text('지난주 Codex 주간 한도 2회 소진 · 3주 연속')
    expect(facts.nth(1)).to_contain_text('0%로 머문 시간 12.5시간')
    expect(facts.nth(2)).to_contain_text('Claude 사용량 그 전 주의 2.5배')
    expect(facts.nth(3)).to_contain_text('Devin 최근 30일 수집된 기록 없음 · 월 $20')
    expect(facts.nth(4)).to_contain_text('10월 OpenCode Go 공개 단가 환산 $4 · 구독료 $10의 0.4배')
    expect(facts.nth(5)).to_contain_text('사용이 가장 많은 시간대 14–17시 · 최근 30일의 31%')
    # A week before last (its report not built yet) names its Monday instead of 지난주.
    older = page.evaluate("factText({kind:'zero_hours',week:'2026-01-05',route:'codex',bucket:'codex · 10080분',display_name:'주간',hours:2})")
    assert older == '1월 5일 주 Codex 주간 한도가 0%로 머문 시간 2시간', older
    text = page.locator('#view-overview').inner_text()
    for word in ('예상', '전망', '추천', '전환하', '권장'):
        assert word not in text, word

    # The 30-day chart: one hit area per day, a tooltip with the day's total, service colors.
    hits = page.locator('#ov-chart .ov-hit')
    expect(hits).to_have_count(30)
    hits.last.hover()
    expect(page.locator('#tip')).to_contain_text('합계')
    legend = page.locator('#ov-legend .ov-key')
    assert legend.count() == len(overview['recent']['services'])
    assert page.locator('#ov-chart text').evaluate_all('ts=>ts.at(-1).textContent') == page.evaluate(
        "(()=>{const d=lastOverview.recent.days.at(-1);return Number(d.slice(5,7))+'/'+Number(d.slice(8,10));})()")
    page.screenshot(path=str(output/'overview-desktop.png'), full_page=True)

    # A project row opens the usage tab over the same 30 days, on the same number.
    project = overview['projects'][0]
    page.locator(f'#ov-projects [data-scope="project:{project["name"]}"]').click()
    expect(page.locator('#tab-usage')).to_have_attribute('aria-selected', 'true')
    expect(page.locator('#period')).to_have_value('30d')
    expect(page.locator('#scope')).to_have_value('project:'+project['name'])
    page.wait_for_function('()=>lastUsage&&lastUsage.scope===%r' % ('project:'+project['name']))
    assert page.evaluate('total(lastUsage.totals)') == project['tokens']
    # The drill lands on the same estimate, and the composition panel splits it by token type.
    assert abs(page.evaluate('lastUsage.cost.period')-project['cost']) < 1e-6
    mix = page.locator('#composition .cost-mix')
    expect(mix).to_contain_text('비용 구성')
    assert mix.locator('.cost-mix-bar rect').count() == page.evaluate('Object.values(lastUsage.cost.by_type).filter(v=>v>0).length')
    assert abs(page.evaluate("[...document.querySelectorAll('.cost-mix-key b')].reduce((n,b)=>n+parseFloat(b.textContent),0)")-100) < 0.3
    expect(page.locator('#rows tr td:last-child small').first).to_contain_text('1M당 $')
    # The fixture's records state no reasoning effort: the panel says so instead of listing zeros.
    expect(page.locator('#efforts')).to_contain_text('추론 수준이 기록된 요청이 없습니다')
    assert page.locator('#group option[value=effort]').count() == 1
    page.go_back(); expect(page.locator('#tab-overview')).to_have_attribute('aria-selected', 'true')
    # Ranked rows carry their estimate, and the cost fact opens its model over the same 30 days.
    expect(page.locator('#ov-models .rank-cost').first).to_have_text('· '+page.evaluate('usd(lastOverview.models[0].cost)'))
    cost_fact = next(f for f in overview['facts'] if f['kind'] == 'cost_share')
    page.locator('#ov-facts .ov-fact[data-scope]').click()
    expect(page.locator('#scope')).to_have_value('model:'+cost_fact['model'])
    page.wait_for_function('()=>lastUsage&&lastUsage.scope===%r' % ('model:'+cost_fact['model']))
    assert abs(page.evaluate('lastUsage.cost.period')-cost_fact['cost']) < 1e-6
    page.go_back(); expect(page.locator('#tab-overview')).to_have_attribute('aria-selected', 'true')
    # The today tile does the same for today.
    page.locator('#ov-tiles .ov-tile').first.click()
    expect(page.locator('#period')).to_have_value('today')
    page.wait_for_function("()=>lastUsage&&lastUsage.period==='today'&&!lastUsage.scope")
    assert page.evaluate('total(lastUsage.totals)') == overview['today']['tokens']
    # The week tile opens Monday to Sunday (KST) and names both ends as dates of that week.
    page.keyboard.press('1'); expect(page.locator('#tab-overview')).to_have_attribute('aria-selected', 'true')
    page.locator('#ov-tiles .ov-tile').nth(1).click()
    expect(page.locator('#period')).to_have_value('custom')
    page.wait_for_function("()=>lastUsage&&lastUsage.period==='custom'")
    monday = overview['week']['start'][:10]
    sunday = page.evaluate("s=>new Date(Date.parse(s+'T00:00:00Z')+6*864e5).toISOString().slice(0,10)", monday)
    expect(page.locator('#cards-period')).to_contain_text(f'{monday} ~ {sunday}')
    # A quota row opens its card on the quota tab.
    page.keyboard.press('1'); expect(page.locator('#tab-overview')).to_have_attribute('aria-selected', 'true')
    page.locator('#ov-quota .ov-q').filter(has_text='Codex').click()
    expect(page.locator('#tab-quota')).to_have_attribute('aria-selected', 'true')
    expect(page.locator('#quota-codex')).to_be_visible()
    page.close()

    # Defaults saved before 개요 existed open on it once; a later choice is kept.
    page = open_page(context)
    page.goto(origin+'/?view=quota'); expect(page.locator('#updated')).to_contain_text('마지막 갱신')
    page.evaluate("storage.set('llmDefaults',JSON.stringify({view:'quota',period:'7d'}))")
    page.goto(origin+'/'); loaded(page)
    expect(page.locator('#tab-overview')).to_have_attribute('aria-selected', 'true')
    page.keyboard.press('2'); expect(page.locator('#tab-quota')).to_have_attribute('aria-selected', 'true')
    page.goto(origin+'/'); expect(page.locator('#updated')).to_contain_text('마지막 갱신')
    expect(page.locator('#tab-quota')).to_have_attribute('aria-selected', 'true')
    page.close()

    # Offline: the last overview answer kept on this device is shown and says how old it is.
    page = open_page(context)
    page.goto(origin+'/?view=overview'); loaded(page)
    assert page.evaluate("""async()=>{for(let i=0;i<50;i++){const c=await caches.open(OFFLINE_CACHE);
        if(await c.match('/api/overview'))return true;await new Promise(r=>setTimeout(r,100));}return false;}""")
    shown = page.locator('#ov-tiles .ov-tile').first.locator('.stat-value').inner_text()
    page.close()
    page = open_page(context, offline=True)
    page.goto(origin+'/?view=overview')
    expect(page.locator('#offline')).to_be_visible()
    expect(page.locator('#ov-tiles .ov-tile').first.locator('.stat-value')).to_have_text(shown)
    page.close()
    context.close()

    # Phones: rows stack, four tabs fit the bottom bar and nothing scrolls sideways.
    context = browser.new_context(service_workers='block', viewport={'width': 390, 'height': 844}, is_mobile=True, has_touch=True)
    page = open_page(context)
    page.goto(origin+'/'); loaded(page)
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
    tabs = page.locator('#tabs [role=tab]')
    expect(tabs).to_have_count(4)
    assert page.evaluate("[...document.querySelectorAll('#tabs [role=tab]')].every(t=>t.scrollWidth<=t.clientWidth+1)")
    assert page.locator('#ov-chart svg').evaluate('s=>s.getBoundingClientRect().right<=innerWidth')
    page.screenshot(path=str(output/'overview-mobile.png'), full_page=True)
    context.close()
    assert not errors, errors
    browser.close()
print('Overview passed: default view, usable-now rows, tiles and rows matching the usage tab, facts, 30-day chart, saved defaults, offline copy and 390px.')
