import copy
import hashlib
import json
import time
from datetime import datetime, timezone

import pytest

from quota_guard.engine import Engine
from quota_guard.journal import Journal
from quota_guard.ledger import Ledger
from quota_guard.meter import Scanner, weighted
from quota_guard.pairing import Cipher, create_code, read_code, save_config, load_config
from quota_guard.quota import identity, normalize
from quota_guard.storage import Database, defaults

A = 'a'*64
B = 'b'*64


def snap(at, used, reset=10000, account=A):
    return dict(account=account, at=at, used=used, reset_at=reset)


def event(device='one', at=150, weight=1, known=True):
    return dict(id=hashlib.sha256(f'{device}-{at}'.encode()).hexdigest(), device=device, account=A,
                ts=at, model='gpt-6-astra', tokens=1000, weight=weight, known=known)


def ledger(tmp_path):
    db = Database(tmp_path/'test.sqlite')
    return db, Ledger(db)


def register(l, device='one', now=100):
    return l.ingest(dict(account=A, device=device, name=device, events=[], cap=33, scan_at=now), now)


def test_weights_cache_not_double_counted():
    assert weighted('gpt-6-astra', 1000, 900, 100, 1) == (.1725, True)
    assert weighted('missing', 1, 0, 1, 1) == (0, False)
    assert weighted('gpt-6-astra', 1000, 900, 100, 2)[0] == .345


@pytest.mark.parametrize('slot', ['primary_window', 'secondary_window'])
def test_weekly_window_is_not_assumed_secondary(slot):
    data = {'rate_limit': {slot: dict(used_percent=84, limit_window_seconds=604800, reset_at=10000)}}
    assert normalize(data, A, 100)['used'] == 84
    with pytest.raises(RuntimeError):
        normalize({'rate_limit': {}}, A, 100)


def test_account_identity_and_api_mode(tmp_path):
    (tmp_path/'auth.json').write_text(json.dumps(dict(tokens=dict(account_id='account', access_token='local'))))
    assert identity(tmp_path)['mode'] == 'account'
    (tmp_path/'config.toml').write_text('model_provider = "custom"')
    assert identity(tmp_path)['mode'] == 'api'
    (tmp_path/'config.toml').write_text('model_provider = "openai"')
    assert identity(tmp_path)['account'] == hashlib.sha256(b'account').hexdigest()
    (tmp_path/'auth.json').write_text(json.dumps(dict(OPENAI_API_KEY='dummy', tokens=dict(account_id='account', access_token='local'))))
    assert identity(tmp_path)['mode'] == 'api'


def test_allocation_and_late_upload(tmp_path):
    db, l = ledger(tmp_path)
    for d in ('one', 'two', 'three'):
        register(l, d)
    l.observe(snap(100, 20))
    l.observe(snap(200, 32))
    assert l.summary(A, 400)['unassigned'] == 12
    for d, w in [('one', 2), ('two', 1), ('three', 1)]:
        l.ingest(dict(account=A, device=d, name=d, events=[event(d, weight=w)], scan_at=400), 400)
    s = l.summary(A, 401)
    assert [d['estimated'] for d in s['devices']] == [6, 3, 3]
    assert s['unassigned'] == 0
    assert sum(d['estimated'] for d in s['devices']) == 12
    assert s['epoch']['baseline'] == 20


def test_unknown_model_does_not_erase_sole_device_ownership(tmp_path):
    db, l = ledger(tmp_path)
    register(l)
    l.observe(snap(100, 20))
    l.ingest(dict(account=A, device='one', name='one', events=[event(known=False)]), 160)
    l.observe(snap(200, 24))
    assert l.summary(A, 400)['unassigned'] == 0
    assert l.summary(A, 400)['devices'][0]['estimated'] == 4
    assert l.summary(A, 400)['devices'][0]['unknown_tokens'] == 1000


def test_resets_require_evidence_and_confirm_early(tmp_path):
    db, l = ledger(tmp_path)
    l.observe(snap(100, 80))
    original = l.summary(A, 200)['epoch']['cycle']
    assert l.summary(A, 10001)['epoch']['cycle'] == original
    l.observe(snap(200, 0, 20000))
    assert l.summary(A, 201)['reset_pending']
    l.observe(snap(230, 1, 20000))
    current = l.summary(A, 231)
    assert current['epoch']['cycle'] != original
    assert current['epoch']['baseline'] == 1
    assert not current['reset_pending']
    l.observe(snap(260, 80, 10000))
    assert l.summary(A, 260)['epoch']['used'] == 1


def test_scheduled_reset(tmp_path):
    _, l = ledger(tmp_path)
    l.observe(snap(100, 80, 200))
    l.observe(snap(201, 0, 200+604800))
    assert l.summary(A, 202)['epoch']['used'] == 0


def test_transient_drop_does_not_reset(tmp_path):
    _, l = ledger(tmp_path)
    l.observe(snap(100, 80))
    l.observe(snap(200, 78))
    l.observe(snap(230, 81))
    s = l.summary(A, 300)
    assert s['epoch']['baseline'] == 80
    assert not s['reset_pending']


def line(kind, payload, ts=100):
    return json.dumps(dict(type=kind, payload=payload, timestamp=datetime.fromtimestamp(ts, timezone.utc).isoformat()))+'\n'


def usage(total, ts):
    return line('event_msg', dict(type='token_count', info=dict(total_token_usage=dict(
        input_tokens=total, cached_input_tokens=total//2, output_tokens=total//10))), ts)


def test_scanner_baseline_partial_replay_archive(tmp_path):
    db = Database(tmp_path/'local.sqlite')
    home = tmp_path/'codex'
    sessions = home/'sessions'
    sessions.mkdir(parents=True)
    f = sessions/'test.jsonl'
    f.write_text(line('session_meta', dict(id='session'))+line('turn_context', dict(model='gpt-6-astra'))+usage(1000, 100))
    scan = Scanner(db, home, 'one', 150)
    scan.seed()
    assert scan.pending() == []
    with f.open('a') as out:
        out.write(line('turn_context', dict(model='gpt-6-astra'), 190)+usage(2000, 200)[:-1])
    scan.scan(A)
    assert scan.pending() == []
    with f.open('a') as out:
        out.write('\n'+usage(2000, 201))
    scan.scan(A)
    assert len(scan.pending()) == 1
    assert scan.pending()[0]['tokens'] == 1100
    archive = home/'archived_sessions'
    archive.mkdir()
    f.rename(archive/'moved.jsonl')
    scan.scan(A)
    assert len(scan.pending()) == 1
    assert scan.pending(account=B) == []


def test_scanner_activity_and_api_exclusion(tmp_path):
    db = Database(tmp_path/'local.sqlite')
    home = tmp_path/'codex'
    folder = home/'sessions'
    folder.mkdir(parents=True)
    scan = Scanner(db, home, 'one', 100)
    scan.seed()
    f = folder/'new.jsonl'
    f.write_text(line('session_meta', dict(id='s', model_provider='custom'))+
                 line('event_msg', dict(type='task_started'), 200)+usage(1000, 210))
    scan.scan(A)
    assert scan.pending() == []
    assert scan.activity(211) == (0, 0)
    assert scan.activity(400) == (0, 0)
    with f.open('a') as out:
        out.write(line('event_msg', dict(type='task_complete'), 410))
    scan.scan(A)
    assert scan.activity(420) == (0, 0)


def test_scanner_reasoning_is_part_of_output_and_keeps_event_identity(tmp_path):
    db=Database(tmp_path/'local.sqlite');home=tmp_path/'codex';folder=home/'sessions';folder.mkdir(parents=True)
    scan=Scanner(db,home,'one',100);scan.seed(A)
    def record(i,c,o,r,ts):
        last=(i,c,o,r) if ts==210 else (i-1000,c-800,o-100,r-70)
        return line('event_msg',dict(type='token_count',info=dict(total_token_usage=dict(input_tokens=i,cached_input_tokens=c,output_tokens=o,reasoning_output_tokens=r),last_token_usage=dict(zip(('input_tokens','cached_input_tokens','output_tokens','reasoning_output_tokens'),last)))),ts)
    f=folder/'new.jsonl';f.write_text(line('session_meta',dict(id='reasoning'))+line('turn_context',dict(model='gpt-6-astra'),200)+record(1000,800,100,70,210))
    scan.scan(A)
    with f.open('a') as out:out.write(record(2200,1700,300,220,220))
    scan.scan(A);events=sorted(scan.pending(),key=lambda e:e['ts'])
    assert [e['tokens'] for e in events]==[1100,1400]
    assert [e['reasoning_output_tokens'] for e in events]==[70,150]
    assert events[1]['input_tokens']==1200 and events[1]['cached_input_tokens']==900 and events[1]['output_tokens']==200
    assert events[0]['id']==hashlib.sha256(('reasoning'+json.dumps([1000,800,100])).encode()).hexdigest()
    scan.scan(A);assert len(scan.pending())==2


def test_truncated_counters_do_not_suppress_new_input_and_cache_or_repeat_archives(tmp_path):
    db=Database(tmp_path/'local.sqlite');home=tmp_path/'codex';folder=home/'sessions';folder.mkdir(parents=True)
    scan=Scanner(db,home,'one',100);scan.seed(A)
    def record(current,last,ts):
        keys=('input_tokens','cached_input_tokens','output_tokens')
        return line('event_msg',dict(type='token_count',info=dict(total_token_usage=dict(zip(keys,current)),last_token_usage=dict(zip(keys,last)))),ts)
    f=folder/'timeline.jsonl';f.write_text(line('session_meta',dict(id='s'))+line('turn_context',dict(model='gpt-6-astra'),200)+record([10000,9000,100],[10000,9000,100],210))
    scan.scan(A)
    with f.open('a') as out:
        out.write(record([1000,800,120],[200,150,20],220))
        out.write(record([1300,1050,150],[300,250,30],230))
        out.write(record([1300,1050,150],[300,250,30],240))
    scan.scan(A);events=sorted(scan.pending(),key=lambda e:e['ts'])
    assert [e['tokens'] for e in events]==[10100,220,330]
    assert [e['cached_input_tokens'] for e in events]==[9000,150,250]
    archive=home/'archived_sessions';archive.mkdir();f.rename(archive/'timeline.jsonl')
    scan.scan(A);assert len(scan.pending())==3


def test_activity_is_scoped_to_current_account(tmp_path):
    db = Database(tmp_path/'local.sqlite')
    folder = tmp_path/'codex'/'sessions'
    folder.mkdir(parents=True)
    scan = Scanner(db, tmp_path/'codex', 'one', 100)
    scan.seed(A)
    f = folder/'new.jsonl'
    f.write_text(line('session_meta', dict(id='s'))+line('event_msg', dict(type='task_started'), 200)+usage(1000, 210))
    scan.scan(A)
    assert scan.activity(211, A) == (1, 0)
    assert scan.activity(211, B) == (0, 0)
    assert scan.activity(400, A) == (0, 1)


def test_journal_gaps_reconciliation_and_dedup(tmp_path):
    db1, l1 = ledger(tmp_path/'a')
    db2, l2 = ledger(tmp_path/'b')
    j1, j2 = Journal(db1, l1, 'one'), Journal(db2, l2, 'two')
    first = j1.append(A, 'profile', dict(device='one', name='one', cap=33), 50)
    q1 = j1.append(A, 'quota', snap(100, 20), 100)
    j2.merge(A, [q1])
    assert j2.vector(A).get('one', 0) == 0
    j2.merge(A, [first])
    assert j2.vector(A)['one'] == 2
    j1.append(A, 'events', [event()], 160)
    j1.append(A, 'quota', snap(200, 24), 200)
    records = j1.since(A, j2.vector(A))
    j2.merge(A, list(reversed(records)))
    j2.merge(A, records)
    s1, s2 = l1.summary(A, 400), l2.summary(A, 400)
    assert s1['devices'][0]['estimated'] == s2['devices'][0]['estimated'] == 4
    assert s1['devices'][0]['tokens'] == s2['devices'][0]['tokens'] == 1000
    assert s1['epoch']['cycle'] == s2['epoch']['cycle']
    corrupted = copy.deepcopy(first)
    corrupted['payload']['name'] = 'forked'
    with pytest.raises(ValueError):
        j2.merge(A, [corrupted])
    with pytest.raises(ValueError):
        j2.merge(B, [first])


def test_pair_code_and_dpapi(tmp_path):
    c = defaults()
    c.update(rendezvous_url='wss://relay.example.com', relay_token='x'*32)
    code = create_code(c)
    assert read_code(code)['group_secret'] == c['group_secret']
    assert 'access_token' not in code
    with pytest.raises(ValueError):
        read_code(code[:-1]+'!')
    with pytest.raises(ValueError):
        create_code(dict(c, rendezvous_url='ws://remote.example.com'))
    path = tmp_path/'settings.json'
    save_config(path, c)
    assert c['group_secret'] not in path.read_text()
    assert load_config(path) == c


def test_cipher_auth_account_isolation_and_replay():
    c1, c2 = Cipher('x'*32, A), Cipher('x'*32, A)
    msg = c1.seal('one', 'two', 'app', dict(tokens=100))
    assert c2.open(msg, 'two')['tokens'] == 100
    with pytest.raises(ValueError):
        c2.open(msg, 'two')
    with pytest.raises(Exception):
        Cipher('x'*32, B).open(c1.seal('one', 'two', 'app', {}), 'two')
    changed = c1.seal('one', 'two', 'app', {})
    changed['sender'] = 'attacker'
    with pytest.raises(Exception):
        c2.open(changed, 'two')


class FakeFirewall:
    def __init__(self):
        self.calls = []
    def apply(self, paths): self.calls.append('apply')
    def pause(self, paths, database): self.calls.append('pause')
    def resume(self, database): self.calls.append('resume')
    def restore(self): self.calls.append('restore')


def test_enforcement_and_reset_resume(tmp_path):
    db = Database(tmp_path/'local.sqlite')
    cfg = defaults()
    cfg.update(device_id='one', auto_block=True, program_paths=['dummy.exe'], tracked_accounts={A: dict(added_at=0)})
    fw = FakeFirewall()
    engine = Engine(db, cfg, firewall=fw)
    s = dict(account=A, epoch=dict(cycle='c1', observed_at=100), reset_pending=False,
             devices=[dict(id='one', estimated=34, settled=34, cap=33)])
    engine.enforce(s, 101)
    assert fw.calls == ['apply', 'pause']
    assert not engine.snapshot()['notifications']
    s['epoch']['cycle'] = 'c2'
    s['devices'][0].update(estimated=0, settled=0)
    engine.enforce(s, 102)
    assert fw.calls[-2:] == ['resume', 'restore']
    assert not engine.blocked


def test_no_block_on_stale_or_unsettled_estimate(tmp_path):
    db = Database(tmp_path/'local.sqlite')
    cfg = defaults()
    cfg.update(device_id='one', auto_block=True, tracked_accounts={A: dict(added_at=0)})
    fw = FakeFirewall()
    engine = Engine(db, cfg, firewall=fw)
    s = dict(account=A, epoch=dict(cycle='c1', observed_at=100), reset_pending=False,
             devices=[dict(id='one', estimated=34, settled=0, cap=33)])
    engine.enforce(s, 101)
    s['devices'][0]['settled'] = 34
    engine.enforce(s, 500)
    assert not fw.calls


def test_cap_increase_unblocks(tmp_path):
    db = Database(tmp_path/'local.sqlite')
    cfg = defaults()
    cfg.update(device_id='one', auto_block=True, program_paths=['dummy'], tracked_accounts={A: dict(added_at=0)})
    fw = FakeFirewall()
    engine = Engine(db, cfg, firewall=fw)
    s = dict(account=A, epoch=dict(cycle='c1', observed_at=100), reset_pending=False,
             devices=[dict(id='one', estimated=34, settled=34, cap=33)])
    engine.enforce(s, 101)
    s['devices'][0]['cap'] = 50
    engine.enforce(s, 102)
    assert not engine.blocked


def test_api_mode_never_queries_or_joins_mesh(tmp_path):
    cfg = defaults()
    cfg['codex_home'] = str(tmp_path/'codex')
    def forbidden(*_): raise AssertionError('quota must not be queried')
    engine = Engine(Database(tmp_path/'local.sqlite'), cfg, quota_reader=forbidden,
                    identity_reader=lambda _: dict(mode='api', account='', label='API', multiplier=1))
    engine.step(100)
    assert engine.snapshot()['summary'] is None
    assert engine.mesh is None


def test_external_recovery_disables_enforcement(tmp_path):
    cfg = defaults()
    cfg.update(auto_block=True, codex_home=str(tmp_path/'codex'))
    db = Database(tmp_path/'local.sqlite')
    engine = Engine(db, cfg, firewall=FakeFirewall(),
                    identity_reader=lambda _: dict(mode='api', account='', label='API', multiplier=1))
    engine.blocked = True
    db.put('manual_restore_at', 101)
    engine.step(102)
    assert not engine.config['auto_block']
    assert not engine.blocked
