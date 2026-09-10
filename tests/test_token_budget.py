import hashlib

import pytest

from quota_guard.journal import Journal
from quota_guard.ledger import Ledger
from quota_guard.storage import Database
from quota_guard.token_budget import budget_text, compact_tokens
from quota_guard.cycle_statistics import cycle_statistics


A, B = 'a'*64, 'b'*64


def setup_ledger(tmp_path, baseline=0):
    ledger = Ledger(Database(tmp_path/'budget.sqlite'))
    ledger.observe(dict(account=A, at=100, used=baseline, reset_at=10000))
    return ledger


def upload(ledger, device, tokens, at=150, account=A):
    event = dict(id=hashlib.sha256(f'{device}:{at}:{account}'.encode()).hexdigest(),
                 account=account, device=device, ts=at, model='unknown',
                 tokens=tokens, weight=0, known=False)
    ledger.ingest(dict(account=account, device=device, name=device, events=[event], scan_at=400), max(400, at))


def observe(ledger, used, at=200, reset=10000):
    ledger.observe(dict(account=A, at=at, used=used, reset_at=reset))


def budget(ledger, now=500):
    return ledger.summary(A, now)['token_budget']


def test_idle_profile_watermark_does_not_change_peer_cycle_estimate(tmp_path):
    ledger = setup_ledger(tmp_path)
    upload(ledger, 'local', 1000000)
    upload(ledger, 'remote', 3000000)
    observe(ledger, 10)
    # Logs after the last increment must not replace the aligned sample.
    upload(ledger, 'local', 1000000, at=250)
    observe(ledger, 10, at=300)
    expected = cycle_statistics(ledger.db, A, 500)['rows'][0]
    assert expected['total_tokens'] == 40000000
    for watermark in (0, 190, 400):
        ledger.ingest(dict(account=A, device='old-unused', name='old',
                           events=[], scan_at=watermark), 400)
        actual = cycle_statistics(ledger.db, A, 500)['rows'][0]
        assert actual['total_tokens'] == expected['total_tokens']
        assert actual['source'] == expected['source'] == '同步样本'
        assert actual['sample_tokens'] == 4000000
        assert actual['sample_percent'] == 10


def test_all_devices_including_offline_not_other_accounts(tmp_path):
    ledger = setup_ledger(tmp_path)
    upload(ledger, 'local', 2000000)
    upload(ledger, 'remote', 8000000)
    upload(ledger, 'other-account', 90000000, account=B)
    ledger.logout('remote')
    observe(ledger, 20)
    result = budget(ledger)
    assert result['used_tokens'] == 10000000
    assert result['total_tokens'] == 50000000
    assert budget_text(result) == '本周期已同步 Token：10.00M'


def test_baseline_uses_matching_percent_delta(tmp_path):
    ledger = setup_ledger(tmp_path, baseline=40)
    upload(ledger, 'local', 1000000)
    upload(ledger, 'remote', 4000000)
    observe(ledger, 50)
    result = budget(ledger)
    assert result['sampled_tokens'] == 5000000
    assert result['total_tokens'] == 50000000
    assert result['used_tokens'] == 25000000


def test_late_upload_recalibrates_idempotently(tmp_path):
    ledger = setup_ledger(tmp_path)
    upload(ledger, 'local', 1000000)
    observe(ledger, 10)
    assert budget(ledger)['total_tokens'] == 10000000
    upload(ledger, 'remote', 3000000)
    upload(ledger, 'remote', 3000000)
    assert budget(ledger)['total_tokens'] == 40000000
    assert budget(ledger)['used_tokens'] == 4000000


def test_tokens_after_snapshot_do_not_inflate_total(tmp_path):
    ledger = setup_ledger(tmp_path)
    upload(ledger, 'local', 1000000)
    observe(ledger, 10)
    upload(ledger, 'local', 1000000, at=250)
    assert budget(ledger)['used_tokens'] == 1000000
    assert budget(ledger)['total_tokens'] == 10000000
    observe(ledger, 20, at=300)
    assert budget(ledger)['total_tokens'] == 10000000


def test_event_without_device_profile_is_counted(tmp_path):
    ledger = setup_ledger(tmp_path)
    journal = Journal(ledger.db, ledger, 'remote')
    event = dict(id='e'*64, account=A, device='remote', ts=150, model='unknown',
                 tokens=5000000, weight=0, known=False)
    journal.append(A, 'events', [event], now=160)
    observe(ledger, 10)
    assert ledger.summary(A, 500)['devices'] == []
    assert budget(ledger)['total_tokens'] == 50000000


def test_zero_percent_and_missing_sample_are_not_fixed_budgets(tmp_path):
    ledger = setup_ledger(tmp_path)
    assert budget(ledger)['total_tokens'] is None
    assert budget_text(budget(ledger)) == '本周期已同步 Token：0.00k'
    observe(ledger, 10)
    assert budget(ledger)['total_tokens'] is None
    assert budget_text(None) == '本周期已同步 Token：—'


def test_displayed_usage_never_falls_below_synchronized_device_logs(tmp_path):
    ledger = setup_ledger(tmp_path)
    upload(ledger, 'local', 10_000_000)
    observe(ledger, 10)
    upload(ledger, 'remote', 10_000_000, at=300)
    result = budget(ledger, now=500)
    assert result['used_tokens'] < result['sampled_tokens']
    assert budget_text(result) == '本周期已同步 Token：20.00M'


def test_nonzero_baseline_without_delta_cannot_infer_full_cycle(tmp_path):
    ledger = setup_ledger(tmp_path, baseline=60)
    upload(ledger, 'local', 1000000)
    assert budget(ledger)['used_tokens'] is None
    assert budget(ledger)['total_tokens'] is None


def test_pending_and_confirmed_reset_do_not_reuse_previous_cycle(tmp_path):
    ledger = setup_ledger(tmp_path)
    upload(ledger, 'local', 2000000)
    observe(ledger, 20)
    assert budget(ledger)['total_tokens'] == 10000000
    observe(ledger, 0, at=300, reset=20000)
    assert budget(ledger)['total_tokens'] == 10000000
    assert budget(ledger)['used_tokens'] is None
    observe(ledger, 0, at=320, reset=20000)
    assert budget(ledger)['used_tokens'] == 0
    assert budget(ledger)['total_tokens'] == 10000000
    upload(ledger, 'remote', 1000000, at=350)
    observe(ledger, 5, at=400, reset=20000)
    assert budget(ledger, now=600)['total_tokens'] == 20000000


def test_period_start_and_future_tokens_are_excluded(tmp_path):
    ledger = setup_ledger(tmp_path)
    upload(ledger, 'local', 9000000, at=100)
    upload(ledger, 'local', 1000000, at=150)
    upload(ledger, 'remote', 8000000, at=550)
    observe(ledger, 10)
    assert budget(ledger)['sampled_tokens'] == 1000000
    assert budget(ledger)['total_tokens'] == 10000000


@pytest.mark.parametrize('value, expected', [(0, '0.00k'), (999, '1.00k'),
    (12345, '12.35k'), (999994, '999.99k'), (999995, '1.00M'), (1000000, '1.00M'),
    (1234567890, '1234.57M')])
def test_compact_units(value, expected):
    assert compact_tokens(value) == expected


def test_missing_device_keeps_calibration_and_estimates_shared_use(tmp_path):
    ledger = setup_ledger(tmp_path)
    upload(ledger, 'local', 1000000)
    upload(ledger, 'remote', 4000000)
    observe(ledger, 10)
    assert budget(ledger)['total_tokens'] == 50000000
    ledger.logout('remote')
    upload(ledger, 'local', 1000000, at=450)
    observe(ledger, 30, at=500)
    result = budget(ledger, now=700)
    assert result['total_tokens'] == 50000000
    assert result['used_tokens'] == 15000000
    assert result['sampled_tokens'] == 6000000
    # Both devices finish syncing; the same past interval is recalibrated.
    upload(ledger, 'remote', 14000000, at=450)
    for device in ('local', 'remote'):
        ledger.ingest(dict(account=A, device=device, name=device, events=[], scan_at=700), 700)
    assert budget(ledger, now=700)['total_tokens'] == pytest.approx(20000000*100/30)


def test_calibration_survives_restart_and_account_isolation(tmp_path):
    ledger = setup_ledger(tmp_path)
    upload(ledger, 'local', 1000000)
    observe(ledger, 10)
    assert budget(ledger)['total_tokens'] == 10000000
    restarted = Ledger(Database(tmp_path/'budget.sqlite'))
    observe(restarted, 30, at=600)
    assert budget(restarted, now=800)['used_tokens'] == 3000000
    assert restarted.db.get('token_budget:'+B) is None


def test_no_sample_does_not_fabricate_initial_estimate(tmp_path):
    ledger = setup_ledger(tmp_path, baseline=40)
    observe(ledger, 50)
    assert budget(ledger)['total_tokens'] is None
