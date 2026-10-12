"""Exercise reconciled API totals and KST interval labels in a real browser."""
from browser_support import fixture_page,watch_csp
from playwright.sync_api import sync_playwright,expect

with sync_playwright() as p:
    browser=p.chromium.launch()
    for width in (1440,390,320):
        page=browser.new_page(viewport={'width':width,'height':1000},service_workers='block')
        fixture_page(page);errors=[]
        page.on('pageerror',lambda e:errors.append(str(e)));watch_csp(page,errors)
        page.goto('https://dashboard.test/?view=usage&period=7d')
        expect(page.locator('#usage-reconciliation')).to_be_visible()
        expect(page.locator('#usage-reconciliation')).to_contain_text('날짜 미배분')
        expect(page.locator('#usage-reconciliation')).to_contain_text('KST')
        assert page.evaluate('lastUsage.totals.total_tokens===lastUsage.reconciliation.recorded_tokens+lastUsage.reconciliation.supplemental_tokens')
        assert page.evaluate('lastUsage.series.reduce((n,p)=>n+Object.values(p.values).reduce((s,v)=>s+total(v),0),0)+lastUsage.reconciliation.unallocated_tokens===total(lastUsage.totals)')
        assert page.evaluate('lastUsage.rows.reduce((n,r)=>n+total(r),0)===total(lastUsage.totals)')
        expect(page.locator('#composition')).to_contain_text('내역 미분류')
        assert not page.evaluate("document.querySelector('#composition').innerHTML.includes('NaN')")
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
        assert not errors,errors
        page.close()
    browser.close()
print('Reconciled totals, unallocated intervals and KST labels passed at 1440/390/320px.')
