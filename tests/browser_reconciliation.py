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
        details=page.locator('#usage-reconciliation details')
        summary=details.locator('summary')
        expect(details).not_to_have_attribute('open','')
        expect(details.locator('.reconciliation-detail')).not_to_be_visible()
        assert page.locator('#usage-reconciliation').bounding_box()['height']<116
        summary.focus();page.keyboard.press('Enter')
        expect(details.locator('.reconciliation-detail')).to_be_visible()
        expect(details.locator('.reconciliation-intervals li').first).to_be_visible()
        page.evaluate('renderReconciliation(lastUsage.reconciliation)')
        expect(details).to_have_attribute('open','')
        summary.press('Enter')
        expect(details.locator('.reconciliation-detail')).not_to_be_visible()
        # Warnings remain visible while long interval lists and explanations fold.
        page.evaluate('renderReconciliation({...lastUsage.reconciliation,conflicts:[{}]})')
        expect(summary).to_contain_text('충돌 1건')
        assert page.evaluate('lastUsage.totals.total_tokens===lastUsage.reconciliation.recorded_tokens+lastUsage.reconciliation.supplemental_tokens')
        assert page.evaluate('lastUsage.series.reduce((n,p)=>n+Object.values(p.values).reduce((s,v)=>s+total(v),0),0)+lastUsage.reconciliation.unallocated_tokens===total(lastUsage.totals)')
        assert page.evaluate('lastUsage.rows.reduce((n,r)=>n+total(r),0)===total(lastUsage.totals)')
        # Tokens without a breakdown stay in the composition key: here all of them are the
        # Codex account residual, its own item (out of the ring by default) rather than 내역 미분류.
        expect(page.locator('#composition [data-token="residual_tokens"]')).to_contain_text('로컬 미기록')
        expect(page.locator('#composition [data-token="unclassified_tokens"]')).to_have_count(0)
        assert not page.evaluate("document.querySelector('#composition').innerHTML.includes('NaN')")
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
        assert not errors,errors
        page.close()
    browser.close()
print('Reconciled totals, compact disclosure, keyboard, refresh state and KST labels passed at 1440/390/320px.')
