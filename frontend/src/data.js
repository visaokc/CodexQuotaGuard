export const COLORS = ['#669cff','#f6b763','#55d6be','#cd8af0','#ed8299','#c6db76','#f39777','#62cde2'];
export const COLOR_NAMES = ['蓝色','琥珀','薄荷','薰衣草','玫瑰','青柠','珊瑚','晴青'];
const STANDARD_MODELS=['gpt-6-astra','gpt-5.6-sol','gpt-5.6-terra','gpt-5.6-luna'];
const MODEL_NAMES={'gpt-6-astra':'GPT-6 Astra','gpt-5.6-sol':'GPT-5.6 Sol','gpt-5.6-terra':'GPT-5.6 Terra','gpt-5.6-luna':'GPT-5.6 Luna'};
export function displayModelName(model){return MODEL_NAMES[model]||model;}
function hiddenModel(model){return model==='codex-auto-review'||model==='unknown';}
export function cycleUnknown(cycle){return (cycle.models||[]).filter(row=>hiddenModel(row.model)).reduce((sum,row)=>sum+(row.tokens||0),0);}
export function cycleModels(cycle){
  const rows=cycle.models||[],total=cycle.sampled_tokens??rows.reduce((n,m)=>n+m.tokens,0);
  return [...new Set([...STANDARD_MODELS,...rows.map(m=>m.model)])].filter(model=>!hiddenModel(model)).map(model=>{
    const tokens=rows.find(m=>m.model===model)?.tokens||0;
    return {model,tokens,usage_share:total>0?tokens/total*100:0};
  });
}
export function officialQuotaRemaining(used){
  if(typeof used!=='number'||!Number.isFinite(used))return null;
  return Math.min(100,Math.max(0,100-used));
}
export function officialUsageColor(used){
  const remaining=officialQuotaRemaining(used);
  if(remaining===null)return null;
  return remaining<10?'#ed8d98':remaining<20?'#e8bf75':null;
}
export function dailyQuotaUsage(epoch,pending=false){
  if(pending||!epoch)return null;
  const {used,baseline,started,observed_at:observed}=epoch;
  if(![used,baseline,started,observed].every(value=>typeof value==='number'&&Number.isFinite(value))||observed<=started||used<baseline)return null;
  return (used-baseline)*86400/(observed-started);
}
export function refreshRemaining(resetAt,now){
  if(typeof resetAt!=='number'||!Number.isFinite(resetAt)||!Number.isFinite(now))return '—';
  const seconds=resetAt-now;
  if(seconds<=0)return '等待刷新';
  if(seconds<3600)return Math.ceil(seconds/60)+' 分钟';
  if(seconds<=48*3600)return (seconds/3600).toFixed(1)+' 小时';
  return (seconds/86400).toFixed(1)+' 天';
}
export function compact(value, digits=2) {
  if (value == null || !Number.isFinite(Number(value))) return '—';
  const n=Number(value), a=Math.abs(n);
  if(a>=1e9) return (n/1e9).toFixed(digits)+'B';
  if(a>=1e6) return (n/1e6).toFixed(digits)+'M';
  if(a>=1e3) return (n/1e3).toFixed(digits)+'k';
  return Number.isInteger(n) ? String(n) : n.toFixed(digits);
}
export function devicesFor(snapshot) {
  const account=snapshot.view?.display_account||snapshot.view?.identity?.account;
  const settings=snapshot.settings||{};
  const current=snapshot.view?.identity?.account;
  const notes={...settings.device_notes?.[current],...settings.device_notes?.[account]}, colors={...settings.device_colors?.[current],...settings.device_colors?.[account]};
  const ids=(snapshot.view?.summary?.devices||[]).filter(d=>!d.removed).map(d=>d.id).sort();
  const saved=settings.device_order?.[account]||settings.device_order?.[current]||[], order=[...saved.filter(id=>ids.includes(id)),...ids.filter(id=>!saved.includes(id))];
  return (snapshot.view?.summary?.devices||[]).filter(d=>!d.removed).map(d=>({
    ...d,label:notes[d.id]||d.device_ids?.map(id=>notes[id]).find(Boolean)||d.name||d.id.slice(0,10),
    color:COLORS.includes(colors[d.id])?colors[d.id]:d.avatar?({'person1':'#cd8af0','person2':'#669cff','person3':'#55d6be'}[d.id]||COLORS[0]):COLORS[ids.indexOf(d.id)%COLORS.length],
    local:d.local===true||d.id===settings.device_id
  })).sort((a,b)=>Number(b.local)-Number(a.local)||order.indexOf(a.id)-order.indexOf(b.id));
}
export function modelVersionOrder(a,b){
  const version=model=>(model.match(/\d+(?:\.\d+)*/)?.[0]||'').split('.').filter(Boolean).map(Number);
  const left=version(a),right=version(b);
  for(let i=0;i<Math.max(left.length,right.length);i++){const difference=(right[i]||0)-(left[i]||0);if(difference)return difference;}
  const leftRank=STANDARD_MODELS.indexOf(a),rightRank=STANDARD_MODELS.indexOf(b);
  return leftRank>=0&&rightRank>=0?leftRank-rightRank:a.localeCompare(b);
}
export function modelOptions(snapshot){
  const data=snapshot.view?.analytics||{},account=snapshot.view?.display_account||snapshot.view?.identity?.account;
  return [{value:'',label:'全部模型'},...(data.account===account?data.models||[]:[])
    .filter(model=>!hiddenModel(model))
    .sort(modelVersionOrder)
    .map(value=>({value,label:displayModelName(value)}))];
}
export function aggregate(snapshot, window, model='', device='', accountFilter='') {
  const data=snapshot.view?.analytics||{}, account=snapshot.view?.display_account||snapshot.view?.identity?.account;
  const active=devicesFor(snapshot), valid=new Set(active.map(d=>d.id));
  const source=data.account===account ? data.windows?.[window]||{} : {};
  const quotaUnavailable=snapshot.view?.shared_group?.enabled&&snapshot.view.shared_group.stage==='preparing'?'计费待启用':'';
  const points=Array(Math.max(1,source.count||1)).fill(0), totals={},quotaPoints=points.slice(),quotaTotals={},quotaPendingPoints=points.map(()=>false),quotaEstimatedPoints=points.map(()=>false),quotaStates={};
  const series=active.filter(d=>!device||device===d.id).map(d=>({id:d.id,label:d.label,color:d.color,points:points.slice(),quotaPoints:points.slice(),quotaPendingPoints:points.map(()=>false)}));
  const byDevice=new Map(series.map(item=>[item.id,item]));
  const cacheQuotaPoints=points.slice(),cacheQuotaTotals={},cacheMissingPoints=points.map(()=>false),cacheMissingTotals={};
  for(const item of series){item.cachePoints=points.slice();item.cacheQuotaPoints=points.slice();item.cacheMissingPoints=points.map(()=>false);}
  for(const row of source.rows||[]) {
    if(!valid.has(row.device)||(model&&row.model!==model)||(device&&row.device!==device)||(accountFilter&&row.account!==accountFilter)) continue;
    const tokens=Number(row.tokens)||0;
    if(Number.isInteger(row.bucket)&&row.bucket>=0&&row.bucket<points.length){
      points[row.bucket]+=tokens;
      byDevice.get(row.device).points[row.bucket]+=tokens;
      const cache=byDevice.get(row.device).cachePoints;
      if(row.detail_missing>0||row.cache_tokens==null||cache[row.bucket]===null){
        cache[row.bucket]=null;
        byDevice.get(row.device).cacheMissingPoints[row.bucket]=true;
        cacheMissingPoints[row.bucket]=true;cacheMissingTotals[row.device]=true;
      }
      else cache[row.bucket]+=Number(row.cache_tokens)||0;
    }
    totals[row.device]=(totals[row.device]||0)+tokens;
  }
  for(const row of (quotaUnavailable?[]:source.quota_rows||[])) {
    const item=byDevice.get(row.device);
    if(!item||(accountFilter&&row.account!==accountFilter)||(model&&row.model!==model)||!Number.isInteger(row.bucket)||row.bucket<0||row.bucket>=points.length)continue;
    const d=active.find(d=>d.id===row.device),cap=d.fair_base_cap??d.cap;
    const personal=['personal','fair'].includes(snapshot.settings?.quota_display||'personal');
    const multiplier=personal&&cap>0?100/cap:(snapshot.view?.shared_group?.stage==='billing'?.5:1);
    const value=Number(row.quota)*multiplier;
    item.quotaPoints[row.bucket]+=value;quotaPoints[row.bucket]+=value;
    quotaTotals[row.device]=(quotaTotals[row.device]||0)+value;
    if(typeof row.cache_quota==='number'&&Number.isFinite(row.cache_quota)){
      const cached=row.cache_quota*multiplier;
      item.cacheQuotaPoints[row.bucket]+=cached;cacheQuotaPoints[row.bucket]+=cached;
      cacheQuotaTotals[row.device]=(cacheQuotaTotals[row.device]||0)+cached;
    }else{
      item.cacheMissingPoints[row.bucket]=true;cacheMissingPoints[row.bucket]=true;cacheMissingTotals[row.device]=true;
    }
  }
  for(const row of source.quota_pending_rows||[]) {
    const item=byDevice.get(row.device);
    if(!item||(accountFilter&&row.account!==accountFilter)||(model&&row.model!==model)||!Number.isInteger(row.bucket)||row.bucket<0||row.bucket>=points.length)continue;
    item.quotaPendingPoints[row.bucket]=true;quotaPendingPoints[row.bucket]=true;
    quotaStates[row.device]='pending';
  }
  if(window==='cycle'&&!model&&!accountFilter) {
    for(const d of active) if(!device||device===d.id) totals[d.id]=Number(d.tokens)||0;
    for(const item of series){item.points.fill(0);item.points[0]=totals[item.id]||0;}
    points.fill(0);
    points[0]=Object.values(totals).reduce((a,b)=>a+b,0);
  }
  const cacheTotals=Object.fromEntries(series.map(item=>[item.id,item.cachePoints.includes(null)?null:item.cachePoints.reduce((a,b)=>a+b,0)]));
  return {quotaUnavailable,cacheTotals,cacheQuotaPoints,cacheQuotaTotals,cacheMissingPoints,cacheMissingTotals,quotaPendingPoints,quotaEstimatedPoints,quotaStates,quotaPoints,quotaTotals,quotaReady:!quotaUnavailable&&source.quota_ready===true,points,series,totals,total:Object.values(totals).reduce((a,b)=>a+b,0),start:source.start||0,step:source.step||1};
}
export function historyWindow(source,end,duration=3600){
  const count=duration/source.step,start=Math.floor(end/source.step)*source.step-(count-1)*source.step;
  const offset=Math.round((start-source.start)/source.step),result={...source,start,count};
  if(typeof source.quota_available==='boolean')result.quota_ready=source.quota_available&&!(source.quota_gaps||[]).some(g=>g.end>start&&g.start<end);
  for(const key of ['rows','quota_rows','quota_pending_rows','quota_estimate_rows'])result[key]=(source[key]||[]).filter(row=>row.bucket>=offset&&row.bucket<offset+count).map(row=>({...row,bucket:row.bucket-offset}));
  return result;
}
export function historyMinimum(snapshot,source,duration,lookback,model='',device='',account=''){
  const step=duration===86400?3600:duration===3600?60:300,latest=Math.floor((snapshot.view?.analytics?.at||0)/step)*step;
  const ids=new Set(devicesFor(snapshot).map(d=>d.id));
  const rows=(source?.rows||[]).filter(r=>ids.has(r.device)&&(!device||r.device===device)&&(!model||r.model===model)&&(!account||r.account===account)&&r.tokens>0);
  if(!rows.length)return latest;
  const first=Math.min(...rows.map(r=>source.start+r.bucket*source.step));
  return Math.min(latest,Math.max(latest-lookback,first+duration-source.step));
}
export function userBreakdown(snapshot,device,window='cycle'){
  const source=snapshot.view?.analytics,account=snapshot.view?.display_account||snapshot.view?.identity?.account;
  const rows=source?.account===account?(source.windows?.[window]?.rows||[]).filter(row=>row.device===device):[];
  const sum=items=>{
    const value={};
    for(const key of ['tokens','cache_tokens','input_tokens','output_tokens','reasoning_tokens','reasoning_count','reasoning_missing','event_count','detail_count'])value[key]=items.reduce((n,row)=>n+(Number(row[key])||0),0);
    value.detail_missing=items.reduce((n,row)=>n+(row.detail_missing??row.tokens),0);
    value.miss_tokens=value.input_tokens-value.cache_tokens;
    value.non_reasoning_tokens=value.output_tokens-value.reasoning_tokens;
    value.hit_rate=value.input_tokens>0?value.cache_tokens/value.input_tokens*100:null;
    value.first_at=Math.min(...items.map(row=>row.first_at||Infinity));
    value.last_at=Math.max(0,...items.map(row=>row.last_at||0));
    value.coverage=value.tokens>0?(value.tokens-value.detail_missing)/value.tokens*100:100;
    return value;
  };
  const fixed=STANDARD_MODELS;
  const totals=sum(rows),extra=[...new Set(rows.map(row=>row.model))].filter(model=>!fixed.includes(model));
  const models=[...fixed,...extra].filter(model=>!hiddenModel(model)).map(model=>({model,...sum(rows.filter(row=>row.model===model))}));
  for(const model of models)model.usage_share=totals.tokens>0?model.tokens/totals.tokens*100:0;
  const quota=aggregate(snapshot,window,'',device);
  return {...totals,models,quota:quota.quotaUnavailable||chartQuotaPercent(quota.quotaTotals[device],quota.quotaReady,quota.quotaStates[device]==='pending',quota.quotaStates[device]==='estimated'),cacheQuota:quota.quotaUnavailable||chartQuotaPercent(quota.cacheQuotaTotals[device],quota.quotaReady&&!quota.cacheMissingTotals[device],quota.quotaStates[device]==='pending',quota.quotaStates[device]==='estimated')};
}
export function chartQuotaPercent(value,ready,pending=false,estimated=false,digits=2){
  if(!ready)return '—';
  if(pending&&!(value>0))return '待更新';
  return (value||0).toFixed(digits)+'%';
}
export function trendForMode(data,mode){
  if(mode!=='cache')return {...data,mode};
  const series=data.series.map(item=>({...item,points:item.cachePoints.slice(),quotaPoints:item.cacheQuotaPoints}));
  const points=data.points.map((_,i)=>series.some(item=>item.points[i]===null)?null:series.reduce((sum,item)=>sum+item.points[i],0));
  const totals=Object.fromEntries(series.map(item=>[item.id,item.points.includes(null)?null:item.points.reduce((a,b)=>a+b,0)]));
  return {...data,mode,series,points,totals,total:points.includes(null)?null:points.reduce((a,b)=>a+b,0),quotaPoints:data.cacheQuotaPoints,quotaTotals:data.cacheQuotaTotals};
}
export function quotaValue(device, mode='personal') {
  const used=Number(device.estimated),personal=mode===true||mode==='personal'||mode==='fair';
  const cap=device.fair_base_cap??device.cap;
  return personal ? (cap>0?(used+(mode==='fair'?(device.carry||0):0))/cap*100:null) : used;
}
export function niceScale(max) {
  if(!(max>0)) return 4;
  const raw=max/4, base=10**Math.floor(Math.log10(raw));
  return ([1,1.25,1.5,2,2.5,3,4,5,6,8,10].find(x=>x*base>=raw)||10)*base*4;
}
// Monotone interpolation preserves extrema and never invents negative usage.
export function linePath(values,width=416,height=122,maximum=niceScale(Math.max(0,...values))) {
  if(values.includes(null)){
    let result='',index=0;
    while(index<values.length){
      if(values[index]===null){index++;continue;}
      const start=index;
      while(index<values.length&&values[index]!==null)index++;
      const spacing=values.length>1?width/(values.length-1):0;
      const geometry=curveGeometry(values.slice(start,index),(index-start-1)*spacing,height,maximum);
      for(const point of geometry.p)point.x+=start*spacing;
      result+=geometryPath(geometry);
    }
    return result;
  }
  return geometryPath(curveGeometry(values,width,height,maximum));
}
function geometryPath({p,m}){
  if(!p.length)return '';
  if(p.length===1)return `M${p[0].x},${p[0].y}`;
  return `M${p[0].x},${p[0].y}`+p.slice(1).map((v,i)=>{const dx=(v.x-p[i].x)/3;
    return `C${p[i].x+dx},${p[i].y+m[i]*dx} ${v.x-dx},${v.y-m[i+1]*dx} ${v.x},${v.y}`;}).join('');
}
export function curveGeometry(values,width=416,height=122,maximum=niceScale(Math.max(0,...values))) {
  const p=values.map((v,i)=>({x:values.length>1?i*width/(values.length-1):width/2,y:height-Math.max(0,v)/maximum*height}));
  if(p.length<2)return {p,m:[0]};
  const slopes=p.slice(1).map((v,i)=>(v.y-p[i].y)/(v.x-p[i].x));
  const m=p.map((_,i)=>i===0?slopes[0]:i===p.length-1?slopes.at(-1):slopes[i-1]*slopes[i]<=0?0:(slopes[i-1]+slopes[i])/2);
  for(let i=0;i<slopes.length;i++) {
    if(slopes[i]===0){m[i]=m[i+1]=0;continue;}
    const a=m[i]/slopes[i],b=m[i+1]/slopes[i], s=a*a+b*b;
    if(s>9){const t=3/Math.sqrt(s);m[i]=t*a*slopes[i];m[i+1]=t*b*slopes[i];}
  }
  return {p,m};
}
export function curveY(geometry,x){
  const {p,m}=geometry;
  if(!p.length)return 0;
  if(p.length===1)return p[0].y;
  const index=Math.min(p.length-2,Math.max(0,Math.floor(x/(p[1].x-p[0].x))));
  const a=p[index],b=p[index+1],dx=b.x-a.x,t=Math.min(1,Math.max(0,(x-a.x)/dx));
  return (2*t**3-3*t*t+1)*a.y+(t**3-2*t*t+t)*dx*m[index]+(-2*t**3+3*t*t)*b.y+(t**3-t*t)*dx*m[index+1];
}
export function timeLabel(ts,seconds=false) {
  if(!ts)return '—';
  const d=new Date(ts*1000), pad=n=>String(n).padStart(2,'0');
  return `${pad(d.getMonth()+1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}${seconds?':'+pad(d.getSeconds()):''}`;
}
