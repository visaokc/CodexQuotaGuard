import test from 'node:test';
import assert from 'node:assert/strict';
import {displayModelName,cycleModels,cycleUnknown,historyMinimum,historyWindow,userBreakdown,aggregate,devicesFor,quotaValue,niceScale,linePath,curveGeometry,curveY,COLORS,modelOptions,officialUsageColor,officialQuotaRemaining,refreshRemaining,dailyQuotaUsage,chartQuotaPercent,trendForMode} from '../src/data.js';
import {fixture} from './fixture.mjs';
import {sharedFixture} from './shared-fixture.mjs';
import {billingFixture} from './billing-fixture.mjs';

test('shared preparation uses the group display scope and never bills old per-account caps',()=>{
  const data=sharedFixture(),group=data.view.display_account;
  data.settings.quota_display='personal';
  data.settings.device_notes={'fixture-account':{'fixture-peer':'保留旧备注'}};
  data.settings.device_colors={'fixture-account':{'fixture-peer':COLORS[4]}};
  assert.equal(devicesFor(data).find(item=>item.id==='fixture-peer').label,'保留旧备注');
  assert.equal(devicesFor(data).find(item=>item.id==='fixture-peer').color,COLORS[4]);
  data.settings.device_notes[group]={'fixture-peer':'共享组备注'};
  assert.equal(devicesFor(data).find(item=>item.id==='fixture-peer').label,'共享组备注');
  data.view.analytics.windows.hour_curve.quota_ready=true;
  data.view.analytics.windows.hour_curve.quota_rows=[{device:'fixture-third',model:'gpt-5.5',bucket:0,quota:100}];
  const result=aggregate(data,'hour_curve');
  assert.equal(result.quotaUnavailable,'计费待启用');assert.equal(result.quotaReady,false);
  assert.deepEqual(result.quotaTotals,{});assert.equal(result.quotaPoints.reduce((sum,item)=>sum+item,0),0);
  assert.equal(result.series.length,3);assert.equal(result.total,data.view.analytics.windows.hour_curve.rows.reduce((sum,row)=>sum+row.tokens,0));
  assert.ok(modelOptions(data).length>1);
  assert.equal(userBreakdown(data,'fixture-third').quota,'计费待启用');
  assert.equal(userBreakdown(data,'fixture-third').cacheQuota,'计费待启用');
  data.view.identity={account:'fixture-account-b',label:'另一个登录账号'};
  assert.equal(aggregate(data,'hour_curve').total,result.total,'switching Codex identity keeps shared chart data');
});
test('current-cycle comparison exactly matches device rows, independently of historical cache',()=>{
  const data=fixture(),result=aggregate(data,'cycle');
  assert.equal(result.totals['fixture-local'],81270000);
  assert.equal(result.totals['fixture-peer'],148360000);
  assert.equal(result.total,229630000);
  assert.ok(!('removed-peer' in result.totals));
  assert.notEqual(aggregate(data,'total').total,result.total);
});
test('raw Token totals are account, model, device and removal scoped in each rolling window',()=>{
  const data=fixture();
  for(const window of ['hour','day','week','month','total'])for(const model of ['','gpt-5.5'])for(const device of ['','fixture-local']){
    const rows=data.view.analytics.windows[window].rows.filter(r=>r.device!=='removed-peer'&&(!model||r.model===model)&&(!device||r.device===device));
    const expected=rows.reduce((n,r)=>n+r.tokens,0),result=aggregate(data,window,model,device);
    assert.equal(result.total,expected);
    assert.equal(result.points.reduce((a,b)=>a+b,0),expected);
  }
  data.view.analytics.account='another-account';
  assert.equal(aggregate(data,'day').total,0);
});

test('all-user chart series preserve each bucket, device color and stacked total',()=>{
  const data=fixture();
  data.settings.device_notes={'fixture-account':{'fixture-local':'Local','fixture-peer':'Peer'}};
  data.settings.device_colors={'fixture-account':{'fixture-local':COLORS[1],'fixture-peer':COLORS[0]}};
  data.settings.device_order={'fixture-account':['fixture-peer','fixture-local']};
  data.view.analytics.windows.day={start:100,step:3600,count:3,rows:[
    {device:'fixture-local',model:'gpt-6-astra',bucket:0,tokens:120},
    {device:'fixture-local',model:'gpt-5.5',bucket:0,tokens:30},
    {device:'fixture-peer',model:'gpt-6-astra',bucket:0,tokens:50},
    {device:'fixture-local',model:'gpt-6-astra',bucket:2,tokens:70},
    {device:'fixture-peer',model:'gpt-6-astra',bucket:1,tokens:200},
    {device:'removed-peer',model:'gpt-6-astra',bucket:0,tokens:9000},
  ]};
  const result=aggregate(data,'day');
  assert.deepEqual(result.points,[200,200,70]);
  assert.equal(result.total,470);
  const basic=items=>items.map(({cachePoints,cacheQuotaPoints,cacheMissingPoints,...item})=>item);
  assert.deepEqual(basic(result.series),[
    {id:'fixture-local',label:'Local',color:COLORS[1],points:[150,0,70],quotaPoints:[0,0,0],quotaPendingPoints:[false,false,false]},
    {id:'fixture-peer',label:'Peer',color:COLORS[0],points:[50,200,0],quotaPoints:[0,0,0],quotaPendingPoints:[false,false,false]},
  ]);
  for(let i=0;i<result.points.length;i++)assert.equal(result.series.reduce((sum,item)=>sum+item.points[i],0),result.points[i]);
  for(const item of result.series)assert.equal(item.points.reduce((sum,value)=>sum+value,0),result.totals[item.id]);
  const selected=aggregate(data,'day','gpt-6-astra','fixture-local');
  assert.deepEqual(basic(selected.series),[{id:'fixture-local',label:'Local',color:COLORS[1],points:[120,0,70],quotaPoints:[0,0,0],quotaPendingPoints:[false,false,false]}]);
  assert.deepEqual(selected.points,[120,0,70]);
  assert.equal(selected.total,190);
  data.view.analytics.account='another-account';
  assert.ok(aggregate(data,'day').series.every(item=>item.points.every(value=>value===0)));
});

test('series match authoritative cycle totals and every seven-day bucket',()=>{
  const data=fixture(),cycle=aggregate(data,'cycle');
  assert.equal(cycle.series.find(item=>item.id==='fixture-local').points[0],81270000);
  assert.equal(cycle.series.find(item=>item.id==='fixture-peer').points[0],148360000);
  assert.equal(cycle.series.reduce((sum,item)=>sum+item.points.reduce((a,b)=>a+b,0),0),229630000);
  const week=aggregate(data,'week');
  assert.equal(week.points.length,7);
  assert.equal(week.step,86400);
  assert.ok(week.series.every(item=>item.points.length===7));
  for(let i=0;i<7;i++)assert.equal(week.series.reduce((sum,item)=>sum+item.points[i],0),week.points[i]);
});
test('weighted personal estimate changes denominator without modifying raw chart usage',()=>{
  const data=fixture(),row=devicesFor(data)[0];
  assert.equal(quotaValue(row,false),18.76);
  assert.equal(quotaValue(row,true),37.52);
  assert.equal(quotaValue({...row,cap:0},true),null);
  data.settings.device_colors={'fixture-account':{'fixture-local':COLORS[4]}};
  assert.equal(devicesFor(data)[0].color,COLORS[4]);
  const prior=Object.fromEntries(devicesFor(data).map(d=>[d.id,d.color]));
  data.view.summary.devices.reverse();
  assert.deepEqual(Object.fromEntries(devicesFor(data).map(d=>[d.id,d.color])),prior);
});
test('smooth path has finite monotone control points and clear axis maxima',()=>{
  assert.equal(niceScale(700.3),800);
  assert.equal(niceScale(21e6),24e6);
  for(const values of [[0,0,0],[0,7e6,0,2e6],[12,1,0,4,8],[3]]){
    const path=linePath(values);
    assert.ok(!/NaN|Infinity/.test(path));
    for(const coordinate of path.matchAll(/-?\d+(?:\.\d+)?/g))assert.ok(Number.isFinite(Number(coordinate[0])));
  }
  const values=[0,6,1,3,0],geometry=curveGeometry(values,400,100,8);
  for(let x=0;x<400;x++){
    const index=Math.floor(x/100),a=geometry.p[index].y,b=geometry.p[index+1].y,y=curveY(geometry,x);
    assert.ok(y>=Math.min(a,b)-1e-8&&y<=Math.max(a,b)+1e-8,'hover follows the curve without overshooting');
  }
});
test('model options use numeric version order and hiding auto-review never removes its Token',()=>{
  const data=fixture(),before=aggregate(data,'day').total;
  data.view.analytics.models=['gpt-5.9','gpt-5.10','gpt-5.10.2','gpt-6','gpt-5.10.10','codex-auto-review'];
  data.view.analytics.windows.day.rows.push({device:'fixture-local',model:'codex-auto-review',bucket:0,tokens:123456,weight:0,unknown:0});
  assert.deepEqual(modelOptions(data).map(o=>o.value),['','gpt-6','gpt-5.10.10','gpt-5.10.2','gpt-5.10','gpt-5.9']);
  assert.equal(aggregate(data,'day').total,before+123456);
  data.view.analytics.models=['gpt-5.6-luna','gpt-5.6-terra','gpt-6-astra','gpt-5.6-sol'];
  assert.deepEqual(modelOptions(data).map(o=>o.value),['','gpt-6-astra','gpt-5.6-sol','gpt-5.6-terra','gpt-5.6-luna']);
});
test('local member stays first while saved peer order, colors and new devices remain stable',()=>{
  const data=fixture(),before=Object.fromEntries(devicesFor(data).map(d=>[d.id,d.color]));
  data.settings.device_order={'fixture-account':['fixture-peer','fixture-local']};
  assert.deepEqual(devicesFor(data).map(d=>d.id),['fixture-local','fixture-peer']);
  assert.deepEqual(Object.fromEntries(devicesFor(data).map(d=>[d.id,d.color])),before);
  data.view.summary.devices.push({id:'z-new-device',name:'New device',tokens:0});
  assert.deepEqual(devicesFor(data).map(d=>d.id),['fixture-local','fixture-peer','z-new-device']);
  data.settings.device_order={'another-account':['fixture-peer','fixture-local']};
  assert.deepEqual(devicesFor(data).map(d=>d.id),['fixture-local','fixture-peer','z-new-device']);
});
test('official usage alerts use remaining quota and never treat missing values as exhausted',()=>{
  assert.equal(officialQuotaRemaining(91),9);
  assert.equal(officialQuotaRemaining(-1),100);
  assert.equal(officialQuotaRemaining(101),0);
  assert.equal(officialQuotaRemaining(null),null);
  assert.equal(officialUsageColor(91),'#ed8d98');
  assert.equal(officialUsageColor(100),'#ed8d98');
  assert.equal(officialUsageColor(90),'#e8bf75');
  assert.equal(officialUsageColor(80.01),'#e8bf75');
  assert.equal(officialUsageColor(80),null);
  assert.equal(officialUsageColor(0),null);
  for(const value of [null,undefined,NaN,Infinity,'91'])assert.equal(officialUsageColor(value),null);
});
test('official refresh switches between days hours and minutes and distinguishes expired or absent timestamps',()=>{
  assert.equal(refreshRemaining(1e6+115.6*3600,1e6),'4.8 天');
  assert.equal(refreshRemaining(1e6+7200,1e6+3600),'1.0 小时');
  assert.equal(refreshRemaining(1e6+48*3600,1e6),'48.0 小时');
  assert.equal(refreshRemaining(1e6+49*3600,1e6),'2.0 天');
  assert.equal(refreshRemaining(1e6+3599,1e6),'60 分钟');
  assert.equal(refreshRemaining(1e6+61,1e6),'2 分钟');
  assert.equal(refreshRemaining(1e6,1e6),'等待刷新');
  assert.equal(refreshRemaining(1e6,1e6+1),'等待刷新');
  for(const value of [undefined,null,NaN,Infinity])assert.equal(refreshRemaining(value,1e6),'—');
});


test('daily quota average uses observed cycle delta and exact elapsed days',()=>{
  assert.equal(dailyQuotaUsage({used:60,baseline:0,started:100,observed_at:100+2*86400}),30);
  assert.equal(dailyQuotaUsage({used:60,baseline:20,started:100,observed_at:100+2*86400}),20);
  assert.equal(dailyQuotaUsage({used:5,baseline:0,started:100,observed_at:100+43200}),10);
  assert.equal(dailyQuotaUsage({used:0,baseline:0,started:100,observed_at:100}),null);
  assert.equal(dailyQuotaUsage({used:60,baseline:0,started:100,observed_at:86500},true),null);
  assert.equal(dailyQuotaUsage({used:60}),null);
});


test('compensation display adds signed carry without changing ordinary use or tokens',()=>{
  const borrower={estimated:0,cap:50,fair_base_cap:50,carry:15,tokens:0};
  const lender={...borrower,carry:-15};
  assert.equal(quotaValue(borrower,'personal'),0);
  assert.equal(quotaValue(lender,'personal'),0);
  assert.equal(quotaValue(borrower,'fair'),30);
  assert.equal(quotaValue(lender,'fair'),-30);
  assert.equal(quotaValue({...borrower,estimated:10},'fair'),50);
  assert.equal(quotaValue({...lender,estimated:10},'fair'),-10);
  assert.equal(quotaValue({...lender,estimated:10},'account'),10);
  assert.equal(quotaValue({...lender,estimated:10}),20);
  assert.equal(borrower.tokens,0);
});


test('chart quota uses official allocation and personal cap, independent of token budget',()=>{
  const data=fixture();
  data.view.analytics.windows.today.quota_rows=[{device:'fixture-local',model:'gpt-5.5',bucket:0,quota:14},{device:'fixture-peer',model:'gpt-5.5',bucket:0,quota:2}];
  assert.deepEqual(aggregate(data,'today').quotaTotals,{'fixture-local':14,'fixture-peer':2});
  data.settings.quota_display='personal';
  assert.deepEqual(aggregate(data,'today').quotaTotals,{'fixture-local':28,'fixture-peer':4});
  data.view.analytics.cycles[0].total_tokens=1;
  assert.equal(aggregate(data,'today','gpt-5.5','fixture-local').quotaPoints[0],28);
});

test('only official quota is displayed and pending logs retain the last calibrated percentage',()=>{
  const data=fixture(),window=data.view.analytics.windows.today;
  window.quota_rows=[{device:'fixture-local',model:'gpt-5.5',bucket:0,quota:1}];
  window.quota_estimate_rows=[{device:'fixture-local',model:'gpt-5.5',bucket:0,quota:.25}];
  window.quota_pending_rows=[{device:'fixture-peer',model:'gpt-5.5',bucket:0}];
  data.settings.quota_display='personal';
  const result=aggregate(data,'today');
  assert.equal(result.quotaTotals['fixture-local'],2);
  assert.equal(result.quotaStates['fixture-local'],undefined);
  assert.equal(result.quotaStates['fixture-peer'],'pending');
  assert.equal(chartQuotaPercent(result.quotaPoints[0],true,result.quotaPendingPoints[0],result.quotaEstimatedPoints[0]),'2.00%');
  const selected=aggregate(data,'today','','fixture-local');
  assert.equal(chartQuotaPercent(selected.quotaPoints[0],true,selected.quotaPendingPoints[0],selected.quotaEstimatedPoints[0]),'2.00%');
  assert.equal(chartQuotaPercent(0,true,true,false),'待更新');
  assert.equal(chartQuotaPercent(2.5,false,false,true),'—');
  window.quota_estimate_rows=[];window.quota_pending_rows=[];
  window.quota_rows[0].quota=1.1;
  const confirmed=aggregate(data,'today');
  assert.equal(confirmed.quotaTotals['fixture-local'],2.2);
  assert.deepEqual(confirmed.quotaStates,{});
  assert.equal(chartQuotaPercent(confirmed.quotaPoints[0],true,false,false),'2.20%');
});

test('cache modes preserve missing intervals and apply quota conversion once',()=>{
  const data=fixture(),window=data.view.analytics.windows.day;
  data.settings.quota_display='personal';
  window.rows=[{device:'fixture-local',model:'gpt-5.5',bucket:0,tokens:100,cache_tokens:80,detail_missing:0},
    {device:'fixture-local',model:'gpt-5.5',bucket:1,tokens:100,cache_tokens:null,detail_missing:100}];
  window.quota_rows=[{device:'fixture-local',model:'gpt-5.5',bucket:0,quota:1,cache_quota:.1}];
  const original=aggregate(data,'day','','fixture-local'),cached=trendForMode(original,'cache');
  assert.equal(original.cacheQuotaTotals['fixture-local'],.2);
  assert.deepEqual(cached.points.slice(0,3),[80,null,0]);
  assert.equal(cached.total,null);
  assert.equal(cached.quotaTotals['fixture-local'],.2);
  assert.equal(trendForMode(original,'combined').points[0],100);
  assert.equal(original.points[1],100);
  assert.equal(linePath([null,null]),'');
  assert.equal((linePath([10,null,20]).match(/M/g)||[]).length,2);
  assert.ok(!linePath([10,null,20]).includes('C'));
});

test('user cycle breakdown distinguishes cache miss and reasoning without double counting',()=>{
  const data=fixture();data.view.analytics.windows.cycle.rows=[{device:'fixture-local',model:'gpt-6-astra',tokens:1100,input_tokens:1000,cache_tokens:800,output_tokens:100,reasoning_tokens:70,detail_missing:0,reasoning_missing:0,event_count:1,detail_count:1,first_at:100,last_at:100},
    {device:'fixture-local',model:'gpt-5.5',tokens:400,input_tokens:null,cache_tokens:null,output_tokens:null,detail_missing:400,event_count:1,detail_count:0,first_at:110,last_at:110}];
  const value=userBreakdown(data,'fixture-local');
  assert.equal(value.tokens,1500);assert.equal(value.miss_tokens,200);assert.equal(value.non_reasoning_tokens,30);
  assert.equal(value.input_tokens+value.output_tokens,1100);assert.equal(value.hit_rate,80);
  assert.equal(value.detail_missing,400);assert.equal(value.event_count,2);assert.equal(value.detail_count,1);
  assert.equal(value.models.find(m=>m.model==='gpt-5.5').hit_rate,null);assert.equal(value.models.find(m=>m.model==='gpt-5.5').coverage,0);
  assert.equal(value.models[0].usage_share,1100/1500*100);
  assert.equal(value.models.reduce((total,item)=>total+item.usage_share,0),100);
  assert.equal(chartQuotaPercent(110,true),'110.00%');
  assert.equal(userBreakdown(data,'fixture-peer').tokens,0);
  assert.deepEqual(userBreakdown(data,'fixture-peer').models.map(m=>[m.model,m.tokens,m.usage_share]),[['gpt-6-astra',0,0],['gpt-5.6-sol',0,0],['gpt-5.6-terra',0,0],['gpt-5.6-luna',0,0]]);
});

test('hour history buffer retains exact minute buckets as the viewport moves',()=>{
  const source={start:0,step:60,count:180,rows:[{bucket:65,tokens:100},{bucket:120,tokens:200}],quota_rows:[{bucket:65,quota:.2}],quota_pending_rows:[]};
  const first=historyWindow(source,7200),next=historyWindow(source,7260);
  assert.equal(first.count,60);assert.equal(first.start,3660);
  assert.deepEqual(first.rows,[{bucket:4,tokens:100},{bucket:59,tokens:200}]);
  assert.deepEqual(next.rows,[{bucket:3,tokens:100},{bucket:58,tokens:200}]);
  assert.deepEqual(source.rows,[{bucket:65,tokens:100},{bucket:120,tokens:200}]);
});

test('day history uses hourly buckets and stops at the selected recorded history',()=>{
  const data=fixture(),latest=Math.floor(data.view.analytics.at/3600)*3600;
  const source={start:latest-32*86400,step:3600,count:32*24,rows:[{device:'fixture-local',model:'gpt-6-astra',bucket:30*24,tokens:100},{device:'fixture-peer',model:'gpt-5.6-sol',bucket:0,tokens:200},{device:'removed-peer',model:'gpt-6-astra',bucket:0,tokens:900}]};
  assert.equal(historyMinimum(data,source,86400,30*86400,'','fixture-local'),latest-2*86400+23*3600);
  assert.equal(historyMinimum(data,source,86400,30*86400),latest-30*86400);
  assert.equal(historyMinimum(data,source,86400,30*86400,'gpt-5.6-luna'),latest);
  assert.equal(historyMinimum(data,{...source,rows:[]},86400,30*86400),latest);
  const window=historyWindow(source,latest-86400,86400);
  assert.equal(window.count,24);assert.equal(window.step,3600);
  assert.deepEqual(window.rows.filter(r=>r.device==='fixture-local'),[]);
  const earliest=historyWindow(source,historyMinimum(data,source,86400,30*86400,'','fixture-local'),86400);
  assert.equal(earliest.rows[0].tokens,100);assert.equal(earliest.rows[0].bucket,0);
  const calibrated={...source,quota_available:true,quota_ready:false,quota_gaps:[{start:source.start,end:source.start+3600}]};
  assert.equal(historyWindow(calibrated,latest,86400).quota_ready,true,'gaps outside the visible window do not hide its calibrated percentages');
  assert.equal(historyWindow(calibrated,source.start+23*3600,86400).quota_ready,false);
});


test('six and twelve hour histories use five minute steps and stop at matching recorded accounts',()=>{
  const data=sharedFixture(),at=data.view.analytics.at,step=300;
  for(const duration of [21600,43200]){
    const count=(86400+duration)/step,start=Math.floor(at/step)*step-(count-1)*step;
    const source={start,step,count,quota_available:true,quota_gaps:[],rows:[
      {account:'fixture-account',device:'fixture-local',model:'gpt-5.5',bucket:0,tokens:11},
      {account:'fixture-account-b',device:'fixture-peer',model:'gpt-5.5',bucket:100,tokens:29}],
      quota_rows:[{account:'fixture-account',device:'fixture-local',model:'gpt-5.5',bucket:0,quota:.1,cache_quota:.05}]};
    const earliest=historyMinimum(data,source,duration,86400,'','fixture-local','fixture-account');
    assert.equal(earliest,start+duration-step);
    const window=historyWindow(source,earliest,duration);
    assert.equal(window.step,300);assert.equal(window.count,duration/300);assert.equal(window.rows[0].tokens,11);
    assert.equal(window.quota_rows[0].quota,.1);assert.equal(window.quota_rows[0].cache_quota,.05);
    assert.equal(historyMinimum(data,source,duration,86400,'','fixture-local','fixture-account-b'),Math.floor(at/step)*step,'empty account selections cannot pan into unrecorded history');
  }
});


test('model names are formatted only for display while IDs and unknown names remain unchanged',()=>{
  const data=fixture(),ids=['gpt-6-astra','gpt-5.6-sol','gpt-5.6-terra','gpt-5.6-luna'];
  const labels=['GPT-6 Astra','GPT-5.6 Sol','GPT-5.6 Terra','GPT-5.6 Luna'];
  data.view.analytics.models=[...ids];
  assert.deepEqual(ids.map(displayModelName),labels);
  assert.deepEqual(modelOptions(data).map(row=>row.value),['',...ids]);
  assert.deepEqual(modelOptions(data).map(row=>row.label),['全部模型',...labels]);
  assert.equal(displayModelName('private-model-r1'),'private-model-r1');
});

test('hidden internal models keep their tokens and appear only in cycle unknown totals',()=>{
  const data=fixture(),device=data.settings.device_id,rows=data.view.analytics.windows.cycle.rows;
  rows.push({...rows.find(r=>r.device===device),model:'codex-auto-review',tokens:72633});
  const result=userBreakdown(data,device);
  assert.equal(result.tokens,rows.filter(r=>r.device===device).reduce((sum,r)=>sum+r.tokens,0));
  assert.ok(!result.models.some(r=>r.model==='codex-auto-review'));
  const cycle={models:[{model:'gpt-6-astra',tokens:1000000},{model:'codex-auto-review',tokens:72633},{model:'unknown',tokens:7}],sampled_tokens:1072640};
  assert.equal(cycleUnknown(cycle),72640);
  assert.equal(cycleModels(cycle).length,4);
  assert.ok(cycleModels(cycle)[0].usage_share<100);
});

test('personal consumption and shared card keep the fixed single-cycle basis without changing Token totals',()=>{
  const data=billingFixture(),person=data.view.summary.devices[2],window=data.view.analytics.windows.cycle;
  person.available_cap=100/3;person.available=30;
  window.rows=[{account:'fixture-account-b',device:person.id,model:'gpt-6-astra',bucket:0,tokens:1000,shared_tokens:600}];
  window.quota_rows=[{account:'fixture-account-b',device:person.id,model:'gpt-6-astra',bucket:0,quota:100/3-30,shared_quota:1,cache_quota:0}];
  const result=userBreakdown(data,person.id);
  assert.equal(result.quota,'10.00%');assert.equal(result.sharedQuota,'3.00%');
  assert.equal(result.shared_tokens,600);assert.equal(result.tokens,1000);
  person.available_cap=200/3;
  assert.equal(userBreakdown(data,person.id).quota,'10.00%');
});
