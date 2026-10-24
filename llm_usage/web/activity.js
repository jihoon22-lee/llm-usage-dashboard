'use strict';
// The usage tab has independent token, consumption and activity views.
let usageView='tokens',activityPage=1,lastActivity=null,lastConsumption=null;
const codexReads=new Map(),codexCopies=new Map(),taskReads=new Map();
const EXECUTION_NAMES={completed:'완료',failed:'실패',cancelled:'취소',running:'진행 중',pending:'실행 대기',unknown:'미확인'};
const ACTIVITY_KINDS={cloud:'클라우드 작업',code_review:'코드 리뷰',security_review:'보안 검토',unknown:'미분류'};
const CONSUMPTION_STATUS={partial:'일부 제공',complete:'제공',available:'제공',unavailable:'미제공',pending:'조회 대기',detail_error:'상세 수집 실패',unlinked:'스레드 미연결',mixed:'일부 스레드 확인',unknown:'제공 상태 미확인'};
function setUsageView(name,sync=true,load=true){
 const next=['tokens','consumption','activity'].includes(name)?name:'tokens';
 if(next!==usageView){for(const request of codexReads.values())request.controller.abort();codexReads.clear();for(const c of taskReads.values())c.abort();taskReads.clear();}
 usageView=next;
 if(next!=='tokens')$('insights-status').hidden=true;
 for(const mode of ['tokens','consumption','activity']){
  $('usage-'+mode).hidden=mode!==next;
  const tab=$('usage-tab-'+mode);tab.setAttribute('aria-selected',String(mode===next));tab.tabIndex=mode===next?0:-1;
 }
 if(sync){saveDefaults();syncUrl(true);}
 if(load&&currentView==='usage'){
  if(next==='tokens'){loadInsights();if(lastUsage)chart(lastUsage);}
  else void loadCodexView();
 }
}
$('usage-nav').addEventListener('click',event=>{const b=event.target.closest('[data-usage-view]');if(b)setUsageView(b.dataset.usageView);});
$('usage-nav').addEventListener('keydown',event=>{
 const tabs=[...$('usage-nav').querySelectorAll('[role=tab]')],i=tabs.indexOf(document.activeElement);
 if(i<0)return;
 const j=event.key==='ArrowRight'?(i+1)%tabs.length:event.key==='ArrowLeft'?(i+tabs.length-1)%tabs.length:event.key==='Home'?0:event.key==='End'?tabs.length-1:null;
 if(j==null)return;event.preventDefault();tabs[j].focus();setUsageView(tabs[j].dataset.usageView);
});
document.addEventListener('click',event=>{
 const link=event.target.closest('[data-open-consumption]');if(!link)return;
 event.preventDefault();setUsageView('consumption',false,false);setView('usage',false);syncUrl(true);
 $('usage-tab-consumption').focus();$('usage-nav').scrollIntoView({behavior:motion(),block:'start'});
});
function activityQuery(){
 const q=new URLSearchParams({period:$('activity-period').value,kind:$('activity-kind').value,state:$('activity-state').value,
  search:$('activity-search').value.trim(),page:String(activityPage)});
 if(q.get('period')==='custom'){q.set('start',$('activity-start').value);q.set('end',$('activity-end').value);}
 return q;
}
function codexPath(mode){return mode==='activity'?'/api/codex/activity?'+activityQuery():'/api/codex/consumption?days='+$('consumption-days').value;}
function sourceCaption(data,names){
 const sources=(data.sources||[]).filter(s=>names.includes(s.name));
 return sources.map(s=>`${sourceName(s.name)} · ${statuses[s.status]||s.status}${s.checked?' · '+when(s.checked)+' KST':''}${s.stale?' · 오래된 값':''}${s.detail?' · '+s.detail:''}`).join(' / ');
}
async function loadCodexView(force=false){
 const mode=usageView;if(currentView!=='usage'||mode==='tokens')return;
 const path=codexPath(mode),kept=codexCopies.get(mode);
 if(!force&&kept?.path===path&&Date.now()-kept.saved<60000)return;
 if(codexReads.get(mode)?.path===path)return codexReads.get(mode).promise;
 codexReads.get(mode)?.controller.abort();
 const op={path,controller:new AbortController(),promise:null};codexReads.set(mode,op);
 const note=$(mode+'-status');note.textContent='저장된 수집 데이터를 불러오는 중…';note.classList.remove('warn-text');
 if(kept?.path!==path){
  if(mode==='activity'){lastActivity=null;for(const c of taskReads.values())c.abort();taskReads.clear();$('activity-rows').replaceChildren();$('activity-summary').replaceChildren();$('activity-coverage').replaceChildren();$('activity-page').textContent='';$('activity-prev').disabled=$('activity-next').disabled=true;}
  else{lastConsumption=null;for(const id of ['consumption-chart','consumption-summary','consumption-detail','consumption-legend','consumption-day'])$(id).replaceChildren();}
 }
 const current=()=>codexReads.get(mode)===op&&codexPath(mode)===path&&usageView===mode;
 op.promise=api(path,undefined,undefined,{signal:op.controller.signal}).then(result=>{
  if(!current())return;
  const previous=codexCopies.get(mode);
  if(previous?.data.account_key!==result.account_key){for(const c of taskReads.values())c.abort();taskReads.clear();}
  codexCopies.set(mode,{path,data:result,saved:Date.now()});
  if(mode==='activity'){lastActivity=result;renderActivity(result);}else{lastConsumption=result;renderConsumption(result);}
  note.textContent=sourceCaption(result,mode==='activity'?['codex-activity','codex-consumption-threads']:['codex-consumption-daily']);
 }).catch(error=>{
  if(!current()||op.controller.signal.aborted)return;
  const previous=codexCopies.get(mode);
  if(previous?.path!==path){
   if(mode==='activity'){$('activity-rows').replaceChildren();$('activity-summary').replaceChildren();$('activity-coverage').replaceChildren();$('activity-page').textContent='';$('activity-prev').disabled=$('activity-next').disabled=true;}
   else{for(const id of ['consumption-chart','consumption-summary','consumption-detail','consumption-legend','consumption-day'])$(id).replaceChildren();}
  }
  note.classList.add('warn-text');note.textContent=error.message+(previous?.path===path?' · 이전 화면을 유지합니다. 표시된 수집 시각을 확인하세요.':' · 이 조건의 데이터를 표시할 수 없습니다. 다시 불러오기를 이용하세요.');
 }).finally(()=>{if(codexReads.get(mode)===op)codexReads.delete(mode);});
 return op.promise;
}
document.querySelectorAll('[data-codex-retry]').forEach(b=>b.addEventListener('click',()=>loadCodexView(true)));
$('consumption-days').addEventListener('change',()=>loadCodexView(true));
$('consumption-group').addEventListener('change',()=>{if(lastConsumption)renderConsumption(lastConsumption);});
$('consumption-day').addEventListener('change',()=>{if(lastConsumption)renderConsumptionDetail(lastConsumption);});
for(const id of ['activity-period','activity-kind','activity-state','activity-start','activity-end'])$(id).addEventListener('change',()=>{
 activityPage=1;const custom=$('activity-period').value==='custom';$('activity-start-wrap').hidden=!custom;$('activity-end-wrap').hidden=!custom;void loadCodexView(true);
});
$('activity-search-form').addEventListener('submit',event=>{event.preventDefault();activityPage=1;void loadCodexView(true);});
$('activity-prev').addEventListener('click',()=>{if(activityPage>1){activityPage--;void loadCodexView(true);}});
$('activity-next').addEventListener('click',()=>{activityPage++;void loadCodexView(true);});
const activityToday=new Intl.DateTimeFormat('en-CA',{timeZone:'Asia/Seoul',year:'numeric',month:'2-digit',day:'2-digit'}).format(new Date());
$('activity-start').value=activityToday;$('activity-end').value=activityToday;

function renderConsumption(data){
 const rows=data.days||[],plan=data.plan||{},old=$('consumption-day').value;
 const notices=[data.missing_days?`미수신 ${data.missing_days}일`:null,plan.approximate===true?'제공사 근사값':null,
  plan.coverage_complete!==true?'제공 범위 일부 또는 미확인':null,plan.error?'플랜 이력 조회 실패':null].filter(Boolean);
 $('consumption-summary').innerHTML=`<span>수신 <strong>${rows.length}일</strong> / ${data.requested_days}일</span><span>분해 합계 차이 <strong>${rows.filter(d=>d.mismatch).length}일</strong></span>`
  +notices.map(n=>`<span class="consumption-caution">${esc(n)}</span>`).join('');
 $('consumption-day').innerHTML=rows.map(d=>`<option value="${esc(d.day)}">${esc(d.day)}</option>`).join('');
 $('consumption-day').value=rows.some(d=>d.day===old)?old:rows.at(-1)?.day||'';
 renderConsumptionChart(data);renderConsumptionDetail(data);
}
function renderConsumptionChart(data){
 const box=$('consumption-chart');if(!box.clientWidth)return;
 const rows=data.days||[],dimension=$('consumption-group').value;
 if(!rows.length){box.innerHTML='<p class="empty">이 기간의 한도 소비 데이터가 아직 없습니다. 상태 탭에서 수집 상태를 확인하세요.</p>';$('consumption-legend').replaceChildren();return;}
 const labels=[...new Set(rows.flatMap(d=>consumptionGroups(d,dimension).map(g=>g.label)))];
 $('consumption-legend').innerHTML=labels.map((label,i)=>`<span class="consumption-key"><i class="consumption-color color-${i%8}"></i>${esc(label)}</span>`).join('');
 const byDay=new Map(rows.map(d=>[d.day,d])),dates=[];
 for(let day=Date.parse(data.start+'T00:00:00Z'),end=Date.parse(data.end+'T00:00:00Z');day<=end;day+=86400000)dates.push(new Date(day).toISOString().slice(0,10));
 const maximum=Math.max(1,...rows.map(d=>consumptionGroups(d,dimension).reduce((sum,g)=>sum+g.value,0)));
 const width=Math.max(280,box.clientWidth),height=220,left=45,top=22,bottom=32,plot=height-top-bottom,step=(width-left-8)/dates.length;
 const tickEvery=Math.max(1,Math.ceil(dates.length/Math.max(3,Math.floor(width/65))));
 let svg=`<svg viewBox="0 0 ${width} ${height}" role="img" aria-label="일별 한도 소비율 · 제공사 기준일 · 단위 퍼센트"><text x="0" y="${top+5}" class="consumption-axis">${esc(consumptionPercent(maximum))}</text><text x="17" y="${height-bottom}" class="consumption-axis">0%</text><line x1="${left}" x2="${width-5}" y1="${height-bottom}" y2="${height-bottom}" stroke="var(--border)"/>`;
 dates.forEach((date,index)=>{
  const d=byDay.get(date),groups=d?consumptionGroups(d,dimension):[],x=left+index*step+step*.12,w=step*.76;
  let offset=0;
  if(d&&groups.length){
   svg+=`<g role="button" tabindex="0" data-consumption-day="${date}" aria-label="${date} 소비 분해 보기"><title>${date}\n${groups.map(g=>esc(g.label+' '+consumptionPercent(g.value))).join('\n')}</title><rect x="${x}" y="${top}" width="${w}" height="${plot}" fill="transparent"/>`;
   for(const group of groups){const h=group.value/maximum*plot;svg+=`<rect x="${x}" y="${height-bottom-offset-h}" width="${w}" height="${h}" fill="var(--chart-${labels.indexOf(group.label)%8})"/>`;offset+=h;}
   svg+='</g>';
  }else svg+=`<text x="${x+w/2}" y="${height-bottom-8}" text-anchor="middle" class="consumption-axis">—<title>${date} ${d?'이 분류의 값 미제공':'미수신'}</title></text>`;
  if(index%tickEvery===0||index===dates.length-1)svg+=`<text x="${Math.min(width-20,x+w/2)}" y="${height-9}" text-anchor="middle" class="consumption-axis">${date.slice(5)}</text>`;
 });
 box.innerHTML=svg+'</svg>';
}
function renderConsumptionDetail(data){
 const day=(data.days||[]).find(d=>d.day===$('consumption-day').value),box=$('consumption-detail');
 if(!day){box.innerHTML='<p class="empty">수신된 기준일을 선택하면 상세가 표시됩니다.</p>';return;}
 const dimension=$('consumption-group').value,groups=consumptionGroups(day,dimension),plan=data.plan||{};
 box.innerHTML=(day.mismatch?'<p class="warn-text">분해 합계가 일치하지 않습니다. 차이를 특정 기능에 배정하지 않았습니다.</p>':'')
  +(day.invalid_rows?`<p class="warn-text">해석하지 못한 분해 ${day.invalid_rows}행이 있습니다.</p>`:'')
  +`<p class="hint">원본 기준일 ${esc(day.day)} · 수집 ${esc(when(day.checked))} KST${plan.data_as_of?' · 플랜 이력 기준 '+esc(when(plan.data_as_of))+' KST':''}</p>`
  +`<div class="table-wrap"><table><thead><tr><th>${dimension==='model'?'모델':'기능'}</th><th>한도 소비율</th></tr></thead><tbody>${groups.map(g=>`<tr><td>${esc(g.label)}</td><td title="${esc(String(g.value))}%">${esc(consumptionPercent(g.value))}</td></tr>`).join('')||'<tr><td colspan="2">이 분류의 값 미제공</td></tr>'}</tbody></table></div>`
  +`<details class="consumption-raw"><summary>원본 분류와 합계 대조</summary><p class="hint">기능 ${esc(consumptionPercent(day.totals?.attribution))} · 모델 ${esc(consumptionPercent(day.totals?.models))} · 제품 화면 ${esc(consumptionPercent(day.totals?.surfaces))}. 서로 다른 분해를 더하지 않습니다.</p>`
  +`<div class="table-wrap"><table><thead><tr><th>출처 / 실행 계기</th><th>모델 / 화면</th><th>소비율</th></tr></thead><tbody>${(day.attribution||[]).map(r=>`<tr><td>${esc(r.thread_source)}<small>${esc(r.turn_trigger)}</small></td><td>${esc(r.model)}<small>${esc(r.surface)}</small></td><td>${esc(consumptionPercent(r.value))}</td></tr>`).join('')}</tbody></table></div></details>`;
}
$('consumption-chart').addEventListener('click',event=>{const day=event.target.closest('[data-consumption-day]');if(day){$('consumption-day').value=day.dataset.consumptionDay;renderConsumptionDetail(lastConsumption);}});
$('consumption-chart').addEventListener('keydown',event=>{if(['Enter',' '].includes(event.key)&&event.target.dataset.consumptionDay){event.preventDefault();event.target.dispatchEvent(new MouseEvent('click',{bubbles:true}));}});
new ResizeObserver(()=>{if(currentView==='usage'&&usageView==='consumption'&&lastConsumption)renderConsumptionChart(lastConsumption);}).observe($('consumption-chart'));

function consumptionBadge(status){return `<span class="consumption-badge">${esc(CONSUMPTION_STATUS[status]||'제공 상태 미확인')}</span>`;}
function taskConsumption(task){
 const c=task.consumption,p=c?.payload;
 return consumptionBadge(task.consumption_status)+(p?.weekly_limit_percent!=null?`<strong>${esc(consumptionPercent(p.weekly_limit_percent))}</strong><small>주간 한도 기준${c.stale?' · 오래된 집계':''}</small>`:task.thread_count>1?`<small>${task.thread_count}개 스레드 · 상세에서 확인</small>`:'');
}
function taskIdentity(task){
 const prs=task.prs||[];
 return prs.map(p=>`<a href="${esc(p.url)}" target="_blank" rel="noopener noreferrer">${esc(p.repository)} #${p.number}</a>`).join('<br>')
  +`<small><code>${esc(shortId(task.task_id))}</code></small>`;
}
function renderActivity(data){
 const opened=[...$('activity-rows').querySelectorAll('[data-task-id][aria-expanded="true"]')].map(b=>b.dataset.taskId);
 $('activity-summary').innerHTML=`<span>작업 <strong>${fmt(data.total)}개</strong></span><span>고유 PR <strong>${fmt(data.pr_count)}개</strong></span><span>생성일 기준 KST</span>`
  +(data.unknown_dates?`<span>시각 미확인 ${fmt(data.unknown_dates)}개 · 기간 선택에서 확인</span>`:'');
 const names={all:'일반 작업',archived:'보관 작업',code_review:'코드 리뷰',security_review:'보안 검토'};
 $('activity-coverage').innerHTML=Object.entries(data.coverage||{}).map(([key,c])=>`<p><strong>${esc(names[key]||key)}</strong> · ${c.complete?'끝 페이지 확인':'페이지 수집 중 / 미수집'}${c.oldest?' · 수집된 최초 생성 '+esc(when(c.oldest))+' KST':''}${c.checked?' · 목록 조회 '+esc(when(c.checked))+' KST':''}${c.error?' · 조회 실패 '+esc(c.error):''}</p>`).join('');
 for(const c of taskReads.values())c.abort();taskReads.clear();
 $('activity-rows').innerHTML=(data.items||[]).map(task=>`<tr><td class="activity-identity" data-label="작업">${taskIdentity(task)}</td><td data-label="종류">${esc(ACTIVITY_KINDS[task.kind]||'미분류')}</td><td data-label="생성 KST">${task.created?esc(when(task.created)):'시각 미확인'}</td><td data-label="실행 상태"><span class="execution-state ${esc(task.status)}">${esc(EXECUTION_NAMES[task.status]||'미확인')}</span></td><td data-label="소비 데이터">${taskConsumption(task)}</td><td class="activity-action"><button type="button" class="mini-btn" data-task-id="${task.id}" aria-expanded="false" aria-controls="task-detail-${task.id}">상세</button></td></tr>`).join('')||'<tr><td colspan="6" class="empty">선택 조건에서 수집된 작업이 없습니다. 수집 범위와 상태를 확인하세요.</td></tr>';
 $('activity-page').textContent=`${data.page} / ${Math.max(1,Math.ceil(data.total/data.page_size))} · ${fmt(data.total)}개`;
 $('activity-prev').disabled=data.page<=1;$('activity-next').disabled=data.page*data.page_size>=data.total;
 for(const key of opened)$('activity-rows').querySelector(`[data-task-id="${CSS.escape(key)}"]`)?.click();
}
function threadConsumption(thread){
 const p=thread.payload;
 const credits=value=>value==null?'미제공':consumptionNumber(value)===0?'0':esc(value);
 const values=payload=>`<dl class="consumption-values"><div><dt>주간 한도 기준</dt><dd>${esc(consumptionPercent(payload.weekly_limit_percent))}</dd></div><div><dt>5시간 한도 기준</dt><dd>${esc(consumptionPercent(payload.five_hour_limit_percent))}</dd></div><div><dt>사용 크레딧</dt><dd>${credits(payload.balance_usage_credits)}</dd></div></dl>`;
 return `<section class="thread-consumption"><h4>스레드 ${esc(shortId(thread.thread_id))} ${consumptionBadge(thread.status)}</h4>`
  +(thread.stale?'<p class="warn-text">오래된 집계입니다. 최근 수집 시각과 집계 기준 시각을 구분하세요.</p>':'')
  +(p?values(p)+`<p class="hint">수집 ${esc(when(p.checked))} KST · 원본 집계 기준 ${p.data_as_of?esc(when(p.data_as_of))+' KST':'미제공'} · 비교 플랜 ${esc(p.comparison_plan||'미제공')}</p>`
   +((p.invalid_fields||[]).length||(p.groups||[]).some(g=>g.invalid_fields?.length)?'<p class="warn-text">일부 소비 필드를 해석하지 못했습니다.</p>':'')
   +((p.groups||[]).length?`<div class="table-wrap"><table><thead><tr><th>소비 집계 모델 / 추론 / 속도</th><th>주간 기준</th><th>5시간 기준</th><th>크레딧</th></tr></thead><tbody>${p.groups.map(g=>`<tr><td>${esc(g.model||'모델 미확인')}<small>${esc(g.reasoning_effort||'미제공')} · ${esc(g.speed||'미제공')}</small></td><td>${esc(consumptionPercent(g.weekly_limit_percent))}</td><td>${esc(consumptionPercent(g.five_hour_limit_percent))}</td><td>${credits(g.balance_usage_credits)}</td></tr>`).join('')}</tbody></table></div>`:''):'<p class="hint">소비 조회 대기 중입니다.</p>')
  +(thread.previous&&(!p||p.weekly_limit_percent==null)?`<details><summary>이전 수신값 · ${esc(when(thread.previous.checked))} KST</summary>${values(thread.previous)}</details>`:'')+'</section>';
}
function renderTaskDetail(task){
 return `<p><strong>실제 실행 모델</strong> · ${task.models?.length?task.models.map(esc).join(', '):'미확인'} · 확인한 실행 ${task.run_count}개 · 스레드 ${task.thread_count}개</p>`
  +`<p class="hint">작업별 서버 수신값입니다. 소비 구간 미제공 · 목록의 생성일 기간 소비 합계가 아닙니다. 원본 입력·출력·캐시 토큰은 미제공입니다.</p>`
  +(task.detail_status==='error'?`<p class="warn-text">상세 수집 실패 (${esc(task.detail_error||'미확인')}) · 기존 연결 유지</p>`:task.detail_status!=='ok'?'<p class="hint">작업 상세 수집 대기 중입니다.</p>':'')
  +(task.threads||[]).map(threadConsumption).join('')
  +(!task.thread_count?'<p class="hint">연결된 실행 스레드가 없어 소비량을 조회하지 못했습니다. 소비 0을 의미하지 않습니다.</p>':'')
  +(task.url?`<a href="${esc(task.url)}" target="_blank" rel="noopener noreferrer">원본 작업 열기</a>`:'');
}
$('activity-rows').addEventListener('click',async event=>{
 const button=event.target.closest('[data-task-id]');if(!button)return;
 const key=button.dataset.taskId,existing=$('task-detail-'+key);
 if(existing){taskReads.get(key)?.abort();taskReads.delete(key);existing.closest('tr').remove();button.setAttribute('aria-expanded','false');return;}
 button.setAttribute('aria-expanded','true');button.closest('tr').insertAdjacentHTML('afterend',`<tr class="activity-detail-row"><td colspan="6"><div id="task-detail-${key}">불러오는 중…</div></td></tr>`);
 const controller=new AbortController();taskReads.set(key,controller);
 try{
  const result=await api('/api/codex/activity/'+encodeURIComponent(key),undefined,undefined,{signal:controller.signal});
  if(controller.signal.aborted||result.account_key!==lastActivity?.account_key)return;
  const host=$('task-detail-'+key);if(host)host.innerHTML=renderTaskDetail(result.task);
 }catch(error){if(!controller.signal.aborted&&$('task-detail-'+key))$('task-detail-'+key).textContent=error.message+' · 상세를 닫았다 다시 열어 재시도하세요.';}
 finally{if(taskReads.get(key)===controller)taskReads.delete(key);}
});
