'use strict';
// Age quota and resource observations together, including offline copies.
let limitRequest=null,limitGeneration=0,lastLimitSnapshot=null,limitClock=null;
function keepObservationFocus(render){
 const active=document.activeElement;
 let selector=active?.id?'#'+CSS.escape(active.id):null;
 if(!selector&&active?.matches('[data-quota]'))selector=`[data-quota="${CSS.escape(active.dataset.quota)}"]`;
 if(!selector&&active?.tagName==='SUMMARY'){
  const parent=active.parentElement;
  for(const attr of ['data-history','data-resource-detail'])if(parent.hasAttribute(attr))selector=`details[${attr}="${CSS.escape(parent.getAttribute(attr))}"] > summary`;
 }
 render();
 if(selector&&active&&!active.isConnected)document.querySelector(selector)?.focus({preventScroll:true});
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
 keepObservationFocus(()=>{renderLimits(view);renderResources(view);if(lastUsage)renderAlerts(view,lastUsage);});
 return true;
}
function ageLimits(){
 if(!lastLimitSnapshot||document.hidden)return;
 const view=currentLimits(lastLimitSnapshot,observedNow(),limitClock.offline);
 keepObservationFocus(()=>{renderLimits(view);renderResources(view);if(lastUsage)renderAlerts(view,lastUsage);});
}
setInterval(ageLimits,30000);
document.addEventListener('visibilitychange',()=>{if(!document.hidden)ageLimits();});

async function loadLimits(){
 const generation=++limitGeneration;
 limitRequest?.abort();limitRequest=new AbortController();
 try{
  const source={};
  const data=await api('/api/limits',undefined,undefined,{signal:limitRequest.signal,source});
  if(generation!==limitGeneration)return;
  acceptLimits(data,source);showSource('limits',source);
 }finally{if(generation===limitGeneration)limitRequest=null;}
}
