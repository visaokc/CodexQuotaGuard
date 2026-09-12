import pytest

from test_clean_start import setup_clean
from test_maintenance_reporting import toggle, view
from test_quota_estimation import calibrated_group
from test_shared_billing import A, B, add_event, close_samples


def test_donut_shared_estimates_conserve_and_official_confirmation_replaces_them(tmp_path):
    db, journals = calibrated_group(tmp_path)
    toggle(db, journals, 'one', True, 410)
    add_event(journals, account=B, at=450)
    data = view(db)
    pie = data['donut_windows']['today']
    assert sum(r['tokens'] for r in pie['rows']) == 2200
    assert sum(r['quota'] for r in pie['quota_rows']) == pytest.approx(3)
    assert sorted(r['quota'] for r in pie['quota_estimate_rows']) == pytest.approx([1, 1, 1])
    assert sum(r['shared_quota'] for r in pie['quota_estimate_rows']) == pytest.approx(3)
    assert pie['quota_pending_rows'] == []
    assert not any(r.get('maintenance') for r in pie['rows'])
    journals['one'].append(B, 'quota', dict(account=B, used=7, reset_at=850, at=510), 510)
    close_samples(journals, at=650)
    pie = view(db, 650)['donut_windows']['today']
    assert sum(r['quota'] for r in pie['quota_rows']) == pytest.approx(7)
    assert sum(r['shared_quota'] for r in pie['quota_rows']) == pytest.approx(4)
    assert pie['quota_estimate_rows'] == []
    assert pie['quota_pending_rows'] == []


def test_partial_estimation_keeps_unresolved_recipient_pending(tmp_path):
    db, journals = calibrated_group(tmp_path)
    toggle(db, journals, 'one', True, 410)
    add_event(journals, account=B, at=450)
    add_event(journals, account=B, at=470, model='unknown-model')
    pie = view(db)['donut_windows']['today']
    assert sum(r['quota'] for r in pie['quota_estimate_rows']) == pytest.approx(3)
    assert {r['device'] for r in pie['quota_pending_rows']} == {'person1', 'person2', 'person3'}
    assert {r['model'] for r in pie['quota_pending_rows']} == {'unknown-model'}


def test_account1_archive_keeps_account2_early_and_estimated_usage(tmp_path):
    db, journals, _ = setup_clean(tmp_path)
    add_event(journals, account=A, at=150)
    add_event(journals, account=B, at=180)
    journals['one'].append(A, 'quota', dict(account=A, used=100, reset_at=800, at=200), 200)
    journals['one'].append(B, 'quota', dict(account=B, used=3, reset_at=850, at=210), 210)
    close_samples(journals)
    add_event(journals, account=B, at=450)
    with db.connect() as con:
        original = [tuple(r) for r in con.execute('SELECT * FROM events ORDER BY id')]
    result = view(db)
    pie = result['donut_windows']['today']
    assert {r['account'] for r in pie['rows']} == {B}
    assert sum(r['tokens'] for r in pie['rows']) == 2200
    assert sum(r['quota'] for r in pie['quota_estimate_rows']) > 0
    assert {r['account'] for r in pie['quota_estimate_rows']} == {B}
    assert sum(r['quota'] for r in pie['quota_estimate_rows']) == pytest.approx(
        sum(r['quota'] for r in result['live_reporting']['events'] if r['account'] == B))
    with db.connect() as con:
        assert original == [tuple(r) for r in con.execute('SELECT * FROM events ORDER BY id')]
