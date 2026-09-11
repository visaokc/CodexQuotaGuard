import json
import random

import pytest

from quota_guard.pool_accounting import FULL, Pool, points, units
from quota_guard.journal import Journal
from quota_guard.ledger import Ledger
from quota_guard.storage import Database
from quota_guard.shared_policy import PERSONS, load_rules, publish_change, validate
from quota_guard.shared_quota import accounting, attribution
from test_shared_billing import A, B, setup_group


def pool():
    value = Pool([A, B], compensation=True, rollover_since=150)
    for account in (A, B):
        value.grant(account, FULL, [100, 800], 100, initial=True)
    return value


def test_unused_balance_rolls_over_without_creating_official_inventory():
    value = pool()
    value.reset(A, [800, 1500], 800, 'natural')
    value.reset(B, [850, 1550], 850, 'card')
    assert sum(value.remaining.values()) == 2*FULL
    for person in value.summary().values():
        assert person['available'] == pytest.approx(400/3)
        assert person['available']/person['available_cap']*100 == pytest.approx(200)
        assert person['rollover'] == pytest.approx(200/3)
    with pytest.raises(ValueError, match='账号余额'):
        value.spend(A, 'person1', FULL+1, 900)


def test_redeeming_carry_preserves_other_members_rights_without_new_debt():
    value = pool()
    value.reset(A, [800, 1500], 800, 'natural')
    before = value.summary()
    value.spend(A, 'person1', FULL, 900)
    after = value.summary()
    assert after['person1']['available'] == pytest.approx(0, abs=1e-8)
    for person in ('person2', 'person3'):
        assert after[person]['available'] == pytest.approx(before[person]['available'])
    assert all(abs(row['debt']) < 1e-8 for row in after.values())
    assert any(row['kind'] == 'redeem' for row in value.entries)
    assert value.remaining[A] == 0


def test_exhausted_balance_borrows_and_repays_once_with_rollover():
    value = pool()
    value.spend(A, 'person1', FULL, 200)
    value.spend(B, 'person1', FULL, 210)
    assert value.debt('person1') > 0
    value.reset(A, [800, 1500], 800, 'natural')
    value.reset(B, [850, 1550], 850, 'card')
    previous = value.summary()
    value.reset(A, [1500, 2200], 1500, 'natural')
    for person in PERSONS:
        before = previous[person]['available']-previous[person]['debt']
        after = value.summary()[person]['available']-value.summary()[person]['debt']
        assert after-before == pytest.approx(100/3, abs=1e-8)


def test_carry_activation_does_not_restore_expired_old_periods():
    value = Pool([A, B], rollover_since=900)
    for account in (A, B):
        value.grant(account, FULL, [100, 800], 100, initial=True)
    value.reset(A, [800, 1500], 800, 'natural')
    assert not any(person['rollover'] for person in value.summary().values())
    value.reset(B, [1000, 1700], 1000, 'official')
    assert sum(person['rollover'] for person in value.summary().values()) == pytest.approx(100)


def test_random_rollover_preserves_claims_and_actual_inventory_separately():
    value, rng, claims = pool(), random.Random(630), 2*FULL
    for index in range(1500):
        account = rng.choice((A, B))
        before = value.summary()
        if rng.random() < .2 or not value.remaining[account]:
            cause = rng.choice(('natural', 'card', 'official'))
            value.reset(account, [1000+index, 2000+index], 1000+index, cause)
            claims += FULL
            if cause != 'official':
                for person in PERSONS:
                    after = value.summary()[person]
                    assert after['available']-after['debt']-before[person]['available']+before[person]['debt'] == pytest.approx(100/3, abs=1e-6)
        else:
            amount = min(value.remaining[account], rng.randint(1, 90)*units(1))
            spender = rng.choice(PERSONS)
            value.spend(account, spender, amount, 1000+index)
            claims -= amount
            for person in PERSONS:
                after = value.summary()[person]
                assert after['available']-after['debt']-before[person]['available']+before[person]['debt'] == pytest.approx(-points(amount) if person == spender else 0, abs=1e-6)
        value.check()
        assert sum(row['available'] for row in value.summary().values()) == pytest.approx(points(claims), abs=1e-6)


def test_rule_activation_replays_identically_and_preserves_current_balance(tmp_path):
    db, journals, _ = setup_group(tmp_path)
    rules = load_rules(db, [A, B], 300)
    before = accounting(rules, attribution(db, rules, 300), 300)
    policy = publish_change(journals['one'], rules, 'one', 'one', 50, dict(rollover_since=300), 300)
    revised = load_rules(db, [A, B], 300)
    after = accounting(revised, attribution(db, revised, 300), 300)
    assert before['people'] == after['people']
    for at in (801, 820):
        journals['one'].append(A, 'quota', dict(account=A, used=0, reset_at=1500, at=at), at)
    revised = load_rules(db, [A, B], 830)
    replay = accounting(revised, attribution(db, revised, 830), 830)
    assert sum(row['available'] for row in replay['people'].values()) == pytest.approx(300)
    assert replay == accounting(revised, attribution(db, revised, 830), 830)
    with db.connect() as connection:
        records = [dict(row) for row in connection.execute('SELECT * FROM facts ORDER BY seq DESC')]
    for row in records:
        row['payload'] = json.loads(row['payload'])
    for index in range(3):
        replica = Database(tmp_path/f'peer-{index}.sqlite')
        receiver = Journal(replica, Ledger(replica), f'receiver-{index}')
        ordered = list(records)
        random.Random(index).shuffle(ordered)
        for row in ordered:
            receiver.merge(row['account'], [row])
            receiver.merge(row['account'], [row])
        rules = load_rules(replica, [A, B], 830)
        assert accounting(rules, attribution(replica, rules, 830), 830) == replay
    with pytest.raises(ValueError, match='结余'):
        validate(dict(policy, rollover_since=301), 'one', 300)
    with pytest.raises(ValueError, match='结余'):
        publish_change(journals['one'], revised, 'one', 'one', 50, dict(rollover_since=0), 840)
