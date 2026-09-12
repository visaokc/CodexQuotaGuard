import copy
import json

import pytest

from test_shared_billing import A, B, add_event, close_samples, setup_group
from quota_guard.journal import Journal
from quota_guard.ledger import Ledger
from quota_guard.shared_costs import token_shares, uncovered
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
    assert sum(r['shared_quota'] for r in view['windows']['cycle']['quota_rows']) == pytest.approx(6)
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


def test_member_tokens_and_all_donut_ranges_share_without_changing_curves_or_events(tmp_path):
    from quota_guard.usage_history import range_window
    db, journals, _ = setup_group(tmp_path)
    for account, device, at in ((B,'one',150), (B,'one',200), (B,'one',201), (B,'two',202), (A,'one',150)):
        add_event(journals, account=account, device=device, at=at)
    before = shared_usage(db, 'group:test', {A:'a', B:'b'}, 400, rules=load_rules(db, [A,B], 400))
    with db.connect() as connection:
        stored = [tuple(r) for r in connection.execute('SELECT * FROM events ORDER BY id')]
    declare(db, journals)
    rules = load_rules(db, [A,B], 400)
    after = shared_usage(db, 'group:test', {A:'a', B:'b'}, 400, rules=rules)
    for name in ('hour', 'hour_curve', 'six_hours', 'twelve_hours', 'day', 'week', 'month'):
        for person in ('person1','person2','person3'):
            for field in ('tokens','cache_tokens','input_tokens','output_tokens'):
                assert sum(r[field] or 0 for r in after['windows'][name]['rows'] if r['device']==person) == sum(r[field] or 0 for r in before['windows'][name]['rows'] if r['device']==person)
        assert sum(r['tokens'] for r in after['windows'][name]['rows'] if r.get('maintenance')) == 2200
    def totals(rows):
        return [sum(r['tokens'] for r in rows if r['device']==p) for p in ('person1','person2','person3')]
    assert totals(after['windows']['cycle']['rows']) == [2934,1834,732]
    assert sum(r.get('shared_tokens',0) for r in after['windows']['cycle']['rows']) == 2200
    for window in after['donut_windows'].values():
        assert totals(window['rows']) == [2934,1834,732]
        assert sum(r['cache_tokens'] for r in window['rows']) == 4500
    selected = range_window(db, [dict(account=B, start=100, after=100, end=400)], rules)
    assert totals(selected['rows']) == [1834,1834,732]
    from quota_guard.web_controller import _view
    safe = _view(dict(analytics=dict(windows=dict(cycle=selected))))['analytics']['windows']['cycle']
    assert sum(r.get('shared_tokens',0) for r in safe['rows']) == 2200
    with db.connect() as connection:
        assert [tuple(r) for r in connection.execute('SELECT * FROM events ORDER BY id')] == stored


def test_daily_donut_archives_only_account1_and_keeps_account2_whole_cycle(tmp_path):
    from test_clean_start import setup_clean
    from quota_guard.usage_history import range_window
    db, journals, _ = setup_clean(tmp_path)
    for at in (150,200,201,250):
        add_event(journals, account=B, at=at)
    add_event(journals, account=A, at=190)
    add_event(journals, account=A, at=240)
    journals['one'].append(A, 'quota', dict(account=A, used=100, reset_at=800, at=200), 200)
    declare(db, journals, through=220, now=300)
    rules = load_rules(db, [A,B], 400)
    view = shared_usage(db, 'group:test', {A:'a',B:'b'}, 400, rules=rules)
    assert view['donut_archive_at'] == 200
    daily = view['donut_windows']['today']['rows']
    assert [sum(r['tokens'] for r in daily if r['device']==p) for p in ('person1','person2','person3')] == [2201,1101,1098]
    assert {r['account'] for r in daily} == {B}
    from quota_guard.usage_history import archive_end
    archived = range_window(db, [dict(account=A,start=100,end=archive_end(db,rules,400))], rules)['rows']
    assert sum(r['tokens'] for r in archived) == 2200
    assert sum(r['tokens'] for r in daily) == 4400
    assert view['donut_windows']['cycle']['rows'] == view['windows']['cycle']['rows']
    assert sum(r['tokens'] for r in view['windows']['hour_curve']['rows']) == 6600
    for at in (801,820):
        journals['one'].append(A,'quota',dict(account=A,used=0,reset_at=1500,at=at),at)
    add_event(journals,account=A,at=825)
    later = shared_usage(db,'group:test',{A:'a',B:'b'},830,rules=load_rules(db,[A,B],830))
    assert sum(r['tokens'] for r in later['donut_windows']['total']['rows'] if r['account']==A) == 1100
    assert sum(r['tokens'] for r in later['donut_windows']['total']['rows'] if r['account']==B) == 4400


def test_token_split_conserves_details_and_missing_values_with_integer_rounding():
    import random
    rng = random.Random(633)
    policy = dict(shared_costs=[dict(account=B,person='person1',since=100,through=200)])
    for _ in range(100):
        inputs, outputs = rng.randrange(10000), rng.randrange(10000)
        event = dict(account=B,ts=150,tokens=inputs+outputs,weight=123,
                     input_tokens=inputs,output_tokens=outputs,cached_input_tokens=rng.randrange(inputs+1),
                     reasoning_output_tokens=rng.randrange(outputs+1))
        parts = [part for _,part in token_shares(event,'person1',policy)]
        for key in ('tokens','input_tokens','output_tokens','cached_input_tokens','reasoning_output_tokens'):
            assert sum(p[key] for p in parts) == event[key]
        assert max(p['tokens'] for p in parts)-min(p['tokens'] for p in parts) <= 1
        for part in parts:
            assert part['tokens'] == part['input_tokens']+part['output_tokens']
            assert 0 <= part['cached_input_tokens'] <= part['input_tokens']
            assert 0 <= part['reasoning_output_tokens'] <= part['output_tokens']
    missing = dict(event,input_tokens=None,output_tokens=None,cached_input_tokens=None,reasoning_output_tokens=None)
    parts = [part for _,part in token_shares(missing,'person1',policy)]
    assert sum(p['tokens'] for p in parts) == missing['tokens']
    assert all(p['input_tokens'] is None and p['cached_input_tokens'] is None for p in parts)
