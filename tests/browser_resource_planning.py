"""User decisions and manual-resource lifecycle through the real Flask API.

All data is numeric and disposable. Every browser request stays on fixture origin;
no provider request, credit purchase, redemption or notification is performed.
"""
import argparse
import json
import re
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit

from browser_support import ORIGIN, Sock, watch_csp, artifact_directory
from playwright.sync_api import sync_playwright, expect
from llm_usage.store import Store
from llm_usage.limits import codex_limits, claude_limits
from llm_usage.resources import observe_account, claude_balance, claude_resets, write_manual
from llm_usage.webapp import create_app

parser=argparse.ArgumentParser()
parser.add_argument('--artifacts',type=Path)
args=parser.parse_args()
output=artifact_directory(args.artifacts)
now=time.time()

with tempfile.TemporaryDirectory(prefix='llm-resource-browser-') as root:
    store=Store(Path(root)/'usage.db')
    with store.connect() as c:
        for day in range(7):
            for route,provider,model in (('codex','OpenAI','gpt-resource-demo'),('claude-code','Anthropic','claude-resource-demo')):
                store.event(c,f'fixture-{route}-{day}',now-day*86400-60,provider,route,model,
                            dict(uncached_input=20000,cached_input=100000,output=10000,cache_creation=0),session='fixture-session')
        observe_account(c,'claude-code',('fixture-user','fixture-org'),now-10800)
        for minute in range(180,-1,-5):
            checked=now-minute*60
            data={'accountId':'fixture-account','rateLimitsByLimitId':{
                'codex':{'limitName':'Codex 공통','primary':{'usedPercent':60-20*min(minute,60)/60,'windowDurationMins':300,'resetsAt':now+14400},
                         'secondary':{'usedPercent':88-8*minute/60,'windowDurationMins':10080,'resetsAt':now+259200},
                         'credits':{'balance':'25','hasCredits':True,'unlimited':False}},
                'special':{'limitName':'별도 모델','primary':{'usedPercent':100,'windowDurationMins':300,'resetsAt':now+3600}}},
                'rateLimitResetCredits':{'availableCount':2,'credits':[
                    {'id':'fixture-reset','expiresAt':now+3600,'status':'available'},
                    {'id':'fixture-reset-later','expiresAt':now+172800,'status':'available'},
                    {'id':'fixture-reset-expired','expiresAt':now-86400,'status':'expired'}]}}
            codex_limits(store,c,data,checked)
            claude_limits(store,c,{'five_hour':{'utilization':30-minute/30,'resets_at':now+14400},
                                  'seven_day':{'utilization':30-minute/60,'resets_at':now+259200},
                                  'seven_day_opus':{'utilization':100,'resets_at':now+259200},
                                  'iguana_necktie':{'limit_dollars':100,'used_dollars':58,'remaining_dollars':42,
                                                    'resets_at':now+86400,'locked_reason':None},
                                  'spend':{'enabled':True,'used':{'amount_minor':1000,'currency':'USD','exponent':2},
                                           'limit':{'amount_minor':3000,'currency':'USD','exponent':2}}},checked)
        claude_balance(c,{'amount':5000,'currency':'USD','next_expires_at':now+432000,'auto_reload_settings':{'enabled':False}},now)
        claude_resets(c,{'cedar_ember':{'eligible':True,'grants':[{'id':'fixture-claude-reset','resets_left':1,
            'clears':['five_hour'],'ends_at':now+86400,'usable_now':False}]}},now)
        for name in ('codex','claude-oauth','claude-credits','claude-resets'):
            store.source(c,name,'ok','Synthetic resource scenario',now)
        for name in ('antigravity','opencode-go','devin'):
            store.source(c,name,'ended','Synthetic inactive route',now)
        store.save_state(c,'collector',{'checked':now,'status':'idle'})
        store.save_state(c,'last_local',now)
    app=create_app(dict(database=str(Path(root)/'usage.db'),origin=ORIGIN,secret_key='fixture-resource-key',allowed_logins=['fixture'],
                       pricing={m:dict(input=2,cached=.2,output=8) for m in ('gpt-resource-demo','claude-resource-demo')}))
    client=app.test_client()

    def call(path,method='GET',data=None):
        headers={'Host':'dashboard.test','Tailscale-User-Login':'fixture','Origin':ORIGIN}
        if method!='GET':headers['X-CSRF-Token']=call('/api/bootstrap').get_json()['csrf']
        return client.open(path,method=method,json=data,headers=headers,environ_base={'gunicorn.socket':Sock()})

    def response(request):
        url=urlsplit(request.url)
        assert url.scheme=='https' and url.netloc=='dashboard.test',request.url
        headers={k:v for k,v in request.headers.items() if k.lower() in ('content-type','x-csrf-token','origin')}
        return client.open(url.path+('?' +url.query if url.query else ''),method=request.method,data=request.post_data,
                           headers={**headers,'Host':'dashboard.test','Tailscale-User-Login':'fixture'},
                           environ_base={'gunicorn.socket':Sock()})

    def page_fixture(page):
        def handle(route):
            res=response(route.request)
            try:route.fulfill(status=res.status_code,body=res.get_data(),headers={k:v for k,v in res.headers.items() if k.lower()!='content-length'})
            finally:res.close()
        page.route('**/*',handle)

    with sync_playwright() as p:
        browser=p.chromium.launch()
        for width in (1440,390,320):
            context=browser.new_context(viewport={'width':width,'height':900},service_workers='block')
            page=context.new_page();page_fixture(page)
            errors=[];page.on('pageerror',lambda error:errors.append(str(error)));watch_csp(page,errors)
            page.goto(ORIGIN)
            expect(page.locator('#work-decision')).to_contain_text('1시간 30분')
            expect(page.locator('#work-decision')).to_contain_text('부족 예상')
            expect(page.locator('#work-decision')).to_contain_text('Claude')
            expect(page.locator('#work-decision')).to_contain_text('Sonnet')
            expect(page.locator('#work-decision')).not_to_contain_text('Opus 기준 보기')
            # Subscription quota must be the first overview content on every width.
            expect(page.locator('#view-quota > .section-heading h2').first).to_have_text('구독 한도')
            if width<600:page.locator('[data-quota="codex"]').click()
            page.evaluate('scrollTo(0,0)')
            quota=page.locator('#quota-codex').bounding_box()
            assert quota['y'] < 900
            assert page.locator('#limits').bounding_box()['y'] < page.locator('#resource-panel').bounding_box()['y']
            # 지금 작업 has its own tab after the quota tab; the quota tab never shows it.
            expect(page.locator('#work-now')).to_be_hidden()
            tabs=page.locator('#tabs [role=tab]').evaluate_all('ts=>ts.map(t=>t.id)')
            assert tabs.index('tab-quota')==0 and tabs.index('tab-plan')==1,tabs
            if width==1440:page.screenshot(path=str(output/'dashboard.png'))
            expect(page.locator('#quota-codex .bucket-pace').first).to_be_visible()
            assert page.locator('#quota-codex .bucket-detail[open]').count()==0
            expect(page.locator('#resource-list')).to_contain_text('2회')
            expect(page.locator('#resource-list')).to_contain_text('25 크레딧')
            expect(page.locator('#resource-list')).to_contain_text('Claude 클라우드 전용 크레딧')
            expect(page.locator('#resource-list')).to_contain_text('42 USD')
            expect(page.locator('#resource-list')).to_contain_text('지급액 100 USD')
            expect(page.locator('#resource-list')).to_contain_text('클라우드 세션 전용')
            expect(page.locator('#resource-list')).to_contain_text('1회')
            expect(page.locator('#work-decision')).not_to_contain_text('클라우드 전용 크레딧')
            # Every provider-supplied expiry is visible without opening details.
            items=call('/api/limits').get_json()['resources']['items']
            for item in items:
                card=page.locator(f'[data-resource-id="{item["id"]}"]')
                expiries=[g['expires'] for g in item.get('grants',[])] or [item.get('period_reset') if item.get('allowance') else item.get('expires')]
                dates=card.locator('.resource-expiry time')
                assert dates.count()==len([e for e in expiries if e])
                for index,expiry in enumerate(e for e in expiries if e):
                    expect(dates.nth(index)).to_be_visible()
                    assert abs(page.evaluate('(value) => Date.parse(value)/1000',dates.nth(index).get_attribute('datetime'))-expiry)<.001
                    assert 'KST' in dates.nth(index).inner_text()
                if not any(expiries):expect(card.locator('.resource-expiry')).to_contain_text('정보 미제공')
                assert card.locator('details[open]').count()==0
            codex_reset=next(r for r in items if r['route']=='codex' and r['kind']=='reset')
            reset_card=page.locator(f'[data-resource-id="{codex_reset["id"]}"]')
            expect(reset_card.locator('.resource-grants li')).to_have_count(3)
            expect(reset_card).to_contain_text('시각 경과')
            expect(reset_card).to_contain_text('만료됨')
            credit=next(r for r in items if r['route']=='claude-code' and r['pool_key']=='prepaid-credits')
            expect(page.locator(f'[data-resource-id="{credit["id"]}"] .resource-expiry')).to_contain_text('일부 잔액 다음 만료')
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
            # When each quota comes back is readable without opening anything.
            expect(page.locator('#reset-timeline')).to_be_visible()
            expect(page.locator('#reset-timeline .reset-item')).to_have_count(4)
            expect(page.locator('#reset-timeline .reset-item').first).to_contain_text('3시간')
            if width<600:
                windows=page.locator('[data-quota="codex"] .ov-win')
                expect(windows).to_have_count(2)
                for index in range(2):expect(windows.nth(index).locator('small')).to_contain_text('시간')
            # Resource rows stay short: one line per fact, plus one line per reset grant.
            sizes=page.locator('#resource-list .resource-card').evaluate_all(
                "cs=>cs.map(c=>[c.getBoundingClientRect().height,c.querySelectorAll('.resource-grants li').length])")
            assert sizes and all(height<=180+50*max(0,grants-1) for height,grants in sizes),sizes
            page.screenshot(path=str(output/f'work-now-{width}.png'),full_page=True)
            if width==1440:
                page.evaluate("() => document.querySelector('.sticky-nav').style.visibility='hidden'")
                page.locator('#resource-panel').screenshot(path=str(output/'resource-expiry.png'))
                page.evaluate("() => document.querySelector('.sticky-nav').style.visibility=''")
            # The manual editor is a modal dialog: centred panel on desktop, bottom sheet on phones.
            page.locator('#resource-add').click()
            expect(page.locator('#resource-dialog')).to_have_attribute('open','')
            assert page.evaluate("document.getElementById('resource-dialog').matches(':modal')")
            expect(page.locator('#resource-label')).to_be_focused()
            box=page.locator('#resource-dialog').bounding_box()
            if width>600:
                assert abs(box['width']-640)<=1 and abs(box['x']+box['width']/2-width/2)<=1,box
            else:
                assert abs(box['x'])<=1 and abs(box['width']-width)<=1 and abs(box['y']+box['height']-900)<=1 and box['height']<=900*.9+1,box
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
            assert page.evaluate("(() => { const d=document.getElementById('resource-dialog'); return d.scrollWidth<=d.clientWidth; })()")
            expect(page.locator('#resource-save')).to_be_in_viewport()
            if width in (1440,390):page.screenshot(path=str(output/f'resource-dialog-{width}.png'))
            page.keyboard.press('Escape')
            expect(page.locator('#resource-dialog')).not_to_have_attribute('open','')
            expect(page.locator('#resource-add')).to_be_focused()
            # Resources ending within a day become alert chips: the Codex reset credit due in an hour
            # (and the Claude one due in a day), not the one due in 2 days or the one already past.
            page.locator('#tab-plan').click()
            expect(page.locator('#view-quota')).to_be_hidden()
            if width<=600 and page.locator('#alerts-toggle').get_attribute('aria-expanded')!='true':page.locator('#alerts-toggle').click()
            chips=page.locator('.alert-chip').all_text_contents()
            codex_chips=[c for c in chips if '초기화권' in c and c.startswith('Codex')]
            assert len(codex_chips)==1 and re.fullmatch(r'Codex 초기화권 1개 \d+분 후 만료',codex_chips[0]),chips
            assert any(re.fullmatch(r'Claude 초기화권 1개 2\d시간 \d+분 후 만료',c) for c in chips),chips
            assert not any('사용 크레딧' in c and '만료' in c for c in chips),chips
            assert not any('2일' in c for c in chips),chips
            page.locator('.alert-chip',has_text='Codex 초기화권').click()
            expect(page.locator('#view-quota')).to_be_visible()
            expect(page.locator(f'[data-resource-id="{codex_reset["id"]}"]')).to_be_in_viewport()
            page.locator('#tab-plan').click()
            expect(page.locator('#work-now')).to_be_visible()
            if width==1440:page.screenshot(path=str(output/'plan-1440.png'),full_page=True)
            page.locator('#plan-model').select_option('special')
            expect(page.locator('#work-decision')).to_contain_text('구독 한도 소진')
            page.locator('#plan-route').select_option('claude-code')
            page.locator('#plan-model').select_option('opus')
            expect(page.locator('#work-decision')).to_contain_text('구독 한도 소진')
            page.locator('#plan-model').select_option('sonnet')
            expect(page.locator('#work-decision')).to_contain_text('관측상 여유')
            expect(page.locator('#work-decision')).not_to_contain_text('70시간')
            expect(page.locator('#work-budget')).to_be_visible()  # no click needed to open the forecast
            page.locator('#plan-today-hours').fill('3')
            page.locator('#plan-week-hours').fill('10')
            page.locator('#work-budget-controls button').click()
            expect(page.locator('#work-budget-results')).to_contain_text('3시간')
            expect(page.locator('#work-budget-results')).to_contain_text('초기화 후 재확인')
            assert not errors,errors
            context.close()

        context=browser.new_context(viewport={'width':390,'height':900},service_workers='block')
        page=context.new_page();page_fixture(page)
        errors=[];page.on('pageerror',lambda error:errors.append(str(error)));watch_csp(page,errors)
        page.goto(ORIGIN);expect(page.locator('#work-decision')).to_contain_text('부족 예상')
        page.locator('#resource-add').click()
        # Escape asks before dropping typed input, keeps the dialog on 'cancel', closes on accept.
        page.locator('#resource-label').fill('임시 입력')
        messages=[]
        page.once('dialog',lambda d:(messages.append(d.message),d.dismiss()))
        page.keyboard.press('Escape')
        assert messages==['저장하지 않은 입력을 닫을까요?'],messages
        expect(page.locator('#resource-dialog')).to_have_attribute('open','')
        expect(page.locator('#resource-label')).to_have_value('임시 입력')
        page.once('dialog',lambda d:(messages.append(d.message),d.accept()))
        page.keyboard.press('Escape')
        assert len(messages)==2
        expect(page.locator('#resource-dialog')).not_to_have_attribute('open','')
        expect(page.locator('#resource-add')).to_be_focused()
        page.locator('#resource-add').click()
        expect(page.locator('#resource-label')).to_have_value('')
        page.locator('#resource-kind').select_option('api_credit')
        page.locator('#resource-label').fill('API 전용 검토 기록')
        page.locator('#resource-amount').fill('20')
        page.locator('#resource-expires').fill(page.evaluate('(value) => resourceTime(value)',now+172800))
        page.locator('#resource-save').click()
        expect(page.locator('#resource-message')).to_contain_text('저장했습니다')
        expect(page.locator('#resource-list')).to_contain_text('API 전용 · 구독 한도에 미포함')
        expect(page.locator('#work-decision')).not_to_contain_text('API 전용 검토 기록')
        record=next(r for r in call('/api/limits').get_json()['resources']['items'] if r['origin']=='manual')
        expect(page.locator(f'[data-resource-id="{record["id"]}"] .resource-expiry time')).to_be_visible()
        concurrent={k:record.get(k) for k in ('route','kind','label','amount','unit','scope','model','expires','checked','enabled','spend_remaining','spend_unlimited','auto_reload','linked_id','revision')}
        concurrent['amount']=12
        assert call('/api/resources/manual/'+record['id'],'PATCH',concurrent).status_code==200
        page.locator('#resource-amount').fill('15')
        page.locator('#resource-save').click()
        expect(page.locator('#resource-message')).to_contain_text('다른 화면에서 변경')
        expect(page.locator('#resource-amount')).to_have_value('15')
        page.locator('#resource-reload').click()
        expect(page.locator('#resource-amount')).to_have_value('12')
        page.locator('#resource-cancel').click()
        expect(page.locator('#resource-dialog')).not_to_have_attribute('open','')
        # An edit button reopens the same dialog and gets focus back on close.
        page.locator(f'[data-resource-edit="{record["id"]}"]').click()
        expect(page.locator('#resource-dialog')).to_have_attribute('open','')
        expect(page.locator('#resource-editor-title')).to_have_text('수동 기록 수정')
        page.locator('#resource-cancel').click()
        expect(page.locator(f'[data-resource-edit="{record["id"]}"]')).to_be_focused()

        # An ambiguous network failure after committing a new record is retried
        # with the same request identity and does not create a second resource.
        fail_once={'done':False}
        def ambiguous(route):
            if route.request.method=='POST' and not fail_once['done']:
                res=response(route.request);assert res.status_code==200;res.close()
                fail_once['done']=True;route.abort('connectionreset')
            else:route.fallback()
        page.route('**/api/resources/manual',ambiguous)
        page.locator('#resource-add').click()
        page.locator('#resource-label').fill('응답 실패 후 재시도')
        page.locator('#resource-amount').fill('10')
        page.locator('#resource-save').click()
        expect(page.locator('#resource-message')).to_contain_text('입력 내용은 유지')
        page.locator('#resource-save').click()
        expect(page.locator('#resource-message')).to_contain_text('저장했습니다')
        items=call('/api/limits').get_json()['resources']['items']
        assert len([r for r in items if r['label']=='응답 실패 후 재시도'])==1
        page.locator('#resource-cancel').click()

        # Cache freshness, countdown and recommendations age together even with
        # auto refresh off; then a fresh server reply restores the decision.
        page.evaluate("() => { document.getElementById('auto').checked=false; window.originalNow=Date.now; }")
        page.wait_for_function("async()=>!!(await (await caches.open('llm-usage-data-v1')).match('/api/limits'))")
        page.route('**/api/limits*',lambda route:route.abort('internetdisconnected'))
        page.evaluate('Date.now=()=>window.originalNow()+7200000')
        page.evaluate('refresh()')
        expect(page.locator('#offline')).to_be_visible()
        expect(page.locator('#work-decision')).to_contain_text('판단 보류')
        expect(page.locator('#work-decision')).not_to_contain_text('Sonnet 기준 보기')
        expect(page.locator('#quota-overview')).not_to_contain_text('최근 확인')
        assert page.locator('.decision-alternatives button').count()==0
        assert page.evaluate('lastLimits.limits.every(r=>r.status!=="fresh")')
        page.evaluate('Date.now=window.originalNow')
        page.unroute('**/api/limits*')
        page.evaluate('refresh()')
        expect(page.locator('#work-decision')).to_contain_text('부족 예상')
        expect(page.locator('#offline')).to_be_hidden()
        assert not errors,errors
        context.close();browser.close()
print('Resource planning browser passed: 3 widths, quota-first overview, visible per-grant/credit expiry, reset/cloud credits, model constraints, manual CRUD/conflict/retry, offline recovery.')
