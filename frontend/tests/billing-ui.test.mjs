// Isolated headless browser: no user desktop input or live credentials.
import assert from 'node:assert/strict';
import http from 'node:http';
import {readFile,mkdir} from 'node:fs/promises';
import path from 'node:path';
import {createRequire} from 'node:module';
import {billingFixture} from './billing-fixture.mjs';
const require=createRequire(import.meta.url),{chromium}=require('playwright');
const root=path.resolve('dist'),artifacts=path.resolve('test-artifacts');await mkdir(artifacts,{recursive:true});
const server=http.createServer(async(req,res)=>{try{if(req.url==='/favicon.ico'){res.writeHead(204).end();return;}const file=path.join(root,new URL(req.url,'http://localhost').pathname.replace(/^\/$/,'/index.html'));if(!file.startsWith(root+path.sep))throw Error('scope');res.setHeader('Content-Type',file.endsWith('.js')?'application/javascript':file.endsWith('.css')?'text/css':file.endsWith('.jpg')?'image/jpeg':'text/html');res.end(await readFile(file));}catch{res.writeHead(404).end();}});
await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
const browser=await chromium.launch({channel:'msedge',headless:true});
try{
  const page=await browser.newPage({viewport:{width:750,height:680},deviceScaleFactor:1,colorScheme:'dark'}),errors=[];
  page.on('pageerror',error=>errors.push(error.message));
  page.on('console',message=>{if(message.type()==='error')errors.push(message.text());});
  await page.addInitScript(data=>{window.__fixture=data;window.__commands=[];window.__CQG_TEST_BRIDGE__={snapshot:async()=>{if(!window.__hasSnapshot){window.__hasSnapshot=true;await new Promise(resolve=>setTimeout(resolve,150));}return structuredClone(window.__fixture);},command:async(action,payload)=>{window.__commands.push({action,payload});if(action==='member_history'){await new Promise(r=>setTimeout(r,payload.cycle==='archive'?120:15));const data=structuredClone(window.__fixture.view.analytics);data.windows.cycle=structuredClone(window.__memberWindows?.[payload.cycle]||data.windows.cycle);return{ok:true,data};}if(action==='chart_history')return{ok:true,data:structuredClone(window.__history||window.__fixture.view.analytics)};return {ok:true,data:{}};},window_action:async()=>({ok:true})};},billingFixture());
  await page.goto(`http://127.0.0.1:${server.address().port}/?test=1`);
  await page.locator('.app-shell.ready').waitFor();await page.evaluate(()=>window.__CQG_TEST__.pausePolling());await page.waitForTimeout(400);
  assert.equal((await page.evaluate(()=>window.__CQG_TEST__.getState())).piePeriod,'today');
  assert.equal(await page.getByTestId('pool-remaining').innerText(),'62.0%');
  assert.equal(await page.locator('.pool-track').getAttribute('aria-valuemax'),'100');
  assert.deepEqual(await page.locator('.pool-track i').evaluateAll(nodes=>nodes.map(n=>n.style.width)),['23.5%','38.5%']);
  assert.equal(await page.locator('.account-reset-row').count(),2);
  assert.equal(await page.getByTestId('pool-refresh-card').getByRole('progressbar').count(),0);
  assert.ok(!(await page.getByTestId('pool-refresh-card').innerText()).includes('%'));
  assert.ok((await page.locator('.account-reset-row>strong').evaluateAll(nodes=>nodes.map(n=>parseFloat(getComputedStyle(n).fontSize)))).every(size=>size>=20));
  const refreshLayout=await page.locator('.account-reset-row').evaluateAll(nodes=>nodes.map(n=>{const r=n.getBoundingClientRect();return {x:r.x,y:r.y,width:r.width};}));
  assert.equal(refreshLayout[0].y,refreshLayout[1].y);assert.ok(Math.abs(refreshLayout[0].width-refreshLayout[1].width)<1);assert.ok(refreshLayout[1].x>refreshLayout[0].x);
  assert.equal(await page.locator('.pool-daily>b').innerText(),'8.50% / 天');
  await page.getByTestId('pool-quota-card').click();await page.locator('.daily-member-list').waitFor();
  assert.equal(await page.locator('.daily-member-row').count(),3);
  assert.equal((await page.locator('.daily-unassigned').innerText()).replace(/\s+/g,''),'尚未分配到成员0.50%/天');
  assert.ok((await page.locator('.daily-basis').innerText()).includes('分别除以'));
  await page.waitForTimeout(300);await page.screenshot({path:path.join(artifacts,'billing-daily-usage.png')});
  await page.getByRole('button',{name:'关闭对话框',exact:true}).click();
  assert.equal(await page.locator('.device-row .avatar img').count(),3);
  assert.ok((await page.locator('.device-row .avatar img').evaluateAll(images=>images.map(i=>i.complete&&i.naturalWidth>0))).every(Boolean));
  assert.equal(await page.locator('.device-usage').first().innerText(),'0.0%');
  assert.equal(await page.locator('.device-columns>span').last().innerText(),'可用额度');
  assert.equal(await page.locator('.model-badge.gold').innerText(),'GPT-6 Astra');
  assert.equal(await page.locator('.model-badge:not(.gold)').innerText(),'GPT-5.6 Sol');
  const badgeLayout=await page.getByTestId('device-row').evaluateAll(rows=>rows.map(row=>{
    const nodes=['.connection-badge','.model-badge, .model-placeholder','.member-activity','.device-tokens','.device-quota-cell'].map(s=>row.querySelector(s).getBoundingClientRect());
    return nodes.map(r=>({x:r.x+r.width/2,y:r.y+r.height/2,left:r.left,right:r.right}));
  }));
  for(const row of badgeLayout){const step=row[1].x-row[0].x;for(let i=1;i<row.length;i++){assert.ok(Math.abs(row[i].x-row[i-1].x-step)<1,'status, model, use, token and quota have equal center spacing');assert.ok(Math.abs(row[i].y-row[0].y)<1);assert.ok(row[i].left>row[i-1].right);}}
  assert.equal(new Set(badgeLayout.map(row=>row[0].x)).size,1);
  assert.equal(await page.locator('.member-activity.using').first().evaluate(n=>getComputedStyle(n).backgroundColor),'rgb(37, 65, 107)');
  await page.getByRole('button',{name:'筛选模型',exact:true}).click();
  for(const name of ['GPT-6 Astra','GPT-5.6 Sol','GPT-5.6 Terra','GPT-5.6 Luna'])assert.equal(await page.getByRole('option',{name,exact:true}).count(),1);
  await page.waitForTimeout(200);await page.screenshot({path:path.join(artifacts,'billing-model-names.png')});
  await page.getByRole('button',{name:'筛选模型',exact:true}).click();
  await page.evaluate(()=>window.__initialPerson=structuredClone(window.__fixture.view.summary.devices[0]));
  for(const [available,available_cap,expected] of [[100/3,100/3,'100.0%'],[100/3-2,100/3,'94.0%'],[100/3,200/3,'50.0%'],[35,100/3,'105.0%'],[0,0,'—']]){
    await page.evaluate(({available,available_cap})=>{Object.assign(window.__fixture.view.summary.devices[0],{available,available_cap});window.__CQG_TEST__.applySnapshot(structuredClone(window.__fixture));},{available,available_cap});
    assert.equal(await page.locator('.device-usage').first().innerText(),expected);
  }
  await page.evaluate(()=>{window.__fixture.view.summary.devices[0]=window.__initialPerson;window.__CQG_TEST__.applySnapshot(structuredClone(window.__fixture));});
  assert.equal(await page.getByTestId('connection-state').nth(1).innerText(),'在线');
  const activeRow=page.getByTestId('device-row').first();
  assert.equal(await activeRow.locator('.member-activity').innerText(),'Codex 使用中');
  for(const [active,model,state] of [[1,null,'识别模型中'],[0,'gpt-6-astra','暂无近期活动'],[1,'gpt-6-astra','Codex 使用中']]){
    await page.evaluate(({active,model})=>{Object.assign(window.__fixture.view.summary.devices[0],{active,active_model:model});window.__CQG_TEST__.applySnapshot(structuredClone(window.__fixture));},{active,model});
    assert.equal(await activeRow.locator('.member-activity').innerText(),state);
    assert.equal(await activeRow.locator('.model-badge').count(),state==='Codex 使用中'?1:0);
  }
  const runtimeOrder=await activeRow.evaluate(row=>['.connection-badge','.model-badge','.member-activity','.device-tokens','.device-quota-cell'].map(selector=>{const r=row.querySelector(selector).getBoundingClientRect();return [r.left,r.right,r.top+r.height/2];}));
  for(let i=1;i<runtimeOrder.length;i++){assert.ok(runtimeOrder[i][0]>runtimeOrder[i-1][1]);assert.ok(Math.abs(runtimeOrder[i][2]-runtimeOrder[0][2])<1);}

  for(const size of [{width:750,height:680},{width:730,height:650}]){
    await page.setViewportSize(size);await page.waitForTimeout(200);
    const box=await page.getByTestId('device-row').last().boundingBox();assert.ok(box.y+box.height<=size.height);
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
  }
  await page.setViewportSize({width:750,height:680});await page.waitForTimeout(200);
  await page.screenshot({path:path.join(artifacts,'billing-overview-dark.png')});
  await page.getByRole('button',{name:'筛选账号',exact:true}).click();await page.getByRole('option',{name:'账号2',exact:true}).click();
  const filtered=await page.evaluate(()=>window.__CQG_TEST__.getState());
  assert.equal(filtered.pie.quotaTotals.person1,undefined);assert.equal(filtered.pie.quotaTotals.person3,billingFixture().view.analytics.windows.today.quota_rows.filter(r=>r.device==='person3').reduce((n,r)=>n+r.quota*1.5,0));
  await page.getByRole('button',{name:'筛选账号',exact:true}).click();await page.getByRole('option',{name:'两账号合计',exact:true}).click();
  await page.getByTestId('device-row').first().click();await page.locator('.user-pool-summary').waitFor();
  assert.equal(await page.locator('.user-account-breakdown article').count(),0);assert.equal(await page.locator('.user-pool-summary>div').count(),3);assert.match(await page.getByTestId('shared-consumption-card').innerText(),/均摊消耗/);assert.ok(!(await page.getByTestId('shared-consumption-card').innerText()).includes('软件开发'));
  assert.deepEqual((await page.locator('.user-model-card header strong').allTextContents()).slice(0,4),['GPT-6 Astra','GPT-5.6 Sol','GPT-5.6 Terra','GPT-5.6 Luna']);
  await page.evaluate(()=>{const data=structuredClone(window.__fixture),rows=data.view.analytics.windows.cycle.rows;rows.push({...rows.find(r=>r.device==='person1'),model:'codex-auto-review',tokens:72633});window.__CQG_TEST__.applySnapshot(data);});
  assert.ok(!(await page.locator('.user-model-list').innerText()).includes('codex-auto-review'));
  await page.evaluate(()=>window.__CQG_TEST__.applySnapshot(structuredClone(window.__fixture)));
  await page.screenshot({path:path.join(artifacts,'billing-member.png')});
  await page.evaluate(()=>{
    const make=tokens=>({start:0,step:1,count:1,quota_ready:true,rows:[{device:'person1',account:'fixture-account',model:'gpt-6-astra',bucket:0,tokens,input_tokens:tokens-10,output_tokens:10,cache_tokens:tokens-20,event_count:1,detail_count:1,detail_missing:0}],quota_rows:[{device:'person1',account:'fixture-account',model:'gpt-6-astra',bucket:0,quota:5,cache_quota:1}]});
    window.__memberWindows={earlier:make(1234),second:make(5678),first:make(9999)};
    const cycle=window.__fixture.view.analytics.cycles[0];
    window.__fixture.view.analytics.cycles.push({...cycle,id:'earlier',started:cycle.started-604800,ended:cycle.started});
    window.__fixture.view.analytics.donut_archive_at=window.__fixture.view.analytics.at-100;
    window.__CQG_TEST__.applySnapshot(structuredClone(window.__fixture));
  });
  async function pickCycle(account,number){
    await page.getByRole('button',{name:'个人明细周期',exact:true}).click();
    assert.equal(await page.getByRole('option',{name:'当前配对周期',exact:true}).count(),0);
    await page.getByRole('option',{name:new RegExp('^'+account)}).click();
    await page.getByRole('option',{name:new RegExp('^第'+number+'周期')}).click();
    await page.locator('.user-cycle-primary').waitFor();
  }
  await pickCycle('账号1',1);
  assert.equal((await page.evaluate(()=>window.__CQG_TEST__.getState())).userCycle.tokens,1234);
  assert.equal(await page.locator('.user-pool-summary>div').count(),1,'historical view shows its shared cost without presenting a current balance as historical');
  assert.equal((await page.getByRole('button',{name:'个人明细周期',exact:true}).innerText()).trim(),'账号1');
  await pickCycle('账号2',1);
  assert.equal((await page.evaluate(()=>window.__CQG_TEST__.getState())).userCycle.tokens,9999);
  assert.equal((await page.getByRole('button',{name:'个人明细周期',exact:true}).innerText()).trim(),'账号2');
  await page.screenshot({path:path.join(artifacts,'billing-member-history.png')});
  await pickCycle('账号1',2);
  assert.equal((await page.evaluate(()=>window.__CQG_TEST__.getState())).userCycle.tokens,5678);
  assert.equal(await page.locator('.user-pool-summary>div').count(),3);
  await page.evaluate(()=>{window.__fixture.view.analytics.cycles=window.__fixture.view.analytics.cycles.filter(c=>c.id!=='earlier');window.__CQG_TEST__.applySnapshot(structuredClone(window.__fixture));});

  await page.getByRole('button',{name:'关闭对话框',exact:true}).click();
  await page.getByTestId('nav-stats').click();await page.locator('.pool-journal').waitFor();
  assert.equal(await page.getByTestId('cycle-record').count(),2);
  await page.evaluate(()=>{const data=structuredClone(window.__fixture);data.view.analytics.windows.cycle.quota_rows=data.view.analytics.windows.cycle.quota_rows.filter(r=>r.device!=='person1');data.view.analytics.windows.cycle.quota_pending_rows=[{device:'person1',account:'fixture-account',model:'gpt-6-astra',bucket:0}];window.__CQG_TEST__.applySnapshot(data);});
  assert.equal(await page.locator('.pool-members-summary b').first().innerText(),'0.00%');
  assert.ok((await page.locator('.pool-members-summary small').first().innerText()).includes('新增待分摊'));
  await page.evaluate(()=>window.__CQG_TEST__.applySnapshot(structuredClone(window.__fixture)));

  assert.deepEqual((await page.locator('.cycle-model-card>span').allTextContents()).slice(0,4),['GPT-6 Astra','GPT-5.6 Sol','GPT-5.6 Terra','GPT-5.6 Luna']);
  await page.locator('.pool-stats-head').getByRole('button',{name:'账号2',exact:true}).click();assert.equal(await page.getByTestId('cycle-record').count(),1);
  await page.evaluate(()=>{const data=structuredClone(window.__fixture),cycle=data.view.analytics.cycles.find(c=>c.account_label==='账号2');cycle.models.push({model:'codex-auto-review',tokens:72633});cycle.sampled_tokens+=72633;window.__CQG_TEST__.applySnapshot(data);});
  assert.ok(!(await page.locator('.cycle-models').innerText()).includes('codex-auto-review'));
  assert.ok((await page.getByTestId('cycle-unknown').innerText()).includes('72.63k'));
  const unknownBox=await page.getByTestId('cycle-unknown').boundingBox(),modelsBox=await page.locator('.cycle-models').boundingBox();assert.ok(unknownBox.y>modelsBox.y+modelsBox.height);assert.ok(Math.abs(unknownBox.x-modelsBox.x)<2);
  await page.screenshot({path:path.join(artifacts,'billing-stats.png')});
  await page.getByTestId('cycle-reset-tag').click();
  await page.getByRole('button',{name:'确认原因',exact:true}).click();
  assert.ok((await page.evaluate(()=>window.__commands)).some(c=>c.action==='group_rule'&&c.payload.kind==='reset'&&c.payload.cause==='official'));
  await page.getByTestId('nav-accounts').click();
  await page.locator('.account-item').first().waitFor();
  assert.deepEqual(await page.locator('.account-item strong').allTextContents(),['账号1','账号2']);
  await page.getByTestId('nav-sync').click();
  await page.locator('.paired-device').first().waitFor();
  assert.equal(await page.locator('.paired-device').count(),2);
  assert.ok((await page.getByTestId('join-progress').innerText()).includes('已连接共享组'));
  await page.locator('.paired-device').first().getByRole('button',{name:'移除',exact:true}).click();
  await page.locator('.modal').getByRole('button',{name:'确认',exact:true}).click();
  assert.ok((await page.evaluate(()=>window.__commands)).some(c=>c.action==='device_remove'&&c.payload.device==='fixture-peer'));
  await page.getByTestId('nav-settings').click();await page.locator('.group-policy-panel').waitFor();
  const toggle=page.getByRole('switch',{name:'跨周期补偿',exact:true});assert.equal(await toggle.isEnabled(),true);
  await toggle.click();assert.ok((await page.evaluate(()=>window.__commands)).some(c=>c.action==='compensation_toggle'&&c.payload.revision===1&&c.payload.enabled===true));
  await page.screenshot({path:path.join(artifacts,'billing-settings.png')});
  await page.evaluate(()=>{window.__fixture.view.shared_group.can_manage=false;window.__CQG_TEST__.applySnapshot(structuredClone(window.__fixture));});
  assert.equal(await toggle.isDisabled(),true);
  // Reproduce the waiting state on a second member's PC, keeping confirmed usage.
  await page.evaluate(()=>{
    const data=window.__fixture;
    data.settings.device_id='fixture-peer';data.settings.quota_display='fair';
    (data.settings.device_order??={})[data.view.display_account]=['person3','person1','person2'];
    data.view.shared_group.rules_locked=true;data.view.summary.compensation_enabled=true;
    data.view.billing.status='waiting';data.view.billing.reason='等待第三位成员同步历史明细';
    for(const person of data.view.summary.devices){person.local=person.id==='person2';person.fair_usage=null;person.available=null;person.debt=null;person.by_account=null;}
    data.view.summary.devices[2].joined=false;
    window.__CQG_TEST__.applySnapshot(structuredClone(data));
  });
  assert.equal(await page.getByTestId('compensation-locked').innerText(),'已开启 · 固定规则');
  assert.equal(await page.getByTestId('display-mode-locked').innerText(),'补偿显示模式 · 固定');
  assert.equal(await page.getByRole('switch',{name:'跨周期补偿',exact:true}).count(),0);
  assert.equal(await page.getByRole('button',{name:'额度显示基准',exact:true}).count(),0);
  assert.ok((await page.locator('.settings-line').first().innerText()).includes('66.67%'));
  assert.equal(await page.getByTestId('settings-user').first().locator('strong').innerText(),'A');
  await page.evaluate(()=>{window.__fixture.view.shared_group.can_manage=true;window.__CQG_TEST__.applySnapshot(structuredClone(window.__fixture));});
  assert.equal(await page.locator('.membership-advanced').getAttribute('open'),null);
  assert.equal(await page.locator('.member-bindings>button').last().isVisible(),false);
  await page.locator('.membership-advanced summary').click();
  assert.equal(await page.locator('.member-bindings>button').last().isVisible(),true);
  await page.screenshot({path:path.join(artifacts,'billing-fixed-settings.png')});
  await page.getByTestId('nav-overview').click();await page.getByTestId('pool-remaining').waitFor();
  assert.deepEqual(await page.getByTestId('device-row').evaluateAll(rows=>rows.map(row=>row.dataset.deviceId)),['person2','person3','person1']);
  assert.equal(await page.locator('.device-row .avatar img').first().getAttribute('src'),'avatars/person2.jpg');
  assert.equal(await page.locator('.legend-name').first().innerText(),'A');
  assert.equal(await page.locator('.device-usage').first().innerText(),'—');
  await page.getByRole('button',{name:'设备占比时间范围',exact:true}).click();await page.getByRole('option',{name:'新账周期',exact:true}).click();
  assert.equal(await page.locator('.donut-main-share').first().innerText(),'34.5%');await page.waitForTimeout(200);
  assert.equal(await page.locator('.device-quota-cell small').count(),0,'unavailable balance is explained once, not repeated as unknown under every member');
  assert.ok((await page.locator('.billing-status').innerText()).includes('已确认消费继续显示'));
  await page.evaluate(()=>{const data=structuredClone(window.__fixture);data.view.billing.last_confirmed={at:data.view.analytics.at-60,pending_quota:1};Object.assign(data.view.summary.devices.find(d=>d.id==='person2'),{confirmed_available:30,confirmed_available_cap:100/3});window.__CQG_TEST__.applySnapshot(data);});
  assert.equal(await page.locator('.device-usage').first().innerText(),'90.0%*');
  assert.equal(await page.locator('.device-columns>span').last().innerText(),'上次确认可用*');
  assert.ok((await page.locator('.billing-status').innerText()).includes('1.00 点待分摊'));
  await page.evaluate(()=>window.__CQG_TEST__.applySnapshot(structuredClone(window.__fixture)));

  const fills=await page.locator('.pool-track').evaluate(track=>({width:track.clientWidth,right:track.getBoundingClientRect().right,rows:[...track.children].map(n=>({left:n.getBoundingClientRect().left,right:n.getBoundingClientRect().right,width:n.getBoundingClientRect().width}))}));
  assert.ok(Math.abs(fills.rows[0].right-fills.right)<1,'account 1 starts at the right edge');
  assert.ok(Math.abs(fills.rows[1].right-fills.rows[0].left)<1,'colored segments remain continuous and grow toward the left');
  assert.ok(Math.abs(fills.rows[0].width/fills.width-.235)<.01&&Math.abs(fills.rows[1].width/fills.width-.385)<.01);
  await page.getByRole('button',{name:'趋势时间范围',exact:true}).click();await page.getByRole('option',{name:'每12小时',exact:true}).click();
  await page.getByRole('button',{name:'筛选用户',exact:true}).click();await page.getByRole('option',{name:'A · 本机',exact:true}).click();
  await page.evaluate(()=>{window.__overviewNode=document.querySelector('.overview');window.__rowNodes=[...document.querySelectorAll('.device-row')];});
  for(let i=0;i<8;i++){
    await page.evaluate(index=>{const data=structuredClone(window.__fixture);data.view.sync_caption=index%2?'等待确认':'同步中';data.view.summary.devices.reverse();window.__CQG_TEST__.applySnapshot(data);},i);
    assert.equal(await page.evaluate(()=>window.__overviewNode===document.querySelector('.overview')&&window.__rowNodes.every(n=>n.isConnected)),true);
  }
  let state=await page.evaluate(()=>window.__CQG_TEST__.getState());
  assert.equal(state.page,'overview');assert.equal(state.period,'twelve_hours');assert.equal(state.user,'person2');
  // Historical buffers retain the shared scope and every supported rolling period.
  await page.evaluate(()=>{
    const data=window.__fixture,history=structuredClone(data.view.analytics),at=history.at;
    for(const [key,step,count] of [['hour',120,780],['hour_curve',60,1560],['six_hours',300,360],['twelve_hours',300,432],['day',3600,768]]){
      const start=Math.floor(at/step)*step-(count-1)*step;
      const rows=Array.from({length:count},(_,bucket)=>({account:'fixture-account',device:'person2',model:'gpt-5.5',bucket,tokens:1000+bucket,input_tokens:900+bucket,cache_tokens:800,output_tokens:100,detail_missing:0,first_at:start+bucket*step,last_at:start+bucket*step}));
      history.windows[key]={start,step,count,rows,quota_rows:rows.map(row=>({...row,quota:.002,cache_quota:.001})),quota_pending_rows:[],quota_available:true,quota_ready:true,quota_gaps:[]};
    }
    window.__history=history;
  });
  for(const [label,period,step,duration] of [['每小时','hour',60,3600],['每6小时','six_hours',300,21600],['每12小时','twelve_hours',300,43200],['每天','day',3600,86400]]){
    await page.getByRole('button',{name:'趋势时间范围',exact:true}).click();await page.getByRole('option',{name:label,exact:true}).click();
    const slider=page.locator('.hour-slider input');await page.waitForFunction(()=>!document.querySelector('.hour-slider input')?.disabled);
    assert.equal(await slider.getAttribute('step'),String(step));
    const initial=await page.evaluate(()=>window.__CQG_TEST__.getState());
    await slider.evaluate((input,step)=>{input.value=Number(input.max)-step*2;input.dispatchEvent(new Event('input',{bubbles:true}));},step);
    await page.waitForFunction(()=>window.__CQG_TEST__.getState().chartEnd!==null);await page.waitForTimeout(350);
    const historical=await page.evaluate(()=>window.__CQG_TEST__.getState());
    assert.equal(historical.period,period);assert.ok(historical.chartEnd>0);
    assert.equal(historical.trend.points.length*historical.trend.step,duration);
    assert.ok(historical.trend.total>0);assert.ok(historical.trend.quotaTotals.person2>0);
    assert.equal(historical.user,initial.user,'history loads preserve member selection');
    await slider.evaluate(input=>{input.value=input.max;input.dispatchEvent(new Event('input',{bubbles:true}));});
    await page.waitForFunction(()=>window.__CQG_TEST__.getState().chartEnd===null);
  }
  assert.equal(await page.locator('.toast').filter({hasText:'历史图表账号不一致'}).count(),0);
  const historyCommands=await page.evaluate(()=>window.__commands.filter(c=>c.action==='chart_history'));
  assert.ok(historyCommands.every(c=>c.payload.account==='group:fixture-shared-group'));
  assert.ok(historyCommands.some(c=>c.payload.period==='six_hours')&&historyCommands.some(c=>c.payload.period==='twelve_hours'));
  assert.ok((await page.getByTestId('trend-chart').boundingBox()).height<=201);
  assert.ok((await page.locator('.hour-slider').boundingBox()).width>=100);
  await page.screenshot({path:path.join(artifacts,'billing-waiting-overview.png')});
  await page.getByTestId('nav-stats').click();await page.locator('.pool-members-summary').waitFor();
  assert.ok((await page.locator('.pool-members-summary strong').first().innerText()).startsWith('A'));
  assert.equal(await page.locator('.pool-members-summary b').first().innerText(),'0.00%');
  assert.deepEqual(await page.locator('.pool-members-summary b').allTextContents(),['0.00%','3.00%','0.00%'],'account 2 member totals include only account 2 confirmed usage');
  assert.equal(await page.getByTestId('cycle-record').count(),1,'the account filter survives page changes');
  assert.ok((await page.getByTestId('cycle-record').innerText()).includes('账号2'));
  const accountHeading=await page.locator('.cycle-account-label').evaluate(n=>({size:parseFloat(getComputedStyle(n).fontSize),color:getComputedStyle(n).color}));
  assert.ok(accountHeading.size>=18);assert.equal(accountHeading.color,'rgb(205, 138, 240)');
  await page.evaluate(()=>{window.__statsNode=document.querySelector('.page.scroll-page');window.__cycleNode=document.querySelector('.cycle-record');});
  for(let i=0;i<8;i++){
    await page.evaluate(index=>{const data=structuredClone(window.__fixture);data.view.sync_caption=index%2?'同步中':'等待确认';window.__CQG_TEST__.applySnapshot(data);},i);
    assert.equal(await page.evaluate(()=>window.__statsNode===document.querySelector('.page.scroll-page')&&window.__cycleNode===document.querySelector('.cycle-record')),true,'sync updates patch the current page without replacing it');
  }
  state=await page.evaluate(()=>window.__CQG_TEST__.getState());assert.equal(state.page,'stats');assert.equal(state.statsAccount,'fixture-account-b');
  assert.equal(await page.locator('img').evaluateAll(images=>images.some(image=>!image.complete||!image.naturalWidth)),false);
  await page.screenshot({path:path.join(artifacts,'billing-waiting-stats.png')});
  await page.getByTestId('nav-overview').click();await page.getByTestId('pool-remaining').waitFor();
  await page.evaluate(()=>{window.__fixture.settings.theme='light';window.__CQG_TEST__.applySnapshot(structuredClone(window.__fixture));});await page.waitForTimeout(300);
  await page.screenshot({path:path.join(artifacts,'billing-overview-light.png')});
  assert.deepEqual(errors,[]);console.log('BILLING_UI_PERCENT_AVATARS_FILTERS_RULES_LAYOUT_OK');
}finally{await browser.close();await new Promise(resolve=>server.close(resolve));}
