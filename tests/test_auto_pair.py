from unittest.mock import patch

import pytest

from quota_guard.autolink import LinkNode, proof, system_proxies
from quota_guard.pairing import create_code, read_code
from quota_guard.storage import defaults

ONE = 'AAAAAAA-'*7+'AAAAAAA'
TWO = 'BBBBBBB-'*7+'BBBBBBB'


def test_automatic_code_needs_no_server_fields_and_keeps_local_identity():
    cfg = defaults()
    cfg.update(link_enabled=True, link_device=ONE, link_peers=[])
    code = create_code(cfg)
    result = read_code(code)
    assert code.startswith('CQG2.') and len(code) < 500
    assert result['link_peers'] == [ONE] and result['group_secret'] == cfg['group_secret']
    assert result['rendezvous_url'] == result['relay_token'] == ''
    assert 'device_id' not in result and 'link_device' not in result and 'tracked_accounts' not in result
    cfg.update(link_device=TWO, link_peers=[ONE])
    assert read_code(create_code(cfg))['link_peers'] == [TWO, ONE]
    with pytest.raises(ValueError):
        read_code(code[:-1]+'!')
    cfg['link_addresses'] = {TWO: ['relay://relay.example:22067/?id='+ONE]}
    assert read_code(create_code(cfg))['link_addresses'] == cfg['link_addresses']
    cfg['link_addresses'][TWO] = ['http://wrong.example/']
    with pytest.raises(ValueError):
        read_code(create_code(cfg))


def test_pending_device_requires_proof_bound_to_its_tls_identity():
    node = LinkNode.__new__(LinkNode)
    node.secret = 's'*43
    node.group = 'fixture'
    writes = []
    pending = {ONE: dict(name=proof(node.secret, ONE)), TWO: dict(name=proof(node.secret, ONE))}
    def api(route, data=None, method=None):
        if method:
            writes.append((route, data))
            return None
        return {'cluster/pending/devices': pending, 'config/devices': [],
                'config/folders/fixture': dict(devices=[]), 'system/connections': dict(connections={})}[route]
    node.request = api
    node.poll()
    assert len(writes) == 2 and writes[0][1]['deviceID'] == ONE
    assert not writes[0][1]['autoAcceptFolders']
    writes.clear()
    node.secret = 'wrong-group-secret'
    node.poll()
    assert not writes


def test_no_proxy_environment_does_not_hide_windows_mixed_proxy():
    with patch('quota_guard.autolink.urllib.request.getproxies_registry', return_value={'http': 'http://127.0.0.1:1234'}), \
         patch('quota_guard.autolink.urllib.request.getproxies', return_value={'no': 'localhost'}):
        assert system_proxies()['http'] == 'http://127.0.0.1:1234'


def test_windows_child_job_ends_only_its_own_child():
    import subprocess
    import sys
    from quota_guard.child_job import ChildJob
    process = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], creationflags=0x08000004)
    job = None
    try:
        job = ChildJob(process)
        assert process.poll() is None
        job.close()
        assert process.wait(timeout=5) is not None
    finally:
        if job:
            job.close()
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=5)
