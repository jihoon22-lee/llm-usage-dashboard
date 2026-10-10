'use strict';
// Heatmap, shared tooltip, quota cards, panel order, alert chips and browser notifications.
function saveDefaults(){storage.set('llmDefaults',JSON.stringify({view:currentView==='settings'?previousView:currentView,...Object.fromEntries(FILTER_IDS.map(id=>[id,$(id).value]))}));}
function renderHeatmap(cells){
 const grid=Array.from({length:7},()=>Array(24).fill(0));let max=0;
 (cells||[]).forEach(c=>{const v=cellMetric(c);grid[c.dow][c.hour]=v;if(v!=null)max=Math.max(max,v);});
 const order=[1,2,3,4,5,6,0],dows={0:'일',1:'월',2:'화',3:'수',4:'목',5:'금',6:'토'};
 let html='<div class="heat-scroll"><div class="heat-grid" role="group" aria-label="요일×시간 사용 패턴 히트맵 · 시간당 최대 '+compact(max)+' '+metricName()+'"><span class="heat-axis"></span>'+Array.from({length:24},(_,h)=>`<span class="heat-axis">${h%3===0?h+'시':''}</span>`).join('');
 for(const d of order){html+=`<span class="heat-axis">${dows[d]}</span>`;
  for(let h=0;h<24;h++){const v=grid[d][h];const level=v?1+Math.min(4,Math.floor(v/max*4)):0;const label=`${dows[d]}요일 ${h}시 · ${v==null?'비용 미산정':metricFmt(v)+metricUnit()}`;html+=`<span class="heat-cell${v==null?' na':' a'+level}" role="img" tabindex="${d===1&&h===0?0:-1}" aria-label="${label}" data-tip="${label}"></span>`;}}
 let wd=0,we=0,peak={d:0,h:0,v:0};
 for(let d=0;d<7;d++)for(let h=0;h<24;h++){const v=grid[d][h];if(v==null)continue;if(d===0||d===6)we+=v;else wd+=v;if(v>peak.v)peak={d,h,v};}
 // Five weekdays against two weekend days: compare per-day averages, not raw shares.
 const days=lastUsage?dayKinds(lastUsage.start,lastUsage.end_exclusive,Date.now()/1000):{weekday:0,weekend:0};
 const perDay=(v,n)=>n?metricFmt($('metric').value==='cost'?v/n:Math.round(v/n))+metricUnit():'—';
 const ratio=days.weekday&&days.weekend&&wd?` (주말이 평일의 ${((we/days.weekend)/(wd/days.weekday)).toFixed(1)}배)`:'';
 const summary=peak.v?`<p class="hint heat-summary">하루 평균 평일 ${perDay(wd,days.weekday)} · 주말 ${perDay(we,days.weekend)}${ratio} · 가장 몰린 시간 ${dows[peak.d]}요일 ${peak.h}시</p>`:'';
 $('heatmap').innerHTML=html+'</div></div>'+(max?`<p class="hint heat-legend">시간당 최대 ${compact(max)} ${metricName()} 기준</p>`:'<p class="hint">선택 기간에 집계할 사용 기록이 없습니다.</p>')+summary;
 $('heatmap').querySelector('.heat-scroll').addEventListener('scroll',()=>{tip.hidden=true;});
 // Arrow keys move between cells (one tab stop for the grid); focus shows the value.
 const heatCells=[...$('heatmap').querySelectorAll('.heat-cell')];
 $('heatmap').querySelector('.heat-grid').onkeydown=event=>{
  const i=heatCells.indexOf(document.activeElement);if(i<0)return;
  const step={ArrowLeft:-1,ArrowRight:1,ArrowUp:-24,ArrowDown:24}[event.key];if(step==null)return;
  const next=heatCells[i+step];if(!next)return;
  event.preventDefault();document.activeElement.tabIndex=-1;next.tabIndex=0;next.focus();
 };
}
// Shared instant tooltip: any element (HTML or SVG) with data-tip shows it on
// hover/touch without the native <title> delay.
const tip=(()=>{const t=document.createElement('div');t.id='tip';t.className='tip';t.hidden=true;document.body.appendChild(t);return t;})();
function showTip(el){
 tip.textContent=el.dataset.tip;tip.hidden=false;
 const box=el.getBoundingClientRect();
 tip.style.left=Math.max(8,Math.min(box.left+box.width/2-tip.offsetWidth/2,innerWidth-tip.offsetWidth-8))+'px';
 const above=box.top-8-tip.offsetHeight>=0;
 tip.style.transform=above?'translateY(-100%)':'none';
 tip.style.top=(above?box.top-8:box.bottom+8)+'px';
}
document.addEventListener('pointerover',event=>{
 const el=event.target.closest?.('[data-tip]');
 if(el)showTip(el);else tip.hidden=true;
});
document.addEventListener('pointerdown',event=>{if(!event.target.closest?.('[data-tip]'))tip.hidden=true;});
// Keyboard focus on a data-tip element shows the same tooltip as hover does.
document.addEventListener('focusin',event=>{const el=event.target.closest?.('[data-tip]');if(el)showTip(el);});
document.addEventListener('focusout',event=>{if(event.target.closest?.('[data-tip]'))tip.hidden=true;});
let selectedQuota=null;
// Routes whose subscription is cancelled: hide the quota card while nothing is
// being received; the first fresh observation makes them visible again.
function briefQuota(rows,route){
 const common=rows.filter(r=>!modelQuota(r));
 if(!common.length)return '공통 한도 미제공';
 const known=common.filter(r=>r.remaining!=null);
 if(!known.length)return '한도 미제공';
 const fresh=known.filter(r=>r.status==='fresh');
 if(!fresh.length)return '최신 한도 수신 대기';
 if(route==='antigravity'){
  const {h5,wk,other}=agyWindows(fresh),list=items=>items.map(r=>bucketName(r)+' '+quotaValue(r)).join(' · ');
  return [h5.length?'5시간: '+list(h5):'',wk.length?'주간: '+list(wk):'',list(other)].filter(Boolean).join('<br>');
 }
 // One line per window: what is left and how long until it comes back.
 return fresh.map(r=>{
  const name=r.bucket.startsWith('seven_day_')?bucketName(r)+' ':/10080|weekly|seven_day/.test(r.bucket)?'주간 ':/300분|five_hour|rolling/.test(r.bucket)?'5h ':/daily/.test(r.bucket)?'일간 ':/monthly/.test(r.bucket)?'월간 ':'';
  const level=r.blocked_by?'level-low':levelOf(r.remaining,lowPercent());
  return `<span class="ov-win ${level}"><b>${esc(name+quotaValue(r))}</b><small>${r.resets?esc(soon(r.seconds_to_reset)):'초기화 미제공'}</small></span>`;
 }).join('');
}
const lowPercent=()=>lastLimits?.low_percent??15;
function renderLimits(data){
 lastLimits=data;
 const opened=new Set([...$('limits').querySelectorAll('details[open]')].map(el=>el.dataset.history));
 function bucket(r,shared,folded=false){
  const lowPct=data.low_percent??15,blocked=r.blocked_by;
  const low=r.status==='fresh'&&r.remaining!=null&&r.remaining<=lowPct;
  const level=r.remaining==null?'':blocked||r.remaining<=lowPct?'level-low':r.remaining<=50?'level-mid':'level-ok';
  // A blocked window cannot be spent, so its own pace, forecast and capacity are not shown.
  const plan=blocked?null:r.plan;
  const forecast=r.forecast&&!blocked?`<p class="forecast${r.forecast.within_window?' warn':''}">최근 ${Math.round(r.forecast.observed_minutes)}분 페이스 ${r.forecast.per_hour.toFixed(1)}%p/시간 유지 시 ${r.forecast.within_window?`<strong>초기화 전 소진 예상</strong> · ${esc(when(r.forecast.depletes_at))} KST`:`초기화까지 약 ${r.forecast.projected_remaining.toFixed(1)}%p 여유 예상`}</p>`:'';
  const win=r.window?`<p class="window-usage">이번 윈도우 사용 <strong>${compact(r.window.tokens)}</strong> 토큰 · ${fmt(r.window.requests)}회 요청${r.window.open?'':' · 종료된 윈도우'}</p>`:'';
  const planAhead=plan?`${plan.ahead>=0?'+':''}${plan.ahead.toFixed(1)}%p ${plan.ahead>=0?'여유':'빠름'}`:'';
  const planElapsed=plan?`창 ${Math.round(plan.elapsed_fraction*100)}% 경과`:'';
  const planText=plan?`계획 대비 <strong${plan.ahead>=0?'':' class="warn-text"'}>${esc(planAhead)}</strong> · ${planElapsed}`:'';
  const capacity=r.capacity&&!blocked?`<p class="capacity">남은 ${percent(r.remaining)} ≈ 약 <strong>${compact(r.capacity.remaining_tokens)}</strong> 토큰 · ${fmt(Math.round(r.capacity.remaining_requests))}회 요청 <small>이번 창 1%p당 ${compact(r.capacity.tokens_per_point)} 토큰 기준 추정</small></p>`:'';
   const hist=(r.capacity_history||[]).filter(h=>h.tokens_per_point);
  const median=hist.length?[...hist].map(h=>h.tokens_per_point).sort((a,b)=>a-b)[Math.floor(hist.length/2)]:null;
  const shrink=hist.length>=3&&hist.at(-1).tokens_per_point<median*0.7;
  const capacityHistory=hist.length?`<p class="capacity-history">지난 창 1%p당 토큰 <small>오래된 순 · 창별 사용 %p</small><br>${hist.map(h=>`<span class="nowrap" data-tip="${esc(when(h.resets))} KST 초기화 창 · ${h.used_points.toFixed(0)}%p · ${fmt(h.tokens)} 토큰">${compact(h.tokens_per_point)}</span>`).join(' · ')}${shrink?'<br><strong class="warn-text">최근 창의 1%p당 토큰이 평소의 70% 미만입니다 · 한도 기준이 바뀌었을 수 있습니다</strong>':''}</p>`:'';
 const events=r.events&&r.events.exhausted?`<p class="quota-events">최근 ${r.events.days}일 소진 ${r.events.exhausted}회 · 마지막 ${esc(when(r.events.last_exhausted))} KST</p>`:'';
  const blockedNote=blocked?`<br><span class="quota-blocked-note">주간 한도 소진 · 주간 초기화(${blocked.resets?when(blocked.resets)+' KST · '+left(blocked.seconds_to_reset):'미제공'}) 후 사용 가능</span>`:'';
  const mark=plan?`<span class="pace-mark" data-x="${Math.max(0,Math.min(100,plan.expected_remaining)).toFixed(1)}" title="고른 속도로 썼다면 지금 남아 있을 잔여 ${plan.expected_remaining.toFixed(1)}%"></span>`:'';
  const key=r.route+':'+r.bucket+':detail';
  const recent=r.paces?.recent,base=r.paces?.baseline;
  const speed=recent?.per_hour;
  const usablePace=r.status==='fresh'&&!blocked&&!r.scope?.identity_unverified;
  const seconds=usablePace&&speed>0&&r.remaining!=null?r.remaining/speed*3600:null;
  const paceText=!usablePace?'속도 판단 보류 · 현재 한도 확인 필요':speed==null?'속도 관측 중 · 연속 15분 이상 필요':speed===0?'최근 30분 감소 관측 없음':`최근 ${Math.round(recent.observed_minutes)}분 · ${speed.toFixed(1)}%p/시간 소모`;
  const horizon=seconds==null?'':r.resets&&seconds<r.resets-data.now?`같은 속도 유지 시 약 ${duration(seconds)}`:'초기화 전 소진 징후 없음';
  const speedChange=r.pace_change&&base?.per_hour!=null?`<small class="pace-change">3시간 비교 ${base.per_hour.toFixed(1)}%p/시간보다 ${r.pace_change==='faster'?'빠르게 사용 중':'느리게 사용 중'}</small>`:'';
  // Derive the plain summary from numeric data; never strip tags from markup.
  const preview=plan?`계획 대비 ${planAhead} · ${planElapsed}`:(r.trends||[]).filter(t=>t.available).map(t=>`${t.window_minutes===60?'1시간':'30분'} −${t.decrease_pp.toFixed(1)}%p`).at(-1)||'';
  return `<div class="bucket ${r.stale?'stale':''}"><div class="limit-head"><span class="bucket-label">${esc(bucketName(r))}</span><span class="badges"><span class="badge ${esc(r.status)}">${esc(statuses[r.status])}</span>${blocked?'<span class="badge low">주간 소진</span>':low?'<span class="badge low">잔여 적음</span>':''}</span></div>`
   +`<div class="bucket-summary"><div class="remaining">${r.remaining==null?'—':Number(r.remaining.toFixed(1))+'%'}<small>${r.status==='fresh'?'잔여':'이전 관측'}${blocked?' · 사용 불가':''}</small></div>`
   +(r.remaining==null?'':`<div class="bar-wrap"><progress class="${level}" max="100" value="${Math.max(0,Math.min(100,r.remaining))}" aria-label="잔여 ${r.remaining}%"></progress>${mark}</div>`)
   +`<p class="bucket-reset">${r.resets?'초기화 '+esc(left(r.seconds_to_reset)):'초기화 정보 미제공'}${blockedNote}</p></div>`
   +(r.note&&!folded?`<p class="quota-note">${esc(r.note)}</p>`:'')
   +`<div class="bucket-pace"><span>${esc(paceText)}</span>${horizon?`<strong>${esc(horizon)}</strong>`:''}${speedChange}</div>`
   +`<details class="bucket-detail" data-history="${esc(key)}"${opened.has(key)?' open':''}><summary>상세${preview?` <small>${esc(preview)}</small>`:''}</summary>`
   +`<p>${r.resets?'초기화 '+esc(when(r.resets))+' KST':''}${shared?'':(r.resets?'<br>':'')+'마지막 확인 '+when(r.checked)+(r.detail?'<br>'+esc(r.detail):'')}</p>`
   +`${planText?`<p class="plan">${planText}</p>`:''}${capacity}${capacityHistory}${events}${forecast}${win}${quotaHistory(r,opened,data.now)}</details></div>`;
 }
 const groups=Object.entries(names).map(([route,name])=>{
  const rows=data.limits.filter(r=>r.route===route);
  if(route==='antigravity')rows.sort((a,b)=>Number(!a.bucket.toLowerCase().includes('gemini'))-Number(!b.bucket.toLowerCase().includes('gemini')));
  return {route,name,rows};
 });
 const savedQuota=orderStore.get().quota;
 if(savedQuota&&savedQuota.length)groups.sort((a,b)=>{const ai=savedQuota.indexOf(a.route),bi=savedQuota.indexOf(b.route);return(ai<0?1e9:ai)-(bi<0?1e9:bi);});
 // Routes with no fresh bucket that ended or stopped reporting fold away.
 const nowTs=data.now||Date.now()/1000,cutoff=(data.quota_hide_days??7)*86400;
 const dormant=g=>!g.rows.some(r=>r.status==='fresh')
  &&(g.rows.some(r=>r.status==='ended')
   ||(Math.max(0,...g.rows.map(r=>r.checked||r.last_attempt||0))&&nowTs-Math.max(0,...g.rows.map(r=>r.checked||r.last_attempt||0))>cutoff));
 const active=groups.filter(g=>!dormant(g)),folded=groups.filter(dormant);
 $('limits').classList.toggle('two-services',active.length===2);
 $('quota-overview').classList.toggle('editing',editingQuota);
 $('quota-overview').innerHTML='<div class="overview-caption"><span>서비스</span><span>'+(editingQuota?'화살표로 순서 조정':'잔여 · 초기화까지')+'</span></div>'+active.map(({route,rows})=>{
  // A route with a current value is current; older buckets beside it are noted, not
  // allowed to mark the whole service as old.
  const common=rows.filter(r=>!modelQuota(r)),anyFresh=common.some(r=>r.status==='fresh');
  const state=common.some(r=>r.status==='error')?'error':anyFresh?'fresh':['stale','unavailable'].find(s=>common.some(r=>r.status===s))||'unavailable';
  const stateLabel=statuses[state]+(state==='fresh'&&common.some(r=>r.status!=='fresh')?' · 일부 오래됨':'');
  const gauge=ring(availability(rows),data.low_percent??15);
  if(editingQuota)return `<div class="quota-edit-row" data-order-route="${route}"><button type="button" data-move="${route}" data-dir="-1" aria-label="위로">↑</button><span><strong>${shortService[route]}</strong><small class="overview-status ${state}">${esc(stateLabel)}</small></span><span class="overview-value">${briefQuota(rows,route)}</span><button type="button" data-move="${route}" data-dir="1" aria-label="아래로">↓</button></div>`;
  return `<button type="button" class="quota-overview-row" data-quota="${route}" aria-controls="quota-${route}" aria-expanded="${selectedQuota===route}"><span class="overview-name">${gauge}<span><strong>${shortService[route]}</strong><small class="overview-status ${state}">${esc(stateLabel)}</small></span></span><span class="overview-value">${briefQuota(rows,route)}</span><span aria-hidden="true">${selectedQuota===route?'−':'＋'}</span></button>`;
 }).join('');
 // The window most likely to stop work (blocked, forecast to deplete, then lowest expected
 // at reset) and the order in which services are worth using now, from current values only.
 // The work decision above uses the selected model and complete constraints.
 $('quota-strip').hidden=true;$('quota-strip').replaceChildren();
 // An exhausted quota's reset is what the user waits for, so it is never cut off;
 // a window blocked by its exhausted parent is left out because its reset frees nothing.
 const validUpcoming=data.limits.filter(r=>r.resets&&r.resets>nowTs&&!modelQuota(r)&&!r.blocked_by);
 const exhausted=r=>r.remaining!=null&&r.remaining<=0;
 const exhaustedUpcoming=validUpcoming.filter(exhausted);
 const regularUpcoming=validUpcoming.filter(r=>!exhausted(r)).sort((a,b)=>a.resets-b.resets);
 const upcoming=[...exhaustedUpcoming,...regularUpcoming.slice(0,Math.max(0,10-exhaustedUpcoming.length))].sort((a,b)=>a.resets-b.resets);
 // Windows of one service resetting together (Devin 일간·주간) are one line.
 const merged=[];
 for(const r of upcoming){
  const last=merged.at(-1);
  if(last&&last.route===r.route&&Math.abs(last.resets-r.resets)<60&&exhausted(last.items[0])===exhausted(r))last.items.push(r);
  else merged.push({route:r.route,resets:r.resets,seconds:r.seconds_to_reset,items:[r]});
 }
 const service=route=>shortService[route]||route;
 // A 7-day axis places each reset; later ones sit at the right edge.
 const axis=merged.length?`<div class="reset-axis" role="img" aria-label="앞으로 7일 초기화 시각">${[0,1,2,3,4,5,6,7].map(d=>`<i class="reset-tick" data-x="${(d/7*100).toFixed(2)}"><small>${d?d+'일':'지금'}</small></i>`).join('')}${merged.map(m=>`<b class="reset-dot${exhausted(m.items[0])?' low':''}" data-x="${Math.min(100,m.seconds/(7*86400)*100).toFixed(2)}" tabindex="0" data-tip="${esc((m.items.length===1?quotaLabel(m.items[0]):service(m.route)+' '+m.items.map(r=>bucketName(r)).join(' · '))+' · '+left(m.seconds)+' · '+when(m.resets)+' KST')}"></b>`).join('')}</div>`:'';
 $('reset-timeline').innerHTML=merged.length?`<h3>다음 초기화</h3>${axis}<div class="reset-list">${merged.map(m=>{
  const first=m.items[0];
  const label=m.items.length===1?quotaLabel(first):`${service(m.route)} ${m.items.map(r=>quotaLabel(r).replace(service(m.route)+' ','')).join(' · ')}`;
  // Only a current observation says the quota is exhausted now.
  const tag=exhausted(first)?` <span class="badge low">${first.status==='fresh'?'소진 중':'이전 관측 소진'}</span>`:'';
  return `<div class="reset-item${exhausted(first)?' low':''}"><strong>${esc(label)}${tag}</strong><span><b>${esc(soon(m.seconds))}</b> · <time datetime="${new Date(m.resets*1000).toISOString()}">${esc(when(m.resets))}</time></span></div>`;
 }).join('')}</div>`:'';
 applyGeometry($('reset-timeline'));
 const cardHtml=({route,name,rows})=>{
  const primary=rows.filter(r=>!modelQuota(r)),secondary=rows.filter(modelQuota),key=route+':secondary';
  const checks=[...new Set(rows.map(r=>r.checked??null))],details=[...new Set(rows.map(r=>r.detail).filter(Boolean))];
  const shared=checks.length<=1&&details.length<=1;
  const meta=shared&&(checks[0]!=null||details.length)?`<p class="card-meta">${checks[0]!=null?'마지막 확인 '+when(checks[0]):''}${details[0]?(checks[0]!=null?' · ':'')+esc(details[0]):''}</p>`:'';
  // Beside a current value, older observations fold into one line so cards stay short.
  const current=primary.some(r=>r.status==='fresh')?primary.filter(r=>r.status==='fresh'||r.remaining==null):primary;
  const older=primary.filter(r=>!current.includes(r)),olderKey=route+':older';
  let content=current.map(r=>bucket(r,shared)).join('');
  if(route==='antigravity'){
   const {h5,wk,other}=agyWindows(current);
   content=[['5시간',h5],['주간',wk],['',other]].filter(([,items])=>items.length)
    .map(([title,items])=>`<div class="quota-window-group">${title?`<h4 class="quota-window-title">${title}</h4>`:''}${items.map(r=>bucket(r,shared)).join('')}</div>`).join('<hr class="quota-divider">');
  }
  if(older.length)content+=`<details class="quota-older" data-history="${esc(olderKey)}"${opened.has(olderKey)?' open':''}><summary>이전 관측 ${older.length}개 <small>${older.map(r=>esc(quotaLabel(r).replace((shortService[route]||route)+' ',''))+' '+(r.remaining==null?'—':percent(r.remaining))+' · '+esc(statuses[r.status]||r.status)).join(' · ')}</small></summary>${older.map(r=>bucket(r,shared,true)).join('')}</details>`
   // Why a window is not current stays readable without opening the fold.
   +[...new Set(older.map(r=>r.note).filter(Boolean))].map(note=>`<p class="quota-note">${esc(note)}</p>`).join('');
  return `<article id="quota-${route}" class="limit-card${selectedQuota===route?' mobile-selected':''}"><div class="limit-head"><strong>${esc(name)}</strong><button type="button" class="quota-close" data-close-quota="${route}">접기</button></div>${meta}${content}${secondary.length?`<details class="quota-secondary" data-history="${key}"${opened.has(key)?' open':''}><summary>모델별 추가 한도 <small>${secondary.filter(r=>r.bucket.includes('300분')).map(r=>'5시간 '+(r.status==='fresh'?percent(r.remaining):'수신 대기')).join(' · ')}</small></summary>${secondary.map(r=>bucket(r,shared)).join('')}</details>`:''}</article>`;
 };
 $('limits').innerHTML=active.map(cardHtml).join('')
  +(folded.length?`<details class="quota-dormant"><summary>관측 중단 ${folded.length}개<small>${folded.map(g=>esc(g.name)).join(' · ')}</small></summary>${folded.map(cardHtml).join('')}</details>`:'');
 fitSvgText($('limits'));applyGeometry($('limits'));
}
$('quota-overview').addEventListener('click',event=>{
 const move=event.target.closest('[data-move]');
 if(move){moveQuota(move.dataset.move,Number(move.dataset.dir));return;}
 const button=event.target.closest('[data-quota]');if(!button)return;
 selectedQuota=selectedQuota===button.dataset.quota?null:button.dataset.quota;syncQuotaSelection();
 if(selectedQuota)document.getElementById('quota-'+selectedQuota)?.scrollIntoView({behavior:motion(),block:'nearest'});
});
$('limits').addEventListener('click',event=>{if(event.target.closest('[data-close-quota]')){selectedQuota=null;syncQuotaSelection();}});
function syncQuotaSelection(){
 $('quota-overview').querySelectorAll('[data-quota]').forEach(button=>{const active=button.dataset.quota===selectedQuota;button.setAttribute('aria-expanded',active);button.lastElementChild.textContent=active?'−':'＋';});
 $('limits').querySelectorAll('.limit-card').forEach(card=>card.classList.toggle('mobile-selected',card.id==='quota-'+selectedQuota));
 // On narrow screens only the selected card is visible; fit its chart text now.
 if(selectedQuota)fitSvgText(document.getElementById('quota-'+selectedQuota));
}
const orderStore={
 get(){try{return JSON.parse(storage.get('llmOrder'))||{}}catch{return{}}},
 set(scope,order){const all=orderStore.get();all[scope]=order;storage.set('llmOrder',JSON.stringify(all));},
};
function moveQuota(route,dir){
 const order=[...$('quota-overview').querySelectorAll('[data-order-route]')].map(el=>el.dataset.orderRoute);
 const i=order.indexOf(route),j=i+dir;if(i<0||j<0||j>=order.length)return;
 [order[i],order[j]]=[order[j],order[i]];orderStore.set('quota',order);
 if(lastLimits)renderLimits(lastLimits);
}
const panelSections=()=>[...document.querySelectorAll('main>[data-view]')].filter(s=>s.querySelector(':scope>[data-panel]'));
const panelEls=section=>[...section.querySelectorAll(':scope>[data-panel]')];
// The saved list spans every section (and ids from earlier layouts); each section only
// follows the ids it owns, so a panel never leaves its own tab.
function applyPanelOrder(){
 const saved=orderStore.get().panels;if(!Array.isArray(saved)||!saved.length)return;
 panelSections().forEach(section=>{
  const els=panelEls(section),own=saved.filter(id=>els.some(el=>el.dataset.panel===id));
  els.sort((a,b)=>{const ai=own.indexOf(a.dataset.panel),bi=own.indexOf(b.dataset.panel);return(ai<0?1e9:ai)-(bi<0?1e9:bi);})
   .forEach(el=>section.appendChild(el));
 });
}
function movePanel(id,dir){
 const el=document.querySelector(`[data-panel="${id}"]`);if(!el)return;
 const els=panelEls(el.parentElement),i=els.indexOf(el),j=i+dir;
 if(i<0||j<0||j>=els.length)return;
 el.parentElement.insertBefore(dir<0?els[i]:els[j],dir<0?els[j]:els[i]);
 orderStore.set('panels',[...document.querySelectorAll('main [data-panel]')].map(e=>e.dataset.panel));
}
function syncPanelEditing(){
 panelSections().forEach(section=>panelEls(section).forEach(el=>{
  el.querySelectorAll('.ord').forEach(node=>node.remove());
  if(!editingPanels)return;
  const s=document.createElement('span');s.className='ord';
  s.innerHTML=`<button type="button" data-move-panel="${el.dataset.panel}" data-dir="-1" aria-label="위로">↑</button><button type="button" data-move-panel="${el.dataset.panel}" data-dir="1" aria-label="아래로">↓</button>`;
  const host=el.querySelector(':scope>summary')||el.querySelector(':scope>.section-heading');
  if(host)host.appendChild(s);else el.prepend(s);
 }));
}
document.querySelectorAll('.activity').forEach(section=>section.addEventListener('click',event=>{
 const button=event.target.closest('[data-move-panel]');if(!button)return;
 event.preventDefault();event.stopPropagation();
 movePanel(button.dataset.movePanel,Number(button.dataset.dir));
}));
document.querySelectorAll('.order-toggle').forEach(button=>button.addEventListener('click',()=>{
 let on;
 if(button.dataset.orderScope==='quota'){on=editingQuota=!editingQuota;if(lastLimits)renderLimits(lastLimits);}
 else{on=editingPanels=!editingPanels;syncPanelEditing();}
 document.querySelectorAll(`[data-order-scope="${button.dataset.orderScope}"]`).forEach(b=>{b.classList.toggle('on',on);b.textContent=on?'완료':'순서';});
}));
// Collapse state survives re-renders on each refresh.
let alertsExpanded=false;
function renderAlerts(limits,usage){
 const items=[];
 (limits.limits||[]).forEach(r=>{
  if(limits.planning&&(r.route!==limits.planning.route||!limits.planning.applies?.includes(r.bucket)))return;
  const label=quotaLabel(r);
  if(r.status==='fresh'&&r.remaining!=null&&r.remaining<=(limits.low_percent??15))items.push({text:`${label} 잔여 ${percent(r.remaining)}`,route:r.route});
  else if(r.forecast&&r.forecast.within_window&&!r.blocked_by)items.push({text:`${label} 초기화 전 소진 예상`,route:r.route});
 });
 for(const [project,budget] of Object.entries(usage.project_budgets||{})){
  if(!usage.project_month)break;
  const st=budgetState(usage.project_month.projects[project],budget);
  if(st.ratio>=.8)items.push({text:`${project} 월 예산 ${Math.round(st.ratio*100)}%${st.ratio>=1?' 초과':''}`,view:'usage'});
 }
 // A burst far above this account's usual hours, and models whose cost cannot be estimated.
 if(usage.spike)items.push({text:`사용량 급증 · ${when(usage.spike.hour_start)}부터 1시간 ${compact(usage.spike.tokens)} 토큰 (평소 상위 5%의 ${usage.spike.ratio.toFixed(1)}배)`,view:'usage'});
 if((usage.unpriced_models||[]).length)items.push({text:`단가 미등록 모델 ${usage.unpriced_models.length}개 · ${usage.unpriced_models.slice(0,3).join(', ')}${usage.unpriced_models.length>3?' 외':''}`,view:'settings'});
 // Only states the owner can act on become chips: an 'unavailable' source (an app that is
 // closed, a status line not in use, a path that does not exist) stays in 수집 상태.
 (usage.sources||[]).filter(s=>s.status==='error').forEach(s=>items.push({text:`${s.name} ${statuses[s.status]||s.status}`,sources:true}));
 (usage.sources||[]).filter(s=>['수집기','일일 백업','원본 대조'].includes(s.name)&&(s.status==='stale'||s.status==='error'))
  .forEach(s=>items.push({text:`${s.name} ${statuses[s.status]||s.status}`,sources:true}));
 const activeStale=s=>s.status==='stale'&&(s.name.endsWith('-records')||s.name.endsWith('-oauth')||s.name.startsWith('WSL ')||s.name.startsWith('Windows '));
 (usage.sources||[]).filter(activeStale).forEach(s=>items.push({text:`${s.name} 수집 지연`,sources:true}));
 // A stopped collector freezes every number on the page, so it is stated above all.
 const collector=(usage.sources||[]).find(s=>s.name==='수집기');
 const banner=$('collector-banner');
 const down=collector&&collector.status!=='ok';
 banner.hidden=!down;
 if(down)banner.textContent=collector.checked?`수집기가 ${Math.max(1,Math.round((Date.now()/1000-collector.checked)/60))}분째 응답하지 않습니다. 표시된 값은 그 이후 갱신되지 않았습니다.`:'수집기 실행 기록이 없습니다. 표시된 값이 갱신되지 않습니다.';
 const el=$('alerts');
 el.hidden=!items.length;
 el.classList.toggle('collapsible',items.length>=2);
 // Two or more alerts collapse behind one toggle on narrow screens; the chips
 // keep their data-alert indices so the toggle must not be a chip itself.
 el.classList.toggle('expanded',alertsExpanded);
 el.innerHTML=(items.length>=2?`<button type="button" id="alerts-toggle" aria-expanded="${alertsExpanded}">⚠ 알림 ${items.length}개 · ${esc(items[0].text)}</button>`:'')
  +items.map((it,i)=>`<button type="button" class="alert-chip" data-alert="${i}">${esc(it.text)}</button>`).join('');
 const toggle=el.querySelector('#alerts-toggle');
 if(toggle)toggle.onclick=()=>{alertsExpanded=!alertsExpanded;el.classList.toggle('expanded',alertsExpanded);toggle.setAttribute('aria-expanded',String(alertsExpanded));};
 el.querySelectorAll('[data-alert]').forEach(button=>button.onclick=()=>{
  const it=items[Number(button.dataset.alert)];
  if(it.route){
   setView('quota');selectedQuota=it.route;syncQuotaSelection();
   document.getElementById('quota-'+it.route)?.scrollIntoView({behavior:motion(),block:'nearest'});
  }else if(it.view){setView(it.view);if(it.view==='settings'){priceFilter.unset=true;$('price-unset').setAttribute('aria-pressed','true');$('price-unset').classList.add('on');}}
  else{setView('status');$('sources-panel').scrollIntoView({behavior:motion()});}
 });
}
const notifyEnabled=()=>'Notification' in window&&Notification.permission==='granted'&&storage.get('llmNotify')==='1';
const ntfEnabled=kind=>notifyEnabled()&&storage.get('llmNotify:'+kind)!=='0';
let notificationWorker=null;
function notificationRegistration(){
 if(!notificationWorker)notificationWorker=(async()=>{
  const registration=await navigator.serviceWorker.register('/sw.js');
  const worker=registration.installing||registration.waiting||registration.active;
  if(!worker)throw Error('알림 준비 실패');
  if(worker.state!=='activated')await new Promise((resolve,reject)=>{
   const timer=setTimeout(()=>finish(Error('알림 준비 시간 초과')),10000);
   function finish(error){clearTimeout(timer);worker.removeEventListener('statechange',changed);error?reject(error):resolve();}
   function changed(){if(worker.state==='activated')finish();else if(worker.state==='redundant')finish(Error('알림 준비 실패'));}
   worker.addEventListener('statechange',changed);changed();
  });
  return registration;
 })().catch(error=>{notificationWorker=null;throw error;});
 return notificationWorker;
}
function notificationStatus(failed){
 $('notify-status').hidden=!failed;
}
async function sendNotification(title,options){
 try{
  if('serviceWorker' in navigator){
   const registration=await notificationRegistration();
   if(!notifyEnabled())return;
   await registration.showNotification(title,options);
  }else new Notification(title,options);
  notificationStatus(false);
 }catch(error){notificationStatus(true);}
}
let opsBad=new Set();
function checkOpsNotify(usage){
 const bad=new Set(((usage&&usage.sources)||[]).filter(s=>['수집기','일일 백업','원본 대조'].includes(s.name)&&(s.status==='stale'||s.status==='error'||s.status==='unavailable')).map(s=>s.name));
 for(const name of bad)if(!opsBad.has(name)&&ntfEnabled('ops'))void sendNotification('수집 상태 경고',{body:name+' 확인이 필요합니다',tag:'ops-'+name});
 opsBad=bad;
}
const notifySent=new Set();
function checkNotify(limits){
 const first=lastLimits===null;
 const prev=new Map(((lastLimits&&lastLimits.limits)||[]).map(r=>[r.route+':'+r.bucket,r]));
 const lowPct=limits.low_percent??15;
 (limits.limits||[]).forEach(r=>{
  const key=r.route+':'+r.bucket,label=quotaLabel(r),before=prev.get(key);
  if(!before||before.account_epoch!==r.account_epoch)return;
  const fresh=r.status==='fresh'&&r.remaining!=null&&!r.scope?.identity_unverified;
  // One notification per kind and reset anchor; a toggling forecast or the
  // reset boundary itself must not page the owner twice for the same window.
  const once=kind=>{const k=`${key}-${r.account_epoch||'legacy'}-${kind}-${Math.round((r.resets||0)/60)}`;
   if(notifySent.has(k))return false;notifySent.add(k);return true;};
  const low=fresh&&r.remaining<=lowPct,wasLow=!!(before&&before.remaining!=null&&before.remaining<=lowPct);
  if(!first&&low&&!wasLow&&ntfEnabled('low')&&once('low'))void sendNotification('한도 잔여 적음',{body:`${label} 잔여 ${percent(r.remaining)}`,tag:key+'-low'});
  if(!first&&wasLow&&!low&&fresh&&r.resets&&r.resets!==before.resets&&ntfEnabled('reset')&&once('reset'))void sendNotification('한도 회복 확인',{body:`${label} 잔여 ${percent(r.remaining)}로 회복`,tag:key+'-reset'});
  const dep=!!(fresh&&r.forecast&&r.forecast.within_window),wasDep=!!(before&&before.forecast&&before.forecast.within_window);
  if(!first&&dep&&!wasDep&&ntfEnabled('dep')&&once('dep'))void sendNotification('한도 소진 예상',{body:`${label} 현재 페이스로 초기화 전 소진 예상`,tag:key+'-dep'});
 });
}
