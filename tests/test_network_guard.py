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


def test_probe_accepts_only_bounded_matching_domain_trace(monkeypatch):
    response = Mock(status=200)
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    response.read.return_value = b'h=chatgpt.com\nip=1.1.1.1\n'
    opener = Mock()
    opener.open.return_value = response
    monkeypatch.setattr(ng.urllib.request, 'build_opener', lambda *args: opener)
    assert ng.probe('chatgpt.com') == '1.1.1.1'
    request = opener.open.call_args.args[0]
    assert request.full_url == 'https://chatgpt.com/cdn-cgi/trace'
    assert not {'Authorization', 'Cookie'}.intersection(request.headers)
    for body in (b'h=example.com\nip=1.1.1.1', b'<html>denied</html>', b'x'*4097):
        response.read.return_value = body
        with pytest.raises(ValueError):
            ng.probe('chatgpt.com')
    with pytest.raises(ValueError):
        ng.NoRedirect().redirect_request(None, None, 302, '', {}, 'http://example.com')


def test_mismatch_errors_recovery_and_expiry_never_report_false_alignment():
    ips = dict(zip(ng.HOSTS, ['1.1.1.1', '1.1.1.1']))
    guard = ng.NetworkGuard(lambda host: ips[host])
    assert guard.snapshot('1.1.1.1')['state'] == 'checking'
    guard.check()
    assert guard.snapshot('1.1.1.1')['state'] == 'aligned'
    assert guard.baseline_candidate() == '1.1.1.1'
    ips[ng.HOSTS[1]] = '8.8.8.8'
    guard.check()
    assert guard.snapshot('1.1.1.1')['state'] == 'mismatch'
    with pytest.raises(ValueError):
        guard.baseline_candidate()
    guard.checking = True
    assert guard.snapshot('1.1.1.1')['state'] == 'mismatch'
    guard.checking = False
    ips[ng.HOSTS[1]] = None
    guard.check()
    assert guard.snapshot('1.1.1.1')['state'] == 'error'
    assert 'None' not in guard.snapshot()['error']
    ips[ng.HOSTS[0]] = '8.8.8.8'
    guard.check()
    assert guard.snapshot('1.1.1.1')['state'] == 'mismatch'
    ips[ng.HOSTS[0]] = '1.1.1.1'
    ips[ng.HOSTS[1]] = '1.1.1.1'
    guard.check()
    assert guard.snapshot('1.1.1.1')['state'] == 'aligned'
    assert guard.snapshot('1.1.1.1', now=guard.checked_at+91)['state'] == 'error'
    guard.checked_at -= 61
    with pytest.raises(ValueError):
        guard.baseline_candidate()


def test_slow_probe_does_not_block_snapshot_or_duplicate_retry():
    entered, release = threading.Event(), threading.Event()
    calls = []
    def read(host):
        calls.append(host)
        entered.set()
        assert release.wait(3)
        return '1.1.1.1'
    guard = ng.NetworkGuard(read)
    guard.start()
    try:
        assert entered.wait(1)
        for _ in range(4):
            guard.retry()
        started = time.monotonic()
        assert guard.snapshot('1.1.1.1')['checking']
        assert time.monotonic()-started < .1
    finally:
        guard.close()
        release.set()
        guard.thread.join(2)
    assert not guard.thread.is_alive()
    assert sorted(calls) == sorted(ng.HOSTS)


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
    guard = controller._network_guard = ng.NetworkGuard(lambda host: '1.1.1.1')
    guard.check()
    result = controller.command('network_baseline', dict(revision=2, ip='8.8.8.8'))
    assert result['ok'], result
    assert controller._engine.commands.get_nowait() == ('group_rule', dict(kind='network_baseline', revision=2, ip='1.1.1.1'))
    group['network_baseline'] = '8.8.8.8'
    assert not controller.command('network_baseline', dict(revision=2))['ok']
    assert controller.command('network_baseline', dict(revision=2, confirmed=True))['ok']
    assert not controller.command('network_baseline', dict(revision=1, confirmed=True))['ok']
    group['can_manage'] = False
    assert not controller.command('network_baseline', dict(revision=2, confirmed=True))['ok']
    guard.retry = Mock()
    assert controller.command('network_retry')['ok']
    guard.retry.assert_called_once_with()
    assert controller.snapshot()['network_guard']['state'] == 'mismatch'
