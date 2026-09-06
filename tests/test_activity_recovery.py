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
