import copy
import json

import pytest

from test_shared_billing import A, B, add_event, close_samples, setup_group
from quota_guard.journal import Journal
from quota_guard.ledger import Ledger
from quota_guard.shared_costs import uncovered
from quota_guard.shared_policy import digest, load_rules, publish_change
from quota_guard.shared_quota import attribution, accounting
from quota_guard.shared_view import shared_usage
from quota_guard.storage import Database


def declare(db, journals, since=100, through=200, now=220):
    rules = load_rules(db, [A, B], now)
    return publish_change(journals['one'], rules, 'one', 'one', 50,
        dict(shared_costs=[dict(account=B, person='person1', since=since, through=through)]), now)


def test_shared_cost_scope_preserves_raw_usage_and_conserves_official_quota(tmp_path):
    db, journals, _ = setup_group(tmp_path)
    add_event(journals, account=B, at=150)
    add_event(journals, account=B, at=200)
    add_event(journals, account=B, at=201)
    add_event(journals, account=B, device='two', at=202)
    add_event(journals, account=A, at=150)
    for account, used in ((A, 3), (B, 12)):
        journals['one'].append(account, 'quota', dict(account=account, used=used, reset_at=800 if account==A else 850, at=210), 210)
    declare(db, journals)
    close_samples(journals)
    rules = load_rules(db, [A, B], 400)
    data = attribution(db, rules, 400)
    book = accounting(rules, data, 400)
    assert book['status'] == 'active'
    # Two maintenance events cost 6 total: 2 each. Later/private usage stays personal.
    assert [p['fair_usage'] for p in book['people'].values()] == pytest.approx([8, 5, 2])
    assert sum(e['units'] for e in data['events']) == 15*10**9
    shared = [e for e in data['events'] if e.get('shared_cost')]
    assert len(shared) == 6 and all(e['source_device'] == 'one' for e in shared)
    assert sum(e['cache_quota'] for e in shared) == pytest.approx(6*22500/172500, abs=1e-8)
    view = shared_usage(db, 'group:test', {A:'a', B:'b'}, 400, rules=rules)
    assert sum(r['tokens'] for r in view['windows']['total']['rows']) == 5500
    assert sum(r['tokens'] for r in view['windows']['total']['rows'] if r['device']=='person3') == 0
    assert not any('shared_cost' in row['kind'] for row in book['entries'])


def test_late_confirmation_and_duplicate_reordered_replication_apply_once(tmp_path):
    db, journals, _ = setup_group(tmp_path)
    add_event(journals, account=B)
    journals['one'].append(B, 'quota', dict(account=B, used=3, reset_at=850, at=210), 210)
    declare(db, journals)
    rules = load_rules(db, [A, B], 400)
    assert accounting(rules, attribution(db, rules, 400), 400)['status']=='syncing'
    close_samples(journals)
    expected = accounting(rules, attribution(db, rules, 400), 400)
    assert [p['fair_usage'] for p in expected['people'].values()] == pytest.approx([1,1,1])
    replica = Database(tmp_path/'replica.sqlite')
    receiver = Journal(replica, Ledger(replica), 'receiver')
    with db.connect() as connection:
        records = [dict(row) for row in connection.execute('SELECT * FROM facts ORDER BY seq DESC')]
    for row in records:
        row['payload'] = json.loads(row['payload'])
        receiver.merge(row['account'], [row])
        receiver.merge(row['account'], [row])
    replayed = load_rules(replica, [A, B], 400)
    assert accounting(replayed, attribution(replica, replayed, 400), 400) == expected


def test_shared_intervals_are_append_only_and_reject_overlap(tmp_path):
    db, journals, _ = setup_group(tmp_path)
    policy = declare(db, journals)
    rules = load_rules(db, [A, B], 400)
    with pytest.raises(ValueError, match='重复'):
        publish_change(journals['one'], rules, 'one', 'one', 50,
            dict(shared_costs=policy['shared_costs']*2), 400)
    revised = copy.deepcopy(policy)
    revised.update(revision=policy['revision']+1, previous=digest(policy), effective=400, shared_costs=[])
    journals['one'].append(A, 'profile', dict(device='one', name='one', cap=50, group_policy=revised), 400)
    assert load_rules(db, [A, B], 400)['status']=='conflict'


def test_release_interval_difference_is_idempotent_and_does_not_cross_scope():
    costs = [dict(account=B, person='person1', since=100, through=200)]
    assert uncovered(costs, B, 'person1', 100, 200)==[]
    assert uncovered(costs, B, 'person1', 100, 300)==[dict(account=B, person='person1', since=200, through=300)]
    assert uncovered(costs, A, 'person1', 100, 200)==[dict(account=A, person='person1', since=100, through=200)]
    assert uncovered(costs, B, 'person2', 100, 200)==[dict(account=B, person='person2', since=100, through=200)]


def test_shared_cost_keeps_third_member_baseline_and_independent_resets(tmp_path):
    from test_clean_start import setup_clean
    db, journals, _ = setup_clean(tmp_path)
    journals['one'].append(A, 'quota', dict(account=A, used=100, reset_at=800, at=140), 140)
    add_event(journals, account=B, at=150)
    journals['one'].append(B, 'quota', dict(account=B, used=8, reset_at=850, at=210), 210)
    declare(db, journals)
    close_samples(journals)
    rules = load_rules(db, [A, B], 400)
    book = accounting(rules, attribution(db, rules, 400), 400)
    assert [p['fair_usage'] for p in book['people'].values()] == pytest.approx([2,2,4])
    assert sum(p['available'] for p in book['people'].values()) == pytest.approx(92)
    for at in (801, 820):
        journals['one'].append(A, 'quota', dict(account=A, used=0, reset_at=1500, at=at), at)
    later = accounting(rules, attribution(db, rules, 830), 830)
    assert [p['fair_usage'] for p in later['people'].values()] == pytest.approx([2,2,4])
    assert sum(p['available'] for p in later['people'].values()) == pytest.approx(192)


def test_shared_cost_does_not_invent_unknown_model_weight(tmp_path):
    db, journals, _ = setup_group(tmp_path)
    add_event(journals, account=B, model='codex-auto-review')
    journals['one'].append(B, 'quota', dict(account=B, used=1, reset_at=850, at=210), 210)
    declare(db, journals)
    close_samples(journals)
    rules = load_rules(db, [A, B], 400)
    data = attribution(db, rules, 400)
    assert not data['events']
    assert accounting(rules, data, 400)['status']=='syncing'
