"""A release cutoff follows raw usage time, even when official confirmation is late."""
import copy

import pytest

from test_shared_billing import A, B, add_event, close_samples, setup_group
from test_shared_costs import declare
from quota_guard.shared_costs import apply, token_shares, uncovered
from quota_guard.shared_policy import load_rules
from quota_guard.shared_quota import accounting, attribution
from quota_guard.shared_view import shared_usage


def test_delayed_official_segment_crosses_cutoff_without_sharing_later_usage(tmp_path):
    db, journals, _ = setup_group(tmp_path)
    raw = [add_event(journals, account=B, device=device, at=at)
           for device, at in [('one',110), ('one',150), ('one',200),
                              ('one',201), ('two',202), ('one',300)]]
    declare(db, journals, since=120, through=200)
    # The release rule is already present when the official snapshot arrives.
    journals['one'].append(B, 'quota', dict(account=B,used=12,reset_at=850,at=350),350)
    close_samples(journals, at=500)
    rules = load_rules(db, [A,B], 500)
    data = attribution(db, rules, 500)
    streams = [s for s in data['streams'] if s['account']==B]
    assert [(s['start'],s['end']) for s in streams] == [(100,350)]
    shared = [e for e in data['events'] if e.get('shared_cost')]
    assert len(shared) == 6
    assert {e['ts'] for e in shared} == {150,200}
    assert {e['confirmed_at'] for e in shared} == {350}
    for event in (raw[0],raw[3],raw[4],raw[5]):
        projected = [e for e in data['events'] if e['id']==event['id']]
        assert len(projected)==1 and not projected[0].get('shared_cost')
        assert projected[0]['quota']==pytest.approx(2)
    assert sum(e['units'] for e in shared) == 4*10**9
    assert sum(e['units'] for e in data['events']) == 12*10**9
    view = shared_usage(db,'group:test',{A:'a',B:'b'},500,rules=rules)
    rows = view['windows']['cycle']['rows']
    assert sum(r['tokens'] for r in rows)==6600
    assert sum(r.get('shared_tokens',0) for r in rows)==2200
    assert [sum(r['tokens'] for r in rows if r['device']==p)
            for p in ('person1','person2','person3')] == [4034,1834,732]
    book = accounting(rules,data,500)
    assert book['status']=='active'
    assert [v['fair_usage'] for v in book['people'].values()] == pytest.approx([22/3,10/3,4/3])
    with db.connect() as connection:
        assert connection.execute('SELECT SUM(tokens) FROM events').fetchone()[0]==6600
        assert connection.execute('SELECT COUNT(*) FROM events').fetchone()[0]==6


def test_every_new_postrelease_segment_belongs_only_to_actual_user(tmp_path):
    db,journals,_ = setup_group(tmp_path)
    add_event(journals,account=B,at=199)
    declare(db,journals,through=200)
    journals['one'].append(B,'quota',dict(account=B,used=3,reset_at=850,at=250),250)
    close_samples(journals,at=400)
    rules=load_rules(db,[A,B],400)
    before=accounting(rules,attribution(db,rules,400),400)
    assert [v['fair_usage'] for v in before['people'].values()] == pytest.approx([1,1,1])
    add_event(journals,account=B,device='two',at=410)
    journals['one'].append(B,'quota',dict(account=B,used=6,reset_at=850,at=430),430)
    close_samples(journals,at=550)
    after=accounting(rules,attribution(db,rules,550),550)
    assert [v['fair_usage'] for v in after['people'].values()] == pytest.approx([1,4,1])
    view=shared_usage(db,'group:test',{A:'a',B:'b'},550,rules=rules)
    assert sum(r.get('shared_tokens',0) for r in view['windows']['cycle']['rows'])==1100
    assert sum(r['tokens'] for r in view['windows']['cycle']['rows'])==2200


def test_late_arriving_pre_cutoff_log_keeps_original_time_for_both_allocations(tmp_path):
    db,journals,_=setup_group(tmp_path)
    declare(db,journals,through=200)
    journals['one'].append(B,'quota',dict(account=B,used=6,reset_at=850,at=300),300)
    rules=load_rules(db,[A,B],350)
    assert accounting(rules,attribution(db,rules,350),350)['status']=='syncing'
    # Arrival is late; event.ts remains the usage time rather than arrival time.
    early=add_event(journals,account=B,at=199)
    late=add_event(journals,account=B,at=201)
    close_samples(journals,at=500)
    data=attribution(db,rules,500)
    assert {e['id'].rsplit(':',1)[0] for e in data['events'] if e.get('shared_cost')} == {early['id']}
    assert [e['device'] for e in data['events'] if e['id']==late['id']] == ['person1']
    assert [v['fair_usage'] for v in accounting(rules,data,500)['people'].values()] == pytest.approx([4,1,1])
    view=shared_usage(db,'group:test',{A:'a',B:'b'},500,rules=rules)
    assert sum(r.get('shared_tokens',0) for r in view['windows']['cycle']['rows'])==1100


@pytest.mark.parametrize('at,shared', [(120,False),(120.000001,True),(200,True),(200.000001,False)])
def test_token_and_quota_use_identical_open_start_closed_end(at,shared):
    policy=dict(shared_costs=[dict(account=B,person='person1',since=120,through=200)])
    event=dict(account=B,ts=at,id='request',tokens=1100,weight=1,input_tokens=1000,
               cached_input_tokens=900,output_tokens=100,reasoning_output_tokens=50)
    tokens=token_shares(event,'person1',policy)
    quota=dict(event,device='person1',quota=3,units=3*10**9,cache_quota=1,confirmed_at=500)
    data=dict(events=[quota],streams=[dict(events=[quota])])
    apply(data,policy)
    assert (len(tokens)==3) is shared
    assert (len(data['events'])==3) is shared
    assert sum(p['tokens'] for _,p in tokens)==1100
    assert sum(e['units'] for e in data['events'])==3*10**9
    once=copy.deepcopy(data)
    apply(data,policy)
    assert data==once
    assert uncovered(policy['shared_costs'],B,'person1',120,200)==[]
