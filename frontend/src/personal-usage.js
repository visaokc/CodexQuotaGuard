import {computed,ref} from 'vue';
import {compact,niceScale} from './data.js';

export const PersonalUsageChart={
  props:{rows:Array,name:String,color:String,kind:String},
  setup(props){
    const selected=ref('');
    const days=computed(()=>[...(props.rows||[])].sort((a,b)=>a.date.localeCompare(b.date)));
    const maximum=computed(()=>niceScale(Math.max(1,...days.value.map(r=>r.percent))));
    const width=384,height=110;
    const step=computed(()=>width/Math.max(1,days.value.length));
    const points=computed(()=>days.value.map((r,i)=>({ ...r,x:(i+.5)*step.value,y:height-r.percent/maximum.value*height })));
    const path=computed(()=>points.value.map((p,i)=>(i?'L':'M')+p.x+','+p.y).join(' '));
    const detail=computed(()=>days.value.find(r=>r.date===selected.value));
    return {days,maximum,width,height,step,points,path,detail,selected,compact};
  },
  template:`<div class="personal-usage-chart" data-testid="personal-usage-chart" :style="{'--chart-color':color}"><span class="axis-unit">个人额度 %</span><svg viewBox="0 0 430 145" preserveAspectRatio="none" aria-label="每天个人额度消耗"><g transform="translate(44,10)"><g v-for="i in 5" :key="i"><line x1="0" :x2="width" :y1="(i-1)*height/4" :y2="(i-1)*height/4" class="grid-line"/><text x="-8" :y="(i-1)*height/4+3" text-anchor="end" class="axis-text">{{(maximum*(5-i)/4).toFixed(1)}}%</text></g><path class="personal-quota-line" v-if="kind!=='bar'" :d="path" fill="none" :stroke="color" stroke-width="2"/><g v-for="(p,i) in points" :key="p.date" tabindex="0" role="img" :aria-label="p.date+' '+p.percent.toFixed(1)+'%'" @pointerenter="selected=p.date" @pointerleave="selected=''" @focus="selected=p.date" @blur="selected=''"><rect :x="i*step" y="0" :width="step" :height="height" fill="transparent"/><rect class="personal-quota-bar" v-if="kind==='bar'" :x="i*step+step*.2" :y="p.y" :width="step*.6" :height="height-p.y" :fill="color" rx="2"/><circle v-else :cx="p.x" :cy="p.y" r="3" :fill="color"/><text v-if="i===0||i===points.length-1||(points.length>6&&i===Math.floor(points.length/2))" :x="p.x" y="130" text-anchor="middle" class="axis-text">{{p.date.slice(5)}}</text></g></g></svg><div v-if="detail" class="personal-usage-detail"><b>{{detail.date}} · {{detail.percent.toFixed(1)}}%</b><span>{{compact(detail.tokens)}} Token</span><small>{{detail.pending?'部分用量待确认':detail.estimated_quota?'包含预估，待官方校准':'官方已确认'}}</small></div><span v-if="!days.length" class="personal-usage-empty">暂无已记录的个人用量</span></div>`
};
