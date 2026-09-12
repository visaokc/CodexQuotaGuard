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

  await page.getByRole('button',{name:'设置',exact:true}).click();
  const panel=page.getByTestId('account-availability');await panel.waitFor();
  assert.equal(await panel.getByRole('button',{name:'暂停使用',exact:true}).count(),2);
  await panel.getByRole('button',{name:'暂停使用',exact:true}).nth(1).click();
  const command=await page.evaluate(()=>window.__commands.find(c=>c.action==='group_rule'));
  assert.equal(command.payload.kind,'availability');assert.equal(command.payload.paused,true);
  assert.equal(command.payload.account,'fixture-account-b');
  await page.evaluate(()=>{window.__fixture.view.account_summaries[1].paused=true;window.__fixture.view.shared_group.revision++;window.__CQG_TEST__.applySnapshot(structuredClone(window.__fixture));});
  await panel.getByRole('button',{name:'恢复使用',exact:true}).waitFor();
  await panel.getByRole('button',{name:'恢复使用',exact:true}).click();
  assert.equal((await page.evaluate(()=>window.__commands.filter(c=>c.action==='group_rule').at(-1))).payload.paused,false);
  await page.getByRole('button',{name:'概览',exact:true}).click();
  await page.waitForTimeout(1300);
  assert.equal(await page.getByTestId('pool-remaining').innerText(),'47.0%');
  assert.match(await page.getByTestId('pool-quota-card').innerText(),/已暂停/);
  assert.deepEqual(await page.locator('.pool-track i').evaluateAll(nodes=>nodes.map(n=>n.style.width)),['47%','0%']);
  assert.equal(await page.getByTestId('device-row').count(),3);
  await page.screenshot({path:path.join(artifacts,'account-paused-069.png')});
  assert.deepEqual(errors,[]);console.log('Availability UI: pause/resume payloads, active pool, retained members passed');
}finally{await browser.close();server.close();}

