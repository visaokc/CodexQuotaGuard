from unittest.mock import Mock

from test_tsnet import config
from quota_guard.pairing import Cipher
from quota_guard.tsnet_mesh import TailscaleMesh


def test_brief_silence_recovers_without_offline_and_real_silence_expires(tmp_path, monkeypatch):
    clock = [1000.]
    monkeypatch.setattr('quota_guard.tsnet_mesh.time.time', lambda: clock[0])
    cfg = config(tmp_path)
    mesh = TailscaleMesh(cfg, 'account', Mock())
    cipher = Cipher(cfg['group_secret'], 'account-policy-v2:account')
    def receive():
        mesh._receive(dict(ip='100.64.0.2', envelope=cipher.seal('b', '*', 'hello', dict(account='account'))))
    receive()
    clock[0] = 1089
    assert 'b' in mesh.peer_states()
    receive()
    clock[0] = 1178
    assert 'b' in mesh.peer_states(), 'fresh authenticated traffic restarts the grace period'
    clock[0] = 1179
    assert 'b' not in mesh.peer_states()
    clock[0] = 1180
    receive()
    assert 'b' in mesh.peer_states()
    mesh.remove_device('b')
    assert not mesh.peer_states(), 'explicit removal must not wait for offline grace'


def test_one_send_failure_preserves_last_authenticated_presence(tmp_path, monkeypatch):
    cfg = config(tmp_path)
    cfg['tailscale_peers'] = {'b':'100.64.0.2'}
    mesh = TailscaleMesh(cfg, 'account', Mock())
    mesh.peers['b'] = dict(last_seen=1000., route='Tailscale · 直连', route_at=1000.)
    monkeypatch.setattr('quota_guard.tsnet_mesh.time.time', lambda: 1010.)
    class Node:
        def __init__(self, *args):
            pass
        def start(self):
            pass
        def close(self):
            pass
        def request(self, method, data=None):
            if method == 'status':
                return dict(state='Running', ready=True)
            if method == 'receive':
                return []
            if method == 'send':
                mesh.stop.set()
                raise OSError('temporary send failure')
    monkeypatch.setattr('quota_guard.tsnet_mesh.EmbeddedNode', Node)
    mesh._run()
    assert mesh.peer_states()['b']['last_seen'] == 1000., 'failed sends neither erase nor refresh evidence'


def test_online_grace_does_not_extend_stale_model_or_codex_activity(tmp_path):
    from test_shared_billing import setup_group, A, B, add_event
    from quota_guard.shared_policy import load_rules
    from quota_guard.shared_view import shared_usage, shared_overview
    db, journals, _ = setup_group(tmp_path)
    add_event(journals, device='two', at=290)
    journals['two'].presence(A, 'two', dict(device='two', account=A, at=300,
                            scan_at=300, active=1, uncertain=0), 300)
    labels = {A:'账号1',B:'账号2'}
    analytics = shared_usage(db, 'group:test', labels, 400, rules=load_rules(db, [A,B], 400))
    members = {'two':dict(name='Two', current_account=A, online=True)}
    for now, active, model in ((329, 1, 'gpt-6-astra'), (330, 0, None)):
        result = shared_overview(db, 'group:test', labels, members, 'one', now, analytics)
        person = next(p for p in result['summary']['devices'] if p['id'] == 'person2')
        assert person['online']
        assert person['active'] == active
        assert person['active_model'] == model
