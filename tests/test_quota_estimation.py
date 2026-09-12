import copy

import pytest

from test_shared_billing import A, B, add_event, close_samples, setup_group
from test_maintenance_reporting import toggle, view


def calibrated_group(tmp_path):
    db, journals, _ = setup_group(tmp_path)
    add_event(journals, account=B, at=150)
    journals['one'].append(B, 'quota', dict(account=B, used=3, reset_at=850, at=210), 210)
    close_samples(journals)
    return db, journals


def test_official_and_personal_use_identical_cache_weight_calibration(tmp_path):
    db, journals = calibrated_group(tmp_path)
    before = copy.deepcopy(view(db)['billing'])
    add_event(journals, account=B, at=450, cached=0)
    # Imported/stale local weights must not override the group's rate policy.
    with db.connect() as con:
        con.execute("UPDATE events SET weight=1 WHERE ts=450")
        events = [tuple(row) for row in con.execute('SELECT * FROM events ORDER BY id')]
    result = view(db)
    account = result['account_estimates'][B]
    personal = result['live_reporting']['balances']['person1']
    assert account['estimate_pending'] > 3  # Same tokens, fewer discounted cache hits.
    assert account['estimate_pending'] == pytest.approx(personal['estimate_pending'])
    assert account['remaining_estimate'] == pytest.approx(97-personal['estimate_pending'])
    assert result['billing'] == before
    with db.connect() as con:
        assert events == [tuple(row) for row in con.execute('SELECT * FROM events ORDER BY id')]


def test_official_confirmation_replaces_estimate_while_member_attribution_waits(tmp_path):
    db, journals = calibrated_group(tmp_path)
    add_event(journals, account=B, at=450)
    journals['one'].append(B, 'quota', dict(account=B, used=7, reset_at=850, at=460), 460)
    add_event(journals, account=B, at=470)
    result = view(db)
    assert result['billing']['status'] == 'syncing'
    assert result['account_estimates'][B]['estimate_pending'] == pytest.approx(3)
    assert result['account_estimates'][B]['remaining_estimate'] == pytest.approx(90)
    # The historical personal base has not deducted the unconfirmed 450 event.
    assert result['live_reporting']['balances']['person1']['estimate_pending'] == pytest.approx(6)
    close_samples(journals, at=650)
    result = view(db, 650)
    assert result['account_estimates'][B]['estimate_samples'] == 2
    assert result['account_estimates'][B]['estimate_pending'] == pytest.approx(3.5)
    assert result['live_reporting']['balances']['person1']['estimate_pending'] == pytest.approx(3.5)
    assert result['live_reporting']['balances']['person1']['available_estimate'] == pytest.approx(200/3-7-3.5)


def test_reset_candidate_blocks_both_estimates_without_touching_ledger(tmp_path):
    db, journals = calibrated_group(tmp_path)
    add_event(journals, account=B, at=450)
    with db.connect() as con:
        con.execute('INSERT INTO meta VALUES (?,?)', ('reset_candidate:'+B, '{}'))
    result = view(db)
    assert result['account_estimates'][B]['remaining_estimate'] is None
    assert result['account_estimates'][B]['estimate_missing']
    assert all(p['available_estimate'] is None and p['estimate_missing']
               for p in result['live_reporting']['balances'].values())
    assert result['billing']['status'] == 'active'


def test_missing_shared_maintenance_weight_blocks_all_recipients(tmp_path):
    db, journals = calibrated_group(tmp_path)
    toggle(db, journals, 'one', True, 410)
    add_event(journals, account=B, at=450, model='unknown-model')
    result = view(db)
    assert result['account_estimates'][B]['remaining_estimate'] is None
    assert all(p['estimate_missing'] and p['available_estimate'] is None
               for p in result['live_reporting']['balances'].values())


def test_estimated_maintenance_and_official_total_conserve_quota(tmp_path):
    db, journals = calibrated_group(tmp_path)
    toggle(db, journals, 'one', True, 410)
    add_event(journals, account=B, at=450)
    result = view(db)
    parts = [p['estimate_pending'] for p in result['live_reporting']['balances'].values()]
    assert parts == pytest.approx([1, 1, 1])
    assert sum(parts) == pytest.approx(result['account_estimates'][B]['estimate_pending'])


def test_confirmed_new_cycle_does_not_learn_old_cycle_rate(tmp_path):
    db, journals = calibrated_group(tmp_path)
    journals['one'].append(B, 'quota', dict(account=B, used=0, reset_at=1700, at=900), 900)
    add_event(journals, account=B, at=920)
    result = view(db, 950)
    assert result['account_estimates'][B]['estimate_samples'] == 0
    assert result['account_estimates'][B]['remaining_estimate'] is None
    assert result['live_reporting']['balances']['person1']['available_estimate'] is None


def test_later_confirmed_stream_is_not_duplicated_in_charts_when_earlier_gap_waits(tmp_path):
    db, journals = calibrated_group(tmp_path)
    add_event(journals, account=B, at=450, model='unknown-model')
    journals['one'].append(B, 'quota', dict(account=B, used=7, reset_at=850, at=460), 460)
    add_event(journals, account=B, device='two', at=470)
    journals['one'].append(B, 'quota', dict(account=B, used=12, reset_at=850, at=480), 480)
    close_samples(journals, at=650)
    result = view(db, 650)
    assert result['billing']['status'] == 'syncing'
    assert result['live_reporting']['balances']['person2']['estimate_pending'] == pytest.approx(5)
    assert result['live_reporting']['balances']['person2']['available_estimate'] == pytest.approx(200/3-5)
    assert result['live_reporting']['events'] == []
    assert sum(r['quota'] for r in result['windows']['cycle']['quota_rows']) == pytest.approx(8)
    assert result['windows']['cycle']['quota_estimate_rows'] == []


def test_official_anchor_excludes_already_included_baseline_events(tmp_path):
    from quota_guard.live_reporting import reports
    from quota_guard.shared_policy import load_rules
    from quota_guard.shared_quota import attribution, accounting

    db, journals = calibrated_group(tmp_path)
    add_event(journals, account=B, at=450)
    add_event(journals, account=B, at=470)
    rules = load_rules(db, [A, B], 500)
    attributed = attribution(db, rules, 500)
    # Migration anchors can be later than the last increment; the observation
    # already includes the earlier raw event in its baseline official balance.
    attributed['anchors'][B]['at'] = 460
    billing = accounting(rules, attributed, 500)
    result = reports(db, rules, attributed, billing, 500)
    assert result['accounts'][B]['estimate_at'] == 460
    assert result['accounts'][B]['estimate_pending'] == pytest.approx(3)
    assert result['balances']['person1']['estimate_pending'] == pytest.approx(3)


def test_history_options_keep_same_unconfirmed_quota_estimates(tmp_path):
    from quota_guard.shared_policy import load_rules
    from quota_guard.shared_view import shared_usage

    db, journals = calibrated_group(tmp_path)
    add_event(journals, account=B, at=450)
    journals['one'].append(B, 'quota', dict(account=B, used=7, reset_at=850, at=460), 460)
    add_event(journals, account=B, at=470)
    live = view(db)
    historical = shared_usage(db, 'group:test', {A:'a', B:'b'}, 500,
        rules=load_rules(db, [A, B], 500), hour_end=500, hour_buffer=True)
    assert historical['account_estimates'] == live['account_estimates']
    assert historical['windows']['cycle']['quota_estimate_rows'] == live['windows']['cycle']['quota_estimate_rows']
