"""Chromium checks for the 2026-10-04 review work against the real Flask app and headers.

The app runs in-process on a temporary database (no private origin needed); pages get the
production CSP, so geometry blocked by the policy would show up here. Covers bar and
marker geometry, the risk strip and reset axis, presets, shortcuts, project detail,
what-if pricing, notification history and the offline copy served by the root worker.

  PYTHONPATH=. .venv/bin/python tests/browser_review.py [--artifacts DIR]
"""
from browser_support import watch_csp
import argparse
import asyncio
from pathlib import Path
import socket
import tempfile
import time
from unittest.mock import patch

from playwright.async_api import async_playwright, expect

from llm_usage import notify
from llm_usage.store import Store
from llm_usage.webapp import create_app

parser=argparse.ArgumentParser()
parser.add_argument('--artifacts',type=Path)
args=parser.parse_args()
ORIGIN='https://review.test'


class Sock:family=socket.AF_UNIX


def fixture(folder):
    now=time.time();store=Store(folder/'usage.db')
    with patch('llm_usage.store.time.time',return_value=now),store.connect() as c:
        for day in range(10):
            for i,(route,model,project) in enumerate((('codex','gpt-test','alpha'),('claude-code','claude-test','beta'))):
                store.event(c,f'e{day}-{i}',now-day*86400-3600*(i+1),'OpenAI' if route=='codex' else 'Anthropic',route,model,
                            dict(uncached_input=200_000,cached_input=800_000,output=50_000),session=f's{day%3}-{i}',project=project,agent_kind='main')
        # Codex weekly: 3 hours of steady use so a forecast exists; Claude 5h: plenty left.
        for k in range(37):
            store.limit(c,'codex','codex · 10080분',70-k*0.5,now+3*86400,now-3*3600+k*300,'codex')
            store.limit(c,'codex','codex · 300분',75-k*1.5,now+4*3600,now-3*3600+k*300,'codex')
        store.limit(c,'claude-code','five_hour',90,now+2*3600,now-60,'claude-oauth')
        store.limit(c,'claude-code','seven_day',60,now+20*3600,now-60,'claude-oauth')
        for name in ('codex','claude-oauth'):store.source(c,name,'ok','',now-60)
        store.save_state(c,'collector',dict(status='idle',checked=now-30))
        store.save_state(c,'last_local',now-60)
    with patch('llm_usage.notify.urllib.request.urlopen',side_effect=OSError('down')):
        notify.deliver(store,{'ntfy_url':'https://ntfy.example/topic'},[('low','Codex 주간 잔여 12%','본문')],now)
    return store


async def main():
    with tempfile.TemporaryDirectory(prefix='llm-usage-review-') as directory:
        folder=Path(directory);fixture(folder)
        (folder/'config.json').write_text('{}')
        app=create_app(dict(database=str(folder/'usage.db'),origin=ORIGIN,secret_key='k'*32,allowed_logins=['owner'],
                            config_file=str(folder/'config.json'),subscription_prices={'codex':20,'claude-code':20},
                            pricing={'gpt-test':dict(input=2,cached=0.2,output=8),'claude-test':dict(input=3,cached=0.3,output=15)},
                            project_budgets={'alpha':dict(tokens=None,usd=10)}))
        client=app.test_client();offline=[False]

        async def handle(route):
            assert route.request.url.startswith(ORIGIN+'/'), route.request.url
            request=route.request
            if offline[0]:await route.abort('internetdisconnected');return
            headers={k:v for k,v in request.headers.items() if k.lower() in ('content-type','x-csrf-token','origin')}
            response=client.open(request.url.replace(ORIGIN,''),method=request.method,data=request.post_data,
                                 headers={**headers,'Tailscale-User-Login':'owner','Host':'review.test'},environ_base={'gunicorn.socket':Sock()})
            body=response.get_data();response.close()
            await route.fulfill(status=response.status_code,body=body,
                                headers={k:v for k,v in response.headers.items() if k.lower()!='content-length'})

        async with async_playwright() as p:

            browser=await p.chromium.launch()
            context=await browser.new_context(viewport={'width':1440,'height':1000})
            await context.route('**/*',handle)
            page=await context.new_page();errors=[]
            page.on('pageerror',lambda error:errors.append(str(error)));watch_csp(page,errors)
            await page.goto(ORIGIN+'/')
            await expect(page.locator('#updated')).to_contain_text('마지막 갱신')

            # A job decision replaces cross-provider percentage rankings. Claude
            # has no observed pace here, so it must not become a confident pick.
            await expect(page.locator('#work-decision')).to_contain_text('부족 예상')
            await expect(page.locator('#quota-strip')).to_be_hidden()
            assert await page.locator('.decision-alternatives button').count()==0
            dots=await page.locator('.reset-dot').evaluate_all('ds=>ds.map(d=>parseFloat(d.style.left))')
            assert len(dots)>=2 and all(0<x<=100 for x in dots),dots
            # The pace mark sits at the even-pace position, not at the bar's left edge.
            await page.locator('#quota-codex .bucket-detail summary').first.click()
            mark=await page.locator('#quota-codex .pace-mark').first.evaluate('m=>parseFloat(getComputedStyle(m).left)/m.parentElement.clientWidth')
            assert 0.3<mark<0.95,mark

            # Analysis: presets, the unified total and what-if pricing.
            await page.keyboard.press('2')
            await expect(page.locator('#tab-analysis')).to_have_attribute('aria-selected','true')
            await page.locator('[data-preset="models"]').click()
            await expect(page.locator('#group')).to_have_value('model')
            await expect(page.locator('#period')).to_have_value('30d')
            await expect(page.locator('#refresh')).to_be_enabled()
            totals=await page.evaluate('[total(lastUsage.totals),modelTotal(lastUsage.totals)]')
            assert totals[0]==totals[1],totals
            await page.locator('#whatif-model').focus()
            await expect(page.locator('#whatif-model option[value="claude-test"]')).to_be_attached()
            await page.locator('#whatif-model').select_option('claude-test')
            await expect(page.locator('#whatif-result')).to_contain_text('claude-test 단가로')

            # Insights: value bars have width, the project detail opens without drilling.
            # Shortcuts ignore keys typed into fields; leave the select first.
            await page.evaluate('document.activeElement.blur()')
            await page.keyboard.press('3')
            # The lazy insights answer redraws the panel; wait for the bar to carry its width.
            await page.wait_for_function("(document.querySelector('#subvalue .value-bar i')||{getBoundingClientRect:()=>({width:0})}).getBoundingClientRect().width>10")
            url=page.url
            await page.locator('.proj-open[data-project="alpha"]').click()
            await expect(page.locator('#projects .sess-detail')).to_contain_text('월말 예상')
            await expect(page.locator('#projects .sess-detail')).to_contain_text('예산')
            assert page.url==url,page.url

            # Settings: the notification history lists the failed send waiting for retry.
            await page.evaluate('document.activeElement.blur()')
            await page.keyboard.press('5')
            await page.locator('.notify-log summary').click()
            await expect(page.locator('#notify-log')).to_contain_text('전송 실패 · 재시도')
            if args.artifacts:
                args.artifacts.mkdir(parents=True,exist_ok=True)
                await page.evaluate('document.activeElement.blur()')
                await page.keyboard.press('1');await page.screenshot(path=str(args.artifacts/'review-overview.png'),full_page=True)

            # Offline: the root worker serves the last page copy and the page the last data.
            await page.evaluate('navigator.serviceWorker.ready')
            await page.goto(ORIGIN+'/?view=overview');await expect(page.locator('#updated')).to_contain_text('마지막 갱신')
            assert await page.evaluate('!!navigator.serviceWorker.controller')
            offline[0]=True;await context.set_offline(True)
            await page.goto(ORIGIN+'/?view=overview')
            await expect(page.locator('#offline')).to_be_visible()
            await expect(page.locator('#offline')).to_contain_text('오프라인')
            await expect(page.locator('#limits .limit-card').first).to_be_visible()
            offline[0]=False;await context.set_offline(False)
            # A regular refresh (no collector runs here) replaces the copy with live data.
            await page.evaluate('refresh()')
            await expect(page.locator('#offline')).to_be_hidden()
            assert not errors,errors
            await browser.close()
    print('Review browser passed: geometry under CSP, risk strip, reset axis, presets, shortcuts, what-if, project detail, notification log, offline copy.')


asyncio.run(main())
