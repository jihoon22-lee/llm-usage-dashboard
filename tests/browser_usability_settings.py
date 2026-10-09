"""Editing and recovery flows against disposable Flask data, never live accounts."""
from browser_support import fixture_page, fixture_response, ORIGIN, watch_csp
from playwright.sync_api import sync_playwright, expect

with sync_playwright() as p:
    browser=p.chromium.launch()
    page=browser.new_page(service_workers='block',viewport={'width':1440,'height':1000})
    fixture_page(page)
    # Deliberate validation errors are normal responses in these scenarios.
    def writes(route):
        response=fixture_response(route.request)
        try:route.fulfill(status=response.status_code,body=response.get_data(),headers={'Content-Type':'application/json'})
        finally:response.close()
    page.route('**/api/config/pricing',writes)
    errors=[];page.on('pageerror',lambda e:errors.append(str(e)));watch_csp(page,errors)
    page.goto(ORIGIN+'/?view=settings')
    expect(page.locator('#updated')).to_contain_text('마지막 갱신')
    a=page.locator('input[data-model="gpt-test"][data-key="input"]')
    b=page.locator('input[data-model="claude-test"][data-key="input"]')
    subscription=page.locator('#cfg-subs input[data-route="codex"]')
    a.fill('7.25');b.fill('8.75');subscription.fill('99')
    page.locator('[data-price-save="gpt-test"]').click()
    expect(page.locator('#cfg-pricing-msg')).to_contain_text('저장')
    expect(b).to_have_value('8.75');expect(subscription).to_have_value('99')
    expect(b.locator('xpath=ancestor::tr')).to_have_class('dirty')
    page.locator('#tab-overview').click();page.locator('#tab-settings').click()
    expect(a).to_have_value('7.25');expect(b).to_have_value('8.75');expect(subscription).to_have_value('99')
    # One rejected row does not discard its draft or retry action.
    a.fill('7.5');b.fill('10001');page.locator('#price-save-all').click()
    expect(page.locator('#cfg-pricing-msg')).to_contain_text('저장 실패')
    expect(b).to_have_value('10001');expect(page.locator('#price-save-all')).to_be_visible()
    page.clock.install();page.clock.fast_forward(6000)
    expect(b.locator('xpath=ancestor::tr')).to_contain_text('0~10000')
    b.fill('8.5');page.locator('#price-save-all').click()
    expect(page.locator('#price-save-all')).to_be_hidden()
    expect(b).to_have_value('8.5')
    # Keep a row locked until its request finishes; unrelated rows remain editable.
    held=[];page.route('**/api/config/pricing',lambda route:held.append(route))
    a.fill('9');page.locator('[data-price-save="gpt-test"]').click()
    expect(a).to_be_disabled();expect(b).to_be_enabled()
    page.locator('#tab-overview').click();page.locator('#tab-settings').click()
    expect(a).to_be_disabled();expect(subscription).to_have_value('99')
    assert len(held)==1
    page.unroute('**/api/config/pricing');page.route('**/api/config/pricing',writes)
    writes(held.pop());expect(a).to_be_enabled();expect(a).to_have_value('9')
    # A cleared price remains editable, including after a full navigation.
    subscription.fill('');page.locator('#cfg-subs-save').click()
    expect(page.locator('#cfg-subs-msg')).to_contain_text('저장했습니다')
    expect(page.locator('#refresh')).to_be_enabled();page.reload()
    expect(subscription).to_be_visible();expect(subscription).to_have_value('')
    subscription.fill('25');page.locator('#cfg-subs-save').click()
    expect(page.locator('#cfg-subs-msg')).to_contain_text('저장했습니다')
    expect(page.locator('#refresh')).to_be_enabled()
    assert page.evaluate("lastUsage.subscriptions.find(s=>s.route==='codex').monthly_usd")==25
    assert not errors,errors
    browser.close()
print('Usability settings passed: independent drafts, partial failure/retry, row locking, tab navigation, subscription restoration.')
