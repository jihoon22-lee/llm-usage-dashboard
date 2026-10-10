// Pure dashboard helpers from llm_usage/web/format.js, run without a DOM.
// node --test tests/js
import {test} from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';

const source=readFileSync(new URL('../../llm_usage/web/format.js',import.meta.url),'utf8');
// Top-level const bindings of a classic script are not context properties, so the
// script itself hands them back. No window/document exists here: any DOM use fails.
const f=vm.runInNewContext(source+`;({esc,fmt,compact,when,left,usd,percent,total,modelTotal,agyBucket,bucketName,
  quotaLabel,quotaValue,usable,availability,levelOf,modelQuota,budgetState,scopeText,niceStep,ago,pctChange,sourceGroup,agyWindows,
  mostUrgent,urgencyText,recommendations,dayKinds,estimateWith,costCsvFields,currentLimits,duration,soon,stamp,expiringResources})`,
  {Intl,Date,Math,Number,String,Set,Map,JSON,RegExp,Object,Array});

test('escaping and number formats',()=>{
  assert.equal(f.esc(`<a href="x">'&`),'&lt;a href=&quot;x&quot;&gt;&#39;&amp;');
  assert.equal(f.esc(null),'');
  assert.equal(f.compact(1234567),'1.23M');
  assert.equal(f.usd(null),'—');
  assert.equal(f.usd(1234.5),'$1,235');
  assert.equal(f.usd(12.345),'$12.35');
  assert.equal(f.percent(null),'—');
  assert.equal(f.percent(12.345),'12.3%');
  assert.equal(f.pctChange(12.6),'+13%');
  assert.equal(f.pctChange(null),'—');
});

test('reset countdown and relative time',()=>{
  assert.equal(f.left(null),'초기화 정보 미제공');
  assert.equal(f.left(0),'초기화 경과 · 재확인 대기');
  assert.equal(f.left(86400+3600*2+60*5),'1일 2시간 5분 남음');
  const now=Date.now()/1000;
  assert.equal(f.ago(now-10),'방금');
  assert.equal(f.ago(now-125),'2분 전');
  assert.equal(f.ago(now-7300),'2시간 전');
});

test('cached quota, reset countdown and recommendations age together without mutating observations',()=>{
 const now=10000;
 const data={now,stale_seconds:600,limits:[{route:'codex',bucket:'weekly',status:'fresh',checked:now,remaining:80,resets:now+1800,
  seconds_to_reset:1800,forecast:{within_window:true},paces:{recent:{per_hour:2}}}],
  planning:{route:'codex',applies:['weekly'],state:'room',valid_until:now+600,resources:[{id:'credit'}],alternatives:[{route:'claude-code',valid_until:now+100}]}};
 const view=f.currentLimits(data,now+7200,true);
 assert.equal(view.limits[0].status,'stale');assert.equal(view.limits[0].remaining,80);
 assert.equal(view.limits[0].seconds_to_reset,0);assert.equal(view.limits[0].forecast,null);
 assert.equal(view.planning.state,'unknown');assert.equal(view.planning.alternatives.length,0);
 assert.equal(data.limits[0].status,'fresh');assert.equal(data.limits[0].seconds_to_reset,1800);
 assert.equal(f.currentLimits(data,now+150).planning.alternatives.length,0);
 assert.equal(f.currentLimits(data,now+700).planning.state,'unknown');
});

test('resource expiry and spending-condition age do not imply remaining funds or an active cap',()=>{
 const data={limits:[],resources:{items:[{id:'x',status:'fresh',checked:1000,ttl:1800,amount:50,expires:1100,
  expiry_is_partial:true,usage_conditions:{checked:100,ttl:600,enabled:true}}]}};
 const view=f.currentLimits(data,1200);
 assert.equal(view.resources.items[0].status,'stale');assert.equal(view.resources.items[0].amount,50);
 assert.equal(view.resources.items[0].usage_conditions,null);
});

test('conditional duration rounds away floating point noise instead of losing a minute',()=>{
 assert.equal(f.duration(5399.999999999),'1시간 30분');
 assert.equal(f.duration(null),'추정 보류');assert.equal(f.duration(0),'현재 소진');
});

test('the model total is the same total the cards and charts use',()=>{
  const v={uncached_input:1,cached_input:2,output:3,cache_creation:4};
  assert.equal(f.modelTotal(v),10);
  assert.equal(f.total(v),10);
});

const row=(over)=>({route:'codex',bucket:'codex · 10080분',status:'fresh',remaining:50,resets:2e9,seconds_to_reset:3600,...over});

test('the most urgent window is a blocked or depleting one, then the lowest expected at reset',()=>{
  const weekly=row({remaining:45,seconds_to_reset:5*86400});
  const fast=row({route:'claude-code',bucket:'five_hour',remaining:80,forecast:{within_window:true,depletes_at:1.9e9,projected_remaining:0}});
  const blocked=row({route:'devin',bucket:'daily',remaining:100,blocked_by:{seconds_to_reset:7200}});
  const easy=row({route:'claude-code',bucket:'seven_day',remaining:60,forecast:{within_window:false,projected_remaining:30}});
  assert.equal(f.mostUrgent([weekly,fast,easy]),fast);
  assert.equal(f.mostUrgent([weekly,fast,blocked]),blocked);
  assert.equal(f.mostUrgent([weekly,easy]),easy);  // 30% expected at reset beats 45% now
  assert.equal(f.mostUrgent([row({status:'stale',remaining:1})]),null);
  assert.match(f.urgencyText(fast),/초기화 전 소진 예상/);
  assert.match(f.urgencyText(easy),/초기화 시 약 30.0% 예상/);
});

test('recommendations put a long window that is about to reset with room left first',()=>{
  const groups=[
    {route:'claude-code',rows:[row({route:'claude-code',bucket:'five_hour',remaining:90,window_seconds:18000})]},
    {route:'codex',rows:[row({remaining:60,window_seconds:604800,seconds_to_reset:20*3600})]},
    {route:'devin',rows:[row({route:'devin',bucket:'daily',remaining:10,window_seconds:86400})]},
  ];
  const result=f.recommendations(groups);
  assert.deepEqual([...result.map(r=>r.route)],['codex','claude-code']);
  assert.match(result[0].note,/60.0% 남음/);
  assert.equal(result[1].note,'');
  groups[1].rows[0].forecast={within_window:true,depletes_at:1.9e9};
  assert.deepEqual([...f.recommendations(groups).map(r=>r.route)],['claude-code']);
});

test('weekday and weekend days stop at now',()=>{
  // 2026-09-28 is a Monday (KST); a full week has five weekdays and two weekend days.
  const start='2026-09-28T00:00:00+09:00',end='2026-10-05T00:00:00+09:00';
  assert.deepEqual({...f.dayKinds(start,end,Date.parse(end)/1000)},{weekday:5,weekend:2});
  assert.deepEqual({...f.dayKinds(start,end,Date.parse('2026-10-01T12:00:00+09:00')/1000)},{weekday:4,weekend:0});
});

test('Antigravity bucket names and labels',()=>{
  assert.deepEqual({...f.agyBucket('3p-weekly-2')},{family:'3rd party',window:'주간',suffix:'2'});
  assert.equal(f.agyBucket('gemini-daily'),null);
  assert.equal(f.bucketName({route:'antigravity',bucket:'gemini-5h'}),'Gemini');
  assert.equal(f.quotaLabel({route:'antigravity',bucket:'3p-5h'}),'Antigravity 5시간 3rd party');
  assert.equal(f.quotaLabel({route:'claude-code',bucket:'five_hour'}),'Claude 5시간');
  assert.equal(f.quotaLabel({route:'codex',bucket:'codex · 10080분',display_name:'Codex 공통 · 주간'}),'Codex 공통 · 주간');
  const rows=[{bucket:'3p-weekly'},{bucket:'gemini-5h'},{bucket:'3p-5h'},{bucket:'other'}];
  const w=f.agyWindows(rows);
  assert.deepEqual(w.h5.map(r=>r.bucket),['gemini-5h','3p-5h']);
  assert.deepEqual(w.wk.map(r=>r.bucket),['3p-weekly']);
  assert.deepEqual(w.other.map(r=>r.bucket),['other']);
});

test('availability uses current values and counts a blocked window as 0',()=>{
  const rows=[{route:'claude-code',bucket:'five_hour',status:'fresh',remaining:80,blocked_by:{bucket:'seven_day'}},
              {route:'claude-code',bucket:'seven_day',status:'fresh',remaining:0},
              {route:'claude-code',bucket:'old',status:'stale',remaining:-5}];
  assert.equal(f.availability(rows).bucket,'five_hour');
  assert.equal(f.usable(rows[0]),0);
  assert.equal(f.quotaValue(rows[0]),'사용 불가');
  assert.equal(f.availability([{route:'x',bucket:'b',status:'stale',remaining:10}]),null);
  assert.equal(f.modelQuota({route:'codex',bucket:'codex_bengalfox · 300분'}),true);
  assert.equal(f.modelQuota({route:'codex',bucket:'codex · 300분'}),false);
  assert.equal(f.levelOf(15,15),'level-low');
  assert.equal(f.levelOf(40,15),'level-mid');
  assert.equal(f.levelOf(80,15),'level-ok');
});

test('budget state takes the larger share and never prices an unpriced month',()=>{
  const st=f.budgetState({tokens:900,cost:12},{tokens:1000,usd:10});
  assert.equal(st.ratio,1.2);assert.equal(st.level,'level-low');
  assert.match(st.text,/900\/1K 토큰 · \$12\/\$10/);
  assert.equal(f.budgetState({tokens:10,cost:null},{usd:10}).text,'비용 미산정');
  assert.equal(f.budgetState(undefined,{tokens:100}).ratio,0);
});

test('scope text, source groups and chart ticks',()=>{
  assert.equal(f.scopeText('route:codex'),'서비스: OpenAI / Codex');
  assert.equal(f.scopeText('plain'),'plain');
  assert.equal(f.sourceGroup('WSL .claude/projects'),'Claude');
  assert.equal(f.sourceGroup('외부 알림'),'운영');
  assert.equal(f.sourceGroup('session-backfill'),'내부 작업');
  assert.equal(f.sourceGroup('mystery'),'기타');
  assert.equal(f.niceStep(1000),250);
  assert.equal(f.niceStep(0),0.25);
});

test('what-if pricing uses the cached and write rates with input fallbacks',()=>{
  const rows=[{uncached_input:1e6,cached_input:1e6,output:1e6,cache_creation:1e6},{output:2e6}];
  assert.equal(f.estimateWith(rows,{input:2,cached:0.5,output:10,cache_write:3}),2+0.5+10+3+20);
  assert.equal(f.estimateWith(rows,{input:2,output:10}),2+2+10+2+20);
});

test('cost CSV distinguishes unpriced, partial, free and unused values and totals',()=>{
 const fields=values=>[...f.costCsvFields(values)];
 assert.deepEqual(fields([{uncached_input:10,unpriced:10,cost:0}]),['','',10,10]);
 assert.deepEqual(fields([{uncached_input:10,unpriced:0,cost:0}]),[0,0,0,0]);
 assert.deepEqual(fields([{}]),[0,0,0,0]);
 assert.deepEqual(fields([{uncached_input:20,unpriced:10,cost:3}]),[3,3,10,10]);
 assert.deepEqual(fields([{uncached_input:10,unpriced:10,cost:0},{output:5,cost:2}]),['',2,2,10,0,10]);
 // Known free usage alongside unpriced usage remains a partial known zero.
 assert.deepEqual(fields([{uncached_input:20,unpriced:10,cost:0}]),[0,0,10,10]);
});

test('short countdown and KST stamps for dense rows',()=>{
  assert.equal(f.soon(null),'미제공');
  assert.equal(f.soon(0),'재확인 대기');
  assert.equal(f.soon(20),'1분');
  assert.equal(f.soon(42*60+30),'42분');
  assert.equal(f.soon(3*3600+59*60+59),'3시간 59분');
  assert.equal(f.soon(2*86400+23*3600+59*60),'2일 23시간');
  const now=Date.UTC(2026,9,10,4,0)/1000;
  assert.equal(f.stamp(Date.UTC(2026,9,11,4,14)/1000,now),'10. 11. 13:14');
  assert.equal(f.stamp(Date.UTC(2026,11,31,15,30)/1000,now),'2027. 01. 01. 00:30');
});

test('expiring resources: within a day, grouped per hour, never past, used or folded',()=>{
 const now=1_800_000_000,H=3600;
 const grant=(id,after,extra={})=>({id,amount:1,expires:now+after,status:'available',...extra});
 const items=[
  {id:'codex-reset',route:'codex',kind:'reset',label:'Codex 초기화권',unit:'count',status:'fresh',amount:5,grants:[
   grant('a',59*60),grant('b',59*60+30),grant('c',2*86400),grant('d',-86400,{status:'expired'}),grant('e',-60),grant('f',2*H,{status:'used'}),grant('g',3*H,{amount:0})]},
  {id:'credit',route:'claude-code',kind:'usage_credit',label:'Claude 사용 크레딧',unit:'USD',status:'manual',amount:50,expires:now+4*H,expiry_is_partial:true},
  {id:'later',route:'claude-code',kind:'usage_credit',unit:'USD',status:'fresh',amount:9,expires:now+86400+1},
  {id:'empty',route:'codex',kind:'usage_credit',unit:'credit',status:'fresh',amount:0,expires:now+H},
  {id:'old-account',route:'codex',kind:'usage_credit',unit:'credit',status:'previous_account',amount:3,expires:now+H},
  {id:'dup',route:'codex',kind:'usage_credit',unit:'credit',status:'fresh',amount:3,expires:now+H,duplicate_of:'credit',effective:false},
  {id:'allowance',route:'claude-code',kind:'usage_credit',unit:'USD',status:'fresh',amount:20,allowance:true,expires:now+H},
  {id:'ended',route:'codex',kind:'usage_credit',unit:'credit',status:'fresh',amount:4,expires:now-1},
  {id:'stale',route:'codex',kind:'api_credit',unit:'USD',status:'stale',amount:2,expires:now+30*60},
 ];
 const out=f.expiringResources(items,now);
 const plain=v=>JSON.parse(JSON.stringify(v));
 assert.deepEqual(plain(out.map(e=>[e.id,e.amount,e.stale])),[['stale',2,true],['codex-reset',2,false],['credit',50,false]]);
 assert.equal(out[1].expires,now+59*60);assert.deepEqual(plain(out[1].ids),['a','b']);
 assert.equal(out[2].partial,true);assert.match(out[2].ids[0],/^credit:/);
 // Grants a few hours apart stay separate entries.
 const split=f.expiringResources([{id:'r',route:'codex',kind:'reset',unit:'count',status:'fresh',amount:2,grants:[grant('x',H/2),grant('y',5*H)]}],now);
 assert.deepEqual(plain(split.map(e=>[e.ids[0],e.amount])),[['x',1],['y',1]]);
 assert.equal(f.expiringResources(null,now).length,0);
 assert.equal(f.expiringResources(items,now,10).length,0);
});
