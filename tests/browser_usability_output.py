"""CSV semantics, small-screen editing and first-run guidance; synthetic data only."""
import argparse
import csv
import io
import tempfile
import time
from pathlib import Path
from browser_support import fixture_page, fixture_client, ORIGIN, watch_csp
import browser_support
from llm_usage.store import Store
from llm_usage.webapp import create_app
from playwright.sync_api import sync_playwright, expect

parser=argparse.ArgumentParser()
parser.add_argument('--artifacts',type=Path)
args=parser.parse_args()
if args.artifacts:args.artifacts.mkdir(parents=True,exist_ok=True)

with sync_playwright() as p:
    browser=p.chromium.launch()
    fixture_client();store=Store(Path(browser_support._temp.name)/'usage.db')
    with store.connect() as c:
        store.event(c,'unpriced-output',time.time()-60,'Example','codex','unpriced-example',dict(uncached_input=1000000,output=20))
    page=browser.new_page(service_workers='block');fixture_page(page)
    page.goto(ORIGIN+'/?view=analysis');expect(page.locator('#updated')).to_contain_text('마지막 갱신')
    page.locator('#group').select_option('model');expect(page.locator('#refresh')).to_be_enabled()
    page.locator('#metric').select_option('cost');expect(page.locator('#refresh')).to_be_enabled()
    for mode in ('0','1'):
        page.locator('#cumulative').select_option(mode);expect(page.locator('#refresh')).to_be_enabled()
        with page.expect_download() as download:page.locator('#csv-series').click()
        rows=list(csv.DictReader(io.StringIO(Path(download.value.path()).read_text(encoding='utf-8-sig'))))
        row=rows[-1]
        assert row['unpriced-example']=='',row
        assert row['unpriced-example 미산정 토큰']=='1000020',row
        assert row['합계 미산정 토큰']=='1000020',row
        assert float(row['합계 비용 추정'])>0,row
    page.goto(ORIGIN+'/?scope=model%3Anot-recorded')
    expect(page.locator('#updated')).to_contain_text('마지막 갱신')
    expect(page.locator('#first-use')).to_be_hidden()
    page.close()
    for width in (320,360,390,768,1440):
        page=browser.new_page(service_workers='block',viewport={'width':width,'height':900});fixture_page(page)
        errors=[];page.on('pageerror',lambda e:errors.append(str(e)));watch_csp(page,errors)
        page.goto(ORIGIN+'/?view=settings');expect(page.locator('#cfg-subs-save')).to_be_enabled()
        # Expand every settings section through its user-visible control.
        for panel in page.locator('#view-settings details.mobile-fold').all():
            if panel.get_attribute('open') is None:panel.locator(':scope > summary').click()
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth'),width
        labels=page.locator('.cfg-toggles').nth(1).locator('label').evaluate_all('els=>els.map(e=>e.getBoundingClientRect().height)')
        assert all(h<=60 for h in labels),(width,labels)
        expect(page.get_by_role('spinbutton',name='자동 갱신 주기 (초)',exact=True)).to_be_visible()
        rate=page.get_by_role('spinbutton',name='gpt-test 입력 단가 (USD/100만 토큰)',exact=True)
        rate.fill('6.25');page.get_by_role('button',name='gpt-test 단가 저장',exact=True).click()
        expect(page.locator('#cfg-pricing-msg')).to_contain_text('저장했습니다')
        expect(rate).to_have_value('6.25')
        page.locator('#ntf-low').uncheck();expect(page.locator('#ntf-low')).not_to_be_checked()
        if args.artifacts and width in (320,390,1440):
            page.screenshot(path=str(args.artifacts/f'settings-{width}.png'),full_page=True)
            page.locator('#cfg-refresh').locator('xpath=ancestor::details').screenshot(path=str(args.artifacts/f'notifications-{width}.png'))
        page.locator('#tab-analysis').focus();page.keyboard.press('ArrowRight')
        expect(page.locator('#tab-insights')).to_have_attribute('aria-selected','true')
        assert not errors,errors
        page.close()
    with tempfile.TemporaryDirectory() as folder:
        root=Path(folder);Store(root/'usage.db');(root/'config.json').write_text('{}')
        browser_support._client=create_app(dict(database=str(root/'usage.db'),config_file=str(root/'config.json'),
            pricing_file=str(root/'pricing.json'),origin=ORIGIN,secret_key='synthetic'*4,allowed_logins=['fixture'])).test_client()
        page=browser.new_page(service_workers='block');fixture_page(page);page.goto(ORIGIN+'/')
        expect(page.locator('#first-use')).to_be_visible()
        expect(page.locator('#first-use')).to_contain_text('아직 수집된 사용 기록이 없습니다')
        page.locator('#first-use-sources').click();expect(page.locator('#tab-sources')).to_have_attribute('aria-selected','true')
        page.locator('#tab-insights').click();expect(page.locator('#quality')).to_contain_text('기록된 해석 실패가 없습니다')
        assert '모든 기록 줄을 해석했습니다' not in page.locator('#quality').inner_text()
        page.close()
    browser.close()
print('Usability output passed: unpriced CSV/current+cumulative, 5 viewport widths, expanded settings/editing, accessible names/keyboard, isolated first run.')
