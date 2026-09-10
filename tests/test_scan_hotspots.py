"""Idle history must be cheap without delaying new or partial usage records."""
from contextlib import contextmanager

from quota_guard.meter import Scanner
from quota_guard.storage import Database
from test_account_scope import append
from test_core import A, line, usage
from test_history_recovery import history, witnessed


def connections(database, monkeypatch):
    opened = []
    original = database.connect
    @contextmanager
    def observed():
        opened.append(1)
        with original() as db:
            yield db
    monkeypatch.setattr(database, 'connect', observed)
    return opened


def test_scanner_batches_idle_offsets_and_still_reads_growth_and_partial_lines(tmp_path, monkeypatch):
    home = tmp_path/'codex'
    (home/'sessions').mkdir(parents=True)
    for index in range(30):
        append(home/'sessions'/f'{index}.jsonl', line('session_meta', dict(id=str(index), model_provider='openai'), 105)+
               line('turn_context', dict(model='gpt-6-astra'), 110)+usage(1000, 120))
    scanner = Scanner(Database(tmp_path/'local.sqlite'), home, 'one', 100, {A: {}})
    scanner.scan(A)
    assert sum(e['tokens'] for e in scanner.pending()) == 33000
    opened = connections(scanner.db, monkeypatch)
    assert scanner.scan(A) == 0
    assert len(opened) <= 2  # Does not scale with 30 unchanged historical files.
    path = home/'sessions'/'0.jsonl'
    append(path, usage(2000, 130).rstrip('\n'))
    scanner.scan(A)
    assert sum(e['tokens'] for e in scanner.pending()) == 33000
    append(path, '\n')
    scanner.scan(A)
    assert sum(e['tokens'] for e in scanner.pending()) == 34100
    scanner.scan(A)
    assert sum(e['tokens'] for e in scanner.pending()) == 34100


def test_history_idle_skips_cursor_writes_and_keeps_late_numeric_evidence(tmp_path, monkeypatch):
    engine, recovery, path = history(tmp_path)
    for index in range(30):
        append(path.parent/f'empty-{index}.jsonl', line('session_meta', dict(id=f'empty-{index}'), 90))
    assert recovery.scan(0)
    assert recovery.reconcile(A, 0)['recovered_tokens'] == 1100
    opened = connections(recovery.db, monkeypatch)
    assert recovery.scan(0)
    assert len(opened) <= 2
    append(path, witnessed(2000, 145).rstrip('\n'))
    recovery.scan(0)
    assert recovery.reconcile(A, 0)['recovered_tokens'] == 1100
    append(path, '\n')
    recovery.scan(0)
    assert recovery.reconcile(A, 0)['recovered_tokens'] == 2200
    recovery.scan(0)
    assert recovery.reconcile(A, 0)['recovered_tokens'] == 2200
