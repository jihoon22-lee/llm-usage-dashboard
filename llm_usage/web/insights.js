'use strict';
// Insights: subscription value, sessions, reports, projects, calendar and cache trend.
function renderInsights(data){
 const subs=data.subscriptions||[];
 const month=data.insights&&data.insights.month;
 const monthLine=month?`<div class="sub-row"><strong>이번 달 환산</strong><span>${month.days_in_month}일 중 ${month.days_elapsed}일 경과 · 공개 단가 기준 리스트 가치</span><strong>누적 ${usd(month.total)} · 월말 예상 ${usd(month.projected)}</strong></div>`:'';
 const monthAlert=month&&month.alert_usd!=null&&month.projected>month.alert_usd
  ?`<p class="hint warn-text">월말 예상 환산 가치가 알림 기준 ${usd(month.alert_usd)}를 초과합니다.</p>`:'';
 // Ratios span 1x to 100x+, so bars use a log scale with the 1x break-even marked.
 const ratios=subs.map(s=>s.monthly_usd>0&&s.monthly_equiv!=null?s.monthly_equiv/s.monthly_usd:null).filter(v=>v!=null);
 const top=Math.max(10,...ratios);
 const bar=ratio=>{const pos=v=>Math.max(0,Math.min(100,Math.log10(Math.max(v,.1))+1)/(Math.log10(top)+1)*100);
  const ticks=[1,10,100,1000].filter(v=>v<=top).map(v=>`<em data-x="${pos(v).toFixed(1)}">${v}배</em>`).join('');
  return `<span class="value-bar${ratio<1?' under':''}" role="img" aria-label="구독료 대비 ${ratio.toFixed(1)}배 · 로그 눈금"><i data-w="${pos(ratio).toFixed(1)}"></i><b data-x="${pos(1).toFixed(1)}" title="본전(1배)"></b>${ticks}</span>`;};
 $('subvalue').innerHTML=monthLine+monthAlert+(subs.length?subs.map(s=>{
  const ratio=s.monthly_usd>0&&s.monthly_equiv!=null?s.monthly_equiv/s.monthly_usd:null;
  return `<div class="sub-row${ratio!=null?' with-bar':''}"><strong>${esc(routeNames[s.route]||s.route)}</strong><span>${s.period_cost!=null?`기간 환산 ${usd(s.period_cost)}`:'기간 내 미산정'} · 월 구독료 ${s.monthly_usd?usd(s.monthly_usd):'무료'}</span><strong>${s.monthly_usd===0?'무료 프리뷰':ratio!=null?`월 환산 ${usd(s.monthly_equiv)} · ${ratio.toFixed(1)}배`:'—'}</strong>${ratio!=null?bar(ratio):''}</div>`;
 }).join(''):month?'':'<p class="hint">구독료가 설정되지 않았습니다.</p>');
 applyGeometry($('subvalue'));
 renderCalendar(data);renderSessions(data);renderProjects(data);
 const insights=data.insights;
 if(insights===undefined){
  // Section not fetched yet — keep the prior render and show progress on first paint.
  if(!$('comparison').textContent.trim())for(const id of ['quality','comparison','cache-overview'])$(id).textContent='불러오는 중…';
  return;
 }
 if(!insights){
  for(const id of ['quality','comparison','cache-overview'])$(id).textContent='분석 데이터 수신 대기';
  $('cache-rows').innerHTML='';$('cache-series').innerHTML='';$('cache-trend').innerHTML='';return;
 }
 const q=insights.quality;
 // Each figure says whether it needs attention or is already handled by the aggregation.
 const qualityRows=[
  ['기록된 해석 실패',fmt(q.import_errors)+'건',q.import_errors>0?'warn':'ok',q.import_errors>0?'읽지 못한 줄이 있습니다 · 수집 상태에서 원본을 확인하세요':'모든 기록 줄을 해석했습니다'],
  ['모델 미확인 토큰 비중',percent(q.unknown_model_percent),q.unknown_model_percent>1?'warn':'ok','모델명이 없는 기록은 unknown으로 따로 집계합니다'],
  ['카운터 초기화 세션',fmt(q.reset_sessions)+'개','info','누적 카운터가 다시 시작된 세션 · 구간을 나눠 집계해 합계에 영향 없음'],
  ['불완전 시작 세션',fmt(q.partial_sessions)+'개','info','기록 시작 전 사용분은 알 수 없어 관측 이후만 집계'],
  ['시각·카운터 불일치 세션',fmt(q.ambiguous_sessions)+'개','info','순서가 엇갈린 관측 · 큰 값을 중복 없이 보수적으로 처리'],
  ['0 요청 기준값 전환 세션',fmt(q.zero_baseline_sessions)+'개','info','요청 없이 기준값만 바뀐 경우 · 사용량에 더하지 않음']];
 $('quality').innerHTML=`<div class="quality-stats">${qualityRows.map(([label,value,level,note])=>`<div class="q-${level}"><span>${label}</span><strong>${value}</strong><small>${esc(note)}</small></div>`).join('')}</div><p class="hint">${esc(q.scope)} Codex 세션 ${fmt(q.sessions)}개 진단 / ${fmt(q.sessions_observed)}개 관측.</p><details><summary>수집원별 마지막 성공</summary>${q.last_successes.map(s=>`<p class="hint"><strong>${esc(s.name)}</strong> · ${esc(when(s.last_success))} KST</p>`).join('')||'<p>성공 기록이 아직 없습니다.</p>'}</details>`;
 const c=insights.comparison;
 if(c.current==null)$('comparison').innerHTML=`<p>${esc(c.reason)}</p>`;
 else{
  const change=c.percent==null?'증감률 계산 불가':`${c.percent>0?'+':''}${c.percent.toFixed(1)}%`;
  $('comparison').innerHTML=`<div class="comparison-value">${esc(change)}<small>관측 토큰 차이 ${signed(c.delta)}</small></div><p class="hint">현재 ${esc(c.start.slice(0,16).replace('T',' '))} → ${esc(c.end_exclusive.slice(0,16).replace('T',' '))}<br>이전 ${esc(c.previous_start.slice(0,16).replace('T',' '))} → ${esc(c.previous_end_exclusive.slice(0,16).replace('T',' '))} KST · 종료 미포함</p><p class="hint">${esc(c.reason)}${c.previous===0?' 이전 관측값이 0이면 증감률을 계산하지 않습니다.':''}</p><div class="contributions">${c.contributions.filter(r=>r.delta!==0).slice(0,6).map(r=>`<div><span>${esc(r.model)}<small>${esc(routeNames[r.route]||r.route)}</small></span><strong>${signed(r.delta)}</strong></div>`).join('')||'<p class="hint">관측 토큰 차이가 없습니다.</p>'}</div><p class="hint">캐시 생성 포함 · 변화량 절댓값 상위 6개</p>`;
 }
 const cache=insights.cache;
 const saved=data.cost&&data.cost.cache_savings;
 $('cache-overview').innerHTML=`<div class="comparison-value">${percent(cache.rate)}<small>선택 기간 입력 중 캐시 읽기 비중</small></div>${saved?`<p class="hint">캐시 읽기를 일반 입력 단가로 냈다면 ${usd(saved)} 더 들었을 환산입니다(공개 단가 차이 · 실제 청구 아님).</p>`:''}`;
 const selected=$('cache-series').value;
 $('cache-series').innerHTML=data.labels.map(label=>`<option value="${esc(label)}">${esc(seriesLabel(label))}</option>`).join('');
 if(data.labels.includes(selected))$('cache-series').value=selected;
 $('cache-rows').innerHTML=`<table><thead><tr><th>사용 서비스</th><th>모델</th><th>캐시 활용률</th></tr></thead><tbody>${cache.rows.map(r=>`<tr><td>${esc(routeNames[r.route]||r.route)}</td><td>${esc(r.model)}</td><td>${percent(r.rate)}</td></tr>`).join('')}</tbody></table>`;
 renderCacheTrend(data);
}
const sessSort={key:'first',dir:'desc'};let sessPage=0;
const SESS_SORTS={first:s=>s.first_ts,requests:s=>s.requests||0,tokens:s=>s.uncached_input+s.cached_input+s.output+s.cache_creation,cost:s=>s.cost??-1};
function renderSessions(data){
 document.querySelectorAll('[data-ssort]').forEach(button=>{
  const on=button.dataset.ssort===sessSort.key;
  button.closest('th').setAttribute('aria-sort',on?(sessSort.dir==='desc'?'descending':'ascending'):'none');
  const marker=button.querySelector('span');if(marker)marker.textContent=on?(sessSort.dir==='desc'?'▼':'▲'):'↕';
 });
 if(!('sessions' in data)){if(!$('sessions').textContent.trim())$('sessions').innerHTML='<tr><td colspan="8" class="empty">불러오는 중…</td></tr>';return;}
 const list=data.sessions||[];
 const val=SESS_SORTS[sessSort.key]||SESS_SORTS.first;
 const sorted=[...list].sort((a,b)=>(val(a)-val(b))*(sessSort.dir==='desc'?-1:1)||String(a.session).localeCompare(String(b.session)));
 const pages=Math.max(1,Math.ceil(sorted.length/20));sessPage=Math.min(sessPage,pages-1);
 $('sessions').innerHTML=sorted.slice(sessPage*20,sessPage*20+20).map(s=>{
  const span=when(s.first_ts)+(s.last_ts>s.first_ts+60?' ~ '+when(s.last_ts):'');
  return `<tr><td><span class="svc-full">${esc(routeNames[s.route]||s.route)}</span><span class="svc-short">${esc(shortService[s.route]||s.route)}</span></td><td><code>${esc(String(s.session).slice(0,8))}</code> <button type="button" class="sess-copy mini-btn" data-id="${esc(String(s.session))}">복사</button> <button type="button" class="sess-open mini-btn" data-id="${esc(String(s.session))}" aria-expanded="${openSession===String(s.session)}">상세</button></td><td class="col-detail">${esc(s.project||'—')}</td><td class="col-detail">${esc(KIND_NAMES[s.kind]||s.kind||'—')}</td><td class="col-detail">${esc(span)}</td><td class="col-detail">${fmt(s.requests)}</td><td>${fmt(s.uncached_input+s.cached_input+s.output+s.cache_creation)}</td><td>${s.cost!=null?usd(s.cost):'—'}</td></tr>`;
 }).join('')||'<tr><td colspan="8" class="empty">선택 기간에 세션 귀속된 기록이 없습니다.</td></tr>';
 const sessTotal=data.sessions_total??sorted.length;
 $('sess-page').textContent=sorted.length?`${sessPage+1}/${pages} · ${sessTotal>100?`전체 ${fmt(sessTotal)}개 중 상위 100개`:`${fmt(sessTotal)}개`}`:'';
 $('sess-prev').disabled=sessPage<=0;$('sess-next').disabled=sessPage>=pages-1;
 $('sessions-note').textContent=(data.sessionless_requests?`세션 미귀속 요청 ${fmt(data.sessionless_requests)}건(세션 정보가 없는 기록). `:'')+'Antigravity는 대화 파일 단위로 묶은 해시 ID이며 프로젝트는 기록되지 않습니다. '
  +'세션 ID는 앞 8자만 표시하고 복사 버튼으로 전체를 가져올 수 있습니다. 상세는 모델별 사용량과 시간대별 흐름입니다. 대화 내용은 저장하지 않습니다.';
 // A refresh redraws the table; keep an opened session's detail under its row.
 const opened=openSession&&$('sessions').querySelector(`.sess-open[data-id="${CSS.escape(openSession)}"]`);
 if(opened&&sessionDetails.has(openSession))opened.closest('tr').insertAdjacentHTML('afterend',sessionDetails.get(openSession));
}
let openSession=null;const sessionDetails=new Map();
let reportsLoaded=0;
async function loadReports(){
 if(Date.now()-reportsLoaded<300000)return;
 try{const d=await api('/api/reports');reportsLoaded=Date.now();renderReports(d.reports||[]);}
 catch(e){$('reports').innerHTML=`<p class="hint">주간 리포트를 불러오지 못했습니다 (${esc(e.message)})</p>`;}
}
function reportHtml(r){
 return `<p class="report-head"><strong>${esc(r.start)} ~ ${esc(r.end)}</strong> · ${compact(r.totals.tokens)} 토큰 · ${fmt(r.totals.requests)}회 · 전주 대비 ${pctChange(r.change_pct)}${r.totals.cost!=null?' · 공개 단가 환산 '+usd(r.totals.cost):''}${r.busiest_day?' · 가장 많이 쓴 날 '+esc(r.busiest_day):''}</p>`
  +`<div class="table-wrap"><table class="report-table"><thead><tr><th>서비스</th><th>토큰</th><th>요청</th><th>전주 대비</th><th>환산</th></tr></thead><tbody>${r.routes.map(x=>`<tr><td>${esc(routeNames[x.route]||x.route)}</td><td>${compact(x.tokens)}</td><td>${fmt(x.requests)}</td><td>${pctChange(x.change_pct)}</td><td>${usd(x.cost)}</td></tr>`).join('')||'<tr><td colspan="5" class="empty">기록 없음</td></tr>'}</tbody></table></div>`
  +`<p class="hint">상위 모델: ${r.models.map(m=>`${esc(m.model)} ${compact(m.tokens)}`).join(' · ')||'—'}</p>`
  +`<p class="hint">${r.exhausted.length?'한도 소진: '+r.exhausted.map(e=>`${esc(quotaLabel({route:e.route,bucket:e.bucket,display_name:e.display_name}))} ${e.count}회`).join(' · '):'이 주에는 한도 소진이 관측되지 않았습니다.'}</p>`;
}
function renderReports(list){
 $('reports').innerHTML=list.length?reportHtml(list[0])+(list.length>1?`<details class="report-older"><summary>이전 리포트 ${list.length-1}개</summary>${list.slice(1).map(r=>`<details><summary>${esc(r.start)} ~ ${esc(r.end)} · ${compact(r.totals.tokens)} 토큰 · 전주 대비 ${pctChange(r.change_pct)}</summary>${reportHtml(r)}</details>`).join('')}</details>`:'')
  :'<p class="hint">첫 리포트는 수집기가 다음 수집 주기에 지난주 기록으로 작성합니다.</p>';
}
function sessionDetailHtml(d){
 if(!d.models.length)return '<tr class="sess-detail-row"><td colspan="8"><p class="hint">이 세션의 기록이 더 이상 없습니다.</p></td></tr>';
 const peak=Math.max(1,...d.hours.map(h=>h.tokens));
 const bars=d.hours.map((h,i)=>{const bh=Math.max(1,h.tokens/peak*36);return `<rect x="${i*8}" y="${40-bh}" width="6" height="${bh}" data-tip="${esc(when(h.hour))} KST · ${fmt(h.tokens)} 토큰 · ${fmt(h.requests)}회"></rect>`;}).join('');
 return `<tr class="sess-detail-row"><td colspan="8"><div class="sess-detail"><p><strong>${esc(d.project||'프로젝트 미분류')}</strong> · ${esc(KIND_NAMES[d.kind]||d.kind||'—')} · ${esc(when(d.first_ts))} ~ ${esc(when(d.last_ts))} KST · ${fmt(d.requests)}회 · 캐시 읽기 비중 ${percent(d.cache_share)} · 비용 추정 ${usd(d.cost)}</p>`
  +`<svg class="sess-hours" viewBox="0 0 ${Math.max(8,d.hours.length*8)} 40" preserveAspectRatio="none" role="img" aria-label="시간대별 토큰 ${d.hours.length}시간">${bars}</svg>`
  +`<table class="sess-models"><thead><tr><th>모델</th><th>경로</th><th>요청</th><th>합계 토큰</th><th>비용 추정</th></tr></thead><tbody>${d.models.map(m=>`<tr><td>${esc(m.model)}</td><td>${esc(shortService[m.route]||m.route)}</td><td>${fmt(m.requests)}</td><td>${fmt(m.uncached_input+m.cached_input+m.output+m.cache_creation)}</td><td>${usd(m.cost)}</td></tr>`).join('')}</tbody></table></div></td></tr>`;
}
$('sessions').addEventListener('click',async event=>{
 const button=event.target.closest('.sess-open');if(!button)return;
 const id=button.dataset.id,row=button.closest('tr');
 $('sessions').querySelectorAll('.sess-detail-row').forEach(el=>el.remove());
 $('sessions').querySelectorAll('.sess-open').forEach(b=>b.setAttribute('aria-expanded','false'));
 // Closing forgets the detail, so reopening a session that is still running reloads it.
 if(openSession===id){openSession=null;sessionDetails.delete(id);return;}
 if(openSession)sessionDetails.delete(openSession);
 openSession=id;button.setAttribute('aria-expanded','true');
 try{
  if(!sessionDetails.has(id))sessionDetails.set(id,sessionDetailHtml(await api('/api/session?id='+encodeURIComponent(id))));
  if(openSession===id&&row.isConnected)row.insertAdjacentHTML('afterend',sessionDetails.get(id));
 }catch(e){row.insertAdjacentHTML('afterend',`<tr class="sess-detail-row"><td colspan="8"><p class="hint">상세를 불러오지 못했습니다 (${esc(e.message)})</p></td></tr>`);}
});
$('sess-prev').addEventListener('click',()=>{sessPage--;if(lastUsage)renderSessions(lastUsage);});
$('sess-next').addEventListener('click',()=>{sessPage++;if(lastUsage)renderSessions(lastUsage);});
document.querySelectorAll('[data-ssort]').forEach(button=>button.addEventListener('click',()=>{
 sessSort.dir=sessSort.key===button.dataset.ssort&&sessSort.dir==='desc'?'asc':'desc';sessSort.key=button.dataset.ssort;sessPage=0;
 if(lastUsage)renderSessions(lastUsage);
}));
$('sessions').addEventListener('click',event=>{
 const button=event.target.closest('.sess-copy');if(!button)return;
 navigator.clipboard?.writeText(button.dataset.id).then(()=>{button.textContent='복사됨';setTimeout(()=>{button.textContent='복사';},1500);}).catch(()=>{button.textContent='실패';});
});
let openProject=null;const projectDetails=new Map();
function projectDetailHtml(d){
 const m=d.month,peak=Math.max(1,...d.daily.map(x=>x.tokens));
 const bars=d.daily.map((x,i)=>{const h=Math.max(1,x.tokens/peak*36);return `<rect x="${i*8}" y="${40-h}" width="6" height="${h}" data-tip="${esc(x.day)} · ${fmt(x.tokens)} 토큰 · ${fmt(x.requests)}회"></rect>`;}).join('');
 const budget=m.budget?` · 예산 ${Math.round(m.budget_ratio*100)}% 사용 → 월말 예상 <strong class="${m.projected_budget_ratio>=1?'warn-text':''}">${Math.round(m.projected_budget_ratio*100)}%</strong>`:'';
 return `<tr class="sess-detail-row"><td colspan="7"><div class="sess-detail"><p><strong>${esc(d.project)}</strong> · 이번 달(${esc(m.month)}) ${m.days_in_month}일 중 ${m.days_elapsed}일 · ${compact(m.tokens)} 토큰${m.cost!=null?' · '+usd(m.cost):''} → 월말 예상 ${compact(m.projected_tokens)} 토큰${m.projected_cost!=null?' · '+usd(m.projected_cost):''}${budget}</p>`
  +`<svg class="sess-hours" viewBox="0 0 ${Math.max(8,d.daily.length*8)} 40" preserveAspectRatio="none" role="img" aria-label="최근 ${d.days}일 일별 토큰">${bars}</svg><p class="hint">최근 ${d.days}일 일별 토큰 · 월말 예상은 경과 일수 기준 단순 외삽</p>`
  +`<table class="sess-models"><thead><tr><th>모델</th><th>경로</th><th>요청</th><th>토큰</th><th>비용 추정</th></tr></thead><tbody>${d.models.map(x=>`<tr><td>${esc(x.model)}</td><td>${esc(shortService[x.route]||x.route)}</td><td>${fmt(x.requests)}</td><td>${fmt(x.tokens)}</td><td>${usd(x.cost)}</td></tr>`).join('')}</tbody></table>`
  +(d.sessions.length?`<p class="hint">많이 쓴 세션: ${d.sessions.map(x=>`<code>${esc(String(x.session).slice(0,8))}</code> ${compact(x.tokens)}`).join(' · ')}</p>`:'')+'</div></td></tr>';
}
$('projects').addEventListener('click',async event=>{
 const button=event.target.closest('.proj-open');if(!button)return;
 event.stopPropagation();
 const name=button.dataset.project,row=button.closest('tr');
 $('projects').querySelectorAll('.sess-detail-row').forEach(el=>el.remove());
 $('projects').querySelectorAll('.proj-open').forEach(b=>b.setAttribute('aria-expanded','false'));
 if(openProject===name){openProject=null;projectDetails.delete(name);return;}
 openProject=name;button.setAttribute('aria-expanded','true');
 try{
  if(!projectDetails.has(name))projectDetails.set(name,projectDetailHtml(await api('/api/project?name='+encodeURIComponent(name))));
  if(openProject===name&&row.isConnected)row.insertAdjacentHTML('afterend',projectDetails.get(name));
 }catch(e){row.insertAdjacentHTML('afterend',`<tr class="sess-detail-row"><td colspan="7"><p class="hint">상세를 불러오지 못했습니다 (${esc(e.message)})</p></td></tr>`);}
});
function renderProjects(data){
 if(!('projects' in data)){if(!$('projects').textContent.trim())$('projects').innerHTML='<tr><td colspan="7" class="empty">불러오는 중…</td></tr>';return;}
 const list=data.projects||[];
 const budgets=data.project_budgets||{},month=data.project_month;
 const budgetLine=name=>{const b=budgets[name];if(!b||!month)return '';const st=budgetState(month.projects[name],b);
  return `<small class="budget-line">이번 달 예산 ${Math.round(st.ratio*100)}% · ${esc(st.text)}</small><span class="budget-bar ${st.level}"><i data-w="${Math.min(100,st.ratio*100).toFixed(1)}"></i></span>`;};
 $('projects').innerHTML=list.map(p=>`<tr data-scope="project:${esc(p.project)}" tabindex="0" aria-describedby="drill-hint"><td><strong>${esc(p.project)}</strong>${budgetLine(p.project)}</td><td class="col-detail">${p.sessions?fmt(p.sessions):'—'}</td><td>${fmt(p.requests)}</td><td>${fmt(p.uncached_input+p.cached_input+p.output+p.cache_creation)}</td><td>${p.cost!=null?usd(p.cost):'—'}</td><td class="col-detail">${esc(when(p.last_ts))}</td><td><button type="button" class="mini-btn proj-open" data-project="${esc(p.project)}" aria-expanded="${openProject===p.project}">상세</button></td></tr>`).join('')
  ||'<tr><td colspan="7" class="empty">선택 기간에 수집된 사용 기록이 없습니다.</td></tr>';
 const opened=openProject&&$('projects').querySelector(`.proj-open[data-project="${CSS.escape(openProject)}"]`);
 if(opened&&projectDetails.has(openProject))opened.closest('tr').insertAdjacentHTML('afterend',projectDetails.get(openProject));
 applyGeometry($('projects'));
}
const cellMetric=row=>{const m=$('metric').value;return m==='requests'?row.requests||0:m==='cost'?(row.cost==null?null:row.cost):row.tokens||0;};
// Summary for the shown weeks, in the selected metric. Unrated days
// (cost==null) are not active days for cost and get their own count.
function calStats(rows,weeks){
 const vals=rows.map(r=>({day:r.day,v:cellMetric(r)}));
 const rated=vals.filter(r=>r.v!=null),active=rated.filter(r=>r.v!==0);
 const sum=rated.reduce((s,r)=>s+r.v,0),costMode=$('metric').value==='cost';
 let streak=0,run=0,prev=null;
 for(const r of vals){
  if(r.v==null||r.v===0){run=0;prev=null;continue;}
  const t=Date.parse(r.day+'T00:00:00Z');
  run=prev!=null&&t-prev===864e5?run+1:1;prev=t;streak=Math.max(streak,run);
 }
 const peak=active.reduce((a,r)=>a&&a.v>=r.v?a:r,null);
 const items=[[weeks+'주 합계',metricFmt(sum)+metricUnit()],
  ['활동일',fmt(active.length)+'일'],
  ['최장 연속 활동',fmt(streak)+'일'],
  ['활동일 평균',active.length?metricFmt(costMode?sum/active.length:Math.round(sum/active.length))+metricUnit():'—'],
  ['최대일',peak?peak.day.slice(5)+' · '+metricFmt(peak.v)+metricUnit():'—']];
 if(costMode&&vals.length>rated.length)items.push(['미산정',fmt(vals.length-rated.length)+'일']);
 return '<dl class="cal-stats">'+items.map(([k,v])=>`<div><dt>${k}</dt><dd>${v}</dd></div>`).join('')+'</dl>';
}
// Wide panels show up to 26 weeks (the API sends 26) instead of leaving the row empty;
// narrow ones keep 16 weeks of larger cells.
let calWeeks=16;
const calendarWeeks=()=>{const w=$('calendar').clientWidth||0;return w>=850?Math.max(16,Math.min(26,Math.floor((w-248)/26))):16;};
// Cell size follows the space left next to the stats column (Q2).
function fitCalendar(){
 const scroll=$('calendar').querySelector('.cal-scroll');
 if(!scroll)return;
 const w=scroll.clientWidth;
 if(!w)return;
 const cell=Math.max(11,Math.min(30,Math.floor((w-48)/calWeeks)));
 scroll.querySelectorAll('.cal-months,.cal-grid').forEach(el=>{el.style.setProperty('--cal-cell',cell+'px');el.style.setProperty('--cal-weeks',calWeeks);});
}
function renderCalendar(data){
 calWeeks=calendarWeeks();
 $('calendar-caption').textContent=`최근 ${calWeeks}주 · 일별 `+metricName()+' · 기간 필터 무관';
 if(!('calendar' in data)){if(!$('calendar').textContent.trim())$('calendar').innerHTML='<div class="empty">불러오는 중…</div>';return;}
 const days=Object.fromEntries((data.calendar||[]).map(r=>[r.day,r]));
 const today=new Date(new Date().toLocaleString('en-US',{timeZone:'Asia/Seoul'}));
 const dow=(today.getDay()+6)%7,first=new Date(today);first.setDate(today.getDate()-dow-(calWeeks-1)*7);
 const cells=[],months=[];let max=0;
 for(let w=0;w<calWeeks;w++)for(let r=0;r<7;r++){
  const d=new Date(first);d.setDate(first.getDate()+w*7+r);
  if(d>today)continue;
  const key=`${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`;
  const e=days[key]||{tokens:0,requests:0,cost:0};const v=cellMetric(e);if(v!=null)max=Math.max(max,v);
  if(d.getDate()===1||(w===0&&r===0))months[w]=d.getMonth()+1;
  cells[w*7+r]={key,v,req:e.requests};
 }
 for(let w=0;w<calWeeks;w++)for(let r=0;r<7;r++)if(!cells[w*7+r])cells[w*7+r]=null;
 const firstKey=`${first.getFullYear()}-${String(first.getMonth()+1).padStart(2,'0')}-${String(first.getDate()).padStart(2,'0')}`;
 if(!Object.keys(days).length){$('calendar').innerHTML='<div class="empty">수집된 기록이 없습니다.</div>';return;}
 const todayKey=`${today.getFullYear()}-${String(today.getMonth()+1).padStart(2,'0')}-${String(today.getDate()).padStart(2,'0')}`;
 $('calendar').innerHTML='<div class="cal-scroll"><div class="cal-months">'+months.map((m,w)=>m?`<span class="cal-m${w+1}">${m}월</span>`:'').join('')+'</div><div class="cal-grid" role="group" aria-label="최근 '+calWeeks+'주 일별 활동 달력 · 하루 최대 '+compact(max)+' '+metricName()+'">'+cells.map(c=>{if(!c)return '<span class="cal-cell cal-pad" aria-hidden="true"></span>';
  const tipText=c.v==null?c.key+' · 비용 미산정':`${c.key} · ${metricFmt(c.v)}${metricUnit()} · ${fmt(c.req)}회`;
  const hint=esc(tipText+' · 클릭하면 그날로 좁혀봅니다');
  return `<span class="cal-cell${c.v==null?' na':' a'+(c.v?1+Math.min(4,Math.floor(c.v/max*4)):0)}" role="button" tabindex="${c.key===todayKey?0:-1}" data-day="${c.key}" aria-label="${hint}" data-tip="${hint}"></span>`;}).join('')+'</div><div class="cal-legend" aria-hidden="true">적음 <i class="cal-key"></i><i class="cal-key a1"></i><i class="cal-key a2"></i><i class="cal-key a3"></i><i class="cal-key a4"></i><i class="cal-key a5"></i> 많음 · <i class="cal-key na"></i> 비용 미산정</div></div>'+calStats((data.calendar||[]).filter(r=>r.day>=firstKey),calWeeks);
 fitCalendar();
}
let calendarWidth=0;
// Observe the calendar's panel: the grid's own width follows the cell size, so
// the reliable resize signal is the panel. Refit only on >8px changes.
const calendarObserver=new ResizeObserver(()=>{
 const scroll=$('calendar').querySelector('.cal-scroll');
 if(!scroll)return;
 const w=Math.round(scroll.clientWidth);
 if(Math.abs(w-calendarWidth)<8)return;
 calendarWidth=w;
 if(lastUsage&&'calendar' in lastUsage&&calendarWeeks()!==calWeeks)renderCalendar(lastUsage);else fitCalendar();
});
calendarObserver.observe($('calendar').closest('.panel')||$('calendar'));
function renderCacheTrend(data){
 if(!data.insights)return;
 const label=$('cache-series').value;
 const lookup=new Map(data.insights.cache.series.filter(p=>p.label===label).map(p=>[p.time,p.rate]));
 const points=data.series.map(p=>({time:Date.parse(p.time)/1000,value:lookup.get(p.time)??null}));
 $('cache-trend').innerHTML=percentChart(points,seriesLabel(label)+' 캐시 활용률 추이')+'<p class="hint">구간별 입력 비중 · 입력이 없으면 — · 누적 그래프 선택과 무관합니다.</p>';
 fitSvgText($('cache-trend'));
}
$('cache-series').addEventListener('change',()=>{if(lastUsage)renderCacheTrend(lastUsage);});
