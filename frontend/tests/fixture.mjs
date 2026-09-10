// Explicit isolated UI fixture. This file is not imported by the production bundle.
export function fixture(){
  const at=1789027560,account='fixture-account',deviceA='fixture-local',deviceB='fixture-peer';
  const windows={};
  for(const [name,seconds,count] of [['cycle',604800,1],['today',86400,1],['pie_hour',3600,1],['pie_six_hours',21600,1],['hour',3600,30],['hour_curve',3600,60],['day',86400,24],['week',604800,7],['month',2592000,30],['total',at,1]]){
    const rows=[];
    for(let i=0;i<count;i++)for(const [device,scale] of [[deviceA,1],[deviceB,1.8]]){
      rows.push({device,model:'gpt-5.5',bucket:i,tokens:Math.round((Math.sin(i*.55)**2*.85+.05)*12e6*scale),weight:i*3e6*scale,unknown:0});
      rows.push({device,model:'gpt-5.3-codex',bucket:i,tokens:Math.round((Math.cos(i*.8)**2*.2)*3e6*scale),weight:i*2e6*scale,unknown:0});
    }
    rows.push({device:'removed-peer',model:'gpt-5.5',bucket:0,tokens:1e12,weight:1e12,unknown:0});
    const step=seconds/count,start=['hour','hour_curve','day','week','month'].includes(name)?Math.floor(at/step)*step-(count-1)*step:at-seconds;
    windows[name]={count,start,step,rows,quota_ready:true,quota_rows:rows.map(r=>({device:r.device,model:r.model,bucket:r.bucket,quota:r.tokens/450e6*100}))};
    if(name==='cycle')windows[name].quota_rows=[{device:deviceA,model:'gpt-5.5',bucket:0,quota:81.27/450*100},{device:deviceB,model:'gpt-5.5',bucket:0,quota:148.36/450*100}];
  }
  return {version:'0.4.2',view:{identity:{account,label:'隔离测试账号',plan:'pro',mode:'account'},status:'监测中 · 所有设备用量均为估算',summary:{token_budget:{total_tokens:450e6,source:'同步样本'},epoch:{used:53,baseline:0,started:at-2*86400,observed_at:at,reset_at:at+400000},devices:[{id:deviceA,name:'橙猫猫',tokens:81.27e6,estimated:18.76,cap:50,online:true,active:1},{id:deviceB,name:'DESKTOP-N41609F',tokens:148.36e6,estimated:34.24,cap:50,online:true,active:0},{id:'removed-peer',name:'USER-20241018IW',tokens:44e3,estimated:0,cap:33,online:false,removed:true}],unassigned:0,provisional:0},analytics:{account,at,statistics_start:at-800000,cycles:[{id:'second',started:at-200000,ended:null,reset_at:at+400000,used_percent:53,sampled_tokens:229.63e6,total_tokens:450e6,source:'同步样本',sample_tokens:90e6,sample_percent:20,is_current:true,change_percent:-10,models:[{model:'gpt-6-astra',tokens:210e6},{model:'gpt-5.6-sol',tokens:9e6},{model:'gpt-5.6-terra',tokens:6e6},{model:'gpt-5.6-luna',tokens:4.63e6}]},{id:'first',started:at-800000,ended:at-200000,reset_at:at-200000,used_percent:90,sampled_tokens:450e6,total_tokens:500e6,source:'同步样本',sample_tokens:450e6,sample_percent:90,is_current:false,change_percent:null,models:[{model:'gpt-5.5',tokens:450e6}]}],models:['gpt-5.5','gpt-5.3-codex'],windows},sync_caption:'已同步',sync_confirmed_at:at,sync_progress:{[deviceB]:{state:'caught_up'}},sync_receipts:{[deviceB]:at},peers:{[deviceB]:{route:'Tailscale · 直连'}},connection:{state:'Running',ready:true,tailnet:'test-network'},auto_block:false},settings:{device_id:deviceA,name:'橙猫猫',quota:50,quota_display:'account',device_notes:{},device_colors:{},autostart:true,auto_update:true,auto_block:false,codex_home:'C:/isolated-test/.codex',interval:30,multiplier:1,program_paths:['C:/isolated-test/codex.exe'],force_relay:false},accounts:[{account,label:'隔离测试账号',cap:50}],pairing:{ready:true,state:'Running',tailnet:'test-network'},update:{status:'隔离测试 · 不联网更新',ready:false},notices:[]};
}
