"""Portable Chromium regression checks using disposable synthetic data and local assets."""
from browser_support import CSP_HEADERS, watch_csp, fixture_page, fixture_json, artifact_directory
import argparse
from urllib.parse import urlsplit
from pathlib import Path
from playwright.sync_api import sync_playwright,expect
from llm_usage.notify import notification_url

parser=argparse.ArgumentParser()
parser.add_argument('--preview-assets',type=Path)
parser.add_argument('--artifacts',type=Path)
parser.add_argument('--origin',help='Explicit HTTPS origin for optional read-only live smoke; never used in CI')
args=parser.parse_args()
if args.origin:
    try:
        parsed=notification_url(args.origin)
        if parsed.path not in ('','/') or '?' in args.origin or '#' in args.origin:
            raise ValueError
    except ValueError:
        parser.error('--origin must be a complete HTTPS origin without credentials, path, query or fragment')
    live=args.origin.rstrip('/')
    with sync_playwright() as p:
        browser=p.chromium.launch()
        page=browser.new_page(service_workers='block',viewport={'width':1440,'height':1000})
        def read_only(route):
            request=route.request
            destination=urlsplit(request.url)
            if request.method!='GET' or destination.scheme!='https' or destination.hostname!=parsed.hostname or (destination.port or 443)!=(parsed.port or 443):
                route.abort('blockedbyclient')
            else:route.continue_()
        page.route('**/*',read_only)
        errors=[];page.on('pageerror',lambda e:errors.append(str(e)));watch_csp(page,errors)
        assert page.goto(live+'/').status==200
        expect(page.locator('#updated')).to_contain_text('마지막 갱신')
        page.locator('#tab-usage').click()
        expect(page.locator('#cards .stat').first).to_be_visible()
        expect(page.locator('#chart > svg')).to_be_visible()
        page.set_viewport_size({'width':390,'height':844})
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
        assert not errors,errors
        browser.close()
    print('Optional live read-only smoke passed; no provider collection or settings writes attempted.')
    raise SystemExit(0)
url='https://dashboard.test/'
artifacts=artifact_directory(args.artifacts)
artifacts.mkdir(exist_ok=True)
with sync_playwright() as p:
    browser=p.chromium.launch()
    page=browser.new_page(service_workers='block',viewport={'width':1440,'height':1000})
    fixture_page(page)
    errors=[];page.on('pageerror',lambda e:errors.append(str(e)));watch_csp(page,errors)
    if args.preview_assets:
        web=args.preview_assets.resolve()
        def serve(route):
            path=urlsplit(route.request.url).path
            if path=='/':
                route.fulfill(path=web/'index.html',content_type='text/html',headers=CSP_HEADERS)
            elif path.startswith('/assets/'):
                route.fulfill(path=web/Path(path).name,content_type='text/css' if path.endswith('.css') else 'text/javascript')
            else:
                route.fallback()
        # Match query strings too: view URLs like /?view=usage must serve preview HTML.
        page.route(url+'**',serve)
    assert page.goto(url).status==200
    expect(page.locator('#updated')).to_contain_text('마지막 갱신')
    # Quota is the default view: quota cards; period cards live on the usage tab.
    expect(page.locator('section[data-view="quota"]')).to_be_visible()
    expect(page.locator('#limits .limit-card').first).to_be_visible()
    expect(page.locator('#cards')).to_be_hidden()
    assert [b.inner_text() for b in page.locator('#tabs [role=tab]').all()][:5]==['한도','계획','사용량','리포트','상태']
    # Old view names in bookmarked URLs still land on the renamed tabs.
    for old,new in (('overview','quota'),('analysis','usage'),('insights','reports'),('sources','status')):
        page.goto(url+'?view='+old)
        expect(page.locator(f'#tab-{new}')).to_have_attribute('aria-selected','true')
        expect(page.locator(f'#view-{new}')).to_be_visible()
        if new!='quota':assert 'view='+new in page.url,page.url
    page.goto(url);expect(page.locator('#updated')).to_contain_text('마지막 갱신')
    # The forecast sits on the plan tab next to 지금 작업, visible without any click.
    page.locator('#tab-plan').click()
    expect(page.locator('#work-now')).to_be_visible()
    expect(page.locator('#work-budget')).to_be_visible()
    expect(page.locator('#plan-today-hours')).to_be_visible()
    page.locator('#tab-quota').click()
    # A panel order saved by an older layout (ids from several tabs) only reorders within each tab.
    page.evaluate("storage.set('llmOrder',JSON.stringify({panels:['calendar','projects','reports','cache','insights','trend','subvalue']}));applyPanelOrder()")
    assert page.evaluate("[...document.querySelectorAll('[data-panel]')].every(e=>e.parentElement===e.closest('main>[data-view]'))")
    assert page.evaluate("[...document.querySelectorAll('#view-reports>[data-panel]')].map(e=>e.dataset.panel)")==['calendar','reports','subvalue']
    assert page.evaluate("[...document.querySelectorAll('#view-usage>[data-panel]')].map(e=>e.dataset.panel)")[:3]==['projects','cache','trend']
    page.evaluate("storage.set('llmOrder','{}')");page.reload()
    expect(page.locator('#updated')).to_contain_text('마지막 갱신')
    # Dormant routes (ended or past quota_hide_days) fold into .quota-dormant;
    # every route still renders a card somewhere inside #limits.
    assert page.locator('#limits .limit-card').count()==5
    assert page.locator('#limits > .limit-card').count()>=3
    expect(page.locator('#limits > .quota-dormant > summary')).to_contain_text('관측 중단')
    # Tab bar switches views and syncs ?view= to the URL.
    page.locator('#tabs [data-view="usage"]').click()
    expect(page.locator('section[data-view="usage"]')).to_be_visible()
    assert 'view=usage' in page.url
    expect(page.locator('#cards .stat').first).to_be_visible()
    expect(page.locator('#chart > svg')).to_be_visible()
    expect(page.locator('#composition .donut')).to_be_visible()
    expect(page.locator('#ranking svg').first).to_be_visible()
    assert page.locator('#chart rect').count()>0
    # Check rendered arithmetic against the synthetic Flask response.
    expect(page.get_by_role('heading',name='모델별 사용량 상세',exact=True)).to_be_visible()
    cells=page.locator('#rows tr').first.locator('td').all_text_contents()
    values=[int(cells[i].replace(',','')) for i in (5,6,7,8,10)]
    assert values[3]==values[0]+values[1]+values[2]+values[4], 'Total = uncached + cached + output + cache creation'
    labels=page.evaluate('lastUsage.labels')
    page.locator('#chart > svg').focus()
    expect(page.locator('#chart-tooltip .tooltip-row:not(.tooltip-missing)')).to_have_count(min(len(labels),8))
    page.keyboard.press('Escape')
    # Metric picker and share modes render without refetch failures.
    page.locator('#metric').select_option('requests')
    expect(page.locator('#chart-caption')).to_contain_text('요청 수')
    page.locator('#cumulative').select_option('share-area')
    expect(page.locator('#chart svg path').first).to_be_visible()
    page.locator('#cumulative').select_option('share-line')
    expect(page.locator('#chart svg path').first).to_be_visible()
    page.locator('#metric').select_option('tokens');page.locator('#cumulative').select_option('0')
    # Compare overlay draws a dashed ghost line and a legend chip.
    with page.expect_response(lambda r:'/api/usage?' in r.url and 'compare=week' in r.url and r.status==200):
        page.locator('#compare').select_option('week')
    expect(page.locator('#chart svg path[stroke-dasharray]')).to_be_visible()
    expect(page.locator('#legend')).to_contain_text('7일 전 동일 구간')
    page.locator('#chart > svg').focus()
    expect(page.locator('#chart-tooltip')).to_contain_text('7일 전')
    page.keyboard.press('Escape')
    with page.expect_response(lambda r:'/api/usage?' in r.url and r.status==200):page.locator('#compare').select_option('')
    # Instant shared tooltip on the heatmap.
    page.locator('.heat-cell').first.dispatch_event('pointerover')
    expect(page.locator('#tip')).to_be_visible()
    page.screenshot(path=str(artifacts/'dashboard-desktop.png'),full_page=True)
    # Usage view also hosts the filter-dependent insight panels.
    expect(page.locator('#sessions tr').first).to_be_visible()
    expect(page.locator('#projects tr').first).to_be_visible()
    # Reports view: subscription value rows and the activity calendar.
    page.locator('#tabs [data-view="reports"]').click()
    assert 'view=reports' in page.url
    expect(page.locator('#subvalue .sub-row').first).to_be_visible()
    # Insights load lazily after the tab switch; wait for the calendar before
    # counting its cells.
    expect(page.locator('#calendar .cal-cell[data-tip]').first).to_be_visible()
    assert page.locator('#calendar .cal-cell[data-tip]').count()>90
    page.locator('#calendar .cal-cell[data-tip]').last.dispatch_event('pointerover')
    expect(page.locator('#tip')).to_be_visible()
    # Status view lists collectors.
    page.locator('#tabs [data-view="status"]').click()
    assert page.locator('#sources .source').count()>0
    # Settings is not a tab: the header gear opens it, no tab stays selected, and Back returns.
    expect(page.locator('#settings-open')).to_have_attribute('aria-label','설정')
    assert page.locator('#tabs [role=tab]').count()==5 and page.locator('#tab-settings').count()==0
    page.locator('#settings-open').click()
    assert 'view=settings' in page.url
    expect(page.locator('#view-settings')).to_be_visible()
    expect(page.locator('#tabs [aria-selected="true"]')).to_have_count(0)
    expect(page.locator('#view-settings #theme')).to_be_visible()  # the theme picker is the first settings block
    page.locator('#settings-back').click()
    expect(page.locator('#tab-status')).to_have_attribute('aria-selected','true')
    assert 'view=status' in page.url
    # The ',' shortcut opens settings too (outside text fields); keys 1-5 leave it again.
    page.locator('#settings-open').focus();page.keyboard.press(',')
    expect(page.locator('#view-settings')).to_be_visible()
    page.keyboard.press('3')
    expect(page.locator('#tab-usage')).to_have_attribute('aria-selected','true')
    page.keyboard.press(',')
    # Settings view renders config editors (read-only checks; no mutation on live).
    expect(page.locator('#view-settings')).to_be_visible()
    # A saved last view never points at settings; reopening the app lands on the tab left behind.
    assert page.evaluate("JSON.parse(localStorage.getItem('llmDefaults')).view")=='usage'
    expect(page.locator('#cfg-subs .cfg-row').first).to_be_visible()
    assert page.locator('#cfg-pricing tbody tr').count()>0
    assert page.locator('#cfg-thresholds input').count()==4
    expect(page.locator('#cfg-refresh')).to_be_visible()
    # Auto refresh and browser alerts moved here from the header.
    assert page.locator('.cfg-toggles input').count()==7
    # Back to usage for chart interactions.
    page.locator('#tabs [data-view="usage"]').click()
    page.locator('#cumulative').select_option('1')
    expect(page.locator('#chart path').first).to_be_visible()
    page.locator('#cumulative').select_option('0')
    page.locator('#legend button').first.click()
    expect(page.locator('#legend button').first).to_have_attribute('aria-pressed','false')
    page.locator('#legend button').first.click()
    # Answers now arrive within ~100 ms: wait for the previous refresh, and change to a
    # different value each time (re-selecting the current one fetches nothing new).
    expect(page.locator('#refresh')).to_be_enabled()
    for group in ['model','provider','project','agent','route']:
        expect(page.locator('#refresh')).to_be_enabled()
        with page.expect_response(lambda r:'/api/usage?' in r.url and r.status==200):page.locator('#group').select_option(group)
    for period in ['today','30d','all','custom','7d']:
        expect(page.locator('#refresh')).to_be_enabled()
        with page.expect_response(lambda r:'/api/usage?' in r.url and r.status==200):page.locator('#period').select_option(period)
        expect(page.locator('#refresh')).to_be_enabled()
    page.locator('#granularity').select_option('hour');expect(page.locator('#refresh')).to_be_enabled()
    page.locator('#granularity').select_option('day');expect(page.locator('#refresh')).to_be_enabled()
    page.set_viewport_size({'width':390,'height':844})
    page.screenshot(path=str(artifacts/'dashboard-mobile.png'),full_page=True)
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth'),'mobile overflow'
    # Five two-character tabs fit the bottom bar at 320px: one line each, no horizontal overflow.
    page.set_viewport_size({'width':320,'height':740})
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth'),'320px overflow'
    assert page.evaluate("(()=>{const t=document.querySelector('#tabs');return t.scrollWidth<=t.clientWidth})()"),'tab bar scrolls sideways'
    lines=page.evaluate("[...document.querySelectorAll('#tabs [role=tab]')].map(b=>{const r=document.createRange();r.selectNodeContents(b);return r.getClientRects().length})")
    assert lines==[1]*5,lines
    boxes=page.locator('#tabs [role=tab]').evaluate_all('bs=>bs.map(b=>{const r=b.getBoundingClientRect();return[r.left,r.right,r.height]})')
    assert all(0<=l and r<=320 and h<=64 for l,r,h in boxes),boxes
    page.set_viewport_size({'width':390,'height':844})
    page.clock.install()
    calls=[];page.on('request',lambda r:calls.append(r.url) if '/api/usage?' in r.url else None)
    page.locator('#settings-open').click()  # the toggle lives in settings
    page.locator('#auto').uncheck();page.locator('#auto').check()
    expect(page.get_by_role('checkbox',name='5분 자동 갱신',exact=True)).to_be_checked()
    page.clock.fast_forward(299000);assert len(calls)==0
    with page.expect_response(lambda r:'/api/usage?' in r.url):page.clock.fast_forward(2000)
    expect(page.locator('#refresh')).to_be_enabled()
    assert len(calls)==1
    page.evaluate("Object.defineProperty(document,'hidden',{configurable:true,value:true});document.dispatchEvent(new Event('visibilitychange'))")
    page.clock.fast_forward(600000);assert len(calls)==1
    page.locator('#auto').uncheck()
    page.evaluate("Object.defineProperty(document,'hidden',{configurable:true,value:false});document.dispatchEvent(new Event('visibilitychange'))")
    page.evaluate('Promise.all([refresh(),refresh(),refresh()])');expect(page.locator('#refresh')).to_be_enabled();assert len(calls)==2
    page.clock.fast_forward(600000);assert len(calls)==2
    # Manual refresh uses authenticated POST; return completion via a browser-only fixture to avoid provider rate-limit calls.
    page.route('**/api/refresh',lambda r:r.fulfill(status=202,json={'accepted':True,'requested':1}))
    page.route('**/api/collection',lambda r:r.fulfill(json={'completed':1}))
    page.locator('#refresh').click();expect(page.locator('#refresh')).to_be_enabled();assert len(calls)==3
    assert not errors,errors
    browser.close()
print('Browser passed: synthetic Flask API, tabs+view URL, charts, metric/share modes, tooltips, insights, sources, mobile, auto/hidden/coalesced refresh, manual UI with completion fixture.')
