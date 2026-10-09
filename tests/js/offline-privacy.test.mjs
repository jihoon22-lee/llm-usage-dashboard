import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
const source=fs.readFileSync(new URL('../../llm_usage/web/core.js',import.meta.url),'utf8');
function setup(){
 const entries=new Map([['/api/config', {saved:1,data:{notify:{url:'https://example.invalid/secret'}}}],['/api/config/notify?x=1',{saved:1,data:{secret:true}}],['/api/usage?period=7d',{saved:1,data:{totals:42}}]]);
 const cache={keys:async()=>[...entries.keys()].map(path=>({url:'https://dashboard.test'+path})),delete:async key=>entries.delete(typeof key==='string'?key:new URL(key.url).pathname+new URL(key.url).search),put:async(path,response)=>entries.set(path,await response.json()),match:async path=>entries.has(path)?new Response(JSON.stringify(entries.get(path))):undefined};
 const context=vm.createContext({URL,Response,Date,Set,Promise,AbortController,DOMException,setTimeout,clearTimeout,location:{origin:'https://dashboard.test'},window:{dispatchEvent(){}},Event,caches:{open:async()=>cache,delete:async()=>{entries.clear();return true;}},fetch:async()=>{throw Error('offline');}});
 vm.runInContext(source.slice(source.indexOf('const OFFLINE_CACHE='),source.indexOf('function percentChart')),context);
 return {entries,run:code=>vm.runInContext(code,context)};
}
test('legacy secrets are purged before a statistics cache read',async()=>{
 const {entries,run}=setup();assert.equal((await run("lastCopy('/api/usage?period=7d')")).data.totals,42);
 assert.equal(entries.has('/api/config'),false);assert.equal(entries.has('/api/config/notify?x=1'),false);
});
test('only exact same-origin statistical GET paths can be retained or returned',async()=>{
 const {entries,run}=setup();
 for(const path of ['/api/config','/api/config?x=1','/api/bootstrap','/api/notify/log','/api/collection','/api/usage-secret','https://external.test/api/usage']){
  await run(`keepCopy(${JSON.stringify(path)},{secret:true})`);assert.equal(await run(`lastCopy(${JSON.stringify(path)})`),null);assert.equal(entries.has(path),false);
 }
 await run("keepCopy('/api/usage?period=all',{totals:123})");assert.equal((await run("lastCopy('/api/usage?period=all')")).data.totals,123);
});
test('cache API denial never masks a successful online response',async()=>{
 const {run}=setup();
 await run("offlineCacheReady=null;caches.open=async()=>{throw Error('denied')};fetch=async()=>new Response(JSON.stringify({totals:7}))");
 assert.equal((await run("api('/api/usage')")).totals,7);
 assert.equal(await run("lastCopy('/api/usage')"),null);
});
test('clear removes statistics and next online read can retain a fresh copy',async()=>{
 const {entries,run}=setup();await run('clearStatisticsCache()');assert.equal(entries.size,0);
 await run("keepCopy('/api/limits',{limits:[]})");assert.equal(entries.size,1);
});
test('successful writes and bootstrap responses never enter statistics cache',async()=>{
 const {entries,run}=setup();await run('clearStatisticsCache()');
 await run("csrf='synthetic-csrf';fetch=async()=>new Response(JSON.stringify({csrf:'synthetic-secret'}))");
 await run("api('/api/usage',{action:'write'})");
 await run("api('/api/bootstrap?refresh=1')");
 assert.equal(entries.size,0);
});
test('failed migration cannot expose an old cached configuration',async()=>{
 const {run}=setup();await run("statisticsCache()");
 await run("offlineCacheReady=null;caches.open=async()=>{throw Error('blocked')}");
 assert.equal(await run("lastCopy('/api/config')"),null);
});

test('a stalled write times out once and reports an unknown server outcome',async()=>{
 const {run}=setup();
 await run("csrf='synthetic';window.calls=0;fetch=()=>{window.calls++;return new Promise(()=>{})}");
 await assert.rejects(run("api('/api/config/value-alert',{value_alert_usd:1},false,{timeout:10})"),/서버 처리 결과/);
 assert.equal(run('window.calls'),1);
});
test('the same deadline bounds a stalled response body',async()=>{
 const {run}=setup();
 await run("fetch=async()=>({ok:true,status:200,json:()=>new Promise(()=>{})})");
 await assert.rejects(run("api('/api/collection',undefined,false,{timeout:10})"),/시간/);
});
test('superseding a request does not invalidate settings or read an old copy',async()=>{
 const {run}=setup();
 await run("window.unavailable=0;window.dispatchEvent=()=>window.unavailable++;fetch=()=>new Promise(()=>{});window.cancel=new AbortController()");
 const request=run("api('/api/usage?period=7d',undefined,false,{signal:window.cancel.signal})");
 run('window.cancel.abort()');
 await assert.rejects(request,{name:'AbortError'});
 assert.equal(run('window.unavailable'),0);
});
test('a late response after timeout cannot become a fresh cached answer',async()=>{
 const {entries,run}=setup();
 await run("fetch=()=>new Promise(resolve=>window.late=resolve)");
 await assert.rejects(run("api('/api/usage?period=uncached',undefined,false,{timeout:10})"),/시간/);
 await run("window.late(new Response(JSON.stringify({late:true})))");
 await new Promise(resolve=>setTimeout(resolve,0));
 assert.equal(entries.has('/api/usage?period=uncached'),false);
});
