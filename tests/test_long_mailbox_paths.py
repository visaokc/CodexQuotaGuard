"""Exercise mailbox IO on Windows without requiring long-path policy changes."""
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from quota_guard.file_mesh import FileMesh
from quota_guard.storage import defaults


@pytest.mark.skipif(os.name != 'nt', reason='Windows legacy path compatibility')
def test_long_mailbox_write_and_receive_without_long_path_opt_in(tmp_path, monkeypatch):
    original_open = Path.open

    def legacy_open(path, *args, **kwargs):
        if len(str(path)) >= 260 and not str(path).startswith('\\\\?\\'):
            raise FileNotFoundError(2, 'Legacy Windows path limit', str(path))
        return original_open(path, *args, **kwargs)

    # Reproduce machines that have not opted into Win32 long paths.
    monkeypatch.setattr(Path, 'open', legacy_open)
    shared = tmp_path / ('g' * 100)
    shared.mkdir()
    config = defaults()
    config.update(device_id='sender', group_secret='s' * 43)
    sender = FileMesh(config, 'fixture-account', lambda *_: None)
    received = []
    receiver = FileMesh(dict(config, device_id='receiver'), 'fixture-account',
                        lambda peer, data: received.append((peer, data)))
    node = SimpleNamespace(shared=shared, process=SimpleNamespace(poll=lambda: None))
    sender.node = receiver.node = node
    assert len(str(shared / sender._path('*', 'hello').name)) >= 260
    device = 'AAAAAAA-' * 7 + 'AAAAAAA'
    sender._write('*', 'hello', dict(link_device=device), 'hello')
    sender.send('receiver', dict(type='facts', value=1))
    sender.send('receiver', dict(type='sync', value=2))
    receiver._read()
    assert receiver.peers['sender']['link_device'] == device
    assert sorted(value['type'] for _, value in received) == ['facts', 'sync']
    # Existing naming / old-client readers remain compatible.
    assert all(p.name.startswith(sender.cipher.room + '-') for p in shared.glob('*.cqg'))
    assert len(list(shared.glob('*.cqg'))) == 3
    assert not list(shared.glob('*.tmp'))


@pytest.mark.skipif(os.name != 'nt', reason='Windows extended path syntax')
@pytest.mark.parametrize(('raw', 'expected'), [
    (r'C:\Users\Administrator\mailbox', r'\\?\C:\Users\Administrator\mailbox'),
    (r'\\server\share\mailbox', r'\\?\UNC\server\share\mailbox'),
    (r'\\?\C:\mailbox', r'\\?\C:\mailbox'),
    (r'\\?\UNC\server\share\mailbox', r'\\?\UNC\server\share\mailbox'),
])
def test_mailbox_extended_path_preserves_drive_and_unc_roots(raw, expected):
    mesh = FileMesh.__new__(FileMesh)
    mesh.node = SimpleNamespace(shared=Path(raw))
    assert str(mesh._shared_path()) == expected
    assert str(mesh.node.shared) == raw
