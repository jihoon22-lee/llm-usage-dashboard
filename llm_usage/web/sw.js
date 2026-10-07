'use strict';
// Notifications and an offline copy of the page. It answers only page loads (network
// first, the last copy when the network fails) and content-versioned assets (kept once
// fetched). Data requests are never intercepted: the page itself keeps its last GET
// answers (core.js) and marks them as offline when it has to use them.
const CACHE='llm-usage-v1';
const VERSIONED=/^[0-9a-f]{12}$/;
self.addEventListener('install',event=>event.waitUntil(self.skipWaiting()));
self.addEventListener('activate',event=>event.waitUntil(self.clients.claim()));
self.addEventListener('fetch',event=>{
 const request=event.request,url=new URL(request.url);
 if(request.method!=='GET'||url.origin!==self.location.origin)return;
 if(request.mode==='navigate'){
  event.respondWith((async()=>{
   const cache=await caches.open(CACHE);
   try{
    const response=await fetch(request);
    if(response.ok)await cache.put('/',response.clone());
    return response;
   }catch(error){
    const copy=await cache.match('/');
    if(copy)return copy;
    throw error;
   }
  })());
 }else if(url.pathname.startsWith('/assets/')&&VERSIONED.test(url.searchParams.get('v')||'')){
  event.respondWith((async()=>{
   const cache=await caches.open(CACHE);
   const hit=await cache.match(request);
   if(hit)return hit;
   const response=await fetch(request);
   if(response.ok){
    // One deploy's assets at a time: drop copies of other versions.
    for(const key of await cache.keys()){
     const old=new URL(key.url);
     if(old.pathname.startsWith('/assets/')&&old.searchParams.get('v')!==url.searchParams.get('v'))await cache.delete(key);
    }
    await cache.put(request,response.clone());
   }
   return response;
  })());
 }
});
self.addEventListener('notificationclick',event=>{
 event.notification.close();
 event.waitUntil((async()=>{
  const url=new URL('/',self.location.origin).href;
  const windows=await self.clients.matchAll({type:'window',includeUncontrolled:true});
  const dashboard=windows.find(client=>new URL(client.url).origin===self.location.origin&&new URL(client.url).pathname==='/');
  if(dashboard)await dashboard.focus();else await self.clients.openWindow(url);
 })());
});
