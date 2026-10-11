"""Recent usage through real Flask responses, with a pinned clock and disposable records."""
import argparse
import csv
import tempfile
import time
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

from browser_support import ORIGIN, Sock, artifact_directory, watch_csp
from playwright.sync_api import sync_playwright, expect
from llm_usage.store import Store
from llm_usage.webapp import create_app

parser=argparse.ArgumentParser()
parser.add_argument('--artifacts',type=Path)
args=parser.parse_args()
output=artifact_directory(args.artifacts)
now=int(time.time());server_clock=[now]

with tempfile.TemporaryDirectory(prefix='llm-recent-browser-') as root:
    path=Path(root)/'usage.db';store=Store(path)
    with store.connect() as c:
        for key,offset,value,route,project,session in (
            ('before',-3601,10000,'codex','outside','old'),
            ('start',-3600,10,'codex','alpha','session-one'),
            ('middle',-1200,20,'claude-code','beta','session-two'),
            ('last',-1,30,'antigravity',None,None),
            ('end',0,10000,'codex','outside','new')):
            store.event(c,key,now+offset,'fixture',route,'model-'+route,
                        dict(uncached_input=value,cached_input=value,output=value,
                             cache_creation=value,reasoning=value),project=project,session=session)
        store.save_state(c,'collector',{'checked':now,'status':'idle'})
        store.save_state(c,'last_local',now)
        for route in ('codex','claude-code','antigravity'):
            store.source(c,route,'ok','Synthetic recent usage',now)
        for route in ('opencode-go','devin'):
            store.source(c,route,'ended','Synthetic inactive route',now)
    client=create_app(dict(database=str(path),origin=ORIGIN,secret_key='fixture-recent-only',
                           allowed_logins=['fixture'])).test_client()
    requests=[]

    def fixture(page):
        def handle(route):
            url=urlsplit(route.request.url)
            assert url.scheme=='https' and url.netloc=='dashboard.test'
            assert route.request.method=='GET'
            requests.append(url.path+'?'+url.query)
            with patch('llm_usage.store.time.time',return_value=server_clock[0]):
                response=client.get(url.path+('?' +url.query if url.query else ''),
                    headers={'Host':'dashboard.test','Tailscale-User-Login':'fixture'},
                    environ_base={'gunicorn.socket':Sock()})
            try:
                route.fulfill(status=response.status_code,body=response.get_data(),
                              headers={k:v for k,v in response.headers.items() if k.lower()!='content-length'})
            finally:response.close()
        page.route('**/*',handle)

    with sync_playwright() as playwright:
        browser=playwright.chromium.launch()
        for width in (1440,390,320):
            server_clock[0]=now
            context=browser.new_context(viewport={'width':width,'height':900},service_workers='block')
            page=context.new_page();fixture(page)
            errors=[];page.on('pageerror',lambda error:errors.append(str(error)));watch_csp(page,errors)
            page.goto(ORIGIN+'/?view=usage')
            expect(page.locator('#updated')).to_contain_text('마지막 갱신')
            expect(page.locator('#recent-usage')).to_be_visible()
            page.locator('#recent-usage').click()
            page.wait_for_function("()=>lastUsage?.period==='1h'&&Array.isArray(lastUsage.projects)")
            expect(page.locator('#period')).to_have_value('1h')
            expect(page.locator('#group')).to_have_value('project')
            expect(page.locator('#granularity')).to_have_value('hour')
            expect(page.locator('#recent-usage')).to_have_attribute('aria-pressed','true')
            assert page.evaluate('total(lastUsage.totals)')==240
            assert page.evaluate('lastUsage.totals.requests')==3
            assert page.evaluate('(Date.parse(lastUsage.end_exclusive)-Date.parse(lastUsage.start))/1000')==3600
            assert page.evaluate('lastUsage.rows.reduce((n,r)=>n+total(r),0)')==240
            assert page.evaluate('lastUsage.series.reduce((n,p)=>n+Object.values(p.values).reduce((n,v)=>n+total(v),0),0)')==240
            assert any('as_of='+str(now) in url for url in requests)
            expect(page.locator('#cards-period')).to_contain_text('최근 1시간')
            expect(page.locator('#cards-period')).to_contain_text('KST')
            expect(page.locator('[data-panel="heatmap"]')).to_be_hidden()
            page.locator('#chart > svg').focus()
            expect(page.locator('#chart-tooltip')).to_contain_text('종료 미포함')
            page.keyboard.press('Escape')
            # A coarser selected granularity drills back to the rolling hour,
            # not to an inverted date range ending on the previous calendar day.
            if page.locator('#filter-fold').get_attribute('open') is None:
                page.locator('#filter-fold > summary').click()
            page.locator('#granularity').select_option('day')
            page.wait_for_function("()=>lastUsage?.granularity==='day'&&!pending")
            page.locator('#chart > svg').focus();page.keyboard.press('Enter')
            page.wait_for_function("()=>lastUsage?.granularity==='hour'&&!pending")
            expect(page.locator('#period')).to_have_value('1h')
            expect(page.locator('#error')).to_be_hidden()
            if width<=600:page.locator('#filter-fold > summary').click()
            expect(page.locator('[data-panel="comparison"]')).to_be_hidden()
            page.locator('[data-usage-jump="projects"]').click()
            expect(page.locator('#projects')).to_be_visible()
            assert page.locator('#projects > tr').count()==3
            expect(page.locator('#projects')).to_contain_text('미분류')
            expect(page.locator('#projects')).not_to_contain_text('outside')
            page.locator('[data-usage-jump="sessions"]').click()
            expect(page.locator('#sessions')).to_be_visible()
            expect(page.locator('#sessions-note')).to_contain_text('세션 미귀속 요청 1건')
            page.locator('#sessions .sess-open').first.click()
            expect(page.locator('#sessions .sess-detail')).to_be_visible()
            assert page.locator('#sessions .sess-detail').get_by_text('상세를 불러오지 못했습니다').count()==0
            page.locator('#sessions .sess-open').first.click()
            # CSV uses exactly the same range and does not misdate a non-midnight end.
            if page.locator('[data-panel="models"]').get_attribute('open') is None:
                page.locator('[data-panel="models"] > summary').click()
            with page.expect_download() as download:
                page.locator('#csv-export').click()
            rows=list(csv.DictReader(Path(download.value.path()).read_text(encoding='utf-8-sig').splitlines()))
            assert sum(int(r['Total (incl. cache creation)']) for r in rows)==240
            assert '_1h.csv' in download.value.suggested_filename
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
            page.evaluate('scrollTo(0,0)')
            page.screenshot(path=str(output/f'recent-usage-{width}.png'),full_page=True)
            # Refresh and a saved URL keep the rolling hour, rather than falling back to seven days.
            page.reload()
            page.wait_for_function("()=>lastUsage?.period==='1h'&&Array.isArray(lastUsage.projects)")
            assert page.evaluate('total(lastUsage.totals)')==240
            # Time passing alone changes the range and empties it; an old cache must not keep old work alive.
            server_clock[0]=now+7200
            page.evaluate('refresh()')
            assert page.evaluate('lastUsage.totals.requests')==0
            assert page.evaluate('lastUsage.projects.length')==0
            page.locator('[data-usage-jump="sessions"]').click()
            expect(page.locator('#sessions')).to_contain_text('선택 기간에 세션 귀속된 기록이 없습니다')
            expect(page.locator('[data-panel="comparison"]')).not_to_contain_text('계산 불가')
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
            assert not errors,errors
            context.close()
        browser.close()

print('Recent usage browser passed: 3 widths, exact totals, anchored details, missing attribution, CSV, saved URL and idle cache expiry.')
