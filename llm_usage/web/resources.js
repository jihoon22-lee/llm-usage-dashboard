'use strict';
const RESOURCE_STATES={fresh:'자동 조회',manual:'수동 확인',stale:'오래된 기록',error:'조회 실패',unavailable:'미제공',conflict:'출처 간 차이',expired:'만료됨',previous_account:'계정·조건 변경 후 재확인 필요'};
const RESOURCE_SCOPES={subscription:'구독 경로 추가 사용',cloud:'클라우드 세션 전용 · 로컬·채팅·API에 미적용',api:'API 전용 · 구독 한도에 미포함',five_hour:'5시간 한도',weekly:'주간 한도',model:'특정 모델',unknown:'적용 범위 확인 필요'};
let editingResource=null,resourceSaving=false,resourceDirty=false,resourceRequestId=null;
function resourceValue(row,value=row.amount){
 if(value==null)return '미제공';
 if(row.unit==='count')return fmt(value)+'회';
 if(row.unit==='credit')return fmt(value)+' 크레딧';
 if(row.unit==='minor')return fmt(value)+' · 통화·소액 단위 확인 필요';
 return fmt(value)+' '+row.unit;
}
function resourceConditions(row){
 const parts=[];
 const conditions=row.usage_conditions||row;
 if(row.allowance)parts.push('선불 잔액과 별도의 지출 제한');
 if(row.scope==='cloud'){
  if(row.limit!=null)parts.push('지급액 '+resourceValue(row,row.limit));
  if(row.spent!=null)parts.push('사용액 '+resourceValue(row,row.spent));
  parts.push('추가 사용 활성화와 별개 · 모델별 사용 조건 확인');
 }
 if(conditions.enabled===false)parts.push(row.kind==='reset'?'현재 제공 대상 아님·사용 조건 확인':row.scope==='cloud'?'클라우드 크레딧 사용 제한':'추가 사용 비활성화');
 else if(conditions.enabled===true)parts.push(row.kind==='reset'?'초기화권 제공 대상':'사용 활성화 확인');
 else parts.push('사용 조건 확인 필요');
 if(conditions.spend_remaining!=null&&!row.allowance)parts.push('남은 지출 한도 '+resourceValue(row,conditions.spend_remaining));
 if(conditions.spend_unlimited===true)parts.push('지출 제한 없음으로 기록됨');
 if(row.auto_reload===true)parts.push('자동 충전 켜짐 · 추가 결제 가능');
 else if(row.auto_reload===false)parts.push('자동 충전 꺼짐');
 if(row.unlimited)parts.push('제공사 크레딧 제한 없음 표시 · 다른 한도는 별도');
 return parts;
}
function resourceExpiry(expires,label='만료'){
 if(!Number.isFinite(expires)||expires<=0)return `<p class="resource-expiry unknown"><b>${esc(label)}</b><span>정보 미제공</span></p>`;
 const date=new Date(expires*1000);
 const display=date.toLocaleString('ko-KR',{timeZone:'Asia/Seoul',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hourCycle:'h23'});
 return `<p class="resource-expiry"><b>${esc(label)}</b><time datetime="${date.toISOString()}">${esc(display)} KST</time>${expires<=observedNow()?'<span class="resource-expired">시각 경과</span>':''}</p>`;
}
function resourceCard(row,opened){
 const checked=row.value_checked??row.checked;
 const grantTargets=[...new Set((row.grants||[]).flatMap(g=>g.targets||[]))];
 const scopeText=row.kind==='reset'&&grantTargets.length?'초기화 대상 · '+grantTargets.map(bucket=>bucketName({route:row.route,bucket})).join(' · '):RESOURCE_SCOPES[row.scope]||'적용 범위 확인 필요';
 const grants=(row.grants||[]).map((g,index)=>{
  const targets=(g.targets||[]).map(bucket=>bucketName({route:row.route,bucket})).join(' · ')||'초기화 대상 확인 필요';
  const eligibility=g.status==='used'?'사용됨':g.status==='expired'?'만료됨':g.paused?'일시 중지':g.usable===false?'현재 사용 조건 미충족':g.usable===true?'사용 조건 확인됨':'사용 조건 확인 필요';
  return `<li><b>초기화권 ${index+1} · ${esc(g.amount)}회</b>${resourceExpiry(g.expires)}<small>${esc(targets)} · ${esc(eligibility)}</small></li>`;
 }).join('');
 const partial=row.kind==='reset'&&row.details_known&&row.amount!=null&&row.amount>(row.grants||[]).reduce((sum,g)=>sum+g.amount,0);
 const expiry=grants?`<ul class="resource-grants" aria-label="초기화권별 만료일">${grants}</ul>${partial?'<p class="resource-note">일부 초기화권 상세만 제공되었습니다. 나머지 만료일은 미제공입니다.</p>':''}`:
  resourceExpiry(row.allowance?row.period_reset:row.expires,row.allowance?'지출 한도 갱신':row.expiry_is_partial?'일부 잔액 다음 만료':row.kind==='reset'&&!row.expires?'초기화권별 만료':'만료');
 const duplicate=row.duplicate_of?`<p class="resource-note">자동 자원과 연결 · 중복 합산하지 않음${row.conflicts_with_auto?' · 자동값과 기록이 다릅니다':''}${row.effective?' · 자동값 재확인 전 수동 기록 참고':''}</p>`:'';
 return `<article class="resource-card ${esc(row.status)}" data-resource-id="${esc(row.id)}"><div class="resource-card-heading"><span>${esc(shortService[row.route]||row.route)}</span><span class="badge ${esc(row.status)}">${esc(RESOURCE_STATES[row.status]||row.status)}</span></div><h3>${esc(row.label)}</h3><strong class="resource-value">${esc(resourceValue(row))}</strong>${expiry}<p class="resource-scope">${esc(scopeText)}${row.model?' · '+esc(row.model):''}</p><p class="resource-condition">${resourceConditions(row).map(esc).join(' · ')}</p>${duplicate}<small class="resource-checked">${checked?'확인 '+esc(when(checked))+' KST':'확인 기록 없음'}</small><details data-resource-detail="${esc(row.id)}"${opened.has(row.id)?' open':''}><summary>확인 근거·사용 조건</summary>${row.reported_total!=null&&row.reported_total!==row.amount?`<p>제공사 전체 잔액 ${esc(resourceValue(row,row.reported_total))} 중 범용 잔액만 위에 표시합니다.</p>`:''}${row.rate_per_hour!=null?`<p>최근 확인한 지출 ${esc(resourceValue(row,row.rate_per_hour))}/시간 · 같은 결제기간 원본 누적 지출 기준</p>`:''}${row.kind==='reset'&&!row.details_known&&row.origin==='auto'?'<p>초기화권별 적용 범위·만료 상세를 받지 못했습니다.</p>':''}<p>${row.origin==='manual'?(row.account_scope==='independent'?'별도 API 자원 수동 기록':'수동 확인 기록 · 현재 계정의 제공사 화면에서 재확인'):(row.identity_verified?'기본 로그인 계정 기준 자동 조회':'계정 식별 미확인 · 제공사에서 계정을 확인하세요.')}</p>${row.check_url?`<a href="${esc(row.check_url)}" target="_blank" rel="noopener noreferrer">제공사에서 확인 ↗</a>`:''}</details>${row.origin==='manual'?`<div class="resource-actions"><button type="button" class="mini-btn" data-resource-edit="${esc(row.id)}">수정</button><button type="button" class="mini-btn" data-resource-delete="${esc(row.id)}">삭제</button></div>`:''}</article>`;
}
function renderResources(data){
 const opened=new Set([...$('resource-panel').querySelectorAll('[data-resource-detail][open]')].map(el=>el.dataset.resourceDetail));
 const focus=document.activeElement?.closest('[data-resource-edit],[data-resource-delete]');
 const focused=focus?{kind:focus.hasAttribute('data-resource-edit')?'edit':'delete',id:focus.dataset.resourceEdit||focus.dataset.resourceDelete}:null;
 const items=data.resources?.items||[];
 const folded=r=>r.status==='previous_account'||r.status==='expired'||(r.amount===0&&['fresh','manual'].includes(r.status))||(r.duplicate_of&&!r.effective);
 const current=items.filter(r=>!folded(r)),older=items.filter(folded);
 $('resource-list').innerHTML=current.map(r=>resourceCard(r,opened)).join('')||'<p class="hint">현재 표시할 추가 잔액·초기화권이 없습니다. 확인된 0과 미수신 상태는 구분됩니다.</p>';
 $('resource-inactive').hidden=!older.length;
 $('resource-inactive-summary').textContent=`잔액 없음·만료·참고 기록 ${older.length}개`;
 $('resource-inactive-list').innerHTML=older.map(r=>resourceCard(r,opened)).join('');
 const failed=(data.sources||[]).filter(r=>['claude-credits','claude-resets','codex'].includes(r.name)&&['error','unavailable'].includes(r.status));
 $('resource-summary').textContent=data._offline?'오프라인 사본입니다. 잔액·초기화권은 연결 후 재확인하세요.':failed.length?'일부 자동 자원을 확인하지 못했습니다. 기록의 확인 시각과 수집 상태를 확인하세요.':'크레딧·초기화권의 단위와 적용 범위를 구분합니다. 조회·기록만 하며 사용·구매는 실행하지 않습니다.';
 $('resource-save').disabled=resourceSaving||data._offline;
 if(focused)$('resource-panel').querySelector(`[data-resource-${focused.kind}="${focused.id}"]`)?.focus({preventScroll:true});
}
const resourceTime=t=>t==null?'':new Date(t*1000+9*3600e3).toISOString().slice(0,16);
const resourceTimestamp=value=>value?Date.parse(value+(value.length===16?':00':'')+'+09:00')/1000:null;
const booleanInput=id=>$(id).value===''?null:$(id).value==='true';
function resourceLinks(selected=''){
 const route=$('resource-route').value,kind=$('resource-kind').value;
 const items=(lastLimits?.resources?.items||[]).filter(r=>r.origin==='auto'&&r.active_account&&r.route===route&&r.kind===kind);
 $('resource-linked').innerHTML='<option value="">별도 자원</option>'+items.map(r=>`<option value="${esc(r.id)}">${esc(r.label)} · ${esc(resourceValue(r))}</option>`).join('');
 $('resource-linked').value=items.some(r=>r.id===selected)?selected:'';
}
function editResource(row=null){
 if(resourceSaving)return;
 editingResource=row?{id:row.id,revision:row.revision}:null;
 resourceRequestId=crypto.randomUUID().replaceAll('-','');
 $('resource-editor').reset();$('resource-editor').hidden=false;
 $('resource-editor-title').textContent=row?'수동 기록 수정':'수동 자원 기록';
 const defaults={route:planPrefs.route||'codex',kind:'usage_credit',label:'',amount:'',unit:'credit',scope:'subscription',model:'',checked:observedNow(),expires:null};
 const values={...defaults,...row};
 for(const name of ['route','kind','label','amount','unit','scope','model'])$('resource-'+name).value=values[name]??'';
 $('resource-checked').value=resourceTime(values.checked);$('resource-expires').value=resourceTime(values.expires);
 for(const [id,key] of [['resource-enabled','enabled'],['resource-auto-reload','auto_reload'],['resource-unlimited','spend_unlimited']])$(id).value=values[key]==null?'':String(values[key]);
 $('resource-spend').value=values.spend_remaining??'';
 resourceLinks(values.linked_id||'');resourceDirty=false;
 $('resource-message').textContent=lastLimits?._offline?'오프라인에서는 초안만 작성할 수 있습니다. 연결 후 저장하세요.':'';
 $('resource-reload').hidden=true;$('resource-label').focus();
 $('resource-editor').scrollIntoView({behavior:motion(),block:'nearest'});
}
function resourceBody(){
 return {route:$('resource-route').value,kind:$('resource-kind').value,label:$('resource-label').value,
  amount:Number($('resource-amount').value),unit:$('resource-unit').value,scope:$('resource-scope').value,
  model:$('resource-model').value,checked:resourceTimestamp($('resource-checked').value),expires:resourceTimestamp($('resource-expires').value),
  enabled:booleanInput('resource-enabled'),auto_reload:booleanInput('resource-auto-reload'),spend_unlimited:booleanInput('resource-unlimited'),
  spend_remaining:$('resource-spend').value===''?null:Number($('resource-spend').value),linked_id:$('resource-linked').value||null,
  ...(editingResource?{revision:editingResource.revision}:{request_id:resourceRequestId})};
}
$('resource-add').addEventListener('click',()=>{
 if(resourceDirty&&!confirm('저장하지 않은 입력을 지우고 새 기록을 작성할까요?'))return;
 editResource();
});
$('resource-cancel').addEventListener('click',()=>{
 if(resourceSaving)return;
 if(resourceDirty&&!confirm('저장하지 않은 입력을 닫을까요?'))return;
 $('resource-editor').hidden=true;resourceDirty=false;$('resource-add').focus();
});
$('resource-editor').addEventListener('input',()=>{resourceDirty=true;});
$('resource-kind').addEventListener('change',()=>{
 const kind=$('resource-kind').value;
 $('resource-unit').value=kind==='reset'?'count':kind==='api_credit'?'USD':'credit';
 $('resource-scope').value=kind==='reset'?'unknown':kind==='api_credit'?'api':'subscription';resourceLinks();
});
$('resource-route').addEventListener('change',()=>resourceLinks());
$('resource-linked').addEventListener('change',()=>{
 const row=lastLimits?.resources?.items.find(r=>r.id===$('resource-linked').value);
 if(row){$('resource-unit').value=row.unit;$('resource-scope').value=row.scope;}
});
$('resource-editor').addEventListener('submit',async event=>{
 event.preventDefault();if(resourceSaving||lastLimits?._offline)return;
 resourceSaving=true;$('resource-save').disabled=true;$('resource-message').textContent='저장 중…';
 const editing=editingResource?{...editingResource}:null,body=resourceBody();
 const controls=[...$('resource-editor').querySelectorAll('input,select,button')];controls.forEach(el=>el.disabled=true);
 try{
  const result=await api('/api/resources/manual'+(editing?'/'+editing.id:''),body,undefined,{method:editing?'PATCH':'POST'});
  editingResource={id:result.id,revision:result.revision};resourceDirty=false;
  $('resource-message').textContent='기록을 저장했습니다.';$('resource-reload').hidden=true;
  await loadWorkPlan();
 }catch(error){
  $('resource-message').textContent=error.message+' 입력 내용은 유지됩니다.';
  $('resource-reload').hidden=error.status!==409;
 }finally{resourceSaving=false;controls.forEach(el=>el.disabled=false);$('resource-save').disabled=!!lastLimits?._offline;}
});
$('resource-reload').addEventListener('click',async()=>{
 const id=editingResource?.id||resourceRequestId;if(!id)return;
 await loadWorkPlan();
 const row=lastLimits?.resources?.items.find(r=>r.id===id);
 if(row)editResource(row);else $('resource-message').textContent='최신 기록을 확인하지 못했습니다. 입력은 유지됩니다.';
});
$('resource-panel').addEventListener('click',async event=>{
 const edit=event.target.closest('[data-resource-edit]'),remove=event.target.closest('[data-resource-delete]');
 const id=edit?.dataset.resourceEdit||remove?.dataset.resourceDelete;if(!id)return;
 const row=lastLimits?.resources?.items.find(r=>r.id===id);if(!row)return;
 if(edit){if(resourceDirty&&!confirm('저장하지 않은 입력을 지우고 이 기록을 수정할까요?'))return;editResource(row);return;}
 if(lastLimits?._offline||!confirm('이 수동 기록을 삭제할까요? 제공사의 실제 자원에는 영향이 없습니다.'))return;
 remove.disabled=true;
 try{await api('/api/resources/manual/'+id,{revision:row.revision},undefined,{method:'DELETE'});await loadWorkPlan();}
 catch(error){$('resource-summary').textContent=error.message;remove.disabled=false;}
});
