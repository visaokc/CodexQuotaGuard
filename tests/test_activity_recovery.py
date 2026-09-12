import json

from quota_guard.meter import Scanner
from quota_guard.storage import Database
from test_core import A, B, line, usage


def fixture(tmp_path):
    home = tmp_path/'codex'
    (home/'sessions').mkdir(parents=True)
    path = home/'sessions'/'running.jsonl'
    path.write_text(line('session_meta', dict(id='running', model_provider='openai'), 90)+
                    line('event_msg', dict(type='task_started'), 95)+usage(1000, 99))
    scanner = Scanner(Database(tmp_path/'local.sqlite'), home, 'one', 100, {A: {}})
    return scanner, path


def test_startup_keeps_observed_activity_without_billing_old_tokens(tmp_path):
    scanner, path = fixture(tmp_path)
    scanner.seed()
    assert scanner.activity(100) == (1, 0)
    scanner.boundary(100)
    assert scanner.activity(100) == (1, 0)
    assert scanner.activity(100, A) == (0, 0)
    assert scanner.pending() == []


def test_upgrade_recovers_activity_without_moving_billing_cursor(tmp_path):
    scanner, path = fixture(tmp_path)
    scanner.seed()
    with scanner.db.connect() as db:
        state = json.loads(db.execute('SELECT state FROM cursors').fetchone()[0])
        state.update(active=False, account='')
        state.pop('activity_account', None)
        db.execute('UPDATE cursors SET state=?', (json.dumps(state),))
    scanner.seed()  # Existing 0.1.1 data, not a fresh installation.
    scanner.boundary(100)
    assert scanner.activity(101, '') == (1, 0)
    assert scanner.activity(101, A) == (0, 0)
    with scanner.db.connect() as db:
        restored = json.loads(db.execute('SELECT state FROM cursors').fetchone()[0])
    assert restored['offset'] == state['offset'] and scanner.pending() == []


def test_activity_and_billing_accounts_remain_separate_across_switch(tmp_path):
    scanner, path = fixture(tmp_path)
    scanner.seed()
    with path.open('a') as f:
        f.write(line('event_msg', dict(type='task_started', turn_id='new'), 110)+usage(2000, 115))
    scanner.scan(A)
    assert scanner.activity(120, A) == (1, 0)
    scanner.boundary(121)
    assert scanner.activity(122, B) == (0, 0)
    assert scanner.activity(122, A) == (1, 0)
    scanner.seed()
    assert scanner.activity(122, A) == (1, 0)
    with path.open('a') as f:
        f.write(usage(3000, 123))
    scanner.scan(B)
    assert not scanner.pending(account=B)


def test_completed_turn_is_not_reopened_by_trailing_accounting(tmp_path):
    scanner, path = fixture(tmp_path)
    scanner.seed()
    with path.open('a') as f:
        f.write(line('event_msg', dict(type='task_complete', turn_id='old'), 110)+usage(2000, 111))
    scanner.scan(A)
    assert scanner.activity(112) == (0, 0)
    with path.open('a') as f:
        f.write(line('event_msg', dict(type='task_started', turn_id='new'), 120)+
                line('event_msg', dict(type='task_complete', turn_id='old'), 121))
    scanner.scan(A)
    assert scanner.activity(122, A) == (1, 0)


def test_current_runtime_evidence_marks_activity_without_start_event(tmp_path):
    scanner, path = fixture(tmp_path)
    scanner.seed()
    with path.open('a') as f:
        f.write(line('event_msg', dict(type='task_complete'), 100))
    scanner.scan(A)
    assert scanner.activity(101, A) == (0, 0)
    with path.open('a') as f:
        f.write(line('turn_context', dict(model='gpt-6-astra'), 110)+
                line('response_item', dict(type='function_call'), 111))
    scanner.scan(A)
    assert scanner.activity(112, A) == (1, 0)
    with path.open('a') as f:
        f.write(line('event_msg', dict(type='task_complete'), 113)+
                line('event_msg', dict(type='token_usage_record'), 114))
    scanner.scan(A)
    assert scanner.activity(115, A) == (0, 0)


def test_parallel_active_sessions_report_each_model(tmp_path):
    scanner, path = fixture(tmp_path)
    scanner.seed()
    scanner.boundary(100)
    with path.open('a') as f:
        f.write(line('turn_context', dict(model='gpt-5.6-sol', turn_id='five'), 110)+
                usage(2000, 111))
    other = path.parent/'six.jsonl'
    other.write_text(line('session_meta', dict(id='six', model_provider='openai'), 105)+
                     line('turn_context', dict(model='gpt-6-astra', turn_id='six'), 112)+
                     usage(1000, 113))
    scanner.scan(A)
    assert scanner.activity(120, A) == (2, 0)
    assert scanner.active_models(120, A) == ['gpt-6-astra', 'gpt-5.6-sol']
    # Concurrent sessions retain their own model and numeric accounting.
    events = scanner.pending()
    assert {event['model']: event['tokens'] for event in events} == {
        'gpt-5.6-sol': 1100, 'gpt-6-astra': 1100}
    assert {event['model']: event['weight'] for event in events} == {
        'gpt-5.6-sol': .105, 'gpt-6-astra': .2625}
    with path.open('a') as f:
        f.write(line('event_msg', dict(type='task_complete', turn_id='five'), 121))
    with other.open('a') as f:
        f.write(usage(1500, 122))
    scanner.scan(A)
    assert scanner.active_models(123, A) == ['gpt-6-astra']
    assert [event['model'] for event in scanner.pending()] == [
        'gpt-5.6-sol', 'gpt-6-astra', 'gpt-6-astra']
    scanner.scan(A)
    assert len(scanner.pending()) == 3  # Repeated scans never duplicate usage.


def test_unbound_activity_reaches_authenticated_peer_presence_without_tokens(tmp_path):
    from test_account_scope import setup, append
    from quota_guard.journal import Journal
    from quota_guard.ledger import Ledger
    e, current, step, _, _, _, home = setup(tmp_path)
    append(home/'sessions'/'one.jsonl', line('session_meta', dict(id='one', model_provider='openai'), 90)+
           line('event_msg', dict(type='task_started'), 95)+usage(1000, 99))
    e.scanner.seed()
    step(100)
    view = e.snapshot()
    assert view['unbound_active'] == 1 and view['active'] == 0
    assert view['summary']['devices'][0]['tokens'] == 0
    db = Database(tmp_path/'peer.sqlite')
    journal = Journal(db, Ledger(db), 'two')
    journal.merge(A, e.journal.since(A, {}))
    presence = dict(device='one', account=A, at=100, scan_at=100, active=0, uncertain=0,
                    unbound_active=1, unbound_uncertain=0)
    journal.presence(A, 'one', presence, 100)
    assert journal.ledger.summary(A, 100)['devices'][0]['unbound_active'] == 1
    assert journal.ledger.summary(A, 100)['devices'][0]['tokens'] == 0
    journal.ledger.logout('one')
    assert journal.ledger.summary(A, 100)['devices'][0]['unbound_active'] == 0


def test_activity_refresh_updates_model_without_changing_billing_cursor(tmp_path):
    scanner, path = fixture(tmp_path)
    scanner.seed()
    with path.open('a') as f:
        f.write(line('turn_context', dict(model='gpt-5.6-sol', turn_id='five'), 110)+usage(2000, 111))
    scanner.scan(A)
    with scanner.db.connect() as db:
        before = json.loads(db.execute('SELECT state FROM cursors').fetchone()[0])
    with path.open('a') as f:
        f.write(line('turn_context', dict(model='gpt-6-astra', turn_id='six'), 120)+usage(3000, 121))
    scanner.refresh_activity(122)
    assert scanner.active_models(122, A) == ['gpt-6-astra']
    with scanner.db.connect() as db:
        state = json.loads(db.execute('SELECT state FROM cursors').fetchone()[0])
    assert state['offset'] == before['offset'] and state['model'] == 'gpt-5.6-sol'
    assert len(scanner.pending()) == 1
    scanner.scan(A)
    assert [(event['model'], event['tokens']) for event in scanner.pending()] == [
        ('gpt-5.6-sol', 1100), ('gpt-6-astra', 1100)]


def test_active_model_list_is_bounded_and_filters_non_identifiers(tmp_path):
    scanner, path = fixture(tmp_path)
    scanner.seed()
    with scanner.db.connect() as db:
        for index, model in enumerate(['', None, 'x'*101, 'line\nbreak', 'codex-auto-review',
                                       'gpt-spark', *['gpt-test-'+str(i) for i in range(20)]]):
            state = dict(session=str(index), activity=110, active=True, provider='openai',
                         activity_account=A, model=model)
            db.execute('INSERT INTO cursors VALUES (?,?)', (str(index), json.dumps(state)))
    models = scanner.active_models(115, A)
    assert len(models) == 16 and len(set(models)) == 16
    assert all(model.startswith('gpt-test-') for model in models)
