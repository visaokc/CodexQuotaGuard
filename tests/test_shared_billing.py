import copy
import hashlib
import random

import pytest

from quota_guard.journal import Journal
from quota_guard.ledger import Ledger
from quota_guard.pool_accounting import FULL, Pool, points, split, units
from quota_guard.shared_policy import PERSONS, digest, genesis, load_rules, person_for
from quota_guard.shared_quota import accounting, attribution
from quota_guard.shared_view import shared_usage, shared_overview
from quota_guard.storage import Database

A, B = 'a'*64, 'b'*64
D = ('one', 'two', 'three')


def fresh_pool(enabled=True):
    pool = Pool([A, B], enabled)
    for account in (A, B):
        pool.grant(account, FULL, [100, 800], 100, initial=True)
    return pool


def test_spending_on_one_account_exchanges_existing_other_account_rights_before_debt():
    pool = fresh_pool()
    pool.spend(A, 'person1', units(60), 200)
    assert all(pool.debt(p) == 0 for p in PERSONS)
    assert pool.summary()['person1']['available'] == pytest.approx(200/3-60, abs=1e-8)
    pool.reset(A, [800, 1500], 800, 'natural')
    assert [pool.summary()[p]['available'] for p in PERSONS] == pytest.approx([40,80,80], abs=1e-8)
    pool.reset(B, [850,1550], 850, 'natural')
    assert [pool.summary()[p]['available'] for p in PERSONS] == pytest.approx([200/3]*3, abs=1e-8)


def test_confirmed_borrowing_repaid_once_across_both_accounts():
    pool = fresh_pool()
    pool.spend(A, 'person1', FULL, 200)
    pool.reset(A, [800,1500], 800, 'natural')
    pool.spend(B, 'person2', units(50), 810)
    pool.spend(B, 'person3', units(50), 820)
    pool.reset(B, [850,1550], 850, 'card')
    assert [pool.summary()[p]['available'] for p in PERSONS] == pytest.approx([100/3,250/3,250/3], abs=1e-8)
    assert all(pool.debt(p) == 0 for p in PERSONS)
    pool.reset(A, [1500,2200], 1500, 'natural')
    assert not any(pool.confirmed.values())


@pytest.mark.parametrize('cause,exempt', [('official',False), ('natural',True), ('card',True)])
def test_official_and_explicit_exemption_drop_only_source_cycle_new_debt(cause, exempt):
    pool = fresh_pool()
    pool.spend(A, 'person1', FULL, 200)
    pool.spend(B, 'person1', FULL, 210)
    old_other = dict(pool.pending[B])
    pool.reset(A, [800,1500], 800, cause, exempt=exempt)
    assert not any(pool.pending[A].values())
    assert pool.pending[B] == old_other
    assert sum(pool.stock[A].values()) == FULL


def test_disabled_compensation_archives_debt_and_never_revives_it():
    pool = fresh_pool()
    pool.spend(A, 'person1', FULL, 200)
    pool.compensation(False, 201)
    assert all(pool.debt(p) == 0 for p in PERSONS)
    pool.compensation(True, 300)
    pool.reset(A, [800,1500], 800, 'natural')
    assert all(pool.debt(p) == 0 for p in PERSONS)
    assert [points(pool.stock[A][p]) for p in PERSONS] == pytest.approx([100/3]*3, abs=1e-8)


def test_random_integer_inventory_conservation_and_large_debt():
    rng = random.Random(919)
    pool = fresh_pool()
    for index in range(3000):
        account = rng.choice((A,B))
        if rng.random() < .2 or not pool.remaining[account]:
            pool.reset(account, [index+900,index+1600], index+900, rng.choice(('natural','card','official')))
        elif rng.random() < .1:
            pool.compensation(rng.choice((True,False)), index+900)
        else:
            pool.spend(account, rng.choice(PERSONS), min(pool.remaining[account], rng.randint(1,50)*units(1)), index+900)
        pool.check()


def setup_group(tmp_path, devices=D, used_a=0):
    database = Database(tmp_path/'group.sqlite')
    ledger = Ledger(database)
    journals = {d: Journal(database, ledger, d) for d in D}
    policy = genesis('one', A, devices, 90)
    policy['accounts'] = [A,B]
    journals['one'].append(A, 'profile', dict(device='one', name='one', cap=50, group_policy=policy), 90)
    for account in (A,B):
        journals['one'].append(account, 'quota', dict(account=account,used=used_a if account==A else 0,reset_at=800 if account==A else 850,at=100), 100)
        for device in D:
            journals[device].append(account, 'profile', dict(device=device,name=device,cap=50), 100)
    return database, journals, policy


def add_event(journals, account=A, device='one', at=200, cached=900, model='gpt-6-astra'):
    event = dict(id=hashlib.sha256(f'{device}:{account}:{at}'.encode()).hexdigest(),device=device,account=account,ts=at,
                 model=model,tokens=1100,input_tokens=1000,cached_input_tokens=cached,output_tokens=100,
                 weight=999999,known=True)
    journals[device].append(account,'events',[event],at)
    return event


def close_samples(journals, at=400):
    for account in (A,B):
        for device in D:
            journals[device].append(account,'profile',dict(device=device,name=device,cap=50,
                sample_checkpoint=dict(through=at-120)),at)


def test_official_attribution_is_shared_by_chart_summary_and_pool_not_local_weight(tmp_path):
    database,journals,_ = setup_group(tmp_path)
    add_event(journals)
    journals['one'].append(A,'quota',dict(account=A,used=60,reset_at=800,at=210),210)
    close_samples(journals)
    rules = load_rules(database,[A,B],400)
    attributed = attribution(database,rules,400)
    book = accounting(rules,attributed,400)
    assert book['status'] == 'active'
    assert attributed['events'][0]['quota'] == 60
    assert attributed['events'][0]['cache_quota'] == pytest.approx(60*22500/172500, abs=1e-8)
    assert book['people']['person1']['available'] == pytest.approx(200/3-60, abs=1e-8)
    view = shared_usage(database,'group:test',{A:'账号1',B:'账号2'},400,rules=rules)
    overview = shared_overview(database,'group:test',{A:'账号1',B:'账号2'},
        {d:dict(name=d,online=True,accounts=[A,B],current_account=A) for d in D},'one',400,view)
    assert view['windows']['cycle']['quota_rows'][0]['quota'] == 60
    assert overview['summary']['devices'][0]['estimated'] == 60
    assert overview['summary']['devices'][0]['tokens'] == 1100
    assert overview['summary']['devices'][0]['local']
    assert [d['avatar'] for d in overview['summary']['devices']] == ['person1.jpg','person2.jpg','person3.jpg']


def test_late_peer_details_wait_then_replay_to_same_official_totals(tmp_path):
    database,journals,_ = setup_group(tmp_path)
    add_event(journals)
    journals['one'].append(A,'quota',dict(account=A,used=10,reset_at=800,at=210),210)
    rules = load_rules(database,[A,B],400)
    waiting = attribution(database,rules,400)
    assert not waiting['events']
    assert accounting(rules,waiting,400)['people']['person1']['available'] is None
    add_event(journals, device='two', at=205)
    close_samples(journals)
    ready = attribution(database,rules,400)
    assert [event['quota'] for event in ready['events']] == [5,5]
    assert accounting(rules,ready,400)['status'] == 'active'


def test_third_person_claim_is_stable_and_multiple_claims_require_admin(tmp_path):
    database,journals,policy = setup_group(tmp_path,devices=D[:2])
    rules = load_rules(database,[A,B],400)
    assert rules['status'] == 'waiting'
    claim = dict(device='three',person='person3',genesis=digest(policy))
    journals['three'].append(B,'profile',dict(device='three',name='three',cap=50,member_claim=claim),150)
    rules = load_rules(database,[A,B],400)
    assert rules['status'] == 'ready' and rules['eligible_at'] == 150
    assert person_for(rules,'three',20) == 'person3'
    fourth = Journal(database,Ledger(database),'four')
    fourth.append(B,'profile',dict(device='four',name='four',cap=50,member_claim=dict(claim,device='four')),160)
    assert load_rules(database,[A,B],400)['status'] == 'conflict'


def test_rule_conflict_and_missing_predecessor_are_not_silently_chosen(tmp_path):
    database,journals,policy = setup_group(tmp_path)
    revised = dict(policy,revision=2,previous=digest(policy),effective=110,compensation=True)
    journals['one'].append(A,'profile',dict(device='one',name='one',cap=50,group_policy=revised),110)
    other = dict(revised,compensation=False)
    journals['one'].append(A,'profile',dict(device='one',name='one',cap=50,group_policy=other),110)
    assert load_rules(database,[A,B],400)['status'] == 'conflict'


def test_migration_distributes_only_actual_remaining_and_not_old_consumption(tmp_path):
    database,journals,_ = setup_group(tmp_path,used_a=70)
    rules=load_rules(database,[A,B],400)
    book=accounting(rules,attribution(database,rules,400),400)
    assert book['status']=='active'
    assert sum(p['available'] for p in book['people'].values())==pytest.approx(130)
    assert all(p['debt']==0 for p in book['people'].values())


def test_confirming_claim_does_not_move_the_official_migration_anchor(tmp_path):
    from quota_guard.shared_policy import publish_change
    database, journals, policy = setup_group(tmp_path, devices=D[:2])
    claim = dict(device='three', person='person3', genesis=digest(policy))
    journals['three'].append(B,'profile',dict(device='three',name='three',cap=50,member_claim=claim),150)
    rules = load_rules(database,[A,B],400)
    publish_change(journals['one'],rules,'one','one',50,
        dict(bindings=policy['bindings']+[dict(device='three',person='person3',since=0)]),200)
    assert load_rules(database,[A,B],400)['eligible_at'] == 150


def test_reordered_gapped_and_duplicate_fact_delivery_converges(tmp_path):
    import json
    database,journals,_ = setup_group(tmp_path)
    add_event(journals, device='one')
    add_event(journals, device='two', at=205)
    journals['one'].append(A,'quota',dict(account=A,used=10,reset_at=800,at=210),210)
    close_samples(journals)
    with database.connect() as db:
        records = [dict(row) for row in db.execute('SELECT * FROM facts ORDER BY account,origin,seq')]
    # Journal wire records have decoded payloads. A future checkpoint arriving
    # before an earlier fact must not declare that origin's sample complete.
    for row in records:
        row['payload'] = json.loads(row['payload'])
    expected = accounting(load_rules(database,[A,B],400),attribution(database,load_rules(database,[A,B],400),400),400)
    for index, order in enumerate((records, list(reversed(records)), random.Random(41).sample(records,len(records)))):
        replica = Database(tmp_path/('replica'+str(index)+'.sqlite'))
        receiver = Journal(replica,Ledger(replica),'receiver')
        for row in order:
            receiver.merge(row['account'],[row])
        for row in order:
            receiver.merge(row['account'],[row])
        rules = load_rules(replica,[A,B],400)
        actual = accounting(rules,attribution(replica,rules,400),400)
        assert actual == expected
    incomplete = Database(tmp_path/'incomplete.sqlite')
    receiver = Journal(incomplete,Ledger(incomplete),'receiver')
    missing = next(row for row in records if row['account']==A and row['origin']=='one' and row['kind']=='events')
    for row in records:
        if row != missing:
            receiver.merge(row['account'],[row])
    rules = load_rules(incomplete,[A,B],400)
    assert not attribution(incomplete,rules,400)['events']
    receiver.merge(A,[missing])
    assert accounting(rules,attribution(incomplete,rules,400),400)==expected


def test_identity_change_splits_one_bucket_without_rewriting_earlier_usage(tmp_path):
    from quota_guard.shared_policy import publish_change
    database,journals,policy = setup_group(tmp_path)
    add_event(journals,at=200)
    rules = load_rules(database,[A,B],400)
    publish_change(journals['one'],rules,'one','one',50,
        dict(bindings=policy['bindings']+[dict(device='one',person='person2',since=202)]),202)
    add_event(journals,at=205)
    rules = load_rules(database,[A,B],400)
    view = shared_usage(database,'group:test',{A:'账号1',B:'账号2'},400,rules=rules)
    rows = view['windows']['cycle']['rows']
    assert sum(r['tokens'] for r in rows if r['device']=='person1') == 1100
    assert sum(r['tokens'] for r in rows if r['device']=='person2') == 1100


def test_late_historical_event_invalidates_old_sample_confirmation(tmp_path):
    database,journals,_ = setup_group(tmp_path)
    add_event(journals)
    journals['one'].append(A,'quota',dict(account=A,used=10,reset_at=800,at=210),210)
    close_samples(journals)
    rules=load_rules(database,[A,B],410)
    assert attribution(database,rules,410)['events'][0]['quota']==10
    add_event(journals,at=202)
    assert not attribution(database,rules,410)['events']
    close_samples(journals,at=405)
    assert sum(e['quota'] for e in attribution(database,rules,410)['events'])==10


def test_old_cycle_consumption_at_reset_boundary_happens_before_expiration():
    policy=genesis('one',A,D,90)
    policy.update(accounts=[A,B],compensation=True)
    rules=dict(status='ready',reason='',accounts=[A,B],policies=[policy])
    old=dict(started=100,reset_at=200,ended=200,baseline=0,reason='自然重置')
    new=dict(started=200,reset_at=900,ended=None,baseline=0,reason='自然重置')
    other=dict(old,reset_at=850,ended=None)
    event=dict(id='usage',ts=200,device='person1',units=FULL)
    attributed=dict(anchors={A:dict(at=100,used=0,cycle=old),B:dict(at=100,used=0,cycle=other)},
        epochs={A:[old,new],B:[other]}, streams=[dict(account=A,start=100,end=200,cycle_start=100,ready=True,events=[event])],overrides={},exempt=set())
    book=accounting(rules,attributed,400)
    assert book['status']=='active'
    assert book['people']['person1']['available']==pytest.approx(0,abs=1e-8)
    assert any(row['kind']=='confirm' for row in book['entries'])
