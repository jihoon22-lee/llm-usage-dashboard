'use strict';
// 개요: what each subscription can be used for now, today, this week and the last 30 days
// against the period before, and observed facts. Period figures come from /api/overview, which
// reads the same totals as the usage tab's cards; quota rows read the last /api/limits view.
let lastOverview=null,overviewLoaded=0,overviewPending=null,overviewWidth=0;
function loadOverview(force=false){
 if(overviewPending)return overviewPending;
 if(!force&&lastOverview&&Date.now()-overviewLoaded<refreshMs)return null;
 if(!lastOverview)overviewStatus('개요를 불러오는 중…');
 const source={};
 overviewPending=api('/api/overview',undefined,undefined,{source}).then(data=>{
  lastOverview=data;overviewLoaded=Date.now();renderOverview(data);showSource('overview',source);
 }).catch(error=>{
  // A failed refresh keeps the figures already shown; only an empty view says so.
  if(!lastOverview)overviewStatus('개요를 불러오지 못했습니다. '+error.message,true);
 }).finally(()=>{overviewPending=null;});
 return overviewPending;
}
function overviewStatus(text,failed=false){
 const el=$('ov-status');el.hidden=!text;el.setAttribute('role',failed?'alert':'status');
 $('ov-status-text').textContent=text;$('ov-retry').hidden=!failed;
}
$('ov-retry').addEventListener('click',()=>loadOverview(true));
const dayLabel=d=>`${Number(d.slice(5,7))}월 ${Number(d.slice(8,10))}일`;
const timesText=v=>v>=10?v.toFixed(0):v.toFixed(1);
const serviceColor=route=>colors[ROUTE_SLOTS[familyOf(route)]]??mutedColor;
// Usage by service covers all of its accounts, so it carries no account name.
const serviceName=route=>shortService[familyOf(route)]||routeNames[route]||route;

// What can be used now: per service (each Claude account, each Antigravity model family) the
// tightest current window. An exhausted window makes the service unusable until it resets.
function overviewQuotaUnits(data){
 const units=[];
 for(const g of serviceGroups(data).active){
  const rows=g.rows.filter(r=>!modelQuota(r)&&!r.retired);
  if(g.route==='claude-code'&&multiClaude()){
   for(const [account,list] of accountGroups(rows))if(!accountEnded(list))units.push({route:g.route,name:serviceShort(account),rows:list});
  }else if(g.route==='antigravity'){
   const families=new Map();
   for(const r of rows){const family=agyBucket(r.bucket)?.family||'';(families.get(family)||families.set(family,[]).get(family)).push(r);}
   if(families.size<=1)units.push({route:g.route,name:shortService[g.route],rows});
   else for(const [family,list] of families)units.push({route:g.route,name:`Antigravity ${family}`.trim(),rows:list});
  }else units.push({route:g.route,name:shortService[g.route],rows});
 }
 return units;
}
const windowName=r=>r.route==='antigravity'&&agyBucket(r.bucket)?agyBucket(r.bucket).window:bucketName(r);
function overviewQuotaRow(unit,low){
 const fresh=unit.rows.filter(r=>r.status==='fresh'&&r.remaining!=null);
 // The windows that are themselves at 0% (not merely held by one): usable again only when
 // the last of them resets.
 const empty=fresh.filter(r=>r.remaining<=0&&!r.blocked_by);
 const best=availability(unit.rows);
 let value,level='',fill=null,caption,reset;
 if(empty.length||best?.blocked_by){
  const root=empty.length?empty.reduce((a,b)=>(b.seconds_to_reset??0)>(a.seconds_to_reset??0)?b:a)
   :{...best.blocked_by,route:best.route,bucket:best.blocked_by.bucket};
  value='사용 불가';level='level-low';fill=0;caption=windowName(root)+' 한도 소진';
  reset=root.resets?soon(root.seconds_to_reset)+' 후 초기화':'초기화 미제공';
 }else if(best){
  value=Number(best.remaining.toFixed(1))+'%';level=levelOf(best.remaining,low);fill=Math.max(0,Math.min(100,best.remaining));
  caption=windowName(best)+' 한도';reset=best.resets?soon(best.seconds_to_reset)+' 후 초기화':'초기화 미제공';
 }else{
  // Nothing current: an older reading is shown as such, never as the value now.
  const old=unit.rows.filter(r=>r.remaining!=null&&!modelQuota(r)).sort((a,b)=>(b.checked||0)-(a.checked||0))[0];
  value=old?'이전 '+Number(old.remaining.toFixed(1))+'%':'미제공';level='muted';
  caption=old?statuses[old.status]||old.status:briefQuota(unit.rows,unit.route).replace(/<[^>]+>/g,' ');
  reset=old?'확인 '+when(old.checked):'';
 }
 return `<button type="button" class="ov-q" data-quota-jump="${esc(unit.route)}" data-ov-key="${esc(unit.route+'|'+unit.name)}"><span class="ov-q-name"><strong>${esc(unit.name)}</strong><small>${esc(caption)}</small></span>`
  +`<span class="ov-q-value ${level}">${esc(value)}</span>`
  +(fill==null?'<span class="ov-q-meter ov-q-blank" aria-hidden="true"></span>':`<span class="ov-q-meter" aria-hidden="true"><i class="${level}" data-w="${fill.toFixed(1)}"></i></span>`)
  +`<span class="ov-q-reset">${esc(reset)}</span></button>`;
}
function renderOverviewQuota(){
 const el=$('ov-quota');
 if(!lastLimits){el.innerHTML='<p class="hint">한도를 불러오는 중…</p>';return;}
 const units=overviewQuotaUnits(lastLimits);
 el.innerHTML=units.length?units.map(u=>overviewQuotaRow(u,lastLimits.low_percent??15)).join(''):'<p class="hint">표시할 구독 한도가 없습니다.</p>';
 applyGeometry(el);
}

// Bars of a series of points; points not reached yet (null) stay empty.
function sparkBars(points){
 const max=Math.max(0,...points.map(v=>v||0));
 if(!max)return '';
 const w=100/points.length;
 return `<svg class="ov-spark" viewBox="0 0 100 24" preserveAspectRatio="none" aria-hidden="true">${points.map((v,i)=>{
  if(!v)return '';const h=Math.max(1,v/max*24);
  return `<rect x="${(i*w+w*.15).toFixed(2)}" y="${(24-h).toFixed(2)}" width="${(w*.7).toFixed(2)}" height="${h.toFixed(2)}"/>`;}).join('')}</svg>`;
}
function overviewTiles(o){
 const delta=(p,period,label)=>{
  const text=priorChange(p.prior,period,p.prior?.current_tokens??p.tokens,p.prior?p.prior.tokens:null,label);
  return text?`<small class="stat-delta">${esc(text)}</small>`:'';
 };
 const cost=p=>p.cost!=null?`<small>공개 단가 환산 ${usd(p.cost)}${p.cost_coverage!=null&&p.cost_coverage<99.5?' · 일부 미산정':''}</small>`:'';
 const tile=(attrs,label,value,body)=>`<button type="button" class="stat ov-tile" ${attrs}><span class="stat-label">${label}</span><span class="stat-value">${value}</span>${body}</button>`;
 const r=o.recent,m=o.month,week=o.week;
 const weekEnd=new Date(Date.parse(week.start)+6*86400000+9*3600000).toISOString().slice(0,10);
 return tile('data-drill-period="today" data-granularity="hour"','오늘',compact(o.today.tokens),delta(o.today,'today')+cost(o.today)+sparkBars(o.today.points))
  +tile(`data-drill-period="custom" data-start="${esc(week.start.slice(0,10))}" data-end="${esc(weekEnd)}"`,'이번 주',compact(week.tokens),delta(week,'custom','지난주')+cost(week)+sparkBars(week.points))
  +tile('data-drill-period="30d"','최근 30일',compact(r.tokens),`<small>하루 평균 ${compact(r.tokens/Math.max(1,r.days.length))}</small><small>${r.days.length}일 중 ${r.active_days}일 사용</small>`)
  +(m?tile('data-goto="reports" data-anchor="monthly"',`${Number(m.month.slice(5))}월 구독 가치`,m.value!=null?usd(m.value):'미산정',
     (m.ratio!=null?`<small class="stat-delta">구독료 ${usd(m.price)}의 ${timesText(m.ratio)}배</small>`:'')+`<small>공개 API 단가 환산 · 실제 청구 아님${m.partial?' · 기록 시작 이후':''}</small>`)
   :tile('data-goto="reports" data-anchor="monthly"','구독 가치','—','<small>정산된 달이 아직 없습니다</small>'));
}

// Observed facts only: no forecast, no expected pace and no advice to switch accounts.
const FACT_KINDS={exhausted:'한도',zero_hours:'한도',service_change:'사용',peak_day:'사용',peak_hours:'사용',idle_subscription:'구독',value_below_price:'구독'};
// A Claude account goes by the name it has on this page now, as in the quota rows above; the
// plan label stored with an older week or month is only a fallback.
const accountName=(route,stored)=>accountLabel(route)||stored||undefined;
// A week's facts name their week here; under a report's dates they go without it.
function factText(f,withWeek=true){
 const quota=()=>quotaLabel({route:f.route,bucket:f.bucket,display_name:f.display_name,account_label:accountName(f.route,f.account_label)});
 const week=withWeek&&f.week?weekName(f.week)+' ':'';
 switch(f.kind){
  case 'exhausted':return `${week}${quota()} 한도 ${f.count}회 소진${f.streak>1?` · ${f.streak}주 연속`:''}`;
  case 'zero_hours':return `${week}${quota()} 한도가 0%로 머문 시간 ${hoursText(f.hours)}`;
  case 'service_change':{
   const ratio=1+f.change_pct/100;
   // A service's change covers all of its accounts.
   return `${week}${serviceName(f.route)} 사용량 ${ratio>=2?`그 전 주의 ${timesText(ratio)}배`:`그 전 주보다 ${f.change_pct>0?'+':'−'}${Math.abs(f.change_pct).toFixed(0)}%`}`;
  }
  case 'peak_day':return `최근 30일 중 가장 많이 쓴 날 ${dayLabel(f.day)} · ${compact(f.tokens)} (사용한 날 평균의 ${timesText(f.ratio)}배)`;
  case 'peak_hours':return `사용이 가장 많은 시간대 ${f.start_hour}–${f.end_hour}시 · 최근 30일의 ${f.share.toFixed(0)}%`;
  case 'idle_subscription':return `${serviceShort(f.route)} 최근 30일 ${f.collected?'사용 기록 없음':'수집된 기록 없음'} · 월 ${usd(f.monthly_usd)}`;
  case 'value_below_price':return `${Number(f.month.slice(5))}월 ${serviceShort(f.route,accountName(f.route,f.label))} 공개 단가 환산 ${usd(f.value)} · 구독료 ${usd(f.monthly_usd)}의 ${f.ratio.toFixed(1)}배`;
 }
 return '';
}
const factTarget=f=>f.kind==='peak_day'?`data-day="${esc(f.day)}"`:f.kind==='peak_hours'?'data-drill-period="30d"'
 :f.kind==='idle_subscription'?`data-quota-jump="${esc(familyOf(f.route))}"`:f.kind==='value_below_price'?'data-goto="reports" data-anchor="monthly"':'data-goto="reports"';
function overviewFacts(facts){
 const items=(facts||[]).map(f=>[f,factText(f)]).filter(([,text])=>text);
 return items.length?items.map(([f,text])=>`<li><button type="button" class="ov-fact" ${factTarget(f)}><span class="ov-fact-kind">${FACT_KINDS[f.kind]}</span><span>${esc(text)}</span></button></li>`).join('')
  :'<li class="hint">아직 짚을 만한 관측이 없습니다.</li>';
}

// The last 30 days by service, stacked; a day's bar opens that day in the usage tab.
function overviewChart(recent){
 const box=$('ov-chart'),width=box.clientWidth;
 if(!width)return;
 overviewWidth=width;
 const services=(recent.services||[]).slice().sort((a,b)=>(ROUTE_SLOTS[familyOf(a.route)]??99)-(ROUTE_SLOTS[familyOf(b.route)]??99)||b.tokens-a.tokens);
 const days=recent.days,totals=days.map((_,i)=>services.reduce((s,x)=>s+x.values[i],0)),peak=Math.max(0,...totals);
 $('ov-legend').innerHTML=services.map(s=>`<span class="ov-key"><svg width="10" height="10" aria-hidden="true"><rect width="10" height="10" rx="2" fill="${serviceColor(s.route)}"/></svg>${esc(serviceName(s.route))} <b>${compact(s.tokens)}</b></span>`).join('');
 $('ov-chart-caption').textContent=recent.unallocated_tokens?`날짜 미배분 ${compact(recent.unallocated_tokens)} · 합계에만 포함`:'';
 $('ov-chart-caption').dataset.tip=recent.unallocated_tokens?'계정 일 단위 누계처럼 하루를 정할 수 없는 사용량은 막대에 나누지 않습니다.':'';
 if(!peak){box.innerHTML='<p class="hint">최근 30일에 날짜별로 나눌 수 있는 사용 기록이 없습니다.</p>';return;}
 const tick=niceStep(peak),top=Math.max(tick,Math.ceil(peak/tick)*tick);
 const H=190,L=46,R=6,T=10,B=24,plot=width-L-R,h=H-T-B,step=plot/days.length,bar=Math.max(2,step*.68);
 const y=v=>T+h-v/top*h;
 let svg=`<svg class="ov-bars" width="${width}" height="${H}" viewBox="0 0 ${width} ${H}" role="img" aria-label="최근 30일 서비스별 사용량 · 합계 ${compact(recent.tokens)}">`;
 for(let v=0;v<=top;v+=tick)svg+=`<line x1="${L}" x2="${width-R}" y1="${y(v).toFixed(1)}" y2="${y(v).toFixed(1)}"/><text x="${L-6}" y="${(y(v)+4).toFixed(1)}" text-anchor="end">${compact(v)}</text>`;
 days.forEach((day,i)=>{
  svg+=stackRects(L+i*step+(step-bar)/2,bar,y,services.map(s=>[serviceColor(s.route),s.values[i]]));
  const parts=services.filter(s=>s.values[i]).map(s=>`${serviceName(s.route)} ${compact(s.values[i])}`);
  svg+=`<rect class="ov-hit" x="${(L+i*step).toFixed(1)}" y="${T}" width="${step.toFixed(1)}" height="${h}" data-day="${esc(day)}" data-tip="${esc([dayLabel(day)+' 합계 '+compact(totals[i]),...parts].join(' · '))}"/>`;
  // Weekly ticks counted back from today, so the last day is always named.
  if((days.length-1-i)%7===0)svg+=`<text x="${(L+i*step+step/2).toFixed(1)}" y="${H-7}" text-anchor="middle">${Number(day.slice(5,7))}/${Number(day.slice(8,10))}</text>`;
 });
 box.innerHTML=svg+'</svg>';
}
new ResizeObserver(()=>{if(lastOverview&&$('ov-chart').clientWidth&&$('ov-chart').clientWidth!==overviewWidth)overviewChart(lastOverview.recent);}).observe($('ov-chart'));

// Top models and projects of the same 30 days; a row opens its scope over those days.
function overviewRanks(list,kind){
 if(!list.length)return '<p class="hint">최근 30일 기록이 없습니다.</p>';
 const max=list[0].tokens;
 return list.map(r=>{
  const name=kind==='model'?esc(r.name)+` <small>${esc(serviceName(r.route))}</small>`:esc(r.name);
  const share=r.share!=null?percent(r.share):'—';
  return `<div class="rank" data-scope="${kind}:${esc(r.name)}" data-period="30d" tabindex="0" aria-describedby="drill-hint"><div class="rank-label"><strong>${name}</strong><span>${compact(r.tokens)} <small>${share}</small></span></div>`
   +`<svg class="rank-bar ov-rank-${kind}" viewBox="0 0 1000 13" preserveAspectRatio="none" aria-hidden="true"><rect width="${(r.tokens/max*1000).toFixed(1)}" height="13"${kind==='model'?` fill="${serviceColor(r.route)}"`:''}/></svg></div>`;
 }).join('');
}

function renderOverview(data){
 renderOverviewQuota();
 if(!data)return;
 overviewStatus('');
 $('ov-tiles').innerHTML=overviewTiles(data);
 $('ov-facts').innerHTML=overviewFacts(data.facts);
 overviewChart(data.recent);
 $('ov-models').innerHTML=overviewRanks(data.models||[],'model');
 $('ov-projects').innerHTML=overviewRanks(data.projects||[],'project');
}
$('view-overview').addEventListener('click',event=>{
 const jump=event.target.closest('[data-quota-jump]');
 if(jump){
  setView('quota');selectedQuota=jump.dataset.quotaJump;syncQuotaSelection();
  document.getElementById('quota-'+selectedQuota)?.scrollIntoView({behavior:motion(),block:'nearest'});return;
 }
 const go=event.target.closest('[data-goto]');
 if(go){setView(go.dataset.goto);if(go.dataset.anchor)$(go.dataset.anchor)?.scrollIntoView({behavior:motion(),block:'start'});return;}
 // Rows with a scope and days with a date are opened by the page-wide drill handlers.
 const drill=event.target.closest('[data-drill-period]');
 if(drill)applyDrill({period:drill.dataset.drillPeriod,granularity:drill.dataset.granularity||'day',
  ...(drill.dataset.start?{start:drill.dataset.start,end:drill.dataset.end}:{})});
});
