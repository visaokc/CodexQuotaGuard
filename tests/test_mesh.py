import asyncio
import queue
import threading
import time

from quota_guard.mesh import Mesh
from quota_guard.signaling import SignalServer
from quota_guard.storage import defaults


def test_three_devices_direct_relay_reconnect_and_isolation():
    async def scenario():
        relay = SignalServer('s'*32)
        server = await relay.start(port=0)
        port = server.sockets[0].getsockname()[1]
        messages = {name: queue.Queue() for name in ('a', 'b', 'c', 'other')}
        meshes = []
        cfg = defaults()
        cfg.update(rendezvous_url=f'ws://127.0.0.1:{port}', relay_token='s'*32, stun_url='', group_secret='g'*32)
        for name in messages:
            mesh = Mesh(dict(cfg, device_id=name), 'b'*64 if name == 'other' else 'a'*64,
                        lambda peer, msg, name=name: messages[name].put((peer, msg)), local_test=True)
            mesh.start()
            meshes.append(mesh)
        try:
            deadline = time.time()+20
            while time.time() < deadline and not all(len(m.peer_states()) == 2 for m in meshes[:3]):
                await asyncio.sleep(.1)
            assert all(len(m.peer_states()) == 2 for m in meshes[:3])
            assert not meshes[3].peer_states()
            deadline = time.time()+25
            while time.time() < deadline and not meshes[0]._direct('b'):
                await asyncio.sleep(.1)
            assert meshes[0]._direct('b'), 'WebRTC direct channel did not open'
            meshes[0].send('b', {'token_test': 123})
            deadline = time.time()+5
            while messages['b'].empty() and time.time() < deadline:
                await asyncio.sleep(.1)
            assert messages['b'].get_nowait() == ('a', {'token_test': 123})
            # Disable the direct transport and verify encrypted fallback still delivers.
            meshes[0].config['force_relay'] = True
            before = relay.forwarded
            meshes[0].send('c', {'relay_test': 456})
            deadline = time.time()+5
            while messages['c'].empty() and time.time() < deadline:
                await asyncio.sleep(.1)
            assert messages['c'].get_nowait() == ('a', {'relay_test': 456})
            assert relay.forwarded > before
            # An already-open direct channel must survive a discovery-service outage.
            meshes[0].config['force_relay'] = False
            server.close()
            await server.wait_closed()
            meshes[0].send('b', {'offline_signal': True})
            deadline = time.time()+5
            while messages['b'].empty() and time.time() < deadline:
                await asyncio.sleep(.1)
            assert messages['b'].get_nowait()[1] == {'offline_signal': True}
            server = await relay.start(port=port)
            deadline = time.time()+15
            while time.time() < deadline and meshes[0].ws is None:
                await asyncio.sleep(.1)
            assert meshes[0].ws is not None
        finally:
            await asyncio.gather(*(asyncio.to_thread(m.close) for m in meshes))
            server.close()
            await server.wait_closed()
    asyncio.run(scenario())
