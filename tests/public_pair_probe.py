"""Opt-in real public relays, isolated synthetic accounts. Never reads Codex auth."""
import json
from pathlib import Path
import queue
import secrets
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from quota_guard.autolink import prepare
from quota_guard.file_mesh import FileMesh
from quota_guard.pairing import create_code, read_code
from quota_guard.storage import defaults


def wait(predicate, timeout=180):
    deadline = time.monotonic()+timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.5)
    raise AssertionError('Public relay wait timed out')


def main():
    root = (Path('work')/('public-mesh-'+secrets.token_hex(4))).resolve()
    print(root, flush=True)
    started = time.monotonic()
    timings = {}
    def mark(stage):
        timings[stage] = round(time.monotonic()-started, 3)
        print(stage, timings[stage], flush=True)
    ca = defaults()
    ca.update(_data_dir=str(root/'a'), device_id='a', link_enabled=True, force_relay=True,
              link_device=prepare(root/'a'))
    inbox = queue.Queue()
    def receive(peer, message):
        if message.get('type') != 'peer_ready':
            inbox.put((peer, message))
    a = FileMesh(ca, 'synthetic-account', lambda p, m: None)
    b = None
    a.start()
    try:
        wait(lambda: a.node and a.node.api and a.node.invitation_addresses(), 90)
        ca['link_addresses'] = {a.node.device: a.node.invitation_addresses()}
        code = create_code(ca)
        mark('code_ready_s')
        cb = defaults()
        cb.update(read_code(code))
        cb.update(_data_dir=str(root/'b'), device_id='b', force_relay=True)
        b = FileMesh(cb, 'synthetic-account', receive)
        b.start()
        wait(lambda: 'b' in a.peer_states() and 'a' in b.peer_states(), 260)
        mark('peers_ready_s')
        assert '中转' in a.peer_states()['b']['route']
        a.send('b', dict(type='facts', probe_tokens=12345))
        a.send('b', dict(type='sync', vector={'a': 1}))
        received = [inbox.get(timeout=45), inbox.get(timeout=45)]
        assert {m['type'] for p, m in received} == {'facts', 'sync'}
        assert all(p == 'a' for p, m in received)
        mark('first_transfer_s')
        own = b.node.device
        b.close()
        b = FileMesh(cb, 'synthetic-account', receive)
        b.start()
        wait(lambda: b.node and b.node.device == own and 'a' in b.peer_states(), 180)
        a.send('b', dict(type='facts', restarted=True))
        deadline = time.monotonic()+45
        while time.monotonic()<deadline:
            _, value = inbox.get(timeout=45)
            if value.get('restarted'):
                break
        else:
            raise AssertionError('No data after restart')
        mark('restart_transfer_s')
        (root/'result.json').write_text(json.dumps(dict(public_relay=True, encrypted_mailboxes=True,
            facts_and_vectors=True, persistent_restart=True, timings=timings)), encoding='utf-8')
        print('PUBLIC_FILE_MESH_PAIR_TRANSFER_RESTART_OK', flush=True)
    finally:
        if b:
            b.close()
        a.close()


if __name__ == '__main__':
    main()
