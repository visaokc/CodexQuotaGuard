// Isolated browser only; never attaches to or controls the user's desktop.
import assert from 'node:assert/strict';
import http from 'node:http';
import {readFile,mkdir} from 'node:fs/promises';
import path from 'node:path';
import {createRequire} from 'node:module';
import {fixture} from './fixture.mjs';
import {sharedFixture} from './shared-fixture.mjs';
const require=createRequire(import.meta.url),{chromium}=require('playwright');
const root=path.resolve('dist'),artifacts=path.resolve('test-artifacts');await mkdir(artifacts,{recursive:true});
const server=http.createServer(async(req,res)=>{try{const file=path.join(root,new URL(req.url,'http://localhost').pathname.replace(/^\/$/,'/index.html'));if(!file.startsWith(root+path.sep))throw Error('scope');const data=await readFile(file);res.setHeader('Content-Type',file.endsWith('.js')?'application/javascript':file.endsWith('.css')?'text/css':'text/html');res.end(data);}catch{res.writeHead(404).end();}});
await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
const browser=await chromium.launch({channel:'msedge',headless:true});
try{
  const page=await browser.newPage({viewport:{width:750,height:680},deviceScaleFactor:1,colorScheme:'dark'}),errors=[];
  page.on('pageerror',error=>errors.push(error.message));
  await page.addInitScript(data=>{
    window.__fixture=data;window.__commands=[];
    window.__CQG_TEST_BRIDGE__={snapshot:async()=>structuredClone(window.__fixture),command:async(action,payload)=>{
      window.__commands.push({action,payload});
      if(action==='chart_history')return {ok:true,data:structuredClone(window.__fixture.view.analytics)};
      if(action==='pair_join'){window.__fixture.view.connection.ready=false;window.__fixture.pairing.ready=false;}
      if(action==='account_scan'){window.__fixture.view.identity={account:'fixture-account-new',label:'待添加的新账号',plan:'pro'};return {ok:true,data:{label:'待添加的新账号'}};}
      if(action==='account_add')window.__fixture.accounts.push({account:window.__fixture.view.identity.account,label:window.__fixture.view.identity.label});
      return {ok:true,data:{}};
    },window_action:async()=>({ok:true})};
  },fixture());
  await page.goto(`http://127.0.0.1:${server.address().port}/?test=1`);
  await page.locator('.app-shell.ready').waitFor();await page.evaluate(()=>window.__CQG_TEST__.pausePolling());await page.waitForTimeout(400);
  assert.equal(await page.getByRole('button',{name:'趋势时间范围',exact:true}).innerText(),'每小时');
  assert.match(await page.getByTestId('trend-chart').locator('h2').innerText(),/^每小时/);
  const hit=page.getByTestId('trend-hit');await hit.hover({position:{x:150,y:40}});
  await page.locator('.chart-tooltip').waitFor();
  const before=await page.locator('.tooltip-summary').innerText();
  for(let i=0;i<3;i++){
    await page.evaluate(()=>{const data=window.__fixture;data.view.analytics.at+=2;for(const window of Object.values(data.view.analytics.windows))for(const row of window.rows)row.tokens+=1000;window.__CQG_TEST__.applySnapshot(structuredClone(data));});
    await page.waitForTimeout(450);
    assert.equal(await page.locator('.chart-tooltip').isVisible(),true,'stationary hover survives live clock and usage snapshots');
  }
  assert.notEqual(await page.locator('.tooltip-summary').innerText(),before,'hovered values update without needing a mouse move');
  await page.mouse.move(5,5);await page.waitForTimeout(180);assert.equal(await page.locator('.chart-tooltip').count(),0,'leaving the chart still closes the tooltip');
  await page.evaluate(data=>{window.__fixture=data;window.__CQG_TEST__.applySnapshot(structuredClone(data));},sharedFixture());
  await page.waitForTimeout(450);
  assert.equal(await page.getByTestId('shared-quota-card').count(),1);
  assert.equal(await page.locator('.account-quota-row').count(),2);
  assert.deepEqual(await page.locator('.account-quota-track').evaluateAll(nodes=>nodes.map(node=>node.getAttribute('aria-valuenow'))),['47','77']);
  assert.equal(new Set(await page.locator('.account-quota-track i').evaluateAll(nodes=>nodes.map(node=>getComputedStyle(node).backgroundColor))).size,2);
  assert.equal(await page.locator('.account-refresh-row').count(),2);
  assert.equal(await page.getByTestId('device-row').count(),3);
  assert.ok((await page.locator('.device-usage').allTextContents()).every(text=>text==='计费待启用'));
  assert.equal(await page.getByTestId('account-add').count(),0,'account enrollment belongs on the Accounts page');
  assert.equal((await page.evaluate(()=>window.__CQG_TEST__.getState())).trend.series.length,3);
  const layout=await page.evaluate(()=>{const overview=document.querySelector('.overview'),rows=[...document.querySelectorAll('.device-row')];return {height:innerHeight,scrollHeight:overview.scrollHeight,clientHeight:overview.clientHeight,lastRow:rows.at(-1).getBoundingClientRect().bottom,overflow:document.documentElement.scrollWidth>innerWidth};});
  assert.equal(layout.overflow,false);assert.ok(layout.lastRow<680,'all three members fit in the enlarged window');assert.ok(layout.scrollHeight<=layout.clientHeight+1,'overview needs no scrolling at 750x680');
  await page.screenshot({path:path.join(artifacts,'shared-overview-dark.png')});
  await page.getByTestId('device-row').first().click();await page.locator('.member-period-picker').waitFor();
  assert.equal(await page.locator('.user-cycle-periods').count(),0);
  assert.doesNotMatch(await page.locator('.user-cycle-primary').innerText(),/估计/);
  await page.waitForTimeout(250);
  await page.screenshot({path:path.join(artifacts,'shared-user-summary.png')});
  await page.getByRole('button',{name:'关闭对话框',exact:true}).click();await page.waitForTimeout(200);
  await hit.hover({position:{x:150,y:40}});await page.locator('.chart-tooltip').waitFor();assert.match(await page.locator('.chart-tooltip').innerText(),/计费待启用/);
  assert.doesNotMatch(await page.locator('.chart-tooltip').innerText(),/仅计官方已确认额度/);
  assert.equal(await page.locator('.chart-tooltip').evaluate(node=>node.scrollWidth<=node.clientWidth),true,'pending billing labels fit inside the tooltip');
  await page.screenshot({path:path.join(artifacts,'shared-tooltip.png')});
  await page.mouse.move(5,5);
  for(const [label,key,count] of [['每6小时','six_hours',72],['每12小时','twelve_hours',144]]){
    await page.getByRole('button',{name:'趋势时间范围',exact:true}).click();await page.getByRole('option',{name:label,exact:true}).click();
    await page.waitForTimeout(180);
    const state=await page.evaluate(()=>window.__CQG_TEST__.getState());
    assert.ok((await page.getByTestId('trend-chart').locator('h2').innerText()).startsWith(label));
    assert.equal(state.trend.points.length,count);assert.equal(state.trend.total,sharedFixture().view.analytics.windows[key].rows.reduce((sum,row)=>sum+row.tokens,0));
    assert.equal(await page.getByRole('slider').count(),1,'six and twelve hour windows provide the historical slider');
    assert.equal(await page.getByRole('slider').getAttribute('step'),'300');
  }
  await page.getByRole('button',{name:'设备占比时间范围',exact:true}).click();await page.getByRole('option',{name:'每12小时',exact:true}).click();
  assert.equal((await page.evaluate(()=>window.__CQG_TEST__.getState())).pie.total,sharedFixture().view.analytics.windows.pie_twelve_hours.rows.reduce((sum,row)=>sum+row.tokens,0));
  await page.setViewportSize({width:730,height:650});await page.waitForTimeout(180);
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
  assert.ok((await page.getByTestId('device-row').last().boundingBox()).y+55<=650,'third member remains visible at the minimum window size');
  await page.setViewportSize({width:750,height:680});
  await page.getByTestId('nav-accounts').click();await page.getByTestId('account-add').waitFor();
  assert.equal(await page.getByTestId('account-add').isDisabled(),true,'already tracked account cannot be added twice');
  await page.getByRole('button',{name:'扫描当前登录',exact:true}).click();
  assert.equal(await page.getByTestId('account-add').isEnabled(),true);
  await page.getByTestId('account-add').click();
  assert.ok((await page.evaluate(()=>window.__commands)).some(item=>item.action==='account_add'));
  await page.screenshot({path:path.join(artifacts,'shared-accounts.png')});
  await page.getByTestId('nav-settings').click();await page.getByRole('switch',{name:'跨周期补偿',exact:true}).waitFor();
  assert.equal(await page.getByRole('switch',{name:'跨周期补偿',exact:true}).isDisabled(),true);
  const compensation=page.locator('.settings-line').filter({has:page.getByRole('switch',{name:'跨周期补偿',exact:true})});
  assert.match(await compensation.innerText(),/准备期间不新增或执行补偿/);
  assert.doesNotMatch(await compensation.innerText(),/默认关闭|110%/,'old compensation instructions stay hidden without a migration note');
  await page.evaluate(()=>{window.__fixture.view.shared_group.billing_start_note='原两人当前周期超额不结转，从下周期开始。';window.__CQG_TEST__.applySnapshot(structuredClone(window.__fixture));});
  assert.match(await compensation.innerText(),/当前周期超额不结转/);
  assert.doesNotMatch(await compensation.innerText(),/默认关闭|110%/,'migration note cannot activate legacy compensation instructions');
  assert.equal(await page.getByRole('switch',{name:'Codex 自动限额',exact:true}).isDisabled(),true);
  await page.getByTestId('nav-sync').click();await page.getByRole('button',{name:'输入匹配码',exact:true}).click();
  await page.locator('.modal textarea').fill('isolated-test-code');await page.getByRole('button',{name:'匹配并同步',exact:true}).click();
  await page.getByRole('button',{name:'确认',exact:true}).click();
  await page.getByTestId('join-modal-progress').waitFor();assert.match(await page.getByTestId('join-modal-progress').innerText(),/匹配码已保存.*等待 Tailscale 授权/);
  await page.evaluate(()=>{window.__fixture.pairing.ready=true;window.__fixture.view.connection.ready=true;window.__CQG_TEST__.applySnapshot(structuredClone(window.__fixture));});
  assert.match(await page.getByTestId('join-modal-progress').innerText(),/已连接共享组.*账本已同步/);
  await page.screenshot({path:path.join(artifacts,'shared-join-progress.png')});
  assert.deepEqual(errors,[]);
  console.log(JSON.stringify({shared_ui:'passed',stationary_tooltip:'passed',layout}));
}finally{await browser.close();await new Promise(resolve=>server.close(resolve));}
