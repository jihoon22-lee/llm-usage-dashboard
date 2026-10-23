"""Exercise reconciled API totals and KST interval labels in a real browser."""
from browser_support import fixture_page,watch_csp
from playwright.sync_api import sync_playwright,expect
import browser_support as fixtures
import csv
from pathlib import Path
import time
from llm_usage.store import Store
from llm_usage.reconciliation import snapshot,link

fixtures.fixture_client()
store=Store(Path(fixtures._temp.name)/'usage.db')
now=time.time()
with store.connect() as c:
    for i in range(101):
        sid=f'rank-{i:03d}'
        store.event(c,sid,now-120,'Anthropic','claude-code','claude-test',dict(output=101-i),session=sid,project='session-ranking')
        link(c,'claude:'+sid,sid,'local')
    snapshot(store,c,source='claude-cloud',scope_id='claude:rank-100',route='claude-code',provider='Anthropic',model='claude-test',
             session='rank-100',project='session-ranking',start=now-3600,end=now-60,tokens=10_000,
             parts=dict(uncached_input=0,cached_input=0,cache_creation=0,output=10_000))

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
        # A session outside the local top 100 enters it after cloud reconciliation,
        # with its original request count in the table, detail and CSV.
        expect(page.locator('#refresh')).to_be_enabled()
        page.goto('https://dashboard.test/?view=usage&period=7d&scope=project%3Asession-ranking')
        expect(page.locator('#sess-page')).to_contain_text('전체 101개 중 상위 100개')
        if page.locator('[data-panel="sessions"]').get_attribute('open') is None:
            page.locator('[data-panel="sessions"] > summary').click()
        page.locator('[data-ssort="tokens"]').click()
        row=page.locator('.sess-open[data-id="rank-100"]').locator('xpath=ancestor::tr')
        expect(row.locator('td').nth(5)).to_have_text('1')
        expect(row.locator('td').nth(6)).to_have_text('10,000')
        row.locator('.sess-open').click()
        expect(page.locator('.sess-detail-row')).to_contain_text('10,000')
        expect(page.locator('#csv-sessions')).to_be_enabled()
        with page.expect_download() as downloaded:
            page.locator('#csv-sessions').click()
        with Path(downloaded.value.path()).open(encoding='utf-8-sig',newline='') as handle:
            exported=list(csv.DictReader(handle))
        assert len(exported)==100
        saved=next(r for r in exported if r['세션']=='rank-100')
        assert (saved['요청 수'],saved['토큰'])==('1','10000'),saved
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
        assert not errors,errors
        page.close()
    browser.close()
print('Reconciled totals, compact disclosure, keyboard, refresh state and KST labels passed at 1440/390/320px.')
