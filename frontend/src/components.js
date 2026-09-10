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
  template:`<div class="select"><button ref="anchor" type="button" class="select-trigger" :class="{expanded:open}" @click="toggle" :aria-label="label||current" aria-haspopup="listbox" :aria-expanded="open"><span>{{current}}</span><svg viewBox="0 0 12 12" width="12" height="12"><path d="m3 4.5 3 3 3-3"/></svg></button><Teleport to="body"><Transition name="menu"><div v-if="open" ref="menu" class="select-menu" :style="position" role="listbox" tabindex="-1" @keydown="key"><button v-for="(item,index) in items" :key="item.value" class="select-option" :class="{chosen:item.value===modelValue,focused:index===active}" @pointerenter="active=index" @click="choose(item)" role="option" :aria-selected="item.value===modelValue"><span>{{item.label}}</span><span v-if="item.value===modelValue" class="check">✓</span></button></div></Transition></Teleport></div>`
};

export const TrendChart={
  props:{data:{required:true},kind:String,color:{default:'#669cff'}},
  setup(props){
    const shown=ref([]),hover=ref(-1),mouseX=ref(0),plot=ref(null);
    const bounds=ref({left:0,top:0,width:430,svgTop:0,svgHeight:140});
    let frame=0,lastTarget=[];
    const width=384,height=112;
    watch(()=>props.data.points,values=>{
      if(values.length===lastTarget.length&&values.every((v,i)=>v===lastTarget[i]))return;
      lastTarget=values.slice();
      cancelAnimationFrame(frame);
      const target=values.slice(),before=shown.value.length===target.length?shown.value.slice():target.map(()=>0),start=performance.now();
      const tick=at=>{const t=Math.min(1,(at-start)/300),ease=1-(1-t)**3;shown.value=target.map((v,i)=>before[i]+(v-before[i])*ease);if(t<1)frame=requestAnimationFrame(tick);};
      frame=requestAnimationFrame(tick);
    },{immediate:true});
    onBeforeUnmount(()=>cancelAnimationFrame(frame));
    const maximum=computed(()=>niceScale(Math.max(0,...props.data.points)));
    const path=computed(()=>linePath(shown.value,width,height,maximum.value));
    const fill=computed(()=>`${path.value}L${shown.value.length>1?width:width/2},${height}L${shown.value.length>1?0:width/2},${height}Z`);
    const dates=computed(()=>[0,0.5,1].map(x=>timeLabel(props.data.start+x*props.data.step*props.data.points.length)));
    const step=computed(()=>width/Math.max(1,shown.value.length));
    const curve=computed(()=>curveGeometry(shown.value,width,height,maximum.value));
    const point=computed(()=>hover.value<0?null:props.kind==='bar'?{x:(hover.value+.5)*step.value,y:height-(props.data.points[hover.value]||0)/maximum.value*height}:{x:mouseX.value,y:curveY(curve.value,mouseX.value)});
    const tooltipStyle=computed(()=>{
      if(!point.value)return {};
      const box=bounds.value,tipWidth=122,tipHeight=43;
      const center=box.width*Math.max(.14,Math.min(.72,(point.value.x/width*.85)+.09));
      const left=Math.max(0,(center-tipWidth/2-7)/box.width*430-44),right=Math.min(width,(center+tipWidth/2+7)/box.width*430-44);
      const ys=[];
      if(props.kind==='bar'){
        for(let i=Math.max(0,Math.floor(left/step.value));i<=Math.min(shown.value.length-1,Math.floor(right/step.value));i++)ys.push(height-shown.value[i]/maximum.value*height);
        ys.push(height);
      }else for(let x=left;x<=right;x+=2)ys.push(curveY(curve.value,x));
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
    return {shown,hover,plot,width,height,maximum,path,fill,dates,step,point,tooltipStyle,move,compact,timeLabel};
  },
  template:`<div class="trend-plot" :style="{'--chart-color':color}"><span class="axis-unit">Token</span><svg class="trend-svg" viewBox="0 0 430 140" preserveAspectRatio="none" aria-label="Token 用量趋势"><defs><linearGradient id="trend-fill" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" :stop-color="color" stop-opacity=".25"/><stop offset="100%" :stop-color="color" stop-opacity=".015"/></linearGradient></defs><g transform="translate(44,7)"><g v-for="i in 5" :key="i"><line x1="0" :x2="width" :y1="(i-1)*height/4" :y2="(i-1)*height/4" class="grid-line"/><text x="-8" :y="(i-1)*height/4+(i===5?-1:3)" text-anchor="end" class="axis-text">{{compact(maximum*(5-i)/4,1)}}</text></g><g ref="plot" @pointermove="move" @pointerleave="hover=-1"><rect data-testid="trend-hit" :width="width" :height="height" fill="transparent"/><g v-if="kind!=='bar'"><path :d="fill" fill="url(#trend-fill)"/><path :d="path" class="trend-line" :stroke="color"/></g><g v-else><rect v-for="(value,i) in shown" :key="i" class="chart-bar" :class="{lit:hover===i}" :x="i*step+step*.18" :y="height-value/maximum*height" :width="Math.max(2,step*.64)" :height="Math.max(0,value/maximum*height)" :fill="color" rx="2.5"/><rect v-if="point" class="hover-band" :x="point.x-step*.48" y="0" :width="step*.96" :height="height" rx="4"/></g><g v-if="point" class="chart-hover" :style="{transform:'translate('+point.x+'px,'+point.y+'px)'}"><line y1="0" :y2="height-point.y" class="hover-line"/><circle r="7" :fill="color" fill-opacity=".18"/><circle r="3" :fill="color" stroke="#e7efff" stroke-width="1"/></g></g></g><text x="44" y="137" class="axis-text">{{dates[0]}}</text><text x="236" y="137" text-anchor="middle" class="axis-text">{{dates[1]}}</text><text x="428" y="137" text-anchor="end" class="axis-text">{{dates[2]}}</text></svg><Teleport to="body"><Transition name="tip"><div v-if="hover>=0" class="chart-tooltip" :style="tooltipStyle"><strong>{{compact(data.points[hover])}} Token</strong><span>{{timeLabel(data.start+hover*data.step)}}</span></div></Transition></Teleport></div>`
};

export const DonutChart={
  props:{devices:{required:true},totals:{required:true}},
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
      const part={...d,length:fraction*314.159,offset:-offset*314.159,dx:Math.cos(angle)*3,dy:Math.sin(angle)*3};offset+=fraction;return part;});});
    const selected=computed(()=>props.devices.find(d=>d.id===hover.value));
    return {hover,arcs,selected,compact};
  },
  template:`<div class="donut-content"><svg class="donut-svg" viewBox="0 0 140 140" aria-label="各设备 Token 用量占比"><circle cx="70" cy="70" r="50" fill="none" stroke="#282c34" stroke-width="21"/><g v-for="arc in arcs" :key="arc.id" class="donut-piece" :style="{transform:hover===arc.id?'translate('+arc.dx+'px,'+arc.dy+'px)':'translate(0,0)'}"><circle v-if="arc.length>0" cx="70" cy="70" r="50" fill="none" :stroke="arc.color" stroke-width="21" :stroke-dasharray="arc.length+' '+(314.159-arc.length)" :stroke-dashoffset="arc.offset" transform="rotate(-90 70 70)" tabindex="0" :aria-label="arc.label+' '+compact(totals[arc.id]||0)+' Token'" @pointerenter="hover=arc.id" @pointerleave="hover=''" @focus="hover=arc.id" @blur="hover=''"/></g></svg><div class="donut-legend"><div v-for="device in devices" :key="device.id" class="legend-row" @pointerenter="hover=device.id" @pointerleave="hover=''"><span class="legend-dot" :style="{background:device.color}"></span><span class="legend-name" :title="device.label">{{device.label}}</span><span class="legend-value">{{compact(totals[device.id]||0)}}</span></div><span v-if="!devices.length" class="muted">暂无设备数据</span></div><Transition name="tip"><div v-if="selected" class="donut-tooltip"><span :style="{color:selected.color}">{{compact(totals[selected.id]||0)}}</span> Token</div></Transition></div>`
};
