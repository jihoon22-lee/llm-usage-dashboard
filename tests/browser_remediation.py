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
    assert all(abs(a-b)<.1 for values,wanted in zip(shares,[[25,50],[75,50]]) for a,b in zip(values,wanted)),shares
    page.locator('#legend button[data-label="A"]').click()
    page.locator('#chart > svg').focus()
    expect(page.locator('#chart-tooltip .tooltip-row.muted-series strong')).to_have_text('—')
    browser.close()
    assert not errors,errors
print('Review charts passed: shared comparison groups, previous-only labels, empty current interval, metrics, hidden labels and mobile.')
