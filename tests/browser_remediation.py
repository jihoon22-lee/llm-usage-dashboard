"""Render regression inputs through the real chart and tooltip under production CSP."""
from browser_support import fixture_page, watch_csp
from playwright.sync_api import sync_playwright, expect

with sync_playwright() as p:
    browser=p.chromium.launch()
    page=browser.new_page(service_workers='block',viewport={'width':1440,'height':1000})
    fixture_page(page)
    errors=[];page.on('pageerror',lambda e:errors.append(str(e)));watch_csp(page,errors)
    page.goto('https://dashboard.test/?view=analysis')
    expect(page.locator('#chart > svg')).to_be_visible()
    expect(page.locator('#refresh')).to_be_enabled()
    for width in (1440,390):
        page.set_viewport_size({'width':width,'height':1000})
        page.evaluate('()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)))')
        for metric,expected in [('tokens','210'),('requests','21'),('cost','$2.1')]:
            page.evaluate('''metric=>{
              $('group').value='project';$('metric').value=metric;$('cumulative').value='0';hiddenLabels.clear();
              const labels=Array.from({length:9},(_,i)=>'P'+i);
              const now=Object.fromEntries(labels.map(l=>[l,{uncached_input:10,requests:1,cost:.1}]));
              const before=Object.fromEntries(labels.map(l=>[l,{uncached_input:20,requests:2,cost:.2}]));
              before.old={uncached_input:30,requests:3,cost:.3};
              window.reviewData={labels,series:[{time:'2026-09-08T00:00:00+09:00',values:now}],
                compare:{label:'previous',start:'2026-09-01',end_exclusive:'2026-09-02',
                series:[{time:'2026-09-08T00:00:00+09:00',compared_time:'2026-09-01',values:before}]}};
              chart(reviewData);
            }''',metric)
            page.locator('#chart > svg').focus()
            expect(page.locator('#chart-tooltip .tooltip-row').filter(has_text='previous').locator('strong')).to_have_text(expected)
            # Hide one member of the shared mapping: its contribution disappears in both periods.
            page.locator('#legend button[data-label="P0"]').click()
            page.locator('#chart > svg').focus()
            reduced={'tokens':'190','requests':'19','cost':'$1.9'}[metric]
            expect(page.locator('#chart-tooltip .tooltip-row').filter(has_text='previous').locator('strong')).to_have_text(reduced)
        for metric,expected in [('tokens','12'),('requests','12'),('cost','$12')]:
            page.evaluate("""metric=>{
              $('group').value='model';$('metric').value=metric;$('cumulative').value='1';hiddenLabels.clear();
              const value=n=>({uncached_input:n,requests:n,cost:n});
              window.cumulativeReview={labels:['A','B'],series:[
                {time:'2026-09-08',values:{A:value(2),B:value(1)}},
                {time:'2026-09-09',values:{A:value(4),B:value(2)}}],
                compare:{label:'previous',start:'2026-09-01',end_exclusive:'2026-09-03',series:[
                  {time:'2026-09-08',values:{A:value(5),B:value(2)}},
                  {time:'2026-09-09',values:{A:value(7),B:value(3)}}]}};
              hiddenLabels.add('B');chart(cumulativeReview);
            }""",metric)
            svg=page.locator('#chart > svg')
            svg.focus();svg.press('End')
            expect(page.locator('#chart-tooltip .tooltip-row').filter(has_text='previous').locator('strong')).to_have_text(expected)
            expect(page.locator('#chart-tooltip .tooltip-total strong')).to_have_text('$4' if metric=='cost' else '4')
            # Comparison uses the same cumulative value as its visible line.
            coords=page.locator('#chart path[stroke-dasharray]').get_attribute('d')
            assert len(coords.split())==2,coords
            # With max=12, niceStep produces ceiling=15 in both layouts.
            last_y=float(coords.split()[-1].split(',')[1])
            floor,height=(162,148) if width==390 else (240,220)
            assert abs((floor-last_y)/height*15-12)<.02,coords
        page.evaluate("$('cumulative').value='0'")
        # Model group must include previous-only models even when the current interval is empty.
        page.evaluate("$('group').value='model';$('metric').value='tokens';hiddenLabels.clear();reviewData.labels=[];reviewData.series[0].values={};chart(reviewData)")
        page.locator('#chart > svg').focus()
        expect(page.locator('#chart-tooltip .tooltip-row').filter(has_text='previous').locator('strong')).to_have_text('210')
        page.evaluate("chartSeries(reviewData).labels.forEach(l=>hiddenLabels.add(l));chart(reviewData)")
        expect(page.locator('#chart .empty')).to_be_visible()
    page.set_viewport_size({'width':1440,'height':1000})
    page.evaluate('()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)))')
    shares=page.evaluate('''()=>{
      $('group').value='model';$('metric').value='tokens';$('cumulative').value='share-line';hiddenLabels.clear();
      window.reviewShare={labels:['A','B'],series:[
        {time:'2026-09-08',values:{A:{uncached_input:25},B:{uncached_input:75}}},
        {time:'2026-09-09',values:{A:{uncached_input:50},B:{uncached_input:50}}}]};
      chart(reviewShare);
      return [...document.querySelectorAll('#chart svg path')].map(path=>
        [...path.getAttribute('d').matchAll(/[ML]([0-9.]+),([0-9.]+)/g)].map(m=>(240-Number(m[2]))/220*100));
    }''')
    assert len(shares)==2 and all(len(values)==2 for values in shares),shares
    assert all(abs(a-b)<.1 for values,wanted in zip(shares,[[25,50],[75,50]]) for a,b in zip(values,wanted)),shares
    page.locator('#legend button[data-label="A"]').click()
    page.locator('#chart > svg').focus()
    expect(page.locator('#chart-tooltip .tooltip-row.muted-series strong')).to_have_text('—')
    browser.close()
    assert not errors,errors
print('Review charts passed: shared comparison groups, previous-only labels, empty current interval, metrics, hidden labels and mobile.')
