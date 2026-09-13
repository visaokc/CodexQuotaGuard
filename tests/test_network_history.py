"""Shared IP history is self-authored metadata, never quota or usage input."""
import json

import pytest

from quota_guard.journal import Journal
from quota_guard.ledger import Ledger
from quota_guard.network_history import members, publish, validate_report
from quota_guard.shared_policy import genesis
from quota_guard.shared_sync import SharedSync
from test_shared_sync import A, B, Bus, seed


def report(at=200, ip='1.1.1.1', **extra):
    return dict(ip=ip, checked_at=at, location='澳大利亚', country='澳大利亚', purity='纯净',
                risk_score=3, risk_at=at, error=None, risk_error=None, **extra)


def rules():
    policy = genesis('one', A, ['one', 'two', 'third'], 100)
    policy['accounts'] = [A, B]
    return dict(policy=policy, accounts=[A, B], bindings=policy['bindings'])


def config(device='one'):
    return dict(device_id=device, name='User '+device, quota=50, tracked_accounts={})


def network_facts(sync):
    with sync.db.connect() as db:
        return [json.loads(row[0]) for row in db.execute("SELECT payload FROM facts WHERE kind='profile'")
                if 'network_change' in json.loads(row[0])]


def test_only_changes_persist_and_restart_does_not_duplicate_or_change_ledger(tmp_path):
    bus = Bus()
    sync = bus.add(tmp_path, 'one', (A, B))
    seed(sync, A)
    with sync.db.connect() as db:
        original = {table: [tuple(row) for row in db.execute('SELECT * FROM '+table)]
                    for table in ('events', 'event_details', 'epochs', 'segments', 'devices')}
    assert not publish(sync.journal, rules(), config(), report(200), 200)
    assert not publish(sync.journal, rules(), config(), report(210), 210)
    fresh = SharedSync(sync.db, Journal(sync.db, Ledger(sync.db), 'one'), 'one', 'User one', sync.tracked)
    assert not publish(fresh.journal, rules(), config(), report(220), 220)
    changed = report(230, '8.8.8.8', previous_ip='1.1.1.1', codex_running=True)
    assert publish(fresh.journal, rules(), config(), changed, 230)
    assert not publish(fresh.journal, rules(), config(), changed, 240)
    assert not publish(fresh.journal, rules(), config(), report(225), 235)
    assert len(network_facts(sync)) == 1
    assert network_facts(sync)[0]['network_change'] == {'checked_at': 230}
    assert '8.8.8.8' not in json.dumps(network_facts(sync)) and '1.1.1.1' not in json.dumps(network_facts(sync))
    with sync.db.connect() as db:
        assert {table: [tuple(row) for row in db.execute('SELECT * FROM '+table)]
                for table in original} == original


def test_live_catalog_refreshes_timestamp_but_only_self_report_and_no_replay(tmp_path):
    bus = Bus()
    left, right = bus.add(tmp_path, 'one', (A,)), bus.add(tmp_path, 'two', (A,))
    mesh = bus.clients['one'][1]
    right.network_reports['two'] = report(200, previous_ip='8.8.8.8', codex_running=True)
    right.network_reports['third'] = report(200, '8.8.8.8')
    catalog = right._catalog(200)
    assert 'previous_ip' not in catalog['network_report']
    assert 'network_reports' not in catalog
    assert left.receive('two', catalog, mesh, 200)
    assert set(left.network_reports) == {'two'}
    right.network_reports['two'] = report(210)
    assert left.receive('two', right._catalog(210), mesh, 210)
    assert left.network_reports['two']['checked_at'] == 210
    older = right._catalog(211)
    older['network_report'] = report(205, '8.8.8.8')
    assert left.receive('two', older, mesh, 211)
    assert left.network_reports['two']['ip'] == '1.1.1.1'
    bad = right._catalog(212)
    bad['network_report']['device'] = 'third'
    assert not left.receive('two', bad, mesh, 212)
    assert set(left.network_reports) == {'two'}
    assert 'network_report' not in right._catalog(400)


@pytest.mark.parametrize('change', [
    dict(ip='127.0.0.1'), dict(ip='10.1.1.1'), dict(ip='1.1.1.1/24'),
    dict(checked_at=1000), dict(checked_at=100), dict(checked_at=True),
    dict(risk_score=float('nan')), dict(risk_score=True), dict(risk_score=101),
    dict(country='A\nB'), dict(purity='x'*41), dict(risk_at=999), dict(device='third'),
    dict(codex_running='true'), dict(previous_ip='8.8.8.8'),
    dict(previous_ip='1.1.1.1', codex_running=True), dict(previous_ip='127.0.0.1', codex_running=True),
])
def test_malformed_live_reports_rejected(change):
    value = report()
    value.update(change)
    with pytest.raises(ValueError):
        validate_report(value, 200, live=True)


def test_journal_rejects_forged_origin_and_invalid_report_atomically(tmp_path):
    bus = Bus()
    sync = bus.add(tmp_path, 'one', (A,))
    row = dict(account=A, origin='two', seq=1, ts=200, kind='profile',
               payload=dict(device='third', name='Forged', cap=50, network_report=report()))
    with pytest.raises(ValueError):
        sync.journal.merge(A, [row])
    row['payload']['device'] = 'two'
    row['payload']['network_report']['risk_score'] = -1
    with pytest.raises(ValueError):
        sync.journal.merge(A, [row])
    assert sync.journal.vector(A) == {}
    assert network_facts(sync) == []


def test_three_member_offline_history_replication_and_missing_history(tmp_path):
    bus = Bus()
    left, right = bus.add(tmp_path, 'one', (A,)), bus.add(tmp_path, 'two', (A,))
    publish(right.journal, rules(), config('two'), report(200, previous_ip='8.8.8.8', codex_running=True), 200)
    right.network_reports['two'] = report(210)
    bus.tick(210, duplicate=True)
    state = left.snapshot(bus.clients['one'][1], 210)
    value = members(left.db, rules(), state['members'], left.network_reports, 'one', 210)
    assert [row['id'] for row in value] == ['person1', 'person2', 'person3']
    assert value[0]['report'] is None and value[0]['history'] == []
    assert value[1]['online'] and value[1]['report']['checked_at'] == 210
    assert value[1]['history'][0]['checked_at'] == 200
    assert value[1]['history'][0]['device_name'] == 'User two'
    assert value[2]['report'] is None and value[2]['history'] == [] and not value[2]['online']
    bus.links = set()
    state = left.snapshot(bus.clients['one'][1], 215)
    offline = members(left.db, rules(), state['members'], left.network_reports, 'one', 215)
    assert not offline[1]['online'] and offline[1]['report']['checked_at'] == 210
    assert len(offline[1]['history']) == 1


def test_history_limit_and_sequence_holes_are_not_presented_as_complete_history(tmp_path):
    bus = Bus()
    left, right = bus.add(tmp_path, 'one', (A,)), bus.add(tmp_path, 'two', (A,))
    for index in range(25):
        publish(right.journal, rules(), config('two'), report(200+index,
            '1.1.1.1' if index%2 else '8.8.8.8',
            previous_ip='8.8.8.8' if index%2 else '1.1.1.1', codex_running=True), 200+index)
    rows = right.journal.since(A, {}, limit=40)
    left.journal.merge(A, rows[1:], limit=40)
    projected = members(left.db, rules(), {}, {}, 'one', 230)
    assert projected[1]['report'] is None and projected[1]['history'] == []
    left.journal.merge(A, rows[:1])
    projected = members(left.db, rules(), {}, {}, 'one', 230)
    assert len(projected[1]['history']) == 20
    assert projected[1]['report'] is None
    assert projected[1]['history'][-1]['checked_at'] == 205


def test_error_transition_keeps_failure_timestamp_and_can_recover(tmp_path):
    bus = Bus()
    sync = bus.add(tmp_path, 'one', (A,))
    assert not publish(sync.journal, rules(), config(), report(200), 200)
    failed = dict(ip=None, checked_at=210, error='Ping0 暂时无法访问')
    assert not publish(sync.journal, rules(), config(), failed, 210)
    assert not publish(sync.journal, rules(), config(), dict(failed, checked_at=220), 220)
    assert not publish(sync.journal, rules(), config(), report(230), 230)
    value = members(sync.db, rules(), {}, {'one': report(230)}, 'one', 230)[0]
    assert value['history'] == []
    assert value['report']['ip'] == '1.1.1.1' and value['report']['checked_at'] == 230


def test_engine_accepts_report_without_three_online_members_and_publishes_view(tmp_path):
    from test_shared_engine import Bus as EngineBus
    bus = EngineBus()
    engine, _, _ = bus.add(tmp_path/'one', 'one', (A, B))
    engine.config.update(shared_billing_v1=True, shared_group_admin='one',
                         shared_initial_devices=['one', 'two', 'third'])
    engine.commands.put(('network_report', report(200)))
    engine.step(200)
    value = engine.view['network_members']
    assert len(value) == 3 and value[0]['report']['ip'] == '1.1.1.1'
    assert value[0]['online'] and not value[1]['online'] and not value[2]['online']
    assert value[0]['history'] == []
    engine.commands.put(('network_report', report(210)))
    engine.step(210)
    assert engine.view['network_members'][0]['report']['checked_at'] == 210
    assert engine.view['network_members'][0]['history'] == []


def test_initial_offline_and_purity_only_reports_never_enter_history(tmp_path):
    bus = Bus()
    sync = bus.add(tmp_path, 'one', (A,))
    publish(sync.journal, rules(), config(), report(200), 200)
    publish(sync.journal, rules(), config(), dict(report(210), purity='危险', risk_score=90), 210)
    publish(sync.journal, rules(), config(), report(220, '8.8.8.8'), 220)
    value = members(sync.db, rules(), {}, {'one': report(220, '8.8.8.8')}, 'one', 230)[0]
    assert value['history'] == [] and value['report']['ip'] == '8.8.8.8'
    assert len(network_facts(sync)) == 0


def test_delayed_engine_retains_each_change_before_latest_normal_report(tmp_path):
    from test_shared_engine import Bus as EngineBus
    bus = EngineBus()
    engine, _, _ = bus.add(tmp_path/'one', 'one', (A, B))
    engine.config.update(shared_billing_v1=True, shared_group_admin='one',
                         shared_initial_devices=['one', 'two', 'third'])
    engine.commands.put(('network_report', report(200)))
    engine.step(200)
    first = report(210, '8.8.8.8', previous_ip='1.1.1.1', codex_running=True)
    second = report(220, previous_ip='8.8.8.8', codex_running=True)
    for value in (first, first, second, report(230)):
        engine.commands.put(('network_report', value))
    engine.step(400)
    member = engine.view['network_members'][0]
    assert [row['checked_at'] for row in member['history']] == [220, 210]
    assert set(member['history'][0]) == {'checked_at', 'device', 'device_name'}
    assert not engine._network_change_pending
    engine.step(410)
    assert len(engine.view['network_members'][0]['history']) == 2
