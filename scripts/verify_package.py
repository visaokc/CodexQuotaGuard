"""Check the actual frozen artifacts contain current code and exact bundled helpers."""
import hashlib
import marshal
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PyInstaller.archive.readers import CArchiveReader
from quota_guard import __version__
import json

root = Path(__file__).resolve().parents[1]
client = CArchiveReader(str(root/'dist'/__version__/'Codex配额管家.exe'))
entry = marshal.loads(client.extract('main'))
assert entry == compile((root/'main.py').read_bytes(), entry.co_filename, 'exec')
pyz = client.open_embedded_archive('PYZ.pyz')
assert all('quota_guard.'+name in pyz.toc for name in ('analytics', 'web_host', 'web_controller', 'app_icon', 'shared_sync', 'shared_view', 'cycle_pair', 'shared_policy', 'shared_quota', 'pool_accounting'))
assert client.extract('assets\\app.ico') == (root/'assets'/'app.ico').read_bytes()
for path in (root/'quota_guard').glob('*.py'):
    module = 'quota_guard' if path.stem == '__init__' else 'quota_guard.'+path.stem
    if module not in pyz.toc:
        continue
    actual = pyz.extract(module)
    assert actual == compile(path.read_bytes(), actual.co_filename, 'exec'), module+' differs from source'
assert 'webview.platforms.edgechromium' in pyz.toc
assert 'tkinter' not in pyz.toc and 'customtkinter' not in pyz.toc
for path in (root/'frontend'/'dist').rglob('*'):
    if path.is_file():
        name = 'frontend\\dist\\'+str(path.relative_to(root/'frontend'/'dist')).replace('/', '\\')
        assert client.extract(name) == path.read_bytes(), name+' differs from frontend build'
native = client.extract('vendor\\tsnet\\cqg-tsnet.exe')
manifest = json.loads(client.extract('vendor\\tsnet\\manifest.json'))
assert hashlib.sha256(native).hexdigest() == manifest['sha256']
assert native == (root/'vendor'/'tsnet'/'cqg-tsnet.exe').read_bytes()
assert not any('syncthing.exe' in p or p.startswith(('av.', 'av\\', 'pylibsrtp\\')) for p in client.toc)
assert 'quota_guard.tsnet_mesh' in pyz.toc and 'quota_guard.mesh' not in pyz.toc
helper = root/'work'/'updater'/'CodexQuotaUpdater.exe'
assert client.extract('vendor\\CodexQuotaUpdater.exe') == helper.read_bytes()
archive = CArchiveReader(str(helper)).open_embedded_archive('PYZ.pyz')
actual = archive.extract('quota_guard.updater')
assert actual == compile((root/'quota_guard'/'updater.py').read_bytes(), actual.co_filename, 'exec')
assert any(p.startswith('licenses\\') for p in client.toc)
print('FROZEN_CODE_NATIVE_HELPER_LICENSES_MATCH_SOURCE_OK')
