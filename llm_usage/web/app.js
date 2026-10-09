'use strict';
// Refresh cycle and startup.
function query(){const q=new URLSearchParams();for(const id of FILTER_IDS)q.set(id,$(id).value);return q;}
let insightsPending=null,insightsGeneration=0,usageKey='',insightKey='',coreController=null;
const displayedCopies=new Map();
function showSource(name,source){
 if(source.saved)displayedCopies.set(name,source.saved);else displayedCopies.delete(name);
 offlineSince=displayedCopies.size?Math.min(...displayedCopies.values()):0;
 offlineText();
}
function offlineText(){
 $('offline').hidden=!offlineSince;
 if(offlineSince)$('offline').textContent=`오프라인 · ${ago(offlineSince/1000)} 받은 데이터를 표시합니다. 연결되면 다음 갱신에서 최신 값으로 바뀝니다.`;
}
window.addEventListener('llm-data-source-change',offlineText);
function insightsState(state,message=''){
 const ready=state==='ready',failed=state==='error';
 $('insights-status').hidden=ready;$('insights-status').setAttribute('role',failed?'alert':'status');
 $('insights-message').textContent=failed?'인사이트를 불러오지 못했습니다. '+message:ready?'':'인사이트를 불러오는 중…';
 $('insights-retry').hidden=!failed;
 for(const id of ['csv-projects','csv-sessions'])$(id).disabled=!ready;
 if(!ready){
  if(lastUsage)for(const key of ['insights','sessions','sessions_total','sessionless_requests','projects','calendar'])delete lastUsage[key];
  const text=failed?'불러오지 못했습니다. 위의 다시 불러오기를 이용하세요.':'불러오는 중…';
  for(const id of ['projects','sessions'])$(id).innerHTML=`<tr><td colspan="8" class="empty">${text}</td></tr>`;
  for(const id of ['calendar','comparison','quality','cache-overview'])$(id).textContent=text;
  for(const id of ['cache-rows','cache-series','cache-trend'])$(id).replaceChildren();
  $('sess-page').textContent='';$('sess-prev').disabled=true;$('sess-next').disabled=true;
 }
}
function cancelInsights(){
 insightsGeneration++;insightsPending?.controller.abort();insightsPending=null;insightKey='';
 displayedCopies.delete('insights');
}
function loadInsights(){
 const key=query().toString();
 if(!lastUsage||usageKey!==key){insightsState('loading');return null;}
 if(insightKey===key&&'insights' in lastUsage)return null;
 if(insightsPending?.key===key)return insightsPending.promise;
 cancelInsights();insightsState('loading');
 const q=query();q.set('sections','insights');
 const operation={key,generation:insightsGeneration,controller:new AbortController(),promise:null};
 const source={};insightsPending=operation;
 const current=()=>insightsPending===operation&&operation.generation===insightsGeneration&&query().toString()===key&&usageKey===key;
 operation.promise=api('/api/usage?'+q,undefined,undefined,{signal:operation.controller.signal,source}).then(data=>{
  if(!current()||!lastUsage)return;
  Object.assign(lastUsage,data);insightKey=key;insightsState('ready');renderInsights(lastUsage);showSource('insights',source);
 }).catch(error=>{
  if(current()&&!operation.controller.signal.aborted)insightsState('error',error.message);
 }).finally(()=>{if(insightsPending===operation)insightsPending=null;});
 return operation.promise;
}
$('insights-retry').addEventListener('click',()=>loadInsights());
let lastRefresh=0,busyTimer=null;
function setBusy(on){
 clearTimeout(busyTimer);
 $('refresh').setAttribute('aria-busy',String(on));
 // Dim panels only for requests that take a noticeable amount of time.
 if(on)busyTimer=setTimeout(()=>document.body.classList.add('busy'),300);
 else document.body.classList.remove('busy');
}
function updatedText(){
 offlineText();
 if(!lastRefresh||pending||!$('error').hidden)return;
 const parts=['마지막 갱신 '+ago(lastRefresh)];
 const collected=lastUsage&&lastUsage.collected_at;
 if(collected)parts.push('수집 '+ago(collected));
 $('updated').title=['마지막 갱신 '+when(lastRefresh),collected?'수집 '+when(collected):''].filter(Boolean).join(' · ')+' KST';
 if($('auto').checked)parts.push('다음 '+Math.max(1,Math.ceil((lastRefresh+refreshMs/1000-Date.now()/1000)/60))+'분 후');
 // Phones get one short phrase; the full line stays in the tooltip.
 $('updated').textContent=compactMedia.matches?parts[0]:parts.join(' · ');
 if(compactMedia.matches)$('updated').title=parts.join(' · ');
 $('updated').classList.toggle('stale',!!(collected&&Date.now()/1000-collected>900));
}
setInterval(updatedText,30000);
async function refresh(manual=false){
 if(pending){
  if(query().toString()!==activeQuery){queued=true;coreController?.abort();cancelInsights();insightsState('loading');}
  queuedManual=queuedManual||manual;
  return pending;
 }
 pending=(async()=>{
  $('refresh').disabled=true;setBusy(true);
  try{
   do{
    queued=false;activeQuery=query().toString();cancelInsights();insightsState('loading');
    const collect=manual||queuedManual;manual=false;queuedManual=false;
    $('error').hidden=true;$('updated').textContent='불러오는 중…';
    try{
     // The token is only needed to write; an unreachable server still shows the last data copy.
     if(!csrf){try{const b=await api('/api/bootstrap');csrf=b.csrf;applyRefreshMs((b.refresh_seconds||300)*1000);}catch(error){if(collect)throw error;}}
     if(collect){
      const b=await api('/api/bootstrap');csrf=b.csrf;applyRefreshMs((b.refresh_seconds||300)*1000);
      const job=await api('/api/refresh',{});$('updated').textContent='수집 요청 중…';
      const deadline=Date.now()+90000;let completed=false;
      while(Date.now()<deadline){
       const state=await api('/api/collection',undefined,undefined,{deadline:Math.min(deadline,Date.now()+10000)})
        .catch(error=>{throw Error('수집 완료 여부를 아직 확인하지 못했습니다. '+error.message);});
       if((state.completed_id??state.completed)>=(job.request_id??job.requested)){completed=true;break;}
       await new Promise(resolve=>setTimeout(resolve,1000));if(document.hidden)break;
      }
      if(!completed&&!document.hidden)throw Error('수집 응답을 기다리는 중입니다. 잠시 후 다시 갱신하세요. 기존 기록은 유지됩니다.');
     }
     activeQuery=query().toString();queued=false;
     const core=query();core.set('sections','core');coreController=new AbortController();
     const sources={core:{},limits:{}};
     const workQuery=planningQuery();
     const [usage,limits]=await Promise.all([api('/api/usage?'+core,undefined,undefined,{signal:coreController.signal,source:sources.core}),
       api(workQuery,undefined,undefined,{signal:coreController.signal,source:sources.limits})]);
     if(activeQuery!==query().toString()){queued=true;continue;}
     usageKey=activeQuery;
     renderUsage(usage);if(!sources.core.saved)checkOpsNotify(usage);
     if(workQuery===planningQuery())acceptLimits(limits,sources.limits);
     lastRefresh=Date.now()/1000;
     // Rendering can fall back from an unavailable cost metric to tokens.
     usageKey=query().toString();activeQuery=usageKey;
     showSource('core',sources.core);if(workQuery===planningQuery())showSource('limits',sources.limits);
     if(currentView==='insights')await loadInsights();
    }catch(e){
     if(activeQuery!==query().toString())queued=true;
     else{$('error').textContent=e.message;$('error').hidden=false;$('updated').textContent='갱신 실패 · 기존 기록 표시';}
    }
   }while(queued||queuedManual);
  }finally{coreController=null;$('refresh').disabled=false;setBusy(false);pending=null;updatedText();schedule();}
 })();return pending;
}
function applyRefreshMs(ms){refreshMs=ms;$('auto-label').textContent=(ms>=60000?`${Math.round(ms/60000)}분`:`${Math.round(ms/1000)}초`)+' 자동 갱신';}
function schedule(){clearTimeout(timer);if($('auto').checked&&!document.hidden)timer=setTimeout(()=>refresh(),refreshMs);}
$('theme').value=window.LLMTheme.choice;
$('theme').addEventListener('change',()=>window.LLMTheme.set($('theme').value));
window.addEventListener('llm-theme-change',()=>{syncThemeColor();updateColors();if(lastUsage){chart(lastUsage);composition(lastUsage);ranking(lastUsage);renderCacheTrend(lastUsage);}});
$('refresh').addEventListener('click',()=>refresh(true));$('auto').addEventListener('change',schedule);
for(const id of FILTER_IDS)$(id).addEventListener('change',()=>{
 const custom=$('period').value==='custom';$('start-wrap').hidden=!custom;$('end-wrap').hidden=!custom;saveDefaults();syncUrl();filterSummary();refresh();
});
$('scope-chip').addEventListener('click',()=>{$('scope').value='';$('scope').dispatchEvent(new Event('change'));});
const notifyBox=$('notify');
if(!('Notification' in window)){notifyBox.disabled=true;notifyBox.closest('label').title='이 브라우저는 알림을 지원하지 않습니다.';}
notifyBox.checked=notifyEnabled();
notifyBox.addEventListener('change',async()=>{
 if(!notifyBox.checked){storage.set('llmNotify','0');notificationStatus(false);return;}
 try{
  const p=Notification.permission==='default'?await Notification.requestPermission():Notification.permission;
  notifyBox.checked=p==='granted';storage.set('llmNotify',p==='granted'?'1':'0');
  if(p==='granted'&&'serviceWorker' in navigator)await notificationRegistration();
  notificationStatus(false);
 }catch(error){notificationStatus(true);}
});
document.addEventListener('visibilitychange',()=>{schedule();if(!document.hidden&&$('auto').checked)refresh();});
const today=new Intl.DateTimeFormat('en-CA',{timeZone:'Asia/Seoul',year:'numeric',month:'2-digit',day:'2-digit'}).format(new Date());$('start').value=today;$('end').value=today;
{
 const params=new URLSearchParams(location.search);
 const stored=(()=>{try{return JSON.parse(storage.get('llmDefaults'))||{}}catch{return{}}})();
 const saved=FILTER_IDS.some(id=>params.has(id))||params.has('view')?null:stored;
 const get=id=>params.get(id)??saved?.[id]??null;
 for(const id of FILTER_IDS){
  const v=get(id);if(v==null)continue;
  const el=$(id);
  if(el.tagName==='SELECT'){
   // The scope options arrive with the first usage response; keep a placeholder so the
   // value survives until renderScope() rebuilds the list.
   if(v&&id==='scope'&&![...el.options].some(o=>o.value===v))el.add(new Option(v,v));
   if([...el.options].some(o=>o.value===v))el.value=v;
  }
  else if(/^\d{4}-\d{2}-\d{2}$/.test(v))el.value=v;
 }
 const custom=$('period').value==='custom';$('start-wrap').hidden=!custom;$('end-wrap').hidden=!custom;
 setView(params.get('view')??saved?.view);filterSummary();
}
$('filter-reset').addEventListener('click',()=>{
 $('period').value='7d';$('granularity').value='day';$('group').value='route';$('cumulative').value='0';$('metric').value='tokens';$('compare').value='';$('scope').value='';
 $('start').value=today;$('end').value=today;$('start-wrap').hidden=true;$('end-wrap').hidden=true;
 saveDefaults();syncUrl();filterSummary();refresh();
});
// Keyboard shortcuts outside text fields: r refreshes, 1–5 switch tabs, / focuses the
// current view's search, ? lists them.
const SHORTCUT_VIEWS=['overview','analysis','insights','sources','settings'];
document.addEventListener('keydown',event=>{
 if(event.ctrlKey||event.metaKey||event.altKey||event.target.closest('input,select,textarea,[contenteditable]'))return;
 const view=SHORTCUT_VIEWS[Number(event.key)-1];
 if(view){event.preventDefault();setView(view);$('tab-'+view).focus();return;}
 if(event.key==='r'){event.preventDefault();refresh(true);}
 else if(event.key==='/'){
  const field=currentView==='settings'?$('price-search'):currentView==='analysis'?$('model-search'):null;
  if(field){event.preventDefault();field.focus();}
 }else if(event.key==='?'){event.preventDefault();$('shortcuts').hidden=!$('shortcuts').hidden;}
});
// The root worker serves the offline page copy and notifications; workers registered
// at /assets/ by earlier versions are removed.
if('serviceWorker' in navigator){
 navigator.serviceWorker.getRegistrations().then(list=>list.filter(r=>new URL(r.scope).pathname==='/assets/').forEach(r=>r.unregister())).catch(()=>{});
 notificationRegistration().catch(()=>{});
}
applyPanelOrder();
refresh();
