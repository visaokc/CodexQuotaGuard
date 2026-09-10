import hashlib
import queue
import time
from unittest.mock import Mock

import pytest

from cryptography.exceptions import InvalidTag
from quota_guard.pair_status import presentation
from quota_guard.storage import defaults
from quota_guard.tsnet_mesh import TailscaleMesh


A, B = 'a'*64, 'b'*64


def config(folder, device='a', shared=True):
    return dict(defaults(), _data_dir=str(folder), device_id=device,
                group_secret='s'*43, shared_group_enabled=shared)


def group_id(cfg):
    return hashlib.sha256(cfg['group_secret'].encode()).hexdigest()[:20]


def packet(sender, value, kind='app', recipient='a'):
    return dict(ip='100.64.0.2', envelope=sender.cipher.seal(
        sender.device, '*' if kind == 'hello' else recipient, kind, value))


def test_legacy_different_accounts_cannot_join_or_exchange(tmp_path):
    got = Mock()
    a = TailscaleMesh(config(tmp_path/'a', shared=False), A, got)
    b = TailscaleMesh(config(tmp_path/'b', 'b', shared=False), B, Mock())
    with pytest.raises(InvalidTag):
        a._receive(packet(b, dict(account=B, membership={}), 'hello'))
    with pytest.raises(InvalidTag):
        a._receive(packet(b, dict(type='sync', account=B, vector={})))
    assert not got.called and not a.peer_states()


def test_shared_group_joins_across_accounts_and_without_login(tmp_path):
    cfg = config(tmp_path/'a')
    received = Mock()
    a = TailscaleMesh(cfg, '', received)
    b = TailscaleMesh(config(tmp_path/'b', 'b'), B, Mock())
    hello = dict(group_id=group_id(cfg), protocol=3, membership={})
    a._receive(packet(b, hello, 'hello'))
    received.assert_called_once_with('b', dict(type='peer_ready', group_id=group_id(cfg), protocol=3))
    assert 'b' in a.peer_states()
    received.reset_mock()
    for account in (A, B):
        message = dict(type='sync', group_id=group_id(cfg), protocol=3, account=account, vector={})
        a._receive(packet(b, message))
        assert received.call_args.args == ('b', message)
    assert received.call_count == 2


def test_shared_group_rejects_wrong_group_protocol_and_legacy_cipher(tmp_path):
    cfg = config(tmp_path/'a')
    got = Mock()
    a = TailscaleMesh(cfg, A, got)
    b = TailscaleMesh(config(tmp_path/'b', 'b'), B, Mock())
    bad_headers = ({}, dict(group_id=group_id(cfg), protocol=2),
                   dict(group_id=group_id(cfg), protocol=True),
                   dict(group_id='wrong-group', protocol=3))
    for header in bad_headers:
        a._receive(packet(b, dict(type='sync', account=A, **header)))
    other = TailscaleMesh(dict(config(tmp_path/'other', 'b'), group_secret='other'*9), B, Mock())
    with pytest.raises(InvalidTag):
        a._receive(packet(other, dict(group_id=group_id(other.config), protocol=3), 'hello'))
    legacy = TailscaleMesh(config(tmp_path/'legacy', 'b', shared=False), A, Mock())
    with pytest.raises(InvalidTag):
        a._receive(packet(legacy, dict(account=A), 'hello'))
    assert not got.called and not a.addresses and not a.peer_states()


def test_shared_pending_preserves_each_account_and_message_slot(tmp_path):
    cfg = dict(config(tmp_path), tailscale_peers={'b': '100.64.0.2'})
    mesh = TailscaleMesh(cfg, 'group:'+group_id(cfg), Mock())
    for n in range(100):
        for account in (A, B):
            for kind in ('sync', 'facts'):
                message = dict(type=kind, account=account, n=n)
                mesh.send('b', message)
                assert 'group_id' not in message
    assert len(mesh.pending) == 4
    for account in (A, B):
        for kind in ('sync', 'facts'):
            assert mesh.pending['b', account, kind] == dict(
                type=kind, account=account, n=99, group_id=group_id(cfg), protocol=3)
    mesh.remove_device('b')
    assert not mesh.pending


def test_shared_hello_directory_validates_then_learns_only_eligible_peers(tmp_path):
    cfg = config(tmp_path/'a')
    got = Mock()
    a = TailscaleMesh(cfg, A, got)
    b = TailscaleMesh(config(tmp_path/'b', 'b'), B, Mock())
    a.remove_device('removed')
    hello = dict(group_id=group_id(cfg), protocol=3, peer_addresses={
        'c': '100.64.0.3', 'a': '100.64.0.4', 'removed': '100.64.0.5'})
    a._receive(packet(b, hello, 'hello'))
    assert a.addresses == {'b': '100.64.0.2', 'c': '100.64.0.3'}
    assert a._hello()['peer_addresses'] == a.addresses
    assert 'account' not in a._hello()
    invalid = [{'bad': '127.0.0.1'}, {'bad': '8.8.8.8'}, {'': '100.64.0.9'},
               {'bad': 123}, {str(n): '100.64.0.9' for n in range(17)}, []]
    for addresses in invalid:
        with pytest.raises(ValueError, match='共享组设备地址无效'):
            a._receive(packet(b, dict(hello, peer_addresses=addresses), 'hello'))
    assert a.addresses == {'b': '100.64.0.2', 'c': '100.64.0.3'}


def test_shared_pair_status_does_not_require_same_account_login():
    view = dict(shared_group_enabled=True, identity=dict(mode='none'), pair_scope=False,
                peers={}, connection=dict(transport='tailscale', ready=True))
    saved = presentation(dict(stage='saved'), view)
    assert '共享组' in saved['title'] and '同一个已添加账号' not in saved['detail']
    view.update(peers={'b': {}}, sync_receipts={'b': 1000})
    connected = presentation(dict(stage='saved'), view, now=1010)
    assert connected['steps'] == [True, True, True, True]
    assert '共享组' in connected['title']
    view.update(peers={}, connection=dict(transport='tailscale', state='NeedsLogin'))
    assert '授权登录 Tailscale' in presentation(dict(stage='saved'), view)['detail']


def test_three_shared_mesh_workers_transfer_both_accounts(tmp_path, monkeypatch):
    bus = {}
    ips = {'a': '100.64.0.1', 'b': '100.64.0.2', 'c': '100.64.0.3'}

    class Node:
        def __init__(self, folder, device):
            self.ip, self.inbox = ips[device], queue.Queue()

        def start(self):
            bus[self.ip] = self

        def close(self):
            bus.pop(self.ip, None)

        def request(self, method, data=None):
            if method == 'status':
                return dict(state='Running', ready=True, ips=[self.ip])
            if method == 'receive':
                result = []
                while not self.inbox.empty():
                    result.append(self.inbox.get_nowait())
                return result
            if method == 'route':
                return dict(Endpoint='1.2.3.4:1234')
            if data['ip'] not in bus:
                raise OSError('offline')
            bus[data['ip']].inbox.put(dict(ip=self.ip, envelope=data['envelope']))
            return dict(ok=True)

    monkeypatch.setattr('quota_guard.tsnet_mesh.EmbeddedNode', Node)
    received = {device: [] for device in ips}
    meshes = []
    for device, account in zip(ips, (A, A, B)):
        cfg = dict(config(tmp_path/device, device),
                   tailscale_peers={} if device == 'a' else {'a': ips['a']})
        meshes.append(TailscaleMesh(cfg, account, lambda peer, value, d=device: received[d].append(value)))

    def wait(check):
        until = time.monotonic()+20
        while not check() and time.monotonic() < until:
            time.sleep(.05)
        assert check()

    try:
        for mesh in meshes:
            mesh.start()
        wait(lambda: all(len(mesh.peer_states()) == 2 for mesh in meshes))
        meshes[0].close()
        for account in (A, B):
            meshes[1].send('c', dict(type='facts', account=account, records=[dict(seq=1)]))
        wait(lambda: {value['account'] for value in received['c'] if value['type'] == 'facts'} == {A, B})
        assert all(value['protocol'] == 3 for messages in received.values() for value in messages)
    finally:
        for mesh in meshes:
            mesh.close()
