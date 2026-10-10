"""Real Chromium checks for new insights and delayed filter responses, all API data fixture-only."""
from browser_support import CSP_HEADERS, watch_csp
import argparse
import asyncio
from pathlib import Path
import re
import tempfile
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from playwright.async_api import async_playwright, expect

from llm_usage.store import Store, stamp

WEB=Path(__file__).resolve().parents[1]/'llm_usage/web'
ORIGIN='https://dashboard.test'
NOW=stamp('2026-09-09T03:00:00Z')
parser=argparse.ArgumentParser()
parser.add_argument('--artifacts',type=Path)
args=parser.parse_args()


async def main():
    with tempfile.TemporaryDirectory(prefix='llm-usage-browser-') as directory:
        store=Store(Path(directory)/'usage.db')
        # Store.limit() keeps history within retention of the real clock; pin it to NOW
        # so the fixed 2026-09-09 fixture does not expire after 30 days.
        with patch('llm_usage.store.time.time',return_value=NOW),store.connect() as c:
            for key,date,scale,model in [('baseline','2026-09-01T00:00:00+09:00',1,'gpt-old'),
                    ('before','2026-09-08T10:00:00+09:00',1,'gpt-test'),
                    ('today','2026-09-09T10:00:00+09:00',2,'gpt-test')]:
                store.event(c,key,stamp(date),'OpenAI','codex',model,dict(uncached_input=10*scale,cached_input=30*scale,cache_creation=20*scale,output=5*scale))
            # Rewriting identical values is a no-op for totals but attaches the session.
            store.event(c,'today',stamp('2026-09-09T10:00:00+09:00'),'OpenAI','codex','gpt-test',
                dict(uncached_input=20,cached_input=60,cache_creation=40,output=10),session='fixture-s1',project='fixture-proj',agent_kind='main')
            # A model with no pricing: its day/hour cells are "미산정", never $0.
            store.event(c,'unrated',stamp('2026-09-07T09:00:00+09:00'),'OpenAI','codex','gpt-noprice',
                dict(uncached_input=7,cached_input=0,cache_creation=0,output=3))
            # Two hours of observations: a weekly window is only forecast after 90 minutes.
            for i in range(25):
                store.limit(c,'codex','codex · 10080분',94-i,NOW+86400,NOW-7200+i*300,'codex')
            store.limit(c,'codex','codex_bengalfox · 300분',88,NOW+3600,NOW,'codex')
            for bucket,value in [('gemini-5h',90),('gemini-weekly',40),('3p-5h',80),('3p-weekly',30)]:
                store.limit(c,'antigravity',bucket,value,NOW+86400,NOW,'antigravity')
            # A display name already carrying the service name must not be doubled
            # in the reset timeline ("Codex Codex …").
            store.save_state(c,'label:codex:codex · 10080분','Codex 주간')
            store.source(c,'codex','ok','',NOW)
            store.source(c,'opencode-go','ended','계정에 구독이 없습니다.')
            store.save_state(c,'last_local',NOW-120)
        limits=store.limits(NOW)
        async with async_playwright() as p:

            browser=await p.chromium.launch()
            page=await browser.new_page(service_workers='block',viewport={'width':1440,'height':1050})
            errors=[];page.on('pageerror',lambda error:errors.append(str(error)));watch_csp(page,errors)
            seen=[];block_next=[False];html_error=[False];bootstrapped=[False]
            started=asyncio.Event();release=asyncio.Event()
            async def route(request):
                assert request.request.url.startswith(ORIGIN+'/'), request.request.url
                path=urlsplit(request.request.url).path
                if path=='/':await request.fulfill(path=WEB/'index.html',content_type='text/html',headers=CSP_HEADERS)
                elif path.startswith('/assets/'):
                    name=Path(path).name
                    await request.fulfill(path=WEB/name,content_type='text/css' if name.endswith('.css') else 'text/javascript')
                elif path=='/api/bootstrap':
                    # The first token becomes stale once the session expires.
                    token='fixture-fresh' if bootstrapped[0] else 'fixture-stale'
                    bootstrapped[0]=True
                    await request.fulfill(json={'csrf':token})
                elif path=='/api/limits':await request.fulfill(json=limits)
                elif path=='/api/notify/log':await request.fulfill(json={'log':[]})
                elif path=='/api/reports':await request.fulfill(json={'reports':[]})
                elif path=='/api/config/thresholds':
                    ok=request.request.headers.get('x-csrf-token')=='fixture-fresh'
                    await request.fulfill(status=200 if ok else 403,
                        json={'thresholds':{}} if ok else {'error':'세션이 만료됐습니다.'})
                elif path=='/api/config':
                    await request.fulfill(json={
                        'subscription_prices':{'codex':123,'claude-code':45},
                        'pricing':{'gpt-test':{'input':1,'output':2}},
                        'pricing_origins':{'gpt-test':'user','gpt-old':'builtin'},
                        'pricing_builtin':['gpt-old'],
                        'models':[{'model':'gpt-test','rate':{'input':1,'output':2},'key':'gpt-test'},
                                  {'model':'gpt-old','rate':{'input':1,'output':2},'key':'gpt-old'},
                                  {'model':'gpt-noprice','rate':None,'key':None}],
                        'thresholds':{'stale_seconds':3600,'retention_days':30,'low_percent':20,'quota_hide_days':7},
                        'refresh_seconds':60,'value_alert_usd':None,'upcoming':{},'today':'2026-09-09'})
                elif path=='/api/usage':
                    if html_error[0]:
                        await request.fulfill(status=502,body='<html><body>Bad Gateway</body></html>',
                                              content_type='text/html');return
                    q={k:v[0] for k,v in parse_qs(urlsplit(request.request.url).query).items()}
                    seen.append(q.copy())
                    if block_next[0]:
                        block_next[0]=False;started.set();await release.wait()
                    await request.fulfill(json=store.usage(period=q['period'],start=q.get('start'),end=q.get('end'),
                        granularity=q['granularity'],group=q['group'],cumulative=q['cumulative']=='1',compare=q.get('compare') or None,scope=q.get('scope') or None,sections=q.get('sections','all'),now=NOW,
                        pricing={'gpt-test':dict(input=1,output=2),'gpt-old':dict(input=1,output=2)}))
                else:await request.abort()
            await page.route('**/*',route)
            await page.goto(ORIGIN+'/')
            await expect(page.locator('#refresh')).to_be_enabled()
            await expect(page.locator('#updated')).to_contain_text('마지막 갱신')
            await expect(page.locator('#updated')).to_contain_text('수집')
            assert not await page.evaluate("document.body.classList.contains('busy')")
            # Auto refresh moved to the settings tab; switch it off without leaving the overview.
            await page.evaluate("$('auto').checked=false;$('auto').dispatchEvent(new Event('change'))")
            # Filters, insights and quotas live on separate tabs since the 09-15 UX split.
            async def view(name):await page.locator(f'#tabs [data-view="{name}"]').first.click()
            await view('analysis')
            await page.locator('#period').select_option('today')
            await view('insights')
            await expect(page.locator('#comparison')).to_contain_text('+100.0%')
            await expect(page.locator('#quality')).to_contain_text('기록된 해석 실패')
            await page.locator('#cache-panel > summary').click()
            await expect(page.locator('#cache-overview')).to_contain_text('50.0%')
            await expect(page.locator('#cache-trend svg')).to_be_visible()
            await expect(page.locator('#cache-rows')).to_contain_text('gpt-test')
            # Calendar cells grow to the 30px cap at 1440px; stats sit to the
            # right of the grid and the 합계 matches the fixture rows (L2).
            await expect(page.locator('#calendar .cal-stats')).to_be_visible()
            cell=await page.locator('#calendar .cal-cell[data-day]').first.bounding_box()
            assert abs(cell['width']-30)<0.6,cell
            stats_box=await page.locator('#calendar .cal-stats').bounding_box()
            grid_box=await page.locator('#calendar .cal-grid').bounding_box()
            assert stats_box['x']>=grid_box['x']+grid_box['width']-1,(stats_box,grid_box)
            cal_sum=await page.evaluate("fmt(lastUsage.calendar.reduce((s,r)=>s+r.tokens,0))")
            await expect(page.locator('#calendar .cal-stats')).to_contain_text(cal_sum+' 토큰')
            offsets=await page.evaluate("""()=>{const cells=[...document.querySelectorAll('#calendar .cal-grid .cal-cell')];
              return [...document.querySelectorAll('#calendar .cal-months span')].map(m=>{
                const w=+m.className.match(/cal-m(\\d+)/)[1]-1,c=cells[w*7];
                return c?Math.abs(m.getBoundingClientRect().x-c.getBoundingClientRect().x):0;});}""")
            assert all(d<=1 for d in offsets),offsets
            await expect(page.locator('#sessions')).to_contain_text('fixture-')
            async with page.expect_download() as pending_download:
                await page.locator('#csv-sessions').click()
            download=await pending_download.value
            filename=download.suggested_filename
            assert filename.startswith('llm-usage-sessions-'),filename
            assert re.search(r'\d{4}-\d{2}-\d{2}_\d{4}-\d{2}-\d{2}\.csv$',filename),filename
            csv_text=Path(await download.path()).read_text(encoding='utf-8-sig')
            assert csv_text.splitlines()[0].startswith('"서비스"'),csv_text.splitlines()[0]
            assert 'fixture-s1' in csv_text and 'fixture-proj' in csv_text
            # Session table: client-side paging and sort direction toggle.
            await page.evaluate("""lastUsage.sessions=Array.from({length:25},(_,i)=>({session:'sess-'+String(i).padStart(3,'0'),route:'codex',project:'p',kind:'main',first_ts:1700000000+i,last_ts:1700000100+i,requests:i+1,uncached_input:i+1,cached_input:0,output:0,cache_creation:0,cost:null}));
                lastUsage.sessions_total=25;renderSessions(lastUsage)""")
            await expect(page.locator('#sess-page')).to_contain_text('1/2 · 25개')
            await expect(page.locator('#sessions tr')).to_have_count(20)
            await page.locator('#sess-next').click()
            await expect(page.locator('#sessions tr')).to_have_count(5)
            await expect(page.locator('#sess-page')).to_contain_text('2/2')
            await expect(page.locator('.sess-copy').first).to_have_attribute('data-id','sess-004')
            # Header sort buttons are plain text controls, not accent pills.
            bg=await page.evaluate("getComputedStyle(document.querySelector('[data-ssort]')).backgroundColor")
            assert bg in ('rgba(0, 0, 0, 0)','transparent'),bg
            await page.locator('[data-ssort="requests"]').click()
            await expect(page.locator('#sessions tr').first).to_contain_text('sess-024')
            assert await page.locator('[data-ssort="requests"]').locator('..').get_attribute('aria-sort')=='descending'
            await page.locator('[data-ssort="requests"]').click()
            await expect(page.locator('#sessions tr').first).to_contain_text('sess-000')
            assert await page.locator('[data-ssort="requests"]').locator('..').get_attribute('aria-sort')=='ascending'
            assert await page.locator('[data-ssort="first"]').locator('..').get_attribute('aria-sort')=='none'
            # Paging copy says the real total once the server caps the list at 100.
            await page.evaluate("""lastUsage.sessions=Array.from({length:100},(_,i)=>({session:'bulk-'+i,route:'codex',project:'p',kind:'main',first_ts:1700000000+i,last_ts:1700000100+i,requests:1,uncached_input:1,cached_input:0,output:0,cache_creation:0,cost:null}));
                lastUsage.sessions_total=150;renderSessions(lastUsage)""")
            await expect(page.locator('#sess-page')).to_contain_text('전체 150개 중 상위 100개')
            await view('overview')
            # Quota cards show a summary; trends, forecast and history sit in the card details.
            await page.locator('.bucket-detail').first.locator(':scope > summary').click()
            await page.locator('.quota-history').first.locator(':scope > summary').click()
            await expect(page.locator('.quota-history svg').first).to_be_visible()
            # The details toggle event is dispatched asynchronously; wait until
            # fitSvgText has actually stamped an inline font-size.
            await page.wait_for_function("!!document.querySelector('#quota-codex .quota-line text').style.fontSize")
            # Fixed-viewBox chart text is rescaled to ~11px screen size (L3):
            # computed font-size × the svg's render scale.
            sizes=await page.evaluate("""[...document.querySelectorAll('#quota-codex .quota-line text,#quota-codex .quota-bars text,#cache-trend svg text')]
              .map(t=>{const s=t.closest('svg');const w=s.getBoundingClientRect().width;
                return w?parseFloat(getComputedStyle(t).fontSize)*w/s.viewBox.baseVal.width:null;})
              .filter(v=>v!=null)""")
            assert sizes and all(9<=v<=15 for v in sizes),sizes
            await expect(page.locator('.bucket').first).to_contain_text('12.0%p/시간')
            # The burn-down forecast extends the domain toward the reset, and the
            # overview lists the next resets. Single-observation rows get no line.
            await expect(page.locator('.quota-history .quota-forecast').first).to_be_visible()
            assert await page.locator('.quota-forecast').count()>=1
            # The reset schedule is part of the overview, visible without opening anything.
            await expect(page.locator('#reset-timeline')).to_be_visible()
            await expect(page.locator('#reset-timeline')).to_contain_text('Codex')
            await expect(page.locator('#reset-timeline')).to_contain_text('Antigravity')
            timeline=await page.locator('#reset-timeline').inner_text()
            assert 'Codex Codex' not in timeline and 'Codex 주간' in timeline,timeline
            # Windows of one service resetting at the same time share one line.
            agy=page.locator('.reset-item').filter(has_text='Antigravity')
            await expect(agy).to_have_count(1)
            assert (await agy.inner_text()).count('·')>=3,await agy.inner_text()
            # A bucket without a reset time prints the fallback once, not twice.
            devin=await page.locator('#quota-devin').inner_text()
            assert devin.count('초기화 정보 미제공')==1,devin
            # Trend figures stay on one line via .nowrap spans.
            assert await page.locator('#quota-codex .quota-trends .nowrap').count()>=4
            antigrav=await page.locator('#quota-antigravity .quota-forecast').count()
            assert antigrav==0,'rows without pace must not draw a forecast line'
            # Hold a response while changing period, grouping and cumulative mode.
            block_next[0]=True
            await page.evaluate('void refresh()')
            await asyncio.wait_for(started.wait(),5)
            await asyncio.sleep(0.5)
            assert await page.evaluate("document.body.classList.contains('busy')")
            assert await page.evaluate("document.querySelector('#refresh').getAttribute('aria-busy')")=='true'
            await view('analysis')
            await page.locator('#period').select_option('30d')
            await page.locator('#group').select_option('model')
            await page.locator('#cumulative').select_option('1')
            release.set()
            await expect(page.locator('#refresh')).to_be_enabled()
            assert seen[-1]['period']=='30d' and seen[-1]['group']=='model' and seen[-1]['cumulative']=='1'
            assert not await page.evaluate("document.body.classList.contains('busy')")
            assert await page.evaluate("lastUsage.start.startsWith('2026-08-11')")
            assert await page.evaluate("lastUsage.labels.includes('gpt-test')")
            # Insights data loads lazily once its tab opens.
            await view('insights')
            await expect(page.locator('#comparison')).to_contain_text('최초 기록')
            assert await page.locator('.quota-history').first.get_attribute('open') is not None
            await view('insights')
            await page.locator('#cache-series').select_option('gpt-test')
            await expect(page.locator('#cache-trend svg')).to_have_attribute('aria-label','gpt-test 캐시 활용률 추이')
            output=args.artifacts or Path(directory)
            output.mkdir(parents=True,exist_ok=True)
            await page.locator('.insight-grid').screenshot(path=str(output/'insights-desktop.png'))
            await page.locator('#theme').select_option('midnight')
            await page.set_viewport_size({'width':390,'height':844})
            await view('overview')
            await expect(page.locator('#quota-overview')).to_be_visible()
            await expect(page.locator('#quota-overview button')).to_have_count(4)
            await expect(page.locator('.limit-card:visible')).to_have_count(0)
            overview=await page.locator('#quota-overview').bounding_box()
            assert overview['height']<350
            order=await page.locator('main h3, #cache-panel > summary, #sources-panel h2').all_text_contents()
            assert order==['다음 초기화','수동 자원 기록','오늘·이번 주 작업 전망','필터','사용 추이','토큰 구성','모델별 사용량 순위','시간대별 사용 패턴','모델별 사용량 상세',
                           '캐시 활용률 분석','구독 가치 분석','일별 활동','프로젝트별 사용량','세션별 사용량','주간 리포트',
                           '이전 기간과 비교','데이터 신뢰도','수집 범위와 상태','이 기기의 오프라인 통계','월 구독료','모델 단가','임계값','알림 · 자동 갱신','프로젝트 예산','외부 알림'],order
            await view('analysis')
            # Filters fold behind a one-line summary on mobile; opening reveals them.
            fold=page.locator('#filter-fold')
            assert not await fold.evaluate('el=>el.open')
            await expect(page.locator('#filter-summary')).to_contain_text('30일')
            await expect(page.locator('#filter-summary')).to_contain_text('모델')
            await fold.locator('summary').click()
            for size in (320,390):
                await page.set_viewport_size({'width':size,'height':844})
                # Six filters wrap into two rows; period, granularity and group stay on the first.
                boxes=[await page.locator('#'+name).bounding_box() for name in ('period','granularity','group','metric','compare','cumulative')]
                assert max(b['y'] for b in boxes[:3])-min(b['y'] for b in boxes[:3])<2
                assert len({round(b['y']) for b in boxes})<=2
                assert await page.evaluate('document.documentElement.scrollWidth<=innerWidth')
            for grain in ('week','month'):
                await page.locator('#granularity').select_option(grain)
                await expect(page.locator('#refresh')).to_be_enabled()
                assert await page.evaluate('lastUsage.granularity')==grain
                await page.locator('#chart > svg').focus()
                await page.keyboard.press('Home')
                await expect(page.locator('#chart-tooltip')).to_contain_text('일부 기간')
                await page.keyboard.press('Escape')
            await page.locator('#granularity').select_option('day')
            await expect(page.locator('#refresh')).to_be_enabled()
            # At 390px the x-axis thins its date labels so none overlap.
            boxes=await page.evaluate('''() => {const s=document.querySelector('#chart > svg');
              const h=s.viewBox.baseVal.height;
              return [...s.querySelectorAll('text')].filter(e=>+e.getAttribute('y')===h-8)
                .map(e=>{const b=e.getBBox();return{x:b.x,w:b.width};}).sort((a,b)=>a.x-b.x);}''')
            assert len(boxes)>=2 and all(a['x']+a['w']<=b['x']+0.5 for a,b in zip(boxes,boxes[1:])),boxes
            # A mouse click still drills straight into the clicked bucket.
            pt=await page.evaluate('''() => {const s=document.querySelector('#chart > svg');
              const l=+s.dataset.l,r=+s.dataset.r;
              const p=s.createSVGPoint();p.x=l+1.5/30*(r-l);p.y=s.viewBox.baseVal.height*0.5;
              const t=p.matrixTransform(s.getScreenCTM());return{x:t.x,y:t.y};}''')
            await page.mouse.click(pt['x'],pt['y'])
            await expect(page.locator('#refresh')).to_be_enabled()
            assert 'period=custom' in page.url,page.url
            await page.go_back()
            await expect(page.locator('#refresh')).to_be_enabled()
            assert 'period=custom' not in page.url
            await page.locator('#compare').select_option('week')
            await expect(page.locator('#refresh')).to_be_enabled()
            # The scope select is grouped by kind and persisted in the URL.
            await expect(page.locator('#scope optgroup[label="서비스"]')).to_have_count(1)
            await expect(page.locator('#scope-chip')).to_be_hidden()
            await page.locator('#scope').select_option('route:codex')
            await expect(page.locator('#refresh')).to_be_enabled()
            assert seen[-1].get('scope')=='route:codex'
            assert 'scope=route%3Acodex' in page.url or 'scope=route:codex' in page.url
            await expect(page.locator('#scope-chip')).to_be_visible()
            await expect(page.locator('#scope-chip')).to_contain_text('Codex')
            await expect(page.locator('#filter-summary')).to_contain_text('서비스: OpenAI / Codex')
            assert await page.evaluate('lastUsage.scope')=='route:codex'
            assert await page.evaluate("lastUsage.totals.requests<4 || lastUsage.totals.uncached_input<=lastUsage.lifetime.uncached_input")
            await page.locator('#scope-chip').click()
            await expect(page.locator('#refresh')).to_be_enabled()
            assert seen[-1].get('scope') in (None,'')
            assert await page.evaluate('lastUsage.scope')==None
            # Drill-down: a calendar day narrows to a custom hourly view, a rank
            # row applies a model scope, and Back restores the pushed filters.
            await view('insights')
            await page.evaluate("document.querySelectorAll('details.mobile-fold').forEach(d=>d.open=true)")
            await page.locator('#calendar .cal-cell[data-day]:not(.a0)').first.click()
            await expect(page.locator('#refresh')).to_be_enabled()
            assert await page.evaluate("$('period').value")=='custom'
            assert await page.evaluate("$('granularity').value")=='hour'
            day0=await page.evaluate("$('start').value")
            assert await page.evaluate("$('end').value")==day0
            assert await page.evaluate("currentView")=='analysis'
            await page.locator('.rank[data-scope]').first.click()
            await expect(page.locator('#refresh')).to_be_enabled()
            assert (await page.evaluate("$('scope').value") or '').startswith('model:')
            await expect(page.locator('#scope-chip')).to_be_visible()
            # With a scope and a custom period the summary must keep both parts.
            summary=await page.locator('#filter-summary').inner_text()
            assert '모델:' in summary and '~' in summary,summary
            await page.go_back()
            await expect(page.locator('#scope-chip')).to_be_hidden()
            assert await page.evaluate("$('period').value")=='custom'
            assert await page.evaluate("$('granularity').value")=='hour'
            await page.go_back()
            assert await page.evaluate("$('period').value")=='30d'
            assert await page.evaluate("$('granularity').value")=='day'
            await view('analysis')
            # Keyboard drill-down: Enter on a focused table row applies its scope.
            await page.locator('#rows tr[data-scope]').first.focus()
            await page.keyboard.press('Enter')
            await expect(page.locator('#refresh')).to_be_enabled()
            assert 'scope=model' in page.url
            await expect(page.locator('#scope-chip')).to_be_visible()
            await page.locator('#scope-chip').click()
            await expect(page.locator('#refresh')).to_be_enabled()
            # Calendar days roam with arrow keys and drill with Enter.
            await view('insights')
            # Lazy insights replace the calendar DOM; focus only after that render.
            await page.wait_for_function('()=>!pending && !insightsPending')
            await page.evaluate("document.querySelectorAll('details.mobile-fold').forEach(d=>d.open=true)")
            cells=page.locator('#calendar .cal-cell[data-day]')
            await cells.last.focus()
            today_day=await cells.last.get_attribute('data-day')
            assert await page.evaluate("document.activeElement.tabIndex")==0
            await page.keyboard.press('ArrowLeft')
            moved=await page.evaluate("document.activeElement.dataset.day")
            assert moved and moved<today_day,(moved,today_day)
            await expect(page.locator('#tip')).to_be_visible()
            await page.keyboard.press('Enter')
            await expect(page.locator('#refresh')).to_be_enabled()
            assert 'period=custom' in page.url
            assert ('start='+moved) in page.url
            await page.go_back()
            await expect(page.locator('#refresh')).to_be_enabled()
            # No interactive elements remain nested inside <summary>.
            assert await page.evaluate("document.querySelectorAll('summary button').length")==0
            await view('analysis')
            # The 전체 토큰 card delta always compares token totals, whatever the metric.
            delta_tokens=await page.evaluate("document.querySelectorAll('#cards .stat')[3].querySelector('small').textContent")
            assert '7일 전 동일 구간 같은 경과 시간 대비' in delta_tokens,delta_tokens
            # Heatmap and calendar follow the selected metric.
            await page.locator('#metric').select_option('requests')
            await expect(page.locator('#refresh')).to_be_enabled()
            await expect(page.locator('#heatmap .heat-legend')).to_contain_text('요청 수')
            assert await page.evaluate("document.querySelectorAll('#cards .stat')[3].querySelector('small').textContent")==delta_tokens
            await view('insights')
            await expect(page.locator('#calendar .cal-grid')).to_have_attribute('aria-label',re.compile('요청 수'))
            await expect(page.locator('#calendar-caption')).to_contain_text('일별 요청 수')
            req_sum=await page.evaluate("fmt(lastUsage.calendar.reduce((s,r)=>s+r.requests,0))+'회'")
            await expect(page.locator('#calendar .cal-stats')).to_contain_text(req_sum)
            await view('analysis')
            await page.locator('#metric').select_option('cost')
            await expect(page.locator('#refresh')).to_be_enabled()
            await view('insights')
            # The unrated-model day/hour renders as a hatched 미산정 cell, not $0.
            # (insights reloads lazily once its tab opens, so poll for the cell)
            na=page.locator('#calendar .cal-cell.na')
            await expect(na.first).to_be_attached()
            assert '미산정' in await na.first.get_attribute('data-tip')
            await expect(page.locator('#heatmap .heat-cell.na').first).to_be_attached()
            await expect(page.locator('#calendar-caption')).to_contain_text('비용 추정')
            # Cost metric marks unrated days and keeps them out of 활동일.
            await expect(page.locator('#calendar .cal-stats')).to_contain_text(re.compile('미산정\\s*1일'))
            # At 390px cells stay in the 11-20px band and stats move below.
            cell=await page.locator('#calendar .cal-cell[data-day]').first.bounding_box()
            assert 11<=cell['width']<=20,cell
            stats_box=await page.locator('#calendar .cal-stats').bounding_box()
            grid_box=await page.locator('#calendar .cal-grid').bounding_box()
            assert stats_box['y']>=grid_box['y']+grid_box['height']-1,(stats_box,grid_box)
            await view('analysis')
            await page.locator('#metric').select_option('tokens')
            await expect(page.locator('#refresh')).to_be_enabled()
            await view('overview')

            await view('overview')
            # Overview leads with quotas; the summary cards carry the active period.
            assert await page.evaluate("document.querySelector('#quota-overview').compareDocumentPosition(document.querySelector('#cards')) & Node.DOCUMENT_POSITION_FOLLOWING")
            await expect(page.locator('#cards-period')).to_contain_text('최근 30일')
            await expect(page.locator('#cards')).to_contain_text('7일 전 동일 구간 같은 경과 시간 대비')
            await expect(page.locator('[data-quota="codex"]')).to_contain_text('주간 70.0%')
            assert '5h' not in await page.locator('[data-quota="codex"]').inner_text()
            await page.screenshot(path=str(output/'mobile-overview.png'),full_page=True)
            await page.locator('[data-quota="codex"]').click()
            await expect(page.locator('.limit-card:visible')).to_have_count(1)
            await expect(page.locator('#quota-codex .quota-trends').first).to_contain_text('12.0')
            await expect(page.locator('#quota-codex .quota-secondary > summary')).to_contain_text('5시간 88.0%')
            await expect(page.locator('#quota-codex .quota-secondary .bucket')).to_be_hidden()
            await expect(page.locator('#quota-codex > .bucket .quota-line')).to_be_visible()
            assert (await page.locator('#quota-codex > .bucket .quota-line path:not(.quota-forecast)').get_attribute('d')).count('L')>=6
            assert await page.locator('#quota-codex > .bucket .quota-line circle').count()<=3
            await expect(page.locator('#quota-codex > .bucket .quota-bars')).to_be_visible()
            # At 390px the same SVG axis text still lands at ~11px on screen.
            sizes=await page.evaluate("""[...document.querySelectorAll('#quota-codex .quota-line text,#quota-codex .quota-bars text')]
              .map(t=>{const s=t.closest('svg');const w=s.getBoundingClientRect().width;
                return w?parseFloat(getComputedStyle(t).fontSize)*w/s.viewBox.baseVal.width:null;})
              .filter(v=>v!=null)""")
            assert sizes and all(9<=v<=15 for v in sizes),sizes
            await page.locator('[data-quota="claude-code"]').click()
            await expect(page.locator('.limit-card:visible')).to_have_count(1)
            await expect(page.locator('#quota-claude-code')).to_be_visible()
            await page.evaluate('data=>renderLimits(data)',limits)
            await expect(page.locator('[data-quota="claude-code"]')).to_have_attribute('aria-expanded','true')
            ag=await page.locator('[data-quota="antigravity"]').inner_text()
            assert all(value in ag for value in ('90.0%','40.0%','80.0%','30.0%'))
            await page.set_viewport_size({'width':1440,'height':1050})
            await expect(page.locator('.limit-card:visible')).to_have_count(4)
            # The ended OpenCode Go card folds into the dormant section.
            await expect(page.locator('#limits > .quota-dormant > summary')).to_contain_text('관측 중단 1개')
            await expect(page.locator('#limits > .quota-dormant')).to_contain_text('OpenCode Go')
            # Quota cards auto-fit a 300px-minimum grid: at 1440px all active
            # cards share one row and the dormant fold spans the full width.
            boxes=await page.locator('#limits > .limit-card').evaluate_all('els=>els.map(e=>e.getBoundingClientRect())')
            assert len(boxes)>=3
            assert len({round(b['y']) for b in boxes})==1,boxes
            assert len({round(b['width']) for b in boxes})==1,boxes
            dormant=await page.locator('#limits > .quota-dormant').bounding_box()
            grid=await page.locator('#limits').bounding_box()
            assert dormant['y']>boxes[0]['y'] and abs(dormant['width']-grid['width'])<=1,(dormant,grid)
            # A stale route past quota_hide_days joins the dormant fold too.
            await page.evaluate('''data=>{const old={...data,limits:data.limits.map(r=>r.route==='antigravity'?{...r,checked:data.now-8*86400,status:'stale'}:r)};renderLimits(old);}''',limits)
            await expect(page.locator('#limits > .quota-dormant > summary')).to_contain_text('관측 중단 2개')
            await expect(page.locator('.limit-card:visible')).to_have_count(3)
            await page.evaluate('data=>renderLimits(data)',limits)
            # Desktop shows every alert chip; the collapse toggle is mobile-only (M1).
            await page.evaluate('data=>renderAlerts({...data,low_percent:95},lastUsage)',limits)
            await expect(page.locator('#alerts-toggle')).to_be_hidden()
            assert await page.locator('.alert-chip:visible').count()>=3
            await page.evaluate('data=>renderAlerts(data,lastUsage)',limits)
            # The pinned nav keeps the tabs on screen and shows the shared
            # filter summary on data tabs only (M2).
            # The overview's period applies only to its summary cards, so the pinned
            # filter summary is shown on the insights tab, not the overview.
            await expect(page.locator('#context-bar')).to_be_hidden()
            summary_text=await page.locator('#filter-summary').inner_text()
            await view('insights')
            await expect(page.locator('#context-bar')).to_be_visible()
            assert await page.locator('#context-summary').inner_text()==summary_text
            await page.evaluate('scrollTo(0,2000)')
            assert await page.evaluate('scrollY')>0
            assert await page.evaluate("document.querySelector('#tabs').getBoundingClientRect().top")<=1
            await page.evaluate('scrollTo(0,0)')
            await view('settings')
            await expect(page.locator('#context-bar')).to_be_hidden()
            await view('overview')
            await page.locator('#cards-edit').click()
            assert await page.evaluate('currentView')=='analysis'
            assert 'view=analysis' in page.url,page.url
            assert await page.evaluate("document.querySelector('#filter-fold').open")
            assert await page.evaluate('document.activeElement.id')=='period'
            await expect(page.locator('#context-bar')).to_be_hidden()
            await view('overview')
            await view('insights')
            # Month-to-date value projection and the budget alert chip.
            await expect(page.locator('#subvalue')).to_contain_text('이번 달 환산')
            await expect(page.locator('#subvalue')).to_contain_text('월말 예상')
            await page.evaluate("""()=>{const d=structuredClone(lastUsage);d.insights.month.alert_usd=0.0001;renderInsights(d);}""")
            await expect(page.locator('#subvalue')).to_contain_text('알림 기준')
            await page.evaluate('()=>renderInsights(lastUsage)')
            await expect(page.locator('#quality')).to_be_visible()
            await page.set_viewport_size({'width':320,'height':720})
            assert await page.evaluate('document.documentElement.scrollWidth<=innerWidth')
            await view('overview')
            await page.set_viewport_size({'width':390,'height':844})
            await page.locator('[data-quota="codex"]').click()
            assert await page.evaluate('document.documentElement.scrollWidth<=innerWidth'),'mobile overflow'
            await page.screenshot(path=str(output/'insights-mobile.png'),full_page=True)
            # The alert chip follows the configured low-remaining threshold, like the card badge.
            await page.evaluate('data=>renderAlerts({...data,low_percent:75},lastUsage)',limits)
            await expect(page.locator('#alerts')).to_contain_text('잔여 70.0%')
            await page.evaluate('data=>renderAlerts({...data,low_percent:15},lastUsage)',limits)
            await expect(page.locator('#alerts')).not_to_contain_text('잔여 70.0%')
            # At 390px, 2+ alerts collapse into one toggle; expanding shows all (M1).
            await page.evaluate('data=>renderAlerts({...data,low_percent:95},lastUsage)',limits)
            await expect(page.locator('#alerts-toggle')).to_be_visible()
            await expect(page.locator('#alerts-toggle')).to_contain_text('알림')
            assert await page.locator('.alert-chip:visible').count()==0
            # Phone tabs are fixed at the bottom, so the overview content is what moves down.
            collapsed_top=await page.evaluate("document.querySelector('#view-overview').getBoundingClientRect().top")
            await page.locator('#alerts-toggle').click()
            await expect(page.locator('#alerts-toggle')).to_have_attribute('aria-expanded','true')
            assert await page.locator('.alert-chip:visible').count()>=3
            expanded_top=await page.evaluate("document.querySelector('#view-overview').getBoundingClientRect().top")
            assert expanded_top>collapsed_top,(collapsed_top,expanded_top)
            await page.evaluate('data=>renderAlerts(data,lastUsage)',limits)
            # Insight tables drop secondary columns at 390px; a 상세 열 toggle in
            # each panel brings them back, and wraps never scroll sideways (M3).
            await view('insights')
            await page.evaluate("document.querySelectorAll('details.mobile-fold').forEach(d=>d.open=true)")
            proj_head=await page.locator('[data-panel="projects"] th:visible').all_text_contents()
            assert proj_head==['프로젝트','요청','토큰','비용 추정','상세'],proj_head  # 상세 opens the project detail
            sess_head=[t.rstrip('↕▼▲ ') for t in await page.locator('[data-panel="sessions"] th:visible').all_text_contents()]
            assert sess_head==['서비스','세션','토큰','비용 추정'],sess_head
            for sel in ('[data-panel="projects"] .table-wrap','[data-panel="sessions"] .table-wrap'):
                over=await page.evaluate("sel=>{const e=document.querySelector(sel);return e.scrollWidth-e.clientWidth;}",sel)
                assert over<=1,(sel,over)
            await page.locator('#proj-cols').click()
            await page.locator('#sess-cols').click()
            assert await page.locator('[data-panel="projects"] th:visible').count()==7
            assert await page.locator('[data-panel="sessions"] th:visible').count()==8
            await page.locator('#proj-cols').click()
            await page.locator('#sess-cols').click()
            # 구독 가치 rows stack name / description / value in one column.
            row_boxes=await page.evaluate("""()=>[...document.querySelector('[data-panel="subvalue"] .sub-row').children]
              .map(c=>{const r=c.getBoundingClientRect();return{x:r.x,y:r.y};})""")
            assert len({round(b['x']) for b in row_boxes})==1 and all(b['y']>row_boxes[0]['y'] for b in row_boxes[1:]),row_boxes
            # The type scale floors every visible HTML text at 11px (T1);
            # SVG text is sized in user units by fitSvgText, so it is skipped.
            tiny=await page.evaluate("""()=>[...document.querySelectorAll('body *')]
              .filter(el=>{if(el.closest('svg')||!el.getClientRects().length)return false;
                let t='';for(const n of el.childNodes)if(n.nodeType===3)t+=n.textContent;
                return t.trim()&&parseFloat(getComputedStyle(el).fontSize)<11;})
              .map(el=>el.tagName+'.'+el.className+':'+el.textContent.trim().slice(0,25))""")
            assert tiny==[],tiny
            # Interactive targets are >=32px tall at 390px (T2). A control inside
            # a sized label or th inherits its ancestor's target size; calendar
            # cells keep their fixed grid size by design (Q2).
            small_targets=await page.evaluate("""()=>[...document.querySelectorAll('button,a,select,input,summary,[role=tab],[tabindex="0"]')]
              .filter(el=>{
                if(el.classList.contains('cal-cell'))return false;
                if(!el.getClientRects().length||getComputedStyle(el).display==='none')return false;
                if(el.getBoundingClientRect().height>=32)return false;
                const label=el.closest('label');if(label&&label.getBoundingClientRect().height>=32)return false;
                const th=el.closest('th');if(th&&th.getBoundingClientRect().height>=32)return false;
                if(el.type==='checkbox'||el.type==='radio')return false;
                return true;})
              .map(el=>el.tagName+'#'+(el.id||'')+'.'+el.className+':'+(el.textContent||'').trim().slice(0,25))""")
            assert small_targets==[],small_targets
            # Tabs expose tablist semantics and arrow-key navigation.
            assert await page.locator('#tabs').get_attribute('role')=='tablist'
            assert await page.locator('#tab-insights').get_attribute('aria-controls')=='view-insights'
            assert await page.locator('section[data-view="insights"]').get_attribute('role')=='tabpanel'
            await page.locator('#tab-overview').focus()
            await page.keyboard.press('ArrowRight')
            await expect(page.locator('#tab-analysis')).to_have_attribute('aria-selected','true')
            await expect(page.locator('#tab-analysis')).to_have_attribute('tabindex','0')
            await expect(page.locator('#tab-overview')).to_have_attribute('tabindex','-1')
            assert 'view=analysis' in page.url
            # This page visited settings earlier. Its old controls can satisfy
            # visibility/count assertions before the next loadConfig replaces
            # them, detaching a handle during bounding_box(). Wait for that
            # replacement before checking the new settings layout.
            previous_save=await page.locator('#cfg-subs-save').element_handle()
            assert previous_save is not None
            await page.keyboard.press('End')
            await expect(page.locator('#tab-settings')).to_have_attribute('aria-selected','true')
            await page.wait_for_function('previous=>!previous.isConnected',arg=previous_save)
            await previous_save.dispose()
            # Settings inputs follow the theme and the pricing table filters
            # client-side; the filters survive loadConfig re-renders (L4).
            await expect(page.locator('#cfg-subs input').first).to_be_visible()
            await expect(page.locator('#cfg-pricing tbody tr')).to_have_count(3)
            surface=await page.evaluate("""()=>{const probe=document.createElement('div');
                probe.style.background='var(--surface)';document.body.appendChild(probe);
                const value=getComputedStyle(probe).backgroundColor;probe.remove();return value;}""")
            input_bg=await page.evaluate("getComputedStyle(document.querySelector('#cfg-subs input')).backgroundColor")
            assert input_bg==surface,(input_bg,surface)
            save=await page.locator('#cfg-subs-save').bounding_box()
            panel=await page.locator('#view-settings .panel').first.bounding_box()
            assert save['width']<panel['width']*0.5,(save,panel)
            await page.locator('#price-search').fill('gpt-noprice')
            await expect(page.locator('#cfg-pricing tbody tr:not([hidden])')).to_have_count(1)
            await expect(page.locator('#price-count')).to_contain_text('1/3개')
            await page.locator('#price-search').fill('')
            await page.locator('#price-unset').click()
            await expect(page.locator('#price-unset')).to_have_attribute('aria-pressed','true')
            await expect(page.locator('#cfg-pricing tbody tr:not([hidden])')).to_have_count(1)
            await expect(page.locator('#cfg-pricing tbody tr:not([hidden]) td').first).to_contain_text('미설정')
            await page.locator('#price-unset').click()
            await expect(page.locator('#cfg-pricing tbody tr:not([hidden])')).to_have_count(3)
            await page.locator('#tabs [data-view="analysis"]').click()
            # The grid is a labelled group of labelled cells reachable by keyboard.
            assert await page.locator('#heatmap .heat-grid').get_attribute('role')=='group'
            assert await page.locator('#heatmap .heat-cell[tabindex="0"]').count()==1
            assert (await page.locator('#heatmap .heat-cell').first.get_attribute('aria-label')).startswith('월요일 0시')
            await view('insights')
            assert await page.locator('#calendar .cal-grid').get_attribute('role')=='group'
            # An expired CSRF token re-bootstraps once and the POST succeeds.
            assert await page.evaluate("api('/api/config/thresholds',{stale_seconds:900}).then(r=>r.thresholds!==undefined).catch(e=>'fail:'+e.message)")is True
            # A non-JSON proxy error surfaces a readable status, not a parser message.
            html_error[0]=True
            await page.evaluate('void refresh()')
            await expect(page.locator('#error')).to_contain_text('서버 응답 오류 (HTTP 502)')
            html_error[0]=False
            await page.evaluate('refresh()')
            await expect(page.locator('#error')).to_be_hidden()
            # Blocked storage with notifications granted must not stall the page.
            restricted=await browser.new_page(service_workers='block',viewport={'width':390,'height':844})
            restricted_errors=[];restricted.on('pageerror',lambda error:restricted_errors.append(str(error)));watch_csp(restricted,restricted_errors)
            await restricted.add_init_script('''() => {
              Storage.prototype.getItem=function(){throw Error('blocked')};
              Storage.prototype.setItem=function(){throw Error('blocked')};
              Object.defineProperty(Notification,'permission',{get:()=>'granted'});
            }''')
            await restricted.route('**/*',route)
            await restricted.goto(ORIGIN+'/')
            await expect(restricted.locator('#updated')).to_contain_text('마지막 갱신')
            assert not restricted_errors,restricted_errors
            # Settings' first panel (월 구독료) opens by default on mobile; the
            # other folds stay closed until touched (M4).
            await restricted.locator('#tabs [data-view="settings"]').click()
            await expect(restricted.locator('#cfg-subs input').first).to_be_visible()
            await expect(restricted.locator('#cfg-pricing')).to_be_hidden()
            await restricted.close()
            # Touch input: the first tap only opens the tooltip with a hint; a
            # second tap on the same bucket drills in (Q1).
            touch=await browser.new_page(service_workers='block',viewport={'width':390,'height':844},has_touch=True,is_mobile=True)
            touch_errors=[];touch.on('pageerror',lambda error:touch_errors.append(str(error)));watch_csp(touch,touch_errors)
            await touch.route('**/*',route)
            await touch.goto(ORIGIN+'/')
            await expect(touch.locator('#refresh')).to_be_enabled()
            await touch.locator('#tabs [data-view="analysis"]').click()
            svg=touch.locator('#chart > svg')
            await svg.scroll_into_view_if_needed()
            point=await svg.evaluate('''svg=>{
              const l=+svg.dataset.l,r=+svg.dataset.r;
              const p=svg.createSVGPoint();p.x=l+2.5/7*(r-l);p.y=svg.viewBox.baseVal.height*0.5;
              const s=p.matrixTransform(svg.getScreenCTM());return{x:s.x,y:s.y};}''')
            await touch.touchscreen.tap(point['x'],point['y'])
            await expect(touch.locator('#chart-tooltip')).to_be_visible()
            await expect(touch.locator('#chart-tooltip')).to_contain_text('한 번 더 탭')
            assert 'period=custom' not in touch.url,touch.url
            await touch.touchscreen.tap(point['x'],point['y'])
            await expect(touch.locator('#refresh')).to_be_enabled()
            assert 'period=custom' in touch.url,touch.url
            # A folded mobile panel opens normally and its CSV button works.
            await touch.locator('#tabs [data-view="insights"]').click()
            await touch.wait_for_function("()=>'insights' in (lastUsage||{})")
            # Only data-mobile-open panels start expanded on mobile (M4).
            assert await touch.evaluate("document.querySelector('[data-panel=subvalue]').open")
            assert not await touch.evaluate("document.querySelector('[data-panel=projects]').open")
            panel=touch.locator('[data-panel="sessions"]')
            assert not await panel.evaluate('el=>el.open')
            await panel.locator('summary').first.click()
            async with touch.expect_download() as pending:
                await touch.locator('#csv-sessions').click()
            assert (await pending.value).suggested_filename.startswith('llm-usage-sessions-')
            # The sources tab is a plain section now — its list is visible
            # without unfolding anything (M4).
            await touch.locator('#tabs [data-view="sources"]').click()
            await expect(touch.locator('#sources .source').first).to_be_visible()
            assert not touch_errors,touch_errors
            await touch.close()
            # Empty data: no fabricated percentages or account coverage.
            empty=Store(Path(directory)/'empty.db').usage(period='today',now=NOW)
            await page.evaluate('data=>renderUsage(data)',empty)
            await view('insights')
            await expect(page.locator('#cache-overview')).to_contain_text('—')
            await expect(page.locator('#cache-trend')).to_contain_text('표시할 관측값이 없습니다.')
            await expect(page.locator('#quality')).to_contain_text('모델 미확인 토큰 비중')
            assert not errors,errors
            await browser.close()
    print('Insight browser passed: API-shaped numeric fixtures, aligned comparison, cache, quota history, delayed filters, theme, mobile, empty values.')


if __name__=='__main__':asyncio.run(main())
