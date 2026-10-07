"""Real worker lifecycle plus failure isolation, with disposable synthetic API snapshots."""
from browser_support import CSP_HEADERS, watch_csp, fixture_client, Sock
from playwright.sync_api import sync_playwright, expect
from browser_support import fixture_page, ORIGIN

with sync_playwright() as p:
    # Chromium's headless shell denies notifications; the installed Chromium channel
    # uses real headless Chromium and supports the worker notification API.
    browser = p.chromium.launch(channel='chromium')
    context = browser.new_context(permissions=['notifications'], viewport={'width':390,'height':844}, is_mobile=True)
    fixture_page(context)
    usage = fixture_client().get('/api/usage?period=7d',headers={'Tailscale-User-Login':'fixture','Host':'dashboard.test'},environ_base={'gunicorn.socket':Sock()}).get_json()
    limits = {'limits':[]}
    page = context.new_page()
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)));watch_csp(page,errors)
    page.route('**/api/bootstrap', lambda r:r.fulfill(json={'csrf':'fixture','refresh_seconds':300}))
    page.route('**/api/usage?*', lambda r:r.fulfill(json=usage))
    page.route('**/api/limits', lambda r:r.fulfill(json=limits))
    page.goto(ORIGIN+'/')
    expect(page.locator('#updated')).to_contain_text('마지막 갱신')
    # Force the mobile constructor error even though desktop Chromium hosts this test.
    page.evaluate('''() => {
      window.Notification=new Proxy(Notification,{construct(){throw new TypeError('Illegal constructor');}});
      localStorage.setItem('llmNotify','1');
    }''')
    # Actual registration, activation and persistent notification in Chromium.
    page.evaluate("sendNotification('fixture notification',{tag:'fixture'})")
    assert page.evaluate("notificationRegistration().then(async r=>(await r.getNotifications()).length)")==1, page.evaluate("({permission:Notification.permission,enabled:notifyEnabled(),status:document.querySelector('#notify-status').textContent,worker:!!notificationWorker})")
    page.evaluate("notificationRegistration().then(async r=>{for(const n of await r.getNotifications())n.close();})")
    expect(page.locator('#notify-status')).to_be_hidden()
    row = {'route':'codex','bucket':'weekly','status':'fresh','remaining':80}
    limits['limits'] = [row]
    page.evaluate('refresh()')
    page.evaluate("() => {ServiceWorkerRegistration.prototype.showNotification=async()=>{throw Error('denied');};}")
    row['remaining']=10
    page.evaluate('refresh()')
    expect(page.locator('#notify-status')).to_be_visible()
    expect(page.locator('#error')).to_be_hidden()
    expect(page.locator('#updated')).to_contain_text('마지막 갱신')
    assert page.evaluate('lastLimits.limits[0].remaining')==10
    # No repeated attempt for the same transition; all three transitions are retained.
    page.evaluate("() => {window.sent=[];ServiceWorkerRegistration.prototype.showNotification=async function(title){sent.push(title);};}")
    page.evaluate('refresh()')
    assert page.evaluate('sent')==[]
    row['remaining']=80
    page.evaluate('refresh()')
    page.wait_for_function('sent.length===1')
    row['forecast']={'within_window':True}
    page.evaluate('refresh()')
    page.wait_for_function('sent.length===2')
    # A toggling forecast must not page twice for the same reset window.
    row['forecast']=None
    page.evaluate('refresh()')
    row['forecast']={'within_window':True}
    page.evaluate('refresh()')
    assert page.evaluate('sent')==['한도 초기화됨','한도 소진 예상']
    # The reset anchor changes after a provider rollover, so a new low
    # transition in the new window notifies again.
    row['remaining']=10;row['resets']=2000000000
    page.evaluate('refresh()')
    page.wait_for_function('sent.length===3')
    assert page.evaluate('sent')==['한도 초기화됨','한도 소진 예상','한도 잔여 적음']
    # Failed worker registration also stays isolated.
    page.evaluate("() => {notificationWorker=null;navigator.serviceWorker.register=async()=>{throw Error('offline');};}")
    page.evaluate("sendNotification('failure',{})")
    expect(page.locator('#notify-status')).to_be_visible()
    assert page.evaluate('notificationWorker===null')
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
    assert not errors, errors
    browser.close()
print('Notifications passed: real worker activation/delivery, mobile constructor rejection, 3 transitions, failure isolation, no repeat, mobile layout.')
