import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
const source=fs.readFileSync(new URL('../../llm_usage/web/core.js',import.meta.url),'utf8');
function setup(){
 const entries=new Map([['/api/config', {saved:1,data:{notify:{url:'https://example.invalid/secret'}}}],['/api/config/notify?x=1',{saved:1,data:{secret:true}}],['/api/usage?period=7d',{saved:1,data:{totals:42}}]]);
 const cache={keys:async()=>[...entries.keys()].map(path=>({url:'https://dashboard.test'+path})),delete:async key=>entries.delete(typeof key==='string'?key:new URL(key.url).pathname+new URL(key.url).search),put:async(path,response)=>entries.set(path,await response.json()),match:async path=>entries.has(path)?new Response(JSON.stringify(entries.get(path))):undefined};
 const context=vm.createContext({URL,Response,Date,Set,Promise,location:{origin:'https://dashboard.test'},window:{dispatchEvent(){}},Event,caches:{open:async()=>cache,delete:async()=>{entries.clear();return true;}},fetch:async()=>{throw Error('offline');}});
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
