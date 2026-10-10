'use strict';
// Decisions and resource observations arrive together in one limits snapshot.
const PLAN_DEFAULTS={route:'codex',model:'common',hours:2,pace:'recent',today_hours:2,week_hours:10};
const planPrefs=(()=>{try{return {...PLAN_DEFAULTS,...JSON.parse(storage.get('llmWorkPlan')||'{}')}}catch{return {...PLAN_DEFAULTS}}})();
let planRequest=null,planGeneration=0,lastLimitSnapshot=null,limitClock=null,planOptionsKey='';
function keepWorkFocus(render){
 const active=document.activeElement;
 let selector=active?.id?'#'+CSS.escape(active.id):null;
 if(!selector&&active?.matches('[data-quota]'))selector=`[data-quota="${CSS.escape(active.dataset.quota)}"]`;
 if(!selector&&active?.matches('[data-plan-route]'))selector=`[data-plan-route="${CSS.escape(active.dataset.planRoute)}"][data-plan-model="${CSS.escape(active.dataset.planModel)}"]`;
 if(!selector&&active?.tagName==='SUMMARY'){
  const parent=active.parentElement;
  for(const attr of ['data-history','data-resource-detail'])if(parent.hasAttribute(attr))selector=`details[${attr}="${CSS.escape(parent.getAttribute(attr))}"] > summary`;
 }
 render();
 if(selector&&active&&!active.isConnected)document.querySelector(selector)?.focus({preventScroll:true});
}
function planningQuery(){
 const query=new URLSearchParams();
 for(const [key,value] of Object.entries(PLAN_DEFAULTS))if(String(planPrefs[key])!==String(value))query.set(key,planPrefs[key]);
 return '/api/limits'+(query.size?'?'+query:'');
}
function observedNow(){
 if(!limitClock)return Date.now()/1000;
 const elapsed=Math.max(0,(Date.now()-limitClock.wall)/1000,(performance.now()-limitClock.monotonic)/1000);
 return limitClock.server+elapsed;
}
function acceptLimits(data,source={}){
 if(lastLimitSnapshot?.now>data.now&&!source.saved)return false;
 lastLimitSnapshot=data;
 const delay=source.saved?Math.max(0,(Date.now()-source.saved)/1000):0;
 limitClock={server:(data.now??Date.now()/1000)+delay,wall:Date.now(),monotonic:performance.now(),offline:!!source.saved};
 const view=currentLimits(data,observedNow(),limitClock.offline);
 checkNotify(view);
 keepWorkFocus(()=>{renderLimits(view);renderWorkPlan(view);renderResources(view);if(lastUsage)renderAlerts(view,lastUsage);});
 return true;
}
function ageLimits(){
 if(!lastLimitSnapshot||document.hidden)return;
 const view=currentLimits(lastLimitSnapshot,observedNow(),limitClock.offline);
 keepWorkFocus(()=>{renderLimits(view);renderWorkPlan(view);renderResources(view);if(lastUsage)renderAlerts(view,lastUsage);});
}
setInterval(ageLimits,30000);
document.addEventListener('visibilitychange',()=>{if(!document.hidden)ageLimits();});
function planStatus(state){return {room:'관측상 여유',shortage:'부족 예상',reset_pending:'초기화 후 재확인',unknown:'판단 보류'}[state]||'판단 보류';}
function fillModelOptions(route,choices,selected){
 const options=choices.filter(o=>o.route===route);
 const html=options.map(o=>`<option value="${esc(o.model)}">${esc(o.label)}</option>`).join('');
 if($('plan-model').innerHTML!==html)$('plan-model').innerHTML=html;
 $('plan-model').value=options.some(o=>o.model===selected)?selected:(options[0]?.model||'common');
}
function syncPlanControls(p){
 const key=JSON.stringify(p.choices||[]);
 if(key!==planOptionsKey){
  const routes=[...new Map((p.choices||[]).map(o=>[o.route,o.service])).entries()];
  $('plan-route').innerHTML=routes.map(([value,name])=>`<option value="${esc(value)}">${esc(name)}</option>`).join('');
  planOptionsKey=key;
 }
 if(document.activeElement?.closest('#plan-controls,#work-budget-controls'))return;
 $('plan-route').value=p.route;fillModelOptions(p.route,p.choices||[],p.model);
 $('plan-hours').value=[.5,2,4].includes(p.hours)?String(p.hours):'custom';
 $('plan-custom-wrap').hidden=$('plan-hours').value!=='custom';$('plan-custom-hours').value=p.hours;
 $('plan-pace').value=p.pace;
 if(p.today)$('plan-today-hours').value=p.today.hours;
 if(p.week)$('plan-week-hours').value=p.week.hours;
}
function resultLine(p){
 if(!p)return '한도 관측을 기다리고 있습니다.';
 if(p.state==='unknown')return '작업 가능 시간 추정 보류';
 if(p.seconds!=null)return p.seconds<=0?(p.provider_blocked?'제공사 사용 제한 확인':'구독 한도 소진'):`같은 속도 유지 시 약 ${duration(p.seconds)}`;
 if(p.state==='shortage')return '계획한 작업량에 부족 예상';
 return p.state==='room'?`${p.hours}시간 사용에 관측상 여유`:p.state==='reset_pending'?'초기화 이후 사용량은 미확정':'작업 가능 시간 추정 보류';
}
function renderWorkPlan(data){
 const p=data.planning;
 $('work-decision').className='work-decision '+(p?.state||'unknown');
 if(!p){$('work-decision').textContent='작업 전망을 확인하려면 최신 한도를 불러오세요.';for(const id of ['work-budget-results','work-budget-table','plan-conditions'])$(id).replaceChildren();return;}
 syncPlanControls(p);
 const bucket=data.limits.find(r=>r.route===p.route&&r.bucket===p.bottleneck);
 const selected=p.choices?.find(o=>o.route===p.route&&o.model===p.model);
 const alternatives=(p.alternatives||[]).map(o=>`<button type="button" class="mini-btn" data-plan-route="${esc(o.route)}" data-plan-model="${esc(o.model)}">${esc(o.service)} · ${esc(o.label)} 기준 보기</button>`).join('');
 const support=p.state==='shortage'?(p.resources||[]).filter(r=>r.can_resolve!==false).slice(0,2):[];
 $('work-decision').innerHTML=`<div class="decision-heading"><span class="decision-state">${esc(planStatus(p.state))}</span><small>${esc(shortService[p.route]||p.route)} · ${esc(selected?.label||p.model)} · ${p.hours}시간 기준</small></div><strong class="decision-time">${esc(resultLine(p))}</strong><p>${esc(p.reason)}</p>${bucket?`<p class="decision-binding">먼저 제약하는 한도 <b>${esc(bucketName(bucket))}</b></p>`:''}${support.length?`<p class="decision-resources">${support.map(r=>esc(r.label)+' · '+esc(r.reason)).join('<br>')} <a href="#resource-panel">추가 자원 보기</a></p>`:''}${alternatives?`<div class="decision-alternatives"><span>관측 조건에 맞는 대안</span>${alternatives}</div>`:''}`;
 $('plan-conditions').innerHTML=(p.conditions||[]).map(t=>`<li>${esc(t)}</li>`).join('');
 $('plan-checked').textContent=data._offline?'오프라인 사본':p.checked?`한도 확인 ${when(p.checked)} KST`:'한도 수신 대기';
 const budget=[['오늘',p.today],['이번 주',p.week]];
 $('work-budget-brief').textContent=(shortService[p.route]||p.route)+' · '+budget.map(([label,result])=>label+' '+planStatus(result?.state)).join(' · ');
 $('work-budget-scope').textContent=`${shortService[p.route]||p.route} · ${selected?.label||p.model} · ${p.pace==='baseline'?'비교 3시간':'최근 30분'} 속도 기준 · 사용량 탭의 필터와 별도입니다.`;
 $('work-budget-results').innerHTML=budget.map(([label,result])=>`<article class="work-budget-result ${esc(result?.state||'unknown')}"><span>${label} 남은 작업 · ${result?.hours??'—'}시간</span><strong>${esc(planStatus(result?.state))}</strong><p>${esc(resultLine(result))}</p><small>${esc(result?.reason||'관측 대기')}</small></article>`).join('');
 const rows=data.limits.filter(r=>r.route===p.route&&p.applies?.includes(r.bucket));
 const projection=(r,h)=>{
  const speed=r.paces?.[p.pace]?.per_hour;
  if(r.status!=='fresh'||speed==null||speed<=0)return '관측 부족';
  if(!r.resets||r.resets<=data.now+h*3600)return '초기화 이후 재확인';
  const remains=r.remaining-speed*h;
  return remains<0?`${(-remains).toFixed(1)}%p 부족`:`약 ${remains.toFixed(1)}% 잔여`;
 };
 $('work-budget-table').innerHTML=rows.map(r=>`<tr><th scope="row">${esc(bucketName(r))}</th><td>${esc(projection(r,p.today?.hours||2))}</td><td>${esc(projection(r,p.week?.hours||10))}</td></tr>`).join('');
}
async function loadWorkPlan(){
 const query=planningQuery(),generation=++planGeneration;
 planRequest?.abort();planRequest=new AbortController();
 $('plan-message').textContent='한도와 조건을 확인하는 중…';$('plan-apply').disabled=true;
 try{
  const source={};const data=await api(query,undefined,undefined,{signal:planRequest.signal,source});
  if(generation!==planGeneration||query!==planningQuery())return;
  acceptLimits(data,source);showSource('limits',source);
  $('plan-message').textContent=source.saved?'저장된 조건의 사본입니다. 연결 후 다시 확인하세요.':'';
  if(data.planning&&!data.planning.selection_changed){Object.assign(planPrefs,{route:data.planning.route,model:data.planning.model});storage.set('llmWorkPlan',JSON.stringify(planPrefs));}
 }catch(error){
  if(generation!==planGeneration)return;
  $('plan-message').textContent=error.message;
  $('work-decision').className='work-decision unknown';$('work-decision').textContent='새 조건의 판단을 불러오지 못했습니다. 다시 확인하세요.';
 }finally{if(generation===planGeneration){planRequest=null;$('plan-apply').disabled=false;}}
}
function readPlanControls(budget=false){
 const hours=Number($('plan-hours').value==='custom'?$('plan-custom-hours').value:$('plan-hours').value);
 const today=Number(budget?$('plan-today-hours').value:planPrefs.today_hours),week=Number(budget?$('plan-week-hours').value:planPrefs.week_hours);
 if(!Number.isFinite(hours)||hours<.1||hours>168||!Number.isFinite(today)||today<.1||today>24||!Number.isFinite(week)||week<.1||week>168){$('plan-message').textContent='작업 시간을 범위 안에서 입력하세요.';return false;}
 Object.assign(planPrefs,{route:$('plan-route').value,model:$('plan-model').value,hours,pace:$('plan-pace').value,today_hours:today,week_hours:week});
 storage.set('llmWorkPlan',JSON.stringify(planPrefs));return true;
}
$('plan-controls').addEventListener('submit',event=>{event.preventDefault();if(readPlanControls())void loadWorkPlan();});
$('work-budget-controls').addEventListener('submit',event=>{event.preventDefault();if(readPlanControls(true))void loadWorkPlan();});
$('plan-route').addEventListener('change',()=>{
 fillModelOptions($('plan-route').value,lastLimits?.planning?.choices||[],'common');
 if(readPlanControls())void loadWorkPlan();
});
for(const id of ['plan-model','plan-hours','plan-pace'])$(id).addEventListener('change',()=>{
 $('plan-custom-wrap').hidden=$('plan-hours').value!=='custom';
 if(id==='plan-hours'&&$('plan-hours').value==='custom'){$('plan-custom-hours').focus();return;}
 if(readPlanControls())void loadWorkPlan();
});
for(const id of ['plan-custom-hours','plan-today-hours','plan-week-hours'])$(id).addEventListener('input',()=>{$('plan-message').textContent='입력한 작업 시간은 확인 버튼을 누르면 적용됩니다.';});
$('work-decision').addEventListener('click',event=>{
 const target=event.target.closest('[data-plan-route]');if(!target)return;
 Object.assign(planPrefs,{route:target.dataset.planRoute,model:target.dataset.planModel});void loadWorkPlan();
});
