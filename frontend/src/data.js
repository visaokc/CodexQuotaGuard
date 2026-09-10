export const COLORS = ['#669cff','#f6b763','#55d6be','#cd8af0','#ed8299','#c6db76','#f39777','#62cde2'];
export const COLOR_NAMES = ['蓝色','琥珀','薄荷','薰衣草','玫瑰','青柠','珊瑚','晴青'];
export function officialQuotaRemaining(used){
  if(typeof used!=='number'||!Number.isFinite(used))return null;
  return Math.min(100,Math.max(0,100-used));
}
export function officialUsageColor(used){
  const remaining=officialQuotaRemaining(used);
  if(remaining===null)return null;
  return remaining<10?'#ed8d98':remaining<20?'#e8bf75':null;
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
  const account=snapshot.view?.identity?.account;
  const settings=snapshot.settings||{};
  const notes=settings.device_notes?.[account]||{}, colors=settings.device_colors?.[account]||{};
  const ids=(snapshot.view?.summary?.devices||[]).filter(d=>!d.removed).map(d=>d.id).sort();
  const saved=settings.device_order?.[account]||[], order=[...saved.filter(id=>ids.includes(id)),...ids.filter(id=>!saved.includes(id))];
  return (snapshot.view?.summary?.devices||[]).filter(d=>!d.removed).map(d=>({
    ...d,label:notes[d.id]||d.name||d.id.slice(0,10),
    color:COLORS.includes(colors[d.id])?colors[d.id]:COLORS[ids.indexOf(d.id)%COLORS.length],
    local:d.id===settings.device_id
  })).sort((a,b)=>order.indexOf(a.id)-order.indexOf(b.id));
}
export function modelVersionOrder(a,b){
  const version=model=>(model.match(/\d+(?:\.\d+)*/)?.[0]||'').split('.').filter(Boolean).map(Number);
  const left=version(a),right=version(b);
  for(let i=0;i<Math.max(left.length,right.length);i++){const difference=(right[i]||0)-(left[i]||0);if(difference)return difference;}
  return a.localeCompare(b);
}
export function modelOptions(snapshot){
  const data=snapshot.view?.analytics||{},account=snapshot.view?.identity?.account;
  return [{value:'',label:'全部模型'},...(data.account===account?data.models||[]:[])
    .filter(model=>model!=='codex-auto-review')
    .sort(modelVersionOrder)
    .map(value=>({value,label:value}))];
}
export function aggregate(snapshot, window, model='', device='') {
  const data=snapshot.view?.analytics||{}, account=snapshot.view?.identity?.account;
  const active=devicesFor(snapshot), valid=new Set(active.map(d=>d.id));
  const source=data.account===account ? data.windows?.[window]||{} : {};
  const points=Array(Math.max(1,source.count||1)).fill(0), totals={};
  for(const row of source.rows||[]) {
    if(!valid.has(row.device)||(model&&row.model!==model)||(device&&row.device!==device)) continue;
    const tokens=Number(row.tokens)||0;
    if(Number.isInteger(row.bucket)&&row.bucket>=0&&row.bucket<points.length) points[row.bucket]+=tokens;
    totals[row.device]=(totals[row.device]||0)+tokens;
  }
  if(window==='cycle'&&!model) {
    for(const d of active) if(!device||device===d.id) totals[d.id]=Number(d.tokens)||0;
    points[0]=Object.values(totals).reduce((a,b)=>a+b,0);
  }
  return {points,totals,total:Object.values(totals).reduce((a,b)=>a+b,0),start:source.start||0,step:source.step||1};
}
export function quotaValue(device, personal) {
  const used=Number(device.estimated);
  return personal ? (device.cap>0?used/device.cap*100:null) : used;
}
export function niceScale(max) {
  if(!(max>0)) return 4;
  const raw=max/4, base=10**Math.floor(Math.log10(raw));
  return ([1,2,2.5,5,10].find(x=>x*base>=raw)||10)*base*4;
}
// Monotone interpolation preserves extrema and never invents negative usage.
export function linePath(values,width=416,height=122,maximum=niceScale(Math.max(0,...values))) {
  const {p,m}=curveGeometry(values,width,height,maximum);
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
