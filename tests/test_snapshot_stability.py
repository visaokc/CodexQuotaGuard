"""Published shared snapshots stay complete while the worker scans and synchronizes."""
import copy
import threading

import pytest

from quota_guard.web_controller import _view
from test_shared_engine import A, Bus, identity, seed


def shared_engine(tmp_path):
    bus = Bus()
    engine, selected, _ = bus.add(tmp_path/'one', 'one', (A,), A)
    engine.config.update(shared_billing_v1=True, auto_block=False,
                         shared_group_admin='one', shared_initial_devices=['one', 'two'])
    seed(engine, A)
    engine.step(2000)
    return engine, selected


def published_data(engine):
    value = _view(engine.snapshot())
    return {key: copy.deepcopy(value.get(key)) for key in
            ('display_account', 'summary', 'analytics', 'account_summaries', 'shared_group', 'billing')}


@pytest.mark.parametrize('logged_in', [True, False])
def test_poll_during_shared_refresh_keeps_complete_published_snapshot(tmp_path, monkeypatch, logged_in):
    engine, selected = shared_engine(tmp_path)
    previous = published_data(engine)
    assert previous['shared_group']['stage'] == 'billing'
    assert len(previous['summary']['devices']) == 3
    assert previous['billing']['status'] == 'waiting'
    assert previous['analytics']['windows']['hour']['start'] != 0
    if not logged_in:
        selected[0] = identity()
    entered, release = threading.Event(), threading.Event()
    update = engine._update_shared_view
    failures = []

    def pause_publication(*args, **kwargs):
        entered.set()
        if not release.wait(5):
            raise RuntimeError('test failed to release shared publisher')
        return update(*args, **kwargs)

    def step():
        try:
            engine.step(2005)
        except BaseException as error:
            failures.append(error)

    monkeypatch.setattr(engine, '_update_shared_view', pause_publication)
    worker = threading.Thread(target=step)
    worker.start()
    try:
        assert entered.wait(5), 'worker did not reach shared publication boundary'
        # This is the bridge's real concurrent read while shared work is pending.
        during = published_data(engine)
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive() and not failures
    assert during == previous
    final = published_data(engine)
    assert final['summary']['allocation'] == 'shared_official_v1'
    assert len(final['summary']['devices']) == 3
    assert final['analytics']['windows'] and final['analytics']['cycles']


@pytest.mark.parametrize('failure', ['analytics', 'overview', 'connection'])
def test_failed_shared_calculation_preserves_last_complete_snapshot(tmp_path, monkeypatch, failure):
    engine, _ = shared_engine(tmp_path)
    previous = published_data(engine)
    engine.shared_analytics_cache = {}

    def unavailable(*args, **kwargs):
        raise RuntimeError('isolated projection failure')

    if failure == 'connection':
        monkeypatch.setattr(engine.mesh, 'connection_state', unavailable)
    else:
        monkeypatch.setattr('quota_guard.engine.'+('shared_usage' if failure == 'analytics' else 'shared_overview'), unavailable)
    with pytest.raises(RuntimeError, match='isolated projection failure'):
        engine.step(2010)
    assert published_data(engine) == previous


def test_login_switch_during_scan_does_not_clear_shared_view(tmp_path, monkeypatch):
    engine, selected = shared_engine(tmp_path)
    previous = published_data(engine)
    scan = engine.scanner.scan

    def switch_during_scan(*args, **kwargs):
        result = scan(*args, **kwargs)
        selected[0] = identity()
        return result

    monkeypatch.setattr(engine.scanner, 'scan', switch_during_scan)
    engine.step(2005)
    assert published_data(engine) == previous


def test_first_shared_wait_has_group_scope_and_complete_analytics_schema(tmp_path):
    bus = Bus()
    engine, _, _ = bus.add(tmp_path/'one', 'one', (A,), None)
    value = engine.snapshot()
    assert value['shared_group_enabled']
    assert value['display_account'].startswith('group:')
    assert value['summary']['account'] == value['display_account']
    assert value['summary']['devices'] == []
    assert value['analytics']['windows']['hour']['start'] > 1_000_000_000
    assert value['analytics']['windows']['hour']['rows'] == []
    assert value['analytics']['cycles'] == []


def test_new_rule_and_cached_cycle_display_publish_in_same_snapshot(tmp_path):
    from quota_guard.shared_policy import load_rules, publish_change
    engine, _ = shared_engine(tmp_path)
    previous = engine.snapshot()
    cycle = previous['analytics']['cycles'][0]
    rules = load_rules(engine.group_db, [A], 2001)
    publish_change(engine.journal, rules, 'one', 'User one', 33,
                   dict(reset_types=[dict(account=A, started=cycle['started'], type='natural')]), 2001)
    # A received rule may change before the normal ten-second analytics TTL.
    engine.step(2001)
    current = engine.snapshot()
    assert current['shared_group']['revision'] == previous['shared_group']['revision']+1
    assert current['analytics']['cycles'][0]['reset_type'] == '自然重置'
    assert current['analytics']['at'] == 2001
