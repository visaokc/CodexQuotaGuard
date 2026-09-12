import copy
import json

import pytest

from test_shared_billing import A, B, add_event, close_samples, setup_group
from quota_guard.maintenance import publish
from quota_guard.shared_policy import load_rules
from quota_guard.shared_quota import attribution, accounting
from quota_guard.shared_view import shared_usage, shared_overview


def toggle(db, journals, device, enabled, at):
    return publish(journals[device], load_rules(db, [A, B], at),
                   dict(device_id=device, name=device, quota=50), enabled, at)


def view(db, at=500):
    return shared_usage(db, 'group:test', {A:'账号1', B:'账号2'}, at,
                        rules=load_rules(db, [A, B], at))


def test_member_switch_bounds_accounts_overlap_and_late_confirmation(tmp_path):
    db, journals, _ = setup_group(tmp_path)
    add_event(journals, account=B, device='two', at=120)
    assert toggle(db, journals, 'two', True, 120)
    assert not toggle(db, journals, 'two', True, 125)
    add_event(journals, account=B, device='two', at=130)
    add_event(journals, account=A, device='two', at=140)
    assert toggle(db, journals, 'three', True, 135)
    add_event(journals, account=B, device='three', at=150)
    assert toggle(db, journals, 'two', False, 160)
    add_event(journals, account=B, device='two', at=160)
    add_event(journals, account=B, device='two', at=161)
    for a, used in ((A, 3), (B, 15)):
        journals['one'].append(a, 'quota', dict(account=a, used=used, reset_at=800 if a==A else 850, at=210), 210)
    with db.connect() as con:
        original = [tuple(r) for r in con.execute('SELECT * FROM events ORDER BY id')]
    assert view(db)['billing']['status'] == 'syncing'
    close_samples(journals)
    result = view(db)
    assert [p['fair_usage'] for p in result['billing']['people'].values()] == pytest.approx([4, 10, 4])
    assert sum(r['tokens'] for r in result['windows']['day']['rows']) == 6600
    assert sum(r['tokens'] for r in result['windows']['day']['rows'] if r.get('maintenance')) == 4400
    assert sum(r['tokens'] for r in result['donut_windows']['today']['rows']) == 6600
    assert not any(r.get('maintenance') for r in result['donut_windows']['today']['rows'])
    assert sum(r['quota'] for r in result['windows']['day']['quota_rows']) == pytest.approx(18)
    assert sum(r['quota'] for r in result['windows']['day']['quota_rows'] if r.get('maintenance')) == pytest.approx(12)
    assert not load_rules(db, [A, B], 500)['maintenance']['person2']['enabled']
    assert load_rules(db, [A, B], 500)['maintenance']['person3']['enabled']
    with db.connect() as con:
        assert [tuple(r) for r in con.execute('SELECT * FROM events ORDER BY id')] == original


def test_switch_replay_is_idempotent_and_cannot_name_another_member(tmp_path):
    from quota_guard.journal import Journal
    from quota_guard.ledger import Ledger
    from quota_guard.storage import Database
    db, journals, _ = setup_group(tmp_path)
    toggle(db, journals, 'two', True, 120)
    toggle(db, journals, 'two', False, 180)
    expected = load_rules(db, [A, B], 500)
    target = Database(tmp_path/'replica.sqlite')
    receiver = Journal(target, Ledger(target), 'receiver')
    with db.connect() as con:
        records = [dict(r) for r in con.execute('SELECT * FROM facts ORDER BY seq DESC')]
    for record in records:
        record['payload'] = json.loads(record['payload'])
        receiver.merge(record['account'], [record])
        receiver.merge(record['account'], [record])
    assert load_rules(target, [A, B], 500) == expected
    with pytest.raises(ValueError, match='开关无效'):
        journals['one'].append(A, 'profile', dict(device='one', name='one', cap=50,
            maintenance=dict(genesis=expected['genesis'], enabled=True, person='person3')), 200)


def test_release_declaration_overlapping_switch_shares_once(tmp_path):
    from test_shared_costs import declare
    db, journals, _ = setup_group(tmp_path)
    toggle(db, journals, 'one', True, 120)
    add_event(journals, account=B, at=150)
    toggle(db, journals, 'one', False, 170)
    declare(db, journals)
    journals['one'].append(B, 'quota', dict(account=B, used=3, reset_at=850, at=210), 210)
    close_samples(journals)
    data = view(db)
    assert [p['fair_usage'] for p in data['billing']['people'].values()] == pytest.approx([1,1,1])
    assert sum(r['tokens'] for r in data['windows']['cycle']['rows']) == 1100


def test_estimate_moves_without_official_change_then_recalibrates_without_posting(tmp_path):
    db, journals, _ = setup_group(tmp_path)
    add_event(journals, account=B, at=150)
    journals['one'].append(B, 'quota', dict(account=B, used=3, reset_at=850, at=210), 210)
    close_samples(journals)
    initial = view(db)
    add_event(journals, account=B, at=450)
    estimated = view(db)
    assert estimated['billing'] == initial['billing']
    report = estimated['live_reporting']
    assert report['balances']['person1']['estimate_pending'] == pytest.approx(3)
    assert report['balances']['person1']['available_estimate'] == pytest.approx(200/3-6)
    assert report['balances']['person1']['balance_estimated']
    assert sum(r['tokens'] for r in report['daily']) == 2200
    assert sum(r['quota']+r['estimated_quota'] for r in report['daily']) == pytest.approx(6)
    journals['one'].append(B, 'quota', dict(account=B, used=7, reset_at=850, at=510), 510)
    close_samples(journals, at=650)
    calibrated = view(db, 650)
    assert calibrated['live_reporting']['balances']['person1']['estimate_pending'] == 0
    assert calibrated['live_reporting']['balances']['person1']['available_estimate'] == pytest.approx(200/3-7)
    assert sum(r['quota']+r['estimated_quota'] for r in calibrated['live_reporting']['daily']) == pytest.approx(7)


def test_estimate_and_daily_tokens_share_same_switch_interval(tmp_path):
    db, journals, _ = setup_group(tmp_path)
    add_event(journals, account=B, at=150)
    journals['one'].append(B, 'quota', dict(account=B, used=3, reset_at=850, at=210), 210)
    close_samples(journals)
    toggle(db, journals, 'two', True, 410)
    add_event(journals, account=B, device='two', at=450)
    data = view(db)['live_reporting']
    assert [p['estimate_pending'] for p in data['balances'].values()] == pytest.approx([1,1,1])
    assert sum(r['tokens'] for r in data['daily']) == 2200
    assert sum(r['estimated_quota'] for r in data['daily']) == pytest.approx(3)


@pytest.mark.parametrize('used', [0, 100])
def test_fixed_basis_does_not_modify_point_inventory(tmp_path, used):
    db, journals, _ = setup_group(tmp_path, used_a=used)
    data = view(db)
    before = copy.deepcopy(data['billing'])
    result = shared_overview(db, 'group:test', {A:'a', B:'b'},
        {p:dict(name=p, online=True) for p in journals}, 'one', 500, data)
    assert all(p['available_cap'] == 100/3 for p in result['members'])
    assert sum(p['available'] for p in result['members']) == pytest.approx(200-used)
    assert result['billing'] == before
    assert result['daily_usage']['account_count'] == (2 if used==0 else 1)
