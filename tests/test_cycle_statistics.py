import hashlib

import pytest

from quota_guard.cycle_statistics import cycle_statistics
from quota_guard.journal import Journal
from quota_guard.ledger import Ledger
from quota_guard.storage import Database


A, B = 'a'*64, 'b'*64


def setup(tmp_path):
    database = Database(tmp_path/'cycles.sqlite')
    ledger = Ledger(database)
    return database, ledger


def observe(ledger, at, used, reset=1000):
    ledger.observe(dict(account=A, at=at, used=used, reset_at=reset))


def upload(ledger, at, tokens, device='local', model='gpt-6-astra', account=A):
    event = dict(id=hashlib.sha256(f'{account}:{device}:{at}'.encode()).hexdigest(),
        account=account, device=device, ts=at, model=model, tokens=tokens, weight=1, known=True)
    ledger.ingest(dict(account=account, device=device, name=device, events=[event], scan_at=5000), 5000)


def test_formal_start_excludes_trial_and_second_cycle_keeps_first(tmp_path):
    db, ledger = setup(tmp_path)
    observe(ledger, 100, 0)
    upload(ledger, 150, 9000)
    observe(ledger, 200, 90)
    observe(ledger, 1000, 0, 2000)
    db.put('statistics_start:'+A, 1000)
    upload(ledger, 1000, 7000)
    upload(ledger, 1050, 1000)
    upload(ledger, 1050, 8000, account=B)
    observe(ledger, 1100, 10, 2000)
    first = cycle_statistics(db, A, 1500)['rows']
    assert len(first) == 1
    assert first[0]['sampled_tokens'] == 1000
    assert first[0]['total_tokens'] == 10000
    observe(ledger, 2000, 0, 3000)
    second = cycle_statistics(Database(tmp_path/'cycles.sqlite'), A, 2500)['rows']
    assert len(second) == 2
    assert second[1]['id'] == first[0]['id']
    assert second[1]['sampled_tokens'] == 1000
    assert second[0]['total_tokens'] is None
    assert second[0]['change_percent'] is None
    upload(ledger, 2050, 2000, model='gpt-5.6-sol')
    observe(ledger, 2100, 10, 3000)
    second = cycle_statistics(db, A, 2500)['rows']
    assert second[0]['total_tokens'] == 20000
    assert second[0]['change_percent'] == 100
    assert second[0]['models'] == [dict(model='gpt-5.6-sol', tokens=2000)]
    assert second[0]['sample_tokens'] == 2000
    assert second[0]['sample_percent'] == 10


@pytest.mark.parametrize('advanced', [False, True])
def test_early_reset_pending_and_confirmed_never_inherit(tmp_path, advanced):
    db, ledger = setup(tmp_path)
    observe(ledger, 100, 0)
    upload(ledger, 150, 1000)
    observe(ledger, 200, 10)
    before = cycle_statistics(db, A, 500)['rows'][0]
    assert before['total_tokens'] == 10000
    reset = 2000 if advanced else 1000
    observe(ledger, 600, 0, reset)
    pending = cycle_statistics(db, A, 650)['rows']
    assert len(pending) == 1
    assert pending[0]['total_tokens'] is None
    observe(ledger, 620, 0, reset)
    rows = cycle_statistics(db, A, 800)['rows']
    assert len(rows) == 2
    assert rows[0]['id'] != rows[1]['id']
    assert rows[0]['total_tokens'] is None
    assert rows[0]['sample_tokens'] is None
    assert rows[0]['change_percent'] is None
    assert rows[1]['total_tokens'] == 10000


def test_late_data_updates_closed_cycle_and_comparison_without_duplicate(tmp_path):
    db, ledger = setup(tmp_path)
    observe(ledger, 100, 0)
    upload(ledger, 150, 1000)
    observe(ledger, 200, 10)
    observe(ledger, 1000, 0, 2000)
    upload(ledger, 1050, 2000)
    observe(ledger, 1100, 10, 2000)
    before = cycle_statistics(db, A, 1500)['rows']
    assert before[0]['change_percent'] == 100
    assert before[1]['sampled_tokens'] == 1000
    upload(ledger, 160, 1000, device='remote')
    upload(ledger, 160, 1000, device='remote')
    after = cycle_statistics(db, A, 1500)['rows']
    assert [r['id'] for r in after] == [r['id'] for r in before]
    assert after[1]['sampled_tokens'] == 2000
    assert after[1]['total_tokens'] == 20000
    assert after[0]['change_percent'] == 0


def test_journal_rebuild_preserves_cycle_identity(tmp_path):
    db, ledger = setup(tmp_path)
    journal = Journal(db, ledger, 'local')
    for at, used, reset in [(100, 0, 1000), (200, 10, 1000), (1000, 0, 2000)]:
        journal.append(A, 'quota', dict(account=A, at=at, used=used, reset_at=reset), at)
    before = cycle_statistics(db, A, 1500)['rows']
    with db.connect() as connection:
        old_ids = [r[0] for r in connection.execute('SELECT id FROM epochs ORDER BY started')]
    journal.project(A)
    with db.connect() as connection:
        new_ids = [r[0] for r in connection.execute('SELECT id FROM epochs ORDER BY started')]
    assert new_ids != old_ids
    assert cycle_statistics(db, A, 1500)['rows'] == before


def test_midcycle_start_keeps_only_actual_tokens_without_mismatched_estimate(tmp_path):
    db, ledger = setup(tmp_path)
    observe(ledger, 100, 20)
    upload(ledger, 150, 9000)
    upload(ledger, 250, 1000)
    observe(ledger, 300, 30)
    db.put('statistics_start:'+A, 200)
    row = cycle_statistics(db, A, 500)['rows'][0]
    assert row['sampled_tokens'] == 1000
    assert row['total_tokens'] is None


def test_empty_or_future_samples_do_not_create_estimate(tmp_path):
    db, ledger = setup(tmp_path)
    assert cycle_statistics(db, A, 500) == dict(rows=[])
    observe(ledger, 100, 20)
    upload(ledger, 600, 1000)
    observe(ledger, 200, 30)
    row = cycle_statistics(db, A, 500)['rows'][0]
    assert row['sampled_tokens'] == 0
    assert row['total_tokens'] is None
    assert row['source'] == '等待样本'


def test_reference_uses_three_previous_estimated_cycles_and_recalculates_late_data(tmp_path):
    db, ledger = setup(tmp_path)
    for index, tokens in enumerate([5000, 10000, 15000, 20000, 10000]):
        at = 100+index*900
        observe(ledger, at, 0, at+900)
        upload(ledger, at+50, tokens)
        observe(ledger, at+100, 50, at+900)
    rows = cycle_statistics(db, A, 4900)['rows']
    assert rows[0]['reference_total_tokens'] == 30000
    assert rows[0]['reference_starts'] == [1000, 1900, 2800]
    assert rows[0]['reduction_tokens'] == 10000
    assert rows[0]['reduction_percent'] == pytest.approx(100/3)
    assert rows[1]['reference_total_tokens'] == 20000
    assert rows[1]['reduction_tokens'] == -20000
    assert rows[-1]['reference_count'] == 0
    assert rows[-2]['reference_count'] == 1
    assert rows[-2]['reference_total_tokens'] == 10000
    assert rows[-3]['reference_count'] == 2
    assert rows[-3]['reference_total_tokens'] == 15000
    upload(ledger, 1051, 15000)
    updated = cycle_statistics(Database(db.path), A, 4900)['rows'][0]
    assert updated['reference_total_tokens'] == 40000
    assert updated['reduction_percent'] == 50
    db.put('statistics_start:'+A, 1900)
    cut = cycle_statistics(db, A, 4900)['rows'][0]
    assert cut['reference_count'] == 2
    assert cut['reference_total_tokens'] == 35000
    assert cut['reduction_percent'] == pytest.approx(15000/35000*100)


def test_reference_skips_closed_cycles_without_an_estimate(tmp_path):
    db, ledger = setup(tmp_path)
    for index, tokens in enumerate([5000, 0, 10000, 15000, 10000]):
        at = 100+index*900
        observe(ledger, at, 0, at+900)
        if tokens:
            upload(ledger, at+50, tokens)
            observe(ledger, at+100, 50, at+900)
    current = cycle_statistics(db, A, 4900)['rows'][0]
    assert current['reference_count'] == 3
    assert current['reference_starts'] == [100, 1900, 2800]
    assert current['reference_total_tokens'] == 20000
    assert current['reduction_percent'] == 0


def test_confirmed_cycle_label_is_read_from_replicated_profile(tmp_path):
    db,ledger=setup(tmp_path)
    observe(ledger,100,0)
    Journal(db,ledger,'local').append(A,'profile',dict(device='local',name='Local',cap=50,cycle_reset_type={'started':100,'type':'自然重置'}),200)
    assert cycle_statistics(db,A,300)['rows'][0]['reset_type']=='自然重置'
