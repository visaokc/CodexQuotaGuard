"""Historical addresses are erased, while sequence continuity and accounting remain intact."""
import copy
import hashlib
import json

import pytest

from quota_guard.journal import Journal, canonical
from quota_guard.ledger import Ledger
from quota_guard.network_history import members, redact_persisted, validate_change
from quota_guard.web_controller import _view
from test_network_history import A, Bus, report, rules, seed


def legacy(sync, seq, at, changed=True):
    value = report(at, '2001:4860:4860::8844',
                   previous_ip='9.9.9.9' if changed else None, codex_running=True)
    return dict(account=A, origin='one', seq=seq, ts=at, kind='profile',
                payload=dict(device='one', name='User one', cap=50, network_report=value))


def raw_insert(sync, record):
    with sync.db.connect() as db:
        db.execute('INSERT INTO facts VALUES (?,?,?,?,?,?,?)',
                   tuple(record[key] for key in ('account','origin','seq','ts','kind'))+
                   (canonical(record['payload']), hashlib.sha256(canonical(record).encode()).hexdigest()))


def test_migration_erases_addresses_preserves_other_facts_tables_and_vectors(tmp_path):
    sync = Bus().add(tmp_path, 'one', (A,))
    seed(sync, A)
    with sync.db.connect() as db:
        original = [tuple(row) for row in db.execute('SELECT * FROM facts ORDER BY origin,seq')]
        tables = {name: [tuple(row) for row in db.execute('SELECT * FROM '+name)]
                  for name in ('events','event_details','epochs','segments','devices')}
    start = sync.journal.vector(A)['one']+1
    rows = [legacy(sync, start, 200, False), legacy(sync, start+1, 210)]
    for record in rows:
        raw_insert(sync, record)
    vector = sync.journal.vector(A)
    fresh = Journal(sync.db, Ledger(sync.db), 'one')
    assert fresh.vector(A) == vector
    assert redact_persisted(sync.db) == 0
    with sync.db.connect() as db:
        assert [tuple(row) for row in db.execute('SELECT * FROM facts WHERE seq<? ORDER BY origin,seq', (start,))] == original
        assert {name: [tuple(row) for row in db.execute('SELECT * FROM '+name)] for name in tables} == tables
        assert db.execute("SELECT COUNT(*) FROM facts WHERE json_type(payload,'$.network_report') IS NOT NULL").fetchone()[0] == 0
        assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    history = members(sync.db, rules(), {}, {}, 'one', 220)[0]['history']
    assert history == [dict(checked_at=210, device='one', device_name='User one')]
    for path in (sync.db.path, sync.db.path.with_name(sync.db.path.name+'-wal')):
        if path.exists():
            assert b'2001:4860:4860::8844' not in path.read_bytes()
            assert b'9.9.9.9' not in path.read_bytes()
    # Replayed legacy packets converge to the same address-free fact, without restoring addresses.
    assert fresh.merge(A, rows) == 0
    rows[0]['payload']['name'] = 'Changed identity'
    with pytest.raises(ValueError, match='分叉'):
        fresh.merge(A, rows[:1])


def test_legacy_packet_redacted_before_first_write_and_forwarding(tmp_path):
    sync = Bus().add(tmp_path, 'one', (A,))
    row = legacy(sync, 1, 200)
    untouched = copy.deepcopy(row)
    assert sync.journal.merge(A, [row]) == 1
    assert row == untouched
    sent = sync.journal.since(A, {})
    assert sent[0]['payload']['network_change'] == {'checked_at':200}
    assert 'network_report' not in sent[0]['payload']
    assert '9.9.9.9' not in json.dumps(sent) and '2001:4860' not in json.dumps(sent)


@pytest.mark.parametrize('value', [
    {'checked_at':True}, {'checked_at':float('nan')}, {'checked_at':300},
    {'checked_at':200,'ip':'9.9.9.9'}, {'checked_at':200,'location':'secret'},
])
def test_address_free_change_schema_rejects_extra_fields(value):
    with pytest.raises(ValueError):
        validate_change(value, 200)


def test_bridge_drops_all_legacy_history_fields():
    value = _view(dict(network_members=[dict(id='person1', history=[
        dict(checked_at=200, ip='9.9.9.9', previous_ip='1.1.1.1', location='secret', device_name='9.9.9.9')])]))
    assert value['network_members'][0]['history'] == [{'checked_at':200}]
