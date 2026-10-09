'use strict';
// Shared state, API access, theme colors and the quota line/bar charts.
const $=id=>document.getElementById(id);
// Scrolling animates only when the system does not ask for reduced motion.
const motion=()=>matchMedia('(prefers-reduced-motion: reduce)').matches?'auto':'smooth';
const seriesLabel=label=>$('group').value==='route'?(routeNames[label]||label):$('group').value==='agent'?(KIND_NAMES[label]||label):(['Unknown','Other'].includes(label)?'제작사 미확인':label);
const missingRoutes=data=>$('group').value==='route'?(data.unavailable_routes||[]).filter(r=>!data.labels.includes(r.route)):[];
const gapLabel=row=>row.observed?(row.observation_stale?'토큰 관측 있음 · 합계 검증 대기':'토큰 수신 중 · 합계 검증 대기'):'토큰 상세 미수집';
// Short numbers (1.2M) are a per-browser reading preference; exact values stay in the tooltip.
const compactNumbers=()=>storage.get('llmCompactNumbers')==='1';
const tableNumber=v=>compactNumbers()?`<span data-tip="${fmt(v)}">${compact(v)}</span>`:fmt(v);
const tableCells=(row,denom)=>fields.map(k=>{
 const cls=k==='total'?'total-cell':DETAIL_COLS.has(k)?'col-detail':'';
 if(k==='reasoning'&&REASONING_UNREPORTED.has(row.route)&&!row.reasoning)return `<td class="${cls}"><span class="na-text" data-tip="이 경로는 추론 토큰을 따로 기록하지 않습니다">미제공</span></td>`;
 const cell=`<td${cls?` class="${cls}"`:''}>${tableNumber(k==='total'?modelTotal(row):row[k])}${k==='reasoning'&&row.output?`<small>${(row.reasoning/row.output*100).toFixed(0)}%</small>`:''}</td>`;
 return k==='total'?cell+`<td class="share-cell">${denom?percent(modelTotal(row)/denom*100):'—'}</td>`:cell;
}).join('');
let colors=[],mutedColor='#888';
function updateColors(){const style=getComputedStyle(document.documentElement);colors=Array.from({length:8},(_,i)=>style.getPropertyValue('--chart-'+i).trim());mutedColor=style.getPropertyValue('--muted').trim()||mutedColor;}
// The browser chrome color follows the active theme's page background.
// theme.js fires llm-theme-change before this deferred script registers its
// listener, so sync once at startup too.
function syncThemeColor(){
 const meta=document.querySelector('meta[name=theme-color]');
 if(meta)meta.content=getComputedStyle(document.documentElement).getPropertyValue('--bg').trim();
}
updateColors();syncThemeColor();
let pending=null,timer=null,csrf='',refreshMs=300000,lastUsage=null,lastLimits=null,activeQuery='',queued=false,queuedManual=false,editingQuota=false,editingPanels=false;
const hiddenLabels=new Set();
const tableSort={key:'total',direction:'desc'};
const compareText=new Intl.Collator('ko',{numeric:true,sensitivity:'base'});
const sortNames={provider:'제작사 / 사용 서비스',model:'모델',requests:'요청 수',avg:'평균/요청',uncached_input:'일반 입력',cached_input:'캐시 읽기',output:'출력',total:'합계',share:'비중',cache_creation:'캐시 생성',reasoning:'추론',est_cost:'비용 추정'};
const storage={
 get(k){try{return localStorage.getItem(k)}catch{return null}},
 set(k,v){try{localStorage.setItem(k,v)}catch{}},
};
// The last answer to each data GET is kept in Cache Storage on this device. When the
// server cannot be reached (offline, Tailscale down) the page shows that copy and says
// how old it is instead of an empty screen. Nothing is stored for writes or the CSRF token.
const OFFLINE_CACHE='llm-usage-data-v1',OFFLINE_KEEP=40;
// Exact paths only: configuration, authentication and notification endpoints never
// enter the statistics cache, including query-string variants and future APIs.
const OFFLINE_PATHS=new Set(['/api/usage','/api/limits','/api/reports','/api/session','/api/project']);
let offlineSince=0,offlineCacheReady=null,offlineCacheEpoch=0;
function cacheablePath(path){
 try{const url=new URL(path,location.origin);return url.origin===location.origin&&OFFLINE_PATHS.has(url.pathname);}catch{return false;}
}
async function statisticsCache(){
 if(!offlineCacheReady)offlineCacheReady=(async()=>{
  const cache=await caches.open(OFFLINE_CACHE);
  // Migrate the existing v1 cache before any read or write. Changing the name alone
  // would leave old configuration and notification addresses on this device.
  for(const key of await cache.keys())if(!cacheablePath(key.url))await cache.delete(key);
  return cache;
 })().catch(error=>{offlineCacheReady=null;throw error;});
 return offlineCacheReady;
}
async function keepCopy(path,data){
 const epoch=offlineCacheEpoch;
 try{
  const cache=await statisticsCache();
  if(!cacheablePath(path)||epoch!==offlineCacheEpoch)return;
  await cache.put(path,new Response(JSON.stringify({saved:Date.now(),data}),{headers:{'Content-Type':'application/json'}}));
  const keys=await cache.keys();
  for(const key of keys.slice(0,Math.max(0,keys.length-OFFLINE_KEEP)))await cache.delete(key);
 }catch{}
}
async function lastCopy(path){
 try{const cache=await statisticsCache();if(!cacheablePath(path))return null;const hit=await cache.match(path);return hit?await hit.json():null;}catch{return null;}
}
async function clearStatisticsCache(){
 offlineCacheEpoch++;
 if(offlineCacheReady)await offlineCacheReady.catch(()=>{});
 await caches.delete(OFFLINE_CACHE);offlineCacheReady=null;
}
// Purge legacy configuration even when this visit only makes online requests.
void statisticsCache().catch(()=>{});
// The deadline covers both headers and body, including one CSRF recovery.
async function api(path,body,retried,requestOptions={}){
 const deadline=requestOptions.deadline??Date.now()+(requestOptions.timeout??30000);
 const external=requestOptions.signal;
 if(external?.aborted)throw external.reason;
 if(body&&!csrf)csrf=(await api('/api/bootstrap',undefined,undefined,{...requestOptions,deadline})).csrf;
 const controller=new AbortController();
 const options={headers:{Accept:'application/json'},signal:controller.signal};
 if(body){options.method=requestOptions.method||'POST';options.headers['Content-Type']='application/json';options.headers['X-CSRF-Token']=csrf;options.body=JSON.stringify(body);}
 let timer,abortListener;
 const forwardAbort=()=>controller.abort(external.reason);
 external?.addEventListener('abort',forwardAbort,{once:true});
 let r,d;
 try{
  const aborted=new Promise((_,reject)=>{
   abortListener=()=>reject(controller.signal.reason);
   controller.signal.addEventListener('abort',abortListener,{once:true});
   const timeout=()=>controller.abort(new DOMException('응답 시간 초과','TimeoutError'));
   if(deadline<=Date.now())timeout();else timer=setTimeout(timeout,deadline-Date.now());
  });
  [r,d]=await Promise.race([(async()=>{
   const response=await fetch(path,options);
   const data=await response.json().catch(error=>{if(controller.signal.aborted)throw controller.signal.reason;return null;});
   return [response,data];
  })(),aborted]);
 }catch(error){
  if(external?.aborted)throw external.reason; // Superseded queries are not outages.
  window.dispatchEvent(new Event('llm-api-unavailable'));
  const copy=!body&&cacheablePath(path)?await lastCopy(path):null;
  if(!copy){
   const message=error.name==='TimeoutError'?'서버 응답 시간이 초과되었습니다. 다시 불러오세요.':'서버에 연결할 수 없습니다. 네트워크·Tailscale 연결을 확인하세요.';
   throw Error(message+(body?' 서버 처리 결과는 확인하지 못했습니다. 저장·수집 상태를 확인한 뒤 다시 시도하세요.':''));
  }
  if(requestOptions.source)requestOptions.source.saved=copy.saved;
  else{offlineSince=offlineSince?Math.min(offlineSince,copy.saved):copy.saved;window.dispatchEvent(new Event('llm-data-source-change'));}
  return copy.data;
 }finally{
  clearTimeout(timer);external?.removeEventListener('abort',forwardAbort);
  controller.signal.removeEventListener('abort',abortListener);
 }
 if(r.status>=500)window.dispatchEvent(new Event('llm-api-unavailable'));
 if(r.ok&&!body&&cacheablePath(path))await keepCopy(path,d);
 if(r.status===403&&body&&!retried){
  csrf='';csrf=(await api('/api/bootstrap',undefined,undefined,{...requestOptions,deadline})).csrf;
  return api(path,body,true,{...requestOptions,deadline});
 }
 if(!r.ok){const error=Error((d&&d.error)||`서버 응답 오류 (HTTP ${r.status})`);error.status=r.status;throw error;}
 if(requestOptions.source)requestOptions.source.saved=0;
 return d;
}
function percentChart(points,label){
 const valid=points.filter(p=>p.value!=null);
 if(!valid.length)return '<p class="hint">표시할 관측값이 없습니다.</p>';
 const first=points[0].time,last=points.at(-1).time,span=Math.max(1,last-first);
 const x=p=>40+(p.time-first)/span*510,y=p=>110-p.value;
 let path='',connected=false;
 for(const p of points){
  if(p.value==null){connected=false;continue;}
  path+=`${connected&&!p.break_before?'L':'M'}${x(p).toFixed(1)},${y(p).toFixed(1)} `;connected=true;
 }
 const stride=Math.max(1,Math.ceil(valid.length/100));
 const dots=valid.filter((p,i)=>i%stride===0||i===valid.length-1||p.break_before).map(p=>`<circle cx="${x(p)}" cy="${y(p)}" r="2.5" data-tip="${esc(when(p.time))} KST · ${percent(p.value)}"/>`).join('');
 return `<svg class="percent-chart" viewBox="0 0 580 142" role="img" aria-label="${esc(label)}"><text x="0" y="15">100%</text><text x="13" y="113">0%</text><line class="chart-gridline" x1="40" x2="550" y1="110" y2="110"/><path d="${path}"/>${dots}<text x="40" y="137">${esc(when(first))}</text><text x="550" y="137" text-anchor="end">${esc(when(last))}</text></svg>`;
}
// The CSP (style-src 'self') blocks style="" attributes inserted with innerHTML,
// so bar widths and marker positions travel as data-w/data-x (percent) and are
// applied through the CSSOM, which the policy allows.
function applyGeometry(root){
 root.querySelectorAll('[data-w]').forEach(el=>{el.style.width=el.dataset.w+'%';});
 root.querySelectorAll('[data-x]').forEach(el=>{el.style.left=el.dataset.x+'%';});
}
// Fixed-viewBox charts scale their geometry with the panel, which used to
// shrink or grow the axis text with it. Scale each <text> back so it renders
// at ~11px (13px for the end-of-line badge) on screen at any width.
function fitSvgText(root){
 const svgs=root.querySelectorAll?root.querySelectorAll('svg.percent-chart,svg.quota-line,svg.quota-bars'):[];
 svgs.forEach(svg=>{
  const vb=svg.viewBox&&svg.viewBox.baseVal,w=svg.clientWidth;
  if(!vb||!vb.width||!w)return;
  const ratio=vb.width/w;
  svg.querySelectorAll('text').forEach(t=>{
   t.style.fontSize=(t.classList.contains('quota-end')?13:11)*ratio+'px';
  });
 });
}
// Details opening/closing does not resize the viewport, but reveals SVGs that
// were skipped while hidden (clientWidth 0). toggle does not bubble.
document.addEventListener('toggle',event=>{
 const d=event.target;
 if(!(d instanceof HTMLDetailsElement))return;
 if(d.closest('#limits')||d.closest('#cache-panel'))fitSvgText(d);
},true);
function quotaLine(history,row){
 const points=history.filter(p=>p.remaining!=null);
 if(!points.length)return '<p class="hint">표시할 한도 관측이 없습니다.</p>';
 const first=points[0],last=points.at(-1);
 // The x domain runs to the earlier of the next reset and last observation+6h
 // so the burn-down forecast has room to draw.
 const horizon=Math.max(last.checked,Math.min(row&&row.resets||Infinity,last.checked+21600));
 const span=Math.max(300,horizon-first.checked);
 const low=Math.max(0,Math.floor(Math.min(...points.map(p=>p.remaining))-5));
 const high=Math.min(100,Math.ceil(Math.max(...points.map(p=>p.remaining))+5));
 const x=p=>38+(p.checked-first.checked)/span*475,y=p=>112-(p.remaining-low)/Math.max(1,high-low)*96;
 let path='';const isolated=[];
 points.forEach((p,i)=>{path+=`${i&&!p.break_before?'L':'M'}${x(p).toFixed(1)},${y(p).toFixed(1)} `;if((i===0||p.break_before)&&(i===points.length-1||points[i+1].break_before))isolated.push(p);});
 const markers=points.filter(p=>p.break_before&&p.break_reason!=='start').slice(-3).map(p=>`<g><line class="quota-break" x1="${x(p)}" x2="${x(p)}" y1="12" y2="116"/><text x="${Math.min(470,x(p)+3)}" y="9">${({reset:'초기화 변경',gap:'수집 공백',error:'조회 실패',increase:'잔여 증가',expired:'초기화 경과',unknown:'정보 부족'})[p.break_reason]||'수집 공백'}</text></g>`).join('');
 const pace=row&&row.forecast&&row.forecast.per_hour;
 const forecast=pace&&(!row.resets||row.resets>last.checked)&&horizon>last.checked
  ?`<path class="quota-forecast" d="M${x(last).toFixed(1)},${y(last).toFixed(1)} L${x({checked:horizon}).toFixed(1)},${y({remaining:Math.max(low,last.remaining-pace*(horizon-last.checked)/3600)}).toFixed(1)}"/>`:'';
 return `<svg class="quota-line" viewBox="0 0 580 146" role="img" aria-label="잔여 한도 선 그래프${forecast?' · 점선은 최근 페이스 예측':''} · 세로축 ${low}~${high}%"><text x="0" y="20">${high}%</text><text x="0" y="115">${low}%</text><line class="chart-gridline" x1="38" x2="513" y1="112" y2="112"/>${markers}<path d="${path}"/>${forecast}${isolated.map(p=>`<circle cx="${x(p)}" cy="${y(p)}" r="2"/>`).join('')}<circle cx="${x(last)}" cy="${y(last)}" r="3"/><text class="quota-end" x="520" y="${Math.max(22,y(last)+4)}">${percent(last.remaining)}</text><text x="38" y="141">${esc(when(first.checked))}</text><text x="513" y="141" text-anchor="end">${esc(when(horizon))}</text></svg>`;
}
function quotaBars(rows){
 if(!rows.some(r=>r.decrease_pp!=null))return '<p class="hint">감소량을 비교할 관측이 부족합니다.</p>';
 const max=Math.max(1,...rows.map(r=>r.decrease_pp||0)),width=480/rows.length;
 return `<svg class="quota-bars" viewBox="0 0 580 115" role="img" aria-label="최근 12시간 · 확인 시각별 관측 감소량"><text x="0" y="15">${max.toFixed(1)}%p</text><line class="chart-gridline" x1="42" x2="522" y1="82" y2="82"/>${rows.map((r,i)=>{
  const x=42+(i+.5)*width,label=new Date(r.time*1000).toLocaleTimeString('ko-KR',{timeZone:'Asia/Seoul',hour:'2-digit',hour12:false});
  const description=label+' · '+(r.decrease_pp==null?'미관측':r.decrease_pp.toFixed(1)+'%p 감소 · '+r.observed_minutes.toFixed(0)+'분 관측');
  return `${r.decrease_pp==null?`<text x="${x}" y="78" text-anchor="middle">—</text>`:`<rect tabindex="0" x="${x-width*.3}" y="${82-(r.decrease_pp/max)*62}" width="${width*.6}" height="${Math.max(1,r.decrease_pp/max*62)}" aria-label="${esc(description)}" data-tip="${esc(description)}"/>`}${i%3===0||i===rows.length-1?`<text x="${x}" y="104" text-anchor="middle">${esc(label)}</text>`:''}`;
 }).join('')}</svg>`;
}
function quotaTrends(row){
 // Only observed spans are listed, on one line; an unobserved span adds nothing.
 const trends=(row.trends||[]).filter(t=>t.available);
 if(!trends.length)return '';
 return `<p class="quota-trends">${trends.map(t=>`<span class="nowrap">최근 ${t.window_minutes===60?'1시간':'30분'} −${t.decrease_pp.toFixed(1)}%p</span> <span class="nowrap">(${percent(t.start_remaining)} → ${percent(t.end_remaining)},</span> <span class="nowrap">${t.observed_minutes.toFixed(1)}분 관측,</span> <span class="nowrap">${t.per_hour.toFixed(1)}%p/시간)</span>`).join('<br>')}</p>`;
}
function quotaHistory(row,opened,now){
 const key=row.route+':'+row.bucket,history=row.history||[];
 const visible=history.filter(p=>p.checked>=(now||Date.now()/1000)-86400);
 const source=({'codex':'계정 조회','claude-oauth':'계정 조회','codex-local':'로컬 사용 기록','claude-code':'상태줄 수신','antigravity':'상태줄 수신','antigravity-app':'앱 조회','opencode-go':'계정 조회','devin':'계정 조회'})[row.history_source]||'원본 관측';
 return `${quotaTrends(row)}<details class="quota-history" data-history="${esc(key)}"${opened.has(key)?' open':''}><summary>한도 변화 이력 <small>선 그래프 · 감소량</small></summary>${quotaLine(visible,row)}<p>최근 24시간 · ${source}${visible.length?' · 마지막 '+esc(when(visible.at(-1).checked))+' KST':''}<br>선은 관측값 연결이며 공백·초기화는 구분합니다.</p>${quotaBars(row.decreases||[])}<p>최근 12시간 · 관측이 끝난 시각에 감소량을 합산합니다. 미관측 구간은 —.</p>${history.length?`<details data-history="${esc(key+':values')}"${opened.has(key+':values')?' open':''}><summary>최근 관측값 ${Math.min(40,history.length)}개</summary><div class="table-wrap insight-table"><table><thead><tr><th>확인 KST</th><th>잔여</th><th>초기화 KST</th></tr></thead><tbody>${history.slice(-40).reverse().map(p=>`<tr><td>${esc(when(p.checked))}</td><td>${percent(p.remaining)}</td><td>${p.resets?esc(when(p.resets)):'미제공'}</td></tr>`).join('')}</tbody></table></div></details>`:'<p>수신된 원본 관측부터 이력이 쌓입니다.</p>'}</details>`;
}
