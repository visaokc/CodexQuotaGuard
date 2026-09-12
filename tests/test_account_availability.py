import copy

import pytest

from quota_guard.pool_accounting import FULL, Pool, units
from quota_guard.shared_policy import load_rules, publish_change, validate
from test_shared_billing import A, B, add_event
from test_quota_estimation import calibrated_group
from test_maintenance_reporting import view
from test_web_controller import controller


def pool():
    result = Pool([A, B], compensation=True)
    for account in (A, B):
        result.grant(account, FULL, [100, 800], 100, initial=True)
    return result


def test_pause_resume_keeps_inventory_and_counts_usage_without_refilling():
    p = pool()
    p.availability([B], 200)
    assert [v['available'] for v in p.summary().values()] == pytest.approx([100/3]*3)
    p.spend(A, 'person1', units(1), 210)
    p.spend(B, 'person1', units(2), 220)
    assert p.summary()['person1']['available'] == pytest.approx(100/3-1)
    assert p.remaining[B] == units(98)
    p.availability([], 230)
    assert p.summary()['person1']['available'] == pytest.approx(200/3-3)
    before = copy.deepcopy(p.summary())
    p.availability([], 240)
    assert p.summary() == before


def test_paused_account_cannot_fund_active_account_usage():
    p = pool()
    p.availability([B], 200)
    original = copy.deepcopy(p.stock[B])
    p.spend(A, 'person1', units(40), 210)
    assert p.stock[B] == original
    assert p.pending[A]['person1'] > 0


def test_reset_while_paused_keeps_new_inventory_and_defers_repayment():
    p = pool()
    p.confirmed.update(person1=units(6), person2=-units(3), person3=-units(3))
    p.availability([B], 200)
    p.spend(B, 'person1', units(2), 210)
    p.reset(B, [800, 1500], 800, 'official')
    assert p.confirmed['person1'] == units(6)
    p.availability([], 810)
    assert p.summary()['person1']['available'] == pytest.approx(200/3-6)
    assert p.confirmed['person1'] == 0
    before = copy.deepcopy(p.summary())
    p.availability([B], 820)
    p.availability([], 830)
    assert p.summary() == before


def change(db, journals, paused, at):
    rules = load_rules(db, [A, B], at)
    return publish_change(journals['one'], rules, 'one', 'one', 50, dict(paused_accounts=paused), at)


def test_shared_pause_keeps_statistics_and_restores_only_actual_remaining(tmp_path):
    db, journals = calibrated_group(tmp_path)
    original = view(db, 400)
    change(db, journals, [B], 410)
    paused = view(db, 420)
    assert [p['available'] for p in paused['billing']['people'].values()] == pytest.approx([100/3]*3)
    assert paused['windows']['cycle']['rows'] == original['windows']['cycle']['rows']
    add_event(journals, account=B, at=450)
    estimate = view(db, 500)
    assert all(p['estimate_pending'] == 0 for p in estimate['live_reporting']['balances'].values())
    assert estimate['live_reporting']['events']  # Paused usage still appears in statistics.
    change(db, journals, [], 510)
    resumed = view(db, 520)
    assert resumed['live_reporting']['balances']['person1']['available_estimate'] == pytest.approx(200/3-6)


def test_invalid_pause_policy_is_rejected(tmp_path):
    db, journals = calibrated_group(tmp_path)
    original = load_rules(db, [A, B], 400)['policy']
    for paused in ([B, B], ['x'*64], True):
        value = dict(original, paused_accounts=paused)
        with pytest.raises(ValueError):
            validate(value, value['admin'], 400)


def test_bridge_pause_works_without_online_members_and_rejects_non_admin(controller):
    import queue
    import threading
    from types import SimpleNamespace
    group = dict(can_manage=True, revision=2, available_accounts=[dict(account=A), dict(account=B)], devices=[])
    controller._config['shared_billing_v1'] = True
    controller._engine = SimpleNamespace(snapshot=lambda: dict(shared_group=group), commands=queue.Queue(), wakeup=threading.Event())
    payload = dict(kind='availability', revision=2, account=B, paused=True)
    assert controller.command('group_rule', payload)['ok']
    assert controller._engine.commands.get_nowait() == ('group_rule', payload)
    assert controller._engine.wakeup.is_set()
    for changes in (dict(paused='true'), dict(account='unknown'), dict(revision=1)):
        assert not controller.command('group_rule', dict(payload, **changes))['ok']
    group['can_manage'] = False
    assert not controller.command('group_rule', payload)['ok']
