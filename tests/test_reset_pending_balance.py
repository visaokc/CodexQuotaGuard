import copy

import pytest

from quota_guard.shared_quota import accounting
from quota_guard.pool_accounting import units


def case():
    policy = dict(effective=1, compensation=True, unused_expiry_from=1)
    rules = dict(status='ready', policy=policy, policies=[policy], accounts=['a', 'b'])
    epochs = {}
    for account, at in [('a', 300), ('b', 310)]:
        epochs[account] = [dict(started=100, ended=at, reset_at=800, baseline=0,
                               used=0, reason='natural'),
                           dict(started=at, ended=None, reset_at=1000, baseline=0,
                                used=0, reason='官方临时重置')]
    data = dict(epochs=epochs, anchors={a: dict(at=100, used=0, cycle=e[0]) for a, e in epochs.items()},
                streams=[dict(account='b', start=110, end=200, cycle_start=100,
                              units=units(90), ready=False, reason='等待成员采集确认', events=[])],
                events=[], overrides={}, exempt=set())
    return rules, data


def test_two_official_resets_release_current_balance_without_erasing_pending_history():
    rules, data = case()
    original = copy.deepcopy(data)
    result = accounting(rules, data, 400)
    assert result['status'] == 'active'
    assert [p['available'] for p in result['people'].values()] == pytest.approx([200/3]*3)
    assert data == original
    # Late attribution may exchange rights and create pending borrowing before
    # the resets. Neither may change the new balances after both official resets.
    for person in ('person1', 'person2', 'person3'):
        completed = copy.deepcopy(data)
        completed['streams'][0].update(ready=True, reason='', events=[
            dict(id='old', account='b', device=person, ts=190, units=units(90))])
        replay = accounting(rules, completed, 400)
        assert replay['people'] == result['people']


@pytest.mark.parametrize('change', ['one_reset', 'natural', 'unknown', 'rollover', 'between'])
def test_uncertainty_that_can_affect_new_inventory_still_blocks_balance(change):
    rules, data = case()
    if change == 'one_reset':
        data['epochs']['a'].pop()
    elif change in ('natural', 'unknown'):
        data['epochs']['a'][1]['reason'] = change
    elif change == 'rollover':
        rules['policy'].pop('unused_expiry_from')
        rules['policy']['rollover_since'] = 1
    else:
        data['streams'].append(dict(account='a', start=300, end=305, cycle_start=300,
                                    units=units(1), ready=True, reason='', events=[
                                        dict(id='between', account='a', device='person1', ts=304, units=units(1))]))
    assert accounting(rules, data, 400)['status'] == 'syncing'


def test_confirmed_reset_override_and_new_consumption_are_applied():
    rules, data = case()
    data['epochs']['a'][1]['reason'] = '提前重置原因未确认'
    data['overrides']['a', 300] = '官方临时重置'
    data['streams'].append(dict(account='a', start=310, end=330, cycle_start=300,
                                units=units(2), ready=True, reason='', events=[
                                    dict(id='new', account='a', device='person1', ts=320, units=units(2))]))
    result = accounting(rules, data, 400)
    assert result['status'] == 'active'
    assert [p['available'] for p in result['people'].values()] == pytest.approx([200/3-2, 200/3, 200/3])
