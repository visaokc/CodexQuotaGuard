import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
import xml.etree.ElementTree as ET

import pytest

from quota_guard.autolink import LinkNode, proof
from quota_guard.file_mesh import FileMesh
from quota_guard.journal import Journal
from quota_guard.ledger import Ledger
from quota_guard.pairing import Cipher
from quota_guard.storage import Database, defaults
from test_account_scope import setup
from test_auto_pair import ONE, TWO
from test_core import A


def test_hello_before_transport_poll_survives_without_a_second_hello(tmp_path):
    config = dict(defaults(), device_id='local')
    mesh = FileMesh(config, A, lambda *_: None)
    mesh.node = SimpleNamespace(shared=tmp_path, connections={})
    sender = Cipher(config['group_secret'], 'account-policy-v2:'+A)
    envelope = sender.seal('peer', '*', 'hello', dict(link_device=ONE))
    (tmp_path/(mesh.cipher.room+'-hello.cqg')).write_text(json.dumps(envelope))
    mesh._read()
    assert mesh.peer_states() == {}
    mesh.node.connections[ONE] = dict(connected=True, type='relay-client')
    mesh._read()
    assert mesh.peer_states()['peer']['route'] == '公共加密中转'
    mesh.node.connections[ONE] = dict(connected=True, type='tcp-client')
    assert mesh.peer_states()['peer']['route'] == 'P2P 直连'


def test_acceptance_retries_folder_after_device_saved_but_folder_write_failed():
    node = LinkNode.__new__(LinkNode)
    node.secret, node.group = 's'*43, 'fixture'
    devices, folder = [], dict(devices=[])
    calls = []
    failure = [True]
    def api(route, data=None, method=None):
        calls.append((route, method))
        if route == 'cluster/pending/devices':
            return {} if devices else {ONE: dict(name=proof(node.secret, ONE))}
        if route == 'config/devices':
            if method:
                devices.append(data)
            return devices
        if route == 'config/folders/fixture':
            if method:
                if failure[0]:
                    failure[0] = False
                    raise OSError('temporary local API failure')
                folder.update(data)
            return json.loads(json.dumps(folder))
        return dict(connections={})
    node.request = api
    with pytest.raises(OSError):
        node.poll()
    node.poll()
    assert folder['devices'] == [dict(deviceID=ONE)]
    node.poll()
    assert folder['devices'] == [dict(deviceID=ONE)]
    assert calls.count(('config/devices', 'POST')) == 1


class Mesh:
    status = 'fixture'
    def __init__(self):
        self.sent = []
    def peer_states(self):
        return {'peer': {'route': 'fixture'}}
    def send(self, peer, message):
        self.sent.append((peer, message))


def test_file_transport_uses_large_bounded_history_pages(tmp_path):
    db = Database(tmp_path/'group.sqlite')
    journal = Journal(db, Ledger(db), 'one')
    for n in range(300):
        journal.append(A, 'profile', dict(device='one', name='设备名称'*15, cap=20+n % 10), n+1)
    ordinary = journal.since(A, {})
    expanded = journal.since(A, {}, limit=FileMesh.history_limit, byte_limit=FileMesh.history_bytes)
    assert 0 < len(ordinary) <= 60
    assert len(expanded) > len(ordinary)
    envelope = Cipher('s'*43, 'account-policy-v2:'+A).seal(
        'one', 'two', 'app', dict(type='facts', account=A, records=expanded))
    assert len(json.dumps(envelope)) < 240*1024


def test_engine_uses_file_transport_history_capacity(tmp_path):
    e, _, step, *_ = setup(tmp_path)
    step(100)
    for n in range(200):
        e.journal.append(A, 'cap', dict(device='one', cap=20+n % 10), 101+n)
    e.mesh = Mesh()
    e.mesh.history_limit = FileMesh.history_limit
    e.mesh.history_bytes = FileMesh.history_bytes
    e.receive('peer', dict(type='sync', account=A, records=[], vector={}))
    step(400)
    facts = next(message for _, message in e.mesh.sent if message['type'] == 'facts')
    assert len(facts['records']) > 60


def test_background_protected_bootstrap_wakes_and_new_facts_ack_without_periodic_delay(tmp_path):
    e, _, step, *_ = setup(tmp_path)
    step(100)
    e.mesh = Mesh()
    e.background_mode = True
    e.config['auto_block'] = True
    e.last_broadcast = 100
    e.wakeup.clear()
    e.receive('peer', dict(type='peer_ready', account=A))
    assert e.wakeup.is_set()
    step(101)
    assert len(e.mesh.sent) == 1
    assert not e.snapshot()['sync_receipts']
    e.mesh.sent.clear()
    record = dict(account=A, origin='peer', seq=1, ts=101, kind='profile',
                  payload=dict(device='peer', name='Peer', cap=33))
    e.wakeup.clear()
    e.receive('peer', dict(type='facts', account=A, records=[record]))
    assert e.wakeup.is_set()
    step(102)
    assert e.mesh.sent[0][1]['vector']['peer'] == 1
    e.mesh.sent.clear()
    # Duplicate batches and unchanged heartbeats must not create an ACK loop.
    e.receive('peer', dict(type='facts', account=A, records=[record]))
    step(103)
    assert not e.mesh.sent


def test_background_protected_new_peer_sync_wakes_but_known_idle_heartbeats_do_not(tmp_path):
    e, _, step, *_ = setup(tmp_path)
    step(100)
    e.background_mode = True
    e.config['auto_block'] = True
    e.wakeup.clear()
    message = dict(type='sync', account=A, records=[], vector={})
    e.receive('peer', message)
    assert e.wakeup.is_set()
    step(101)
    e.wakeup.clear()
    e.receive('peer', message)
    assert not e.wakeup.is_set()
    e.receive('peer', dict(message, vector={'peer': 1}))
    assert e.wakeup.is_set(), 'History vector progress must wake a hidden sender'


def test_visible_monitor_broadcasts_presence_within_three_seconds(tmp_path):
    e, _, step, *_ = setup(tmp_path)
    step(100)
    e.mesh = Mesh()
    e.last_broadcast = 100
    step(103)
    assert any(message['type'] == 'sync' and message['presence']['at'] == 103
               for _, message in e.mesh.sent)


@pytest.mark.parametrize('background,maximum', [(False, 2), (True, 5)])
def test_monitor_poll_interval_keeps_activity_status_timely(tmp_path, background, maximum):
    e, *_ = setup(tmp_path)
    waits = []
    class Wake:
        def wait(self, timeout):
            waits.append(timeout)
            e.stop_event.set()
        def clear(self):
            pass
        def set(self):
            pass
    e.wakeup = Wake()
    e.background_mode = background
    e.scanner.seed = Mock()
    e.step = Mock()
    e.run()
    assert waits == [maximum]


@pytest.mark.parametrize('same_group', [True, False])
def test_generate_code_reuses_live_node_only_for_same_group(tmp_path, same_group):
    from quota_guard.gui import App
    config = dict(defaults(), link_enabled=True)
    node = SimpleNamespace(device=ONE, process=SimpleNamespace(poll=lambda: None))
    app = SimpleNamespace(config=config, folder=tmp_path, demo=False, pair_request=1,
        engine=SimpleNamespace(config=dict(config, group_secret=config['group_secret'] if same_group else 'different'),
                               mesh=SimpleNamespace(node=node)),
        persist_restart=Mock(), mesh_label=Mock(), root=Mock())
    with patch('quota_guard.gui.save_config') as save:
        App.finish_auto_pair(app, ONE)
        if same_group:
            save.assert_called_once()
            app.persist_restart.assert_not_called()
        else:
            app.persist_restart.assert_called_once()


@pytest.mark.parametrize('failures,starts', [(1, 1), (3, 2)])
def test_transient_poll_failure_keeps_node_but_persistent_failure_restarts(failures, starts):
    mesh = FileMesh(dict(defaults(), _data_dir='fixture'), A, lambda *_: None)
    node = Mock()
    node.process.poll.return_value = None
    node.poll.side_effect = [OSError('temporary')]*failures+[{}]
    node.invitation_addresses.return_value = []
    node.connections = {}
    mesh._write = Mock()
    mesh._read = lambda: mesh.stop.set()
    with patch('quota_guard.file_mesh.LinkNode', return_value=node) as constructor, \
            patch.object(mesh.stop, 'wait', side_effect=[False]*10+[AssertionError('retry loop did not finish')]):
        mesh._run()
    assert constructor.call_count == node.start.call_count == node.close.call_count == starts


def test_new_invitation_refreshes_existing_peers_stale_relay_hint(tmp_path):
    node = LinkNode.__new__(LinkNode)
    node.home, node.shared = tmp_path, tmp_path/'ciphertext'
    node.group, node.device, node.secret, node.local_test = 'fixture', TWO, 's'*43, True
    old = 'relay://old.example:22067/?id='+TWO
    new = 'relay://new.example:22067/?id='+TWO
    node.config = dict(link_peers=[ONE], link_addresses={ONE: [new]})
    (tmp_path/'group.txt').write_text(node.group)
    (tmp_path/'config.xml').write_text('<configuration><gui><apikey>fixture</apikey></gui><options/>'+
        f'<device id="{TWO}"/><device id="{ONE}"><address>dynamic</address><address>{old}</address></device>'+
        '<defaults><folder/></defaults></configuration>')
    with patch('quota_guard.autolink.binary', return_value=Path('fixture.exe')), \
            patch('quota_guard.autolink.subprocess.Popen', side_effect=RuntimeError('stop before launch')):
        with pytest.raises(RuntimeError, match='stop before launch'):
            node.start()
    peer = ET.parse(tmp_path/'config.xml').find(f"device[@id='{ONE}']")
    assert [a.text for a in peer.findall('address')] == ['dynamic', new]
