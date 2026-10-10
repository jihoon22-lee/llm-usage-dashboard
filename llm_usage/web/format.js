'use strict';
// Pure formatting, labels and quota arithmetic: no DOM access, loaded first and unit-tested in Node (tests/js).
const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const fmt=v=>Number(v||0).toLocaleString('ko-KR');
const compact=v=>Intl.NumberFormat('en',{notation:'compact',maximumFractionDigits:2}).format(v||0);
const when=t=>t?new Date(t*1000).toLocaleString('ko-KR',{timeZone:'Asia/Seoul',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false}):'확인 기록 없음';
const left=s=>s==null?'초기화 정보 미제공':s<=0?'초기화 경과 · 재확인 대기':`${Math.floor(s/86400)?Math.floor(s/86400)+'일 ':''}${Math.floor(s%86400/3600)}시간 ${Math.floor(s%3600/60)}분 남음`;
// Dense rows (overview, reset schedule): the two largest units, without "남음".
const soon=s=>s==null?'미제공':s<=0?'재확인 대기':s<3600?`${Math.max(1,Math.floor(s/60))}분`:s<86400?`${Math.floor(s/3600)}시간 ${Math.floor(s%3600/60)}분`:`${Math.floor(s/86400)}일 ${Math.floor(s%86400/3600)}시간`;
// KST expiry dates; the year is shown only when it differs from the current one.
const kstYear=t=>new Date(t*1000).toLocaleString('en',{timeZone:'Asia/Seoul',year:'numeric'});
const stamp=(t,now=Date.now()/1000)=>new Date(t*1000).toLocaleString('ko-KR',{timeZone:'Asia/Seoul',...(kstYear(t)===kstYear(now)?{}:{year:'numeric'}),month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hourCycle:'h23'});
const names={'codex':'OpenAI / Codex','claude-code':'Anthropic / Claude Code','antigravity':'Google / Antigravity','opencode-go':'OpenCode Go','devin':'Cognition / Devin'};
const routeNames={...names,opencode:'OpenCode'};
const statuses={fresh:'최근 확인',stale:'오래된 값',error:'수집 실패',unavailable:'미제공',ended:'구독 없음',ok:'수집 완료',partial:'일부 미수집',idle:'미사용'};
const fields=['uncached_input','cached_input','output','total','cache_creation','reasoning'];
// Every table, ranking and share uses the same total as the cards and charts (cache creation included).
const modelTotal=v=>total(v);
const usd=v=>v==null?'—':'$'+Number(v).toLocaleString('en-US',{maximumFractionDigits:v>=100?0:2});
const DETAIL_COLS=new Set(['uncached_input','cached_input','output','cache_creation','reasoning']);
const total=v=>(v.uncached_input||0)+(v.cached_input||0)+(v.output||0)+(v.cache_creation||0);
// Cost CSV retains the distinction between a known zero and unpriced tokens.
function costCsvFields(values){
 const missing=values.map(v=>v.unpriced||0),tokens=values.map(v=>total(v));
 const costs=values.map((v,i)=>missing[i]>0&&tokens[i]<=missing[i]?'':(v.cost||0));
 const unpriced=missing.reduce((a,b)=>a+b,0),allTokens=tokens.reduce((a,b)=>a+b,0);
 const sum=unpriced>0&&allTokens<=unpriced?'':values.reduce((s,v)=>s+(v.cost||0),0);
 return [...costs,sum,...missing,unpriced];
}
const percent=v=>v==null?'—':Number(v).toFixed(1)+'%';
const signed=v=>(v>0?'+':'')+fmt(v);
// These sources do not report reasoning separately: their 0 means not provided.
const REASONING_UNREPORTED=new Set(['claude-code','devin']);
const KIND_NAMES={main:'메인',sub:'서브 에이전트',fork:'포크'};
// Period rows priced at one rate (USD per 1M tokens), cache reads at its cached rate.
const estimateWith=(rows,rate)=>rows.reduce((s,r)=>s+((r.uncached_input||0)*rate.input+(r.cached_input||0)*(rate.cached??rate.input)
 +(r.output||0)*rate.output+(r.cache_creation||0)*(rate.cache_write??rate.input))/1e6,0);
const pctChange=v=>v==null?'—':`${v>0?'+':''}${v.toFixed(0)}%`;
// Month-to-date use against a project's token and/or USD budget (the larger share counts).
function budgetState(usage,budget){
 const parts=[];
 if(budget.tokens)parts.push({ratio:(usage?.tokens||0)/budget.tokens,text:`${compact(usage?.tokens||0)}/${compact(budget.tokens)} 토큰`});
 if(budget.usd)parts.push(usage?.cost==null?{ratio:0,text:'비용 미산정'}:{ratio:usage.cost/budget.usd,text:`${usd(usage.cost)}/${usd(budget.usd)}`});
 const ratio=Math.max(0,...parts.map(p=>p.ratio));
 return {ratio,text:parts.map(p=>p.text).join(' · '),level:ratio>=1?'level-low':ratio>=.8?'level-mid':'level-ok'};
}
const shortService={'codex':'Codex','claude-code':'Claude','antigravity':'Antigravity','opencode-go':'OpenCode Go','devin':'Devin'};
// Antigravity buckets are '<family>-<window>[-n]'; anything else keeps its own name.
const agyBucket=bucket=>{const m=/^(gemini|3p)-(5h|weekly)(?:-(\d+))?$/.exec(bucket);return m&&{family:m[1]==='3p'?'3rd party':'Gemini',window:m[2]==='5h'?'5시간':'주간',suffix:m[3]||''};};
const bucketName=r=>{
 const agy=r.route==='antigravity'&&agyBucket(r.bucket);
 if(agy)return agy.family+(agy.suffix?' '+agy.suffix:'');
 return ({five_hour:'5시간',seven_day:'주간',seven_day_opus:'Opus 주간',seven_day_sonnet:'Sonnet 주간',seven_day_oauth_apps:'앱 주간',rolling:'5시간',daily:'일간',weekly:'주간',monthly:'월간'})[r.bucket]||r.display_name||r.bucket;
};
// Antigravity lists the 5h window before the weekly one, Gemini first in each.
function agyWindows(rows){
 const pick=window=>rows.filter(r=>agyBucket(r.bucket)?.window===window).sort((a,b)=>Number(agyBucket(a.bucket).family!=='Gemini')-Number(agyBucket(b.bucket).family!=='Gemini'));
 return {h5:pick('5시간'),wk:pick('주간'),other:rows.filter(r=>!agyBucket(r.bucket))};
}
// The API marks a window whose longer parent window is currently exhausted.
const quotaValue=r=>r.blocked_by?'사용 불가':percent(r.remaining);
// What a service can still be used for now: its tightest current window, 0 when blocked.
const usable=r=>r.blocked_by?0:r.remaining;
function availability(rows){
 const fresh=rows.filter(r=>!modelQuota(r)&&r.status==='fresh'&&r.remaining!=null);
 return fresh.length?fresh.reduce((a,b)=>usable(b)<usable(a)?b:a):null;
}
const levelOf=(v,low)=>v<=low?'level-low':v<=50?'level-mid':'level-ok';
const ring=(row,low)=>row?`<svg class="ring ${levelOf(usable(row),low)}" viewBox="0 0 36 36" aria-hidden="true"><circle cx="18" cy="18" r="15.9" class="ring-bg"/><circle cx="18" cy="18" r="15.9" class="ring-fg" pathLength="100" stroke-dasharray="${Math.max(0,Math.min(100,usable(row))).toFixed(1)} 100" transform="rotate(-90 18 18)"/></svg>`:'<span class="ring ring-empty" aria-hidden="true"></span>';
const modelQuota=r=>['model','unknown'].includes(r.scope?.role)||(r.route==='codex'&&r.bucket.includes(' · ')&&r.bucket.split(' · ')[0]!=='codex')||(r.route==='claude-code'&&r.bucket.startsWith('seven_day_'));
const quotaLabel=r=>{
 const agy=r.route==='antigravity'&&agyBucket(r.bucket);
 if(agy)return `Antigravity ${agy.window} ${agy.family}${agy.suffix?' '+agy.suffix:''}`;
 const sv=shortService[r.route]||r.route,bn=bucketName(r);
 return bn.startsWith(sv)?bn:`${sv} ${bn}`;
};
const SCOPE_LABELS={route:'서비스',provider:'제작사',model:'모델',project:'프로젝트'};
const scopeText=scope=>{const i=scope.indexOf(':');if(i<0)return scope;
 const kind=scope.slice(0,i),value=scope.slice(i+1);
 const name=kind==='route'?(routeNames[value]||value):value;
 return (SCOPE_LABELS[kind]||kind)+': '+name;};
const SOURCE_GROUPS=[['Claude',/claude/i],['Codex',/codex/i],['Antigravity',/antigravity/i],['Devin',/devin/i],['OpenCode',/opencode/i],
 ['운영',/^(수집기|일일 백업|원본 대조|외부 알림|데이터베이스)$/],['내부 작업',/^(normalization|session-backfill)$/]];
const SOURCE_RANK={error:0,unavailable:1,partial:2,stale:3,ok:4,fresh:4,idle:5,ended:5};
const SOURCE_HINTS={error:'수집에 실패했습니다. 다음 주기에 다시 시도하며, 계속되면 상세 문구의 원인을 확인하세요.',
 unavailable:'경로·구독·실행 중인 앱이 없어 지금은 수집하지 않습니다.',
 partial:'일부를 읽지 못했거나 확인이 필요한 기록이 있습니다. 상세 문구의 개수를 확인하세요.',
 stale:'최근 관측이 없습니다. 해당 도구를 쓰거나 다음 수집 주기에 갱신됩니다.',
 idle:'이 경로는 지금 쓰이지 않습니다. 다른 경로가 값을 대신 채웁니다.',ended:'구독이 없어 수집을 멈췄습니다.'};
const sourceGroup=name=>(SOURCE_GROUPS.find(([,re])=>re.test(name))||['기타'])[0];
// Round tick step (1/2/2.5/5×10^n) so y labels stay readable numbers.
function niceStep(max){
 const rough=Math.max(max,1)/4,mag=Math.pow(10,Math.floor(Math.log10(rough)));
 for(const m of [1,2,2.5,5,10])if(mag*m>=rough)return mag*m;
 return mag*10;
}
// Relative time reads at a glance; the exact KST time stays in the tooltip.
const ago=t=>{const s=Math.max(0,Date.now()/1000-t);return s<60?'방금':s<3600?`${Math.floor(s/60)}분 전`:s<86400?`${Math.floor(s/3600)}시간 전`:when(t);};
// The window most likely to stop work, from current values only: a blocked window or one
// forecast to run out before its reset comes first (soonest first), then the lowest
// remaining expected at the reset (the forecast when there is one, else the current value).
function urgencyKey(r){
 if(r.blocked_by)return [0,r.blocked_by.seconds_to_reset??Infinity];
 const f=r.forecast;
 if(f&&f.within_window)return [1,f.depletes_at??Infinity];
 return [2,f?f.projected_remaining:r.remaining];
}
function mostUrgent(rows){
 const fresh=rows.filter(r=>!modelQuota(r)&&r.status==='fresh'&&r.remaining!=null);
 return fresh.sort((a,b)=>{const x=urgencyKey(a),y=urgencyKey(b);return x[0]-y[0]||x[1]-y[1];})[0]||null;
}
function urgencyText(r){
 if(r.blocked_by)return `${quotaLabel(r)} 사용 불가 · 주간 초기화 ${left(r.blocked_by.seconds_to_reset)}`;
 const f=r.forecast;
 if(f&&f.within_window)return `${quotaLabel(r)} ${percent(r.remaining)} · 초기화 전 소진 예상 ${when(f.depletes_at)}`;
 return `${quotaLabel(r)} ${percent(r.remaining)}${f?` · 초기화 시 약 ${percent(f.projected_remaining)} 예상`:''} · ${r.resets?'초기화 '+left(r.seconds_to_reset):'초기화 정보 미제공'}`;
}
// Which service to reach for now: services with room in their tightest current window,
// those whose long window (a day or more) resets within a day with plenty left first —
// that remainder is lost at the reset — then by room.
function recommendations(groups,minimum=20){
 const items=[];
 for(const g of groups){
  const tight=availability(g.rows);
  if(!tight||usable(tight)<minimum)continue;
  // A service already on course to run out before a reset is not a place to add work.
  if(g.rows.some(r=>!modelQuota(r)&&r.status==='fresh'&&r.forecast&&r.forecast.within_window))continue;
  const expiring=g.rows.find(r=>!modelQuota(r)&&r.status==='fresh'&&!r.blocked_by&&r.remaining>=30
   &&(r.window_seconds||0)>=86400&&r.seconds_to_reset!=null&&r.seconds_to_reset<=86400);
  items.push({route:g.route,value:usable(tight),expiring,
   note:expiring?`${bucketName(expiring)} ${percent(expiring.remaining)} 남음 · ${left(expiring.seconds_to_reset).replace(' 남음','')} 후 초기화`:''});
 }
 return items.sort((a,b)=>(!!b.expiring-!!a.expiring)||b.value-a.value);
}
// Weekdays and weekend days (KST calendar days) in [start, end), stopping at now.
function dayKinds(startIso,endIso,nowSec){
 const start=Date.parse(startIso),end=Math.min(Date.parse(endIso),nowSec*1000);
 let weekday=0,weekend=0;
 for(let t=start;t<end;t+=864e5){const dow=new Date(t+9*3600e3).getUTCDay();if(dow===0||dow===6)weekend++;else weekday++;}
 return {weekday,weekend};
}

// Age the observation itself, not just the offline banner. This is a pure view
// transform; cached provider data and reset values are never modified in place.
function currentLimits(data,now,offline=false){
 const ttl=data.stale_seconds??600;
 const rows=(data.limits||[]).map(original=>{
  const r={...original};
  if(r.resets)r.seconds_to_reset=Math.max(0,r.resets-now);
  const old=offline||r.checked==null||r.checked>now+120||now-r.checked>ttl||(r.resets&&r.resets<=now);
  if(old&&r.status==='fresh')r.status='stale';
  if(r.status!=='fresh'){
   r.stale=true;r.forecast=null;r.capacity=null;r.plan=null;r.pace_per_hour=null;
   r.paces={recent:{per_hour:null,reason:'최신 한도 확인 필요'},baseline:{per_hour:null,reason:'최신 한도 확인 필요'}};
   r.blocked_by=null;
  }
  return r;
 });
 for(const r of rows)if(r.blocked_by){
  const parent=rows.find(p=>p.route===r.route&&p.bucket===r.blocked_by.bucket);
  if(!parent||parent.status!=='fresh')r.blocked_by=null;
  else r.blocked_by={...r.blocked_by,seconds_to_reset:parent.seconds_to_reset};
 }
 const items=(data.resources?.items||[]).map(original=>{
  const r={...original},checked=r.value_checked??r.checked;
  if(['fresh','manual'].includes(r.status)&&(offline||checked==null||checked>now+120||now-checked>(r.ttl??1800)))r.status='stale';
  if(['fresh','manual'].includes(r.status)&&r.expires&&r.expires<=now)r.status=r.expiry_is_partial?'stale':'expired';
  if(r.usage_conditions&&(offline||now-r.usage_conditions.checked>(r.usage_conditions.ttl??600)))r.usage_conditions=null;
  return r;
 });
 let planning=data.planning?{...data.planning}:null;
 if(planning)planning.alternatives=(planning.alternatives||[]).filter(p=>p.valid_until!=null&&p.valid_until>now);
 if(planning&&planning.resources_valid_until<=now){
  planning.resources=[];
  planning.conditions=[...(planning.conditions||[]),'추가 자원의 확인·만료 시점이 지났습니다. 제공사에서 재확인하세요.'];
 }
 if(planning&&(offline||planning.valid_until<=now||rows.some(r=>r.route===planning.route&&planning.applies?.includes(r.bucket)&&r.status!=='fresh'))){
  const invalidate=p=>p?{...p,state:'unknown',seconds:null,bottleneck:null,resources:[],alternatives:[],reason:offline?'오프라인 사본입니다. 연결 후 한도를 다시 확인하세요.':'관측값이 오래됐습니다. 한도를 다시 확인하세요.'}:p;
  planning=invalidate(planning);planning.today=invalidate(planning.today);planning.week=invalidate(planning.week);
 }
 return {...data,now,limits:rows,resources:{...data.resources,items},planning,_offline:offline};
}
const duration=s=>{if(s==null)return '추정 보류';if(s<=0)return '현재 소진';if(s<60)return '1분 미만';const minutes=Math.round(s/60);return `${Math.floor(minutes/60)?Math.floor(minutes/60)+'시간 ':''}${minutes%60}분`;};
// Resources and reset grants that run out within `within` seconds (default a day). Past expiry,
// folded rows (previous account, expired, unlinked duplicates), spending allowances (their date
// is a renewal), used grants and empty balances are skipped. Grants of one resource ending in the
// same hour become one entry with the summed count. `ids` are stable per grant (or resource +
// expiry) so a notification can fire once; `stale` marks records that are no longer current.
function expiringResources(items,now,within=86400){
 const soonEnough=t=>Number.isFinite(t)&&t>now&&t-now<=within;
 const out=[];
 for(const r of items||[]){
  if(r.status==='previous_account'||r.status==='expired'||r.allowance||(r.duplicate_of&&!r.effective))continue;
  const base={id:r.id,route:r.route,kind:r.kind,label:r.label,unit:r.unit,stale:r.status!=='fresh'&&r.status!=='manual'};
  if(r.grants?.length){
   const groups=new Map();
   for(const g of r.grants){
    if(g.status==='used'||g.status==='expired'||!(g.amount>0)||!soonEnough(g.expires))continue;
    const hour=Math.floor(g.expires/3600),group=groups.get(hour)||groups.set(hour,{...base,amount:0,expires:g.expires,ids:[]}).get(hour);
    group.amount+=g.amount;group.expires=Math.min(group.expires,g.expires);group.ids.push(g.id||`${r.id}:${Math.round(g.expires/60)}`);
   }
   out.push(...groups.values());
  }else if(r.amount>0&&soonEnough(r.expires))out.push({...base,amount:r.amount,expires:r.expires,partial:!!r.expiry_is_partial,ids:[`${r.id}:${Math.round(r.expires/60)}`]});
 }
 return out.sort((a,b)=>a.expires-b.expires);
}
