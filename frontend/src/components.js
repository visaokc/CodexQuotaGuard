import {ref, computed, watch, onMounted, onBeforeUnmount, nextTick} from 'vue';
import {compact,niceScale,linePath,timeLabel,curveGeometry,curveY} from './data.js';

export const SelectBox={
  props:{modelValue:{default:''},options:{default:()=>[]},label:String},emits:['update:modelValue'],
  setup(props,{emit}) {
    const open=ref(false),anchor=ref(null),menu=ref(null),position=ref({}),active=ref(0);
    const items=computed(()=>props.options.map(o=>typeof o==='string'?{value:o,label:o}:o));
    const current=computed(()=>items.value.find(o=>o.value===props.modelValue)?.label||props.label||'请选择');
    async function toggle() {
      open.value=!open.value;
      if(!open.value)return;
      active.value=Math.max(0,items.value.findIndex(o=>o.value===props.modelValue));
      const r=anchor.value.getBoundingClientRect(), below=innerHeight-r.bottom-10;
      position.value={left:Math.max(8,Math.min(r.left,innerWidth-Math.max(r.width,160)-8))+'px',
        width:Math.max(r.width,160)+'px',maxHeight:Math.min(300,Math.max(below,r.top-10))+'px',
        ...(below<150?{bottom:innerHeight-r.top+5+'px'}:{top:r.bottom+5+'px'})};
      await nextTick();menu.value?.focus();
    }
    function choose(item){emit('update:modelValue',item.value);open.value=false;anchor.value?.focus();}
    function outside(e){if(open.value&&!anchor.value?.contains(e.target)&&!menu.value?.contains(e.target))open.value=false;}
    function key(e){
      if(e.key==='Escape'){open.value=false;anchor.value?.focus();}
      else if(['ArrowDown','ArrowUp'].includes(e.key)){e.preventDefault();active.value=(active.value+(e.key==='ArrowDown'?1:-1)+items.value.length)%items.value.length;menu.value?.children[active.value]?.scrollIntoView({block:'nearest'});}
      else if(e.key==='Enter'&&items.value[active.value]){e.preventDefault();choose(items.value[active.value]);}
      else if(e.key==='Tab')open.value=false;
    }
    onMounted(()=>document.addEventListener('pointerdown',outside));
    onBeforeUnmount(()=>document.removeEventListener('pointerdown',outside));
    return {open,anchor,menu,position,active,items,current,toggle,choose,key};
  },
  template:`<div class="select"><button ref="anchor" type="button" class="select-trigger" :class="{expanded:open}" @click="toggle" :aria-label="label||current" :title="current" aria-haspopup="listbox" :aria-expanded="open"><slot><span>{{current}}</span></slot><svg viewBox="0 0 12 12" width="12" height="12"><path d="m3 4.5 3 3 3-3"/></svg></button><Teleport to="body"><Transition name="menu"><div v-if="open" ref="menu" class="select-menu" :style="position" role="listbox" tabindex="-1" @keydown="key"><button v-for="(item,index) in items" :key="item.value" class="select-option" :class="{chosen:item.value===modelValue,focused:index===active}" @pointerenter="active=index" @click="choose(item)" role="option" :aria-selected="item.value===modelValue"><span>{{item.label}}</span><span v-if="item.value===modelValue" class="check">✓</span></button></div></Transition></Teleport></div>`
};

export const TrendChart={
  props:{data:{required:true},kind:String,color:{default:'#669cff'},quotaTotal:{default:null}},
  setup(props){
    const animated=ref([]),hover=ref(-1),mouseX=ref(0),plot=ref(null);
    const bounds=ref({left:0,top:0,width:430,svgTop:0,svgHeight:140});
    let frame=0,lastTarget='';
    const width=384,height=112;
    const focused=ref('');
    const series=computed(()=>props.data.series||[{id:'total',label:'Token',color:props.color,points:props.data.points}]);
    watch(series,values=>{
      const signature=JSON.stringify(values);
      if(signature===lastTarget)return;
      lastTarget=signature;
      hover.value=-1;
      cancelAnimationFrame(frame);
      const target=values.map(item=>({...item,points:item.points.slice()})),before=new Map(animated.value.map(item=>[item.id,item.points])),start=performance.now();
      const tick=at=>{const t=Math.min(1,(at-start)/300),ease=1-(1-t)**3;animated.value=target.map(item=>({...item,points:item.points.map((v,i)=>{
        const prior=before.get(item.id),old=prior?.length===item.points.length?prior[i]:0;return old+(v-old)*ease;
      })}));if(t<1)frame=requestAnimationFrame(tick);};
      frame=requestAnimationFrame(tick);
    },{immediate:true});
    onBeforeUnmount(()=>cancelAnimationFrame(frame));
    const shown=computed(()=>props.data.points.map((_,i)=>animated.value.reduce((total,item)=>total+(item.points[i]||0),0)));
    const maximum=computed(()=>niceScale(Math.max(0,...(props.kind==='bar'?props.data.points:series.value.flatMap(item=>item.points)))));
    const curves=computed(()=>animated.value.map(item=>({...item,path:linePath(item.points,width,height,maximum.value),geometry:curveGeometry(item.points,width,height,maximum.value)})));
    function area(path){return `${path}L${shown.value.length>1?width:width/2},${height}L${shown.value.length>1?0:width/2},${height}Z`;}
    const barWidth=computed(()=>Math.min(24,step.value*.66));
    const singleColor=computed(()=>series.value[0]?.color||props.color);
    const dates=computed(()=>[0,0.5,1].map(x=>timeLabel(props.data.start+x*props.data.step*props.data.points.length)));
    const step=computed(()=>width/Math.max(1,shown.value.length));
    const dailyLabels=computed(()=>props.kind==='bar'&&props.data.points.length===7&&props.data.step===86400?props.data.points.map((_,i)=>({x:44+(i+.5)*width/7,label:timeLabel(props.data.start+i*86400).slice(0,5)})):[]);
    const bars=computed(()=>{
      const totals=props.data.points.map(()=>0);
      return animated.value.map(item=>({...item,buckets:item.points.map((value,i)=>{
        totals[i]+=value;return {value,y:height-totals[i]/maximum.value*height};
      })}));
    });
    const markers=computed(()=>curves.value.map(item=>({id:item.id,color:item.color,y:curveY(item.geometry,mouseX.value)})));
    const point=computed(()=>hover.value<0?null:props.kind==='bar'?{x:(hover.value+.5)*step.value,y:height-(shown.value[hover.value]||0)/maximum.value*height}:{x:mouseX.value,y:Math.min(height,...markers.value.map(item=>item.y))});
    const tooltipStyle=computed(()=>{
      if(!point.value)return {};
      const box=bounds.value,multiple=series.value.length>1,tipWidth=multiple?220:180,tipHeight=43+(multiple?series.value.length*18+3:0);
      const center=Math.max(tipWidth/2+4,Math.min(box.width-tipWidth/2-4,box.width*Math.max(.14,Math.min(.72,(point.value.x/width*.85)+.09))));
      const left=Math.max(0,(center-tipWidth/2-7)/box.width*430-44),right=Math.min(width,(center+tipWidth/2+7)/box.width*430-44);
      const ys=[];
      if(props.kind==='bar'){
        for(let i=Math.max(0,Math.floor(left/step.value));i<=Math.min(shown.value.length-1,Math.floor(right/step.value));i++)ys.push(height-shown.value[i]/maximum.value*height);
        ys.push(height);
      }else for(const curve of curves.value)for(let x=left;x<=right;x+=2)ys.push(curveY(curve.geometry,x));
      if(!ys.length)ys.push(height);
      const origin=box.svgTop+7*box.svgHeight/140,scale=box.svgHeight/140;
      const peak=origin+Math.min(...ys)*scale,lowest=origin+Math.max(...ys)*scale,baseline=box.top+13;
      const overlaps=peak<baseline+tipHeight+9&&lowest>baseline-9;
      return {position:'fixed',left:box.left+center+'px',top:Math.max(8,overlaps?Math.min(baseline,peak-tipHeight-10):baseline)+'px',width:tipWidth+'px',minHeight:tipHeight+'px'};
    });
    function move(e){
      const svg=plot.value.ownerSVGElement,r=svg.getBoundingClientRect(),container=svg.parentElement.getBoundingClientRect();
      const next={left:container.left,top:container.top,width:container.width,svgTop:r.top,svgHeight:r.height};
      if(Object.keys(next).some(key=>next[key]!==bounds.value[key]))bounds.value=next;
      const x=Math.max(0,Math.min(width,(e.clientX-r.left)/r.width*430-44));mouseX.value=x;hover.value=Math.min(props.data.points.length-1,Math.max(0,props.kind==='bar'?Math.floor(x/step.value):Math.round(x/width*(props.data.points.length-1))));
    }
    function quotaPercent(value){return props.quotaTotal>0?(value/props.quotaTotal*100).toFixed(2)+'%':'—';}
    function share(id){const total=props.quotaTotal;return total>0?((props.data.totals?.[id]||0)/total*100).toFixed(1)+'%':'—';}
    return {quotaPercent,share,focused,area,barWidth,dailyLabels,shown,series,curves,bars,markers,singleColor,hover,plot,width,height,maximum,dates,step,point,tooltipStyle,move,compact,timeLabel};
  },
  template:`<div class="trend-plot" :style="{'--chart-color':color}"><span class="axis-unit">Token</span><svg class="trend-svg" viewBox="0 0 430 140" preserveAspectRatio="none" aria-label="Token 用量趋势"><defs><clipPath id="trend-reveal"><rect :key="kind" class="curve-reveal" :class="{drawing:kind!=='bar'}" x="-2" y="-7" :width="width+4" :height="height+14"/></clipPath><linearGradient v-for="(curve,index) in curves" :key="curve.id" :id="'trend-fill-'+index" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" :stop-color="curve.color" stop-opacity=".22"/><stop offset="100%" :stop-color="curve.color" stop-opacity=".015"/></linearGradient><clipPath v-for="(value,i) in shown" :key="i" :id="'bar-round-'+i"><rect :x="i*step+(step-barWidth)/2" :y="height-value/maximum*height" :width="barWidth" :height="Math.max(0,value/maximum*height)" rx="3"/></clipPath></defs><g transform="translate(44,7)"><g v-for="i in 5" :key="i"><line x1="0" :x2="width" :y1="(i-1)*height/4" :y2="(i-1)*height/4" class="grid-line"/><text x="-8" :y="(i-1)*height/4+(i===5?-1:3)" text-anchor="end" class="axis-text">{{compact(maximum*(5-i)/4,1)}}</text></g><g ref="plot" @pointermove="move" @pointerleave="hover=-1"><rect data-testid="trend-hit" :width="width" :height="height" fill="transparent"/><g v-if="kind!=='bar'" key="line" class="line-chart" clip-path="url(#trend-reveal)"><path v-for="(curve,index) in curves" :key="'fill-'+curve.id" :d="area(curve.path)" :fill="'url(#trend-fill-'+index+')'" class="trend-area" :class="{dimmed:focused&&focused!==curve.id}"/><path v-for="curve in curves" :key="curve.id" :data-device="curve.id" :d="curve.path" class="trend-line" :class="{dimmed:focused&&focused!==curve.id,focused:focused===curve.id}" :stroke="curve.color"/></g><g v-else key="bar" class="bar-chart"><g v-for="bar in bars" :key="bar.id" :data-device="bar.id"><rect v-for="(bucket,i) in bar.buckets" :key="i" class="chart-bar" :class="{lit:hover===i,dimmed:focused&&focused!==bar.id}" :data-device="bar.id" :x="i*step+(step-barWidth)/2" :y="bucket.y" :width="barWidth" :height="Math.max(0,bucket.value/maximum*height)" :fill="bar.color" :clip-path="'url(#bar-round-'+i+')'"/></g><rect v-if="point" class="hover-band" :x="point.x-step*.48" y="0" :width="step*.96" :height="height" rx="4"/></g><g v-if="point" class="chart-hover" :style="{transform:'translate('+point.x+'px,'+point.y+'px)'}"><line y1="0" :y2="height-point.y" class="hover-line"/><g v-if="kind!=='bar'"><g v-for="marker in markers" :key="marker.id" :transform="'translate(0,'+(marker.y-point.y)+')'"><circle r="7" :fill="marker.color" fill-opacity=".18"/><circle r="3" :fill="marker.color" stroke="#e7efff" stroke-width="1"/></g></g><g v-else><circle r="7" :fill="singleColor" fill-opacity=".18"/><circle r="3" :fill="singleColor" stroke="#e7efff" stroke-width="1"/></g></g></g></g><text v-for="day in dailyLabels" :key="day.x" :x="day.x" y="137" text-anchor="middle" class="axis-text day-label">{{day.label}}</text><text v-if="!dailyLabels.length" x="44" y="137" class="axis-text">{{dates[0]}}</text><text v-if="!dailyLabels.length" x="236" y="137" text-anchor="middle" class="axis-text">{{dates[1]}}</text><text v-if="!dailyLabels.length" x="428" y="137" text-anchor="end" class="axis-text">{{dates[2]}}</text></svg><div class="trend-legend"><span v-for="item in series" :key="item.id" class="trend-legend-item" @pointerenter="focused=item.id" @pointerleave="focused=''" :data-device="item.id" :title="item.label+' · '+compact(data.totals?.[item.id]||0)+' Token'"><i class="trend-legend-dot" :style="{background:item.color}"></i><span class="trend-legend-name">{{item.label}}</span><span class="trend-legend-value">{{compact(data.totals?.[item.id]||0)}}</span><span class="trend-legend-share" title="所选时段用量 / 推算本周期总额度" :style="{color:item.color}">{{share(item.id)}}</span></span></div><Teleport to="body"><Transition name="tip"><div v-if="hover>=0" class="chart-tooltip" :style="tooltipStyle"><strong>{{compact(data.points[hover])}} Token · <span class="tooltip-total-percent" :style="series.length===1?{color:series[0].color}:{}" title="占推算本周期总额度">{{quotaPercent(data.points[hover])}}</span></strong><div v-if="series.length>1" class="tooltip-series"><div v-for="item in series" :key="item.id" class="tooltip-series-row" :data-device="item.id"><i class="trend-legend-dot" :style="{background:item.color}"></i><span class="tooltip-series-name">{{item.label}}</span><span class="tooltip-series-value">{{compact(item.points[hover]||0)}}</span><b class="tooltip-user-percent" :style="{color:item.color}" title="占推算本周期总额度">{{quotaPercent(item.points[hover]||0)}}</b></div></div><span>{{timeLabel(data.start+hover*data.step)}}</span></div></Transition></Teleport></div>`
};

export const DonutChart={
  props:{devices:{required:true},totals:{required:true},quotaTotal:{default:null}},
  setup(props){
    const hover=ref(''),animated=ref({});let frame=0,lastTarget={};
    watch(()=>props.totals,target=>{
      if(Object.keys(target).length===Object.keys(lastTarget).length&&Object.entries(target).every(([k,v])=>v===lastTarget[k]))return;
      lastTarget={...target};
      cancelAnimationFrame(frame);const start=performance.now(),before={...animated.value};
      const tick=at=>{const t=Math.min(1,(at-start)/320),e=1-(1-t)**3;animated.value=Object.fromEntries(Object.entries(target).map(([id,v])=>[id,(before[id]||0)+(v-(before[id]||0))*e]));if(t<1)frame=requestAnimationFrame(tick);};
      frame=requestAnimationFrame(tick);
    },{immediate:true});
    onBeforeUnmount(()=>cancelAnimationFrame(frame));
    const arcs=computed(()=>{let offset=0;const sum=Object.values(animated.value).reduce((a,b)=>a+b,0);return props.devices.map(d=>{const fraction=sum?(animated.value[d.id]||0)/sum:0,angle=(offset+fraction/2)*Math.PI*2-Math.PI/2;
      const gap=Object.values(animated.value).filter(v=>v>0).length>1?Math.min(3,fraction*314.159*.3):0;
      const part={...d,length:fraction*314.159-gap,offset:-offset*314.159-gap/2,dx:Math.cos(angle)*3,dy:Math.sin(angle)*3};offset+=fraction;return part;});});
    const total=computed(()=>props.devices.reduce((sum,d)=>sum+(props.totals[d.id]||0),0));
    function share(id){return props.quotaTotal>0?((props.totals[id]||0)/props.quotaTotal*100).toFixed(1)+'%':'—';}
    return {share,hover,arcs,compact};
  },
  template:`<div class="donut-content"><svg class="donut-svg" viewBox="0 0 140 140" aria-label="各设备 Token 用量占比"><circle cx="70" cy="70" r="50" fill="none" stroke="#282c34" stroke-width="21"/><g v-for="arc in arcs" :key="arc.id" class="donut-piece" :style="{transform:hover===arc.id?'translate('+arc.dx+'px,'+arc.dy+'px)':'translate(0,0)'}"><circle v-if="arc.length>0" cx="70" cy="70" r="50" fill="none" :stroke="arc.color" stroke-width="21" :stroke-dasharray="arc.length+' '+(314.159-arc.length)" :stroke-dashoffset="arc.offset" transform="rotate(-90 70 70)" tabindex="0" :aria-label="arc.label+' '+compact(totals[arc.id]||0)+' Token'" @pointerenter="hover=arc.id" @pointerleave="hover=''" @focus="hover=arc.id" @blur="hover=''"/></g></svg><div class="donut-legend"><div v-for="device in devices" :key="device.id" class="legend-row" @pointerenter="hover=device.id" @pointerleave="hover=''"><span class="legend-dot" :style="{background:device.color}"></span><span class="legend-name" :title="device.label">{{device.label}}</span><em class="donut-share" title="所选时段用量 / 推算本周期总额度" :style="{color:device.color}">{{share(device.id)}}</em><span class="legend-value">{{compact(totals[device.id]||0)}}</span></div><span v-if="!devices.length" class="muted">暂无设备数据</span></div></div>`
};
