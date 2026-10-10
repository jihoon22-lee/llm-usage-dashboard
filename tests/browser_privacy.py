"""Real browser migration, offline settings and storage-failure regressions; synthetic only."""
from browser_support import fixture_page, ORIGIN, watch_csp
from playwright.sync_api import sync_playwright, expect

with sync_playwright() as p:
    browser=p.chromium.launch()
    for width in (1440,390):
        context=browser.new_context(viewport={'width':width,'height':900})
        fixture_page(context)
        page=context.new_page();errors=[]
        page.on('pageerror',lambda e:errors.append(str(e)));watch_csp(page,errors)
        page.goto(ORIGIN+'/')
        expect(page.locator('#updated')).to_contain_text('마지막 갱신')
        page.evaluate("""async()=>{
            const c=await caches.open('llm-usage-data-v1');
            for(const path of ['/api/config','/api/config/notify?old=1','/api/bootstrap','/api/notify/log'])
                await c.put(path,new Response(JSON.stringify({saved:Date.now(),data:{secret:'https://synthetic.invalid/secret-token'}})));
        }""")
        page.reload();expect(page.locator('#updated')).to_contain_text('마지막 갱신')
        keys=page.evaluate("async()=> (await (await caches.open('llm-usage-data-v1')).keys()).map(r=>new URL(r.url).pathname)")
        assert keys and set(keys)<= {'/api/usage','/api/limits','/api/reports','/api/session','/api/project'},keys
        page.locator('#settings-open').click()
        expect(page.locator('#cfg-subs-save')).to_be_enabled()
        page.locator('#ntfy-url').fill('https://synthetic.invalid/unsaved-secret') if width==1440 else page.evaluate("$('ntfy-url').value='https://synthetic.invalid/unsaved-secret'")
        # Abort API only; the fixture shell remains reloadable without network.
        context.route('**/api/**',lambda route:route.abort('internetdisconnected'))
        assert page.evaluate('navigator.onLine')
        # Tailscale/server can fail while the browser still reports online.
        page.evaluate('refresh()')
        expect(page.locator('#notify-save')).to_be_disabled()
        assert page.locator('#ntfy-url').input_value()==''
        assert page.locator('#cfg-pricing input').count()==0
        expect(page.locator('#local-statistics-clear')).to_be_enabled()
        expect(page.locator('#offline')).to_be_visible()
        page.evaluate('loadConfig()')
        expect(page.locator('#settings-status')).to_contain_text('온라인 연결')
        page.locator('#tab-status').click()  # the offline-copy control lives on the status tab
        page.locator('#local-statistics-clear').click()
        expect(page.locator('#local-statistics-status')).to_contain_text('다시 저장')
        assert page.evaluate("caches.has('llm-usage-data-v1')")==False
        page.reload()
        expect(page.locator('#error')).to_be_visible()
        assert page.locator('#cards .stat').count()==0
        context.unroute('**/api/**')
        page.evaluate('refresh()')
        expect(page.locator('#updated')).to_contain_text('마지막 갱신')
        assert page.evaluate("async()=> (await (await caches.open('llm-usage-data-v1')).keys()).length")>0
        page.locator('#settings-open').click();page.evaluate('loadConfig()')
        expect(page.locator('#notify-save')).to_be_enabled()
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
        assert not errors,errors
        context.close()
    # Responses already in flight cannot restore settings after an outage.
    context=browser.new_context(service_workers='block')
    fixture_page(context)
    page=context.new_page();page.goto(ORIGIN+'/')
    expect(page.locator('#updated')).to_contain_text('마지막 갱신')
    page.locator('#settings-open').click()
    expect(page.locator('#cfg-subs-save')).to_be_enabled()
    for endpoint,action in (('/api/config','loadConfig()'),
                            ('/api/notify/log','loadNotifyLog()'),
                            ('/api/config/notify',"saveNotify({ntfy_url:'https://synthetic.invalid/topic'},'saved')")):
        page.evaluate('loadConfig()')
        expect(page.locator('#notify-save')).to_be_enabled()
        page.evaluate("""endpoint=>{
            window.originalFetch=fetch;window.releaseResponse=null;
            window.fetch=async(...args)=>{
                const response=await originalFetch(...args);
                if(new URL(args[0],location.origin).pathname===endpoint)
                    await new Promise(resolve=>window.releaseResponse=resolve);
                return response;
            };
        }""",endpoint)
        page.evaluate('void(window.delayedSettings='+action+')')
        page.wait_for_function("()=>typeof releaseResponse==='function'")
        page.evaluate("window.dispatchEvent(new Event('offline'));releaseResponse()")
        page.evaluate('delayedSettings')
        expect(page.locator('#notify-save')).to_be_disabled()
        expect(page.locator('#notify-test')).to_be_disabled()
        expect(page.locator('#local-statistics-clear')).to_be_enabled()
        assert page.locator('#cfg-pricing input').count()==0
        assert page.locator('#notify-log').inner_html()==''
        assert page.locator('#notify-events input').count()==0
        page.evaluate('void(window.fetch=originalFetch)')
    # A failed write also invalidates settings even without a browser offline event.
    page.evaluate('loadConfig()')
    expect(page.locator('#notify-save')).to_be_enabled()
    context.route('**/api/config/notify',lambda route:route.abort('internetdisconnected'))
    page.locator('#ntfy-url').fill('https://synthetic.invalid/write-secret')
    page.locator('#notify-save').click()
    expect(page.locator('#notify-save')).to_be_disabled()
    assert page.locator('#ntfy-url').input_value()==''
    context.unroute('**/api/config/notify')
    page.evaluate('loadConfig()')
    expect(page.locator('#notify-save')).to_be_enabled()
    context.route('**/api/limits',lambda route:route.fulfill(status=503,json={'error':'fixture unavailable'}))
    page.evaluate('refresh()')
    expect(page.locator('#notify-save')).to_be_disabled()
    expect(page.locator('#local-statistics-clear')).to_be_enabled()
    context.close()
    context=browser.new_context(service_workers='block')
    fixture_page(context)
    context.add_init_script("Object.defineProperty(window,'caches',{get(){throw Error('storage unavailable')}})")
    page=context.new_page();page.goto(ORIGIN+'/')
    expect(page.locator('#updated')).to_contain_text('마지막 갱신')
    page.locator('#settings-open').click();page.locator('#tab-status').click();page.locator('#local-statistics-clear').click()
    expect(page.locator('#local-statistics-status')).to_contain_text('삭제를 확인하지 못했습니다')
    expect(page.locator('#cfg-subs-save')).to_be_enabled()
    browser.close()
print('Privacy passed: v1 secret purge, offline statistics/settings, clear/reload/recache, storage failure, API outage/write failure, delayed responses, desktop/mobile.')
