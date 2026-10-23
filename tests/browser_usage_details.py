"""Real Flask fixture responses: advisor, known zero, TTL prices and CSV on desktop/mobile."""
import argparse
import csv
from datetime import datetime,timezone
from pathlib import Path
import tempfile
import time
from urllib.parse import urlsplit

from browser_support import ORIGIN,Sock,artifact_directory,watch_csp
from playwright.sync_api import sync_playwright,expect
from llm_usage.claude_usage import ingest_message
from llm_usage.store import Store
from llm_usage.webapp import create_app
from test_usage_details import message,RATES

parser=argparse.ArgumentParser();parser.add_argument('--artifacts',type=Path);args=parser.parse_args()
output=artifact_directory(args.artifacts)
with tempfile.TemporaryDirectory(prefix='llm-details-browser-') as folder:
    path=Path(folder)/'usage.db';store=Store(path);now=time.time()
    with store.connect() as c:
        value=message();value['timestamp']=datetime.fromtimestamp(now-30,timezone.utc).isoformat()
        ingest_message(store,c,'claude-code',value)
        for model,details in [('claude-zero',{'thinking_tokens':0}),('claude-missing',{})]:
            extra=dict(value,message=dict(id=model,model=model,usage=dict(input_tokens=1,output_tokens=10,output_tokens_details=details)))
            ingest_message(store,c,'claude-code',extra)
        store.save_state(c,'last_local',now);store.save_state(c,'collector',dict(checked=now,status='idle'))
    client=create_app(dict(database=str(path),origin=ORIGIN,secret_key='synthetic-details-only',allowed_logins=['fixture'],pricing=RATES)).test_client()
    def fixture(page):
        def handle(route):
            request=route.request;url=urlsplit(request.url)
            assert url.scheme=='https' and url.netloc=='dashboard.test'
            headers={k:v for k,v in request.headers.items() if k.lower() in ('content-type','x-csrf-token','origin')}
            response=client.open(url.path+('?' +url.query if url.query else ''),method=request.method,data=request.post_data,
                headers={**headers,'Host':'dashboard.test','Tailscale-User-Login':'fixture'},environ_base={'gunicorn.socket':Sock()})
            try:route.fulfill(status=response.status_code,body=response.get_data(),headers={k:v for k,v in response.headers.items() if k.lower()!='content-length'})
            finally:response.close()
        page.route('**/*',handle)
    with sync_playwright() as p:
        browser=p.chromium.launch()
        for width in (1440,390,320):
            context=browser.new_context(viewport={'width':width,'height':1000},service_workers='block')
            page=context.new_page();fixture(page);errors=[]
            page.on('pageerror',lambda e:errors.append(str(e)));watch_csp(page,errors)
            page.goto(ORIGIN+'/?view=usage')
            page.wait_for_function("()=>lastUsage?.totals?.advisor_calls===1&&Array.isArray(lastUsage.sessions)")
            expect(page.locator('#cards')).to_contain_text('3회 요청 · Advisor 1회')
            assert page.evaluate('total(lastUsage.totals)')==542
            panel=page.locator('[data-panel="models"]')
            if panel.get_attribute('open') is None:panel.locator('summary').click()
            toggle=page.locator('#cols-toggle')
            if toggle.is_visible() and toggle.get_attribute('aria-pressed')!='true':toggle.click()
            advisor=page.locator('#rows tr[data-scope="model:claude-advisor"]')
            expect(advisor).to_contain_text('Advisor 1회')
            expect(page.locator('#rows tr[data-scope="model:claude-parent"]')).to_contain_text('5분 10 · 1시간 20')
            expect(page.locator('#rows tr[data-scope="model:claude-zero"]')).not_to_contain_text('미제공')
            expect(page.locator('#rows tr[data-scope="model:claude-missing"]')).to_contain_text('미제공')
            with page.expect_download() as download:page.locator('#csv-export').click()
            rows=list(csv.DictReader(Path(download.value.path()).read_text(encoding='utf-8-sig').splitlines()))
            parent=next(r for r in rows if r['모델']=='claude-parent')
            assert (parent['Cache creation'],parent['캐시 생성 5분'],parent['캐시 생성 1시간'],parent['Reasoning'])==('30','10','20','25')
            assert next(r for r in rows if r['모델']=='claude-advisor')['Advisor 내부 호출']=='1'
            assert next(r for r in rows if r['모델']=='claude-zero')['Reasoning']=='0'
            assert next(r for r in rows if r['모델']=='claude-missing')['Reasoning']==''
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
            page.screenshot(path=str(output/f'details-{width}.png'))
            page.locator('#settings-open').click()
            expect(page.locator('input[data-model="claude-parent"][data-key="cache_write_1h"]')).to_have_value('2')
            expect(page.locator('input[data-model="claude-parent"][data-key="cache_write_5m"]')).to_be_attached()
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
            assert not errors,errors
            context.close()
        browser.close()
print('Usage details passed: advisor count, TTL pricing, observed zero, CSV, desktop and 390/320px mobile.')
