"""Filtering before copying preserves the bridge DTO and its lock boundary."""
import copy
import threading

from quota_guard.engine import Engine
from quota_guard.web_controller import _snapshot


def engine():
    value = Engine.__new__(Engine)
    value.config = dict(auto_block=True)
    value.shared_mode = True
    value.view_lock = threading.Lock()
    value.view = dict(notifications=['采集完成'], error='private diagnostic',
        shared_group=dict(network_baseline='1.1.1.1', members=[]),
        analytics=dict(live_reporting=dict(daily=[dict(person='person1', used=2.3)], events=[dict(id='private-event')]),
                       windows=dict(today=dict(start=1, step=2, count=1, rows=[dict(device='person1',tokens=20)]))))
    return value


def test_projected_snapshot_matches_legacy_dto_and_drains_notices_once():
    item = engine()
    expected = _snapshot(dict(copy.deepcopy(item.view), auto_block=False))
    assert item.snapshot(_snapshot) == expected
    assert item.snapshot(_snapshot)['notifications'] == []
    assert expected['notifications'] == ['采集完成']


def test_projector_runs_under_lock_and_returns_detached_nested_values():
    item = engine()
    def project(value):
        assert not item.view_lock.acquire(blocking=False)
        return _snapshot(value)
    actual = item.snapshot(project)
    actual['view']['analytics']['personal_daily'][0]['used'] = 90
    actual['view']['analytics']['windows']['today']['rows'][0]['tokens'] = 100
    assert item.view['analytics']['live_reporting']['daily'][0]['used'] == 2.3
    assert item.view['analytics']['windows']['today']['rows'][0]['tokens'] == 20
    assert item.view_lock.acquire(blocking=False)
    item.view_lock.release()


def test_private_accounting_graph_is_not_copied_for_bridge_snapshot():
    class NotForDisplay:
        def __deepcopy__(self, memo):
            raise AssertionError('Private accounting graph copied')
    item = engine()
    item.view['analytics']['official_events'] = NotForDisplay()
    item.view['analytics']['rules'] = NotForDisplay()
    projected = item.snapshot(_snapshot)['view']
    assert 'official_events' not in projected['analytics']
    assert projected['analytics']['windows']['today']['rows'][0]['tokens'] == 20


def test_legacy_snapshot_still_copies_all_internal_fields():
    item = engine()
    value = item.snapshot()
    assert value['analytics']['live_reporting']['events'] == [dict(id='private-event')]
    assert value['auto_block'] is False
    value['analytics']['live_reporting']['events'].clear()
    assert item.view['analytics']['live_reporting']['events']
