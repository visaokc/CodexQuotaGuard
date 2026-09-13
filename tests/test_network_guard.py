import copy
import queue
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from quota_guard import network_guard as ng
from quota_guard.shared_policy import load_rules, publish_change, validate
from test_shared_billing import A, B, setup_group
from test_web_controller import controller


@pytest.mark.parametrize('value', ['127.0.0.1', '192.168.1.1', '100.64.0.1', '::1', 'ff02::1', '8.8.8.8:80', 'https://1.1.1.1', 'fe80::1%3', None])
def test_baseline_rejects_non_public_addresses(value):
    with pytest.raises(ValueError):
        ng.public_ip(value)


def geo(ip='1.1.1.1'):
    return dict(ip=ip, location='美国 加利福尼亚州 洛杉矶', country='美国')


def guard(reader=lambda: geo(), risk_reader=lambda ip: dict(purity='极度纯净', risk_score=2)):
    return ng.NetworkGuard(reader, risk_reader, process_reader=lambda: True)


def test_probe_uses_documented_geo_and_bounded_https_without_account_credentials(monkeypatch):
    response = Mock(status=200)
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    response.read.return_value = '1.1.1.1\n美国 洛杉矶\nAS1\nISP\n'.encode()
    opener = Mock()
    opener.open.return_value = response
    monkeypatch.setattr(ng.urllib.request, 'build_opener', lambda *args: opener)
    assert ng.probe() == dict(ip='1.1.1.1', location='美国 洛杉矶', country='美国')
    request = opener.open.call_args.args[0]
    assert request.full_url == 'https://ping0.cc/geo'
    assert not {'Authorization', 'Cookie'}.intersection(request.headers)
    for body in (b'127.0.0.1\nUSA\nAS1\nISP', b'<html>denied</html>', b'x'*4097):
        response.read.return_value = body
        with pytest.raises(ValueError): ng.probe()
    with pytest.raises(ValueError):
        ng.NoRedirect().redirect_request(None, None, 302, '', {}, 'http://example.com')


def test_risk_parser_checks_ip_and_preserves_risk_instead_of_inverting_purity():
    page = '''<script>window.ip = '1.1.1.1'</script><div class="riskitem riskcurrent"><span class="value">2%</span><span class="lab"> 极度纯净 </span></div>'''
    assert ng.parse_risk(page, '1.1.1.1') == dict(purity='极度纯净', risk_score=2)
    assert ng.parse_risk(page.replace('2%', '0%'), '1.1.1.1')['risk_score'] == 0
    for invalid in (page.replace('1.1.1.1', '8.8.8.8'), page.replace('2%', '102%'), page.replace('riskcurrent', 'riskitem')):
        with pytest.raises(ValueError): ng.parse_risk(invalid, '1.1.1.1')


def test_risk_cache_is_keyed_by_ip_and_retry_refreshes_without_false_ip_alarm():
    current = ['1.1.1.1']
    risk = Mock(return_value=dict(purity='极度纯净', risk_score=2))
    item = guard(lambda: geo(current[0]), risk)
    item.check();item.check()
    assert risk.call_count == 1
    item.check(force=True)
    assert risk.call_count == 2
    current[0] = '8.8.8.8'
    risk.side_effect = ValueError('Unavailable')
    item.check()
    value = item.snapshot('8.8.8.8')
    assert value['state'] == 'aligned' and value['purity'] is None and value['risk_score'] is None
    assert value['risk_error'] and risk.call_count == 3
    item.risk_cache['risk_at'] -= 601
    item.check();assert risk.call_count == 4


def test_mismatch_errors_recovery_and_expiry_never_report_false_alignment():
    current = ['1.1.1.1']
    item = guard(lambda: geo(current[0]))
    assert item.snapshot('1.1.1.1')['state'] == 'checking'
    item.check();assert item.snapshot('1.1.1.1')['state'] == 'aligned'
    assert item.baseline_candidate() == '1.1.1.1'
    current[0] = '8.8.8.8';item.check()
    assert item.snapshot('1.1.1.1')['state'] == 'mismatch'
    item.checking = True
    assert item.snapshot('1.1.1.1')['state'] == 'mismatch'
    with pytest.raises(ValueError): item.baseline_candidate()
    item.checking = False
    current[0] = None;item.check()
    assert item.snapshot('1.1.1.1')['state'] == 'error'
    current[0] = '1.1.1.1';item.check()
    assert item.snapshot('1.1.1.1')['state'] == 'aligned'
    assert item.snapshot('1.1.1.1', now=item.checked_at+31)['state'] == 'error'
    item.checked_at -= 31
    with pytest.raises(ValueError): item.baseline_candidate()


def test_slow_probe_does_not_block_snapshot_or_duplicate_retry():
    entered, release = threading.Event(), threading.Event()
    calls = []
    def read():
        calls.append(1);entered.set()
        assert release.wait(3)
        return geo()
    item = guard(read)
    item.start()
    try:
        assert entered.wait(1)
        for _ in range(4): item.retry()
        started = time.monotonic()
        assert item.snapshot('1.1.1.1')['checking']
        assert time.monotonic()-started < .1
    finally:
        item.close();release.set();item.thread.join(2)
    assert not item.thread.is_alive() and len(calls) == 1


def test_polling_keeps_ten_second_start_interval(monkeypatch):
    item = guard()
    waits = []
    item.wakeup = SimpleNamespace(clear=lambda: None, wait=lambda seconds: (waits.append(seconds), item.closed.set()))
    ticks = iter([100, 102])
    monkeypatch.setattr(ng.time, 'monotonic', lambda: next(ticks))
    item._run()
    assert waits == [8] and item.snapshot()['check_interval'] == 10


def test_process_detection_failure_keeps_ip_monitoring():
    item = guard()
    item.process_reader = Mock(side_effect=OSError('process snapshot unavailable'))
    item.check()
    assert item.snapshot('1.1.1.1')['state'] == 'aligned'
    assert not item.snapshot()['codex_running']
    item.process_reader = lambda: True
    item.check()
    assert item.snapshot()['codex_running']


def test_baseline_is_authenticated_group_policy_and_preserves_account_rules(tmp_path):
    db, journals, _ = setup_group(tmp_path)
    rules = load_rules(db, [A, B], 400)
    old = copy.deepcopy(rules['policy'])
    publish_change(journals['one'], rules, 'one', 'one', 50, dict(network_baseline='1.1.1.1'), 410)
    updated = load_rules(db, [A, B], 420)['policy']
    assert updated['network_baseline'] == '1.1.1.1'
    assert all(updated[k] == old[k] for k in ('accounts', 'bindings', 'rates', 'compensation'))
    with pytest.raises(ValueError):
        validate(dict(updated, network_baseline='127.0.0.1'), 'one', 420)
    with pytest.raises(ValueError):
        publish_change(journals['two'], load_rules(db, [A, B], 420), 'two', 'two', 50, dict(network_baseline='8.8.8.8'), 430)


def test_bridge_uses_verified_current_ip_and_admin_revision_not_browser_supplied_ip(controller):
    group = dict(can_manage=True, revision=2, network_baseline=None)
    controller._config.update(shared_group_enabled=True, shared_billing_v1=True)
    controller._engine = SimpleNamespace(snapshot=lambda: dict(shared_group=group), commands=queue.Queue(), wakeup=threading.Event())
    item = controller._network_guard = guard()
    item.check()
    result = controller.command('network_baseline', dict(revision=2, ip='8.8.8.8'))
    assert result['ok'], result
    assert controller._engine.commands.get_nowait() == ('group_rule', dict(kind='network_baseline', revision=2, ip='1.1.1.1'))
    group['network_baseline'] = '8.8.8.8'
    assert not controller.command('network_baseline', dict(revision=2))['ok']
    assert controller.command('network_baseline', dict(revision=2, confirmed=True))['ok']
    assert not controller.command('network_baseline', dict(revision=1, confirmed=True))['ok']
    group['can_manage'] = False
    assert not controller.command('network_baseline', dict(revision=2, confirmed=True))['ok']
    item.retry = Mock()
    assert controller.command('network_retry')['ok']
    item.retry.assert_called_once_with()
    assert controller.snapshot()['network_guard']['state'] == 'mismatch'


def test_native_alert_only_when_codex_running_and_once_until_recovery(controller):
    group = dict(network_baseline='8.8.8.8')
    controller._engine = SimpleNamespace(snapshot=lambda: dict(shared_group=group), commands=queue.Queue(), wakeup=threading.Event())
    controller._hidden = True
    controller._window_handler = Mock(return_value=dict(ok=True))
    item = controller._network_guard = guard()
    item.on_result = controller._network_result
    item.process_reader = lambda: False
    item.check();assert not controller._window_handler.called
    item.process_reader = lambda: True
    item.check();item.check()
    controller._window_handler.assert_called_once_with('network_alert')
    group['network_baseline'] = '1.1.1.1';item.check()
    group['network_baseline'] = '8.8.8.8';item.check()
    assert controller._window_handler.call_count == 2
    item.process_reader = lambda: False;item.check()
    item.process_reader = lambda: True;item.check()
    assert controller._window_handler.call_count == 3


def test_host_alert_reveals_window_and_signals_overview():
    from quota_guard.web_host import DesktopHost
    host = DesktopHost(Mock(), Mock(), SimpleNamespace())
    host.show = Mock()
    assert host.dispatch('network_alert')['ok'] and host.network_alert_pending
    assert not host.show.called
    host.ready = True
    assert host.dispatch('network_alert')['ok']
    host.show.assert_called_once_with(alert=True)
    host.window.evaluate_js.assert_called_once_with("window.dispatchEvent(new Event('network-alert'))")


def test_member_network_bridge_exports_only_display_fields(controller):
    controller._demo_view['network_members'] = [dict(id='person1', name='Local', online=True, device='one',
        report=dict(geo(), checked_at=100, secret='hidden'), history=[dict(geo(), checked_at=90, secret='hidden')])]
    result = controller.snapshot()['view']['network_members'][0]
    assert result['report']['ip'] == '1.1.1.1' and result['history'][0]['checked_at'] == 90
    assert 'secret' not in result['report'] and 'secret' not in result['history'][0]
