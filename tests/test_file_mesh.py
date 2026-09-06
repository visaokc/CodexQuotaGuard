import json
import time
from datetime import datetime, timezone

from quota_guard.autolink import prepare
from quota_guard.engine import Engine
from quota_guard.file_mesh import FileMesh
from quota_guard.storage import Database, defaults


def test_three_real_native_mesh_engines_tokens_caps_restart(tmp_path):
    account = 'a'*64
    engines, configs = [], []
    identities = [prepare(tmp_path/str(i)) for i in range(3)]
    used, reset = [20], time.time()+600000
    def identity(_):
        return dict(mode='account', account=account, label='fixture', plan='pro', multiplier=1)
    def quota(_):
        return dict(account=account, used=used[0], reset_at=reset, at=time.time())
    def factory(c, a, callback):
        return FileMesh(c, a, callback, local_test=True)
    def wire():
        # Test transport uses loopback only. Production discovers these addresses automatically.
        nodes = [getattr(e.mesh, 'node', None) for e in engines]
        addresses = {}
        for node in nodes:
            if not node or not node.api:
                continue
            try:
                services = node.request('system/status')['connectionServiceStatus']
                addresses[node.device] = next(a for s in services.values() for a in s['lanAddresses'] if a.startswith('tcp://'))
            except (OSError, StopIteration):
                pass
        for node in nodes:
            if not node or not node.api:
                continue
            try:
                for device in node.request('config/devices'):
                    peer = device['deviceID']
                    if peer in addresses and peer != node.device and device['addresses'] != [addresses[peer]]:
                        device['addresses'] = [addresses[peer]]
                        node.request('config/devices/'+peer, device, 'PUT')
            except OSError:
                pass
    def wait(predicate, timeout=65):
        deadline = time.monotonic()+timeout
        while time.monotonic()<deadline:
            wire()
            for e in engines:
                e.wakeup.set()
            if predicate():
                return
            time.sleep(.5)
        raise AssertionError([e.snapshot() for e in engines])
    try:
        for i in range(3):
            folder = tmp_path/str(i)
            (folder/'codex'/'sessions').mkdir(parents=True)
            cfg = defaults()
            cfg.update(device_id=str(i), group_secret='g'*43, link_enabled=True, link_device=identities[i],
                link_peers=identities[:i], interval=1, codex_home=str(folder/'codex'), started_at=time.time()-1)
            cfg['tracked_accounts'] = {account: dict(label='fixture', added_at=0)}
            e = Engine(Database(folder/'local.sqlite'), cfg, quota_reader=quota, identity_reader=identity, mesh_factory=factory)
            configs.append(cfg); engines.append(e); e.start()
        wait(lambda: all(e.snapshot().get('summary') and len(e.snapshot()['summary']['devices']) == 3 for e in engines))
        for i, tokens in enumerate((2000, 1000, 1000)):
            stamp = datetime.now(timezone.utc).isoformat()
            rows = [dict(type='session_meta', payload=dict(id='session'+str(i))),
                dict(type='turn_context', payload=dict(model='gpt-6-astra')),
                dict(type='event_msg', timestamp=stamp, payload=dict(type='task_started')),
                dict(type='event_msg', timestamp=stamp, payload=dict(type='token_count', info=dict(total_token_usage=dict(input_tokens=tokens))))]
            (tmp_path/str(i)/'codex'/'sessions'/'test.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in rows))
        wait(lambda: all(sum(d['tokens'] for d in e.snapshot()['summary']['devices']) == 4000 for e in engines))
        used[0] = 32
        wait(lambda: all(abs(sum(d['estimated'] for d in e.snapshot()['summary']['devices'])-12)<.001 for e in engines))
        engines[1].set_cap(40)
        wait(lambda: all(next(d for d in e.snapshot()['summary']['devices'] if d['id']=='1')['cap'] == 40 for e in engines))
        engines[2].close()
        engines[2] = Engine(Database(tmp_path/'2'/'local.sqlite'), configs[2], quota_reader=quota, identity_reader=identity, mesh_factory=factory)
        engines[2].start()
        wait(lambda: engines[2].snapshot().get('summary') and len(engines[2].snapshot()['peers']) == 2)
        assert sum(d['tokens'] for d in engines[2].snapshot()['summary']['devices']) == 4000
        # A different subscription account cannot decrypt the same group's mailbox.
        from quota_guard.pairing import Cipher
        import pytest
        envelope = engines[0].mesh.cipher.seal('0', '1', 'app', {'private': True})
        with pytest.raises(Exception):
            Cipher('g'*43, 'account-policy-v2:'+'b'*64).open(envelope, '1')
    finally:
        for e in engines:
            e.close()
