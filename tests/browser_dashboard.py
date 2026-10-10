"""Portable Chromium regression checks using disposable synthetic data and local assets."""
from browser_support import CSP_HEADERS, watch_csp, fixture_page, fixture_json, artifact_directory
import argparse
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright, expect

parser = argparse.ArgumentParser()
parser.add_argument('--preview-assets', type=Path)
parser.add_argument('--artifacts',type=Path)
args = parser.parse_args()
origin = 'https://dashboard.test'
artifacts = artifact_directory(args.artifacts)
artifacts.mkdir(exist_ok=True)

# A large creation count shows in Total (cache creation included) and makes sorting observable.
fields = ('uncached_input', 'cached_input', 'output', 'cache_creation', 'reasoning')
labels = ['OpenAI', 'Anthropic', 'Google', 'OpenCode', 'Other']
def tokens(i, cached=200):
    return dict(uncached_input=100*i, cached_input=cached*i, output=40*i, cache_creation=999*i, reasoning=10*i)
empty = dict.fromkeys(fields, 0)
rows = [dict(provider=name, route='route-'+name, model='model-'+name, **tokens(2*i+1)) for i,name in enumerate(labels)]
totals = {key:sum(row[key] for row in rows) for key in fields}
data = dict(start='2026-09-07T00:00:00+09:00', end_exclusive='2026-09-10T00:00:00+09:00', labels=labels,
            rows=rows, totals=totals, lifetime={key:value*2 for key,value in totals.items()},
            series=[dict(time='2026-09-07T00:00:00+09:00',values={name:tokens(i) for i,name in enumerate(labels)}),
                    dict(time='2026-09-08T00:00:00+09:00',values={name:tokens(i+1) for i,name in enumerate(labels)}),
                    dict(time='2026-09-09T00:00:00+09:00',values={name:empty.copy() for name in labels})],
            unavailable_routes=[dict(route='antigravity',detail='binary token history')],
            sources=[], coverage='Numeric test fixture', token_note='Reasoning is included in Output.')

def preview(page):
    if not args.preview_assets:
        return
    web = args.preview_assets.resolve()
    def serve(route):
        path = urlsplit(route.request.url).path
        if path == '/':
            route.fulfill(path=web/'index.html', content_type='text/html',headers=CSP_HEADERS)
        elif path.startswith('/assets/'):
            name = Path(path).name
            content_type = 'text/css' if name.endswith('.css') else 'text/javascript'
            route.fulfill(path=web/name, content_type=content_type)
        else:
            route.fallback()
    # Match query strings too: view URLs like /?view=usage must serve preview HTML.
    page.route(origin+'/**', serve)

def loaded(page):
    expect(page.locator('#updated')).to_contain_text('마지막 갱신')
    expect(page.locator('#refresh')).to_be_enabled()

def hover_date(page,index):
    svg = page.locator('#chart > svg')
    svg.scroll_into_view_if_needed()
    point = svg.evaluate('''(svg,index) => {
      const l=+svg.dataset.l, r=+svg.dataset.r;
      const p=svg.createSVGPoint(); p.x=l+(index+.5)/3*(r-l);p.y=svg.viewBox.baseVal.height*0.5;
      const screen=p.matrixTransform(svg.getScreenCTM());return {x:screen.x,y:screen.y};
    }''', index)
    page.mouse.move(point['x'],point['y'])
    return point

with sync_playwright() as playwright:

    browser = playwright.chromium.launch()
    page = browser.new_page(service_workers='block',viewport={'width':1440,'height':1050})
    fixture_page(page)
    errors=[];page.on('pageerror',lambda error:errors.append(str(error)));watch_csp(page,errors)
    preview(page)
    page.route(origin+'/api/usage?*',lambda route:route.fulfill(json=data))
    assert page.goto(origin+'/').status == 200
    loaded(page)
    page.locator('#tabs [data-view="usage"]').click()
    expect(page.locator('#group')).to_have_value('route')
    # The scope picker is width-capped so it stays on the filter row (L5).
    scope_box=page.locator('#scope').bounding_box();metric_box=page.locator('#metric').bounding_box()
    assert abs(scope_box['y']-metric_box['y'])<2,(scope_box,metric_box)
    expect(page.locator('#usage-gaps')).to_contain_text('Antigravity · 토큰 상세 미수집')
    expect(page.locator('#legend .unavailable-series')).to_contain_text('Antigravity')
    expect(page.get_by_role('heading',name='모델별 사용량 상세',exact=True)).to_be_visible()
    expect(page.locator('#rows tr').filter(has_text='model-OpenAI').locator('td').nth(8)).to_have_text('1,339')  # 100+200+40 plus cache creation 999
    expect(page.locator('#rows tr').filter(has_text='model-OpenAI').locator('td').nth(10)).to_have_text('999')
    expect(page.locator('#total-row tr').first.locator('td').nth(6)).to_have_text('33,475')  # 8,500 plus cache creation 24,975
    expect(page.locator('#total-row tr').nth(1).locator('td').nth(6)).to_have_text('66,950')
    assert page.locator('.donut-key > div').evaluate_all('(rows)=>rows.map(r=>r.dataset.token)') == ['cache_creation','cached_input','uncached_input','output']
    original_input_color=page.locator('[data-token="uncached_input"] circle').get_attribute('fill')
    # Period changes can change the ordering; token colors remain tied to the token type.
    page.evaluate('''() => {const copy=structuredClone(lastUsage);copy.totals={uncached_input:900,cached_input:100,output:800,cache_creation:0,reasoning:10};composition(copy);}''')
    assert page.locator('.donut-key > div').evaluate_all('(rows)=>rows.map(r=>r.dataset.token)') == ['uncached_input','output','cached_input','cache_creation']
    assert page.locator('[data-token="uncached_input"] circle').get_attribute('fill') == original_input_color
    page.evaluate('composition(lastUsage)')
    # 캐시 읽기 제외 defaults ON (Q3): Cached Input is out of the donut but stays
    # in the key as a muted row so the share is not hidden.
    expect(page.locator('#donut-nocache')).to_have_attribute('aria-pressed','true')
    assert '차트 제외' in page.locator('.donut-key').inner_text()
    assert 'excluded' in page.locator('.donut-key [data-token="cached_input"]').get_attribute('class')
    expect(page.locator('#composition .donut')).to_contain_text('캐시 읽기 제외')
    page.locator('#donut-nocache').click()
    expect(page.locator('#donut-nocache')).to_have_attribute('aria-pressed','false')
    assert page.locator('.donut-key > div').evaluate_all('(rows)=>rows.map(r=>r.dataset.token)') == ['cache_creation','cached_input','uncached_input','output']
    assert '차트 제외' not in page.locator('.donut-key').inner_text()
    page.locator('#donut-nocache').click()
    expect(page.locator('#donut-nocache')).to_have_attribute('aria-pressed','true')
    assert page.locator('.donut-key > div').evaluate_all('(rows)=>rows.map(r=>r.dataset.token)') == ['cache_creation','cached_input','uncached_input','output']
    assert '차트 제외' in page.locator('.donut-key').inner_text()
    hover_date(page,0)
    tip=page.locator('#chart-tooltip');expect(tip).to_be_visible()
    expect(tip.locator('.tooltip-heading')).to_contain_text('2026-09-07')
    expect(tip.locator('.tooltip-row:not(.tooltip-missing)')).to_have_count(5)
    expect(tip.locator('.tooltip-missing strong')).to_have_text('—')
    for i,label in enumerate(labels):
        row=tip.locator('.tooltip-row:not(.tooltip-missing)').nth(i);expect(row).to_contain_text(label)
        expect(row.locator('strong')).to_have_text(f'{1339*i:,}')
    hover_date(page,1);expect(tip.locator('.tooltip-heading')).to_contain_text('2026-09-08')
    expect(tip.locator('.tooltip-total strong')).to_have_text('20,085')
    hover_date(page,2)
    assert tip.locator('.tooltip-row:not(.tooltip-missing) strong').all_text_contents()==['0']*5
    page.mouse.move(5,5);expect(tip).to_be_hidden()
    page.locator('#chart > svg').focus();expect(tip).to_be_visible()
    page.keyboard.press('End');expect(tip).to_contain_text('2026-09-09')
    page.keyboard.press('ArrowLeft');expect(tip).to_contain_text('2026-09-08')
    page.keyboard.press('Escape');expect(tip).to_be_hidden()
    page.locator('#legend button').first.click();hover_date(page,1)
    expect(tip.locator('.tooltip-row:not(.tooltip-missing)')).to_have_count(5)
    expect(tip.locator('.tooltip-row:not(.tooltip-missing)').first).to_contain_text('숨김')
    page.locator('#legend button').first.click()
    page.locator('#cumulative').select_option('1');loaded(page);hover_date(page,1)
    expect(page.locator('#chart-tooltip')).to_contain_text('선택 기간 누적')
    page.locator('#cumulative').select_option('0');loaded(page)
    # Default Total descending with consecutive row numbers; every data column toggles.
    def numeric_values(key):
        index={'uncached_input':5,'cached_input':6,'output':7,'total':8,'cache_creation':10,'reasoning':11}[key]
        return page.locator('#rows tr').evaluate_all('(rows,index)=>rows.map(r=>Number(r.children[index].textContent.replaceAll(",","")))',index)
    assert numeric_values('total') == sorted(numeric_values('total'),reverse=True)
    assert page.locator('#rows .row-rank').all_text_contents()==['1','2','3','4','5']
    for key in ('uncached_input','cached_input','output','cache_creation','reasoning','total'):
        button=page.locator('[data-sort="'+key+'"]');button.click()
        values=numeric_values(key)
        direction=button.locator('..').get_attribute('aria-sort')
        assert values==sorted(values,reverse=direction=='descending')
        button.click();values=numeric_values(key)
        assert button.locator('..').get_attribute('aria-sort') != direction
        assert values==sorted(values,reverse=direction!='descending')
    for key,index in (('provider',1),('model',2)):
        button=page.locator('[data-sort="'+key+'"]');button.click()
        before=page.locator('#rows tr').evaluate_all('(rows,index)=>rows.map(r=>r.children[index].textContent)',index)
        assert before==sorted(before)
        button.click()
        after=page.locator('#rows tr').evaluate_all('(rows,index)=>rows.map(r=>r.children[index].textContent)',index)
        assert after==list(reversed(before))
    # Table search narrows rows by model/provider and reports the count.
    page.locator('#model-search').fill('model-openai')
    expect(page.locator('#rows tr')).to_have_count(1)
    expect(page.locator('#model-search-count')).to_have_text('1/5개')
    page.locator('#model-search').fill('zzz')
    expect(page.locator('#rows')).to_contain_text('검색과 일치하는 모델이 없습니다')
    page.locator('#model-search').fill('')
    expect(page.locator('#rows tr')).to_have_count(5)
    page.locator('[data-sort="output"]').click()
    direction=page.locator('[data-sort="output"]').locator('..').get_attribute('aria-sort')
    page.evaluate('refresh()');loaded(page)
    expect(page.locator('[data-sort="output"]').locator('..')).to_have_attribute('aria-sort',direction)
    page.locator('[data-sort="total"]').focus();page.keyboard.press('Enter')
    expect(page.locator('[data-sort="total"]').locator('..')).to_have_attribute('aria-sort','descending')
    # Ranking follows the table metric: leader value matches the Total column.
    top=page.locator('#ranking .rank').first
    expect(top.locator('.rank-label strong')).to_contain_text('model-Other')
    expect(top.locator('.rank-label strong small')).to_contain_text('route-Other')
    assert '12,051' in top.locator('.rank-bar rect').get_attribute('data-tip')  # 3,060 plus cache creation 8,991
    # Ten series collapse to 7+기타 with eight distinct colors; 기타 is muted.
    page.evaluate('''() => {const copy=structuredClone(lastUsage);
      copy.labels=[...lastUsage.labels,'m6','m7','m8','m9','m10'];
      for(const p of copy.series)for(const l of copy.labels)p.values[l]=p.values[l]||{uncached_input:1,cached_input:0,output:0,cache_creation:0,reasoning:0};
      chart(copy);}''')
    fills=page.locator('#legend button').evaluate_all('els=>els.map(e=>e.style.getPropertyValue("--color"))')
    assert len(fills)==8 and len(set(fills))==8,fills
    # Colors follow the label, not the ordering.
    a=page.evaluate("paletteFor(['aa','bb','cc'])('aa')")
    b=page.evaluate("paletteFor(['cc','aa','bb'])('aa')")
    assert a==b
    page.evaluate('chart(lastUsage)')
    # The 5h window precedes the weekly one; Gemini stays ahead of 3p without changing percentages.
    page.evaluate("renderLimits({limits:['3p-5h','3p-weekly','gemini-5h','gemini-weekly'].map(bucket=>({route:'antigravity',bucket,remaining:42,status:'fresh'}))})")
    quota=page.locator('.limit-card').filter(has_text='Google / Antigravity')
    assert quota.locator('.quota-window-title').all_text_contents()==['5시간','주간']
    assert quota.locator('.bucket-label').all_text_contents()==['Gemini','3rd party','Gemini','3rd party']
    assert all('42%' in value for value in quota.locator('.remaining').all_text_contents())
    # Themes affect chart palettes and all surfaces, not only the page background.
    themes=['forest','paper','charcoal','midnight','blue','violet','rose','amber','ocean']
    backgrounds=set();palettes=set()
    page.locator('#settings-open').click()  # the theme picker is the first block of the settings view
    for theme in themes:
        page.locator('#theme').select_option(theme)
        expect(page.locator('html')).to_have_attribute('data-theme',theme)
        backgrounds.add(page.evaluate('getComputedStyle(document.documentElement).backgroundColor'))
        palettes.add(page.locator('[data-token="uncached_input"] circle').get_attribute('fill'))
        assert page.evaluate("localStorage.getItem('llm-usage-theme')") == theme
        assert not page.locator('#error').is_visible()
    assert len(backgrounds)==9 and len(palettes)==9
    # The browser theme-color follows the active theme, on change and at load (T4).
    page.locator('#theme').select_option('midnight')
    assert page.evaluate("document.querySelector('meta[name=theme-color]').content")=='#10182d'
    page.reload();loaded(page)
    assert page.evaluate("document.querySelector('meta[name=theme-color]').content")=='#10182d'
    expect(page.locator('#theme')).to_have_value('midnight')
    expect(page.locator('html')).to_have_attribute('data-theme','midnight')
    page.locator('#tabs [data-view="usage"]').click()
    hover_date(page,1)
    expect(page.locator('#chart-tooltip')).to_be_visible()
    page.screenshot(path=str(artifacts/'dashboard-midnight-tooltip.png'))
    page.locator('#settings-open').click()
    page.locator('#theme').select_option('system')
    page.emulate_media(color_scheme='dark');expect(page.locator('html')).to_have_attribute('data-theme','charcoal')
    page.emulate_media(color_scheme='light');expect(page.locator('html')).to_have_attribute('data-theme','paper')
    page.locator('#theme').select_option('rose')
    page.screenshot(path=str(artifacts/'dashboard-rose.png'),full_page=True)
    page.evaluate("localStorage.setItem('llm-usage-theme','invalid-theme')")
    page.reload();loaded(page)
    expect(page.locator('html')).to_have_attribute('data-theme','forest')
    expect(page.locator('#theme')).to_have_value('forest')
    # Mobile pointer conversion uses the SVG transform, including its aspect-ratio padding.
    mobile=browser.new_page(service_workers='block',viewport={'width':390,'height':844},has_touch=True,is_mobile=True)
    fixture_page(mobile)
    preview(mobile);mobile.route(origin+'/api/usage?*',lambda route:route.fulfill(json=data))
    mobile.goto(origin+'/');loaded(mobile);mobile.locator('#settings-open').click()
    mobile.locator('#theme').select_option('ocean');mobile.locator('#settings-back').click()
    mobile.locator('#tabs [data-view="usage"]').click()
    point=hover_date(mobile,1);mobile.touchscreen.tap(point['x'],point['y'])
    expect(mobile.locator('#chart-tooltip')).to_be_visible()
    expect(mobile.locator('#chart-tooltip .tooltip-row:not(.tooltip-missing)')).to_have_count(5)
    assert mobile.evaluate('document.documentElement.scrollWidth<=innerWidth')
    box=mobile.locator('#chart-tooltip').bounding_box();assert box['x']>=0 and box['x']+box['width']<=390
    # Detail columns fold on mobile behind a toggle; the table stays in the wrap.
    mobile.locator('[data-panel="models"] > summary').click()
    expect(mobile.locator('#model-table th.col-detail').first).to_be_hidden()
    expect(mobile.locator('#rows td.col-detail').first).to_be_hidden()
    mobile.locator('#cols-toggle').click()
    expect(mobile.locator('#model-table th.col-detail').first).to_be_visible()
    mobile.locator('#cols-toggle').click()
    # Responsive chart: viewBox tracks the container so text renders near 1:1,
    # and evenly thinned x labels never overlap.
    scale=mobile.evaluate('''() => {const s=document.querySelector('#chart > svg');
      return s.getBoundingClientRect().width/s.viewBox.baseVal.width;}''')
    assert 0.8<scale<=1.01,scale
    boxes=mobile.evaluate('''() => [...document.querySelectorAll('#chart > svg > text')]
      .filter(e=>+e.getAttribute('y')>170).map(e=>{const b=e.getBBox();return{x:b.x,width:b.width};}).sort((a,b)=>a.x-b.x)''')
    assert boxes and all(a['x']+a['width']<=b['x']+0.5 for a,b in zip(boxes,boxes[1:])),boxes
    mobile.screenshot(path=str(artifacts/'dashboard-ocean-mobile.png'))
    # Storage restrictions and corrupt preferences must not break the dashboard.
    restricted=browser.new_page(service_workers='block')
    fixture_page(restricted);preview(restricted)
    restricted.add_init_script("Storage.prototype.getItem=function(){throw Error('blocked')};Storage.prototype.setItem=function(){throw Error('blocked')};")
    restricted.route(origin+'/api/usage?*',lambda route:route.fulfill(json=data))
    restricted.goto(origin+'/');loaded(restricted);restricted.locator('#settings-open').click();restricted.locator('#theme').select_option('violet')
    expect(restricted.locator('html')).to_have_attribute('data-theme','violet')
    assert not errors,errors
    browser.close()
print('Dashboard UI passed: shared date tooltip, zero/hidden values, touch/keyboard, composition order/colors, exact Total, nine themes, persistence, system theme, storage fallback.')
