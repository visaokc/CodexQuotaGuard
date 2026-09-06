import hashlib
import json
from unittest.mock import patch

import pytest

from quota_guard.accounts import enroll
from quota_guard.engine import Engine
from quota_guard.quota import identity
from quota_guard.storage import Database, defaults
from test_core import FakeFirewall, line, usage


@pytest.mark.parametrize('target', ['provider', 'profile', 'custom_openai', 'apikey', 'other', 'tracked_other', 'logout'])
def test_reached_limit_switch_restores_before_scan_and_preserves_account_ledger(tmp_path, target):
    home = tmp_path/'home'
    (home/'sessions').mkdir(parents=True)
    auth = home/'auth.json'
    auth.write_text(json.dumps(dict(tokens=dict(account_id='first', access_token='fixture'))))
    cfg = defaults()
    cfg.update(codex_home=str(home), device_id='one', auto_block=True, started_at=0, interval=15,
               program_paths=[str(tmp_path/'codex.exe')])
    first = identity(home)
    enroll(cfg, first, now=0)
    other = hashlib.sha256(b'other').hexdigest()
    if target == 'tracked_other':
        enroll(cfg, dict(first, account=other, label='other'), now=0)
    clock, used = [100], [20]
    def quota(_):
        return dict(account=identity(home)['account'], at=clock[0], used=used[0], reset_at=10000)
    fw = FakeFirewall()
    e = Engine(Database(tmp_path/'local.sqlite'), cfg, quota_reader=quota, firewall=fw)
    e.step(100)
    path = home/'sessions'/'one.jsonl'
    path.write_text(line('session_meta', dict(id='one', model_provider='openai'), 110)+
                    line('turn_context', dict(model='gpt-6-astra'), 115)+usage(1000, 120))
    clock[0], used[0] = 150, 60
    e.step(150)
    clock[0] = 300
    e.step(300)  # Real ledger settlement, not a fabricated reached flag.
    assert e.blocked and fw.calls[-2:] == ['apply', 'pause']
    old = e.ledger.summary(first['account'], 300)
    old_tokens = old['devices'][0]['tokens']
    config = ''
    if target == 'provider': config = 'model_provider="relay"'
    if target == 'profile': config = 'profile="relay"\n[profiles.relay]\nmodel_provider="custom"'
    if target == 'custom_openai': config = '[model_providers.openai]\nbase_url="https://relay.invalid/v1"'
    (home/'config.toml').write_text(config)
    if target == 'apikey':
        auth.write_text(json.dumps(dict(auth_mode='apikey', OPENAI_API_KEY='fixture')))
    if target in ('other', 'tracked_other'):
        auth.write_text(json.dumps(dict(tokens=dict(account_id='other', access_token='fixture'))))
    if target == 'logout': auth.unlink()
    original = e.scanner.boundary
    def boundary(now):
        assert not e.blocked, 'Release must precede potentially slow log scanning'
        original(now)
    clock[0] = 301
    with patch.object(e.scanner, 'boundary', boundary):
        e.step(301)
    assert not e.blocked and not e.db.get('block_state')
    assert fw.calls[-2:] == ['resume', 'restore']
    assert e.config['auto_block']  # Preference remains on, applicability is account-scoped.
    assert e.ledger.summary(first['account'], 301)['devices'][0]['tokens'] == old_tokens
    assert e.ledger.summary(first['account'], 301)['epoch']['cycle'] == old['epoch']['cycle']
    if target != 'tracked_other':
        assert e.snapshot()['summary'] is None and e.mesh is None
    else:
        assert e.snapshot()['summary']['devices'][0]['tokens'] == 0
    # Returning to the same spent account must not let an API detour reset its cap.
    auth.write_text(json.dumps(dict(tokens=dict(account_id='first', access_token='fixture'))))
    (home/'config.toml').write_text('')
    clock[0] = 302
    e.step(302)
    assert e.blocked
    assert e.ledger.summary(first['account'], 302)['devices'][0]['tokens'] == old_tokens


def test_discovery_filters_real_executables_and_duplicates(tmp_path):
    from quota_guard.firewall import discover_programs
    exe = tmp_path/'codex.exe'
    exe.write_bytes(b'fixture-not-executable')
    missing = tmp_path/'missing.exe'
    with patch('quota_guard.firewall.powershell', return_value=json.dumps([str(exe), str(exe), str(missing), None])), \
         patch.dict('os.environ', {'LOCALAPPDATA': str(tmp_path)}):
        assert discover_programs() == [str(exe)]


def test_restart_with_auto_disabled_releases_old_restriction_even_without_quota(tmp_path):
    from test_account_scope import setup
    e, current, step, *_ = setup(tmp_path)
    e.config['auto_block'] = False
    e.db.put('block_state', dict(account=current[0]['account'], cycle='old', cap=33))
    e.blocked = True
    e.quota_reader = lambda _: (_ for _ in ()).throw(RuntimeError('offline'))
    step(100)
    assert not e.blocked and e.firewall.calls == ['resume', 'restore']


def test_switch_during_firewall_apply_restores_before_pause(tmp_path):
    from test_account_scope import setup, ident
    from test_core import A
    e, current, step, *_ = setup(tmp_path)
    step(100)
    e.config['auto_block'] = True
    def apply(_):
        e.firewall.calls.append('apply')
        current[0] = ident('', 'api')
    e.firewall.apply = apply
    summary = dict(account=A, epoch=dict(cycle='fixture', observed_at=101), reset_pending=False,
                   devices=[dict(id='one', estimated=34, settled=34, cap=33)])
    e.enforce(summary, 101)
    assert not e.blocked and e.firewall.calls == ['apply', 'resume', 'restore']
