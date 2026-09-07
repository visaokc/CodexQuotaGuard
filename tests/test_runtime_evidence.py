import json
import sqlite3

from quota_guard.runtime_evidence import RuntimeEvidence
from quota_guard.storage import Database
from test_core import A, B, line, usage
from test_account_scope import append, setup, ident
from test_history_recovery import witnessed


def logdb(home):
    home.mkdir(exist_ok=True, parents=True)
    path = home/'logs_2.sqlite'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE logs(id INTEGER PRIMARY KEY, ts INTEGER, ts_nanos INTEGER, target TEXT, thread_id TEXT, process_uuid TEXT, feedback_log_body TEXT)')
    return path


def log(path, ts, process='p1', thread='s', turn='t', auth=False):
    body = ('app-server event: account/updated targeted_connections=1' if auth else
            f'session_loop{{thread_id={thread}}}:turn{{otel.name="session_task.turn" thread.id={thread} turn.id={turn} model=gpt-6-astra}}:session_task.run: SECRET PROMPT MUST NOT BE STORED')
    with sqlite3.connect(path) as db:
        db.execute('INSERT INTO logs(ts,ts_nanos,target,thread_id,process_uuid,feedback_log_body) VALUES (?,?,?,?,?,?)',
                   (ts, 0, 'codex_app_server::outgoing_message' if auth else 'codex_core::session::turn', None if auth else thread, process, body))


def row(eid='1', ts=120, session='s', turn='t', start=110):
    return dict(id=eid, session=session, ts=ts, payload=json.dumps(dict(turn_id=turn, turn_at=start)))


def fixture(tmp_path):
    home = tmp_path/'codex'
    src = logdb(home)
    index = RuntimeEvidence(Database(tmp_path/'local.sqlite'), home, 'one')
    return index, src


def test_runtime_identity_can_cover_other_turn_without_per_token_auth(tmp_path):
    index, src = fixture(tmp_path)
    log(src, 110); log(src, 120)
    log(src, 130, thread='other', turn='new'); log(src, 140, thread='other', turn='new')
    assert index.scan(0)
    index.prepare([row(), row('2', 140, 'other', 'new', 130)], {'1': dict(account=A, sent=1)})
    assert index.resolve('2')['account'] == A
    assert index.resolve('2')['anchor'] == '1'


def test_restart_and_pid_reuse_do_not_inherit_old_identity(tmp_path):
    index, src = fixture(tmp_path)
    log(src, 120, process='pid:7:old')
    log(src, 220, process='pid:7:new', turn='new')
    index.scan(0)
    index.prepare([row(), row('2', 220, turn='new', start=210)], {'1': dict(account=A, sent=1)})
    assert index.resolve('2')['reason'] == 'unbound_runtime'


def test_old_request_tail_keeps_old_process_even_after_new_process_starts(tmp_path):
    index, src = fixture(tmp_path)
    log(src, 110); log(src, 120); log(src, 230)
    log(src, 210, process='p2', turn='new'); log(src, 220, process='p2', turn='new')
    index.scan(0)
    index.prepare([row(), row('tail', 230), row('new', 220, turn='new', start=210)],
                  {'1': dict(account=A, sent=1), 'new': dict(account=B, sent=1)})
    assert index.resolve('tail')['account'] == A
    assert index.resolve('new')['account'] == B


def test_auth_change_splits_new_turns_and_preserves_late_scanned_old_events(tmp_path):
    index, src = fixture(tmp_path)
    log(src, 110); log(src, 120)
    log(src, 150, auth=True)
    log(src, 160, turn='new'); log(src, 170, turn='new')
    log(src, 180)
    index.scan(0)
    index.prepare([row(), row('new', 170, turn='new', start=160), row('tail', 140), row('ambiguous', 180)],
                  {'1': dict(account=A, sent=1), 'new': dict(account=B, sent=1)})
    assert index.resolve('tail')['account'] == A
    assert index.resolve('new')['account'] == B
    assert index.resolve('new')['segment'] == 150
    assert index.resolve('ambiguous')['reason'] == 'ambiguous_runtime'


def test_conflicting_accounts_in_same_runtime_are_not_merged(tmp_path):
    index, src = fixture(tmp_path)
    log(src, 110); log(src, 150)
    index.scan(0)
    index.prepare([row(), row('2', 140), row('3', 150)],
                  {'1': dict(account=A, sent=1), '2': dict(account=B, sent=1)})
    assert index.resolve('3')['reason'] == 'identity_conflict'


def test_bounded_scan_does_not_authorize_inference_before_auth_history_loaded(tmp_path):
    index, src = fixture(tmp_path)
    log(src, 120); log(src, 150, auth=True); log(src, 220, turn='new')
    assert not index.scan(0, row_budget=1)
    index.prepare([row()], {'1': dict(account=A, sent=1)})
    assert index.resolve('1')['reason'] == 'indexing'
    assert index.scan(0)
    before = src.read_bytes()
    restarted = RuntimeEvidence(index.db, index.home, 'one')
    assert restarted.scan(0)
    assert before == src.read_bytes()
    with index.db.connect() as db:
        assert 'SECRET PROMPT' not in '\n'.join(db.iterdump())


def test_only_inference_is_not_an_identity_anchor(tmp_path):
    index, src = fixture(tmp_path)
    log(src, 120)
    index.scan(0)
    index.prepare([row()], {'1': dict(account=A, sent=1, attribution='session_inference')})
    assert index.resolve('1')['reason'] == 'unbound_runtime'


def test_explicit_provider_conflict_blocks_runtime_propagation(tmp_path):
    index, src = fixture(tmp_path)
    log(src, 110); log(src, 150)
    api = row('api', 140)
    api['payload'] = json.dumps(dict(turn_id='t', turn_at=110, provider='relay'))
    index.scan(0)
    index.prepare([row(), api, row('new', 150)], {'1': dict(account=A, sent=1)})
    assert index.resolve('new')['reason'] == 'identity_conflict'


def test_real_engine_recovers_using_runtime_identity_across_turns(tmp_path):
    e, _, step, *_rest, home = setup(tmp_path)
    src = logdb(home)
    path = home/'sessions'/'one.jsonl'
    e.journal.append(A, 'quota', dict(account=A, at=100, used=20, reset_at=10000), 100)
    e.journal.append(A, 'quota', dict(account=A, at=150, used=21, reset_at=10000), 150)
    append(path, line('session_meta', dict(id='s', model_provider='openai'), 90)+
           line('turn_context', dict(model='gpt-6-astra', turn_id='t'), 110)+witnessed(1000, 120))
    other = home/'sessions'/'other.jsonl'
    append(other, line('session_meta', dict(id='other', model_provider='openai'), 125)+
           line('turn_context', dict(model='gpt-6-astra', turn_id='new'), 130)+usage(1000, 140))
    log(src, 110); log(src, 120)
    log(src, 130, thread='other', turn='new'); log(src, 140, thread='other', turn='new')
    e.scanner.seed()
    step(160)
    assert e.db.get('history_recovery:'+A)['runtime_tokens'] == 1100
    assert sum(d['tokens'] for d in e.ledger.summary(A, 300)['devices']) == 2200


def test_engine_keeps_old_tail_in_a_while_current_login_is_api_or_b(tmp_path):
    e, current, step, _, _, queries, home = setup(tmp_path)
    src = logdb(home)
    path = home/'sessions'/'one.jsonl'
    step(100)
    append(path, line('session_meta', dict(id='s', model_provider='openai'), 105)+
           line('turn_context', dict(model='gpt-6-astra', turn_id='t'), 110)+usage(1000, 120))
    log(src, 110); log(src, 120)
    step(140)
    assert e.ledger.summary(A, 140)['devices'][0]['tokens'] == 1100
    current[0] = ident('', 'api')
    step(150)
    before = len(queries)
    append(path, usage(2000, 170)); log(src, 170)
    api = home/'sessions'/'api.jsonl'
    append(api, line('session_meta', dict(id='api', model_provider='openai'), 155)+
           line('turn_context', dict(model='gpt-6-astra', turn_id='api'), 160)+usage(5000, 170))
    # Even if a config switch is observed without an account/updated log or
    # restart, the API turn must not inherit the old process's account anchor.
    log(src, 160, process='p1', thread='api', turn='api')
    log(src, 170, process='p1', thread='api', turn='api')
    step(180)
    assert len(queries) == before and e.mesh is None
    assert e.ledger.summary(A, 180)['devices'][0]['tokens'] == 2200
    current[0] = ident(B)
    step(200)
    append(path, usage(3000, 220)); log(src, 220)
    step(230)
    assert e.ledger.summary(A, 230)['devices'][0]['tokens'] == 3300
    assert e.ledger.summary(B, 230)['devices'][0]['tokens'] == 0
