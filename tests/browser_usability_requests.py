"""Query races, recoverable failures and bounded requests using synthetic responses."""
from browser_support import fixture_page, fixture_json, ORIGIN, watch_csp
from playwright.sync_api import sync_playwright, expect

def ready(browser,view='quota'):
    page=browser.new_page(service_workers='block',viewport={'width':1440,'height':1000})
    fixture_page(page)
    page.goto(ORIGIN+'/?view='+view)
    expect(page.locator('#updated')).to_contain_text('마지막 갱신')
    page.evaluate("$('auto').checked=false;schedule()")
    return page

def wait_held(page,held,count):
    # The usage tab's refresh waits for its (held) insights answer, so the refresh button stays busy.
    for _ in range(100):
        if len(held)>=count:break
        page.wait_for_timeout(50)

with sync_playwright() as p:
    browser=p.chromium.launch()
    page=ready(browser);held=[]
    page.route('**/api/usage?**sections=insights',lambda r:held.append(r))
    page.locator('#tab-usage').click();page.wait_for_function('() => !!insightsPending')
    page.locator('#period').select_option('today')
    wait_held(page,held,2);page.wait_for_timeout(100)
    assert len(held)==2, 'A new query must not reuse the old pending insights request'
    old,new=held;new.fulfill(json=fixture_json(new.request))
    expect(page.locator('#projects')).to_contain_text('fixture-project')
    today=page.locator('#projects').inner_text()
    old.fulfill(json=fixture_json(old.request))
    expect(page.locator('#projects')).to_have_text(today,use_inner_text=True)
    expect(page.locator('#insights-status')).to_be_hidden()
    # A->B->A must not accept a superseded response even when its query matches.
    page.close();page=ready(browser);held=[]
    page.route('**/api/usage?**sections=insights',lambda r:held.append(r))
    page.locator('#tab-usage').click();page.wait_for_function('() => !!insightsPending')
    for count,period in ((2,'today'),(3,'7d')):
        page.locator('#period').select_option(period)
        wait_held(page,held,count);page.wait_for_timeout(100)
    assert len(held)==3
    old,middle,new=held
    old.fulfill(json=fixture_json(old.request));middle.fulfill(json=fixture_json(middle.request))
    expect(page.locator('#csv-projects')).to_be_disabled()
    new.fulfill(json=fixture_json(new.request));expect(page.locator('#csv-projects')).to_be_enabled()
    page.close()
    # Failed lazy load must not leave yesterday's table under today's heading.
    page=ready(browser,'usage');expect(page.locator('#projects')).to_contain_text('fixture-project')
    old=page.locator('#projects').inner_text()
    page.route('**/api/usage?**sections=insights',lambda r:r.fulfill(status=503,json={'error':'synthetic failure'}))
    page.locator('#period').select_option('today')
    expect(page.locator('#refresh')).to_be_enabled();expect(page.locator('#insights-status')).to_contain_text('synthetic failure')
    assert page.locator('#projects').inner_text()!=old
    expect(page.locator('#csv-projects')).to_be_disabled()
    page.unroute('**/api/usage?**sections=insights');page.locator('#insights-retry').click()
    expect(page.locator('#insights-status')).to_be_hidden();expect(page.locator('#csv-projects')).to_be_enabled()
    assert page.evaluate('lastUsage.projects[0].requests')==3
    # Cached insights are labeled immediately, with no 30-second interval needed.
    page.route('**/api/usage?**sections=insights',lambda r:r.abort('internetdisconnected'))
    page.evaluate('refresh()');expect(page.locator('#offline')).to_be_visible()
    page.close()
    # Both a stalled fetch and a stalled response body must time out.
    for stalled in ('headers','body'):
        page=ready(browser);page.clock.install()
        page.evaluate("""kind=>{
            const original=fetch;window.fetch=(url,options)=>String(url).startsWith('/api/usage?')
                ?(kind==='headers'?new Promise(()=>{}):Promise.resolve(new Response(new ReadableStream({start(){}}))))
                :original(url,options);
            void clearStatisticsCache().then(()=>refresh());
        }""",stalled)
        expect(page.locator('#refresh')).to_be_disabled();page.clock.fast_forward(31000)
        expect(page.locator('#refresh')).to_be_enabled();expect(page.locator('#error')).to_contain_text('시간')
        page.close()
    # A single collection GET cannot hang forever, nor bypass the overall 90s bound.
    page=ready(browser);page.clock.install();posts=[]
    page.route('**/api/refresh',lambda r:(posts.append(r.request.url),r.fulfill(status=202,json={'request_id':1})))
    page.evaluate("""()=>{const original=fetch;window.fetch=(url,options)=>url==='/api/collection'?new Promise(()=>{}):original(url,options);void refresh(true);}""")
    expect(page.locator('#updated')).to_contain_text('수집 요청 중');page.clock.fast_forward(11000)
    expect(page.locator('#refresh')).to_be_enabled();expect(page.locator('#error')).to_be_visible();assert len(posts)==1
    page.close()
    page=ready(browser);page.clock.install();posts=[]
    page.route('**/api/refresh',lambda r:(posts.append(r.request.url),r.fulfill(status=202,json={'request_id':2})))
    page.route('**/api/collection',lambda r:r.fulfill(json={'completed_id':1}))
    page.locator('#refresh').click()
    for _ in range(91):page.clock.run_for(1000)
    expect(page.locator('#refresh')).to_be_enabled();expect(page.locator('#error')).to_contain_text('수집')
    assert len(posts)==1
    page.close();browser.close()
print('Usability requests passed: out-of-order/A-B-A queries, failed insights/retry, offline copies, header/body/poll deadlines, no duplicate POST.')
