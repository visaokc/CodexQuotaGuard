import time
from unittest.mock import Mock

import pytest

from quota_guard.pairing import Cipher, create_code, read_code
from quota_guard.storage import defaults
from quota_guard.tsnet_mesh import TailscaleMesh
from quota_guard.tsnet_node import auth_url, route_label, tail_ip
from quota_guard.transport import make_mesh


def config(tmp_path):
    return dict(defaults(), _data_dir=str(tmp_path), device_id='a', tailscale_enabled=True,
                tailscale_ip='100.64.0.1', group_secret='s'*43)


def test_new_pairing_preserves_identity_and_group(tmp_path):
    cfg = config(tmp_path)
    code = create_code(cfg)
    pair = read_code(code)
    assert code.startswith('CQG4.')
    assert pair['group_secret'] == cfg['group_secret']
    assert pair['tailscale_peers'] == {'a': '100.64.0.1'}
    assert 'device_id' not in pair and 'tailscale_ip' not in pair
    assert pair['rendezvous_url'] == ''
    for value in ('127.0.0.1', '192.168.1.1', '8.8.8.8', '::1', 'http://100.64.0.1', 'fd00::1', None):
        with pytest.raises(ValueError):
            create_code(dict(cfg, tailscale_ip=value))


def test_route_is_observed_not_inferred_from_derp_home():
    assert '待确认' in route_label({'Relay': 'hkg'})
    assert '直连' in route_label({'Endpoint': '1.2.3.4:1234'})
    assert 'DERP 中转 (hkg)' in route_label({'DERPRegionID': 20, 'DERPRegionCode': 'hkg'})
    assert 'Peer Relay' in route_label({'PeerRelay': '100.64.1.1:123:1'})
    assert '待确认' in route_label({'Err': 'timeout', 'Endpoint': 'old:1'})
    assert tail_ip('fd7a:115c:a1e0::1') and not tail_ip('100.128.0.1')
    assert not tail_ip(1681915905)
    assert auth_url('https://login.tailscale.com/a/example')
    assert not auth_url('https://login.tailscale.com.evil.test/')
    assert not auth_url('http://login.tailscale.com/')


def test_authenticated_handshake_learns_peer_and_survives_restart(tmp_path):
    cfg = config(tmp_path)
    received = Mock()
    mesh = make_mesh(cfg, 'account', received)
    assert isinstance(mesh, TailscaleMesh)
    cipher = Cipher(cfg['group_secret'], 'account-policy-v2:account')
    msg = cipher.seal('b', '*', 'hello', {'account': 'account'})
    mesh._receive(dict(ip='100.64.0.2', envelope=msg))
    assert mesh.addresses == {'b': '100.64.0.2'}
    assert 'b' in mesh.peer_states()
    received.assert_called_once_with('b', dict(type='peer_ready', account='account'))
    assert TailscaleMesh(cfg, 'account', Mock()).addresses == mesh.addresses
    mesh.peers['b'].update(route='Tailscale · 直连', route_at=time.time()-40)
    assert '待确认' in mesh.peer_states()['b']['route']
    mesh.peers['b']['last_seen'] -= 31
    assert not mesh.peer_states()


def test_wrong_account_tamper_replay_never_reaches_engine(tmp_path):
    cfg = config(tmp_path)
    received = Mock()
    mesh = TailscaleMesh(cfg, 'account', received)
    cipher = Cipher(cfg['group_secret'], 'account-policy-v2:other')
    with pytest.raises(Exception):
        mesh._receive(dict(ip='100.64.0.2', envelope=cipher.seal('b','a','app',dict(account='other'))))
    assert not received.called and not mesh.addresses
    cipher = Cipher(cfg['group_secret'], 'account-policy-v2:account')
    envelope = cipher.seal('b','a','app',dict(type='sync',account='account',records=[]))
    mesh._receive(dict(ip='100.64.0.2', envelope=envelope))
    calls = received.call_count
    with pytest.raises(ValueError):
        mesh._receive(dict(ip='100.64.0.2', envelope=envelope))
    assert received.call_count == calls


def test_offline_queue_bounded_and_keeps_facts_separate(tmp_path):
    cfg = config(tmp_path)
    cfg['tailscale_peers'] = {'b': '100.64.0.2'}
    mesh = TailscaleMesh(cfg, 'account', Mock())
    for i in range(100):
        mesh.send('b',dict(type='sync',n=i))
        mesh.send('b',dict(type='facts',n=i))
        mesh.send('unknown',dict(type='sync'))
    assert len(mesh.pending)==2
    assert mesh.pending['b','facts']['n']==99


def test_two_real_mesh_workers_pair_transfer_and_restart(tmp_path, monkeypatch):
    import queue
    bus = {}
    addresses = {'a': '100.64.0.1', 'b': '100.64.0.2'}
    class Node:
        def __init__(self, folder, device):
            self.ip = addresses[device]
            self.inbox = queue.Queue()
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
                return dict(DERPRegionID=20, DERPRegionCode='hkg')
            if data['ip'] not in bus:
                raise OSError('offline')
            bus[data['ip']].inbox.put(dict(ip=self.ip, envelope=data['envelope']))
            return dict(ok=True)
    monkeypatch.setattr('quota_guard.tsnet_mesh.EmbeddedNode', Node)
    ca, cb = config(tmp_path/'a'), config(tmp_path/'b')
    cb.update(device_id='b', tailscale_peers={'a': '100.64.0.1'})
    got_a, got_b = [], []
    a = TailscaleMesh(ca, 'account', lambda p, v: got_a.append(v))
    b = TailscaleMesh(cb, 'account', lambda p, v: got_b.append(v))
    def wait(check):
        end = time.monotonic()+12
        while not check() and time.monotonic()<end:
            time.sleep(.05)
        assert check()
    try:
        a.start(); b.start()
        wait(lambda: 'b' in a.peer_states() and 'a' in b.peer_states())
        a.send('b', dict(type='facts', account='account', records=[dict(seq=1, tokens=123)]))
        wait(lambda: any(v.get('type')=='facts' for v in got_b))
        assert next(v for v in got_b if v.get('type')=='facts')['records'][0]['tokens']==123
        b.close()
        b = TailscaleMesh(cb, 'account', lambda p,v: got_b.append(v))
        b.start()
        b.send('a',dict(type='sync',account='account',vector={'a':1}))
        wait(lambda: any(v.get('vector')=={'a':1} for v in got_a))
        assert a.addresses['b']=='100.64.0.2'
    finally:
        a.close(); b.close()
    assert not bus

def test_local_reset_does_not_restart_authorized_node(tmp_path, monkeypatch):
    nodes = []
    class Node:
        def __init__(self, folder, device):
            self.process = Mock(pid=42)
            self.process.poll.return_value = None
            self.calls = 0
            self.closed = False
            nodes.append(self)
        def start(self):
            pass
        def close(self):
            self.closed = True
        def request(self, method, data=None):
            self.calls += 1
            if self.calls == 2:
                raise ConnectionResetError(10054, 'connection reset')
            if method == 'status':
                return dict(state='Running', ready=True, ips=['100.64.0.1'])
            return []
    monkeypatch.setattr('quota_guard.tsnet_mesh.EmbeddedNode', Node)
    mesh = TailscaleMesh(config(tmp_path), 'account', Mock())
    try:
        mesh.start()
        end = time.monotonic()+8
        while time.monotonic()<end:
            if nodes and nodes[0].calls >= 4:
                break
            time.sleep(.05)
        assert len(nodes) == 1, 'transient IPC reset restarted enrolled node'
        assert not nodes[0].closed
        assert nodes[0].calls >= 4
        assert mesh.connection_state().get('ready')
    finally:
        mesh.close()

def test_dead_node_error_is_not_hidden(tmp_path):
    mesh = TailscaleMesh(config(tmp_path), 'account', Mock())
    mesh.node = Mock()
    mesh.node.process.poll.return_value = 1
    mesh.node.request.side_effect = ConnectionResetError(10054, 'reset')
    with pytest.raises(ConnectionResetError):
        mesh._poll_local('status')


def test_login_during_recovery_does_not_claim_node_missing(monkeypatch):
    from quota_guard.gui import App
    show = Mock()
    monkeypatch.setattr('quota_guard.gui.messagebox.showinfo', show)
    app = Mock(demo=False)
    app.engine.mesh.connection_state.return_value = dict(phase='retrying', ready=False)
    App.login_tailscale(app)
    assert '无需重复登录' in show.call_args.args[1]
    assert '请先生成' not in show.call_args.args[1]

def test_group_removal_gossips_persists_and_blocks_sync(tmp_path):
    ca = config(tmp_path/'a')
    cb = dict(config(tmp_path/'b'), device_id='b')
    cc = dict(config(tmp_path/'c'), device_id='c')
    a, b, c = [TailscaleMesh(cfg, 'account', Mock()) for cfg in (ca, cb, cc)]
    cipher = Cipher(ca['group_secret'], 'account-policy-v2:account')
    def hello(receiver, sender, removed):
        receiver._receive(dict(ip='100.64.0.2', envelope=cipher.seal(sender, '*', 'hello',
            dict(account='account', removed_devices=removed))))
    hello(a, 'b', [])
    a.remove_device('b')
    assert 'b' not in a.peer_states()
    a.send('b', dict(type='sync'))
    assert not a.pending
    # An offline third member learns the tombstone through another member.
    hello(c, 'a', ['b'])
    restarted = TailscaleMesh(cc, 'account', Mock())
    assert restarted.removed_devices() == {'b'}
    hello(restarted, 'b', [])
    assert 'b' not in restarted.peer_states()
    hello(b, 'c', ['b'])
    b.send('a', dict(type='sync'))
    assert not b.pending and not b.peer_states()
    with pytest.raises(ValueError):
        a.remove_device('a')
    # Concurrent removals converge by union, not last-writer-wins.
    hello(c, 'a', ['d'])
    assert c.removed_devices() == {'b', 'd'}


def test_remove_device_requires_confirmation_and_cannot_remove_self(monkeypatch):
    import threading
    from quota_guard.gui import App
    app = Mock()
    app.config = dict(device_id='a')
    app.table.selection.return_value = ('b',)
    app.engine.view_lock = threading.Lock()
    app.engine.view = dict(summary=dict(account='account', devices=[dict(id='b', name='Other')]))
    confirm = Mock(return_value=False)
    monkeypatch.setattr('quota_guard.gui.messagebox.askokcancel', confirm)
    monkeypatch.setattr('quota_guard.gui.messagebox.showinfo', Mock())
    App.remove_device(app)
    app.engine.commands.put.assert_not_called()
    assert confirm.call_args.kwargs['default'] == 'cancel'
    confirm.return_value = True
    App.remove_device(app)
    app.engine.commands.put.assert_called_once_with(('remove_device', dict(account='account', device='b')))
    app.table.selection.return_value = ('a',)
    confirm.reset_mock()
    App.remove_device(app)
    confirm.assert_not_called()

def test_overview_deletes_removed_row_without_mutating_history():
    import copy
    from quota_guard.gui import App
    app = Mock()
    app.config = dict(defaults(), device_id='a')
    app.pair_flow = {}
    app.history_values = {}
    app.cards = {k: Mock() for k in ('global', 'local', 'reset')}
    app.meter = {}
    app.table.get_children.return_value = ('a', 'b')
    devices = [dict(id=p, name=p, active=0, uncertain=0, online=False,
                    tokens=100, estimated=1, cap=50, removed=p=='b') for p in ('a', 'b')]
    summary = dict(devices=devices, epoch=dict(used=10, reset_at=time.time()+100, baseline=0),
                   unassigned=0, provisional=0, reset_pending=False)
    original = copy.deepcopy(summary)
    App.render(app, dict(summary=summary))
    app.table.delete.assert_called_once_with('b')
    assert app.table.item.call_count == 1
    assert app.table.item.call_args.args[0] == 'a'
    assert summary == original
    assert '已配对设备合计 100.00 k' not in app.cycle_tokens.set.call_args.args[0]
    assert app.cycle_tokens.set.call_args.args[0].endswith('已配对设备合计 0.10 k')

def test_explicit_repair_restores_membership_but_restart_does_not(tmp_path):
    ca = config(tmp_path/'a')
    cb = dict(config(tmp_path/'b'), device_id='b', tailscale_peers={'a':'100.64.0.1'})
    cc = dict(config(tmp_path/'c'), device_id='c')
    a, b, c = [TailscaleMesh(cfg, 'account', Mock()) for cfg in (ca, cb, cc)]
    cipher = Cipher(ca['group_secret'], 'account-policy-v2:account')
    def exchange(receiver, sender):
        receiver._receive(dict(ip='100.64.0.1', envelope=cipher.seal(sender.device, '*', 'hello',
            dict(account='account', membership=sender.membership_records()))))
    a.remove_device('b')
    old_removal = a.membership_records()
    exchange(b, a)
    b = TailscaleMesh(cb, 'account', Mock())
    exchange(b, a)
    assert b.removed_devices() == {'b'}
    # Only accepting a pairing code creates a fresh request.
    cb['tailscale_join_request'] = 'explicit-pair-1'
    b = TailscaleMesh(cb, 'account', Mock())
    exchange(b, a)
    assert 'b' not in b.removed_devices()
    exchange(a, b)
    exchange(c, a)
    assert not a.removed_devices() and not c.removed_devices()
    c._merge_membership(old_removal)
    assert not c.removed_devices(), 'stale removal must not undo newer rejoin'
    a.remove_device('b')
    exchange(b, a)
    b = TailscaleMesh(cb, 'account', Mock())
    exchange(b, a)
    assert 'b' in b.removed_devices(), 'consumed pairing request must not auto-rejoin'
    cb['tailscale_join_request'] = 'explicit-pair-2'
    b = TailscaleMesh(cb, 'account', Mock())
    exchange(b, a)
    exchange(a, b)
    assert not a.removed_devices()

def test_removed_device_can_get_membership_reply_from_new_pair_target(tmp_path):
    cfg = dict(config(tmp_path), device_id='c')
    c = TailscaleMesh(cfg, 'account', Mock())
    c.remove_device('b')
    cipher = Cipher(cfg['group_secret'], 'account-policy-v2:account')
    c._receive(dict(ip='100.64.0.2', envelope=cipher.seal('b', '*', 'hello',
        dict(account='account', membership=c.membership_records()))))
    assert c.addresses['b'] == '100.64.0.2'
    assert not c.peer_states()
    c.send('b', dict(type='facts'))
    assert not c.pending
