import {fixture} from './fixture.mjs';
import {sharedFixture} from './shared-fixture.mjs';

export function billingFixture(){
  const data=sharedFixture(),view=data.view,at=view.analytics.at;
  data.version='0.6.0';data.settings.quota_display='personal';
  const ids=['fixture-local','fixture-peer','fixture-third'];
  view.shared_group={...view.shared_group,stage:'billing',state:'active',reason:'',revision:1,admin:ids[0],can_manage:true,
    devices:ids.map((id,index)=>({id,name:['橙猫猫','A','第三人'][index]})),bindings:ids.map((device,index)=>({device,person:'person'+(index+1),since:0})),
    billing_start_note:'原两人账号本周期免结转。补偿默认关闭。'};
  view.account_summaries.forEach((a,index)=>{a.label='账号'+(index+1);a.epoch.started=a.epoch.reset_at-604800;});
  view.analytics.cycle_pair.cycles.forEach((cycle,index)=>cycle.label='账号'+(index+1));
  data.accounts=view.account_summaries.map(({account,label})=>({account,label}));
  view.summary.compensation_enabled=false;view.summary.allocation='shared_official_v1';view.summary.billing_status='active';
  for(const [index,person] of view.summary.devices.entries())Object.assign(person,{id:'person'+(index+1),device_ids:[ids[index]],local:index===0,
    cap:200/3,fair_base_cap:200/3,active_model:['gpt-6-astra','gpt-5.6-sol',null][index],avatar:'person'+(index+1)+'.jpg',joined:true,estimated:[75,23,2][index],available:[0,50,50][index],debt:0,pending_debt:0,confirmed_debt:0,
    by_account:{'fixture-account':[0,25,25][index],'fixture-account-b':[0,25,25][index]}});
  for(const window of Object.values(view.analytics.windows)){
    window.rows.forEach(row=>{const index=ids.indexOf(row.device);row.device='person'+(index+1);row.account=index===2?'fixture-account-b':'fixture-account';});
    window.quota_ready=true;
    window.quota_rows=window.rows.map(row=>({...row,quota:row.tokens/1e9*100,cache_quota:row.tokens/1e9*20}));
  }
  view.analytics.windows.cycle.quota_rows=[75,23,2].map((quota,index)=>({account:index===2?'fixture-account-b':'fixture-account',device:'person'+(index+1),model:'gpt-5.5',bucket:0,quota,cache_quota:quota*.2}));
  view.analytics.cycles=fixture().view.analytics.cycles.map((cycle,index)=>({...cycle,account:view.account_summaries[index].account,account_label:'账号'+(index+1)}));
  view.billing={status:'active',reason:'',active_since:at-1000,entries:[{kind:'initial',account:'fixture-account',at:at-1000,amount:100},{kind:'exchange',account:'fixture-account',other_account:'fixture-account-b',person:'person1',at:at-100,amount:20}]};
  view.daily_usage={total:8.5,people:[{id:'person1',quota:5},{id:'person2',quota:2.5},{id:'person3',quota:.5}],unassigned:.5,basis:'各账号已确认消耗分别除以该周期已过去的天数，再按两账号总额度换算。'};
  view.analytics.models=[...new Set([...view.analytics.models,'gpt-6-astra','gpt-5.6-sol','gpt-5.6-terra','gpt-5.6-luna'])];
  view.status='共享额度 · 官方增量分摊';
  return data;
}
