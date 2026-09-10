import hashlib
import json

import pytest

from quota_guard.tsnet_node import install_binary


def bundle(root, content):
    folder = root/'vendor'/'tsnet'
    folder.mkdir(parents=True)
    (folder/'cqg-tsnet.exe').write_bytes(content)
    (folder/'manifest.json').write_text(json.dumps({'sha256': hashlib.sha256(content).hexdigest()}))


def test_helper_path_survives_different_extraction_folders_and_verified_updates(tmp_path):
    first, second, third = [tmp_path/n for n in ('extraction-a', 'extraction-b', 'extraction-c')]
    bundle(first, b'version-one')
    bundle(second, b'version-one')
    bundle(third, b'version-two')
    data = tmp_path/'data'
    target = install_binary(first, data)
    assert target == data/'runtime'/'cqg-tsnet.exe'
    stamp = target.stat().st_mtime_ns
    assert install_binary(second, data) == target
    assert target.stat().st_mtime_ns == stamp
    assert install_binary(third, data) == target
    assert target.read_bytes() == b'version-two'
    target.write_bytes(b'corrupted-cache')
    assert install_binary(third, data).read_bytes() == b'version-two'


def test_unverified_bundle_never_replaces_existing_helper(tmp_path):
    root, data = tmp_path/'bundle', tmp_path/'data'
    bundle(root, b'verified')
    target = install_binary(root, data)
    (root/'vendor'/'tsnet'/'cqg-tsnet.exe').write_bytes(b'tampered')
    with pytest.raises(RuntimeError, match='校验失败'):
        install_binary(root, data)
    assert target.read_bytes() == b'verified'
