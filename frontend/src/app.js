import {createApp,ref,reactive,computed,watch,onMounted,onBeforeUnmount,nextTick} from 'vue';
import {SelectBox,TrendChart,DonutChart} from './components.js';
import {COLORS,COLOR_NAMES,compact,devicesFor,aggregate,quotaValue,timeLabel,modelOptions,officialUsageColor,officialQuotaRemaining,refreshRemaining,dailyQuotaUsage} from './data.js';

const testMode=new URLSearchParams(location.search).get('test')==='1';
const bridge=()=>testMode&&window.__CQG_TEST_BRIDGE__ ? window.__CQG_TEST_BRIDGE__ : window.pywebview?.api;
const app=createApp({
  components:{SelectBox,TrendChart,DonutChart},
  setup(){
    const snapshot=ref({version:'',view:{},settings:{},accounts:[],pairing:{},update:{},notices:[]});
    const page=ref('overview'),ready=ref(false),busy=ref(false),toast=ref(''),selectedDevice=ref(''),selectedAccount=ref('');
    const model=ref(''),user=ref(''),kind=ref('line'),period=ref('day'),piePeriod=ref('cycle');
    const modal=ref(null),textInput=ref(''),capInput=ref(50),pairCode=ref(''),joinCode=ref(''),history=ref(null),historyPeriod=ref('day');
    const advanced=ref(false),settingsForm=reactive({}),connectionForm=reactive({rendezvous_url:'',relay_token:'',stun_url:'',force_relay:false});
    const formDirty=ref(false),lastReceipt=ref(null),selectedPath=ref(-1),pairPending=ref(false);
    const userColor=ref(''),dragging=ref(''),pendingOrder=ref(null);
    const systemTheme=window.matchMedia('(prefers-color-scheme: dark)'),systemDark=ref(systemTheme.matches);
    const updateSystemTheme=e=>systemDark.value=e.matches;
    systemTheme.addEventListener('change',updateSystemTheme);
    const clockNow=ref(Date.now()/1000);
    let devicePress=null;
    let pollTimer=0,toastTimer=0,pairTimer=0,disposed=false;
    const seenNotices=new Set();
    const view=computed(()=>snapshot.value.view||{}),settings=computed(()=>snapshot.value.settings||{});
    const theme=computed(()=>settings.value.theme||'system');
    watch([theme,systemDark],()=>{document.documentElement.dataset.theme=theme.value==='system'?(systemDark.value?'dark':'light'):theme.value;},{immediate:true});
    async function setTheme(value){await command('settings_save',{settings:{theme:value}});}
    const devices=computed(()=>{const items=devicesFor(snapshot.value);if(!pendingOrder.value)return items;const order=[...pendingOrder.value,...items.map(d=>d.id).filter(id=>!pendingOrder.value.includes(id))];return items.sort((a,b)=>order.indexOf(a.id)-order.indexOf(b.id));}),account=computed(()=>view.value.identity?.account||'');
    const selected=computed(()=>devices.value.find(d=>d.id===selectedDevice.value));
    const epoch=computed(()=>view.value.summary?.epoch);
    const officialUsed=computed(()=>typeof epoch.value?.used==='number'&&Number.isFinite(epoch.value.used)?epoch.value.used:null);
    const dailyAverage=computed(()=>dailyQuotaUsage(epoch.value,view.value.summary?.reset_pending));
    const officialLeft=computed(()=>officialQuotaRemaining(officialUsed.value));
    const models=computed(()=>modelOptions(snapshot.value));
    const users=computed(()=>[{value:'',label:'全部用户'},...devices.value.map(d=>({value:d.id,label:d.label+(d.local?' · 本机':'')}))]);
    const trend=computed(()=>aggregate(snapshot.value,period.value,model.value,user.value));
    const cycleBudget=computed(()=>view.value.analytics?.cycles?.find(c=>c.is_current)||view.value.summary?.token_budget||{});
    const pie=computed(()=>aggregate(snapshot.value,piePeriod.value,model.value));
    const trendAll=computed(()=>aggregate(snapshot.value,period.value,model.value));
    const trendColor=computed(()=>devices.value.find(d=>d.id===user.value)?.color||COLORS[0]);
    const trendName=computed(()=>({hour:'最近 1 小时',day:'最近 24 小时',week:'最近 7 天',month:'最近 30 天'})[period.value]);
    const localDevice=computed(()=>devices.value.find(d=>d.local));
    const updateText=computed(()=>snapshot.value.update?.message||snapshot.value.update?.status||'签名校验 · 保留账号与账本');
    const accounts=computed(()=>Array.isArray(snapshot.value.accounts)?snapshot.value.accounts:[]);
    const pairStatus=computed(()=>snapshot.value.pairing?.error||snapshot.value.pairing?.message||({preparing:'正在准备连接',ready:'连接已就绪',error:'连接异常'})[snapshot.value.pairing?.stage]||snapshot.value.pairing?.state||'等待连接');
    const historyRows=computed(()=>Object.entries(history.value?.history?.[historyPeriod.value]||{}).sort(([a],[b])=>b.localeCompare(a)));
    const nav=[['overview','概览','grid'],['stats','总数据','chart'],['accounts','账号','user'],['sync','同步','sync'],['settings','设置','settings'],['help','说明','info']];
    function inform(message){toast.value=String(message);clearTimeout(toastTimer);toastTimer=setTimeout(()=>toast.value='',5000);}
    function applySnapshot(data){
      if(!data?.view)return;
      clockNow.value=Date.now()/1000;
      const oldAccount=account.value;snapshot.value=data;
      if(oldAccount!==account.value){lastReceipt.value=null;selectedDevice.value='';model.value='';user.value='';cancelDeviceDrag();}
      if(view.value.sync_confirmed_at)lastReceipt.value=view.value.sync_confirmed_at;
      for(const notice of data.notices||[])if(typeof notice==='string'&&!seenNotices.has(notice)){seenNotices.add(notice);inform(notice);}
      if(user.value&&!devices.value.some(d=>d.id===user.value))user.value='';
      if(!devices.value.some(d=>d.id===selectedDevice.value))selectedDevice.value='';
      if(model.value&&!models.value.some(m=>m.value===model.value))model.value='';
      if(!selectedAccount.value&&accounts.value.length)selectedAccount.value=accounts.value[0].account;
      if(!ready.value){connectionForm.force_relay=!!settings.value.force_relay;resetForm();ready.value=true;nextTick(()=>host('shown'));}
    }
    async function poll(){
      if(disposed)return;
      try{if(bridge())applySnapshot(await bridge().snapshot());}
      catch(error){if(!ready.value)inform('读取数据失败：'+error.message);}
      if(!disposed)pollTimer=setTimeout(poll,2000);
    }
    async function command(action,payload={},message=''){
      if(busy.value)return null;
      busy.value=true;
      try{
        const api=bridge();if(!api)throw Error('界面正在连接本机服务');
        const result=await api.command(action,payload);
        if(!result?.ok)throw Error(result?.error||'操作未完成');
        if(message)inform(message);
        // A snapshot patches existing keyed components; open menus and selection survive.
        applySnapshot(await api.snapshot());
        return result.data??{};
      }catch(error){inform(error.message||String(error));return null;}
      finally{busy.value=false;}
    }
    async function host(action){
      try{const result=await bridge()?.window_action(action);if(result?.ok===false)throw Error(result.error||'窗口操作未完成');return result;}catch(error){inform(error.message||String(error));}
    }
    function nativeDrag(e){if(e.button===0&&!e.target.closest('button,input,select,a,.select'))host('drag');}
    function switchPage(target){cancelDeviceDrag();page.value=target;if(target==='settings'&&!formDirty.value)resetForm();}
    function resetForm(){Object.assign(settingsForm,{...settings.value,program_paths:[...(settings.value.program_paths||[])]});formDirty.value=false;}
    function openModal(type,device){
      if(device)selectedDevice.value=device.id;
      textInput.value=settings.value.device_notes?.[account.value]?.[selectedDevice.value]||'';
      userColor.value=selected.value?.color||COLORS[0];capInput.value=localDevice.value?.cap||settings.value.quota||50;
      modal.value={type};nextTick(()=>document.querySelector('.modal input,.modal textarea')?.focus());
    }
    function confirm(title,body,action,payload={}){modal.value={type:'confirm',title,body,action,payload};}
    async function confirmAction(){const m=modal.value;if(!m)return;const result=await command(m.action,{...m.payload,confirmed:true});if(result!==null)modal.value=null;}
    async function saveUserSettings(){
      const payload={account:account.value,device:selectedDevice.value};
      if(textInput.value.trim()!==(settings.value.device_notes?.[account.value]?.[selectedDevice.value]||'')){
        if(await command('note_save',{...payload,text:textInput.value})===null)return;
      }
      if(userColor.value!==selected.value?.color&&await command('color_save',{...payload,color:userColor.value})===null)return;
      modal.value=null;inform('用户设置已保存');
    }
    function beginDeviceDrag(event,device){if(event.button!==0||busy.value)return;devicePress={id:device.id,x:event.clientX,y:event.clientY,account:account.value,moved:false};}
    function moveDeviceDrag(event){
      if(!devicePress)return;
      if(!devicePress.moved&&Math.hypot(event.clientX-devicePress.x,event.clientY-devicePress.y)<7)return;
      devicePress.moved=true;dragging.value=devicePress.id;
      const target=document.elementFromPoint(event.clientX,event.clientY)?.closest('[data-device-id]')?.dataset.deviceId;
      if(!target||target===devicePress.id)return;
      const order=devices.value.map(d=>d.id),from=order.indexOf(devicePress.id),to=order.indexOf(target);
      if(from>=0&&to>=0){order.splice(from,1);order.splice(to,0,devicePress.id);pendingOrder.value=order;}
    }
    async function endDeviceDrag(event){
      const press=devicePress;if(!press)return;devicePress=null;dragging.value='';
      if(press.moved){
        if(pendingOrder.value&&press.account===account.value){
          const result=await command('device_order_save',{account:press.account,devices:[...pendingOrder.value]});
          if(result===null)try{applySnapshot(await bridge().snapshot());}catch{/* The regular snapshot poll will retry. */}
        }
        pendingOrder.value=null;
      }else if(event.target.closest('[data-device-id]')?.dataset.deviceId===press.id){
        const device=devices.value.find(d=>d.id===press.id);if(device)openModal('user',device);
      }
    }
    function cancelDeviceDrag(){devicePress=null;dragging.value='';pendingOrder.value=null;}
    async function saveCap(){if(await command('cap_save',{account:account.value,cap:Number(capInput.value)},'设备配额已保存')!==null)modal.value=null;}
    async function scanAccount(){const data=await command('account_scan');if(data)inform(data.label?`识别到：${data.label}`:'当前没有可添加的订阅账号');}
    async function showHistory(){const data=await command('account_history',{account:selectedAccount.value});if(data){history.value=data;modal.value={type:'history'};}}
    async function generatePair(){
      pairPending.value=true;modal.value={type:'pair'};
      const fetchCode=async()=>{
        if(busy.value){pairTimer=setTimeout(fetchCode,1500);return;}
        const data=await command('pair_generate');
        if(disposed)return;
        if(data?.code){pairCode.value=data.code;pairPending.value=false;}
        else if(data?.pending&&modal.value?.type==='pair')pairTimer=setTimeout(fetchCode,1500);
        else pairPending.value=false;
      };
      await fetchCode();
    }
    function joinPair(){confirm('加入设备组','配对会切换本机设备组并重新连接。请确认匹配码来自你的另一台设备。','pair_join',{code:joinCode.value.trim()});}
    async function copyPair(){try{await navigator.clipboard.writeText(pairCode.value);inform('匹配码已复制');}catch{document.querySelector('.pair-code')?.select();inform('已选中匹配码，请按 Ctrl+C 复制');}}
    async function saveSettings(){
      const payload={settings:Object.fromEntries(['name','codex_home','quota','multiplier','interval','program_paths','autostart'].map(key=>[key,settingsForm[key]]))};
      for(const key of ['quota','interval','multiplier'])payload.settings[key]=Number(payload.settings[key]);
      if(settings.value.auto_block&&(settingsForm.codex_home!==settings.value.codex_home||JSON.stringify(settingsForm.program_paths)!==JSON.stringify(settings.value.program_paths))){confirm('更改限额目标','自动限额已开启。新的数据目录或程序路径会改变限额保护目标，请确认保存。','settings_save',payload);return;}
      if(await command('settings_save',payload,'监测设置已保存')!==null)formDirty.value=false;
    }
    function toggleLimit(){if(settings.value.auto_block)command('limit_toggle',{enabled:false,account:account.value},'自动限额已关闭');else confirm('开启自动限额','达到设备配额时会暂停选定 Codex 进程并限制其联网，需要管理员权限。','limit_toggle',{enabled:true,account:account.value});}
    async function discover(){const data=await command('programs_discover');const paths=Array.isArray(data)?data:data?.paths||data?.program_paths;if(paths){settingsForm.program_paths=[...paths];formDirty.value=true;}}
    async function browseProgram(){const result=await host('browse_program');const paths=Array.isArray(result?.data)?result.data:typeof result==='string'?[result]:result?.path?[result.path]:[];if(paths.length){settingsForm.program_paths=[...new Set([...(settingsForm.program_paths||[]),...paths])];formDirty.value=true;}}
    function removePath(){if(selectedPath.value>=0){settingsForm.program_paths.splice(selectedPath.value,1);selectedPath.value=-1;formDirty.value=true;}}
    function route(d){return d.local?'本机设备':view.value.peers?.[d.id]?.route||'未连接';}
    function deviceConnected(d){return d.local||Object.prototype.hasOwnProperty.call(view.value.peers||{},d.id);}
    function deviceState(d){return d.online?(d.active?'Codex 使用中':d.unbound_active?'活动待归属':'暂无近期活动'):view.value.peers?.[d.id]?'连接在线 · 状态过期':'离线 / 已切换';}
    function usage(d){const n=quotaValue(d,settings.value.quota_display||'personal');return n==null?'—':n.toFixed(2)+'%';}
    async function saveConnection(){const payload={force_relay:connectionForm.force_relay};for(const key of ['rendezvous_url','relay_token','stun_url'])if(connectionForm[key].trim())payload[key]=connectionForm[key].trim();await command('connection_save',payload,'连接设置已保存');}
    async function exportDiagnostics(){await host('export_diagnostics');}
    function hostQuit(){if(view.value.blocked)modal.value={type:'quit'};else host('quit');}
    watch(()=>modal.value,(current,previous)=>{if(previous?.type==='user'&&current?.type!=='user')selectedDevice.value='';if(!modal.value){clearTimeout(pairTimer);pairPending.value=false;}});
    onMounted(()=>{poll();window.addEventListener('host-quit',hostQuit);window.addEventListener('pointermove',moveDeviceDrag);window.addEventListener('pointerup',endDeviceDrag);window.addEventListener('pointercancel',cancelDeviceDrag);window.addEventListener('blur',cancelDeviceDrag);window.addEventListener('keydown',e=>{if(e.key==='Escape'){modal.value=null;cancelDeviceDrag();}});});
    onBeforeUnmount(()=>{systemTheme.removeEventListener('change',updateSystemTheme);disposed=true;clearTimeout(pollTimer);clearTimeout(toastTimer);clearTimeout(pairTimer);window.removeEventListener('host-quit',hostQuit);window.removeEventListener('pointermove',moveDeviceDrag);window.removeEventListener('pointerup',endDeviceDrag);window.removeEventListener('pointercancel',cancelDeviceDrag);window.removeEventListener('blur',cancelDeviceDrag);});
    if(testMode)window.__CQG_TEST__={pausePolling:()=>clearTimeout(pollTimer),applySnapshot,getState:()=>({page:page.value,selectedDevice:selectedDevice.value,user:user.value,model:model.value,pie:pie.value,trend:trend.value})};
    return {dailyAverage,cycleBudget,theme,setTheme,snapshot,page,ready,busy,toast,selectedDevice,selectedAccount,model,user,kind,period,piePeriod,modal,textInput,capInput,pairCode,joinCode,history,historyPeriod,advanced,settingsForm,connectionForm,formDirty,lastReceipt,selectedPath,pairPending,userColor,dragging,view,settings,devices,account,selected,epoch,officialUsed,officialLeft,officialUsageColor,clockNow,refreshRemaining,models,users,trend,trendAll,pie,trendColor,trendName,localDevice,updateText,accounts,pairStatus,historyRows,nav,COLORS,COLOR_NAMES,compact,timeLabel,command,host,nativeDrag,switchPage,openModal,confirm,confirmAction,saveUserSettings,beginDeviceDrag,saveCap,scanAccount,showHistory,generatePair,joinPair,copyPair,saveSettings,toggleLimit,discover,browseProgram,removePath,route,deviceConnected,deviceState,usage,saveConnection,exportDiagnostics,inform};
  },
  template:`
  <div class="app-shell" :class="{ready}">
    <header class="titlebar" @mousedown="nativeDrag" @dblclick.self="host('maximize')">
      <div class="app-logo" title="Codex 配额管家"><svg viewBox="0 0 30 30"><path d="M22 10a10 10 0 1 0 1 9"/><rect x="12" y="15" width="3" height="6"/><rect x="17" y="11" width="3" height="10"/><circle cx="24" cy="7" r="2"/></svg></div>
      <nav><button v-for="[id,label,icon] in nav" :key="id" :data-testid="'nav-'+id" :class="{active:page===id}" @click="switchPage(id)"><svg viewBox="0 0 20 20" aria-hidden="true"><g v-if="icon==='grid'"><rect x="3" y="3" width="5" height="5" rx="1"/><rect x="12" y="3" width="5" height="5" rx="1"/><rect x="3" y="12" width="5" height="5" rx="1"/><rect x="12" y="12" width="5" height="5" rx="1"/></g><g v-else-if="icon==='chart'"><path d="M3 17h14M5 14V9m5 5V3m5 11V6"/></g><g v-else-if="icon==='user'"><circle cx="10" cy="6" r="3"/><path d="M4 17v-2a6 6 0 0 1 12 0v2"/></g><g v-else-if="icon==='sync'"><path d="M3 7h13l-3-3m4 9H4l3 3"/></g><g v-else-if="icon==='settings'"><path d="m8 2 4 0 1 3 3 1 2 4-2 4-3 1-1 3H8l-1-3-3-1-2-4 2-4 3-1z"/><circle cx="10" cy="10" r="3"/></g><g v-else><circle cx="10" cy="10" r="7"/><path d="M10 9v5M10 5v1"/></g></svg><span>{{label}}</span></button></nav>
      <span class="version">v{{snapshot.version||'…'}}</span><div class="window-buttons"><div class="theme-picker"><SelectBox :model-value="theme" @update:model-value="setTheme" :options="[{value:'system',label:'跟随系统'},{value:'light',label:'白天模式'},{value:'dark',label:'黑夜模式'}]" label="界面主题"><svg class="theme-icon" viewBox="0 0 20 20" aria-hidden="true"><g v-if="theme==='light'"><circle cx="10" cy="10" r="3.5"/><path d="M10 1v2m0 14v2M1 10h2m14 0h2M3.6 3.6 5 5m10 10 1.4 1.4M3.6 16.4 5 15M15 5l1.4-1.4"/></g><path v-else-if="theme==='dark'" d="M16.5 12.3A7 7 0 0 1 7.7 3.5 7 7 0 1 0 16.5 12.3Z"/><g v-else><rect x="2" y="3" width="16" height="11" rx="2"/><path d="M7 18h6m-3-4v4"/></g></svg></SelectBox></div><button @click="host('minimize')" title="最小化" aria-label="最小化"><svg viewBox="0 0 16 16"><path d="M4 8h8"/></svg></button><button class="window-close" @click="host('close')" title="隐藏到托盘" aria-label="隐藏到托盘"><svg viewBox="0 0 16 16"><path d="m5 5 6 6m0-6-6 6"/></svg></button></div>
    </header>
    <main><Transition name="page" mode="out-in">
      <section v-if="page==='overview'" key="overview" class="overview page">
        <div class="summary-grid">
          <article class="summary-card"><span class="eyebrow">官方周额度剩余</span><strong data-testid="official-remaining" :style="{color:officialUsageColor(officialUsed)}">{{officialLeft!==null?officialLeft.toFixed(0)+'%':'—'}}</strong><div class="daily-average" title="本周期已观测额度增量 ÷ 已经过天数；不包含首次观测前的未知用量"><span>日均使用</span><b data-testid="daily-quota-average">{{dailyAverage!==null?dailyAverage.toFixed(1):'—'}}<small v-if="dailyAverage!==null">% / 天</small></b></div><div class="quota-progress" role="progressbar" :aria-valuenow="officialLeft" aria-valuemin="0" aria-valuemax="100"><i :style="{transform:'scaleX('+(officialLeft||0)/100+')',background:officialUsageColor(officialUsed)}"></i></div></article>
          <article class="summary-card sync-card"><span class="eyebrow">设备同步状态</span><strong :class="{'sync-ok':view.sync_caption==='已同步'}">{{view.sync_caption||'等待连接'}}</strong><small>最近同步 {{lastReceipt?timeLabel(lastReceipt,true):'—'}}</small></article>
          <article class="summary-card refresh-card"><span class="eyebrow">距下次官方刷新</span><strong class="reset-time" data-testid="refresh-remaining">{{refreshRemaining(epoch?.reset_at,clockNow)}}</strong><small data-testid="refresh-date">{{timeLabel(epoch?.reset_at)}}</small></article>
        </div>
        <div class="chart-controls"><div class="filters"><SelectBox v-model="model" :options="models" label="筛选模型"/><SelectBox v-model="user" :options="users" label="筛选用户"/></div><div class="chart-modes"><div class="segment"><button :class="{active:kind==='line'}" @click="kind='line'">曲线</button><button :class="{active:kind==='bar'}" @click="kind='bar'">柱状</button></div><SelectBox v-model="period" :options="[{value:'hour',label:'一小时'},{value:'day',label:'一天'},{value:'week',label:'一周'},{value:'month',label:'一月'}]" label="趋势时间范围"/></div></div>
        <div class="charts-grid"><article class="chart-card donut-card" data-testid="pie-chart"><h2>设备使用占比</h2><DonutChart :devices="devices" :totals="pie.totals"/><div class="cycle-budget" data-testid="cycle-budget" :title="'按账号官方额度与同步 Token 比例推算；来源：'+(cycleBudget.source||'等待样本')+'。所有模型的本周期估计，不随图表筛选改变。'"><span>推算本周期总量</span><strong>{{cycleBudget.total_tokens>0?'≈ '+compact(cycleBudget.total_tokens):'等待样本'}} <small v-if="cycleBudget.total_tokens>0">Token</small></strong></div><div class="pie-period"><SelectBox v-model="piePeriod" :options="[{value:'cycle',label:'本周期'},{value:'hour',label:'最近一小时'},{value:'day',label:'近24小时'},{value:'week',label:'近7天'},{value:'month',label:'近30天'},{value:'total',label:'累计使用'}]" label="设备占比时间范围"/></div></article><article class="chart-card trend-card" data-testid="trend-chart"><h2>{{trendName}}<span data-testid="trend-total">{{compact(trend.total)}} Token</span></h2><TrendChart :comparison-total="trendAll.total" :data="trend" :kind="kind" :color="trendColor"/><div class="chart-footnote">原始 Token 用量 · 悬停查看数值</div></article></div>
        <div class="devices-table"><div class="device-columns"><span>用户 / 设备</span><span>状态</span><span>本周期 Token</span><span>{{settings.quota_display==='fair'?'个人明细已用':settings.quota_display==='account'?'估算已用':'个人已用'}}</span></div><TransitionGroup name="device-shift" tag="div" class="device-list" :class="{reordering:dragging}"><button v-for="device in devices" :key="device.id" class="device-row" data-testid="device-row" :class="{selected:selectedDevice===device.id,dragging:dragging===device.id}" :data-device-id="device.id" @pointerdown="beginDeviceDrag($event,device)" @click="$event.detail===0&&openModal('user',device)" :style="{'--device-color':device.color}"><div class="device-name"><span class="avatar">{{device.label.slice(0,1)}}</span><span class="device-label"><strong>{{device.label}}<em v-if="device.local"> · 本机</em></strong><small :title="route(device)">{{route(device)}}</small></span><span class="connection-badge" data-testid="connection-state" :class="{connected:deviceConnected(device)}" :title="deviceConnected(device)?(device.local?'监控软件运行中 · 本机':'已收到对方监控软件心跳'):'未收到对方监控软件心跳（约 30 秒超时）'"><i></i>{{deviceConnected(device)?'在线':'离线'}}</span></div><span class="device-state" :class="{using:device.online&&device.active}" :title="deviceState(device)">{{deviceState(device)}}</span><span class="device-tokens">{{compact(device.tokens)}}</span><strong class="device-usage">{{usage(device)}}</strong></button><div v-if="!devices.length" key="empty" class="empty-state">{{ready?'等待当前账号的设备账本':'正在连接本机服务…'}}</div></TransitionGroup></div>
        <footer class="status-line"><span class="status-dot" :class="{error:view.error}"></span><span>{{view.error||view.status||'等待监测数据'}}{{view.blocked?' · Codex 已限制':''}}</span></footer>
      </section>
      <section v-else-if="page==='stats'" key="stats" class="page scroll-page"><div class="page-heading"><h1>总数据</h1><span>账号周期统计 · Token</span></div><p class="help-text stats-intro">记录每个正式周期的推算总量。模型与缓存比例会影响估计，变化不等同于官方额度调整。</p><article v-for="(cycle,index) in view.analytics?.cycles||[]" :key="cycle.id" class="panel cycle-record" data-testid="cycle-record"><div class="cycle-heading"><h2>第 {{view.analytics.cycles.length-index}} 周期 <span class="cycle-current" v-if="cycle.is_current">进行中</span></h2><span class="muted">{{timeLabel(cycle.started)}} → {{cycle.ended?timeLabel(cycle.ended):'现在'}}</span></div><div class="cycle-metrics"><div><span>推算周期总量</span><strong>{{cycle.total_tokens>0?'≈ '+compact(cycle.total_tokens):'等待样本'}}</strong></div><div><span>已统计 Token</span><strong>{{compact(cycle.sampled_tokens)}}</strong></div><div><span>官方额度已用</span><strong>{{cycle.used_percent?.toFixed(0)??'—'}}%</strong></div><div class="cycle-comparison"><span>较上周期（估计）</span><strong data-testid="cycle-change" :class="{decreased:cycle.change_percent<0,increased:cycle.change_percent>0}">{{cycle.change_percent==null?'—':(cycle.change_percent>0?'+':'')+cycle.change_percent.toFixed(1)+'%'}}</strong></div></div><div class="cycle-progress" role="progressbar" aria-label="本周期官方额度已用" :aria-valuenow="cycle.used_percent" aria-valuemin="0" aria-valuemax="100"><i :style="{transform:'scaleX('+Math.min(100,Math.max(0,cycle.used_percent||0))/100+')',background:officialUsageColor(cycle.used_percent)}"></i></div><div class="cycle-details"><span>{{cycle.source||'等待样本'}} · 样本 {{compact(cycle.sample_tokens)}} Token / {{cycle.sample_percent?.toFixed(1)??'0'}}% 额度</span></div><div class="cycle-models"><div v-for="m in cycle.models||[]" :key="m.model" class="cycle-model-card"><span :title="m.model">{{m.model}}</span><strong>{{compact(m.tokens)}} <small>Token</small></strong></div></div><div class="cycle-reference" data-testid="cycle-reference" :title="'采用此前最近最多 3 个已结束且可估算周期的推算总量均值。参考周期：'+(cycle.reference_starts||[]).map(ts=>timeLabel(ts)).join('、')"><template v-if="cycle.reference_total_tokens>0"><span>近 {{cycle.reference_count}} 个周期平均参考 <strong>≈ {{compact(cycle.reference_total_tokens)}} <small>Token</small></strong></span><span v-if="cycle.reduction_tokens!==null&&cycle.reduction_tokens!==undefined" class="reference-change" :class="{decreased:cycle.reduction_tokens>0}">{{cycle.reduction_tokens>0?'预计减少':cycle.reduction_tokens<0?'预计增加':'与参考持平'}}<template v-if="cycle.reduction_tokens!==0"> ≈ {{compact(Math.abs(cycle.reduction_tokens))}} Token · {{Math.abs(cycle.reduction_percent).toFixed(1)}}%</template></span><span v-else>等待本周期估算</span></template><span v-else>历史平均参考：等待已结束周期</span></div></article><div v-if="!view.analytics?.cycles?.length" class="panel empty-state">等待正式周期记录</div><p class="help-text">累计统计起点：{{view.analytics?.statistics_start?timeLabel(view.analytics.statistics_start,true):'首次记录'}}。官方刷新或确认重置后，新建下一周期；此前正式周期继续保留。</p></section>
      <section v-else-if="page==='accounts'" key="accounts" class="page scroll-page"><div class="page-heading"><h1>账号管理</h1><span>各账号独立计量</span></div><article class="panel"><h2>当前登录</h2><p class="identity">{{view.identity?.label||'尚未识别账号'}} <span class="tag">{{view.identity?.plan?.toUpperCase()||view.identity?.mode||'—'}}</span></p><p class="muted">{{accounts.some(a=>a.account===account)?'已纳入统计':'尚未纳入订阅统计'}}</p><div class="actions"><button @click="scanAccount" :disabled="busy">扫描当前登录</button><button class="primary" @click="command('account_add',{},'账号已添加')" :disabled="busy">添加当前账号到统计</button></div></article><article class="panel"><h2>已添加账号</h2><button v-for="item in accounts" :key="item.account" class="account-item" :class="{selected:selectedAccount===item.account}" @click="selectedAccount=item.account"><strong>{{item.label}}</strong><small>{{item.account.slice(0,16)}}</small></button><p v-if="!accounts.length" class="muted">暂无账号，先扫描并添加当前登录账号。</p><div class="actions"><button :disabled="!selectedAccount||busy" @click="showHistory">查看账号账本</button><button :disabled="!selectedAccount||busy" @click="confirm('停止统计账号','将停止采集和同步此账号，已有账本保留。','account_remove',{account:selectedAccount})">停止统计</button></div></article><article class="panel compact-panel"><h2>计量状态</h2><p>本周期已同步 {{compact(devices.reduce((n,d)=>n+d.tokens,0))}} Token</p><p class="muted">监测前基线 {{view.summary?.epoch?.baseline??'—'}}% · 未归属 {{(view.summary?.unassigned??0).toFixed(2)}}% · 待稳定分摊 {{(view.summary?.provisional??0).toFixed(2)}}%</p><p class="muted">本机历史补记 {{compact(view.recovery?.recovered_tokens)}} Token · 日志待核对 {{compact(view.recovery?.unresolved_tokens)}} Token</p></article><p class="help-text">添加的账号只保存在本机，配对不会自动添加账号。API 会话、未添加账号和无法确认归属的记录不计入订阅用量。</p></section>
      <section v-else-if="page==='sync'" key="sync" class="page scroll-page"><div class="page-heading"><h1>匹配与同步</h1><button @click="command('refresh')">刷新连接</button></div><article class="panel connection-summary"><div><span class="eyebrow">当前连接</span><h2>{{view.sync_caption||'等待连接'}}</h2></div><span class="muted">最近同步 {{lastReceipt?timeLabel(lastReceipt,true):'—'}}</span></article><div class="pair-grid"><article class="panel"><span class="step-number">01</span><h2>从这台设备发起</h2><p class="muted">将设备加入同一 Tailscale 网络，再发送匹配码。</p><button class="primary" @click="generatePair" :disabled="busy">生成匹配码</button></article><article class="panel"><span class="step-number">02</span><h2>加入已有设备组</h2><p class="muted">粘贴另一台设备的匹配码，配对后自动同步。</p><button @click="openModal('join')" :disabled="busy">输入匹配码</button></article></div><article class="panel"><h2>连接状态</h2><p>{{pairStatus}}</p><p class="muted">所属网络：{{snapshot.pairing?.tailnet||view.connection?.tailnet||'尚未确认'}}</p><p class="muted break-text">{{view.mesh||''}}</p><div v-for="(progress,id) in view.sync_progress||{}" :key="id" class="sync-peer"><strong>{{devices.find(d=>d.id===id)?.label||id.slice(0,12)}}</strong><span>{{({caught_up:'账本已同步',syncing:'同步中',waiting:'等待确认',stale:'等待重新确认',error:'同步异常'})[progress.state]||progress.state}}</span></div><div class="actions"><button @click="command('tailscale_login')" :disabled="busy">授权登录 Tailscale</button><button @click="confirm('切换 Tailscale 网络','将断开当前内嵌节点并重新授权。已配对设备仍需处于同一网络。','tailscale_switch')" :disabled="busy">换账号 / 切换网络</button></div></article><article class="panel"><h2>已配对设备</h2><div v-for="device in devices.filter(d=>!d.local)" :key="device.id" class="paired-device"><div><strong>{{device.label}}</strong><small>{{route(device)}}</small></div><button @click="confirm('移除设备', '将 '+device.label+' 从设备组移除，停止显示并同步该设备。', 'device_remove',{account,device:device.id})">移除</button></div><p v-if="devices.filter(d=>!d.local).length===0" class="muted">尚未配对其他设备</p></article><div class="actions"><button @click="advanced=!advanced">{{advanced?'收起高级连接设置':'高级连接设置'}}</button><button @click="exportDiagnostics">导出同步诊断</button></div><Transition name="expand"><article v-if="advanced" class="panel advanced-panel"><label>WSS 服务地址<input v-model="connectionForm.rendezvous_url" placeholder="留空保留当前地址"></label><label>服务访问密钥<input v-model="connectionForm.relay_token" type="password" autocomplete="off" placeholder="留空保留现有密钥"></label><label>STUN 地址<input v-model="connectionForm.stun_url" placeholder="STUN 地址"></label><label class="check-row"><input type="checkbox" v-model="connectionForm.force_relay">仅使用加密中转</label><button @click="saveConnection" :disabled="busy">保存连接设置</button></article></Transition><p class="help-text">内嵌 Tailscale，无需另装客户端。优先直连，失败时通过 DERP 中转。连接在线与账本同步分别显示，双方需添加同一 Codex 账号。</p></section>
      <section v-else-if="page==='settings'" key="settings" class="page scroll-page"><div class="page-heading"><h1>设置</h1><span>用量 · 限额 · 设备</span></div><article class="panel"><h2>用户设置</h2><div class="settings-users"><button v-for="device in devices" :key="device.id" class="settings-user" data-testid="settings-user" @click="openModal('user',device)" :style="{'--device-color':device.color}"><span class="avatar">{{device.label.slice(0,1)}}</span><span><strong>{{device.label}}</strong><small>{{device.local?'本机设备':route(device)}}</small></span><svg viewBox="0 0 16 16"><path d="m6 4 4 4-4 4"/></svg></button><p v-if="!devices.length" class="muted">等待当前账号的设备数据</p></div></article><article class="panel"><div class="settings-line"><div><h2>设备配额</h2><p class="muted">本机在当前账号内的配额 {{localDevice?.cap??settings.quota??'—'}}%</p></div><button @click="openModal('cap')" :disabled="!account">调整配额</button></div><div class="settings-line"><span>估算已用的显示基准</span><SelectBox :model-value="settings.quota_display||'personal'" @update:model-value="value=>command('settings_save',{settings:{quota_display:value}})" :options="[{value:'account',label:'账号总额度'},{value:'personal',label:'个人额度 = 100%'},{value:'fair',label:'补偿显示模式'}]" label="额度显示基准"/></div></article><article class="panel"><div class="settings-line"><div><h2>Codex 自动限额</h2><p class="muted">{{settings.auto_block?'达到设备配额后自动限制':'当前仅统计和提醒'}}</p></div><button class="toggle" :class="{on:settings.auto_block}" role="switch" :aria-checked="!!settings.auto_block" aria-label="Codex 自动限额" @click="toggleLimit"><i></i></button></div><p v-if="view.blocked" class="notice-inline">Codex 已受限，解除后才能继续使用。</p><button @click="command('restore',{},'已请求解除限制')">解除限制 / 恢复 Codex</button><p class="help-text">限额通过暂停选定 Codex 进程和出站防火墙实现，需要管理员权限；无法隔离同一 EXE 内并行运行的多账号会话。</p></article><article class="panel"><div class="settings-line"><div><h2>软件更新 <span class="muted">v{{snapshot.version}}</span></h2><p class="muted">{{updateText}}</p></div><button @click="command('update_check')" :disabled="busy">检测更新</button></div><label class="check-row"><input type="checkbox" :checked="settings.auto_update" @change="command('settings_save',{settings:{auto_update:$event.target.checked}})">自动检测并安装正式版更新</label><button v-if="snapshot.update?.ready||snapshot.update?.downloaded||snapshot.update?.stage==='ready'" class="primary" @click="confirm('安装更新','安装经过签名验证的更新并重启配额管家，账号与账本保留。','update_install')">安装更新并重启</button></article><article class="panel" @input="formDirty=true"><h2>监测设置</h2><div class="form-grid"><label>本机设备名称<input v-model="settingsForm.name" maxlength="80"></label><label>新账号默认配额（%）<input v-model="settingsForm.quota" type="number" min="0.01" max="100" step="0.1"></label><label class="span-two">Codex 数据目录<input v-model="settingsForm.codex_home"></label><label>本机权重校准系数<input v-model="settingsForm.multiplier" type="number" min="0.05" max="20" step="0.05"></label><label>额度查询间隔（秒）<input v-model="settingsForm.interval" type="number" min="15" max="300"></label></div><label class="check-row"><input v-model="settingsForm.autostart" type="checkbox">Windows 登录后自动启动</label><h3>限制目标程序</h3><div class="program-list"><button v-for="(path,index) in settingsForm.program_paths||[]" :key="path" :class="{selected:selectedPath===index}" @click="selectedPath=index">{{path}}</button><p v-if="!settingsForm.program_paths?.length" class="muted">尚未选择 Codex EXE</p></div><div class="actions"><button @click="discover" :disabled="busy">自动查找</button><button @click="browseProgram">添加 EXE</button><button @click="removePath" :disabled="selectedPath<0">移除选中</button><button class="primary push-right" @click="saveSettings" :disabled="busy">保存监测设置</button></div></article><p class="help-text">点击 X 隐藏到托盘，监测会继续运行。彻底退出请使用托盘菜单。权重系数只影响新采集记录，不改变图表中的原始 Token。</p></section>
      <section v-else key="help" class="page scroll-page"><div class="page-heading"><h1>计量说明</h1><span>Codex 配额管家</span></div><article class="panel prose"><h2>Token 与额度</h2><p>图表和设备 Token 列显示日志记录的原始 Token。官方周额度来自账号快照；设备“估算已用”按模型权重和周期记录分摊，两者单位不同。</p><p>设备占比默认“本周期”，与下面的设备表使用同一份周期快照。“历史累计”包含以前的周期，不能与本周期 Token 直接比较。</p><h2>自动同步</h2><p>运行期间持续采集、发送并接收账本记录；重连后补齐缺失记录。“已同步”需要对端账本确认，下面的小字是最近一次确认时间。</p><p>“Codex 使用中”根据最近日志活动判断，连接在线不代表正在使用。未确认账号归属的记录不会强行计入当前账号。</p><h2>时间与模型</h2><p>一小时视图每 2 分钟一格，共 30 格。一天、一周、一个月分别查看最近 24 小时、7 天、30 天。模型与用户筛选仅影响所选范围；用户筛选控制趋势图，设备占比始终保留同账号设备比较。</p><h2>个人额度</h2><p>选择“个人额度 = 100%”后，会将分配给该设备的额度作为 100%。例如账号配额为 50%、账号口径已用 20%，个人已用显示 40%。</p><h2>本机数据</h2><p>账号凭据、设备组密钥和账本保存在本机。界面仅接收显示所需数据，所有计量、校准与同步均由本机服务处理。</p><div class="actions"><button @click="exportDiagnostics">导出诊断</button></div></article></section>
    </Transition></main>
    <span class="resize-grip" @mousedown.left.prevent="host('resize')" title="调整窗口大小"></span>
    <Transition name="toast"><div v-if="toast" class="toast" role="status">{{toast}}</div></Transition>
    <Teleport to="body"><Transition name="modal"><div v-if="modal" class="modal-backdrop" @mousedown.self="modal=null"><section class="modal" :class="{'wide-modal':modal.type==='history'}" role="dialog" aria-modal="true"><button class="modal-close" @click="modal=null" aria-label="关闭对话框">×</button>
      <template v-if="modal.type==='confirm'"><h2>{{modal.title}}</h2><p>{{modal.body}}</p><div class="modal-actions"><button @click="modal=null">取消</button><button class="primary" @click="confirmAction" :disabled="busy">{{busy?'处理中…':'确认'}}</button></div></template>
      <template v-else-if="modal.type==='user'"><h2>用户设置</h2><p class="muted">{{selected?.name}}</p><label class="user-note-label">设备备注<input v-model="textInput" maxlength="40" :placeholder="selected?.name||'输入容易辨认的名称'" @keydown.enter="saveUserSettings"></label><h3>用户颜色</h3><div class="palette"><button v-for="(color,index) in COLORS" :key="color" :style="{'--swatch':color}" :class="{chosen:userColor===color}" @click="userColor=color" :disabled="busy"><span>{{userColor===color?'✓':''}}</span>{{COLOR_NAMES[index]}}</button></div><div class="modal-actions"><button @click="modal=null">取消</button><button class="primary" @click="saveUserSettings" :disabled="busy">保存用户设置</button></div></template>
      <template v-else-if="modal.type==='cap'"><h2>调整本机配额</h2><p class="muted">当前账号 {{view.identity?.label}}</p><label class="cap-input"><input v-model="capInput" type="number" min="0.01" max="100" step="0.1" @keydown.enter="saveCap"><span>%</span></label><div class="modal-actions"><button @click="modal=null">取消</button><button class="primary" @click="saveCap" :disabled="busy">保存配额</button></div></template>
      <template v-else-if="modal.type==='pair'"><h2>设备匹配码</h2><p class="muted">在另一台设备选择“输入匹配码”。</p><div v-if="pairPending" class="waiting"><span class="spinner"></span>正在准备设备连接…</div><textarea v-else class="pair-code" :value="pairCode" readonly rows="5"></textarea><div class="modal-actions"><button @click="command('tailscale_login')">授权 Tailscale</button><button class="primary" @click="copyPair" :disabled="!pairCode">复制匹配码</button></div></template>
      <template v-else-if="modal.type==='join'"><h2>加入设备组</h2><p class="muted">粘贴另一台设备提供的完整匹配码。</p><textarea v-model="joinCode" rows="5" placeholder="设备匹配码"></textarea><div class="modal-actions"><button @click="modal=null">取消</button><button class="primary" @click="joinPair" :disabled="!joinCode.trim()||busy">匹配并同步</button></div></template>
      <template v-else-if="modal.type==='history'"><h2>账号账本</h2><p class="muted">该账号最近保存的快照，不查询其他账号凭据。</p><div class="history-summary"><strong>周额度 {{history.summary?.epoch?.used??'—'}}%</strong><span>刷新 {{timeLabel(history.summary?.epoch?.reset_at)}}</span></div><div v-for="device in history.summary?.devices||[]" :key="device.id" class="history-device"><span>{{device.name}}</span><span>{{compact(device.tokens)}} Token</span><span>{{device.estimated?.toFixed(2)}}%</span></div><div class="segment history-period"><button v-for="[id,label] in [['day','按日'],['week','按自然周'],['month','按月']]" :key="id" :class="{active:historyPeriod===id}" @click="historyPeriod=id">{{label}}</button></div><div class="history-list"><div v-for="[date,value] in historyRows" :key="date"><span>{{date}}</span><span>{{compact(value)}} Token</span></div><p v-if="!historyRows.length" class="muted">暂无历史记录</p></div></template>
      <template v-else-if="modal.type==='quit'"><h2>退出配额管家</h2><p>Codex 当前受限。退出时将按程序退出流程处理限制。</p><div class="modal-actions"><button @click="modal=null">取消</button><button class="primary" @click="host('quit')">退出程序</button></div></template>
    </section></div></Transition></Teleport>
  </div>`
});
app.mount('#app');
