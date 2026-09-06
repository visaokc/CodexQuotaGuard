import asyncio
import json
import time
from datetime import datetime, timezone

from quota_guard.engine import Engine
from quota_guard.mesh import Mesh
from quota_guard.signaling import SignalServer
from quota_guard.storage import Database, defaults


def test_three_engine_account_group_sync_and_cap(tmp_path):
    async def scenario():
        relay = SignalServer('s'*32)
        server = await relay.start(port=0)
        port = server.sockets[0].getsockname()[1]
        used = [20]
        now = time.time()
        reset = now+600000
        account = 'a'*64
        engines = []
        configs = []
        def ident(_): return dict(mode='account', account=account, label='test', plan='pro', multiplier=1)
        def quota(_): return dict(account=account, used=used[0], reset_at=reset, at=time.time())
        for i in range(3):
            folder = tmp_path/str(i)
            (folder/'codex'/'sessions').mkdir(parents=True)
            cfg = defaults()
            cfg.update(device_id=str(i), name='device'+str(i), group_secret='g'*32,
                       rendezvous_url=f'ws://127.0.0.1:{port}', relay_token='s'*32, stun_url='', interval=1,
                       codex_home=str(folder/'codex'), started_at=now-1)
            cfg['tracked_accounts'] = {account: dict(label='test', added_at=now-1)}
            engine = Engine(Database(folder/'local.sqlite'), cfg, quota_reader=quota, identity_reader=ident,
                            mesh_factory=lambda c, a, cb: Mesh(c, a, cb, local_test=True))
            engine.start()
            engines.append(engine)
            configs.append(cfg)
        async def wait(predicate, timeout=25):
            deadline = time.time()+timeout
            while time.time() < deadline:
                if predicate(): return
                await asyncio.sleep(.1)
            raise AssertionError([e.snapshot() for e in engines])
        try:
            await wait(lambda: all(e.snapshot().get('summary') and len(e.snapshot()['summary']['devices']) == 3 for e in engines))
            for i, total in enumerate((2000, 1000, 1000)):
                stamp = datetime.now(timezone.utc).isoformat()
                rows = [dict(type='session_meta', payload=dict(id='session'+str(i))),
                        dict(type='turn_context', payload=dict(model='gpt-6-astra')),
                        dict(type='event_msg', timestamp=stamp, payload=dict(type='task_started')),
                        dict(type='event_msg', timestamp=stamp, payload=dict(type='token_count', info=dict(
                            total_token_usage=dict(input_tokens=total, cached_input_tokens=0, output_tokens=0))))]
                path = tmp_path/str(i)/'codex'/'sessions'/'test.jsonl'
                path.write_text(''.join(json.dumps(r)+'\n' for r in rows))
                engines[i].wakeup.set()
            await wait(lambda: all(sum(d['tokens'] for d in e.snapshot()['summary']['devices']) == 4000 for e in engines))
            used[0] = 32
            for e in engines: e.last_read = 0; e.wakeup.set()
            await wait(lambda: all(e.snapshot()['summary']['epoch']['used'] == 32 and
                                  abs(sum(d['estimated'] for d in e.snapshot()['summary']['devices'])-12) < .001 for e in engines))
            for e in engines:
                devices = {d['id']: d for d in e.snapshot()['summary']['devices']}
                assert [devices[str(i)]['estimated'] for i in range(3)] == [6, 3, 3]
            engines[1].set_cap(40)
            await wait(lambda: all(next(d for d in e.snapshot()['summary']['devices'] if d['id'] == '1')['cap'] == 40 for e in engines))
            # Reconstruct a client from the same persisted local/group stores.
            await asyncio.to_thread(engines[2].close)
            replacement = Engine(Database(tmp_path/'2'/'local.sqlite'), configs[2], quota_reader=quota, identity_reader=ident,
                                 mesh_factory=lambda c, a, cb: Mesh(c, a, cb, local_test=True))
            engines[2] = replacement
            replacement.start()
            await wait(lambda: replacement.snapshot().get('summary') and len(replacement.snapshot()['summary']['devices']) == 3)
            assert sum(d['tokens'] for d in replacement.snapshot()['summary']['devices']) == 4000
            assert sum(d['estimated'] for d in replacement.snapshot()['summary']['devices']) == 12
        finally:
            await asyncio.gather(*(asyncio.to_thread(e.close) for e in engines))
            server.close()
            await server.wait_closed()
    asyncio.run(scenario())
