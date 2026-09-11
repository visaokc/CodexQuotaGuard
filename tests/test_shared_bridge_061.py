import copy
import hashlib
from types import SimpleNamespace

import pytest

from test_web_controller import controller
from test_shared_billing import setup_group, A, B, add_event, close_samples
from quota_guard.shared_policy import load_rules
from quota_guard.shared_view import shared_usage, shared_overview, latest_active_model
from quota_guard.ledger import Ledger


@pytest.mark.parametrize('period,count,step', [('hour',26*60,60),('six_hours',360,300),('twelve_hours',432,300),('day',32*24,3600)])
def test_shared_history_returns_chart_schema_not_account_history(controller, tmp_path, period, count, step):
    db, journals, _ = setup_group(tmp_path/'shared')
    add_event(journals)
    close_samples(journals)
    controller._config.update(shared_group_enabled=True, shared_billing_v1=True)
    scope='group:'+hashlib.sha256(controller._config['group_secret'].encode()).hexdigest()[:20]
    view=dict(account_summaries=[dict(account=A,label='账号1'),dict(account=B,label='账号2')])
    controller._engine=SimpleNamespace(group_db=db, snapshot=lambda:copy.deepcopy(view),ledger=Ledger(db))
    result=controller.command('chart_history',dict(account=scope,period=period,end=400))
    assert result['ok'], result
    data=result['data']
    assert data['account']==scope and 'history' not in data and 'summary' not in data
    key='hour_curve' if period=='hour' else period
    assert data['windows'][key]['count']==count
    assert data['windows'][key]['step']==step
    assert sum(r['tokens'] for r in data['windows'][key]['rows'])==1100


def test_shared_compensation_and_display_are_locked(controller):
    controller._config['shared_billing_v1']=True
    controller._engine=SimpleNamespace(snapshot=lambda:dict(shared_group=dict(can_manage=True,revision=2)))
    result=controller.command('group_rule',dict(kind='compensation',revision=2,enabled=False))
    assert not result['ok'] and '固定开启' in result['error']
    result=controller.command('settings_save',dict(settings=dict(quota_display='personal')))
    assert not result['ok'] and '补偿模式' in result['error']


def test_daily_average_is_normalized_once_and_preserves_unassigned_official_amount(tmp_path):
    db,journals,_=setup_group(tmp_path,used_a=20)
    add_event(journals)
    journals['one'].append(A,'quota',dict(account=A,used=30,reset_at=800,at=210),210)
    close_samples(journals)
    labels={A:'账号1',B:'账号2'}
    view=shared_usage(db,'group:test',labels,400,rules=load_rules(db,[A,B],400))
    overview=shared_overview(db,'group:test',labels,{},'one',400,view)
    daily=overview['daily_usage']
    assert daily['total']==pytest.approx(30/((400-100)/86400)/2)
    assert daily['unassigned']==pytest.approx(daily['total']*2/3)
    assert sum(p['quota'] for p in daily['people'])+daily['unassigned']==pytest.approx(daily['total'])


def test_active_model_requires_running_request_and_recent_real_token_increment(tmp_path):
    db,journals,_=setup_group(tmp_path)
    add_event(journals,model='gpt-6-astra',at=200)
    add_event(journals,model='gpt-5.6-sol',at=250)
    people={'one':dict(current_account=A)}
    devices=[dict(id='one',online=True,active=1)]
    assert latest_active_model(db,people,devices,300)=='gpt-5.6-sol'
    devices[0]['active']=0
    assert latest_active_model(db,people,devices,300) is None
    devices[0]['active']=1
    assert latest_active_model(db,people,devices,500) is None
    devices[0]['online']=False
    assert latest_active_model(db,people,devices,300) is None
    devices[0]['online']=True
    people['one']['current_account']=B
    assert latest_active_model(db,people,devices,300) is None


def test_natural_week_average_uses_official_clock_not_recent_first_sync(tmp_path):
    db,journals,_=setup_group(tmp_path,used_a=20)
    labels={A:'账号1',B:'账号2'}
    view=shared_usage(db,'group:test',labels,400,rules=load_rules(db,[A,B],400))
    for cycle in view['cycles']:
        cycle['reset_type']='自然重置'
    result=shared_overview(db,'group:test',labels,{},'one',400,view)
    assert result['daily_usage']['total']==pytest.approx(20/((400-(800-7*86400))/86400)/2)
