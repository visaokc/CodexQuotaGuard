from datetime import datetime
import hashlib
from unittest.mock import MagicMock, patch

import pytest

from quota_guard.accounts import enroll
from quota_guard.engine import Engine
from quota_guard.gui import number
from quota_guard.storage import Database, defaults
from quota_guard import startup
from test_account_scope import ident, setup
from test_core import A, B, FakeFirewall


@pytest.mark.parametrize('kind', ['scheduled', 'early', 'reset_card_x1'])
def test_three_devices_reset_together_but_history_and_other_account_survive(tmp_path, kind):
    engines = []
    for i in range(3):
        cfg = defaults()
        cfg.update(device_id=str(i), codex_home=str(tmp_path/'codex'), auto_block=True)
        enroll(cfg, ident(A), 0)
        enroll(cfg, ident(B), 0)
        e = Engine(Database(tmp_path/str(i)/'local.sqlite'), cfg, firewall=FakeFirewall())
        engines.append(e)
        e.journal.append(A, 'profile', dict(device=str(i), name=str(i), cap=10+i*10), 90)
        e.journal.append(A, 'events', [dict(id=hashlib.sha256(str(i).encode()).hexdigest(), device=str(i),
            account=A, ts=150, model='gpt-6-astra', tokens=1000, weight=1, known=True)], 150)
        e.journal.append(B, 'profile', dict(device=str(i), name=str(i), cap=55), 90)
        e.journal.append(B, 'quota', dict(account=B, at=100, used=50, reset_at=9999), 100)
    source = engines[0]
    for at, used in [(100, 0), (200, 99), (350, 99)]:
        source.journal.append(A, 'quota', dict(account=A, at=at, used=used, reset_at=500), at)
    def sync(targets=None):
        for e in engines if targets is None else targets:
            for other in engines:
                batch = other.journal.since(A, e.journal.vector(A))
                if batch:
                    e.journal.merge(A, batch)
    sync()
    histories, old_cycles = [], []
    for e in engines:
        s = e.ledger.summary(A, 350)
        e.enforce(s, 350)
        assert e.blocked
        assert [d['estimated'] for d in s['devices']] == [33, 33, 33]
        old_cycles.append(s['epoch']['cycle'])
        histories.append(e.ledger.history(A, e.config['device_id'], 350))
    if kind == 'scheduled':
        refresh_at = 501
        source.journal.append(A, 'quota', dict(account=A, at=501, used=0, reset_at=500+604800), 501)
    else:
        # Card redemption intent alone isn't a quota reset. Until the official weekly
        # snapshot changes, all clients remain capped; no actual credit is consumed.
        if kind == 'reset_card_x1':
            source.journal.append(A, 'quota', dict(account=A, at=355, used=99, reset_at=500), 355)
            sync()
            assert all(e.ledger.summary(A, 355)['epoch']['cycle'] == old_cycles[i] for i, e in enumerate(engines))
        reset_at = 10000 if kind == 'early' else 500
        source.journal.append(A, 'quota', dict(account=A, at=360, used=0, reset_at=reset_at), 360)
        sync()
        for e in engines:
            pending = e.ledger.summary(A, 360)
            assert pending['reset_pending']
            e.enforce(pending, 360)
            assert e.blocked
        source.journal.append(A, 'quota', dict(account=A, at=380, used=0, reset_at=reset_at), 380)
        refresh_at = 380
    # Simulates durable log replication; the third device receives only after reconnect.
    sync(engines[:2])
    assert engines[2].ledger.summary(A, refresh_at)['epoch']['cycle'] == old_cycles[2]
    assert engines[2].blocked
    sync([engines[2]])
    cycles = []
    for i, e in enumerate(engines):
        summary = e.ledger.summary(A, refresh_at)
        e.enforce(summary, refresh_at)
        assert not e.blocked and e.firewall.calls[-2:] == ['resume', 'restore']
        assert e.config['auto_block'] is True
        assert [d['cap'] for d in summary['devices']] == [10, 20, 30]
        assert all(d['tokens'] == 0 and d['estimated'] == 0 for d in summary['devices'])
        assert e.ledger.history(A, e.config['device_id'], 350) == histories[i]
        assert e.ledger.summary(B, refresh_at)['epoch']['used'] == 50
        cycles.append(summary['epoch']['cycle'])
    assert len(set(cycles)) == 1 and cycles[0] != old_cycles[0]


def test_remote_cap_change_is_rejected(tmp_path):
    cfg = defaults()
    e = Engine(Database(tmp_path/'local.sqlite'), cfg)
    with pytest.raises(ValueError, match='只能设置自己'):
        e.journal.merge(A, [dict(account=A, origin='one', seq=1, ts=100, kind='cap', payload=dict(device='two', cap=99))])


def test_calendar_day_iso_week_month_survive_restart_and_reset(tmp_path):
    cfg = defaults()
    e = Engine(Database(tmp_path/'local.sqlite'), cfg)
    device = cfg['device_id']
    dates = [('2025-12-31', 1000), ('2026-01-01', 2000), ('2026-01-05', 3000)]
    for i, (date, tokens) in enumerate(dates):
        ts = datetime.strptime(date+' 12:00', '%Y-%m-%d %H:%M').timestamp()
        e.journal.append(A, 'events', [dict(id=hashlib.sha256(date.encode()).hexdigest(), device=device,
            account=A, ts=ts, model='gpt-6-astra', tokens=tokens, weight=1, known=True)], ts)
    now = datetime(2026, 1, 5, 18).timestamp()
    history = e.ledger.history(A, device, now)
    assert history['week'] == {'2026-W01': 3000, '2026-W02': 3000}
    assert history['month'] == {'2025-12': 1000, '2026-01': 5000}
    assert history['current'] == dict(day=3000, week=3000, month=5000)
    assert e.ledger.history(B, device, now)['day'] == {}
    assert e.ledger.history(A, 'other', now)['day'] == {}
    replacement = Engine(Database(tmp_path/'local.sqlite'), cfg)
    assert replacement.ledger.history(A, device, now) == history
    assert number(0) == '0.00 k' and number(1500) == '1.50 k' and number(1_250_000) == '1.25 M'


def test_startup_defaults_and_scoped_enable_disable(tmp_path):
    assert defaults()['autostart'] is True
    with patch.object(startup, '_ps') as ps, patch.object(startup, 'winreg') as reg:
        key = reg.CreateKey.return_value.__enter__.return_value
        startup.apply(True, tmp_path, elevated=False)
        command = reg.SetValueEx.call_args.args[-1]
        assert '--background' in command and '--data-dir' in command and str(tmp_path) in command
        name = reg.SetValueEx.call_args.args[1]
        startup.apply(False, tmp_path)
        reg.DeleteValue.assert_called_with(key, name)
        startup.apply(True, tmp_path, elevated=True)
        script = ps.call_args.args[0]
        assert '-RunLevel Highest' in script and '-LogonType Interactive' in script
        assert '-AtLogOn' in script and '-ExecutionTimeLimit ([TimeSpan]::Zero)' in script


def test_hidden_mode_does_not_wake_for_every_peer_packet(tmp_path):
    e = Engine(Database(tmp_path/'local.sqlite'), defaults())
    e.background_mode = True
    e.receive('peer', dict(type='sync'))
    assert not e.wakeup.is_set() and e.inbox.qsize() == 1
    e.background_mode = False
    e.receive('peer', dict(type='sync'))
    assert e.wakeup.is_set()


def test_unchanged_quota_does_not_fill_replication_history(tmp_path):
    e, _, step, used, *_ = setup(tmp_path)
    step(100)
    step(101)
    with e.group_db.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM facts WHERE kind='quota'").fetchone()[0] == 1
        assert db.execute('SELECT observed_at FROM epochs ORDER BY id DESC LIMIT 1').fetchone()[0] == 101
    step(701)
    with e.group_db.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM facts WHERE kind='quota'").fetchone()[0] == 2
    used[A] += 1
    step(702)
    with e.group_db.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM facts WHERE kind='quota'").fetchone()[0] == 3
