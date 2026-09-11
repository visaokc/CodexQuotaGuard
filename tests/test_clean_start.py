import pytest

from test_shared_billing import A, B, D, setup_group, add_event, close_samples
from quota_guard.shared_policy import load_rules, publish_change
from quota_guard.shared_quota import accounting, attribution
from quota_guard.shared_view import shared_usage


def setup_clean(tmp_path):
    db, journals, policy = setup_group(tmp_path, devices=D[:2], used_a=92)
    # This is the first official observation of account2's existing 2%.
    journals['one'].append(B, 'quota', dict(account=B, used=2, reset_at=850, at=120), 120)
    rules = load_rules(db, [A, B], 130)
    clean = dict(account=A, started=100, reset_at=800,
                 baseline=dict(account=B, started=100, reset_at=850, at=120, used=2, person='person3'))
    policy = publish_change(journals['one'], rules, 'one', 'one', 50,
        dict(compensation=True, rules_locked=True, clean_start=clean), 130)
    return db, journals, policy


def book(db, now=400):
    rules = load_rules(db, [A, B], now)
    attributed = attribution(db, rules, now)
    return accounting(rules, attributed, now), attributed


def test_clean_start_waits_for_official_exhaustion_and_keeps_existing_confirmed_quota(tmp_path):
    db, journals, _ = setup_clean(tmp_path)
    waiting, attributed = book(db)
    assert waiting['status'] == 'armed'
    assert waiting['compensation_enabled']
    assert attributed['events'][-1]['quota'] == 2
    journals['one'].append(A, 'quota', dict(account=A, used=100, reset_at=800, at=200), 200)
    current, _ = book(db)
    assert current['status'] == 'active'
    assert current['active_since'] == 200
    assert current['anchors'][A]['remaining'] == 0
    assert [p['available'] for p in current['people'].values()] == pytest.approx([100/3, 100/3, 100/3-2])
    assert [p['fair_usage'] for p in current['people'].values()] == pytest.approx([0, 0, 2])
    assert [p['available']/p['available_cap']*100 for p in current['people'].values()] == pytest.approx([100,100,94])
    assert all(p['debt'] == 0 for p in current['people'].values())
    assert current['entries'][0]['balances'] == dict(person1=50., person2=50., person3=0.)


def test_no_artificial_account1_refill_and_each_account_resets_independently(tmp_path):
    db, journals, _ = setup_clean(tmp_path)
    journals['one'].append(A, 'quota', dict(account=A, used=100, reset_at=800, at=200), 200)
    assert sum(p['available'] for p in book(db)[0]['people'].values()) == pytest.approx(98)
    journals['one'].append(A, 'quota', dict(account=A, used=0, reset_at=1500, at=801), 801)
    journals['one'].append(A, 'quota', dict(account=A, used=0, reset_at=1500, at=820), 820)
    refreshed, _ = book(db, 830)
    assert refreshed['status'] == 'active'
    assert sum(p['available'] for p in refreshed['people'].values()) == pytest.approx(198)
    assert [p['fair_usage'] for p in refreshed['people'].values()] == pytest.approx([0, 0, 2])
    assert [p['available']/p['available_cap']*100 for p in refreshed['people'].values()] == pytest.approx([100,100,97])
    journals['one'].append(B, 'quota', dict(account=B, used=0, reset_at=1550, at=851), 851)
    journals['one'].append(B, 'quota', dict(account=B, used=0, reset_at=1550, at=870), 870)
    refreshed, _ = book(db, 880)
    assert sum(p['available'] for p in refreshed['people'].values()) == pytest.approx(200)
    assert all(p['fair_usage'] == 0 for p in refreshed['people'].values())


def test_third_member_logs_replace_baseline_attribution_without_double_charge(tmp_path):
    db, journals, _ = setup_clean(tmp_path)
    journals['one'].append(A, 'quota', dict(account=A, used=100, reset_at=800, at=200), 200)
    before, _ = book(db)
    rules = load_rules(db, [A, B], 300)
    journals['three'].append(B, 'profile', dict(device='three', name='third', cap=50,
        member_claim=dict(device='three', person='person3', genesis=rules['genesis'])), 300)
    add_event(journals, account=B, device='three', at=110)
    add_event(journals, account=B, device='one', at=210)
    journals['one'].append(B, 'quota', dict(account=B, used=5, reset_at=850, at=220), 220)
    close_samples(journals)
    after, attributed = book(db)
    assert after['status'] == 'active'
    assert after['people']['person3']['fair_usage'] == 2
    assert after['people']['person3']['available'] == before['people']['person3']['available']
    assert after['people']['person1']['fair_usage'] == 3
    assert sum(row['quota'] for row in attributed['events'] if row['account'] == B) == 5
    view = shared_usage(db, 'group:test', {A:'账号1',B:'账号2'},400,rules=load_rules(db,[A,B],400))
    assert sum(row['tokens'] for row in view['windows']['cycle']['rows']) == 2200
    assert sum(row['quota'] for row in view['windows']['cycle']['quota_rows']) == 5
    assert all(row['account'] != A for row in view['windows']['cycle']['rows'])


def test_clean_start_declaration_and_enabled_lock_are_immutable(tmp_path):
    db, journals, policy = setup_clean(tmp_path)
    rules=load_rules(db,[A,B],400)
    with pytest.raises(ValueError, match='锁定'):
        publish_change(journals['one'],rules,'one','one',50,dict(compensation=False),400)


def test_initial_unavailable_inventory_is_not_reported_as_consumption(tmp_path):
    db, journals, _ = setup_group(tmp_path, used_a=70)
    value, _ = book(db)
    assert all(row['fair_usage'] == 0 for row in value['people'].values())


def test_immediate_start_excludes_account1_entire_current_cycle_but_accepts_next_reset(tmp_path):
    db,journals,policy=setup_group(tmp_path, devices=D[:2], used_a=92)
    journals['one'].append(B,'quota',dict(account=B,used=2,reset_at=850,at=120),120)
    clean=dict(trigger='immediate',at=130,account=A,started=100,reset_at=800,
        baseline=dict(account=B,started=100,reset_at=850,at=120,used=2,person='person3'))
    publish_change(journals['one'],load_rules(db,[A,B],130),'one','one',50,
        dict(compensation=True,rules_locked=True,clean_start=clean),130)
    current,_=book(db)
    assert current['status']=='active'
    assert [p['available']/p['available_cap']*100 for p in current['people'].values()]==pytest.approx([100,100,94])
    add_event(journals,account=A,at=150)
    journals['one'].append(A,'quota',dict(account=A,used=95,reset_at=800,at=160),160)
    close_samples(journals)
    unchanged,_=book(db)
    assert unchanged['status']=='active' and unchanged['people']==current['people']
    view=shared_usage(db,'group:test',{A:'账号1',B:'账号2'},400,rules=load_rules(db,[A,B],400))
    assert sum(row['quota'] for row in view['windows']['cycle']['quota_rows'])==2
    assert not view['windows']['cycle']['rows']
    journals['one'].append(A,'quota',dict(account=A,used=0,reset_at=1500,at=801),801)
    journals['one'].append(A,'quota',dict(account=A,used=0,reset_at=1500,at=820),820)
    current,_=book(db,830)
    assert current['status']=='active'
    assert sum(p['available'] for p in current['people'].values())==pytest.approx(198)
