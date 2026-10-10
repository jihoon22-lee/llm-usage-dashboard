"""Portable Chromium regression checks using disposable synthetic data and local assets."""
from browser_support import CSP_HEADERS, watch_csp, fixture_page, fixture_json, artifact_directory
import argparse
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import sync_playwright, expect

parser=argparse.ArgumentParser()
parser.add_argument('--preview-assets',type=Path)
args=parser.parse_args()
origin='https://dashboard.test'
labels=[f'model-{i}' for i in range(11)]+['gpt-6.1-sol']
rows=[dict(provider='OpenAI',route='codex',model=model,uncached_input=(12-i)*100,
           cached_input=0,output=0,cache_creation=0,reasoning=0,requests=12-i,
           est_cost=(12-i)*0.01) for i,model in enumerate(labels)]
totals={k:sum(r[k] for r in rows) for k in ('uncached_input','cached_input','output','cache_creation','reasoning','requests')}
values={r['model']:{**r,'cost':r['est_cost']} for r in rows}
data=dict(start='2026-10-03T00:00:00+09:00',end_exclusive='2026-10-04T00:00:00+09:00',
          labels=labels,rows=rows,totals=totals,lifetime=totals,series=[dict(time='2026-10-03T00:00:00+09:00',values=values,
          range_start='2026-10-03T00:00:00+09:00',range_end_exclusive='2026-10-04T00:00:00+09:00')],
          sources=[],coverage='Visibility fixture',token_note='Reasoning is included in Output.',
          scope_options={'model':labels},cost=dict(period=0.78,lifetime=0.78,coverage=100,lifetime_coverage=100))

with sync_playwright() as p:
    browser=p.chromium.launch()
    page=browser.new_page(service_workers='block',viewport={'width':1440,'height':1000})
    fixture_page(page)
    errors=[];page.on('pageerror',lambda e:errors.append(str(e)));watch_csp(page,errors)
    if args.preview_assets:
        web=args.preview_assets.resolve()
        def serve(route):
            path=urlsplit(route.request.url).path
            if path=='/':route.fulfill(path=web/'index.html',content_type='text/html',headers=CSP_HEADERS)
            elif path.startswith('/assets/'):
                asset=web/Path(path).name
                route.fulfill(path=asset,content_type='text/css' if asset.suffix=='.css' else 'text/javascript')
            else:route.fallback()
        page.route(origin+'/**',serve)
    def usage(route):
        q=parse_qs(urlsplit(route.request.url).query)
        route.fulfill(json={**data,'scope':q.get('scope',[''])[0]})
    page.route(origin+'/api/usage?*',usage)
    assert page.goto(origin+'/?view=usage&group=model').status==200
    expect(page.locator('#updated')).to_contain_text('마지막 갱신')
    expect(page.locator('#legend button')).to_have_count(12)
    expect(page.locator('#legend button[data-label="gpt-6.1-sol"]')).to_be_visible()
    expect(page.locator('#legend button[data-label="기타"]')).to_have_count(0)
    expect(page.locator('#ranking .rank')).to_have_count(12)
    assert len(set(page.locator('#legend button').evaluate_all('(es)=>es.map(e=>e.style.getPropertyValue("--color"))')))==12
    assert len(set(page.locator('#chart > svg > rect:not(.chart-current)').evaluate_all('(es)=>es.map(e=>e.getAttribute("fill"))')))==12
    for metric,want in [('tokens','100'),('requests','1'),('cost','$0.01')]:
        page.locator('#metric').select_option(metric)
        expect(page.locator('#refresh')).to_be_enabled()
        page.locator('#chart > svg').focus()
        tip=page.locator('#chart-tooltip')
        expect(tip.locator('.tooltip-row')).to_have_count(12)
        expect(tip.locator('.tooltip-row').filter(has_text='gpt-6.1-sol').locator('strong')).to_have_text(want)
        page.keyboard.press('Escape')
    # Explicit model view from the default service grouping; no saved-filter reset.
    page.locator('#group').select_option('route')
    expect(page.locator('#chart-by-model')).to_be_visible()
    page.locator('#chart-by-model').click()
    expect(page.locator('#group')).to_have_value('model')
    expect(page.locator('#legend button')).to_have_count(12)
    # Crossing the gap below the plot must keep the mouse-scrollable list open.
    expect(page.locator('#refresh')).to_be_enabled()
    # The period cards sit above the chart now; centre the plot so its tooltip list fits below.
    plot=page.locator('#chart > svg');plot.evaluate("el=>el.scrollIntoView({block:'start'})");page.evaluate('scrollBy(0,-200)')
    plot.hover(position={'x':100,'y':100})
    tip=page.locator('#chart-tooltip');expect(tip).to_be_visible()
    detail=tip.bounding_box()
    page.mouse.move(detail['x']+detail['width']/2,detail['y']+10,steps=12)
    expect(tip).to_be_visible()
    tip.scroll_into_view_if_needed()
    tip.hover(position={'x':20,'y':20})
    page.mouse.wheel(0,600)
    expect(tip.locator('.tooltip-row').filter(has_text='gpt-6.1-sol')).to_be_in_viewport()
    # Narrow screen: every model stays reachable; tooltip can scroll and Escape closes it.
    page.mouse.move(0,0)
    page.set_viewport_size({'width':390,'height':844})
    page.wait_for_function('''() => {const svg=document.querySelector('#chart > svg');
      return svg && Math.abs(svg.viewBox.baseVal.width-document.querySelector('#chart').clientWidth)<1;}''')
    page.locator('#chart > svg').focus()
    tip=page.locator('#chart-tooltip');expect(tip).to_be_visible()
    tip.focus()
    tip.evaluate('(e)=>e.scrollTop=e.scrollHeight')
    expect(tip.locator('.tooltip-row').filter(has_text='gpt-6.1-sol')).to_be_in_viewport()
    page.keyboard.press('Escape');expect(tip).to_be_hidden()
    panel=page.locator('#ranking').locator('..')
    if not panel.get_attribute('open'):panel.locator('summary').click()
    new_rank=page.locator('#ranking [data-scope="model:gpt-6.1-sol"]')
    new_rank.scroll_into_view_if_needed();expect(new_rank).to_be_in_viewport()
    new_rank.click();expect(page.locator('#scope')).to_have_value('model:gpt-6.1-sol')
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
    # A long scrollable detail list must not swallow the two-tap bucket drill.
    touch=browser.new_page(service_workers='block',viewport={'width':390,'height':844},has_touch=True,is_mobile=True)
    fixture_page(touch)
    touch.on('pageerror',lambda e:errors.append(str(e)));watch_csp(touch,errors)
    if args.preview_assets:touch.route(origin+'/**',serve)
    touch.route(origin+'/api/usage?*',usage)
    touch.goto(origin+'/?view=usage&group=model')
    expect(touch.locator('#legend button')).to_have_count(12)
    svg=touch.locator('#chart > svg');svg.scroll_into_view_if_needed()
    point=svg.evaluate('''svg=>{const p=svg.createSVGPoint();
      p.x=(Number(svg.dataset.l)+Number(svg.dataset.r))/2;p.y=svg.viewBox.baseVal.height/2;
      const q=p.matrixTransform(svg.getScreenCTM());return {x:q.x,y:q.y};}''')
    touch.touchscreen.tap(point['x'],point['y'])
    expect(touch.locator('#chart-tooltip')).to_contain_text('한 번 더 탭')
    touch.touchscreen.tap(point['x'],point['y'])
    expect(touch.locator('#granularity')).to_have_value('hour')
    assert not errors,errors
    browser.close()
print('Model visibility passed: 12 series, low-volume new model, distinct colors, metrics, scope, scrollable mobile tooltip and ranking.')
