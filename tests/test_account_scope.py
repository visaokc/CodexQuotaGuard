import json
from unittest.mock import patch

import pytest

from quota_guard.accounts import enroll
from quota_guard.engine import Engine
from quota_guard.meter import Scanner
from quota_guard.quota import identity, read_quota
from quota_guard.storage import Database, defaults
from test_core import A, B, FakeFirewall, line, usage


def ident(account=A, mode='account', revision=1):
    return dict(mode=mode, account=account, revision=revision, label='test-'+account[:1], plan='pro', multiplier=1)


def setup(tmp_path, tracked=(A, B)):
    home = tmp_path/'codex'
    (home/'sessions').mkdir(parents=True)
    cfg = defaults()
    cfg.update(codex_home=str(home), started_at=0, interval=1, device_id='one')
    for account in tracked:
        enroll(cfg, ident(account), now=0)
    current = [ident()]
    clock, used, resets = [100], {A: 20, B: 70}, {A: 10000, B: 20000}
    queries = []
    def quota(_):
        account = current[0]['account']
        queries.append(account)
        return dict(account=account, at=clock[0], used=used[account], reset_at=resets[account])
    engine = Engine(Database(tmp_path/'local.sqlite'), cfg, quota_reader=quota,
                    identity_reader=lambda _: current[0], firewall=FakeFirewall())
    def step(now):
        clock[0] = now
        engine.step(now)
    return engine, current, step, used, resets, queries, home


def append(path, data):
    with path.open('a') as f:
        f.write(data)


def test_enrollment_explicit_persistent_and_api_rejected(tmp_path):
    from quota_guard.pairing import save_config, load_config
    config = defaults()
    assert config['tracked_accounts'] == {}
    enroll(config, ident(), 100)
    enroll(config, ident(), 200)
    assert config['tracked_accounts'][A]['added_at'] == 100
    enroll(config, ident(B), 300)
    with pytest.raises(ValueError):
        enroll(config, ident('', 'api'))
    save_config(tmp_path/'settings.json', config)
    assert load_config(tmp_path/'settings.json')['tracked_accounts'] == config['tracked_accounts']


def test_unadded_account_no_quota_events_or_mesh(tmp_path):
    e, current, step, used, resets, queries, home = setup(tmp_path, tracked=(A,))
    current[0] = ident(B)
    step(100)
    append(home/'sessions'/'b.jsonl', line('session_meta', dict(id='b', model_provider='openai'), 110)+
           line('turn_context', dict(model='gpt-6-astra'), 120)+usage(1000, 130))
    step(140)
    assert not queries and not e.scanner.pending() and e.mesh is None
    assert e.snapshot()['summary'] is None
    assert e.journal.vector(A) == {} and e.journal.vector(B) == {}


def test_a_api_b_a_same_session_does_not_mix_tokens_or_quota(tmp_path):
    e, current, step, used, resets, queries, home = setup(tmp_path)
    path = home/'sessions'/'one.jsonl'
    step(100)
    append(path, line('session_meta', dict(id='one', model_provider='openai'), 105)+
           line('turn_context', dict(model='gpt-6-astra'), 110)+usage(1000, 120))
    used[A] = 24
    step(140)
    a = e.ledger.summary(A, 140)
    assert a['devices'][0]['tokens'] == 1100
    assert a['devices'][0]['estimated'] == 4
    current[0] = ident('', 'api')
    step(150)
    append(path, line('turn_context', dict(model='gpt-6-astra'), 160)+usage(5000, 170))
    before = len(queries)
    step(180)
    assert len(queries) == before and e.scanner.pending() == []
    assert e.ledger.summary(A, 180)['devices'][0]['tokens'] == 1100
    current[0] = ident(B)
    step(200)
    # An unbound API continuation arriving after switching to B is not B's usage.
    append(path, usage(6000, 205))
    step(206)
    assert e.ledger.summary(B, 206)['devices'][0]['tokens'] == 0
    append(path, line('turn_context', dict(model='gpt-6-astra'), 210)+usage(7000, 220))
    used[B] = 74
    step(230)
    b = e.ledger.summary(B, 230)
    assert b['devices'][0]['tokens'] == 1100 and b['devices'][0]['estimated'] == 4
    assert b['epoch']['reset_at'] == 20000
    assert e.ledger.summary(A, 230)['epoch']['reset_at'] == 10000
    current[0] = ident(A)
    step(240)
    append(path, usage(8000, 250))
    step(255)
    assert e.ledger.summary(A, 255)['devices'][0]['tokens'] == 1100
    append(path, line('turn_context', dict(model='gpt-6-astra'), 260)+usage(9000, 270))
    used[A] = 25
    step(280)
    assert e.ledger.summary(A, 280)['devices'][0]['tokens'] == 2200
    assert e.ledger.summary(B, 280)['devices'][0]['tokens'] == 1100


def test_caps_and_early_resets_are_account_independent(tmp_path):
    e, current, step, used, resets, queries, home = setup(tmp_path)
    step(100)
    e.set_cap(15)
    step(110)
    a_cycle = e.ledger.summary(A, 110)['epoch']['cycle']
    current[0] = ident(B)
    step(120)
    e.set_cap(60)
    step(130)
    used[B], resets[B] = 0, 30000
    step(140)
    step(160)
    a, b = e.ledger.summary(A, 160), e.ledger.summary(B, 160)
    assert a['devices'][0]['cap'] == 15 and b['devices'][0]['cap'] == 60
    assert a['epoch']['cycle'] == a_cycle and a['epoch']['used'] == 20
    assert b['epoch']['used'] == 0 and b['epoch']['reset_at'] == 30000


def test_api_switch_during_quota_query_rejects_snapshot(tmp_path):
    e, current, step, used, resets, queries, home = setup(tmp_path)
    def changed(_):
        current[0] = ident('', 'api')
        return dict(account=A, used=80, at=100, reset_at=10000)
    e.quota_reader = changed
    step(100)
    assert e.ledger.summary(A, 100)['epoch'] is None
    assert e.snapshot()['summary'] is None and not e.firewall.calls


def test_login_switch_during_log_scan_quarantines_new_events(tmp_path):
    e, current, step, used, resets, queries, home = setup(tmp_path)
    step(100)
    path = home/'sessions'/'one.jsonl'
    append(path, line('session_meta', dict(id='one'), 110)+
           line('turn_context', dict(model='gpt-6-astra'), 120)+usage(1000, 130))
    original = e.scanner.scan
    def switching(account, multiplier=1):
        result = original(account, multiplier)
        if account:
            current[0] = ident(B)
        return result
    with patch.object(e.scanner, 'scan', switching):
        step(140)
    assert not e.scanner.pending()
    assert e.ledger.summary(A, 140)['devices'][0]['tokens'] == 0
    with e.db.connect() as db:
        assert db.execute('SELECT sent FROM outbox').fetchone()[0] == 2
    current[0] = ident(A)
    e.db.put('published_group:'+A, 'changed')
    step(160)
    assert not e.scanner.pending()


def test_startup_cannot_keep_other_accounts_block(tmp_path):
    e, current, step, used, resets, queries, home = setup(tmp_path)
    e.db.put('block_state', dict(account=A, cycle='old', cap=33))
    e.blocked = True
    current[0] = ident(B)
    step(100)
    assert not e.blocked and e.firewall.calls == ['resume', 'restore']


def test_same_account_config_revision_breaks_ambiguous_interval(tmp_path):
    e, current, step, used, resets, queries, home = setup(tmp_path)
    step(100)
    path = home/'sessions'/'one.jsonl'
    append(path, line('session_meta', dict(id='one'), 110)+
           line('turn_context', dict(model='gpt-6-astra'), 120)+usage(1000, 130))
    # A -> API -> A can occur between polls; config modification is still detected.
    current[0] = ident(A, revision=2)
    step(140)
    assert not e.scanner.pending() and e.ledger.summary(A, 140)['devices'][0]['tokens'] == 0


def test_profile_and_openai_custom_endpoint_are_api(tmp_path):
    (tmp_path/'auth.json').write_text(json.dumps(dict(tokens=dict(account_id='a', access_token='dummy'))))
    for config in ('[model_providers.openai]\nbase_url="https://relay.example.com/v1"',
                   'profile="relay"\n[profiles.relay]\nmodel_provider="custom"'):
        (tmp_path/'config.toml').write_text(config)
        assert identity(tmp_path)['mode'] == 'api'


def test_quota_request_does_not_use_other_accounts_or_api_credentials(tmp_path):
    (tmp_path/'auth.json').write_text(json.dumps(dict(tokens=dict(account_id='b', access_token='dummy'))))
    with patch('quota_guard.quota.urllib.request.build_opener', side_effect=AssertionError('No HTTP allowed')):
        with pytest.raises(RuntimeError, match='已停止额度请求'):
            read_quota(tmp_path, expected_account=A)
        (tmp_path/'config.toml').write_text('model_provider="custom"')
        with pytest.raises(RuntimeError, match='已停止额度请求'):
            read_quota(tmp_path)


def test_unattended_quota_failure_notifies_once_without_false_reset(tmp_path):
    e, current, step, used, resets, queries, home = setup(tmp_path)
    step(100)
    old = e.ledger.summary(A, 100)['epoch']['cycle']
    def failure(_):
        raise RuntimeError('expired login')
    e.quota_reader = failure
    step(110)
    assert not e.snapshot()['notifications']
    step(290)
    assert len(e.snapshot()['notifications']) == 1
    step(400)
    assert not e.snapshot()['notifications']
    assert e.ledger.summary(A, 400)['epoch']['cycle'] == old
