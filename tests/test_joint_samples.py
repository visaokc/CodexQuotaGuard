import hashlib

import pytest

from quota_guard.cycle_statistics import cycle_statistics
from quota_guard.engine import Engine
from quota_guard.journal import Journal
from quota_guard.ledger import Ledger
from quota_guard.sample_pool import sample_checkpoints
from quota_guard.storage import Database, defaults
from quota_guard.web_controller import _view


ACCOUNT = 'a'*64


def peer(path):
    db = Database(path)
    ledger = Ledger(db)
    return db, ledger, Journal(db, ledger, 'one')


def records(path):
    db, ledger, one = peer(path)
    two = Journal(db, ledger, 'two')
    for journal in (one, two):
        journal.append(ACCOUNT, 'profile', dict(device=journal.device, name=journal.device, cap=50), 90)
    one.append(ACCOUNT, 'quota', dict(account=ACCOUNT, at=100, used=0, reset_at=10000), 100)
    for journal, tokens in ((one, 1000000), (two, 3000000)):
        event = dict(id=hashlib.sha256(journal.device.encode()).hexdigest(), device=journal.device,
                     account=ACCOUNT, ts=150, model='gpt-6-astra', tokens=tokens, weight=tokens, known=True)
        journal.append(ACCOUNT, 'events', [event], 160)
    one.append(ACCOUNT, 'quota', dict(account=ACCOUNT, at=200, used=10, reset_at=10000), 200)
    for journal in (one, two):
        journal.append(ACCOUNT, 'profile', dict(device=journal.device, name=journal.device,
                       cap=50, sample_checkpoint=dict(through=200)), 400)
    return one.since(ACCOUNT, {}, byte_limit=100000)


def test_same_joint_records_converge_without_local_heartbeats(tmp_path):
    facts = records(tmp_path/'origin.sqlite')
    results = []
    for index, ordering in enumerate((facts, list(reversed(facts)))):
        db, ledger, journal = peer(tmp_path/f'peer-{index}.sqlite')
        journal.merge(ACCOUNT, ordering)
        with db.connect() as conn:
            conn.execute('UPDATE devices SET scan_at=?', (index*1000,))
        ledger.ingest(dict(account=ACCOUNT, device='unused', name='unused', events=[], scan_at=0), 500)
        value = cycle_statistics(db, ACCOUNT, 500+index*17)['rows'][0]
        results.append({k: value[k] for k in ('total_tokens','sample_tokens','sample_percent',
            'sample_devices','sample_ready','sample_segments','source')})
        assert ledger.summary(ACCOUNT, 600)['token_budget']['total_tokens'] == value['total_tokens']
        db.put('statistics_start:'+ACCOUNT, 175)
        filtered = cycle_statistics(db, ACCOUNT, 600)['rows'][0]
        assert filtered['sampled_tokens'] == 0
        assert filtered['total_tokens'] == value['total_tokens']
    assert results[0] == results[1] == dict(total_tokens=40000000, sample_tokens=4000000,
        sample_percent=10, sample_devices=2, sample_ready=2, sample_segments=1, source='联合样本')


def test_checkpoint_waits_for_missing_events_and_late_corrections(tmp_path):
    facts = records(tmp_path/'origin.sqlite')
    missing = next(r for r in facts if r['origin']=='two' and r['kind']=='events')
    db, ledger, journal = peer(tmp_path/'peer.sqlite')
    journal.merge(ACCOUNT, [r for r in facts if r is not missing])
    value = cycle_statistics(db, ACCOUNT, 500)['rows'][0]
    assert value['total_tokens'] is None
    assert (value['sample_ready'], value['sample_devices']) == (1, 2)
    journal.merge(ACCOUNT, [missing])
    assert cycle_statistics(db, ACCOUNT, 500)['rows'][0]['total_tokens'] == 40000000
    two = Journal(db, ledger, 'two')
    extra = dict(missing['payload'][0], id='e'*64, ts=180, tokens=1000000, weight=1000000)
    two.append(ACCOUNT, 'events', [extra], 510)
    assert cycle_statistics(db, ACCOUNT, 520)['rows'][0]['total_tokens'] is None
    two.append(ACCOUNT, 'profile', dict(device='two', name='two', cap=50,
               sample_checkpoint=dict(through=200)), 530)
    assert cycle_statistics(db, ACCOUNT, 540)['rows'][0]['total_tokens'] == 50000000
    journal.merge(ACCOUNT, facts)
    assert cycle_statistics(db, ACCOUNT, 550)['rows'][0]['total_tokens'] == 50000000


def test_checkpoint_validation_and_account_scope(tmp_path):
    facts = records(tmp_path/'origin.sqlite')
    db, ledger, journal = peer(tmp_path/'peer.sqlite')
    journal.merge(ACCOUNT, facts)
    with db.connect() as conn:
        assert sample_checkpoints(conn, 'b'*64) is None
    for through in (True, float('nan'), -1, 450):
        with pytest.raises(ValueError, match='样本采集确认'):
            journal.append(ACCOUNT, 'profile', dict(device='one', name='one', cap=50,
                           sample_checkpoint=dict(through=through)), 500)


def test_sample_status_reaches_the_display_without_internal_facts(tmp_path):
    db, ledger, journal = peer(tmp_path/'peer.sqlite')
    journal.merge(ACCOUNT, records(tmp_path/'origin.sqlite'))
    cycles = cycle_statistics(db, ACCOUNT, 500)['rows']
    value = _view(dict(summary=ledger.summary(ACCOUNT, 500), analytics=dict(cycles=cycles)))
    assert value['summary']['token_budget']['sample_ready'] == 2
    assert value['analytics']['cycles'][0]['sample_devices'] == 2
    assert value['analytics']['cycles'][0]['sample_segments'] == 1


def test_engine_publishes_once_after_scan_and_outbox_complete(tmp_path, monkeypatch):
    config = defaults()
    config.update(device_id='one', tracked_accounts={ACCOUNT:dict(added_at=0,cap=50)})
    engine = Engine(Database(tmp_path/'local.sqlite'), config)
    engine.journal.merge(ACCOUNT, records(tmp_path/'origin.sqlite'))
    engine.recovery.complete = False
    engine.publish_sample_checkpoint(ACCOUNT, 500)
    assert engine.group_db.get('published_sample_checkpoint:'+ACCOUNT) is None
    engine.recovery.complete = True
    original = engine.scanner.pending
    monkeypatch.setattr(engine.scanner, 'pending', lambda **_: ['waiting'])
    engine.publish_sample_checkpoint(ACCOUNT, 500)
    assert engine.group_db.get('published_sample_checkpoint:'+ACCOUNT) is None
    monkeypatch.setattr(engine.scanner, 'pending', original)
    engine.publish_sample_checkpoint(ACCOUNT, 300)
    assert engine.group_db.get('published_sample_checkpoint:'+ACCOUNT) is None
    engine.publish_sample_checkpoint(ACCOUNT, 500)
    vector = engine.journal.vector(ACCOUNT)
    engine.publish_sample_checkpoint(ACCOUNT, 600)
    assert engine.journal.vector(ACCOUNT) == vector
    assert engine.group_db.get('published_sample_checkpoint:'+ACCOUNT)['through'] == 200
