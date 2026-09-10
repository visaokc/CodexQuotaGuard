"""One explicitly identified transition cycle is exempt; usage and older debt survive."""
import hashlib

import pytest

from quota_guard.journal import Journal
from quota_guard.ledger import Ledger
from quota_guard.storage import Database


A, B = 'a'*64, 'b'*64


def setup(tmp_path):
    database = Database(tmp_path/'exemption.sqlite')
    ledger = Ledger(database)
    one, two = (Journal(database, ledger, device) for device in ('one', 'two'))
    for journal in (one, two):
        journal.append(A, 'profile', dict(device=journal.device, name=journal.device, cap=50,
                                        fairness_start=100, compensation_enabled=True), 100.)
    quota(one, 100, 0, 1000)
    return database, ledger, one, two


def quota(journal, at, used, reset, account=A):
    journal.append(account, 'quota', dict(account=account, at=float(at), used=used, reset_at=reset), float(at))


def consume(one, two, at, reset):
    for journal, weight in ((one, 130), (two, 70)):
        event = dict(id=hashlib.sha256(f'{journal.device}-{at}'.encode()).hexdigest(), account=A,
            device=journal.device, ts=float(at), model='gpt-6-astra', tokens=1000, weight=weight, known=True)
        journal.append(A, 'events', [event], float(at))
    quota(one, at+10, 100, reset)


def exempt(journal, started, reset, at):
    journal.append(A, 'profile', dict(device=journal.device, name=journal.device, cap=50,
        no_debt_cycle=dict(started=started, reset_at=reset)), float(at))


def balances(ledger):
    return {row['id']: row['carry'] for row in ledger.summary(A, 5000)['devices']}


def test_only_marked_cycle_is_exempt_and_next_cycle_settles_normally(tmp_path):
    database, ledger, one, two = setup(tmp_path)
    consume(one, two, 150, 1000)
    exempt(one, 100, 1000, 200)
    quota(one, 1001, 0, 2000)
    assert balances(ledger) == {'one': 0, 'two': 0}
    with database.connect() as db:
        assert db.execute('SELECT SUM(tokens) FROM events WHERE account=?', (A,)).fetchone()[0] == 2000
        assert db.execute('SELECT used FROM epochs WHERE account=? AND started=100', (A,)).fetchone()[0] == 100
    consume(one, two, 1100, 2000)
    quota(one, 2001, 0, 3000)
    assert balances(ledger) == pytest.approx({'one': 15, 'two': -15})


def test_exemption_keeps_earlier_confirmed_debt_and_replays_identically(tmp_path):
    database, ledger, one, two = setup(tmp_path)
    consume(one, two, 150, 1000)
    quota(one, 1001, 0, 2000)
    assert balances(ledger) == pytest.approx({'one': 15, 'two': -15})
    consume(one, two, 1100, 2000)
    exempt(one, 1001, 2000, 1200)
    quota(one, 2001, 0, 3000)
    assert balances(ledger) == pytest.approx({'one': 15, 'two': -15})
    consume(one, two, 2100, 3000)
    quota(one, 3001, 0, 4000)
    expected = {'one': 30, 'two': -30}
    assert balances(ledger) == pytest.approx(expected)
    peer_db = Database(tmp_path/'peer.sqlite')
    peer_ledger = Ledger(peer_db)
    peer = Journal(peer_db, peer_ledger, 'observer')
    records = one.since(A, {}, limit=60)
    # The marker may arrive after the ended-cycle facts; projection converges.
    plain = [row for row in records if 'no_debt_cycle' not in row['payload']]
    marked = [row for row in records if row not in plain]
    peer.merge(A, list(reversed(plain)))
    assert balances(peer_ledger) == pytest.approx({'one': 45, 'two': -45})
    peer.merge(A, marked)
    peer.merge(A, records)
    peer.project(A)
    assert balances(peer_ledger) == pytest.approx(expected)
    assert peer.vector(A) == one.vector(A)
    with peer_db.connect() as db:
        assert db.execute('SELECT SUM(tokens) FROM events WHERE account=?', (A,)).fetchone()[0] == 6000


def test_cycle_marker_does_not_exempt_a_different_start_or_account(tmp_path):
    database, ledger, one, two = setup(tmp_path)
    consume(one, two, 150, 1000)
    exempt(one, 99, 1000, 200)
    one.append(B, 'profile', dict(device='one', name='one', cap=50,
        no_debt_cycle=dict(started=100, reset_at=1000)), 200.)
    quota(one, 1001, 0, 2000)
    assert balances(ledger) == pytest.approx({'one': 15, 'two': -15})


@pytest.mark.parametrize('value', [None, True, {}, {'started': 100},
    {'started': True, 'reset_at': 1000}, {'started': '100', 'reset_at': 1000},
    {'started': -1, 'reset_at': 1000}, {'started': 201, 'reset_at': 1000},
    {'started': 100, 'reset_at': True}, {'started': 100, 'reset_at': 99},
    {'started': 100, 'reset_at': 100}, {'started': float('nan'), 'reset_at': 1000},
    {'started': 100, 'reset_at': float('inf')}, {'started': 100, 'reset_at': 1000, 'extra': 1}])
def test_cycle_exemption_marker_rejects_invalid_values(tmp_path, value):
    _, _, one, _ = setup(tmp_path)
    before = one.vector(A)
    with pytest.raises(ValueError, match='周期免结转标记无效'):
        one.append(A, 'profile', dict(device='one', name='one', cap=50, no_debt_cycle=value), 200.)
    assert one.vector(A) == before
