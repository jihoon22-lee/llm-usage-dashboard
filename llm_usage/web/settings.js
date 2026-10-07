'use strict';
// Settings: subscription prices, model rates, thresholds, external alerts and budgets.
const RATE_KEYS=['input','cached','output','cache_write'];
const THRESHOLD_FIELDS=[['stale_seconds','stale 판정','초 · 60~86400'],['retention_days','한도 이력 보존','일 · 1~365'],['low_percent','잔여 적음 배지','% · 1~50'],['quota_hide_days','관측 중단 접기','일 · 1~90']];
function cfgMsg(id,text){const el=$(id);el.textContent=text;setTimeout(()=>{if(el.textContent===text)el.textContent='';},5000);}
let settingsEpoch=0,settingsOnline=false;
function settingsAvailable(available,message=''){
 if(!available)settingsEpoch++;
 settingsOnline=available;
 $('settings-status').textContent=message;
 for(const control of $('view-settings').querySelectorAll('input,button,select')){
  if(control.closest('#local-statistics')||['auto','notify','ntf-low','ntf-dep','ntf-reset','ntf-ops'].includes(control.id))continue;
  control.disabled=!available;
  if(!available&&control.matches('input')){control.value='';control.checked=false;}
 }
 if(!available){
  for(const id of ['cfg-subs','cfg-pricing','cfg-thresholds','cfg-budgets','notify-events','notify-log'])$(id).replaceChildren();
  for(const id of ['ntfy-state','webhook-state','telegram-state'])$(id).textContent='연결 후 확인';
  $('price-save-all').hidden=true;
 }
}
for(const event of ['offline','llm-api-unavailable'])window.addEventListener(event,()=>settingsAvailable(false,'서버에 연결할 수 없어 설정을 비웠습니다. 온라인 연결 후 다시 설정을 여세요.'));
// A write completed after an outage must not restore cleared settings or start
// a fresh reload on behalf of the obsolete operation.
async function settingsApi(path,body){
 const epoch=settingsEpoch;
 if(!settingsOnline)throw Error('온라인 연결 후 다시 설정을 여세요.');
 const result=await api(path,body);
 if(epoch!==settingsEpoch||!settingsOnline)throw Error('연결 상태가 바뀌었습니다. 설정을 다시 여세요.');
 return result;
}
$('local-statistics-clear').addEventListener('click',async()=>{
 try{await clearStatisticsCache();$('local-statistics-status').textContent='이 기기의 통계 사본을 지웠습니다. 온라인에서 다음 조회 시 다시 저장됩니다.';}
 catch{$('local-statistics-status').textContent='브라우저 저장소에 접근할 수 없어 삭제를 확인하지 못했습니다.';}
});
async function loadConfig(){
 const epoch=++settingsEpoch;
 let cfg;
 try{cfg=await api('/api/config');}
 catch(e){settingsAvailable(false,'설정을 불러오지 못했습니다. 온라인 연결 후 다시 설정을 여세요. '+e.message);return;}
 if(epoch!==settingsEpoch)return;
 settingsAvailable(true);
 const prices=cfg.subscription_prices||{};
 const routes=[...new Set([...Object.keys(prices),...(lastUsage?.subscriptions||[]).map(s=>s.route)])].sort(compareText.compare);
 $('cfg-subs').innerHTML=(routes.map(r=>`<label class="cfg-row"><span>${esc(routeNames[r]||r)}<small>${esc(r)}</small></span><input type="number" min="0" max="100000" step="0.01" data-route="${esc(r)}" value="${prices[r]??''}" placeholder="미설정"></label>`).join('')||'<p class="empty">구독 경로가 아직 없습니다.</p>')
  +'<button type="button" class="mini-btn" id="cfg-subs-save">저장</button>';
 const eff=cfg.pricing||{},resolved=Object.fromEntries((cfg.models||[]).map(m=>[m.model,m.rate]));
 const origins=cfg.pricing_origins||{},builtinSet=new Set(cfg.pricing_builtin||[]);
 const today=cfg.today;
 const currentRate=m=>{const e=eff[m];return Array.isArray(e)?e.filter(x=>x.since<=today).pop()||e[0]:e;};
 // Models used in the last 30 days come first, by volume; unused table entries follow by name.
 const usage30=cfg.model_usage_30d||{},providerOf=Object.fromEntries((cfg.models||[]).map(m=>[m.model,m.provider]));
 const models=[...new Set([...Object.keys(resolved),...Object.keys(eff),...Object.keys(origins)])]
  .sort((a,b)=>(usage30[b]||0)-(usage30[a]||0)||compareText.compare(a,b));
 $('cfg-pricing').innerHTML='<div class="table-wrap"><table class="cfg-table"><thead><tr><th>모델</th><th>최근 30일</th><th>입력</th><th>캐시 읽기</th><th>출력</th><th>캐시 쓰기</th><th></th></tr></thead><tbody>'+models.map(m=>{
  const entry=eff[m],origin=origins[m];
  const rate=origin==='hidden'?{}:(currentRate(m)||resolved[m]||{});
  const up=(cfg.upcoming||{})[m];
  let state='',buttons;
  if(origin==='hidden'){state='<small class="warn-text">숨김</small>';buttons=`<button type="button" class="mini-btn" data-price-del="${esc(m)}">숨김 해제</button>`;}
  else{
   state=(origin==='user'?'<small>사용자</small>':origin==='builtin'?'<small>내장</small>':resolved[m]?'<small>상위 단가 상속</small>':'<small class="warn-text">미설정</small>')
    +(Array.isArray(entry)?'<small>날짜별 이력</small>':'')
    +(up?`<small class="warn-text">${esc(up.since)}부터 $${up.input}/$${up.output} 예정</small>`:'');
   buttons=`<button type="button" class="mini-btn" data-price-save="${esc(m)}">저장</button>`
    +(origin==='user'?`<button type="button" class="mini-btn" data-price-del="${esc(m)}">${builtinSet.has(m)?'내장값 복원':'삭제'}</button>`:'')
    +(origin?`<button type="button" class="mini-btn" data-price-hide="${esc(m)}">숨기기</button>`:'');
  }
  return `<tr data-price-row="${esc(m)}"><td>${esc(m)}${providerOf[m]?`<small>${esc(providerOf[m])}</small>`:''}${state}</td><td class="price-usage">${usage30[m]?compact(usage30[m]):'—'}</td>`+RATE_KEYS.map(k=>`<td><input type="number" min="0" max="10000" step="0.001" data-model="${esc(m)}" data-key="${k}" value="${rate[k]??''}" placeholder="—"${origin==='hidden'?' disabled':''}></td>`).join('')
   +`<td>${buttons}</td></tr>`;
 }).join('')+'</tbody></table></div>';
 applyPriceFilter();syncPriceDirty();
 renderNotifySettings(cfg.notify||{});renderBudgets(cfg.project_budgets||{});loadNotifyLog();
 $('cfg-thresholds').innerHTML=THRESHOLD_FIELDS.map(([k,label,unit])=>`<label class="cfg-row"><span>${label}<small>${unit}</small></span><input type="number" step="1" id="cfg-${k}" value="${cfg.thresholds[k]}"></label>`).join('')+'<button type="button" class="mini-btn" id="cfg-thresholds-save">저장</button>';
 $('cfg-refresh').value=cfg.refresh_seconds;
 $('cfg-valert').value=cfg.value_alert_usd??'';
 for(const k of['low','dep','reset','ops'])$('ntf-'+k).checked=storage.get('llmNotify:'+k)!=='0';
}
$('cfg-subs').addEventListener('click',async e=>{
 if(e.target.id!=='cfg-subs-save')return;
 const prices={};
 for(const i of $('cfg-subs').querySelectorAll('input[data-route]')){
  if(i.value==='')continue;
  const v=parseFloat(i.value);
  if(!Number.isFinite(v)||v<0){cfgMsg('cfg-subs-msg','금액을 확인하세요: '+i.dataset.route);return;}
  prices[i.dataset.route]=v;
 }
 try{await settingsApi('/api/config/subscription-prices',{prices});cfgMsg('cfg-subs-msg','저장했습니다.');refresh();}
 catch(err){cfgMsg('cfg-subs-msg',err.message);}
});
function priceBody(model){
 const body={model};
 for(const k of RATE_KEYS){
  const input=$('cfg-pricing').querySelector(`input[data-model="${CSS.escape(model)}"][data-key="${k}"]`);
  if(input.value==='')continue;
  const v=parseFloat(input.value);
  if(!Number.isFinite(v)||v<0)throw Error('단가를 확인하세요: '+model);
  body[k]=v;
 }
 return body;
}
// Rows whose inputs changed since the table was drawn can be saved together.
function syncPriceDirty(){
 const dirty=$('cfg-pricing').querySelectorAll('tr.dirty').length;
 $('price-save-all').hidden=!dirty;$('price-save-all').textContent=`변경 ${dirty}개 저장`;
}
$('cfg-pricing').addEventListener('input',e=>{const row=e.target.closest('tr[data-price-row]');if(row){row.classList.add('dirty');syncPriceDirty();}});
$('price-save-all').addEventListener('click',async()=>{
 const epoch=settingsEpoch;
 const models=[...$('cfg-pricing').querySelectorAll('tr.dirty')].map(tr=>tr.dataset.priceRow);
 const failed=[];
 for(const model of models){try{await settingsApi('/api/config/pricing',priceBody(model));}catch(err){failed.push(`${model} (${err.message})`);}}
 if(epoch!==settingsEpoch||!settingsOnline)return;
 cfgMsg('cfg-pricing-msg',failed.length?'저장 실패: '+failed.join(', '):`${models.length}개 저장했습니다.`);
 loadConfig();refresh();
});
$('cfg-pricing').addEventListener('click',async e=>{
 const save=e.target.dataset.priceSave,del=e.target.dataset.priceDel,hide=e.target.dataset.priceHide;
 if(!save&&!del&&!hide)return;
 const model=save||del||hide;let body={model};
 if(del)body.delete=true;
 else if(hide){if(!confirm(`"${model}" 단가를 숨길까요? 비용이 미산정으로 표시되고 설정에서 되돌릴 수 있습니다.`))return;body.hide=true;}
 else try{body=priceBody(model);}catch(err){cfgMsg('cfg-pricing-msg',err.message);return;}
 try{await settingsApi('/api/config/pricing',body);cfgMsg('cfg-pricing-msg',(del?'복원':hide?'숨김':'저장')+'했습니다: '+model);loadConfig();refresh();}
 catch(err){cfgMsg('cfg-pricing-msg',err.message);}
});
// External alerts: secrets are write-only here; the page only learns which channels are set.
const NOTIFY_EVENTS={low:'잔여 적음',exhausted:'소진',recovered:'초기화 후 회복',spike:'사용량 급증',collector:'수집기 재시작',source:'수집 실패 지속',budget:'프로젝트 예산',report:'주간 리포트'};
// Budgets: this month's busiest projects plus every budgeted one; tokens are entered in millions.
const extraBudgetProjects=new Set();
function renderBudgets(budgets){
 const month=lastUsage?.project_month?.projects||{};
 const names=[...new Set([...Object.entries(month).sort((a,b)=>b[1].tokens-a[1].tokens).slice(0,12).map(([n])=>n),...Object.keys(budgets),...extraBudgetProjects])];
 $('cfg-budgets').innerHTML=names.length?names.map(n=>{const b=budgets[n]||{},u=month[n];
  return `<div class="cfg-row" data-budget="${esc(n)}"><span>${esc(n)}<small>이번 달 ${compact(u?.tokens||0)} 토큰${u?.cost!=null?' · '+usd(u.cost):''}</small></span><span><input type="number" class="cfg-num" min="0" step="1" data-budget-key="tokens" value="${b.tokens?b.tokens/1e6:''}" placeholder="—"> M 토큰 <input type="number" class="cfg-num" min="0" step="0.01" data-budget-key="usd" value="${b.usd??''}" placeholder="—"> USD</span></div>`;}).join('')
  :'<p class="hint">이번 달 프로젝트 기록이 아직 없습니다.</p>';
}
// A project with no use this month yet can still get a budget.
$('budget-add').addEventListener('click',()=>{
 const name=$('budget-new').value.trim();if(!name)return;
 extraBudgetProjects.add(name);$('budget-new').value='';
 const kept=Object.fromEntries([...$('cfg-budgets').querySelectorAll('[data-budget]')].map(row=>{
  const t=row.querySelector('[data-budget-key="tokens"]').value,u=row.querySelector('[data-budget-key="usd"]').value;
  return [row.dataset.budget,{tokens:t===''?null:parseFloat(t)*1e6,usd:u===''?null:parseFloat(u)}];}).filter(([,b])=>b.tokens!=null||b.usd!=null));
 renderBudgets(kept);
 $('cfg-budgets').querySelector(`[data-budget="${CSS.escape(name)}"] input`)?.focus();
});
$('budgets-save').addEventListener('click',async()=>{
 const budgets={};
 for(const row of $('cfg-budgets').querySelectorAll('[data-budget]')){
  const t=row.querySelector('[data-budget-key="tokens"]').value,u=row.querySelector('[data-budget-key="usd"]').value;
  if(t===''&&u==='')continue;
  const tokens=t===''?null:Math.round(parseFloat(t)*1e6),dollars=u===''?null:parseFloat(u);
  if((tokens!=null&&!(tokens>0))||(dollars!=null&&!(dollars>0))){cfgMsg('budgets-msg','예산은 0보다 커야 합니다: '+row.dataset.budget);return;}
  budgets[row.dataset.budget]={tokens,usd:dollars};
 }
 try{const r=await settingsApi('/api/config/project-budgets',{budgets});cfgMsg('budgets-msg',`${Object.keys(r.project_budgets).length}개 프로젝트 예산을 저장했습니다.`);renderBudgets(r.project_budgets);refresh();}
 catch(err){cfgMsg('budgets-msg',err.message);}
});
function renderNotifySettings(n){
 const ch=n.channels||{};
 $('ntfy-state').textContent=ch.ntfy?`설정됨 · ${ch.ntfy}`:'https://ntfy.sh/토픽';
 $('webhook-state').textContent=ch.webhook?`설정됨 · ${ch.webhook}`:'Discord·Slack 호환';
 $('telegram-state').textContent=ch.telegram?'설정됨':'봇 토큰 · 채팅 ID';
 for(const id of ['ntfy-url','webhook-url','tg-token','tg-chat'])$(id).value='';
 $('notify-events').innerHTML=Object.entries(NOTIFY_EVENTS).map(([k,label])=>`<label><input type="checkbox" data-notify-event="${k}"${(n.events||{})[k]!==false?' checked':''}> ${label}</label>`).join('');
 $('quiet-start').value=n.quiet?n.quiet[0]:'';$('quiet-end').value=n.quiet?n.quiet[1]:'';
 $('notify-test').disabled=!(ch.ntfy||ch.webhook||ch.telegram);
}
const LOG_STATUS={sent:'보냄',resent:'보류 후 보냄',held:'방해 금지 보류',failed:'전송 실패 · 재시도',expired:'만료되어 버림'};
async function loadNotifyLog(){
 const epoch=settingsEpoch;
 try{const d=await api('/api/notify/log');
  if(epoch!==settingsEpoch||!settingsOnline)return;
  $('notify-log').innerHTML=d.log.length?'<ul>'+d.log.map(e=>`<li><span>${esc(when(e.ts))}</span> <strong>${esc(e.title)}</strong> <small class="${e.status==='failed'||e.status==='expired'?'warn-text':''}">${esc(LOG_STATUS[e.status]||e.status)}${e.error?' · '+esc(e.error):''}</small></li>`).join('')+'</ul>':'<p class="hint">아직 전송 이력이 없습니다.</p>';}
 catch(e){if(epoch!==settingsEpoch||!settingsOnline)return;$('notify-log').innerHTML=`<p class="hint">이력을 불러오지 못했습니다 (${esc(e.message)})</p>`;}
}
async function saveNotify(body,message){
 try{renderNotifySettings(await settingsApi('/api/config/notify',body));cfgMsg('notify-msg',message);}
 catch(err){cfgMsg('notify-msg',err.message);}
}
$('notify-save').addEventListener('click',()=>{
 const body={events:Object.fromEntries([...$('notify-events').querySelectorAll('input')].map(i=>[i.dataset.notifyEvent,i.checked]))};
 for(const [id,key] of [['ntfy-url','ntfy_url'],['webhook-url','webhook_url'],['tg-token','telegram_token'],['tg-chat','telegram_chat']])
  if($(id).value.trim())body[key]=$(id).value.trim();
 const start=$('quiet-start').value,end=$('quiet-end').value;
 const hours=[start,end].map(v=>Number(v));
 if((start==='')!==(end==='')){cfgMsg('notify-msg','방해 금지는 시작과 끝 시각을 모두 입력하거나 둘 다 비우세요.');return;}
 if(start!==''&&(hours.some(h=>!Number.isInteger(h)||h<0||h>23)||hours[0]===hours[1])){cfgMsg('notify-msg','방해 금지 시각은 서로 다른 0~23시 정수여야 합니다.');return;}
 body.quiet=start===''?null:hours;
 saveNotify(body,'저장했습니다. 방해 금지 중 알림은 끝난 뒤 모아서 보냅니다.');
});
$('cfg-notify').addEventListener('click',e=>{
 const key=e.target.dataset.notifyClear;if(!key)return;
 saveNotify(key==='telegram'?{telegram_token:'',telegram_chat:''}:{[key]:''},'지웠습니다.');
});
$('notify-test').addEventListener('click',async()=>{
 try{const r=await settingsApi('/api/notify/test',{});cfgMsg('notify-msg',`테스트를 보냈습니다: ${r.channels.join(', ')}`);}
 catch(err){cfgMsg('notify-msg','테스트 실패: '+err.message);}
});
$('cfg-thresholds').addEventListener('click',async e=>{
 if(e.target.id!=='cfg-thresholds-save')return;
 const body={};
 for(const[k]of THRESHOLD_FIELDS){const v=parseFloat($('cfg-'+k).value);if(Number.isFinite(v))body[k]=v;}
 try{await settingsApi('/api/config/thresholds',body);cfgMsg('cfg-thresholds-msg','저장했습니다.');refresh();}
 catch(err){cfgMsg('cfg-thresholds-msg',err.message);}
});
// The pricing filter lives outside #cfg-pricing because loadConfig replaces
// that node's contents; state persists across re-renders and is reapplied.
const priceFilter={query:'',unset:false};
function applyPriceFilter(){
 const rows=[...$('cfg-pricing').querySelectorAll('tbody tr')];
 let shown=0;
 rows.forEach(tr=>{
  const cell=tr.cells[0];
  const unset=[...cell.querySelectorAll('.warn-text')].some(el=>el.textContent.trim()==='미설정');
  const ok=(!priceFilter.query||cell.textContent.toLowerCase().includes(priceFilter.query))&&(!priceFilter.unset||unset);
  tr.hidden=!ok;if(ok)shown++;
 });
 $('price-count').textContent=(priceFilter.query||priceFilter.unset)?`${shown}/${rows.length}개`:'';
}
$('price-search').addEventListener('input',event=>{priceFilter.query=event.target.value.trim().toLowerCase();applyPriceFilter();});
$('price-unset').addEventListener('click',event=>{
 priceFilter.unset=!priceFilter.unset;
 event.currentTarget.setAttribute('aria-pressed',String(priceFilter.unset));
 event.currentTarget.classList.toggle('on',priceFilter.unset);
 applyPriceFilter();
});
for(const k of['low','dep','reset','ops'])$('ntf-'+k).addEventListener('change',e=>{storage.set('llmNotify:'+k,e.target.checked?'1':'0');});
$('cfg-refresh-save').addEventListener('click',async()=>{
 const v=parseInt($('cfg-refresh').value,10);
 if(!Number.isFinite(v)||v<30||v>3600){cfgMsg('cfg-refresh-msg','30~3600초 범위로 입력하세요.');return;}
 try{const r=await settingsApi('/api/config/refresh',{refresh_seconds:v});applyRefreshMs(r.refresh_seconds*1000);schedule();cfgMsg('cfg-refresh-msg','저장했습니다.');}
 catch(err){cfgMsg('cfg-refresh-msg',err.message);}
});
$('cfg-valert-save').addEventListener('click',async()=>{
 const raw=$('cfg-valert').value.trim(),v=raw===''?null:parseFloat(raw);
 if(v!==null&&(!Number.isFinite(v)||v<0||v>1000000)){cfgMsg('cfg-refresh-msg','0 이상 USD로 입력하거나 비워서 해제하세요.');return;}
 try{await settingsApi('/api/config/value-alert',{value_alert_usd:v});cfgMsg('cfg-refresh-msg','저장했습니다.');refresh();}
 catch(err){cfgMsg('cfg-refresh-msg',err.message);}
});
