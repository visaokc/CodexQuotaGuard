import base64
import hashlib
from io import BytesIO
from unittest.mock import patch

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from quota_guard import updater as u


@pytest.fixture
def signed(monkeypatch):
    key = Ed25519PrivateKey.generate()
    monkeypatch.setattr(u, 'PUBLIC_KEY', base64.b64encode(key.public_key().public_bytes_raw()).decode())
    def sign(data=b'MZ-test', **changes):
        payload = dict(schema=1, version='0.3.0', size=len(data), sha256=hashlib.sha256(data).hexdigest(),
            url=f'https://github.com/{u.REPOSITORY}/releases/download/v0.3.0/CodexQuotaGuard-0.3.0.exe')
        payload.update(changes)
        return dict(payload=payload, signature=base64.b64encode(key.sign(u.canonical(payload))).decode())
    return sign


def test_signature_tamper_and_no_downgrade(signed):
    m = signed()
    assert u.verify(m, '0.2.0')['version'] == '0.3.0'
    assert u.verify(m, '0.3.0') is None
    assert u.verify(m, '0.4.0') is None
    m['payload']['size'] += 1
    with pytest.raises(Exception):
        u.verify(m)


@pytest.mark.parametrize('changes', [dict(version='0.3.0-beta'), dict(schema=2), dict(size=0),
    dict(size=u.MAX_SIZE+1), dict(size=True), dict(sha256='bad'), dict(url='https://evil.example/app.exe')])
def test_reject_even_signed_invalid_metadata(signed, changes):
    with pytest.raises(ValueError):
        u.verify(signed(**changes))


@pytest.mark.parametrize('url', ['http://github.com/a', 'https://github.com.evil/a',
    'https://user@github.com/a', 'https://github.com:444/a', 'file:///C:/a.exe'])
def test_redirect_allowlist(url):
    with pytest.raises(ValueError):
        u.safe_url(url)


def test_download_verified_cached_and_tamper_preserves_install(signed, tmp_path):
    data = b'MZ-example'
    manifest = signed(data)
    with patch.object(u, 'open_url', return_value=BytesIO(data)) as network:
        result = u.download(manifest, tmp_path)
        assert result.read_bytes() == data
        assert u.download(manifest, tmp_path) == result
        network.assert_called_once()
    result.unlink()
    for bad in (b'bad', data+b'extra', b'X'*len(data)):
        with patch.object(u, 'open_url', return_value=BytesIO(bad)), pytest.raises(ValueError):
            u.download(manifest, tmp_path)
        assert not result.exists() and not list(tmp_path.glob('*.part'))


def test_manifest_read_is_bounded():
    with patch.object(u, 'open_url', return_value=BytesIO(b'x'*16385)), pytest.raises(ValueError):
        u.check('0.2.0')


def test_auto_update_default_and_suppressed_in_demo():
    from quota_guard.storage import defaults
    from quota_guard.gui import App
    from types import SimpleNamespace
    status = []
    app = SimpleNamespace(update_running=False, busy=False, demo=True, update_status=SimpleNamespace(set=status.append))
    App.check_update(app, True)
    assert defaults()['auto_update'] is True
    assert '不安装更新' in status[0]


def test_onefile_bootloader_lock_is_retried():
    with patch.object(u.os, 'replace', side_effect=[PermissionError(), None]) as replace, patch.object(u.time, 'sleep'):
        u.replace_when_unlocked('new', 'app')
        assert replace.call_count == 2


def test_installer_defers_without_restoring_account_block():
    from quota_guard.gui import App
    from types import SimpleNamespace
    from unittest.mock import Mock
    engine = Mock(blocked=True)
    app = SimpleNamespace(busy=False, engine=engine, update_status=Mock(), root=Mock(), retry_update=lambda: None)
    App.install_update(app)
    engine.close.assert_not_called()
    engine.restore.assert_not_called()
    app.root.after.assert_called_once()
