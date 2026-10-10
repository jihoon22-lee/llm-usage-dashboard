"""Portable Chromium regression checks using disposable synthetic data and local assets."""
from browser_support import CSP_HEADERS, watch_csp, fixture_page, fixture_json, artifact_directory
import argparse
from pathlib import Path
from urllib.parse import parse_qs,urlsplit

from playwright.sync_api import sync_playwright,expect

parser=argparse.ArgumentParser()
parser.add_argument('--database',type=Path,help='Route read-only API views to a staged numeric DB; no manual collection')
parser.add_argument('--artifacts',type=Path)
args=parser.parse_args()
origin='https://dashboard.test'
if args.database:
    from llm_usage.store import Store
    store=Store(args.database)
output=artifact_directory(args.artifacts)
output.mkdir(parents=True,exist_ok=True)
with sync_playwright() as p:
    browser=p.chromium.launch()
    page=browser.new_page(service_workers='block',viewport={'width':1440,'height':1000})
    fixture_page(page)
    errors=[];page.on('pageerror',lambda error:errors.append(str(error)));watch_csp(page,errors)
    if args.database:
        def api(route):
            path=urlsplit(route.request.url).path
            if path=='/api/limits':route.fulfill(json=store.limits());return
            q={k:v[0] for k,v in parse_qs(urlsplit(route.request.url).query).items()}
            q['cumulative']=q.get('cumulative')=='1'
            q={k:v for k,v in q.items() if k in ('period','start','end','granularity','group','cumulative','compare','scope','sections')}
            route.fulfill(json=store.usage(**q))
        page.route(origin+'/api/usage?*',api);page.route(origin+'/api/limits',api)
    assert page.goto(origin+'/?view=usage').status==200
    expect(page.locator('#refresh')).to_be_enabled()
    page.evaluate("$('auto').checked=false;$('auto').dispatchEvent(new Event('change'))")
    def select(selector,value):
        with page.expect_response(lambda r:'/api/usage?' in r.url and r.status==200):page.locator(selector).select_option(value)
        expect(page.locator('#refresh')).to_be_enabled()
    select('#period','all');select('#group','provider')
    assert page.evaluate("['OpenAI','Anthropic','Google'].every(p=>lastUsage.labels.includes(p))")
    assert page.evaluate("!lastUsage.unavailable_routes.some(r=>r.route==='antigravity')")
    expect(page.locator('#usage-gaps')).to_be_hidden()
    expect(page.locator('#rows')).to_contain_text('gemini-3.8-flash')
    page.wait_for_function("()=>'insights' in (lastUsage||{})")
    expect(page.locator('#cache-rows')).to_contain_text('gemini-3.8-flash')
    total=page.evaluate("lastUsage.rows.filter(r=>r.route==='antigravity').reduce((n,r)=>n+total(r),0)")
    assert total>0
    select('#group','route')
    for grain in ('day','week','month'):
        if page.locator('#granularity').input_value()!=grain:select('#granularity',grain)
        assert page.evaluate("lastUsage.series.reduce((n,p)=>n+total(p.values.antigravity||{}),0)")==total
    expect(page.locator('#legend')).to_contain_text('Antigravity')
    assert '합계 검증 대기' not in page.locator('#legend').inner_text()
    page.locator('#chart > svg').focus();page.keyboard.press('End')
    tip=page.locator('#chart-tooltip .tooltip-row').filter(has_text='Antigravity')
    expect(tip).to_be_visible();assert tip.locator('strong').inner_text() not in ('—','0')
    page.keyboard.press('Escape');select('#cumulative','1')
    assert page.evaluate('total(lastUsage.series.at(-1).values.antigravity)')==total
    select('#group','model')
    expect(page.locator('#legend')).to_contain_text('gemini-3.8-flash')
    page.screenshot(path=str(output/'antigravity-history-desktop.png'),full_page=True)
    select('#group','provider');select('#cumulative','0')
    page.set_viewport_size({'width':390,'height':844})
    page.locator('#tabs [data-view="quota"]').click()
    expect(page.locator('#quota-overview')).to_be_visible()
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
    page.screenshot(path=str(output/'antigravity-history-mobile.png'),full_page=True)
    page.set_viewport_size({'width':320,'height':844})
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
    assert not errors,errors
    print('Antigravity UI passed:',{'staged_database':bool(args.database),'tokens':total,'providers':['OpenAI','Anthropic','Google']})
    browser.close()
