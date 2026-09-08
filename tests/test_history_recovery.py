import json

import pytest

from quota_guard.recovery import HistoryRecovery
from test_account_scope import append, setup
from test_core import A, B, event, ledger, line, register, snap, usage


def witnessed(total, ts, used=21, reset=10000):
    row = json.loads(usage(total, ts))
    row['payload']['rate_limits'] = dict(limit_id='codex', primary=dict(
        used_percent=used, window_minutes=10080, resets_at=reset))
    return json.dumps(row)+'\n'


def history(tmp_path, provider='openai'):
    e, current, step, *_rest, home = setup(tmp_path)
    e.journal.append(A, 'quota', snap(100, 20), 100)
    e.journal.append(A, 'quota', snap(150, 21), 150)
    path = home/'sessions'/'old.jsonl'
    append(path, line('session_meta', dict(id='old', model_provider=provider), 90)+
           line('turn_context', dict(model='gpt-6-astra'), 110)+witnessed(1000, 140))
    recovery = HistoryRecovery(e.db, e.group_db, e.scanner.home, 'one')
    return e, recovery, path


def test_upgrade_automatically_recovers_seeded_unbound_turn_and_syncs(tmp_path):
    e, recovery, path = history(tmp_path)
    e.scanner.seed()
    e.step(160)
    summary = e.ledger.summary(A, 400)
    assert summary['unassigned'] == 0
    assert summary['devices'][0]['tokens'] == 1100
    assert e.db.get('history_recovery:'+A)['recovered_tokens'] == 1100
    facts = e.journal.since(A, {})
    assert any(r['kind'] == 'events' and r['payload'][0].get('attribution') == 'quota_correlation' for r in facts)
    e.step(170)
    assert e.ledger.summary(A, 400)['devices'][0]['tokens'] == 1100


def test_replay_deduplicates_archive_and_normal_outbox(tmp_path):
    e, recovery, path = history(tmp_path)
    recovery.scan(0)
    assert recovery.reconcile(A, 0)['recovered_events'] == 1
    archive = e.scanner.home/'archived_sessions'
    archive.mkdir()
    path.rename(archive/'moved.jsonl')
    recovery.scan(0)
    assert recovery.reconcile(A, 0)['recovered_events'] == 1
    assert len(e.scanner.pending()) == 1
    resumed = HistoryRecovery(e.db, e.group_db, e.scanner.home, 'one')
    resumed.scan(0)
    assert resumed.reconcile(A, 0)['recovered_events'] == 1
    assert len(e.scanner.pending()) == 1


@pytest.mark.parametrize('problem', ['provider', 'reset', 'used', 'stale', 'ambiguous', 'enrollment', 'rejected'])
def test_no_guessed_account_or_scope_override(tmp_path, problem):
    e, recovery, path = history(tmp_path, provider='custom' if problem == 'provider' else 'openai')
    if problem in ('reset', 'used', 'stale'):
        data = path.read_text()
        if problem == 'reset':
            data = data.replace('"resets_at": 10000', '"resets_at": 20000')
        elif problem == 'used':
            data = data.replace('"used_percent": 21', '"used_percent": 88')
        else:
            data = data.replace('1970-01-01T00:02:20', '1970-01-01T00:12:20')
        path.write_text(data)
    elif problem == 'ambiguous':
        e.journal.append(B, 'quota', snap(150, 21, account=B), 150)
    recovery.scan(0)
    if problem == 'rejected':
        recovery.reconcile(A, 0)
        e.scanner.reject_since(0)
    recovery.reconcile(A, 145 if problem == 'enrollment' else 0)
    assert e.scanner.pending() == []


def test_late_snapshot_retries_previously_unmatched_history(tmp_path):
    e, recovery, path = history(tmp_path)
    path.write_text(path.read_text().replace('"used_percent": 21', '"used_percent": 22'))
    recovery.scan(0)
    assert recovery.reconcile(A, 0)['recovered_events'] == 0
    e.journal.append(A, 'quota', snap(165, 22), 165)
    assert recovery.reconcile(A, 0)['recovered_events'] == 1


def test_outage_with_identical_bracketing_snapshots_can_be_recovered(tmp_path):
    e, recovery, path = history(tmp_path)
    path.write_text(path.read_text().replace('1970-01-01T00:02:20', '1970-01-01T00:12:20'))
    recovery.scan(0)
    assert recovery.reconcile(A, 0)['recovered_events'] == 0
    e.journal.append(A, 'quota', snap(1300, 21), 1300)
    assert recovery.reconcile(A, 0)['recovered_events'] == 1


def test_reset_second_rounding_does_not_drop_usage(tmp_path):
    e, recovery, path = history(tmp_path)
    path.write_text(path.read_text().replace('"resets_at": 10000', '"resets_at": 10001'))
    recovery.scan(0)
    assert recovery.reconcile(A, 0)['recovered_events'] == 1


def test_reset_transition_jitter_uses_same_cycle_tolerance_as_ledger(tmp_path):
    e, recovery, path = history(tmp_path)
    path.write_text(path.read_text().replace('"resets_at": 10000', '"resets_at": 9986'))
    recovery.scan(0)
    assert recovery.reconcile(A, 0)['recovered_events'] == 1


def test_reset_tolerance_never_selects_between_two_accounts(tmp_path):
    e, recovery, path = history(tmp_path)
    e.journal.append(B, 'quota', snap(150, 21, reset=10060, account=B), 150)
    recovery.scan(0)
    assert recovery.reconcile(A, 0)['recovered_events'] == 0


def test_paginated_tail_does_not_claim_whole_previous_session(tmp_path):
    e, recovery, path = history(tmp_path)
    data = path.read_text().replace('"id": "old"', '"id": "old", "history_base": {"end_ordinal_exclusive": 100}')
    path.write_text(data)
    recovery.scan(0)
    assert recovery.reconcile(A, 0)['recovered_tokens'] == 0
    append(path, witnessed(2000, 145))
    recovery.scan(0)
    assert recovery.reconcile(A, 0)['recovered_tokens'] == 1100


def test_recovery_uses_historical_tier_not_current_fast_mode(tmp_path):
    e, recovery, path = history(tmp_path)
    recovery.scan(0)
    recovery.reconcile(A, 0)
    assert e.scanner.pending()[0]['weight'] == pytest.approx(.2625)


def test_other_device_upgrade_recovers_itself_and_converges(tmp_path):
    first, _, _ = history(tmp_path/'first')
    second, _, path = history(tmp_path/'second')
    path.write_text(path.read_text().replace('"id": "old"', '"id": "other-session"'))
    # Recreate the second engine with its own persistent device identity.
    from quota_guard.engine import Engine
    from test_core import FakeFirewall
    second.config['device_id'] = 'two'
    second = Engine(second.db, second.config, quota_reader=second.quota_reader,
                    identity_reader=second.identity_reader, firewall=FakeFirewall())
    for e in (first, second):
        e.scanner.seed()
        e.step(160)
    for e, other in ((first, second), (second, first)):
        e.journal.merge(A, other.journal.since(A, e.journal.vector(A)))
    for e in (first, second):
        s = e.ledger.summary(A, 400)
        assert s['unassigned'] == 0
        assert {d['id']: d['tokens'] for d in s['devices']} == {'one': 1100, 'two': 1100}
        assert {d['id']: d['estimated'] for d in s['devices']} == {'one': .5, 'two': .5}


def test_partial_line_and_repeated_scan_do_not_lose_usage(tmp_path):
    e, recovery, path = history(tmp_path)
    append(path, witnessed(2000, 145)[:-1])
    recovery.scan(0)
    assert recovery.reconcile(A, 0)['recovered_tokens'] == 1100
    append(path, '\n')
    recovery.scan(0)
    assert recovery.reconcile(A, 0)['recovered_tokens'] == 2200
    recovery.scan(0)
    assert recovery.reconcile(A, 0)['recovered_tokens'] == 2200


def test_bulk_inference_provenance_fits_real_journal_record_limit(tmp_path):
    e, _, path = history(tmp_path)
    from quota_guard.engine import Engine
    from test_core import FakeFirewall
    e.config['device_id'] = 'd'*100
    e = Engine(e.db, e.config, quota_reader=e.quota_reader, identity_reader=e.identity_reader, firewall=FakeFirewall())
    append(path, line('turn_context', dict(model='m'*100), 141))
    for i in range(2, 62):
        append(path, usage(i*1000, 140+i))
    e.scanner.seed()
    e.step(300)
    assert not e.scanner.pending()
    assert e.db.get('history_recovery:'+A)['recovered_events'] == 61
    assert sum(d['tokens'] for d in e.ledger.summary(A, 400)['devices']) == 67100


def test_one_confirmed_token_can_anchor_continuous_session(tmp_path):
    e, recovery, path = history(tmp_path)
    append(path, usage(2000, 145))
    recovery.scan(0)
    result = recovery.reconcile(A, 0)
    assert result['unresolved_events'] == 0
    assert result['inferred_tokens'] == 1100
    assert sum(x['tokens'] for x in e.scanner.pending()) == 2200
    assert e.scanner.pending()[-1]['attribution'] == 'session_inference'


def test_same_account_endpoint_tokens_allow_interval_inference(tmp_path):
    e, recovery, path = history(tmp_path)
    append(path, usage(2000, 142)+witnessed(3000, 145))
    recovery.scan(0)
    result = recovery.reconcile(A, 0)
    assert result['recovered_tokens'] == 3300
    events = sorted(e.scanner.pending(), key=lambda x: x['ts'])
    assert events[1]['attribution'] == 'interval_inference'
    assert events[1]['evidence']['anchors'] == [events[0]['id'], events[2]['id']]


@pytest.mark.parametrize('kind', ['provider', 'account', 'rejected', 'quota'])
def test_session_inference_stops_at_conflict(tmp_path, kind):
    e, recovery, path = history(tmp_path)
    if kind == 'provider':
        append(path, line('turn_context', dict(model='gpt-6-astra', model_provider='custom'), 141)+
               usage(2000, 142)+line('turn_context', dict(model='gpt-6-astra', model_provider='openai'), 143))
    elif kind == 'quota':
        append(path, witnessed(2000, 142, used=88))
    else:
        append(path, witnessed(2000, 142))
        recovery.scan(0)
        recovery.reconcile(A, 0)
        with e.db.connect() as db:
            r = db.execute("SELECT id,payload FROM outbox WHERE json_extract(payload,'$.ts')=142").fetchone()
            data = json.loads(r['payload'])
            if kind == 'account':
                data['account'] = B
            db.execute('UPDATE outbox SET payload=?,sent=? WHERE id=?', (json.dumps(data), 2 if kind == 'rejected' else 1, r['id']))
    append(path, usage(3000, 145))
    recovery.scan(0)
    result = recovery.reconcile(A, 0)
    assert result['unresolved_events'] >= 1
    assert all(x['ts'] != 145 for x in e.scanner.pending())


def test_current_login_alone_does_not_rewrite_unanchored_old_history(tmp_path):
    e, recovery, path = history(tmp_path)
    path.write_text(line('session_meta', dict(id='unconfirmed', model_provider='openai'), 90)+
                    line('turn_context', dict(model='gpt-6-astra'), 110)+usage(1000, 140))
    recovery.scan(0)
    assert recovery.reconcile(A, 0)['unresolved_tokens'] == 1100
    assert not e.scanner.pending()


def test_incomplete_history_never_reconstructs_inflated_delta(tmp_path):
    e, recovery, path = history(tmp_path)
    # Simulate bounded first read stopping before earlier cumulative counters.
    data = path.read_bytes()
    recovery.scan(0, byte_budget=30)
    assert recovery.reconcile(A, 0)['recovered_events'] == 0
    for _ in range(20):
        if recovery.scan(0, byte_budget=200):
            break
    assert recovery.reconcile(A, 0)['recovered_tokens'] == 1100
    assert path.read_bytes() == data


def test_sole_device_unknown_weight_is_not_unknown_device(tmp_path):
    _, l = ledger(tmp_path)
    register(l)
    l.observe(snap(100, 20))
    l.ingest(dict(account=A, device='one', name='one', events=[event(known=False, weight=0)]), 160)
    l.observe(snap(200, 24))
    s = l.summary(A, 400)
    assert s['unassigned'] == 0
    assert s['devices'][0]['estimated'] == 4
    assert s['devices'][0]['unknown_tokens'] == 1000
    assert s['calibration'] is None


def test_unresolved_quota_has_actionable_window_and_device_evidence(tmp_path):
    _, l = ledger(tmp_path)
    for d in ('one', 'two'):
        register(l, d)
    l.observe(snap(100, 20))
    l.observe(snap(200, 21))
    for d, known in [('one', False), ('two', True)]:
        l.ingest(dict(account=A, device=d, name=d, events=[event(d, 250, known=known)]), 260)
    l.observe(snap(300, 22))
    s = l.summary(A, 500)
    assert s['unassigned'] == 2
    # Cycle-wide allocation reports one unresolved cycle, not independent windows.
    assert [x['reason'] for x in s['attribution_gaps']] == ['unknown_weight']
    assert s['attribution_gaps'][0]['start'] == 100
    assert set(s['attribution_gaps'][0]['devices']) == {'one', 'two'}


def test_unresolved_diagnostics_do_not_label_missing_evidence_as_api(tmp_path):
    e, recovery, path = history(tmp_path)
    path.write_text(path.read_text().replace('"used_percent": 21', '"used_percent": 22'))
    recovery.scan(0)
    result = recovery.reconcile(A, 0)
    assert result['unresolved_events'] == 1
    assert sum(v['tokens'] for v in result['unresolved_reasons'].values()) == result['unresolved_tokens']
    assert all('api' not in reason for reason in result['unresolved_reasons'])
    e.journal.append(A, 'quota', snap(165, 22), 165)
    result = recovery.reconcile(A, 0)
    assert result['unresolved_reasons'] == {}
    assert result['recovered_events'] == 1
