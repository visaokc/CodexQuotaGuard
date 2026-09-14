import pytest

from quota_guard.pool_accounting import personal_balance, units
from test_shared_billing import A, B, D, fresh_pool, setup_group, add_event, close_samples
from quota_guard.shared_policy import load_rules, publish_change
from quota_guard.shared_view import shared_usage, shared_overview


@pytest.mark.parametrize('cause', ['natural', 'card', 'official'])
def test_net_balance_preserves_inventory_and_reset_repayment(cause):
    pool = fresh_pool()
    pool.availability([B], 110)
    pool.spend(A, 'person1', units(40), 200)
    people = pool.summary()
    assert people['person1']['available'] == 0
    assert personal_balance(people['person1']) == pytest.approx(100/3-40)
    assert sum(personal_balance(p) for p in people.values()) == pytest.approx(60)
    pool.reset(A, [800, 1500], 800, cause)
    expected = 100/3 if cause == 'official' else 200/3-40
    assert personal_balance(pool.summary()['person1']) == pytest.approx(expected)
    assert sum(personal_balance(p) for p in pool.summary().values()) == pytest.approx(100)
    pool.check()


def test_confirmed_and_estimated_overspend_reach_member_summary(tmp_path):
    db, journals, _ = setup_group(tmp_path)
    rules = load_rules(db, [A, B], 110)
    publish_change(journals['one'], rules, 'one', 'one', 50,
                   dict(compensation=True, paused_accounts=[B]), 110)
    add_event(journals, account=A, at=150)
    journals['one'].append(A, 'quota', dict(account=A, used=40, reset_at=800, at=210), 210)
    close_samples(journals)
    rules = load_rules(db, [A, B], 500)
    data = shared_usage(db, 'group:test', {A:'账号1', B:'账号2'}, 500, rules=rules)
    assert data['billing']['status'] == 'active'
    assert data['billing']['people']['person1']['available'] == 0
    assert data['live_reporting']['balances']['person1']['available_estimate'] == pytest.approx(100/3-40)
    overview = shared_overview(db, 'group:test', {A:'账号1', B:'账号2'},
        {d:dict(name=d,online=True,accounts=[A,B],current_account=A) for d in D}, 'one', 500, data)
    assert overview['summary']['devices'][0]['available'] == pytest.approx(100/3-40)
    add_event(journals, account=A, at=450)
    data = shared_usage(db, 'group:test', {A:'账号1', B:'账号2'}, 500, rules=rules)
    assert data['live_reporting']['balances']['person1']['available_estimate'] == pytest.approx(100/3-80)
    assert data['billing']['people']['person1']['debt'] == pytest.approx(40-100/3)
    assert data['account_estimates'][A]['remaining_estimate'] == pytest.approx(20)
