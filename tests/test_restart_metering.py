import pytest

from quota_guard.engine import Engine
from test_account_scope import append, ident, setup
from test_core import A, B, FakeFirewall, line, usage


def restart(engine, current):
    result = Engine(engine.db, engine.config, quota_reader=engine.quota_reader,
                    identity_reader=lambda _: current[0], firewall=FakeFirewall())
    result.scanner.seed()
    return result


def running(tmp_path):
    e, current, step, *_, home = setup(tmp_path)
    e.scanner.seed()
    step(100)
    path = home/'sessions'/'running.jsonl'
    append(path, line('session_meta', dict(id='running', model_provider='openai'), 105)+
           line('turn_context', dict(model='gpt-6-astra'), 110)+usage(1000, 120))
    step(130)
    assert e.ledger.summary(A, 130)['devices'][0]['tokens'] == 1100
    return e, current, path


def test_restart_keeps_bound_turn_and_collects_downtime_without_duplicates(tmp_path):
    e, current, path = running(tmp_path)
    append(path, usage(2000, 140))
    resumed = restart(e, current)
    resumed.step(150)
    assert resumed.ledger.summary(A, 150)['devices'][0]['tokens'] == 2200
    append(path, usage(3000, 160))
    resumed.step(170)
    assert resumed.ledger.summary(A, 170)['devices'][0]['tokens'] == 3300
    again = restart(resumed, current)
    again.step(180)
    assert again.ledger.summary(A, 180)['devices'][0]['tokens'] == 3300


@pytest.mark.parametrize('change', ['revision', 'account', 'api', 'missing', 'enrollment', 'no_revision'])
def test_restart_requires_unchanged_scope_evidence(tmp_path, change):
    e, current, path = running(tmp_path)
    if change == 'revision':
        current[0] = ident(A, revision=2)
    elif change == 'account':
        current[0] = ident(B)
    elif change == 'api':
        current[0] = ident('', 'api')
    elif change == 'missing':
        e.db.put('scanner_scope', None)
    elif change == 'no_revision':
        current[0] = ident(A, revision=None)
    else:
        e.config['tracked_accounts'][A]['added_at'] = 135
    append(path, usage(2000, 140))
    resumed = restart(e, current)
    resumed.step(150)
    append(path, usage(3000, 160))
    resumed.step(170)
    assert resumed.ledger.summary(A, 170)['devices'][0]['tokens'] == 1100
    assert not resumed.scanner.pending()


def test_restart_collects_new_turn_started_during_downtime(tmp_path):
    e, current, path = running(tmp_path)
    append(path, line('turn_context', dict(model='gpt-6-astra'), 140)+usage(2000, 145))
    resumed = restart(e, current)
    resumed.step(150)
    assert resumed.ledger.summary(A, 150)['devices'][0]['tokens'] == 2200


def test_restart_does_not_bind_previously_unbound_turn(tmp_path):
    e, current, step, *_, home = setup(tmp_path)
    path = home/'sessions'/'running.jsonl'
    append(path, line('session_meta', dict(id='running', model_provider='openai'), 80)+
           line('turn_context', dict(model='gpt-6-astra'), 85)+usage(1000, 90))
    e.scanner.seed()
    step(100)
    append(path, usage(2000, 140))
    resumed = restart(e, current)
    resumed.step(150)
    assert resumed.ledger.summary(A, 150)['devices'][0]['tokens'] == 0


def test_restart_tokens_fill_quota_segment_with_real_revision_shape(tmp_path):
    e, current, step, used, _, _, home = setup(tmp_path)
    current[0] = ident(A, revision=(123456789, 987654321))
    e.scanner.seed()
    step(100)
    path = home/'sessions'/'running.jsonl'
    append(path, line('session_meta', dict(id='running', model_provider='openai'), 105)+
           line('turn_context', dict(model='gpt-6-astra'), 110)+usage(1000, 120))
    step(130)
    append(path, usage(2000, 140))
    resumed = restart(e, current)
    resumed.quota_reader = lambda _: dict(account=A, at=150, used=21, reset_at=10000)
    resumed.step(150)
    summary = resumed.ledger.summary(A, 300)
    assert summary['devices'][0]['tokens'] == 2200
    assert summary['devices'][0]['estimated'] == 1
    assert summary['unassigned'] == 0


def test_identity_race_invalidates_restart_evidence(tmp_path):
    e, current, path = running(tmp_path)
    original = e.scanner.scan
    def switching(account, multiplier=1):
        result = original(account, multiplier)
        if account:
            current[0] = ident(B)
        return result
    e.scanner.scan = switching
    append(path, usage(2000, 140))
    e.step(150)
    assert e.db.get('scanner_scope') is None
    current[0] = ident(A)
    resumed = restart(e, current)
    append(path, usage(3000, 160))
    resumed.step(170)
    assert resumed.ledger.summary(A, 170)['devices'][0]['tokens'] == 1100
