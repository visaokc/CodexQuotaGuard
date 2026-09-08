import pytest

from quota_guard.journal import Journal
from quota_guard.ledger import Ledger
from quota_guard.storage import Database
from test_core import A, event, ledger, register, snap


def totals(l, now=1000):
    return {d['id']: d['estimated'] for d in l.summary(A, now)['devices']}


def test_cycle_weights_replace_independent_interval_allocations(tmp_path):
    _, l = ledger(tmp_path)
    for d in ('one', 'two'):
        register(l, d)
    l.observe(snap(100, 0))
    l.ingest(dict(account=A, device='one', name='one', events=[event('one', 150, 3)]), 150)
    l.observe(snap(200, 27))
    l.ingest(dict(account=A, device='two', name='two', events=[event('two', 250, 1)]), 250)
    l.observe(snap(300, 30))
    assert totals(l) == pytest.approx({'one': 22.5, 'two': 7.5})
    # New tokens revise the ratio even before the next official increase.
    l.ingest(dict(account=A, device='two', name='two', events=[event('two', 350, 2)]), 350)
    assert totals(l) == pytest.approx({'one': 15, 'two': 15})
    assert sum(d['tokens'] for d in l.summary(A)['devices']) == 3000


def test_late_encrypted_journal_pages_converge_and_preserve_baseline(tmp_path):
    from quota_guard.pairing import Cipher
    journals = []
    for device in ('one', 'two'):
        db = Database(tmp_path/(device+'.sqlite'))
        journals.append(Journal(db, Ledger(db), device))
    one, two = journals
    one.append(A, 'profile', dict(device='one', name='one', cap=50), 100)
    one.append(A, 'quota', snap(100, 10), 100)
    one.append(A, 'events', [event('one', 150, 3)], 160)
    one.append(A, 'quota', snap(200, 40), 200)
    two.append(A, 'profile', dict(device='two', name='two', cap=50), 100)
    two.append(A, 'events', [event('two', 250, 1)], 260)
    assert totals(one.ledger) == {'one': 30}
    cipher = Cipher('test-only', A)
    for sender, receiver in ((one, two), (two, one)):
        while batch := sender.since(A, receiver.vector(A), limit=2):
            sealed = cipher.seal(sender.device, receiver.device, 'app', batch)
            receiver.merge(A, cipher.open(sealed, receiver.device))
    for journal in journals:
        assert totals(journal.ledger) == pytest.approx({'one': 22.5, 'two': 7.5})
        assert journal.ledger.summary(A)['epoch']['baseline'] == 10
    one.project(A)
    assert totals(one.ledger) == totals(two.ledger)
    one.append(A, 'quota', snap(10001, 0, 10000+604800), 10001)
    assert totals(one.ledger, 10002) == {'one': 0, 'two': 0}


def test_cycle_uses_weights_not_raw_token_counts_and_keeps_uncertainty(tmp_path):
    _, l = ledger(tmp_path)
    for d in ('one', 'two'):
        register(l, d)
    l.observe(snap(100, 0))
    l.observe(snap(200, 30))
    for d, w in [('one', 9), ('two', 1)]:
        l.ingest(dict(account=A, device=d, name=d, events=[event(d, 250, w)]), 250)
    assert totals(l) == pytest.approx({'one': 27, 'two': 3})
    l.ingest(dict(account=A, device='two', name='two',
                  events=[event('two', 300, 0, known=False)]), 300)
    s = l.summary(A, 1000)
    assert s['unassigned'] == 30
    assert all(d['estimated'] == 0 and d['settled'] == 0 for d in s['devices'])


def test_stability_uses_old_quota_pool_with_current_cycle_ratio(tmp_path):
    _, l = ledger(tmp_path)
    for d in ('one', 'two'):
        register(l, d, now=500)
        l.ingest(dict(account=A, device=d, name=d, events=[event(d, 150)]), 500)
    l.observe(snap(100, 0))
    l.observe(snap(200, 20))
    l.observe(snap(490, 30))
    s = l.summary(A, 500)
    assert s['provisional'] == 10
    assert [d['estimated'] for d in s['devices']] == [15, 15]
    assert [d['settled'] for d in s['devices']] == [10, 10]


@pytest.mark.parametrize('unassigned,still_blocked', [(0, False), (10, True)])
def test_reallocation_below_cap_unblocks_only_with_complete_weights(tmp_path, unassigned, still_blocked):
    from test_account_scope import setup
    e, _, step, *_ = setup(tmp_path)
    step(100)
    e.config['auto_block'] = True
    s = dict(account=A, epoch=dict(cycle='cycle', observed_at=100), reset_pending=False,
             allocation='cycle_weighted_v1', unassigned=0,
             devices=[dict(id='one', cap=25, estimated=27, settled=27)])
    e.enforce(s, 101)
    assert e.blocked
    s['devices'][0].update(estimated=20, settled=20)
    s['unassigned'] = unassigned
    e.enforce(s, 102)
    assert e.blocked is still_blocked
