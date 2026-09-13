"""Hidden windows defer display rebuilds without suspending collection or IP sync."""
from unittest.mock import Mock

from quota_guard import engine as engine_module
from test_shared_billing import add_event
from test_shared_engine import A, B, Bus
from test_network_history import report


def configured(tmp_path, current=None):
    bus = Bus()
    engine, _, _ = bus.add(tmp_path/'one', 'one', (A, B), current=current)
    engine.config.update(shared_billing_v1=True, shared_group_admin='one',
                         shared_initial_devices=['one', 'two', 'third'])
    return engine


def test_background_keeps_control_plane_and_new_usage_then_refreshes_on_focus(tmp_path, monkeypatch):
    engine = configured(tmp_path)
    rebuild = Mock(wraps=engine_module.shared_usage)
    monkeypatch.setattr(engine_module, 'shared_usage', rebuild)
    engine.step(200)
    rebuild.reset_mock()
    initial = engine.view['analytics']
    engine.background_mode = True
    engine.step(220)
    add_event({'one': engine.journal}, account=A, device='one', at=230)
    engine.commands.put(('network_report', report(230)))
    engine.step(230)
    assert rebuild.call_count == 0
    assert engine.view['analytics'] is initial
    assert engine.shared.network_reports['one']['checked_at'] == 230
    assert engine.view['network_members'][0]['report']['checked_at'] == 230
    engine.wakeup.clear()
    engine.background_mode = False
    assert engine.wakeup.is_set()
    engine.step(231)
    assert rebuild.call_count == 1
    assert engine.view['analytics']['at'] == 231
    assert sum(row['tokens'] for row in engine.view['analytics']['windows']['total']['rows']) == 1100


def test_background_start_uses_initial_display_without_heavy_rebuild(tmp_path, monkeypatch):
    engine = configured(tmp_path)
    engine.background_mode = True
    rebuild = Mock(wraps=engine_module.shared_usage)
    monkeypatch.setattr(engine_module, 'shared_usage', rebuild)
    engine.commands.put(('network_report', report(200)))
    engine.step(200)
    assert rebuild.call_count == 0
    assert engine.view['shared_group']['revision']
    assert engine.view['network_members'][0]['report']['checked_at'] == 200
    engine.background_mode = False
    engine.step(201)
    assert rebuild.call_count == 1


def test_background_still_collects_queries_and_publishes_usage(tmp_path, monkeypatch):
    engine = configured(tmp_path, current=A)
    engine.step(200)
    engine.background_mode = True
    scanner = Mock(wraps=engine.scanner.scan)
    quota = Mock(wraps=engine.quota_reader)
    publish = Mock(wraps=engine.publish_events)
    monkeypatch.setattr(engine.scanner, 'scan', scanner)
    monkeypatch.setattr(engine, 'quota_reader', quota)
    monkeypatch.setattr(engine, 'publish_events', publish)
    rebuild = Mock(wraps=engine_module.shared_usage)
    monkeypatch.setattr(engine_module, 'shared_usage', rebuild)
    engine._force_read = True
    engine.step(220)
    assert scanner.call_count and quota.call_count and publish.call_count
    assert rebuild.call_count == 0
