"""Retain the erase obligation until SQLite can replace its old physical pages."""
import hashlib
import json
import sqlite3
from contextlib import contextmanager

import pytest

from quota_guard.journal import Journal, canonical
from quota_guard.ledger import Ledger
from quota_guard.network_history import redact_persisted
from quota_guard.storage import Database


ACCOUNT = 'a'*64
OLD_IP = '1.1.1.1'
CURRENT_IP = '8.8.8.8'
PENDING = 'network_history_erasure_pending'


class ShortWaitDatabase(Database):
    @contextmanager
    def connect(self):
        with super().connect() as connection:
            connection.execute('PRAGMA busy_timeout=50')
            yield connection


def legacy_database(tmp_path):
    database = ShortWaitDatabase(tmp_path/'privacy.sqlite')
    Journal(database, Ledger(database), 'one')
    records = [dict(account=ACCOUNT, origin='one', seq=1, ts=200, kind='profile',
        payload=dict(device='one', name='User', cap=50,
            network_report=dict(ip=CURRENT_IP, previous_ip=OLD_IP,
                                checked_at=200, codex_running=True))),
        dict(account=ACCOUNT, origin='one', seq=2, ts=201, kind='quota',
             payload=dict(account=ACCOUNT, used=3, reset_at=900, at=201))]
    # Emulate records written by the released client, before normalization existed.
    with database.connect() as connection:
        for row in records:
            connection.execute('INSERT INTO facts VALUES (?,?,?,?,?,?,?)',
                (row['account'],row['origin'],row['seq'],row['ts'],row['kind'],
                 canonical(row['payload']),hashlib.sha256(canonical(row).encode()).hexdigest()))
    assert OLD_IP.encode() in database.path.read_bytes()
    return database, records


def assert_clean(database, records):
    assert database.get(PENDING) is None
    journal = Journal(database, Ledger(database), 'one')
    rows = journal.since(ACCOUNT, {})
    assert journal.vector(ACCOUNT) == {'one':2}
    assert rows[1] == records[1]
    assert rows[0] == dict(records[0],payload=dict(device='one',name='User',cap=50,
                                                  network_change=dict(checked_at=200)))
    for suffix in ('','-wal','-journal'):
        path = database.path.with_name(database.path.name+suffix)
        if path.exists():
            content = path.read_bytes()
            assert OLD_IP.encode() not in content
            assert CURRENT_IP.encode() not in content


def test_busy_checkpoint_keeps_pending_and_retries_without_legacy_sql_rows(tmp_path):
    database, records = legacy_database(tmp_path)
    reader = sqlite3.connect(database.path)
    try:
        reader.execute('BEGIN')
        reader.execute('SELECT * FROM facts').fetchall()
        assert redact_persisted(database) == 1
        with database.connect() as connection:
            assert connection.execute("SELECT COUNT(*) FROM facts WHERE json_type(payload,'$.network_report') IS NOT NULL").fetchone()[0] == 0
        assert database.get(PENDING) is True
        # SQL is already redacted, but an old reader still pins original pages.
        assert OLD_IP.encode() in database.path.read_bytes()
        assert redact_persisted(database) == 0
        assert database.get(PENDING) is True
    finally:
        reader.close()
    # Startup must retry physical erasure even with no legacy report rows left.
    Journal(database, Ledger(database), 'one')
    assert_clean(database, records)


def test_interrupted_after_redaction_commit_retries_physical_erasure(tmp_path, monkeypatch):
    database, records = legacy_database(tmp_path)
    original_connect = database.connect

    class InterruptedConnection:
        def __init__(self, connection):
            self.connection = connection

        def execute(self, sql, *args):
            if sql == 'VACUUM':
                raise sqlite3.OperationalError('simulated interruption before vacuum')
            return self.connection.execute(sql,*args)

        def commit(self):
            self.connection.commit()

    @contextmanager
    def interrupted_connect():
        with original_connect() as connection:
            yield InterruptedConnection(connection)

    with monkeypatch.context() as patch:
        patch.setattr(database,'connect',interrupted_connect)
        with pytest.raises(sqlite3.OperationalError,match='simulated interruption'):
            redact_persisted(database)
    assert database.get(PENDING) is True
    with database.connect() as connection:
        payload = json.loads(connection.execute('SELECT payload FROM facts WHERE seq=1').fetchone()[0])
    assert 'network_report' not in payload
    Journal(database, Ledger(database), 'one')
    assert_clean(database, records)
