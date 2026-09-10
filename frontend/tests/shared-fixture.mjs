import {fixture} from './fixture.mjs';

export function sharedFixture(){
  const data=fixture(),view=data.view,third='fixture-third',group='group:fixture-shared-group';
  data.version='0.5.0';
  view.display_account=group;
  view.shared_group={enabled:true,id:'fixture-shared-group',stage:'preparing',members:[
    {id:'fixture-local',name:'橙猫猫',local:true,online:true,accounts:['fixture-account']},
    {id:'fixture-peer',name:'A',local:false,online:true,accounts:['fixture-account']},
    {id:third,name:'第三人',local:false,online:true,accounts:['fixture-account-b']},
  ]};
  view.account_summaries=[
    {account:'fixture-account',label:'账号 A',epoch:structuredClone(view.summary.epoch)},
    {account:'fixture-account-b',label:'账号 B',epoch:{...view.summary.epoch,used:23,reset_at:view.summary.epoch.reset_at-86400}},
  ];
  data.accounts=view.account_summaries.map(({account,label})=>({account,label}));
  view.summary.devices=view.summary.devices.filter(item=>!item.removed);
  view.summary.devices[1].name='A';
  view.summary.devices.push({id:third,name:'第三人',tokens:37.09e6,online:true,active:1,cap:100});
  for(const item of view.summary.devices){item.estimated=null;item.settled=null;}
  view.summary.epoch=null;
  view.analytics.account=group;
  view.analytics.cycle_pair={complete:true,overlap_seconds:5*86400,cycles:view.account_summaries.map(item=>({account:item.account,label:item.label,started:item.epoch.started,ended:null,reset_at:item.epoch.reset_at,usage_until:view.analytics.at}))};
  for(const window of Object.values(view.analytics.windows)){
    window.rows=window.rows.filter(row=>row.device!=='removed-peer');
    window.rows.push(...window.rows.filter(row=>row.device==='fixture-peer').map(row=>({...row,device:third,tokens:Math.round(row.tokens/4)})));
    for(const row of window.rows){row.input_tokens=Math.floor(row.tokens*.95);row.cache_tokens=Math.floor(row.tokens*.85);row.output_tokens=row.tokens-row.input_tokens;row.detail_missing=0;row.detail_count=1;row.event_count=1;}
    window.quota_rows=[];window.quota_ready=false;
  }
  view.analytics.cycles=[];
  view.peers[third]={route:'Tailscale · 直连'};
  view.sync_progress[third]={state:'caught_up'};
  view.status='共享组准备中 · 两账号 Token 已合计 · 计费待启用';
  return data;
}
