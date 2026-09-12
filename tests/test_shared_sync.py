"""Three isolated journals and a bounded fake bus; no login, native node, or credentials."""
import copy
import hashlib
import json

import pytest

from quota_guard.journal import Journal
from quota_guard.ledger import Ledger
from quota_guard.shared_sync import SharedSync
from quota_guard.storage import Database


A, B, C = 'a'*64, 'b'*64, 'c'*64


class Bus:
    def __init__(self):
        self.clients, self.messages, self.trace = {}, [], []
        self.links = None
        self.drop_facts = False

    def add(self, tmp_path, device, accounts):
        database = Database(tmp_path/device/'group.sqlite')
        journal = Journal(database, Ledger(database), device)
        tracked = {account: dict(label='Account '+account[0], added_at=0, cap=50) for account in accounts}
        sync = SharedSync(database, journal, device, 'User '+device, tracked)
        mesh = Endpoint(self, device)
        self.clients[device] = (sync, mesh)
        return sync

    def deliver(self, now, reverse=False, duplicate=False):
        for _ in range(3000):
            if not self.messages:
                return
            origin, recipient, message = self.messages.pop(-1 if reverse else 0)
            if self.drop_facts and message['type'] == 'facts':
                continue
            target, mesh = self.clients[recipient]
            target.receive(origin, message, mesh, now)
            if duplicate:
                target.receive(origin, copy.deepcopy(message), mesh, now)
        raise AssertionError('Replication messages did not drain')

    def tick(self, now, reverse=False, duplicate=False):
        for sync, mesh in self.clients.values():
            sync.tick(mesh, now)
        self.deliver(now, reverse, duplicate)


class Endpoint:
    history_limit = 2
    history_bytes = 24000

    def __init__(self, bus, device):
        self.bus, self.device, self.removed = bus, device, set()

    def peer_states(self):
        return {device: dict(route='isolated fixture') for device in self.bus.clients
                if device != self.device and device not in self.removed
                and (self.bus.links is None or frozenset((self.device, device)) in self.bus.links)}

    def removed_devices(self):
        return self.removed

    def send(self, peer, message):
        if peer in self.peer_states():
            packet = (self.device, peer, copy.deepcopy(message))
            self.bus.messages.append(packet)
            self.bus.trace.append(packet)


def seed(sync, account, count=6):
    device = sync.device
    sync.journal.append(account, 'profile', dict(device=device, name=sync.name, cap=50), 100.)
    sync.journal.append(account, 'quota', dict(account=account, at=101, used=0, reset_at=10000 if account == A else 20000), 101.)
    for number in range(count):
        at = 110.+number
        event = dict(id=hashlib.sha256(f'{account}-{device}-{number}'.encode()).hexdigest(),
            account=account, device=device, ts=at, model='gpt-6-astra', tokens=100+number,
            weight=number+1, known=True, input_tokens=90+number, cached_input_tokens=80,
            output_tokens=10, reasoning_output_tokens=2)
        sync.journal.append(account, 'events', [event], at)


def facts(sync, account):
    with sync.db.connect() as database:
        return [tuple(row) for row in database.execute(
            'SELECT origin,seq,digest FROM facts WHERE account=? ORDER BY origin,seq', (account,))]


def three(tmp_path):
    bus = Bus()
    one, two, third = (bus.add(tmp_path, 'one', (A,)), bus.add(tmp_path, 'two', (A,)), bus.add(tmp_path, 'third', (B,)))
    for sync, account in ((one, A), (two, A), (third, B)):
        seed(sync, account)
    return bus, one, two, third


@pytest.mark.parametrize('reverse,duplicate', [(False, False), (True, False), (True, True)])
def test_three_devices_two_accounts_converge_without_current_login(tmp_path, reverse, duplicate):
    bus, one, two, third = three(tmp_path)
    originals = {sync.device: copy.deepcopy(sync.tracked) for sync in (one, two, third)}
    expected = {A: sorted(facts(one, A)+facts(two, A)), B: facts(third, B)}
    for now in (200, 205, 210, 215):
        bus.tick(now, reverse, duplicate)
    for sync, mesh in bus.clients.values():
        assert facts(sync, A) == expected[A]
        assert facts(sync, B) == expected[B]
        assert sync.tracked == originals[sync.device]
        assert sync.journal.vector(A) == {'one': 8, 'two': 8}
        assert sync.journal.vector(B) == {'third': 8}
        value = sync.snapshot(mesh, 215)
        assert set(value['members']) == {'one', 'two', 'third'}
        assert set(value['account_labels']) == {A, B}
        assert all(row['current_account'] is None for row in value['members'].values())
        assert all(row['online'] for row in value['members'].values())
        assert all(row['state'] == 'caught_up' for row in value['sync_progress'].values())
        assert value['sync_errors'] == {}
        with sync.db.connect() as database:
            assert [tuple(row) for row in database.execute('SELECT account,id,cap FROM devices ORDER BY account,id')] == [
                (A, 'one', 50), (A, 'two', 50), (B, 'third', 50)]
            assert database.execute('SELECT COUNT(*) FROM event_details').fetchone()[0] == 18
            assert database.execute('SELECT SUM(cached_input_tokens) FROM event_details').fetchone()[0] == 1440
    fact_packets = [message for _, _, message in bus.trace if message['type'] == 'facts']
    assert fact_packets and all(len(message['records']) <= 2 for message in fact_packets)
    assert {message['account'] for message in fact_packets} == {A, B}
    bus.trace.clear()
    bus.tick(220)
    bus.tick(225)
    assert not any(message['type'] == 'facts' for _, _, message in bus.trace)


def test_account_vectors_do_not_overwrite_each_other_for_same_peer(tmp_path):
    bus = Bus()
    left, right = bus.add(tmp_path, 'left', (A, B)), bus.add(tmp_path, 'right', (A, B))
    seed(left, A, 8)
    seed(left, B, 3)
    for now in (200, 205, 210):
        bus.tick(now, reverse=True, duplicate=True)
    assert right.journal.vector(A) == {'left': 10}
    assert right.journal.vector(B) == {'left': 5}
    assert left.vectors[A, 'right'] == {'left': 10}
    assert left.vectors[B, 'right'] == {'left': 5}
    assert (A, 'right') in left.receipts and (B, 'right') in left.receipts


def test_relayed_directory_and_history_keep_original_declarer(tmp_path):
    bus, one, two, third = three(tmp_path)
    bus.links = {frozenset(('one', 'two')), frozenset(('two', 'third'))}
    for now in (200, 205, 210, 215):
        bus.tick(now)
    assert facts(one, B) == facts(third, B)
    assert one.directory['third']['accounts'] == [dict(account=B, label='Account b')]
    assert one.directory['two']['accounts'] == [dict(account=A, label='Account a')]
    assert B not in one.tracked
    value = one.snapshot(bus.clients['one'][1], 215)
    assert value['members']['third']['online'] is False
    assert value['members']['two']['online'] is True
    restarted = SharedSync(one.db, one.journal, one.device, one.name, one.tracked)
    assert restarted.directory == one.directory
    assert B in restarted.snapshot(None, 220)['account_labels']
    assert restarted.snapshot(None, 220)['members']['third']['online'] is False


def test_unannounced_account_and_invalid_catalog_are_rejected_without_enrollment(tmp_path):
    bus = Bus()
    local, remote = bus.add(tmp_path, 'local', (A,)), bus.add(tmp_path, 'remote', (B,))
    seed(remote, B)
    mesh = bus.clients['local'][1]
    page = remote.journal.since(B, {}, limit=2)
    assert not local.receive('remote', dict(type='facts', account=B, records=page), mesh, 200)
    assert local.journal.vector(B) == {}
    catalog = remote._catalog(200)
    catalog['vectors'][C] = {'remote': 99}
    before = copy.deepcopy(local.directory)
    assert not local.receive('remote', catalog, mesh, 200)
    assert local.directory == before and set(local.tracked) == {A}
    assert local.receive('remote', remote._catalog(201), mesh, 201)
    assert local.receive('remote', dict(type='facts', account=B, records=page), mesh, 201)
    assert local.journal.vector(B) == {'remote': 2}
    assert set(local.tracked) == {A}
    unknown = copy.deepcopy(page)
    for record in unknown:
        record['account'] = C
    assert not local.receive('remote', dict(type='facts', account=C, records=unknown), mesh, 202)
    assert local.journal.vector(C) == {}


def test_out_of_order_facts_do_not_ack_a_hole_and_retries_recover(tmp_path):
    bus = Bus()
    left, right = bus.add(tmp_path, 'left', (A,)), bus.add(tmp_path, 'right', (A,))
    seed(left, A)
    mesh = bus.clients['right'][1]
    rows = left.journal.since(A, {}, limit=20)
    assert right.receive('left', dict(type='facts', account=A, records=rows[2:4]), mesh, 200)
    assert right.journal.vector(A) == {}
    bus.messages.clear()
    for now in (205, 210, 215):
        bus.tick(now, reverse=True, duplicate=True)
    assert facts(right, A) == facts(left, A)


def test_presence_is_direct_only_and_switches_clear_old_activity(tmp_path):
    bus = Bus()
    local, remote = bus.add(tmp_path, 'local', (A,)), bus.add(tmp_path, 'remote', (A, B))
    seed(remote, A, 0)
    seed(remote, B, 0)
    bus.tick(200)
    remote_mesh, local_mesh = bus.clients['remote'][1], bus.clients['local'][1]
    def presence(account, now):
        return dict(device='remote', account=account, at=now, scan_at=now, active=1, uncertain=0,
                    active_models=['gpt-6-astra', 'gpt-5.6-sol'])
    remote.tick(remote_mesh, 205, presence(A, 205))
    bus.deliver(205)
    assert local.snapshot(local_mesh, 205)['members']['remote']['current_account'] == A
    remote.tick(remote_mesh, 206, presence(B, 206))
    bus.deliver(206)
    with local.db.connect() as database:
        rows = {row['account']: dict(row) for row in database.execute("SELECT * FROM devices WHERE id='remote'")}
    assert rows[A]['active'] == 0 and rows[A]['logged_in'] == 0
    assert rows[B]['active'] == 1 and rows[B]['logged_in'] == 1
    assert json.loads(rows[A]['active_models']) == []
    assert json.loads(rows[B]['active_models']) == ['gpt-6-astra', 'gpt-5.6-sol']
    forged = presence(A, 207)
    forged['device'] = 'somebody-else'
    assert not local.receive('remote', dict(type='presence', account=A, presence=forged), local_mesh, 207)
    remote.tick(remote_mesh, 208, None)
    bus.deliver(208)
    assert local.snapshot(local_mesh, 208)['members']['remote']['current_account'] is None
    with local.db.connect() as database:
        assert database.execute("SELECT SUM(active) FROM devices WHERE id='remote'").fetchone()[0] == 0
    stale = remote._catalog(205)
    stale.update(current_account=A, presence=presence(A, 205))
    assert local.receive('remote', stale, local_mesh, 209)
    assert local.snapshot(local_mesh, 209)['members']['remote']['current_account'] is None
    bus.links = set()
    assert local.snapshot(local_mesh, 210)['members']['remote']['online'] is False


def test_empty_third_member_can_join_without_any_account(tmp_path):
    bus = Bus()
    owner, third = bus.add(tmp_path, 'owner', (A,)), bus.add(tmp_path, 'third', ())
    seed(owner, A)
    bus.tick(200)
    bus.tick(205)
    value = owner.snapshot(bus.clients['owner'][1], 205)
    assert value['members']['third'] == dict(id='third', name='User third', online=True, accounts=[], current_account=None)
    assert third.tracked == {}
    assert facts(third, A) == facts(owner, A)


def test_lost_pages_retry_at_next_catalog_and_never_report_early_completion(tmp_path):
    bus = Bus()
    source, receiver = bus.add(tmp_path, 'source', (A,)), bus.add(tmp_path, 'receiver', (A,))
    seed(source, A)
    bus.drop_facts = True
    bus.tick(200)
    assert receiver.journal.vector(A) == {}
    state = receiver.snapshot(bus.clients['receiver'][1], 200)['sync_progress']['source']
    assert state['state'] == 'syncing' and state['receive'] == 8
    bus.drop_facts = False
    bus.trace.clear()
    bus.tick(201)
    assert not any(message['type'] == 'facts' for _, _, message in bus.trace)
    bus.tick(205)
    bus.tick(210)
    assert facts(receiver, A) == facts(source, A)
    state = receiver.snapshot(bus.clients['receiver'][1], 210)['sync_progress']['source']
    assert state['state'] == 'caught_up'
    assert receiver.snapshot(bus.clients['receiver'][1], 245)['sync_progress']['source']['state'] == 'stale'


def test_new_explicit_account_is_shared_but_stopping_collection_retains_history(tmp_path):
    bus = Bus()
    source, receiver = bus.add(tmp_path, 'source', (A,)), bus.add(tmp_path, 'receiver', ())
    seed(source, A)
    bus.tick(200)
    source.tracked[B] = dict(label='Second account', added_at=200, cap=33)
    seed(source, B)
    bus.tick(205)
    bus.tick(210)
    assert B in receiver.snapshot(bus.clients['receiver'][1], 210)['account_labels']
    assert receiver.tracked == {}
    assert facts(receiver, B) == facts(source, B)
    del source.tracked[B]
    source.tick(bus.clients['source'][1], 215)
    bus.deliver(215)
    assert B not in source.tracked
    assert B in source.snapshot(None, 215)['account_labels']
    assert B in receiver.snapshot(None, 215)['account_labels']


def test_forwarded_member_cannot_claim_another_members_presence(tmp_path):
    bus, one, two, third = three(tmp_path)
    bus.tick(200)
    mesh = bus.clients['one'][1]
    presence = dict(device='third', account=B, at=205, scan_at=205, active=1, uncertain=0)
    assert not one.receive('two', dict(type='presence', account=B, presence=presence), mesh, 205)
    assert one.snapshot(mesh, 205)['members']['third']['current_account'] is None
    assert one.snapshot(mesh, 205)['members']['two']['current_account'] is None


def test_removed_peer_cannot_rejoin_by_sending_a_catalog(tmp_path):
    bus = Bus()
    local, remote = bus.add(tmp_path, 'local', (A,)), bus.add(tmp_path, 'remote', (B,))
    mesh = bus.clients['local'][1]
    mesh.removed.add('remote')
    assert not local.receive('remote', remote._catalog(200), mesh, 200)
    assert 'remote' not in local.directory


@pytest.mark.parametrize('mutation', [
    lambda message: message.update(name='x'*81),
    lambda message: message.update(accounts=[dict(account=A, label='x')]*17),
    lambda message: message.update(accounts=[dict(account='x'*64, label='x')]),
    lambda message: message.update(vectors={A: {'one': True}}),
    lambda message: message.update(vectors={A: {str(i): 1 for i in range(129)}}),
    lambda message: message.update(current_account=C),
    lambda message: message.update(at=float('nan')),
    lambda message: message.update(directory=[{}]*17),
    lambda message: message.update(unexpected='x'*(160*1024)),
])
def test_catalog_validation_is_bounded_and_atomic(tmp_path, mutation):
    bus = Bus()
    local, remote = bus.add(tmp_path, 'local', (A,)), bus.add(tmp_path, 'remote', (A,))
    before = copy.deepcopy(local.directory)
    message = remote._catalog(200)
    mutation(message)
    assert not local.receive('remote', message, bus.clients['local'][1], 200)
    assert local.directory == before
    assert local.catalog_receipts == {}


@pytest.mark.parametrize('models', ['gpt-6-astra', {}, [None], [''], ['x'*101],
                                  ['line\nbreak'], ['gpt-6-astra']*17])
def test_model_presence_is_validated_before_both_sync_and_direct_writes(tmp_path, models):
    bus = Bus()
    local, remote = bus.add(tmp_path, 'local', (A,)), bus.add(tmp_path, 'remote', (A,))
    seed(remote, A, 0)
    bus.tick(200)
    presence = dict(device='remote', account=A, at=205, scan_at=205, active=1, uncertain=0,
                    active_models=models)
    with pytest.raises(ValueError):
        local.journal.presence(A, 'remote', presence, 205)
    assert not local.receive('remote', dict(type='presence', account=A, presence=presence),
                             bus.clients['local'][1], 205)
    with local.db.connect() as db:
        row = dict(db.execute("SELECT * FROM devices WHERE id='remote'").fetchone())
    assert row['active_models'] == 'null' and row['active'] == 0


def test_old_presence_clears_previous_models_and_migration_preserves_records(tmp_path):
    import sqlite3
    path = tmp_path/'legacy.sqlite'
    with sqlite3.connect(path) as db:
        db.execute("""CREATE TABLE devices (account TEXT NOT NULL, id TEXT NOT NULL,
            name TEXT NOT NULL, cap REAL NOT NULL, seen REAL NOT NULL,
            scan_at REAL NOT NULL DEFAULT 0, active INTEGER NOT NULL DEFAULT 0,
            uncertain INTEGER NOT NULL DEFAULT 0, logged_in INTEGER NOT NULL DEFAULT 1,
            PRIMARY KEY(account,id))""")
        db.execute("INSERT INTO devices(account,id,name,cap,seen) VALUES (?,?,?,?,?)",
                   (A, 'old', 'old', 33, 100))
    database = Database(path)
    journal = Journal(database, Ledger(database), 'local')
    presence = dict(device='old', account=A, at=205, scan_at=205, active=1, uncertain=0)
    journal.presence(A, 'old', dict(presence, active_models=['gpt-6-astra']), 205)
    journal.presence(A, 'old', dict(presence, active_models=[]), 206)
    with database.connect() as db:
        assert db.execute('SELECT active_models FROM devices').fetchone()[0] == '[]'
    journal.presence(A, 'old', presence, 207)
    Database(path)  # Migration is repeatable.
    with database.connect() as db:
        row = dict(db.execute('SELECT * FROM devices').fetchone())
    assert row['name'] == 'old' and row['cap'] == 33
    assert row['active_models'] == 'null' and row['active'] == 1 and row['logged_in'] == 1



def test_explicit_empty_models_do_not_reuse_history_and_mixed_peers_remain_visible(tmp_path):
    from quota_guard.shared_view import latest_active_models
    bus = Bus()
    local, remote = bus.add(tmp_path, 'local', (A,)), bus.add(tmp_path, 'remote', (A,))
    seed(local, A, 1)
    seed(remote, A, 1)
    bus.tick(200)
    people = {device: dict(current_account=A) for device in ('local', 'remote')}
    devices = [dict(id='local', online=True, active=1, active_models=[]),
               dict(id='remote', online=True, active=1, active_models=None)]
    assert latest_active_models(local.db, people, devices[:1], 200) == []
    assert latest_active_models(local.db, people, devices, 200) == ['gpt-6-astra']
    devices[0]['active_models'] = ['gpt-5.6-sol']
    assert latest_active_models(local.db, people, devices, 200) == ['gpt-5.6-sol', 'gpt-6-astra']
