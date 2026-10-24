"""Consumption navigation, real Flask projections and mobile layouts with synthetic data."""
import argparse
import json
from pathlib import Path
import socket
import tempfile
import time
from types import SimpleNamespace
from urllib.parse import urlsplit

from browser_support import fixture_page,watch_csp,artifact_directory,CSP_HEADERS
from playwright.sync_api import sync_playwright,expect
from llm_usage.store import Store
from llm_usage.webapp import create_app
from llm_usage import codex_activity as activity
from test_codex_activity import task,detail,usage,daily,SECRET

parser=argparse.ArgumentParser();parser.add_argument('--artifacts',type=Path);args=parser.parse_args()
artifacts=artifact_directory(args.artifacts)
with tempfile.TemporaryDirectory(prefix='codex-browser-') as temporary:
    now=time.time();store=Store(Path(temporary)/'usage.db');account='browser-account'
    with store.connect() as c:
        store.save_state(c,'codex_activity:account',dict(key=account,status='active'))
        for i in range(25):
            item=task(i,status='failed' if i==1 else 'cancelled' if i==2 else 'completed')
            item.update(created_at=now-100-i*60,updated_at=now-10)
            key=activity.record_task(c,account,item,now)
            sid=f'{i:08x}-2222-4333-8444-555555555555'
            raw=detail(item,sid=sid)
            if i==2:raw.update(current_assistant_turn=None,current_diff_task_turn=None)
            activity.record_detail(c,key,raw,now)
            if i!=2:activity.record_consumption(c,account,usage(sid=sid,status='partial' if i<2 else 'unavailable',weekly=2.5,as_of=now-90000),[sid],now)
        from datetime import datetime,timedelta,timezone
        dates={}
        for i in range(7):
            raw=daily();raw['data'][0]['date']=(datetime.now(timezone.utc).date()-timedelta(days=i)).isoformat()
            dates[i]=raw['data'][0]['date']
            if i==1:raw['data'][0]['models'][0]['credits']=3
            if i==2:
                raw['data'][0]['models']=[dict(model='gpt-zero' if j==0 else f'gpt-model-{j}',speed='standard',credits=j/10) for j in range(12)]
            if i==3:raw['data'][0].update(attribution=[],models=[],product_surface_usage_values={})
            if i in (4,5):
                value=0 if i==4 else .0002
                raw['data'][0]['attribution'][0]['value']=value
                raw['data'][0]['models'][0]['credits']=value
                raw['data'][0]['product_surface_usage_values']['github_code_review']=value
            activity.record_daily(c,account,raw,now)
        for source in activity.SOURCES:store.source(c,source,'partial','Synthetic partial coverage',now)
        store.save_state(c,'codex_activity:plan:'+account,dict(approximate=True,coverage_complete=False,data_as_of=now-86400))
    app=create_app(dict(database=store.path,origin='https://dashboard.test',secret_key='synthetic',allowed_logins=['fixture']))
    client=app.test_client();kwargs=dict(headers={'Tailscale-User-Login':'fixture'},environ_overrides={'gunicorn.socket':SimpleNamespace(family=socket.AF_UNIX)})
    fail=[False];calls=[]
    def handle(route):
        url=urlsplit(route.request.url);calls.append(url.path)
        if fail[0]:route.fulfill(status=503,json={'error':'Synthetic fetch failure'},headers=CSP_HEADERS);return
        response=client.get(url.path+('?' +url.query if url.query else ''),**kwargs)
        route.fulfill(status=response.status_code,body=response.get_data(),headers={k:v for k,v in response.headers.items() if k.lower()!='content-length'})
        response.close()
    def day_point(page,day):
        page.locator('#consumption-chart > svg').scroll_into_view_if_needed()
        return page.locator(f'[data-consumption-day="{day}"] > rect').first.evaluate('''e=>{
            const r=e.getBoundingClientRect();return {x:r.x+r.width/2,y:r.y+r.height/2};}''')
    def hover_day(page,day):
        point=day_point(page,day);page.mouse.move(point['x'],point['y'])
        expect(page.locator('#consumption-tooltip')).to_be_visible()
    with sync_playwright() as p:
        browser=p.chromium.launch();page=browser.new_page(viewport={'width':1440,'height':1000},service_workers='block')
        fixture_page(page);page.route('**/api/codex/**',handle)
        errors=[];page.on('pageerror',lambda e:errors.append(str(e)));watch_csp(page,errors)
        page.goto('https://dashboard.test/')
        expect(page.locator('#updated')).to_contain_text('마지막 갱신')
        assert not calls,'Quota tab fetched historical consumption'
        page.locator('[data-open-consumption]').click()
        expect(page.locator('#usage-consumption')).to_be_visible()
        expect(page.locator('#consumption-chart > svg')).to_be_visible()
        expect(page.locator('#usage-tokens')).to_be_hidden()
        expect(page.locator('#filter-fold')).to_be_hidden()
        expect(page.locator('#consumption-summary')).to_contain_text('근사값')
        assert 'usage_view=consumption' in page.url
        hover_day(page,dates[0]);tip=page.locator('#consumption-tooltip')
        expect(tip.locator('.tooltip-heading strong')).to_have_text(dates[0])
        expect(tip).to_contain_text('제공사 기준일 · 기능별 한도 소비율')
        expect(tip.locator('.tooltip-row strong')).to_have_text('2%')
        expect(tip.locator('.tooltip-total strong')).to_have_text('2%')
        assert tip.locator('.tooltip-row svg').bounding_box()['width']==9
        page.screenshot(path=artifacts/'consumption-desktop.png',full_page=True)
        hover_day(page,dates[4]);expect(tip.locator('.tooltip-total strong')).to_have_text('0%')
        hover_day(page,dates[5]);expect(tip.locator('.tooltip-total strong')).to_have_text('<0.001%')
        hover_day(page,dates[3]);expect(tip).to_contain_text('이 분류의 값 미제공')
        expect(tip.locator('.tooltip-total strong')).to_have_text('—')
        page.locator('#consumption-group').select_option('model')
        expect(page.locator('#consumption-detail')).to_contain_text('gpt-test')
        hover_day(page,dates[1]);expect(tip).to_contain_text('분해 합계 불일치')
        expect(tip.locator('.tooltip-total strong')).to_have_text('3%')
        hover_day(page,dates[2]);expect(tip.locator('.tooltip-row')).to_have_count(12)
        expect(tip.locator('.tooltip-total strong')).to_have_text('6.6%')
        expect(tip).to_have_class('chart-tooltip scrollable')
        # Entering and scrolling a long tooltip must not redraw it through ResizeObserver.
        tip.scroll_into_view_if_needed();tip.hover(position={'x':20,'y':20});page.mouse.wheel(0,600)
        expect(tip.locator('.tooltip-row').filter(has_text='gpt-zero')).to_be_in_viewport()
        tip.focus();page.keyboard.press('Escape');expect(tip).to_be_hidden()
        svg=page.locator('#consumption-chart > svg');svg.focus();page.keyboard.press('End')
        expect(tip.locator('.tooltip-heading strong')).to_have_text(dates[0])
        page.keyboard.press('ArrowLeft');page.keyboard.press('Enter')
        expect(page.locator('#consumption-day')).to_have_value(dates[1])
        page.keyboard.press('Escape');expect(tip).to_be_hidden()
        page.locator('#consumption-days').select_option('30')
        expect(page.locator('#consumption-summary')).to_contain_text('/ 30일')
        svg.focus();page.keyboard.press('Home');expect(tip).to_contain_text('미수신')
        expect(tip.locator('.tooltip-total strong')).to_have_text('—')
        page.locator('#consumption-days').select_option('7')
        expect(page.locator('#consumption-summary')).to_contain_text('/ 7일')
        page.locator('#usage-tab-activity').click()
        expect(page.locator('#activity-summary')).to_contain_text('25개')
        expect(page.locator('#activity-rows [data-task-id]')).to_have_count(20)
        page.locator('#activity-rows [data-task-id]').first.click()
        expect(page.locator('.thread-consumption')).to_contain_text('2.5%')
        expect(page.locator('.thread-consumption')).to_contain_text('오래된 집계')
        expect(page.locator('.activity-detail-row')).to_contain_text('원본 입력·출력·캐시 토큰은 미제공')
        page.locator('[data-codex-retry="activity"]').click()
        expect(page.locator('#activity-status')).to_contain_text('Synthetic partial coverage')
        expect(page.locator('.thread-consumption')).to_contain_text('2.5%')
        assert SECRET not in page.content()
        page.screenshot(path=artifacts/'activity-desktop.png',full_page=True)
        page.locator('#activity-next').click();expect(page.locator('#activity-rows [data-task-id]')).to_have_count(5)
        page.locator('#activity-state').select_option('failed')
        expect(page.locator('#activity-summary')).to_contain_text('1개')
        expect(page.locator('#activity-rows')).to_contain_text('실패')
        expect(page.locator('#activity-rows')).to_contain_text('2.5%')
        page.locator('#activity-state').select_option('cancelled')
        expect(page.locator('#activity-rows')).to_contain_text('스레드 미연결')
        assert '0%' not in page.locator('#activity-rows').inner_text()
        page.locator('#activity-kind').select_option('cloud')
        expect(page.locator('#activity-rows')).to_contain_text('수집된 작업이 없습니다')
        page.locator('#activity-kind').select_option('');page.locator('#activity-state').select_option('')
        expect(page.locator('#activity-summary')).to_contain_text('25개')
        fail[0]=True;page.locator('[data-codex-retry="activity"]').click()
        expect(page.locator('#activity-status')).to_contain_text('이전 화면을 유지')
        expect(page.locator('#activity-summary')).to_contain_text('25개')
        page.locator('#activity-period').select_option('30d')
        expect(page.locator('#activity-status')).to_contain_text('이 조건의 데이터를 표시할 수 없습니다')
        expect(page.locator('#activity-rows [data-task-id]')).to_have_count(0)
        fail[0]=False;page.locator('[data-codex-retry="activity"]').click()
        expect(page.locator('#activity-summary')).to_contain_text('25개')
        for width in (390,320):
            page.set_viewport_size({'width':width,'height':844})
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth'),width
            page.locator('#activity-rows [data-task-id]').first.click()
            expect(page.locator('.thread-consumption')).to_be_visible()
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth'),width
            page.screenshot(path=artifacts/f'activity-{width}.png')
            page.locator('#usage-tab-consumption').click()
            expect(page.locator('#consumption-chart > svg')).to_be_visible()
            expect(page.locator('#insights-status')).to_be_hidden()
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth'),width
            page.screenshot(path=artifacts/f'consumption-{width}.png')
            page.locator('#consumption-days').select_option('30')
            expect(page.locator('#consumption-summary')).to_contain_text('/ 30일')
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth'),width
            page.locator('#consumption-days').select_option('7')
            page.locator('#usage-tab-activity').click()
            # A fresh table is not required to close an existing detail.
            open_button=page.locator('#activity-rows [aria-expanded=true]')
            if open_button.count():open_button.click()
        page.locator('#usage-tab-tokens').click();expect(page.locator('#filter-fold')).to_be_visible()
        page.locator('#recent-usage').click();expect(page.locator('#period')).to_have_value('1h')
        page.locator('#usage-tab-consumption').click();expect(page.locator('#consumption-days')).to_have_value('7')
        page.locator('#usage-tab-consumption').focus();page.keyboard.press('ArrowRight')
        expect(page.locator('#usage-tab-activity')).to_be_focused()
        page.go_back();expect(page.locator('#usage-tab-consumption')).to_have_attribute('aria-selected','true')
        mobile=browser.new_page(viewport={'width':390,'height':844},service_workers='block',is_mobile=True,has_touch=True)
        fixture_page(mobile);mobile.route('**/api/codex/**',handle)
        mobile.on('pageerror',lambda e:errors.append(str(e)));watch_csp(mobile,errors)
        mobile.goto('https://dashboard.test/?view=usage&usage_view=consumption')
        expect(mobile.locator('#consumption-chart > svg')).to_be_visible()
        for width in (390,320):
            mobile.set_viewport_size({'width':width,'height':844})
            mobile.locator('#consumption-group').select_option('source')
            point=day_point(mobile,dates[0]);mobile.touchscreen.tap(point['x'],point['y'])
            tip=mobile.locator('#consumption-tooltip');expect(tip).to_be_visible()
            expect(tip.locator('.tooltip-total strong')).to_have_text('2%')
            bounds=tip.bounding_box();assert bounds['x']>=0 and bounds['x']+bounds['width']<=width
            mobile.screenshot(path=artifacts/f'consumption-tooltip-{width}.png')
            mobile.locator('#consumption-group').select_option('model')
            point=day_point(mobile,dates[2]);mobile.touchscreen.tap(point['x'],point['y'])
            expect(tip.locator('.tooltip-row')).to_have_count(12)
            expect(mobile.locator('#consumption-day')).to_have_value(dates[2])
            tip.scroll_into_view_if_needed();tip.evaluate('e=>e.scrollTop=e.scrollHeight')
            expect(tip.locator('.tooltip-row').filter(has_text='gpt-zero')).to_be_in_viewport()
            assert mobile.evaluate('document.documentElement.scrollWidth<=innerWidth'),width
        mobile.close()
        assert not errors,errors
        browser.close()
print('Codex browser passed: isolated API, three usage modes, filters, activity states, safe date tooltips, hover/touch/keyboard, missing/zero/tiny values, long tooltip scrolling, 1440/390/320px and history.')
