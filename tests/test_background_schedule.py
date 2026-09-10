"""Virtual-time scheduling with isolated journals and an actual message queue."""
import copy
from unittest.mock import Mock

from test_account_scope import setup, ident, append
from test_core import A, B, line, usage


class Wire:
    history_limit = 2
    history_bytes = 24000
    status = 'fixture'

    def __init__(self, peer):
        self.peer, self.sent, self.online = peer, [], True

    def peer_states(self):
        return {self.peer: {'route': 'fixture'}} if self.online else {}

    def send(self, peer, message):
        self.sent.append((peer, copy.deepcopy(message)))

    def close(self):
        pass


def test_background_query_cadence_local_collection_and_foreground_resume(tmp_path):
    e, current, step, used, resets, queries, home = setup(tmp_path)
    e.config['interval'] = 30
    step(100)
    e.mesh = Wire('two')
    e.background_mode = True
    step(101)
    append(home/'sessions'/'local.jsonl',
           line('session_meta', dict(id='local', model_provider='openai'), 110) +
           line('turn_context', dict(model='gpt-6-astra'), 111) + usage(1000, 112))
    step(130)
    assert len(queries) == 1
    assert sum(d['tokens'] for d in e.snapshot()['summary']['devices']) > 0
    assert not any(m['type'] in ('sync', 'facts') for _, m in e.mesh.sent)
    assert any(m['type'] == 'presence' for _, m in e.mesh.sent)
    step(699)
    assert len(queries) == 1
    step(700)
    assert len(queries) == 2
    e.wakeup.clear()
    e.background_mode = False
    assert e.wakeup.is_set()
    step(701)
    assert len(queries) == 3
    assert any(m['type'] == 'sync' for _, m in e.mesh.sent)
    e.background_mode = True
    step(702)
    current[0] = ident(B)
    step(703)
    assert queries[-1] == B


def test_hidden_packets_are_coalesced_without_quota_or_fact_work(tmp_path):
    e, _, step, *rest = setup(tmp_path)
    step(100)
    e.background_mode = True
    step(101)
    e.wakeup.clear()
    for n in range(2000):
        e.receive('two', dict(type='sync', account=A, vector={'two': n},
            presence=dict(device='two', account=A, at=102, scan_at=102, active=n % 2, uncertain=0)))
        e.receive('two', dict(type='facts', account=A, records=[]))
    assert e.inbox.empty() and len(e._pending_presence) == 1
    assert not e.wakeup.is_set()
    e.journal.merge = Mock(wraps=e.journal.merge)
    step(102)
    e.journal.merge.assert_not_called()


def test_offset_background_rounds_finish_all_pages_and_reconnect(tmp_path):
    left = setup(tmp_path/'left')
    right = setup(tmp_path/'right')
    a, _, astep, *_ = left
    b, _, bstep, *_ = right
    b.config['device_id'] = b.journal.device = b.scanner.device = 'two'
    astep(100)
    bstep(100)
    a.mesh, b.mesh = Wire('two'), Wire('one')
    for n in range(19):
        a.journal.append(A, 'cap', dict(device='one', cap=40+n % 2), 101+n)
        b.journal.append(A, 'cap', dict(device='two', cap=50+n % 2), 101+n)
    a.background_mode = True
    astep(101)
    b.background_mode = True
    bstep(201)
    a.mesh.sent.clear()
    b.mesh.sent.clear()
    astep(701)
    for _, message in a.mesh.sent:
        b.receive('one', message)
    a.mesh.sent.clear()
    bstep(702)
    assert a._sync_burst_active
    astep(750)
    assert not any(m['type'] in ('sync', 'facts') for _, m in a.mesh.sent)
    bstep(801)

    def drain(now):
        trace = []
        for n in range(40):
            for sender, receiver, origin in ((a, b, 'one'), (b, a, 'two')):
                messages, sender.mesh.sent = sender.mesh.sent, []
                for _, message in messages:
                    trace.append((n, origin, message['type'], message.get('vector'),
                                  [(r['origin'], r['seq']) for r in message.get('records', [])]))
                    receiver.receive(origin, message)
            astep(now+n*.1)
            bstep(now+n*.1)
            if not a._sync_burst_active and not b._sync_burst_active:
                return
        raise AssertionError((a.journal.vector(A), b.journal.vector(A),
                              a._sync_burst_goals, b._sync_burst_goals,
                              a.sync_errors, b.sync_errors, trace))

    drain(802)
    assert a.journal.vector(A) == b.journal.vector(A)
    a.mesh.sent.clear()
    b.mesh.sent.clear()
    a.journal.append(A, 'cap', dict(device='one', cap=43), 900)
    astep(950)
    bstep(950)
    assert not any(m['type'] in ('sync', 'facts') for _, m in a.mesh.sent+b.mesh.sent)
    a.mesh.online = b.mesh.online = False
    astep(1301)
    bstep(1401)
    a.mesh.sent.clear()
    b.mesh.sent.clear()
    a.mesh.online = b.mesh.online = True
    astep(1901)
    for _, message in a.mesh.sent:
        b.receive('one', message)
    a.mesh.sent.clear()
    bstep(1902)
    bstep(2001)
    drain(2002)
    assert a.journal.vector(A) == b.journal.vector(A)


def test_auto_limit_and_pending_reset_keep_short_query_confirmation(tmp_path):
    e, _, step, used, resets, queries, _ = setup(tmp_path)
    e.config['interval'] = 30
    step(100)
    e.background_mode = True
    e.config['auto_block'] = True
    step(130)
    assert len(queries) == 2
    e.config['auto_block'] = False
    used[A] = 0
    resets[A] += 10000
    step(730)
    assert e.group_db.get('reset_candidate:'+A)
    step(744)
    assert len(queries) == 3
    step(760)
    assert len(queries) == 4
    assert not e.group_db.get('reset_candidate:'+A)
