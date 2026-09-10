import test from 'node:test';
import assert from 'node:assert/strict';
import {aggregate,devicesFor,quotaValue,niceScale,linePath,curveGeometry,curveY,COLORS,modelOptions,officialUsageColor,officialQuotaRemaining,refreshRemaining} from '../src/data.js';
import {fixture} from './fixture.mjs';
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
});
test('saved device order is local to its account, colors remain stable and new devices append',()=>{
  const data=fixture(),before=Object.fromEntries(devicesFor(data).map(d=>[d.id,d.color]));
  data.settings.device_order={'fixture-account':['fixture-peer','fixture-local']};
  assert.deepEqual(devicesFor(data).map(d=>d.id),['fixture-peer','fixture-local']);
  assert.deepEqual(Object.fromEntries(devicesFor(data).map(d=>[d.id,d.color])),before);
  data.view.summary.devices.push({id:'z-new-device',name:'New device',tokens:0});
  assert.deepEqual(devicesFor(data).map(d=>d.id),['fixture-peer','fixture-local','z-new-device']);
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
