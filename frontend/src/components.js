import {ref, computed, watch, onMounted, onBeforeUnmount, nextTick} from 'vue';
import {chartQuotaPercent,compact,niceScale,linePath,timeLabel,curveGeometry,curveY} from './data.js';

export const RollingValue={
  props:{value:[String,Number]},
  setup(props){
    const node=ref(null),previous=ref(''),changing=ref(false),direction=ref(1);let timer=0;
    function roll(value,old=''){
      previous.value=old??'';direction.value=parseFloat(String(value).replace(/[^\d.-]/g,''))<parseFloat(String(old).replace(/[^\d.-]/g,''))?-1:1;
      changing.value=false;clearTimeout(timer);
      nextTick(()=>{if(!node.value)return;changing.value=true;node.value.animate([{transform:`translateY(${direction.value*110}%)`,opacity:0},{transform:'translateY(0)',opacity:1}],{duration:420,easing:'cubic-bezier(.2,.7,.2,1)'});timer=setTimeout(()=>changing.value=false,420);});
    }
    watch(()=>props.value,roll);onMounted(()=>roll(props.value));onBeforeUnmount(()=>clearTimeout(timer));
    return {node,previous,changing,direction};
  },
  template:`<span class="rolling-value" :class="{'number-changing':changing}" :data-previous="previous" :style="{'--roll-direction':direction}"><span ref="node">{{value}}</span></span>`
};

export const SelectBox={
  props:{modelValue:{default:''},options:{default:()=>[]},label:String},emits:['update:modelValue'],
  setup(props,{emit}) {
    const open=ref(false),anchor=ref(null),menu=ref(null),position=ref({}),active=ref(0),expanded=ref('');
    const options=computed(()=>props.options.map(o=>typeof o==='string'?{value:o,label:o}:o));
    const items=computed(()=>options.value.flatMap(o=>[o,...(o.children&&expanded.value===o.value?o.children.map(child=>({...child,child:true})):[])]));
    const current=computed(()=>options.value.find(o=>o.value===props.modelValue||o.children?.some(c=>c.value===props.modelValue))?.label||props.label||'请选择');
    async function toggle() {
      open.value=!open.value;
      if(!open.value)return;
      expanded.value='';
      active.value=Math.max(0,items.value.findIndex(o=>o.value===props.modelValue));
      const r=anchor.value.getBoundingClientRect(), below=innerHeight-r.bottom-10;
      position.value={left:Math.max(8,Math.min(r.left,innerWidth-Math.max(r.width,160)-8))+'px',
        width:Math.max(r.width,160)+'px',maxHeight:Math.min(300,Math.max(below,r.top-10))+'px',
        ...(below<150?{bottom:innerHeight-r.top+5+'px'}:{top:r.bottom+5+'px'})};
      await nextTick();menu.value?.focus();
    }
    function choose(item){if(item.children){expanded.value=expanded.value===item.value?'':item.value;return;}emit('update:modelValue',item.value);open.value=false;anchor.value?.focus();}
    function outside(e){if(open.value&&!anchor.value?.contains(e.target)&&!menu.value?.contains(e.target))open.value=false;}
    function key(e){
      if(e.key==='Escape'){open.value=false;anchor.value?.focus();}
      else if(['ArrowDown','ArrowUp'].includes(e.key)){e.preventDefault();active.value=(active.value+(e.key==='ArrowDown'?1:-1)+items.value.length)%items.value.length;menu.value?.children[active.value]?.scrollIntoView({block:'nearest'});}
      else if(e.key==='Enter'&&items.value[active.value]){e.preventDefault();choose(items.value[active.value]);}
      else if(e.key==='Tab')open.value=false;
    }
    onMounted(()=>document.addEventListener('pointerdown',outside));
    onBeforeUnmount(()=>document.removeEventListener('pointerdown',outside));
    return {open,anchor,menu,position,active,expanded,items,current,toggle,choose,key};
  },
  template:`<div class="select"><button ref="anchor" type="button" class="select-trigger" :class="{expanded:open}" @click="toggle" :aria-label="label||current" :title="current" aria-haspopup="listbox" :aria-expanded="open"><slot><span>{{current}}</span></slot><svg viewBox="0 0 12 12" width="12" height="12"><path d="m3 4.5 3 3 3-3"/></svg></button><Teleport to="body"><Transition name="menu"><div v-if="open" ref="menu" class="select-menu" :style="position" role="listbox" tabindex="-1" @keydown="key"><button v-for="(item,index) in items" :key="item.value" class="select-option" :class="{chosen:item.value===modelValue,focused:index===active,child:item.child}" @pointerenter="active=index" @click="choose(item)" role="option" :aria-selected="item.value===modelValue"><span>{{item.label}}<small v-if="item.period">{{item.period}}</small></span><span v-if="item.children" class="check">{{expanded===item.value?'−':'+'}}</span><span v-else-if="item.value===modelValue" class="check">✓</span></button></div></Transition></Teleport></div>`
};

export const TrendChart={
  props:{data:{required:true},kind:String,color:{default:'#669cff'},pannable:Boolean,earliestEnd:Number,sliderLabel:String,sliderStep:{default:60},viewEnd:Number,sliderEnd:Number,latestEnd:Number,liveRevision:Number,live:Boolean},emits:['pan'],
  setup(props,{emit}){
    const animated=ref([]),hover=ref(-1),mouseX=ref(0),plot=ref(null);
    let lastPointer=null;
    function leave(){lastPointer=null;hover.value=-1;}
    watch([()=>props.kind,()=>props.data.mode,()=>props.data.step],leave);
    watch(()=>[props.data.start,props.viewEnd,props.data.points.length],()=>{if(lastPointer)move(lastPointer);},{flush:'post'});
    const sliderMax=computed(()=>Math.floor((Number.isFinite(props.latestEnd)?props.latestEnd:props.data.start+props.data.step*Math.max(1,props.data.points.length))/props.sliderStep)*props.sliderStep),sliderMin=computed(()=>Math.min(sliderMax.value,props.earliestEnd??sliderMax.value));
    function seek(e){const end=Number(e.target.value);emit('pan',end>=sliderMax.value?props.latestEnd:end);}
    const bounds=ref({left:0,top:0,width:430,svgTop:0,svgHeight:140});
    let frame=0,lastTarget='',lastStart=null;
    const width=384,height=112;
    const focused=ref('');
    const series=computed(()=>props.data.series||[{id:'total',label:'Token',color:props.color,points:props.data.points}]);
    watch(series,values=>{
      const signature=JSON.stringify([props.data.start,values]);
      if(signature===lastTarget)return;
      lastTarget=signature;
      cancelAnimationFrame(frame);
      const target=values.map(item=>({...item,points:item.points.slice()})),before=new Map(animated.value.map(item=>[item.id,item.points])),start=performance.now();
      const shift=lastStart===null?0:Math.round((props.data.start-lastStart)/props.data.step);lastStart=props.data.start;
      if(shift)for(const [id,values] of before)before.set(id,values.map((_,i)=>values[i+shift]??0));
      if(shift)animated.value=target.map(item=>({...item,points:item.points.map((v,i)=>v===null?null:before.get(item.id)?.[i]??0)}));
      const tick=at=>{const t=Math.min(1,(at-start)/300),ease=1-(1-t)**3;animated.value=target.map(item=>({...item,points:item.points.map((v,i)=>{
        if(v===null)return null;const prior=before.get(item.id),old=prior?.length===item.points.length?prior[i]:0;return old+(v-old)*ease;
      })}));if(t<1)frame=requestAnimationFrame(tick);};
      frame=requestAnimationFrame(tick);
    },{immediate:true});
    onBeforeUnmount(()=>cancelAnimationFrame(frame));
    const shown=computed(()=>props.data.points.map((_,i)=>animated.value.reduce((total,item)=>total+(item.points[i]||0),0)));
    const requestedMaximum=computed(()=>niceScale(Math.max(0,...(props.kind==='bar'?props.data.points:series.value.flatMap(item=>item.points)))));
    const maximum=ref(requestedMaximum.value);let scaleFrame=0,scaleTarget=maximum.value;
    function resizeScale(target){
      if(target===scaleTarget)return;
      cancelAnimationFrame(scaleFrame);scaleTarget=target;
      const before=maximum.value,started=performance.now();
      const tick=at=>{const t=Math.min(1,(at-started)/280);maximum.value=before+(target-before)*(1-(1-t)**3);if(t<1)scaleFrame=requestAnimationFrame(tick);};
      scaleFrame=requestAnimationFrame(tick);
    }
    watch(requestedMaximum,resizeScale);
    watch(()=>[props.kind,props.data.mode,props.pannable,props.liveRevision,series.value.map(item=>item.id).join('|')].join(':'),()=>resizeScale(requestedMaximum.value));
    onBeforeUnmount(()=>cancelAnimationFrame(scaleFrame));
    const span=computed(()=>props.data.step*Math.max(1,props.data.points.length));
    const liveEnd=ref(props.viewEnd);let clockFrame=0,clockAnchor=performance.now(),clockValue=props.viewEnd;
    watch(()=>props.viewEnd,value=>{clockValue=props.live?Math.max(value,liveEnd.value??value):value;clockAnchor=performance.now();liveEnd.value=clockValue;});
    onMounted(()=>{const tick=at=>{if(props.live&&Number.isFinite(clockValue))liveEnd.value=clockValue+(at-clockAnchor)/1000;clockFrame=requestAnimationFrame(tick);};clockFrame=requestAnimationFrame(tick);});
    onBeforeUnmount(()=>cancelAnimationFrame(clockFrame));
    const visibleStart=computed(()=>props.pannable&&Number.isFinite(props.viewEnd)?(props.live?liveEnd.value:props.viewEnd)-span.value:props.data.start);
    const panOffset=computed(()=>props.pannable?(props.data.start-visibleStart.value)/span.value*width:0);
    const curves=computed(()=>animated.value.map(item=>({...item,path:linePath(item.points,props.pannable?width-step.value:width,height,maximum.value),geometry:curveGeometry(item.points,props.pannable?width-step.value:width,height,maximum.value)})));
    function area(path){return `${path}L${shown.value.length>1?width:width/2},${height}L${shown.value.length>1?0:width/2},${height}Z`;}
    const barWidth=computed(()=>Math.min(24,step.value*.66));
    const cacheCurves=computed(()=>series.value.map(item=>({...item,path:linePath(item.cachePoints,props.kind==='bar'||props.pannable?width-step.value:width,height,maximum.value)})));
    function cachePercent(value,missing,pending,estimated){return props.data.quotaUnavailable||chartQuotaPercent(value,props.data.quotaReady&&!missing,pending,estimated);}
    function cacheTotalAt(index){const values=series.value.map(item=>item.cachePoints[index]);return values.includes(null)?null:values.reduce((a,b)=>a+b,0);}
    const singleColor=computed(()=>series.value[0]?.color||props.color);
    const dates=computed(()=>[0,0.5,1].map(x=>timeLabel(props.pannable?visibleStart.value+x*span.value:props.data.start+x*props.data.step*props.data.points.length)));
    const step=computed(()=>width/Math.max(1,shown.value.length));
    const dailyLabels=computed(()=>props.kind==='bar'&&props.data.points.length===7&&props.data.step===86400?props.data.points.map((_,i)=>({x:44+(i+.5)*width/7,label:timeLabel(props.data.start+i*86400).slice(0,5)})):[]);
    const bars=computed(()=>{
      const totals=props.data.points.map(()=>0);
      return animated.value.map(item=>({...item,buckets:item.points.slice(0,totals.length).map((value,i)=>{
        totals[i]+=value||0;return {value,y:height-totals[i]/maximum.value*height};
      })}));
    });
    const markers=computed(()=>curves.value.filter(item=>item.points[hover.value]!=null).map(item=>({id:item.id,color:item.color,y:Math.max(0,Math.min(height,curveY(item.geometry,mouseX.value)))})));
    const point=computed(()=>hover.value<0?null:props.kind==='bar'?{x:(hover.value+.5)*step.value+panOffset.value,y:height-(shown.value[hover.value]||0)/maximum.value*height}:{x:mouseX.value+panOffset.value,y:Math.min(height,...markers.value.map(item=>item.y))});
    const tooltipStyle=computed(()=>{
      if(!point.value)return {};
      const box=bounds.value,multiple=series.value.length>1,tipWidth=props.data.quotaUnavailable?278:238,tipHeight=multiple?99+series.value.length*(props.data.mode==='cache'?25:47):113;
      const center=Math.max(tipWidth/2+4,Math.min(box.width-tipWidth/2-4,box.width*Math.max(.14,Math.min(.72,(point.value.x/width*.85)+.09))));
      const left=Math.max(0,(center-tipWidth/2-7)/box.width*430-44-panOffset.value),right=Math.min(width,(center+tipWidth/2+7)/box.width*430-44-panOffset.value);
      const ys=[];
      if(props.kind==='bar'){
        for(let i=Math.max(0,Math.floor(left/step.value));i<=Math.min(shown.value.length-1,Math.floor(right/step.value));i++)ys.push(height-shown.value[i]/maximum.value*height);
        ys.push(height);
      }else for(const curve of curves.value)ys.push(curveY(curve.geometry,left),curveY(curve.geometry,right),...curve.geometry.p.filter(p=>p.x>=left&&p.x<=right).map(p=>p.y));
      if(!ys.length)ys.push(height);
      const origin=box.svgTop+7*box.svgHeight/140,scale=box.svgHeight/140;
      const peak=origin+Math.min(...ys)*scale,lowest=origin+Math.max(...ys)*scale,baseline=box.top+13;
      const overlaps=peak<baseline+tipHeight+9&&lowest>baseline-9;
      return {position:'fixed',left:box.left+center+'px',top:Math.max(8,overlaps?Math.min(baseline,peak-tipHeight-10):baseline)+'px',width:tipWidth+'px',minHeight:tipHeight+'px'};
    });
    function move(e){
      if(!plot.value)return;
      lastPointer={clientX:e.clientX,clientY:e.clientY};
      const svg=plot.value.ownerSVGElement,r=svg.getBoundingClientRect(),container=svg.parentElement.getBoundingClientRect();
      const next={left:container.left,top:container.top,width:container.width,svgTop:r.top,svgHeight:r.height};
      if(Object.keys(next).some(key=>next[key]!==bounds.value[key]))bounds.value=next;
      const x=Math.max(0,Math.min(width,(e.clientX-r.left)/r.width*430-44-panOffset.value));mouseX.value=x;hover.value=Math.min(props.data.points.length-1,Math.max(0,props.kind==='bar'?Math.floor(x/step.value):props.pannable?Math.round(x/step.value):Math.round(x/width*(props.data.points.length-1))));
    }
    function quotaPercent(value,pending,estimated,missing){return props.data.quotaUnavailable||chartQuotaPercent(value,props.data.quotaReady&&!(props.data.mode==='cache'&&missing),pending,estimated);}
    function share(id){return props.data.quotaUnavailable||chartQuotaPercent(props.data.quotaTotals?.[id],props.data.quotaReady&&!(props.data.mode==='cache'&&props.data.cacheMissingTotals[id]),props.data.quotaStates?.[id]==='pending',props.data.quotaStates?.[id]==='estimated',1);}
    return {sliderMax,sliderMin,span,seek,panOffset,cacheTotalAt,cacheCurves,cachePercent,quotaPercent,share,focused,area,barWidth,dailyLabels,shown,series,curves,bars,markers,singleColor,hover,plot,width,height,maximum,dates,step,point,tooltipStyle,move,leave,compact,timeLabel};
  },
  template:`<div class="trend-plot" :style="{'--chart-color':color}"><span class="axis-unit">Token<span v-if="data.mode==='combined'&&kind!=='bar'"> · 虚线：缓存</span></span><svg class="trend-svg" viewBox="0 0 430 140" preserveAspectRatio="none" aria-label="Token 用量趋势"><defs><clipPath id="trend-viewport"><rect x="0" y="-7" :width="width" :height="height+14"/></clipPath><clipPath id="trend-reveal"><rect :key="kind+data.mode+liveRevision" class="curve-reveal" :class="{drawing:kind!=='bar'}" x="-2" y="-7" :width="width+4" :height="height+14"/></clipPath><linearGradient v-for="(curve,index) in curves" :key="curve.id" :id="'trend-fill-'+index" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" :stop-color="curve.color" stop-opacity=".22"/><stop offset="100%" :stop-color="curve.color" stop-opacity=".015"/></linearGradient><clipPath v-for="(value,i) in shown" :key="i" :id="'bar-round-'+i"><rect :x="i*step+(step-barWidth)/2" :y="height-value/maximum*height" :width="barWidth" :height="Math.max(0,value/maximum*height)" rx="3"/></clipPath></defs><g transform="translate(44,7)"><g v-for="i in 5" :key="i"><line x1="0" :x2="width" :y1="(i-1)*height/4" :y2="(i-1)*height/4" class="grid-line"/><text x="-8" :y="(i-1)*height/4+(i===5?-1:3)" text-anchor="end" class="axis-text">{{compact(maximum*(5-i)/4,1)}}</text></g><g ref="plot" @pointermove="move" @pointerleave="leave"><rect data-testid="trend-hit" :width="width" :height="height" fill="transparent"/><g clip-path="url(#trend-viewport)"><g class="trend-pan-content" :transform="'translate('+panOffset+',0)'"><g v-if="kind!=='bar'" key="line" class="line-chart" clip-path="url(#trend-reveal)"><path v-if="data.mode!=='cache'" v-for="(curve,index) in curves" :key="'fill-'+curve.id" :d="area(curve.path)" :fill="'url(#trend-fill-'+index+')'" class="trend-area" :class="{dimmed:focused&&focused!==curve.id}"/><path v-for="curve in curves" :key="curve.id" :data-device="curve.id" :d="curve.path" class="trend-line" :class="{'cache-only':data.mode==='cache',dimmed:focused&&focused!==curve.id,focused:focused===curve.id}" :stroke="curve.color"/></g><g v-else :key="'bar'+liveRevision" class="bar-chart"><g v-for="bar in bars" :key="bar.id" :data-device="bar.id"><rect v-for="(bucket,i) in bar.buckets" :key="i" class="chart-bar" :class="{lit:hover===i,dimmed:focused&&focused!==bar.id}" :data-device="bar.id" :x="i*step+(step-barWidth)/2" :y="bucket.y" :width="barWidth" :height="Math.max(0,bucket.value/maximum*height)" :fill="bar.color" :clip-path="'url(#bar-round-'+i+')'"/></g><rect v-if="point" class="hover-band" :x="point.x-step*.48" y="0" :width="step*.96" :height="height" rx="4"/></g><g v-if="data.mode==='combined'&&kind!=='bar'" clip-path="url(#trend-reveal)" :transform="kind==='bar'?'translate('+step/2+',0)':''"><path v-for="curve in cacheCurves" :key="curve.id" :d="curve.path" class="trend-cache-line" :stroke="curve.color"/></g></g></g><g v-if="point" class="chart-hover" :style="{transform:'translate('+point.x+'px,'+point.y+'px)'}"><line y1="0" :y2="height-point.y" class="hover-line"/><g v-if="kind!=='bar'"><g v-for="marker in markers" :key="marker.id" :transform="'translate(0,'+(marker.y-point.y)+')'"><circle r="7" :fill="marker.color" fill-opacity=".18"/><circle r="3" :fill="marker.color" stroke="#e7efff" stroke-width="1"/></g></g><g v-else><circle r="7" :fill="singleColor" fill-opacity=".18"/><circle r="3" :fill="singleColor" stroke="#e7efff" stroke-width="1"/></g></g></g></g><text v-for="day in dailyLabels" :key="day.x" :x="day.x" y="137" text-anchor="middle" class="axis-text day-label">{{day.label}}</text><text v-if="!dailyLabels.length" x="44" y="137" class="axis-text">{{dates[0]}}</text><text v-if="!dailyLabels.length" x="236" y="137" text-anchor="middle" class="axis-text">{{dates[1]}}</text><text v-if="!dailyLabels.length" x="428" y="137" text-anchor="end" class="axis-text">{{dates[2]}}</text></svg><div v-if="pannable" class="hour-slider"><input type="range" :min="sliderMin" :max="sliderMax" :step="sliderStep" :disabled="sliderMin>=sliderMax" :value="Math.max(sliderMin,Math.min(sliderMax,sliderEnd??viewEnd))" @input="seek" :aria-label="sliderLabel" :title="timeLabel(viewEnd-span)+' — '+timeLabel(viewEnd)"></div><div class="trend-legend"><span v-for="item in series" :key="item.id" class="trend-legend-item" @pointerenter="focused=item.id" @pointerleave="focused=''" :data-device="item.id" :title="item.label+' · '+compact(data.mode==='cache'?data.totals?.[item.id]:(data.totals?.[item.id]||0))+' Token'"><i class="trend-legend-dot" :style="{background:item.color}"></i><span class="trend-legend-name">{{item.label}}</span><span class="trend-legend-value">{{compact(data.mode==='cache'?data.totals?.[item.id]:(data.totals?.[item.id]||0))}}</span><span class="trend-legend-share" title="按官方已确认额度增量和时段用量权重分摊；跟随额度显示模式" :style="{color:item.color}">{{share(item.id)}}</span></span></div><Teleport to="body"><Transition name="tip"><div v-if="hover>=0" class="chart-tooltip" :style="tooltipStyle"><header class="tooltip-heading"><time>{{timeLabel(data.start+hover*data.step)}}</time><span>{{data.mode==='cache'?'缓存用量':'用量明细'}}</span></header><div class="tooltip-summary"><span>{{data.mode==='cache'?'缓存合计':'总用量'}}</span><strong>{{compact(data.points[hover])}} <small>Token</small></strong><b class="tooltip-total-percent" :style="series.length===1?{color:series[0].color}:{}">{{quotaPercent(data.quotaPoints[hover],data.quotaPendingPoints[hover],data.quotaEstimatedPoints[hover],data.cacheMissingPoints[hover])}}</b></div><div v-if="series.length>1" class="tooltip-series"><div class="tooltip-columns"><span>用户 / 明细</span><span>Token</span><span>额度分摊</span></div><div v-for="item in series" :key="item.id" class="tooltip-series-row" :data-device="item.id"><span class="tooltip-series-name"><i class="trend-legend-dot" :style="{background:item.color}"></i>{{item.label}}</span><span class="tooltip-series-value">{{compact(item.points[hover])}}</span><b class="tooltip-user-percent" :style="{color:item.color}"><span class="tooltip-user-quota">{{quotaPercent(item.quotaPoints[hover],item.quotaPendingPoints[hover],item.quotaEstimatedPoints?.[hover],item.cacheMissingPoints[hover])}}</span></b><template v-if="data.mode!=='cache'"><span class="tooltip-cache-label">其中缓存</span><span class="tooltip-cache-value">{{item.cachePoints[hover]===null?'缺少明细':compact(item.cachePoints[hover])}}</span><span class="tooltip-cache-percent" :style="{color:item.color}">{{cachePercent(item.cacheQuotaPoints[hover],item.cacheMissingPoints[hover],item.quotaPendingPoints[hover],item.quotaEstimatedPoints?.[hover])}}</span></template></div></div><div v-else-if="data.mode!=='cache'" class="tooltip-cache-single"><span>其中缓存</span><span>{{cacheTotalAt(hover)===null?'缺少明细':compact(cacheTotalAt(hover))+' Token'}}</span><span :style="{color:series[0]?.color}">{{cachePercent(data.cacheQuotaPoints[hover],data.cacheMissingPoints[hover],data.quotaPendingPoints[hover],data.quotaEstimatedPoints[hover])}}</span></div><footer class="tooltip-note"><span v-if="data.quotaUnavailable">{{data.quotaUnavailable}}</span><span v-else-if="data.quotaEstimatedPoints[hover]" class="quota-estimate-note">≈ 额度分摊</span><span v-else>仅计官方已确认额度</span><span v-if="data.mode!=='cache'">缓存已计入总用量</span></footer></div></Transition></Teleport></div>`
};

export const DonutChart={
  components:{RollingValue},
  props:{devices:{required:true},totals:{required:true},quotaTotals:{default:()=>({})},quotaStates:{default:()=>({})},quotaReady:Boolean,quotaUnavailable:{default:''},cacheTokens:{default:()=>({})},cacheTotals:{default:()=>({})},cacheMissing:{default:()=>({})}},
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
    function share(id){return props.quotaUnavailable||chartQuotaPercent(props.quotaTotals[id],props.quotaReady,props.quotaStates[id]==='pending',props.quotaStates[id]==='estimated',1);}
    function cacheShare(id){return props.quotaUnavailable||chartQuotaPercent(props.cacheTotals[id],props.quotaReady&&!props.cacheMissing[id],props.quotaStates[id]==='pending',props.quotaStates[id]==='estimated');}return {cacheShare,share,hover,arcs,compact};
  },
  template:`<div class="donut-content"><svg class="donut-svg" viewBox="0 0 140 140" aria-label="各设备 Token 用量占比"><circle cx="70" cy="70" r="50" fill="none" stroke="#282c34" stroke-width="21"/><g v-for="arc in arcs" :key="arc.id" class="donut-piece" :style="{transform:hover===arc.id?'translate('+arc.dx+'px,'+arc.dy+'px)':'translate(0,0)'}"><circle v-if="arc.length>0" cx="70" cy="70" r="50" fill="none" :stroke="arc.color" stroke-width="21" :stroke-dasharray="arc.length+' '+(314.159-arc.length)" :stroke-dashoffset="arc.offset" transform="rotate(-90 70 70)" tabindex="0" :aria-label="arc.label+' '+compact(totals[arc.id]||0)+' Token'" @pointerenter="hover=arc.id" @pointerleave="hover=''" @focus="hover=arc.id" @blur="hover=''"/></g></svg><div class="donut-legend"><div v-for="device in devices" :key="device.id" class="legend-row" @pointerenter="hover=device.id" @pointerleave="hover=''"><span class="legend-dot" :style="{background:device.color}"></span><span class="legend-name" :title="device.label">{{device.label}}</span><em class="donut-share" title="按官方已确认额度增量和时段用量权重分摊；跟随额度显示模式" :style="{color:device.color}"><span class="donut-main-share"><RollingValue :value="share(device.id)"/></span><small class="cache-quota" title="缓存消耗已包含在总额度消耗内">缓存 <RollingValue :value="cacheShare(device.id)"/></small></em><span class="legend-value"><RollingValue :value="compact(totals[device.id]||0)"/><small class="cache-quota">缓存 <RollingValue :value="compact(cacheTokens[device.id])"/></small></span></div><span v-if="!devices.length" class="muted">暂无设备数据</span></div></div>`
};
