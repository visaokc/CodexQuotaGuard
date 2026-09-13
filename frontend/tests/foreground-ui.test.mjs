// Focus/visibility events exercise the same polling lifecycle as the native window.
import assert from 'node:assert/strict';
import http from 'node:http';
import {readFile} from 'node:fs/promises';
import path from 'node:path';
import {createRequire} from 'node:module';
import {billingFixture} from './billing-fixture.mjs';
const require=createRequire(import.meta.url),{chromium}=require('playwright'),root=path.resolve('dist');
const server=http.createServer(async(req,res)=>{try{const file=path.join(root,new URL(req.url,'http://localhost').pathname.replace(/^\/$/,'/index.html'));if(!file.startsWith(root+path.sep))throw Error('scope');res.setHeader('Content-Type',file.endsWith('.js')?'application/javascript':file.endsWith('.css')?'text/css':'text/html');res.end(await readFile(file));}catch{res.writeHead(404).end();}});
await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
const browser=await chromium.launch({channel:'msedge',headless:true});
try{
  const page=await browser.newPage({viewport:{width:750,height:680}});
  await page.addInitScript(data=>{window.__fixture=data;window.__snapshotCount=0;window.__CQG_TEST_BRIDGE__={snapshot:async()=>{window.__snapshotCount++;return structuredClone(window.__fixture);},command:async()=>({ok:true,data:{}}),window_action:async()=>({ok:true})};},billingFixture());
  await page.goto(`http://127.0.0.1:${server.address().port}/?test=1`);await page.locator('.app-shell.ready').waitFor();
  await page.evaluate(()=>window.dispatchEvent(new Event('blur')));
  const before=await page.evaluate(()=>window.__snapshotCount),version=await page.locator('.version').innerText();
  await page.evaluate(()=>window.__fixture.version='foreground-refresh');await page.waitForTimeout(2300);
  assert.equal(await page.evaluate(()=>window.__snapshotCount),before);assert.equal(await page.locator('.version').innerText(),version);
  assert.equal(await page.locator('.ui-paused').count(),1);
  await page.evaluate(()=>window.dispatchEvent(new Event('focus')));
  await page.waitForFunction(()=>document.querySelector('.version').textContent.includes('foreground-refresh'));
  assert.equal(await page.locator('.ui-paused').count(),0);
  await page.evaluate(()=>{Object.defineProperty(document,'hidden',{configurable:true,get:()=>true});document.dispatchEvent(new Event('visibilitychange'));});
  const hiddenCount=await page.evaluate(()=>window.__snapshotCount);
  await page.evaluate(()=>window.dispatchEvent(new Event('focus')));await page.waitForTimeout(2300);
  assert.equal(await page.evaluate(()=>window.__snapshotCount),hiddenCount);
  await page.evaluate(()=>{Object.defineProperty(document,'hidden',{configurable:true,get:()=>false});window.__fixture.version='visible-refresh';document.dispatchEvent(new Event('visibilitychange'));});
  await page.waitForFunction(()=>document.querySelector('.version').textContent.includes('visible-refresh'));
  console.log('Foreground lifecycle: no polling or UI replacement while inactive/hidden; immediate fresh snapshot on return passed');
  const startup=await browser.newPage({viewport:{width:750,height:680}});
  await startup.addInitScript(data=>{
    Object.defineProperty(document,'hidden',{configurable:true,get:()=>true});
    document.hasFocus=()=>false;window.__snapshotCount=0;window.__shown=false;
    setTimeout(()=>{window.__CQG_TEST_BRIDGE__={snapshot:async()=>{window.__snapshotCount++;return structuredClone(data);},window_action:async action=>{if(action==='shown')window.__shown=true;return {ok:true};}};},500);
  },billingFixture());
  await startup.goto(`http://127.0.0.1:${server.address().port}/?test=1`);
  await startup.waitForFunction(()=>window.__shown,{},{timeout:6000});
  assert.equal(await startup.locator('.app-shell.ready.ui-paused').count(),1);
  const startupCount=await startup.evaluate(()=>window.__snapshotCount);
  await startup.waitForTimeout(2300);
  assert.equal(await startup.evaluate(()=>window.__snapshotCount),startupCount);
  console.log('Background bootstrap: waits for delayed native bridge, acknowledges shown, then pauses passed');
}finally{await browser.close();server.close();}
