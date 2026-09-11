"""Current inventory expires independently; borrowed compensation survives."""
import copy
import json
import random

import pytest

from quota_guard.journal import Journal
from quota_guard.ledger import Ledger
from quota_guard.pool_accounting import FULL, Pool, points, units
from quota_guard.shared_policy import PERSONS, digest, load_rules, publish_change, validate
from quota_guard.shared_quota import accounting, attribution
from quota_guard.storage import Database
from test_shared_billing import A, B, setup_group


def full_pool():
    value = Pool([A, B], compensation=True, rollover_since=150)
    for account in (A, B):
        value.grant(account, FULL, [100, 800], 100, initial=True)
    value.expire_saved(300)
    return value


def test_unused_expiry_never_accumulates_and_exhaustion_does_not_shrink_base():
    value = full_pool()
    value.spend(A, 'person1', FULL, 400)
    assert all(p['available_cap'] == pytest.approx(200/3) for p in value.summary().values())
    assert value.summary()['person1']['debt'] == pytest.approx(100/3)
    value.reset(A, [800, 1500], 800, 'natural')
    after = value.summary()
    assert after['person1']['available'] == pytest.approx(0, abs=1e-8)
    assert [after[p]['available'] for p in ('person2', 'person3')] == pytest.approx([100, 100])
    assert all(abs(p['debt']) < 1e-8 for p in after.values())
    # The unused B cycle expires; only A compensation inventory remains.
    value.reset(B, [850, 1550], 850, 'natural')
    assert sum(p['available'] for p in value.summary().values()) == pytest.approx(200)
    for i in range(10):
        value.reset(A, [1500+i, 2200+i], 1500+i, 'natural')
        value.reset(B, [1550+i, 2250+i], 1550+i, 'card')
    assert all(p['available'] == pytest.approx(200/3) for p in value.summary().values())
    assert all(p['rollover'] == 0 for p in value.summary().values())


def test_expiry_cancels_only_saved_expired_stock_not_current_stock_or_debt():
    value = Pool([A, B], compensation=True, rollover_since=150)
    for account in (A, B):
        value.grant(account, FULL, [100, 800], 100, initial=True)
    value.reset(A, [800, 1500], 800, 'natural')
    value.spend(B, 'person1', FULL, 810)
    value.spend(A, 'person1', FULL, 820)
    stock = copy.deepcopy(value.stock)
    debts = {p: value.debt(p) for p in PERSONS}
    saved = sum(value.bank.values())
    value.expire_saved(830)
    assert value.stock == stock
    assert {p: value.debt(p) for p in PERSONS} == debts
    assert sum(e['amount'] for e in value.entries if e['kind'] == 'expire_saved') == pytest.approx(points(saved))
    value.reset(B, [850, 1550], 850, 'natural')
    assert sum(p['available'] for p in value.summary().values()) == pytest.approx(100)


def test_independent_refresh_keeps_other_account_and_only_adds_admitted_base():
    value = Pool([A, B], compensation=True)
    value.grant(A, 0, [100, 800], 100, initial=True)
    value.grant(B, FULL, [100, 850], 100, initial=True)
    value.spend(B, 'person1', units(50/3), 200)
    assert value.summary()['person1']['available'] == pytest.approx(50/3)
    assert value.summary()['person1']['available_cap'] == pytest.approx(100/3)
    previous_b = dict(value.stock[B])
    value.reset(A, [800, 1500], 800, 'natural')
    assert value.stock[B] == previous_b
    assert value.summary()['person1']['available'] == pytest.approx(50)
    assert value.summary()['person1']['available_cap'] == pytest.approx(200/3)


def test_expiry_preserves_historical_replay_and_replicates_once(tmp_path):
    db, journals, _ = setup_group(tmp_path)
    rules = load_rules(db, [A, B], 300)
    publish_change(journals['one'], rules, 'one', 'one', 50, dict(rollover_since=300), 300)
    for at in (801, 820):
        journals['one'].append(A, 'quota', dict(account=A, used=0, reset_at=1500, at=at), at)
    old = load_rules(db, [A, B], 830)
    history = accounting(old, attribution(db, old, 830), 830)
    assert sum(p['available'] for p in history['people'].values()) == pytest.approx(300)
    policy = publish_change(journals['one'], old, 'one', 'one', 50, dict(unused_expiry_from=840), 840)
    rules = load_rules(db, [A, B], 900)
    assert rules['status'] == 'ready'
    # Last-confirmed replay earlier than activation must not apply the new rule.
    assert accounting(rules, attribution(db, rules, 830), 830) == history
    current = accounting(rules, attribution(db, rules, 900), 900)
    assert sum(p['available'] for p in current['people'].values()) == pytest.approx(200)
    with db.connect() as connection:
        records = [dict(row) for row in connection.execute('SELECT * FROM facts')]
    for row in records:
        row['payload'] = json.loads(row['payload'])
    for i in range(3):
        replica = Database(tmp_path/f'expiry-peer-{i}.sqlite')
        journal = Journal(replica, Ledger(replica), f'receiver-{i}')
        random.Random(i).shuffle(records)
        for row in records:
            journal.merge(row['account'], [row])
            journal.merge(row['account'], [row])
        synced = load_rules(replica, [A, B], 900)
        assert accounting(synced, attribution(replica, synced, 900), 900) == current
    with pytest.raises(ValueError, match='到期'):
        validate(dict(policy, unused_expiry_from=299), 'one', 900)
    with pytest.raises(ValueError, match='到期'):
        publish_change(journals['one'], rules, 'one', 'one', 50, dict(unused_expiry_from=850), 900)
    changed = dict(policy, previous=digest(policy), revision=policy['revision']+1, effective=900)
    changed.pop('unused_expiry_from')
    journals['one'].append(A, 'profile', dict(device='one', name='one', cap=50, group_policy=changed), 900)
    assert load_rules(db, [A, B], 900)['status'] == 'conflict'


def test_random_expiry_borrowing_and_reset_conserve_real_inventory():
    value, rng = full_pool(), random.Random(640)
    for i in range(1000):
        account = rng.choice((A, B))
        if not value.remaining[account] or rng.random() < .2:
            value.reset(account, [1000+i, 2000+i], 1000+i, rng.choice(('natural', 'card', 'official')))
        else:
            value.spend(account, rng.choice(PERSONS), min(value.remaining[account], units(rng.randrange(1, 101))), 1000+i)
        value.check()
        assert sum(p['available'] for p in value.summary().values()) == pytest.approx(points(sum(value.remaining.values())))
        assert sum(p['debt'] for p in value.summary().values()) == pytest.approx(0, abs=1e-6)
        assert not any(value.bank.values())
