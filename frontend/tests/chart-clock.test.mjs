// Exercise the real live chart; subpixel clock motion must not repaint at display refresh rate.
import assert from 'node:assert/strict';
import http from 'node:http';
import {readFile} from 'node:fs/promises';
import path from 'node:path';
import {createRequire} from 'node:module';
import {billingFixture} from './billing-fixture.mjs';
const require=createRequire(import.meta.url),{chromium}=require('playwright');
const root=path.resolve('dist');
const server=http.createServer(async(req,res)=>{try{const file=path.join(root,new URL(req.url,'http://localhost').pathname.replace(/^\/$/,'/index.html'));if(!file.startsWith(root+path.sep))throw Error('scope');res.setHeader('Content-Type',file.endsWith('.js')?'application/javascript':file.endsWith('.css')?'text/css':'text/html');res.end(await readFile(file));}catch{res.writeHead(404).end();}});
await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
const browser=await chromium.launch({channel:'msedge',headless:true});
try{
  const page=await browser.newPage({viewport:{width:750,height:680}});
  await page.addInitScript(data=>{window.__fixture=data;window.__CQG_TEST_BRIDGE__={snapshot:async()=>structuredClone(window.__fixture),command:async()=>({ok:true,data:{}}),window_action:async()=>({ok:true})};},billingFixture());
  await page.goto(`http://127.0.0.1:${server.address().port}/?test=1`);
  await page.locator('.app-shell.ready').waitFor();await page.evaluate(()=>window.__CQG_TEST__.pausePolling());await page.waitForTimeout(1500);
  const measurement=await page.evaluate(async()=>{
    const chart=document.querySelector('.trend-svg'),pan=chart.querySelector('.trend-pan-content');
    const offset=()=>Number(pan.getAttribute('transform').match(/translate\(([^,]+)/)[1]);
    let mutations=0;const observer=new MutationObserver(rows=>mutations+=rows.length);
    observer.observe(chart,{subtree:true,attributes:true,childList:true,characterData:true});
    const before=offset(),start=performance.now();await new Promise(resolve=>setTimeout(resolve,3000));
    const delta=before-offset(),seconds=(performance.now()-start)/1000;observer.disconnect();
    const trend=window.__CQG_TEST__.getState().trend;
    return {mutations,delta,expected:seconds*384/(trend.step*trend.points.length)};
  });
  assert.ok(measurement.mutations<=40,JSON.stringify(measurement));
  assert.ok(measurement.delta>0&&Math.abs(measurement.delta-measurement.expected)<.1,JSON.stringify(measurement));
  await page.evaluate(()=>{Object.defineProperty(document,'hidden',{configurable:true,get:()=>true});document.dispatchEvent(new Event('visibilitychange'));});
  const before=await page.locator('.trend-pan-content').getAttribute('transform');await page.waitForTimeout(600);
  assert.equal(await page.locator('.trend-pan-content').getAttribute('transform'),before);
  await page.evaluate(()=>{Object.defineProperty(document,'hidden',{configurable:true,get:()=>false});document.dispatchEvent(new Event('visibilitychange'));});
  await page.waitForTimeout(600);assert.notEqual(await page.locator('.trend-pan-content').getAttribute('transform'),before);
  console.log('Live clock: bounded subpixel redraws, elapsed-time position, hidden pause and visible recovery passed',measurement);
}finally{await browser.close();server.close();}
