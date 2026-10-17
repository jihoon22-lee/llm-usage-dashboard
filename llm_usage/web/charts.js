'use strict';
// Usage chart, its tooltip, token composition and model ranking.
let chartData=null,chartWidth=0,chartFrame=0;
function chart(data){
 $('chart').onoutside=null;
 $('chart').onpointerleave=null;
 chartData=data;
 if(!$('chart').clientWidth)return;
 const grouped=chartSeries(data),allLabels=grouped.labels;
 const labels=allLabels.filter(l=>!hiddenLabels.has(l)),points=grouped.points;
 const colorOf=paletteFor(allLabels,groupSlots($('group').value,allLabels));
 $('legend').innerHTML=allLabels.map(label=>`<button data-label="${esc(label)}" aria-pressed="${!hiddenLabels.has(label)}">${esc(seriesLabel(label))}</button>`).join('');
 $('legend').insertAdjacentHTML('beforeend',missingRoutes(data).map(row=>`<span class="unavailable-series">${esc(routeNames[row.route]||row.route)} · ${gapLabel(row)}</span>`).join(''));
 $('legend').querySelectorAll('[data-label]').forEach(el=>{el.style.setProperty('--color',colorOf(el.dataset.label));el.onclick=()=>{hiddenLabels.has(el.dataset.label)?hiddenLabels.delete(el.dataset.label):hiddenLabels.add(el.dataset.label);chart(data);};});
 if(data.compare&&!$('cumulative').value.startsWith('share'))$('legend').insertAdjacentHTML('beforeend',`<span class="unavailable-series" data-tip="${esc(data.compare.label)}: ${esc(data.compare.start.slice(0,10))} ~ ${esc(data.compare.end_exclusive.slice(0,10))} (종료 미포함)">⎯⎯ ${esc(data.compare.label)} 합계</span>`);
 if(!labels.length){$('chart').innerHTML='<div class="empty">표시할 항목을 범례에서 선택하세요. 기록이 없으면 수집 후 표시됩니다.</div>';return;}
 const mode=$('cumulative').value,cumulative=mode==='1',share=mode.startsWith('share');
 const cmp=grouped.compare,cmpPoints=(cmp&&cmp.series)||[];
 const cmpTotal=i=>{const vals=(cmpPoints[i]||{}).values||{};return labels.reduce((s,label)=>s+metricValue(vals[label]),0);};
 const cmpTotals=cmpPoints.map((_,i)=>cmpTotal(i));
 if(cumulative){let run=0;cmpTotals.forEach((v,i)=>{cmpTotals[i]=run+=v;});}
 // The viewBox matches the container so axis text renders near its CSS size.
 const w=Math.max(240,Math.round($('chart').clientWidth)||1080);
 chartWidth=w;
 const narrow=w<560,h=narrow?190:275,l=narrow?42:57,r=narrow?10:15,top=narrow?14:20,b=narrow?28:35,plot=w-l-r;
 let max=1;for(const p of points){let sum=0;for(const label of labels){const v=metricValue(p.values[label]);sum+=v;if(cumulative)max=Math.max(max,v);}if(!cumulative)max=Math.max(max,sum);}
 if(cmp&&!share)cmpTotals.forEach(v=>{max=Math.max(max,v);});
 if(share)max=100;
 const step=share?25:niceStep(max),ceil=step*Math.ceil(max/step);
 const x=i=>l+(i+.5)/Math.max(1,points.length)*plot,y=v=>h-b-v/ceil*(h-top-b);
 let svg='';
 for(let i=0,n=Math.round(ceil/step);i<=n;i++){const yy=y(step*i);svg+=`<line x1="${l}" x2="${w-r}" y1="${yy}" y2="${yy}" class="chart-gridline"/><text x="${l-8}" y="${yy+4}" text-anchor="end">${share?step*i+'%':compact(step*i)}</text>`;}
 const maxX={hour:8,day:7,week:6,month:6}[$('granularity').value]||6,xn=Math.min(maxX,points.length,Math.max(2,Math.floor(plot/72)));
 for(let i=0;i<xn;i++){const index=Math.round(i*(points.length-1)/Math.max(1,xn-1));svg+=`<text x="${x(index)}" y="${h-8}" text-anchor="${i===0?'start':i===xn-1?'end':'middle'}">${esc(bucketLabel(points[index],true))}</text>`;}
 // The bucket that contains now is still filling up; shade it and say so.
 const last=points.at(-1),nowMs=Date.now();
 if(last&&last.range_end_exclusive&&Date.parse(last.range_start||last.time)<=nowMs&&nowMs<Date.parse(last.range_end_exclusive)){
  const band=plot/Math.max(1,points.length);
  svg=`<rect class="chart-current" x="${(x(points.length-1)-band/2).toFixed(1)}" y="${top}" width="${band.toFixed(1)}" height="${h-top-b}"/><text class="chart-current-label" x="${x(points.length-1).toFixed(1)}" y="${top+10}" text-anchor="middle">진행 중</text>`+svg;
 }
 if(cmp&&!share&&cmpTotals.some(v=>v>0)){
  svg+=`<path d="${cmpTotals.map((v,i)=>`${i?'L':'M'}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(' ')}" fill="none" stroke="var(--muted,#888)" stroke-width="1.8" stroke-dasharray="5 4" opacity=".75"/>`;
 }
 if(cumulative){
  labels.forEach(label=>{
   const color=colorOf(label);
   const path=points.map((p,i)=>`${i?'L':'M'}${x(i).toFixed(1)},${y(metricValue(p.values[label])).toFixed(1)}`).join(' ');
   svg+=`<path d="${path}" fill="none" stroke="${color}" stroke-width="2.5"/>`;
   const stride=Math.max(1,Math.floor(points.length/200));
   points.forEach((p,i)=>{if(i%stride===0)svg+=`<circle cx="${x(i)}" cy="${y(metricValue(p.values[label]))}" r="3" fill="${color}"></circle>`;});
  });
 }else if(mode==='share-area'||mode==='share-line'){
  const stacks=points.map(p=>{const denom=labels.reduce((s,l)=>s+metricValue(p.values[l]||{}),0);let acc=0;return labels.map(l=>{const v=denom?metricValue(p.values[l]||{})/denom*100:0;const a=acc;acc+=v;return[a,acc];});});
  labels.forEach((label,li)=>{
   const edge=points.map((p,i)=>`${i?'L':'M'}${x(i).toFixed(1)},${y(mode==='share-line'?stacks[i][li][1]-stacks[i][li][0]:stacks[i][li][1]).toFixed(1)}`).join(' ');
   if(mode==='share-line')svg+=`<path d="${edge}" fill="none" stroke="${colorOf(label)}" stroke-width="2.5"/>`;
   else{const floor=points.map((p,i)=>`L${x(i).toFixed(1)},${y(stacks[i][li][0]).toFixed(1)}`).reverse().join(' ');svg+=`<path d="${edge}${floor}Z" fill="${colorOf(label)}" opacity=".78"/>`;}
  });
 }else{
  const width=Math.max(.5,Math.min(65,plot/Math.max(1,points.length)*.68));
  points.forEach((p,i)=>{const denom=labels.reduce((s,l)=>s+metricValue(p.values[l]),0);let sum=0;labels.forEach(label=>{const v=metricValue(p.values[label]),hv=share&&denom?v/denom*100:v;if(hv)svg+=`<rect x="${x(i)-width/2}" y="${y(sum+hv)}" width="${width}" height="${y(sum)-y(sum+hv)}" fill="${colorOf(label)}"></rect>`;sum+=hv;});});
 }
 $('chart').innerHTML=`<svg tabindex="0" aria-describedby="chart-keyboard-hint" class="chart" viewBox="0 0 ${w} ${h}" data-l="${l}" data-r="${w-r}" role="img" aria-label="${modeAria[mode]} · ${metricName()} 사용 추이">${svg}<line class="chart-cursor" x1="0" x2="0" y1="${top}" y2="${h-b}" visibility="hidden"/></svg><span id="chart-keyboard-hint" class="sr-only">좌우 방향키로 날짜를 선택하고 Enter로 그 구간을 확대하며 Escape로 상세를 닫습니다.</span><div id="chart-tooltip" class="chart-tooltip" role="status" aria-live="polite" hidden></div>`;
 bindChartTooltip(data,grouped,{w,h,l,r,top,b,x,plot},cmpTotals);
}
function bindChartTooltip(data,grouped,geometry,comparisonTotals){
 const root=$('chart'),svg=root.querySelector('svg'),tooltip=$('chart-tooltip'),cursor=root.querySelector('.chart-cursor');
 const colorOf=paletteFor(grouped.labels,groupSlots($('group').value,grouped.labels));
 // Long lists sit below the plot so scrolling never intercepts a bucket tap.
 const scrollable=grouped.labels.length>8;
 tooltip.classList.toggle('scrollable',scrollable);
 let selected=-1,armed=-1,lastPointer='';
 function hide(){tooltip.hidden=true;cursor.setAttribute('visibility','hidden');selected=-1;armed=-1;}
 function show(index){
  index=Math.max(0,Math.min(grouped.points.length-1,index));
  const p=grouped.points[index];if(!p)return;
  selected=index;
  const date=bucketLabel(p);
  const shareMode=$('cumulative').value.startsWith('share');
  const allTotal=grouped.labels.filter(l=>!hiddenLabels.has(l)).reduce((sum,label)=>sum+metricValue(p.values[label]||{}),0);
  const cmp=grouped.compare,cmpP=(cmp&&cmp.series||[])[index];
  const cmpTotal=cmpP?comparisonTotals[index]:null;
  const showRange=data.period==='1h'||['week','month'].includes($('granularity').value);
  const rangeText=p.range_start&&showRange?(data.period==='1h'
   ?when(Date.parse(p.range_start)/1000)+' ~ '+when(Date.parse(p.range_end_exclusive)/1000)
   :p.range_start.slice(0,10)+' ~ '+p.range_end_exclusive.slice(0,10)):'';
  tooltip.innerHTML=`<div class="tooltip-heading"><strong>${esc(date)} KST</strong><small>${modeNames[$('cumulative').value]}${p.partial&&showRange?' · 일부 기간':''}${rangeText?'<br>'+esc(rangeText)+' (종료 미포함)':''}</small></div>${grouped.labels.map(label=>`<div class="tooltip-row${hiddenLabels.has(label)?' muted-series':''}"><span><svg width="9" height="9" aria-hidden="true"><circle cx="4" cy="4" r="4" fill="${colorOf(label)}"/></svg> ${esc(seriesLabel(label))}${hiddenLabels.has(label)?' · 숨김':''}</span><strong>${shareMode?percent(!hiddenLabels.has(label)&&allTotal?metricValue(p.values[label]||{})/allTotal*100:null):metricFmt(metricValue(p.values[label]||{}))+costNote(p.values[label])}</strong></div>`).join('')}${missingRoutes(data).map(row=>`<div class="tooltip-row tooltip-missing"><span>${esc(routeNames[row.route]||row.route)} · ${gapLabel(row)}</span><strong>—</strong></div>`).join('')}<div class="tooltip-total"><span>표시 항목 합계</span><strong>${metricFmt(allTotal)}</strong></div>${cmpTotal!=null&&!shareMode?`<div class="tooltip-row tooltip-missing"><span>⎯⎯ ${esc(cmp.label)} 합계<small>${esc((cmpP.compared_time||cmpP.time).slice(0,10))} 시작</small></span><strong>${metricFmt(cmpTotal)}</strong></div>`:''}${armed===index?'<div class="tooltip-hint">한 번 더 탭하면 이 구간을 확대합니다</div>':''}`;
  tooltip.hidden=false;
  const center=geometry.x(index);
  cursor.setAttribute('x1',center);cursor.setAttribute('x2',center);cursor.setAttribute('visibility','visible');
  const box=svg.getBoundingClientRect(),local=root.getBoundingClientRect();
  const wanted=box.left-local.left+center/geometry.w*box.width+14;
  tooltip.style.left=scrollable?'0':Math.max(0,Math.min(wanted,local.width-tooltip.offsetWidth))+'px';
  tooltip.style.top=scrollable?'0':'12px';
 }
 function pick(event){
  const point=svg.createSVGPoint();point.x=event.clientX;point.y=event.clientY;
  const matrix=svg.getScreenCTM();if(!matrix)return;
  const local=point.matrixTransform(matrix.inverse());
  if(local.x<geometry.l||local.x>geometry.w-geometry.r||local.y<geometry.top||local.y>geometry.h-geometry.b){
   if(!scrollable||local.y<=geometry.h-geometry.b)hide();
   return;
  }
  const index=Math.min(grouped.points.length-1,Math.floor((local.x-geometry.l)/geometry.plot*grouped.points.length));
  if(index!==selected||tooltip.hidden)show(index);
 }
 function zoom(index){
  const p=grouped.points[index];if(!p||!p.range_start)return;
  const g=$('granularity').value;if(g==='hour')return;
  if(data.period==='1h'){applyDrill({granularity:'hour'});return;}
  const until=new Date(p.range_end_exclusive.slice(0,10)+'T00:00:00Z');until.setUTCDate(until.getUTCDate()-1);
  applyDrill({period:'custom',start:p.range_start.slice(0,10),end:until.toISOString().slice(0,10),
              granularity:g==='day'?'hour':'day'});
 }
 svg.addEventListener('pointermove',pick);
 svg.addEventListener('pointerdown',event=>{lastPointer=event.pointerType||'';pick(event);});
 svg.addEventListener('click',()=>{
  if(selected<0)return;
  const p=grouped.points[selected];
  const zoomable=!!(p&&p.range_start)&&$('granularity').value!=='hour';
  // Touch drills in two steps so the tooltip stays reachable: the first tap on
  // a bucket only arms it; a second tap on the same bucket zooms in.
  if(lastPointer==='touch'&&zoomable&&armed!==selected){armed=selected;show(selected);return;}
  armed=-1;zoom(selected);
 });
 svg.addEventListener('pointerleave',event=>{if(event.pointerType!=='touch'&&!(scrollable&&root.contains(event.relatedTarget)))hide();});
 root.onpointerleave=event=>{if(event.pointerType!=='touch')hide();};
 svg.addEventListener('focus',()=>{if(selected<0)show(0);});
 svg.addEventListener('blur',event=>{if(!tooltip.contains(event.relatedTarget))hide();});
 tooltip.tabIndex=0;
 tooltip.addEventListener('keydown',event=>{if(event.key==='Escape'){svg.focus();hide();event.preventDefault();}});
 svg.addEventListener('keydown',event=>{
  if(event.key==='Escape'){hide();event.preventDefault();}
  else if(event.key==='Enter'&&selected>=0){zoom(selected);event.preventDefault();}
  else if(['ArrowLeft','ArrowRight','Home','End'].includes(event.key)){
   const index=event.key==='Home'?0:event.key==='End'?grouped.points.length-1:Math.max(0,selected)+(event.key==='ArrowLeft'?-1:1);
   show(index);event.preventDefault();
  }
 });
 root.onoutside=hide;
}
document.addEventListener('pointerdown',event=>{if(!$('chart').contains(event.target))$('chart').onoutside?.();});
window.addEventListener('resize',()=>{$('chart').onoutside?.();fitSvgText(document);});
// Redraw on real width changes; hidden tabs report 0 and are skipped until shown.
new ResizeObserver(()=>{
 fitSvgText(document);
 const w=Math.round($('chart').clientWidth);
 if(Math.abs(w-chartWidth)<8)return;
 chartWidth=w;
 if(!w)return;
 cancelAnimationFrame(chartFrame);
 chartFrame=requestAnimationFrame(()=>{if(chartData&&$('chart').clientWidth)chart(chartData);});
}).observe($('chart'));
let donutExcludeCache=storage.get('llmDonutNoCache')!=='0',donutExcludeResidual=storage.get('llmDonutNoResidual')!=='0';
function donutToggle(id,key,get,set){
 const button=$(id);
 button.setAttribute('aria-pressed',String(get()));button.classList.toggle('on',get());
 button.addEventListener('click',()=>{
  set(!get());storage.set(key,get()?'1':'0');
  button.setAttribute('aria-pressed',String(get()));button.classList.toggle('on',get());
  if(lastUsage)composition(lastUsage);
 });
}
donutToggle('donut-nocache','llmDonutNoCache',()=>donutExcludeCache,v=>{donutExcludeCache=v;});
donutToggle('donut-noresidual','llmDonutNoResidual',()=>donutExcludeResidual,v=>{donutExcludeResidual=v;});
// Token types of the period. The Codex account residual has no type: it is its own muted
// item, apart from other supplements without a breakdown (내역 미분류), and like cache
// reads it can be left out of the ring while its key row stays.
function composition(data){
 const residual=data.totals.residual_tokens||0;
 const parts={...data.totals,unclassified_tokens:(data.totals.unclassified_tokens||0)-residual,residual_tokens:residual};
 const tint=i=>i<0?mutedColor:colors[i];
 const all=[['일반 입력','uncached_input',0],['캐시 읽기','cached_input',1],['출력','output',2],['캐시 생성','cache_creation',3],['내역 미분류','unclassified_tokens',4],[residualName,'residual_tokens',-1]]
  .filter(([,key])=>!['unclassified_tokens','residual_tokens'].includes(key)||parts[key]);
 const out=key=>(donutExcludeCache&&key==='cached_input')||(donutExcludeResidual&&key==='residual_tokens'&&residual>0);
 $('donut-noresidual').hidden=!residual;$('donut-noresidual').textContent=residualName+' 제외';
 const grand=total(data.totals),excluded=all.filter(([,key])=>out(key));
 const sum=grand-excluded.reduce((n,[,key])=>n+(parts[key]||0),0);
 const segments=all.filter(([,key])=>!out(key)).sort((a,b)=>(parts[b[1]]||0)-(parts[a[1]]||0));
 if(!sum){$('composition').innerHTML='<div class="empty">'+(grand?'제외한 항목만 있습니다. 위의 제외 버튼으로 다시 표시할 수 있습니다.':'선택 기간에 사용 기록이 없습니다.')+'</div>';return;}
 const circumference=2*Math.PI*76;let offset=0;
 const rings=segments.map(([name,key,color])=>{const value=parts[key],length=value/sum*circumference;const circle=`<circle cx="110" cy="110" r="76" fill="none" stroke="${tint(color)}" stroke-width="24" stroke-dasharray="${length} ${circumference-length}" stroke-dashoffset="${-offset}" transform="rotate(-90 110 110)" data-tip="${esc(name)}: ${fmt(value)} (${(value/sum*100).toFixed(1)}%)"/>`;offset+=length;return circle;}).join('');
 // Excluded items stay in the key as muted rows so exclusion never hides data.
 const dot=color=>`<svg width="9" height="9" aria-hidden="true"><circle cx="4" cy="4" r="4" fill="${tint(color)}"/></svg>`;
 const keyRows=all.slice().sort((a,b)=>(parts[b[1]]||0)-(parts[a[1]]||0)).map(([name,key,color])=>{
  if(out(key)){
   const value=parts[key],pct=grand?(value/grand*100).toFixed(1):'0.0';
   return `<div class="excluded" data-token="${key}" data-tip="${esc(name)}: ${fmt(value)} (${pct}%) · 차트 제외"><strong>${dot(color)} ${esc(name)}</strong><span>${fmt(value)} · ${pct}% (차트 제외)</span></div>`;
  }
  return `<div data-token="${key}" data-tip="${esc(name)}: ${fmt(parts[key])} (${(parts[key]/sum*100).toFixed(1)}%)"><strong>${dot(color)} ${esc(name)}</strong><span>${fmt(parts[key])} · ${(parts[key]/sum*100).toFixed(1)}%</span></div>`;
 });
 const names=excluded.map(([name])=>name),cut=names.length?(names.length>1?`${names.length}개 항목`:names[0])+' 제외':'';
 $('composition').innerHTML=`<div class="donut-wrap"><svg class="donut" viewBox="0 0 220 220" role="img" aria-label="${esc(names.length?names.join('·')+' 제외 ':'')}비중순 토큰 구성 도넛 차트">${rings}<text x="110" y="${cut?103:109}" text-anchor="middle" font-size="26" font-weight="600">${compact(sum)}</text><text x="110" y="${cut?123:130}" text-anchor="middle" font-size="11">${cut?esc(cut.length>14?names.length+'개 항목 제외':cut):'전체 토큰'}</text>${cut?`<text x="110" y="140" text-anchor="middle" font-size="11" class="donut-sub">전체 ${compact(grand)}</text>`:''}</svg><div class="donut-key">${keyRows.join('')}</div></div>`;
}
function ranking(data){
 const metric=$('metric').value;
 const rowValue=r=>metric==='requests'?(r.requests||0):metric==='cost'?(r.est_cost??0):modelTotal(r);
 // The Codex account residual is not a model: shares are of model-attributed use, and the
 // residual follows on a muted line (tokens only; it has no requests or cost).
 const residual=data.rows.filter(r=>r.model===RESIDUAL);
 const rows=data.rows.filter(r=>r.model!==RESIDUAL).sort((a,b)=>rowValue(b)-rowValue(a));const max=Math.max(1,...rows.map(rowValue));
 $('ranking-caption').textContent=`전체 ${rows.length}개 · 구독 경로 구분${residual.length?' · 비중은 모델이 기록된 사용량 기준':''}`;
 const denom=rows.reduce((s,r)=>s+rowValue(r),0);
 const share=r=>denom?percent(rowValue(r)/denom*100):'—';
 const colorOf=paletteFor(rows.map(r=>r.model));
 const extra=residual.map(r=>{
  const v=metric==='tokens'?modelTotal(r):null,label=routeNames[r.route]||r.route,all=denom+(v||0);
  const note=v==null?`${metricName()} 없음`:`전체의 ${all?percent(v/all*100):'—'} · 순위 제외`;
  return `<div class="rank rank-residual"><div class="rank-label"><strong>${esc(residualName)} <small>${esc(label)} · 모델 구분 없음</small></strong><span>${v==null?'—':metricFmt(v)} <small>${note}</small></span></div>`
   +(v?`<svg class="rank-bar" viewBox="0 0 1000 13" preserveAspectRatio="none" role="img" aria-label="${esc(residualName)} ${metricFmt(v)} · ${note}"><rect width="${Math.min(1,v/max)*1000}" height="13" fill="${mutedColor}" data-tip="${esc(residualName)} · ${esc(label)} · ${metricFmt(v)} · ${note}"/></svg>`:'')+'</div>';
 }).join('');
 $('ranking').innerHTML=(rows.map(r=>{const v=rowValue(r),label=routeNames[r.route]||r.route;
  return `<div class="rank" data-scope="model:${esc(r.model)}" tabindex="0" aria-describedby="drill-hint"><div class="rank-label"><strong>${esc(r.model)} <small>${esc(label)}</small></strong><span>${metricFmt(v)} <small>${share(r)}</small></span></div><svg class="rank-bar" viewBox="0 0 1000 13" preserveAspectRatio="none" role="img" aria-label="${esc(r.model)} ${metricFmt(v)} · ${share(r)}"><rect width="${v/max*1000}" height="13" fill="${colorOf(r.model)}" data-tip="${esc(r.model)} · ${esc(label)} · ${metricFmt(v)} · ${share(r)} · 클릭하면 이 모델로 좁혀봅니다"/></svg></div>`;}).join('')+extra)||'<div class="empty">선택 기간에 사용 기록이 없습니다.</div>';
}
