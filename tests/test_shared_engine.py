"""Shared Engine integration uses temporary journals, injected identities and an in-memory bus."""
import copy
import hashlib
from unittest.mock import Mock

import pytest

from quota_guard.engine import Engine
from quota_guard.pairing import create_code, load_config, save_config
from quota_guard.storage import Database, defaults
from quota_guard.web_controller import WebController


A, B = 'a'*64, 'b'*64


def identity(account=None):
    return dict(mode='account' if account else 'none', account=account or '', revision=1,
                label='Account '+account[0] if account else 'Logged out', plan='pro', multiplier=1)


class Firewall:
    def __init__(self):
        self.apply, self.pause, self.resume, self.restore = (Mock() for _ in range(4))


class Bus:
    def __init__(self):
        self.engines, self.meshes, self.trace, self.messages = {}, {}, [], []
        self.now = 200

    def factory(self, config, scope, receiver):
        mesh = Endpoint(self, config['device_id'], scope, receiver)
        self.meshes[mesh.device] = mesh
        return mesh

    def add(self, folder, device, accounts, current=None):
        home = folder/'codex'
        (home/'sessions').mkdir(parents=True)
        tracked = {account: dict(label='Account '+account[0], added_at=0, cap=50) for account in accounts}
        config = dict(defaults(), device_id=device, name='User '+device, codex_home=str(home),
                      started_at=0, interval=1, tracked_accounts=tracked, group_secret='s'*43,
                      shared_group_enabled=True, link_enabled=True, tailscale_enabled=True, auto_block=True)
        selected, queries = [identity(current)], []

        def quota(_):
            account = selected[0]['account']
            queries.append(account)
            return dict(account=account, at=self.now, used=10, reset_at=10000 if account == A else 20000)

        engine = Engine(Database(folder/'local.sqlite'), config, quota_reader=quota,
                        identity_reader=lambda _: selected[0], firewall=Firewall(), mesh_factory=self.factory)
        self.engines[device] = engine
        return engine, selected, queries

    def tick(self, now):
        self.now = now
        for engine in self.engines.values():
            engine.step(now)
            while self.messages:
                sender, target, message = self.messages.pop(0)
                self.meshes[target].receiver(sender, message)


class Endpoint:
    history_limit = 400
    history_bytes = 160*1024
    status = 'isolated shared fixture'

    def __init__(self, bus, device, scope, receiver):
        self.bus, self.device, self.scope, self.receiver = bus, device, scope, receiver
        self.running = False
        self.starts = self.closes = 0

    def start(self):
        self.running = True
        self.starts += 1

    def close(self):
        self.running = False
        self.closes += 1

    def peer_states(self):
        return {device: dict(route='isolated fixture') for device, mesh in self.bus.meshes.items()
                if device != self.device and mesh.running}

    def removed_devices(self):
        return set()

    def connection_state(self):
        return dict(transport='tailscale', ready=True)

    def send(self, peer, value):
        if peer in self.peer_states():
            packet = (self.device, peer, copy.deepcopy(value))
            self.bus.messages.append(packet)
            self.bus.trace.append(packet)


def seed(engine, account):
    device = engine.config['device_id']
    engine.journal.append(account, 'profile', dict(device=device, name='User '+device, cap=50), 100)
    engine.journal.append(account, 'quota', dict(account=account, at=101, used=0,
                          reset_at=10000 if account == A else 20000), 101)
    event = dict(id=hashlib.sha256((account+device).encode()).hexdigest(), account=account, device=device,
                 ts=110, model='gpt-6-astra', tokens=100, weight=1, known=True,
                 input_tokens=90, cached_input_tokens=80, output_tokens=10)
    engine.journal.append(account, 'events', [event], 111)


def test_full_billing_auto_enrolls_third_identity_and_replicates_account_order(tmp_path):
    from quota_guard.shared_policy import load_rules
    bus = Bus()
    one, _, _ = bus.add(tmp_path/'one', 'one', (A,), A)
    two, _, _ = bus.add(tmp_path/'two', 'two', (A,), A)
    third, _, _ = bus.add(tmp_path/'third', 'third', (B,), B)
    for engine in (one,two,third):
        engine.config['shared_billing_v1'] = True
        engine.config['auto_block'] = False
    one.config.update(shared_group_admin='one', shared_initial_devices=['one','two'])
    for engine, account in ((one,A),(two,A),(third,B)):
        seed(engine,account)
    for now in range(2000,2240,5):
        bus.tick(now)
    rules = [load_rules(engine.group_db,[A,B],2240) for engine in (one,two,third)]
    assert all(r['status']=='ready' for r in rules)
    assert all(r['accounts']==[A,B] for r in rules)
    assert rules[0] == rules[1] == rules[2]
    views = [engine.snapshot() for engine in (one,two,third)]
    for index, view in enumerate(views):
        assert view['shared_group']['stage']=='billing'
        assert [d['avatar'] for d in view['summary']['devices']]==['person1.jpg','person2.jpg','person3.jpg']
        assert view['summary']['devices'][index]['local']
        assert view['summary']['compensation_enabled']
        assert view['shared_group']['rules_locked']
    books = [view['billing'] for view in views]
    assert all(book['status']=='active' for book in books)
    assert books[0]['people']==books[1]['people']==books[2]['people']
    assert one._group_rule_ready(rules[0],2235)
    one.shared.peer_versions['third']='0.5.0'
    assert not one._group_rule_ready(rules[0],2235)


def test_inactive_tracked_account_still_publishes_completed_sample(tmp_path):
    bus=Bus()
    engine,_,_=bus.add(tmp_path/'one','one',(A,B),B)
    seed(engine,A)
    engine.journal.append(A,'quota',dict(account=A,at=200,used=10,reset_at=10000),200)
    engine.recovery.scan(0)
    engine.recover_inactive(B,400)
    assert engine.group_db.get('published_sample_checkpoint:'+A)['through']==200


def test_three_engines_sync_two_accounts_while_one_has_no_login_and_never_enroll_remote(tmp_path):
    bus = Bus()
    one, _, q1 = bus.add(tmp_path/'one', 'one', (A,), A)
    two, _, q2 = bus.add(tmp_path/'two', 'two', (A,), None)
    third, _, q3 = bus.add(tmp_path/'third', 'third', (B,), B)
    originals = {device: copy.deepcopy(engine.tracked) for device, engine in bus.engines.items()}
    for engine, account in ((one, A), (two, A), (third, B)):
        seed(engine, account)
    for now in range(200, 246, 5):
        bus.tick(now)
    for device, engine in bus.engines.items():
        assert engine.tracked == originals[device]
        assert engine.config['tracked_accounts'] == originals[device]
        assert engine.mesh.scope == 'group:'+hashlib.sha256(b's'*43).hexdigest()[:20]
        assert engine.mesh.starts == 1 and engine.mesh.closes == 0
        snapshot = engine.snapshot()
        assert snapshot['pair_scope'] and snapshot['shared_group_enabled']
        assert {member['id'] for member in snapshot['shared_group']['members']} == {'one', 'two', 'third'}
        assert not snapshot['sync_errors']
        assert snapshot['auto_block'] is False and snapshot['blocked'] is False
        engine.firewall.apply.assert_not_called()
        engine.firewall.pause.assert_not_called()
        with engine.group_db.connect() as db:
            assert {row['account'] for row in db.execute('SELECT DISTINCT account FROM events')} == {A, B}
            assert db.execute('SELECT SUM(tokens) FROM events').fetchone()[0] == 300
    assert q1 and set(q1) == {A}
    assert q2 == []
    assert q3 and set(q3) == {B}
    assert any(message['type'] == 'group_sync' for _, _, message in bus.trace)
    assert not any(message['type'] == 'sync' and 'presence' in message for _, _, message in bus.trace)


def test_account_and_login_switch_keep_same_mesh_and_local_authorization(tmp_path):
    bus = Bus()
    engine, selected, queries = bus.add(tmp_path/'one', 'one', (A, B), A)
    for now, account in ((200, A), (205, B), (210, None), (215, A)):
        selected[0] = identity(account)
        bus.tick(now)
    assert engine.mesh.starts == 1 and engine.mesh.closes == 0
    assert set(engine.tracked) == {A, B}
    assert queries == [A, B, A]
    selected[0] = identity(B)
    assert engine.scope_changed(identity(A), 216)
    assert engine.mesh.running and engine.mesh.closes == 0


def test_shared_message_arriving_after_shared_drain_is_not_consumed_by_legacy_dispatch(tmp_path, monkeypatch):
    bus = Bus()
    engine, _, _ = bus.add(tmp_path/'one', 'one', (A,), A)
    remote, _, _ = bus.add(tmp_path/'remote', 'remote', (B,), B)
    drain = engine._receive_shared
    injected = []

    def arrive_after_drain(now):
        drain(now)
        if not injected:
            engine.receive('remote', remote.shared._catalog(now))
            injected.append(True)

    monkeypatch.setattr(engine, '_receive_shared', arrive_after_drain)
    engine.step(200)
    assert engine.inbox.qsize() == 1
    assert 'remote' not in engine.shared.directory
    engine.step(201)
    assert 'remote' in engine.shared.directory
    assert B in engine.shared.snapshot(engine.mesh, 201)['account_labels']
    assert B not in engine.tracked


def test_shared_mode_restores_existing_legacy_block_instead_of_reapplying(tmp_path):
    bus = Bus()
    engine, _, _ = bus.add(tmp_path/'one', 'one', (A,), A)
    engine.blocked = True
    engine.db.put('block_state', dict(account=A, cycle='old', cap=1))
    bus.tick(200)
    assert not engine.blocked and engine.db.get('block_state') is None
    engine.firewall.resume.assert_called_once()
    engine.firewall.restore.assert_called_once()
    engine.firewall.apply.assert_not_called()
    engine.firewall.pause.assert_not_called()


def test_shared_controller_accepts_old_cqg4_without_login_and_preserves_local_account(tmp_path, monkeypatch):
    config = dict(defaults(), device_id='third', codex_home=str(tmp_path),
                  tracked_accounts={B: dict(label='B', added_at=100, cap=50)}, shared_group_enabled=True)
    controller = WebController(tmp_path, config, Database(tmp_path/'local.sqlite'), demo=True)
    controller._save = Mock()
    monkeypatch.setattr('quota_guard.web_controller.identity', lambda _: identity())
    assert controller._pair_generate({}) == {'pending': True}
    assert controller._pairing['stage'] == 'authorizing'
    inviter = dict(defaults(), device_id='one', tailscale_enabled=True, tailscale_ip='100.64.0.1', group_secret='s'*43)
    controller._pair_join(dict(confirmed=True, code=create_code(inviter)))
    assert controller._config['device_id'] == 'third'
    assert controller._config['group_secret'] == 's'*43
    assert set(controller._config['tracked_accounts']) == {B}
    assert controller._config['shared_group_enabled'] is True
    assert controller._config['auto_block'] is False
    assert controller._pairing['stage'] == 'saved'
    controller._save.assert_called_with(restart=True)
    with pytest.raises(ValueError):
        controller._settings_save(dict(confirmed=True, settings=dict(auto_block=True)))


def test_main_migration_preserves_group_device_and_local_enrollment(tmp_path, monkeypatch):
    import ctypes
    import main

    config = dict(defaults(), device_id='third', group_secret='s'*43, auto_block=True,
                  tracked_accounts={B: dict(label='B', added_at=100, cap=50)})
    save_config(tmp_path/'settings.json', config)
    runner = Mock()
    monkeypatch.setattr('quota_guard.web_host.run', runner)
    monkeypatch.setattr(main.sys, 'argv', ['main.py', '--data-dir', str(tmp_path), '--no-autostart'])
    if main.os.name == 'nt':
        monkeypatch.setattr(ctypes.windll.shcore, 'SetProcessDpiAwareness', Mock())
        monkeypatch.setattr(ctypes, 'WinDLL', Mock(return_value=Mock()))
        monkeypatch.setattr(ctypes, 'get_last_error', lambda: 0)
    main.main()
    migrated = load_config(tmp_path/'settings.json')
    assert migrated['shared_group_enabled'] and migrated['shared_group_prepare_v1']
    assert migrated['device_id'] == 'third' and migrated['group_secret'] == 's'*43
    assert set(migrated['tracked_accounts']) == {B} and migrated['auto_block'] is False
    runner.assert_called_once()
