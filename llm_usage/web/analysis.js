'use strict';
// Model table, CSV, filters, views and URL state, usage summary and collection sources.
function modelRowValue(row){
 const key=tableSort.key;
 return key==='total'||key==='share'?modelTotal(row):key==='provider'?`${row.provider} / ${row.route}`:key==='est_cost'?(row.est_cost??-1):key==='avg'?(row.requests?(row.recorded_tokens??modelTotal(row))/row.requests:-1):row[key];
}
function sortedModelRows(data){
 return[...data.rows].sort((a,b)=>{
  const aa=modelRowValue(a),bb=modelRowValue(b);
  const comparison=typeof aa==='number'?aa-bb:compareText.compare(String(aa??''),String(bb??''));
  return comparison*(tableSort.direction==='desc'?-1:1)||compareText.compare(`${a.model} / ${a.route}`,`${b.model} / ${b.route}`);
 });
}
function downloadCsv(kind,header,rows){
 const quote=v=>'"'+String(v??'').replace(/"/g,'""')+'"';
 const lines=[header.map(quote).join(',')].concat(rows.map(r=>r.map(quote).join(',')));
 const endDate=lastUsage?new Date(Date.parse(lastUsage.end_exclusive)-1+9*3600e3).toISOString().slice(0,10):null;
 const span=lastUsage?lastUsage.start.slice(0,10)+'_'+endDate+(lastUsage.period==='1h'?'_1h':''):new Date().toISOString().slice(0,10);
 const blob=new Blob(['\uFEFF'+lines.join('\r\n')],{type:'text/csv;charset=utf-8'});
 const a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download=`llm-usage-${kind}-${span}.csv`;a.click();setTimeout(()=>URL.revokeObjectURL(a.href),1000);
}
$('csv-export').addEventListener('click',()=>{
 if(!lastUsage)return;
 const denom=modelTotal(lastUsage.totals);
 downloadCsv('models',['제작사','사용 서비스','모델','요청 수','평균 토큰/요청','비중 %','Uncached Input','Cached Input','Output','Total (incl. cache creation)','Cache creation','Reasoning','비용 추정 USD'],
  sortedModelRows(lastUsage).map(r=>[r.provider,routeNames[r.route]||r.route,r.model,r.supplemental_tokens&&!r.requests?'':r.requests||0,r.requests?Math.round((r.recorded_tokens??modelTotal(r))/r.requests):'',denom?(modelTotal(r)/denom*100).toFixed(1):'',r.unclassified_tokens&&!r.recorded_tokens?'':r.uncached_input||0,r.unclassified_tokens&&!r.recorded_tokens?'':r.cached_input||0,r.unclassified_tokens&&!r.recorded_tokens?'':r.output||0,modelTotal(r),r.unclassified_tokens&&!r.recorded_tokens?'':r.cache_creation||0,REASONING_UNREPORTED.has(r.route)&&!r.reasoning?'':r.reasoning||0,r.est_cost??'']));
});
$('csv-sessions').addEventListener('click',()=>{
 if(!lastUsage||!lastUsage.sessions)return;
 downloadCsv('sessions',['서비스','세션','프로젝트','종류','첫 활동 KST','마지막 활동 KST','요청 수','토큰','비용 추정 USD'],
  lastUsage.sessions.map(s=>[routeNames[s.route]||s.route,s.session,s.project||'',KIND_NAMES[s.kind]||s.kind||'',when(s.first_ts),when(s.last_ts),s.requests||0,total(s),s.cost??'']));
});
$('csv-projects').addEventListener('click',()=>{
 if(!lastUsage||!lastUsage.projects)return;
 downloadCsv('projects',['프로젝트','세션','요청 수','토큰','비용 추정 USD','최근 활동 KST'],
  lastUsage.projects.map(p=>[p.project,p.sessions||'',p.requests||0,total(p),p.cost??'',when(p.last_ts)]));
});
$('csv-series').addEventListener('click',event=>{
 event.preventDefault();event.stopPropagation();
 if(!lastUsage)return;
 const labels=lastUsage.labels||[];
 const cost=$('metric').value==='cost';
 const header=['시간 KST',...labels.map(seriesLabel),'합계 '+metricName()];
 if(cost)header.push(...labels.map(label=>seriesLabel(label)+' 미산정 토큰'),'합계 미산정 토큰');
 downloadCsv('series-'+$('metric').value,header,(lastUsage.series||[]).map(p=>[p.time,
  ...(cost?costCsvFields(labels.map(l=>p.values[l]||{})):[...labels.map(l=>metricValue(p.values[l]||{})),labels.reduce((s,l)=>s+metricValue(p.values[l]||{}),0)])]).concat($('metric').value==='tokens'?(lastUsage.reconciliation?.unallocated_intervals||[]).map(e=>['날짜 미배분: '+e.start+' → '+e.end_exclusive,...labels.map(()=>''),e.tokens]):[]));
});
const FILTER_IDS=['period','start','end','granularity','group','cumulative','metric','compare','scope'];
const FILTER_DEFAULTS={period:'7d',granularity:'day',group:'route',cumulative:'0',metric:'tokens',compare:'',scope:''};
function renderScope(data){
 const sel=$('scope'),scope=data.scope||'';
 sel.innerHTML='';sel.add(new Option('전체',''));
 for(const [kind,label] of Object.entries(SCOPE_LABELS)){
  const values=(data.scope_options&&data.scope_options[kind])||[];if(!values.length)continue;
  const group=document.createElement('optgroup');group.label=label;
  for(const v of values){const o=new Option(kind==='route'?(routeNames[v]||v):v,kind+':'+v);group.append(o);}
  sel.add(group);
 }
 if(scope&&![...sel.options].some(o=>o.value===scope))sel.add(new Option(scopeText(scope),scope));
 sel.value=scope||'';
 const chip=$('scope-chip');
 chip.hidden=!scope;
 if(scope)chip.textContent=scopeText(scope)+' ×';
}
// Earlier releases used other tab names; saved views and bookmarked URLs still resolve.
const VIEW_ALIASES={plan:'quota',overview:'quota',analysis:'usage',insights:'reports',sources:'status'};
// Panels fed by the lazily fetched insights section.
const INSIGHT_VIEWS=['usage','reports','status'];
let currentView='quota',previousView='quota';
const viewEls=[...document.querySelectorAll('main>[data-view]')];
function setView(name,sync=true){
 name=VIEW_ALIASES[name]||name;
 const next=viewEls.some(e=>e.dataset.view===name)?name:'quota';
 // Settings is not a tab: remember where to return, and keep the last tab reachable by Tab.
 if(next==='settings'&&currentView!=='settings')previousView=currentView;
 currentView=next;
 const focusable=currentView==='settings'?previousView:currentView;
 viewEls.forEach(e=>{e.hidden=e.dataset.view!==currentView;});
 document.querySelectorAll('#tabs [data-view]').forEach(b=>{const on=b.dataset.view===currentView;b.classList.toggle('active',on);b.setAttribute('aria-selected',String(on));b.tabIndex=b.dataset.view===focusable?0:-1;});
 document.body.dataset.view=currentView;
 if(currentView==='settings')loadConfig();
 if(currentView==='status')loadNotifyLog();
 if(INSIGHT_VIEWS.includes(currentView))loadInsights();
 if(currentView==='reports')loadReports();
 // A hidden chart skipped its draw; render synchronously on tab switch so the
 // ResizeObserver sees the same width and does not queue a duplicate redraw.
 if(chartData&&$('chart').clientWidth)chart(chartData);
 // SVGs rendered while their section was hidden have no fitted font-size yet.
 fitSvgText(document);
 saveDefaults();if(sync)syncUrl();
}
$('tabs').addEventListener('click',event=>{
 const button=event.target.closest('button[data-view]');if(button)setView(button.dataset.view);
});
$('settings-open').addEventListener('click',()=>{setView('settings');$('settings-back').focus({preventScroll:true});});
$('settings-back').addEventListener('click',()=>{setView(previousView);$('settings-open').focus({preventScroll:true});});
$('first-use-sources').addEventListener('click',()=>{setView('status');$('tab-status').focus();});
$('tabs').addEventListener('keydown',event=>{
 const tabs=[...$('tabs').querySelectorAll('button[data-view]')],i=tabs.indexOf(document.activeElement);
 if(i<0)return;
 const j=event.key==='ArrowRight'?(i+1)%tabs.length:event.key==='ArrowLeft'?(i-1+tabs.length)%tabs.length:event.key==='Home'?0:event.key==='End'?tabs.length-1:null;
 if(j==null)return;
 event.preventDefault();tabs[j].focus();setView(tabs[j].dataset.view);
});
function syncUrl(push){
 const q=new URLSearchParams();
 for(const id of FILTER_IDS){
  if((id==='start'||id==='end')&&$('period').value!=='custom')continue;
  const v=$(id).value;
  if(FILTER_DEFAULTS[id]===undefined||v!==FILTER_DEFAULTS[id])q.set(id,v);
 }
 if(currentView!=='quota')q.set('view',currentView);
 const s=q.toString();
 history[push?'pushState':'replaceState'](null,'',location.pathname+(s?'?'+s:''));
}
// Drill-down applies a filter change, moves to the usage view, and pushes a
// history entry so the browser back button restores the previous filters.
function applyDrill(changes,view='usage'){
 for(const [id,v] of Object.entries(changes)){
  if(id==='scope'&&v&&![...$('scope').options].some(o=>o.value===v))$('scope').add(new Option(v,v));
  $(id).value=v;
 }
 const custom=$('period').value==='custom';$('start-wrap').hidden=!custom;$('end-wrap').hidden=!custom;
 saveDefaults();
 // Drill-down is one history entry: skip setView's replaceState so the entry
 // being left behind keeps the pre-drill filters.
 if(view)setView(view,false);
 syncUrl(true);filterSummary();refresh();
}
window.addEventListener('popstate',()=>{
 const params=new URLSearchParams(location.search);
 for(const id of FILTER_IDS){
  const el=$(id),v=params.get(id);
  if(el.tagName==='SELECT'){
   const value=v??FILTER_DEFAULTS[id];
   if(value&&id==='scope'&&![...el.options].some(o=>o.value===value))el.add(new Option(value,value));
   if([...el.options].some(o=>o.value===value))el.value=value;
  }else el.value=/^\d{4}-\d{2}-\d{2}$/.test(v||'')?v:(id==='start'||id==='end'?today:FILTER_DEFAULTS[id]||'');
 }
 const custom=$('period').value==='custom';$('start-wrap').hidden=!custom;$('end-wrap').hidden=!custom;
 setView(params.get('view')||'quota');saveDefaults();filterSummary();refresh();
});
document.addEventListener('click',event=>{
 const scope=event.target.closest('[data-scope]');
 // A button inside a drillable row (project 상세) acts on its own.
 if(scope&&event.target.closest('button')&&!event.target.closest('button[data-scope]'))return;
 if(scope){applyDrill({scope:scope.dataset.scope});return;}
 const day=event.target.closest('[data-day]');
 if(day)applyDrill({period:'custom',start:day.dataset.day,end:day.dataset.day,granularity:'hour'});
});
// Enter/Space on a drillable row or calendar day mirrors click. On the calendar
// grid, ↑↓ move ±1 day, ←→ ±7 days and Home/End jump to the edges (roving
// tabindex keeps the last visited day in the tab order).
document.addEventListener('keydown',event=>{
 const day=event.target.closest?.('[data-day]');
 if(day&&!event.target.closest('button,input,select,textarea,a')){
  if(event.key==='Enter'||event.key===' '){event.preventDefault();applyDrill({period:'custom',start:day.dataset.day,end:day.dataset.day,granularity:'hour'});return;}
  const cells=[...document.querySelectorAll('#calendar .cal-cell[data-day]')],i=cells.indexOf(day);
  const delta={ArrowUp:-1,ArrowDown:1,ArrowLeft:-7,ArrowRight:7}[event.key];
  const j=event.key==='Home'?0:event.key==='End'?cells.length-1:(i<0||delta==null)?-1:i+delta;
  if(j>=0&&j<cells.length){event.preventDefault();cells.forEach(c=>{c.tabIndex=-1;});cells[j].tabIndex=0;cells[j].focus();}
  return;
 }
 if(event.key!=='Enter'&&event.key!==' ')return;
 if(event.target.closest('button,input,select,textarea,a'))return;
 const scope=event.target.closest?.('[data-scope]');
 if(scope){event.preventDefault();applyDrill({scope:scope.dataset.scope});}
});
const compactMedia=matchMedia('(max-width:600px)');
const foldedPanels=[...document.querySelectorAll('details.mobile-fold')];
// Panels marked data-mobile-open start expanded on narrow screens; the rest
// stay folded until the user touches them (compactPanels honours this map).
const mobileOpen=new Map(foldedPanels.map(panel=>[panel,panel.hasAttribute('data-mobile-open')]));
const filterOptions=[...document.querySelectorAll('.filters option')].map(option=>({option,label:option.textContent}));
const shortOptions={period:{'1h':'1시간',today:'오늘','7d':'7일','30d':'30일',all:'전체',custom:'직접'},
 granularity:{day:'일별',hour:'시간',week:'주별',month:'월별'},
 group:{provider:'제작사',route:'서비스',model:'모델',project:'프로젝트',agent:'에이전트'},
 metric:{tokens:'토큰',requests:'요청 수',cost:'비용'},
 compare:{'':'비교 없음',previous:'직전',week:'7일 전',month:'28일 전'},
 cumulative:{'0':'구간별','1':'누적',share:'점유율 막대','share-area':'점유율 면적','share-line':'점유율 꺾은선'}};
function filterSummary(){
 const el=$('filter-summary');if(!el)return;
 const short=id=>{const map=shortOptions[id]||{},opt=$(id).selectedOptions[0];return map[$(id).value]||(opt&&opt.textContent)||'';};
 const parts=[fullLabel('period'),short('granularity'),short('group'),short('metric')];
 if($('period').value==='custom'&&$('start').value&&$('end').value)parts[0]=$('start').value.slice(5)+'~'+$('end').value.slice(5);
 if($('scope').value)parts.unshift(scopeText($('scope').value));
 if($('compare').value)parts.push(short('compare')+' 비교');
 if($('cumulative').value!=='0')parts.push(short('cumulative'));
 el.textContent=parts.join(' · ');
}
function compactPanels(){
 foldedPanels.forEach(panel=>{panel.open=compactMedia.matches?mobileOpen.get(panel):true;});
 filterOptions.forEach(({option,label})=>{if(!option.parentElement)return;option.textContent=compactMedia.matches?(shortOptions[option.parentElement.id]?.[option.value]||label):label;});
}
const fullLabel=id=>{const opt=$(id).selectedOptions[0],f=filterOptions.find(e=>e.option===opt);return (f&&f.label)||(opt&&opt.textContent)||'';};
foldedPanels.forEach(panel=>panel.addEventListener('toggle',()=>{if(compactMedia.matches)mobileOpen.set(panel,panel.open);}));
compactMedia.addEventListener('change',compactPanels);compactPanels();
function renderUsage(data){
 lastUsage=data;
 $('first-use').hidden=!!data.scope||total(data.lifetime||{})>0||data.lifetime?.requests!==0;
 const gaps=data.unavailable_routes||[];
 $('usage-gaps').hidden=!gaps.length;
 $('usage-gaps').innerHTML=gaps.map(row=>`<strong>${esc(routeNames[row.route]||row.route)} · ${gapLabel(row)}</strong><span>${row.observed?'입력·출력·캐시 숫자는 보존되고 있습니다. 반복 수신과 누적값의 의미를 검증하기 전까지 차트·합계에는 포함하지 않습니다. 마지막 수신 '+when(row.observation_checked):'구독 한도와 별개로 소비량 원본이 확보되지 않아 차트·합계에 포함되지 않습니다.'}</span>`).join('');
 const periodLabel=$('period').value==='custom'
  ?`${data.start.slice(0,10)} ~ ${new Date(Date.parse(data.end_exclusive)-864e5).toISOString().slice(0,10)}`
  :fullLabel('period');
 const recent=data.period==='1h';
 const range=recent?`${when(Date.parse(data.start)/1000)} → ${when(Date.parse(data.end_exclusive)/1000)} KST`:null;
 $('cards-period').textContent=`${periodLabel}${range?' · '+range:''}${data.scope?' · '+scopeText(data.scope):''}`;
 $('recent-usage').setAttribute('aria-pressed',String(recent));
 $('recent-usage-note').hidden=!recent;
 document.querySelector('[data-panel="heatmap"]').hidden=recent;
 renderScope(data);
 // Card deltas always compare token totals, whatever metric is selected.
 // The earlier window is cut at the same elapsed time as the running period.
 const cmpSum=data.compare&&data.compare.elapsed?total(data.compare.elapsed):null;
 const curSum=total(data.totals);
 const delta=cmpSum?(curSum-cmpSum)/cmpSum:null;
 const cards=[['일반 입력','uncached_input'],['캐시 읽기','cached_input'],['출력','output'],['전체 토큰','total']];
 $('cards').innerHTML=cards.map(([label,k])=>{
  const d=k==='total'&&delta!=null?` · ${esc(data.compare.label)} 같은 경과 시간 대비 ${delta>=0?'+':'−'}${Math.abs(delta*100).toFixed(1)}%`:'';
  return `<div class="stat"><div class="stat-label">${label}</div><div class="stat-value" data-tip="${fmt(k==='total'?total(data.totals):data.totals[k])}">${compact(k==='total'?total(data.totals):data.totals[k])}</div><small>선택 기간 · 전체 누계 ${compact(k==='total'?total(data.lifetime):data.lifetime[k])}${d}</small></div>`;}).join('')
 +(data.cost&&(data.cost.period!=null||data.cost.lifetime!=null)?`<div class="stat"><div class="stat-label">비용 추정</div><div class="stat-value" data-tip="${esc(usd(data.cost.period))}">${usd(data.cost.period)}</div><small>공개 단가 기준 환산 · 단가 확정 토큰 ${percent(data.cost.coverage)}${data.cost.lifetime!=null?' · 전체 누계 '+usd(data.cost.lifetime):''}${data.cost.cache_savings?` · 캐시 읽기 덕분 ${usd(data.cost.cache_savings)} 절감 환산`:''}</small></div>`:'');
 renderModelTable(data);renderHeatmap(data.heatmap);
 $('coverage').textContent=data.coverage;$('token-note').textContent=data.token_note;
 renderAccountGap(data.account_gap);
 renderReconciliation(data.reconciliation);
 renderSources(data.sources||[]);
 const costOption=$('metric').querySelector('[value="cost"]');
 costOption.disabled=!(data.cost&&(data.cost.period!=null||data.cost.lifetime!=null));
 if(costOption.disabled&&$('metric').value==='cost')$('metric').value='tokens';
 $('chart-caption').textContent=`${range||data.start.slice(0,10)+'부터'} · ${fullLabel('group')} · ${metricName()} · ${modeNames[$('cumulative').value]}`;
 $('chart-by-model').hidden=$('group').value==='model';
 chart(data);composition(data);ranking(data);renderInsights(data);renderWhatif();
 // A refresh may land after a lazy insights fetch; if the new core payload
 // dropped the merged section while a tab that shows it is open, fetch it again.
 if(INSIGHT_VIEWS.includes(currentView)&&!('insights' in data))loadInsights();
}
// Codex account totals minus local records: work that ran on OpenAI's side (ChatGPT
// dot, Codex cloud). UTC days and tokens only, so it never enters charts or costs.
function renderAccountGap(gap){
 const box=$('account-gap');
 if(!gap||!gap.days||!gap.days.length){box.innerHTML='';return;}
 const days=gap.days.slice(-7).reverse();
 box.innerHTML=`<details class="source-group account-gap"${gap.total_gap?' open':''}><summary>Codex 계정 합계 대비 로컬 미기록 <small>최근 ${gap.days.length}일 ${compact(gap.total_gap)} 토큰${gap.checked?' · '+esc(ago(gap.checked))+' 확인':''}</small></summary>`
  +`<p class="source-detail">OpenAI 계정의 KST 09:00–다음 날 09:00 집계 토큰에서 이 PC의 Codex 기록을 뺀 값입니다. ChatGPT 에이전트(dot)·Codex 클라우드처럼 서버 실행·다른 기기·집계 지연 등이 섞일 수 있어 dot 사용량으로 단정하지 않습니다. 모델·입출력 구분이 없어 비용은 추정하지 않습니다.</p>`
  +`<div class="table-wrap"><table><thead><tr><th>집계 범위(KST)</th><th>계정 합계</th><th>로컬 기록</th><th>미기록</th></tr></thead><tbody>`
  +days.map(d=>`<tr><td>${esc(d.day)} 09:00–다음 날 09:00</td><td data-tip="${fmt(d.account)}">${compact(d.account)}</td><td data-tip="${fmt(d.local)}">${compact(d.local)}</td><td data-tip="${fmt(d.gap)}">${d.over?`<span title="로컬 기록이 계정 합계보다 많습니다 · 계정 집계 지연일 수 있습니다">0 (초과 ${compact(d.over)})</span>`:compact(d.gap)}</td></tr>`).join('')
  +'</tbody></table></div></details>';
}
function renderReconciliation(r){
 const box=$('usage-reconciliation');if(!box)return;
 box.hidden=!r||!(r.supplemental_tokens||r.overlapping_intervals?.length||r.conflicts?.length);
 if(box.hidden){box.replaceChildren();return;}
 const opened=box.querySelector('details')?.open,focused=box.contains(document.activeElement);
 const intervals=(items,label)=>items?.length?`<section><h4>${label}</h4><ul class="reconciliation-intervals">${items.map(e=>`<li><span>${esc(stamp(Date.parse(e.start)/1000))} → ${esc(stamp(Date.parse(e.end_exclusive)/1000))} KST</span><b>${compact(e.tokens)} 토큰</b></li>`).join('')}</ul></section>`:'';
 box.innerHTML=`<details${opened?' open':''}><summary><strong>사용량 대조 <small>KST</small></strong><span>보완 <b>${compact(r.supplemental_tokens)}</b> 토큰</span>`
  +(r.unallocated_tokens?`<span class="reconciliation-tag">날짜 미배분 ${fmt(r.unallocated_intervals?.length||0)}구간</span>`:'')
  +(r.conflicts?.length?`<span class="reconciliation-warning">충돌 ${fmt(r.conflicts.length)}건</span>`:'')
  +'<span class="reconciliation-toggle">상세 보기</span></summary><div class="reconciliation-detail">'
  +`<p>요청 기록 ${compact(r.recorded_tokens)} + 서버 누계 보완 ${compact(r.supplemental_tokens)} 토큰</p>`
  +(r.origin_tokens?`<p>세부 기록 출처 · 로컬 ${compact(r.origin_tokens.local||0)} · 클라우드 ${compact(r.origin_tokens.cloud||0)} · telemetry ${compact(r.origin_tokens.telemetry||0)} 토큰</p>`:'')
  +(r.unallocated_tokens?`<p>날짜 미배분 ${compact(r.unallocated_tokens)} 토큰은 기간 합계에 포함되며 일별 차트에는 배분하지 않습니다.</p>`:'')
  +intervals(r.unallocated_intervals,'합계 포함 · 날짜 미배분')
  +intervals(r.overlapping_intervals,'합계 제외 · 기간 경계에 걸친 구간')
  +(r.conflicts?.length?`<p class="reconciliation-warning">누계 대조 충돌 ${fmt(r.conflicts.length)}건 · 원본 재확인 필요</p>`:'')
  +'<p>요청 수·평균·비용은 확인된 세부 기록 기준입니다. 계정 차이는 dot 확정 사용량이 아닙니다.</p></div></details>';
 if(focused)box.querySelector('summary').focus({preventScroll:true});
}
// Collection status: a summary, problems first, grouped by provider, each with what it means.
function renderSources(list){
 const bucket=s=>s.status==='error'||s.status==='unavailable'?'bad':s.status==='partial'||s.status==='stale'?'warn':s.status==='idle'||s.status==='ended'?'idle':'ok';
 const counts={ok:0,warn:0,bad:0,idle:0};list.forEach(s=>counts[bucket(s)]++);
 const groups=new Map();
 for(const s of [...list].sort((a,b)=>(SOURCE_RANK[a.status]??4)-(SOURCE_RANK[b.status]??4)||a.name.localeCompare(b.name)))
  (groups.get(sourceGroup(s.name))||groups.set(sourceGroup(s.name),[]).get(sourceGroup(s.name))).push(s);
 const worst=items=>Math.min(...items.map(s=>SOURCE_RANK[s.status]??4));
 const card=s=>`<div class="source${bucket(s)==='idle'||s.status==='stale'?' stale':''} src-${bucket(s)}"><p class="source-line"><span><strong>${esc(s.name)}</strong> · ${esc(statuses[s.status]||s.status)}</span><small>${s.checked?when(s.checked):'확인 기록 없음'}</small></p>${s.detail?`<small class="source-detail">${esc(s.detail)}</small>`:''}${SOURCE_HINTS[s.status]?`<small class="source-hint">${esc(SOURCE_HINTS[s.status])}</small>`:''}</div>`;
 const ordered=[...groups.entries()].sort((a,b)=>(a[0]==='내부 작업')-(b[0]==='내부 작업')||worst(a[1])-worst(b[1]));
 $('sources').innerHTML=`<div class="source-summary"><span class="ok">정상 <strong>${counts.ok}</strong></span><span class="warn">확인 필요 <strong>${counts.warn}</strong></span><span class="bad">실패·미수집 <strong>${counts.bad}</strong></span><span class="idle">미사용 <strong>${counts.idle}</strong></span></div>`
  +ordered.map(([name,items])=>name==='내부 작업'
   ?`<details class="source-group"><summary>${esc(name)} <small>${items.length}개 · 보정·백필 작업</small></summary><div class="source-list">${items.map(card).join('')}</div></details>`
   :`<section class="source-group"><h4>${esc(name)}</h4><div class="source-list">${items.map(card).join('')}</div></section>`).join('');
}
let modelFilter='';
$('model-search').addEventListener('input',event=>{modelFilter=event.target.value.trim().toLowerCase();if(lastUsage)renderModelTable(lastUsage);});
// 상세 열 toggles share one behaviour across the model, project and session
// tables: flip .show-detail on the table and mirror it to aria-pressed.
function bindColsToggle(button,table){
 button.addEventListener('click',()=>{
  const on=table.classList.toggle('show-detail');
  button.setAttribute('aria-pressed',String(on));
  button.classList.toggle('on',on);
 });
}
bindColsToggle($('cols-toggle'),$('model-table'));
bindColsToggle($('proj-cols'),document.querySelector('[data-panel="projects"] table'));
bindColsToggle($('sess-cols'),document.querySelector('[data-panel="sessions"] table'));
function renderModelTable(data){
 const denom=modelTotal(data.totals);
 const all=sortedModelRows(data);
 const match=r=>`${r.provider} ${routeNames[r.route]||r.route} ${r.model}`.toLowerCase().includes(modelFilter);
 const rows=modelFilter?all.filter(match):all;
 $('model-search-count').textContent=modelFilter?`${rows.length}/${all.length}개`:'';
 const avg=r=>r.requests?(r.recorded_tokens??modelTotal(r))/r.requests:null;
 $('rows').innerHTML=rows.map((r,i)=>`<tr data-scope="model:${esc(r.model)}" tabindex="0" aria-describedby="drill-hint"><td class="row-rank">${fmt(i+1)}</td><td>${esc(r.provider)}<small>${esc(routeNames[r.route]||r.route)}</small></td><td>${esc(r.model)}${r.model==='unknown'?'<small>원본 모델명 미기록</small>':''}</td><td class="col-detail">${r.supplemental_tokens&&!r.requests?'—':fmt(r.requests)}</td><td>${avg(r)!=null?compact(avg(r)):'—'}</td>${tableCells(r,denom)}<td>${usd(r.est_cost)}</td></tr>`).join('')||`<tr><td colspan="13" class="empty">${modelFilter?'검색과 일치하는 모델이 없습니다.':'선택 기간에 수집된 사용 기록이 없습니다.'}</td></tr>`;
 $('total-row').innerHTML=[['선택 기간 합계',data.totals,(data.cost||{}).period],['수집된 전체 누계',data.lifetime,(data.cost||{}).lifetime]].map(([label,row,cost])=>`<tr><td colspan="3">${label}</td><td class="col-detail">${fmt(row.requests)}</td><td>${avg(row)!=null?compact(avg(row)):'—'}</td>${tableCells(row,row===data.totals?denom:0)}<td>${usd(cost)}</td></tr>`).join('');
 document.querySelectorAll('[data-sort]').forEach(button=>{
  const selected=button.dataset.sort===tableSort.key;
  button.closest('th').setAttribute('aria-sort',selected?(tableSort.direction==='desc'?'descending':'ascending'):'none');
  button.querySelector('span').textContent=selected?(tableSort.direction==='desc'?'▼':'▲'):'↕';
 });
 const numeric=!['provider','model'].includes(tableSort.key);
 const direction=numeric?(tableSort.direction==='desc'?'높은 순':'낮은 순'):(tableSort.direction==='desc'?'내림차순':'오름차순');
 $('table-sort-status').textContent=`${sortNames[tableSort.key]} ${direction} · 열 제목을 눌러 정렬`;
}
$('num-compact').setAttribute('aria-pressed',String(compactNumbers()));
$('num-compact').addEventListener('click',()=>{const on=!compactNumbers();storage.set('llmCompactNumbers',on?'1':'0');$('num-compact').setAttribute('aria-pressed',String(on));if(lastUsage)renderModelTable(lastUsage);});
document.querySelectorAll('[data-sort]').forEach(button=>button.addEventListener('click',()=>{
 const key=button.dataset.sort;
 tableSort.direction=tableSort.key===key?(tableSort.direction==='desc'?'asc':'desc'):(['provider','model'].includes(key)?'asc':'desc');
 tableSort.key=key;
 if(lastUsage)renderModelTable(lastUsage);
}));
function bucketLabel(point,short=false){
 const grain=$('granularity').value;
 if(grain==='month')return point.time.slice(0,7);
 if(grain==='week')return point.time.slice(short?5:0,10)+' 주';
 return point.time.slice(short?5:0,grain==='hour'?16:10).replace('T',' ');
}
const metricValue=v=>{const m=$('metric').value;return m==='requests'?(v.requests||0):m==='cost'?(v.cost||0):total(v||{});};
const metricName=()=>({requests:'요청 수',cost:'비용 추정'})[$('metric').value]||'토큰';
const metricFmt=v=>$('metric').value==='cost'?usd(v):fmt(v);
// Cost buckets with tokens of unpriced models say so instead of reading as $0.
const costNote=v=>$('metric').value==='cost'&&v&&v.unpriced?` <small>+미산정 ${compact(v.unpriced)} 토큰</small>`:'';
const metricUnit=()=>({tokens:' 토큰',requests:'회'})[$('metric').value]||'';
const modeNames={'0':'구간별 합계','1':'선택 기간 누적',share:'구간별 점유율 막대','share-area':'구간별 점유율 면적','share-line':'구간별 점유율 꺾은선'};
const modeAria={'0':'구간별 누적 막대 차트','1':'누적 선 그래프',share:'구간별 점유율 막대 차트','share-area':'구간별 점유율 면적 차트','share-line':'구간별 점유율 꺾은선 차트'};
// Model view keeps every observed model, including new low-volume models.
// Other groupings retain the compact top-7 plus 기타 overview.
function chartSeries(data){
 const comparison=data.compare;
 const labels=[...new Set([...data.labels,...(comparison?.series||[]).flatMap(p=>Object.keys(p.values||{}))])];
 const points=data.series;
 const compact=$('group').value!=='model'&&labels.length>8;
 const sum=l=>points.reduce((s,p)=>s+metricValue(p.values[l]||{}),0);
 const kept=new Set(compact?[...labels].filter(l=>l!=='기타').sort((a,b)=>sum(b)-sum(a)).slice(0,7):labels);
 const shown=compact?[...labels.filter(l=>kept.has(l)),'기타']:labels;
 function mapPoints(series){return series.map(p=>{
  const values=Object.fromEntries(shown.map(l=>[l,{}]));
  for(const label of labels){
   const src=p.values[label]||{},target=kept.has(label)?label:'기타';
   for(const [key,value] of Object.entries(src))if(typeof value==='number')values[target][key]=(values[target][key]||0)+value;
  }
  return {...p,values};
 });}
 return {labels:shown,points:mapPoints(points),compare:comparison?{...comparison,series:mapPoints(comparison.series)}:null};
}
// Stable label→palette index so a model keeps its color across period changes.
// Active labels always get distinct indices; inactive owners are evicted.
const labelColorIndex=new Map();
function paletteFor(labels){
 const actives=[...new Set(labels.filter(l=>l!=='기타'))];
 const seen=new Map();
 for(const l of actives){
  const idx=labelColorIndex.get(l);
  if(idx!=null&&!seen.has(idx)){seen.set(idx,l);continue;}
  labelColorIndex.delete(l);
 }
 const free=[];for(let i=0;i<Math.max(colors.length,actives.length);i++)if(!seen.has(i))free.push(i);
 for(const l of actives){
  if(labelColorIndex.has(l))continue;
  let idx;
  if(free.length)idx=free.shift();
  else{
   const inactive=[...labelColorIndex.entries()].find(([k])=>!actives.includes(k));
   idx=inactive?inactive[1]:0;
   if(inactive)labelColorIndex.delete(inactive[0]);
  }
  labelColorIndex.set(l,idx);seen.set(idx,l);
 }
 return label=>{
  if(label==='기타')return mutedColor;
  const i=labelColorIndex.get(label)??0;
  return colors[i]||`hsl(${(i*137.508)%360} 60% 48%)`;
 };
}
$('chart-by-model').addEventListener('click',()=>applyDrill({group:'model'}));
// What-if: the period's tokens at another model's current list rate (rates load once).
let whatifRates=null;
async function loadWhatif(){
 if(whatifRates)return whatifRates;
 const cfg=await api('/api/config');
 const current=e=>Array.isArray(e)?e.filter(x=>x.since<=cfg.today).pop()||e[0]:e;
 whatifRates=Object.fromEntries(Object.entries(cfg.pricing||{}).map(([m,e])=>[m,current(e)]).filter(([,r])=>r&&r.input!=null));
 const sel=$('whatif-model'),kept=sel.value;
 sel.innerHTML='<option value="">모델 선택</option>'+Object.keys(whatifRates).sort(compareText.compare).map(m=>`<option value="${esc(m)}">${esc(m)}</option>`).join('');
 sel.value=kept;return whatifRates;
}
function renderWhatif(){
 const model=$('whatif-model').value,out=$('whatif-result');
 if(!model||!whatifRates||!lastUsage){out.textContent='선택 기간 토큰을 모두 다른 모델의 현재 공개 단가로 쓰면 얼마였을지 환산합니다.';return;}
 const value=estimateWith(lastUsage.rows||[],whatifRates[model]),now=lastUsage.cost&&lastUsage.cost.period;
 const known=(lastUsage.rows||[]).reduce((n,r)=>n+(r.uncached_input||0)+(r.cached_input||0)+(r.output||0)+(r.cache_creation||0),0);
 out.textContent=`내역을 확인한 ${compact(known)} 토큰을 ${model} 단가로: ${usd(value)}`
  +(now?` · 현재 환산 ${usd(now)} 대비 ${value>=now?'+':'−'}${Math.abs((value-now)/now*100).toFixed(0)}%`:'')+' · 모델별 성능 차이와 실제 청구는 반영하지 않습니다.';
}
$('whatif-model').addEventListener('focus',()=>{loadWhatif().catch(e=>{$('whatif-result').textContent='단가를 불러오지 못했습니다. '+e.message;});});
$('whatif-model').addEventListener('change',async()=>{await loadWhatif();renderWhatif();});
// Frequent filter combinations in one tap; each is one history entry like a drill-down.
const PRESETS={recent:{period:'1h',granularity:'hour',group:'project',metric:'tokens'},today:{period:'today',granularity:'hour',group:'route'},week:{period:'7d',granularity:'day',group:'route'},
 models:{period:'30d',granularity:'day',group:'model'},projects:{period:'30d',granularity:'day',group:'project'},
 months:{period:'all',granularity:'month',group:'route'}};
$('view-usage').addEventListener('click',event=>{
 const jump=event.target.closest('[data-usage-jump]');
 if(jump){
  const panel=document.querySelector(`[data-panel="${jump.dataset.usageJump}"]`);
  panel.open=true;mobileOpen.set(panel,true);panel.scrollIntoView({behavior:motion(),block:'start'});return;
 }
 const preset=PRESETS[event.target.closest('[data-preset]')?.dataset.preset];
 if(preset){
  if(preset.period==='1h'){sessSort.key='tokens';sessSort.dir='desc';sessPage=0;}
  applyDrill({cumulative:'0',compare:'',scope:'',...preset});
 }
});
