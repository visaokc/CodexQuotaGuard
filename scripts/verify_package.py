"""Check the actual frozen artifacts contain current code and exact bundled helpers."""
import hashlib
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PyInstaller.archive.readers import CArchiveReader
from quota_guard import __version__
from quota_guard.autolink import BINARY_SHA256

root = Path(__file__).resolve().parents[1]
client = CArchiveReader(str(root/'dist'/__version__/'Codex配额管家.exe'))
pyz = client.open_embedded_archive('PYZ.pyz')
for path in (root/'quota_guard').glob('*.py'):
    module = 'quota_guard' if path.stem == '__init__' else 'quota_guard.'+path.stem
    if module not in pyz.toc:
        continue
    actual = pyz.extract(module)
    assert actual == compile(path.read_bytes(), actual.co_filename, 'exec'), module+' differs from source'
native = client.extract('vendor\\syncthing\\syncthing.exe')
assert hashlib.sha256(native).hexdigest() == BINARY_SHA256
helper = root/'work'/'updater'/'CodexQuotaUpdater.exe'
assert client.extract('vendor\\CodexQuotaUpdater.exe') == helper.read_bytes()
archive = CArchiveReader(str(helper)).open_embedded_archive('PYZ.pyz')
actual = archive.extract('quota_guard.updater')
assert actual == compile((root/'quota_guard'/'updater.py').read_bytes(), actual.co_filename, 'exec')
assert any(p.startswith('licenses\\') for p in client.toc)
print('FROZEN_CODE_NATIVE_HELPER_LICENSES_MATCH_SOURCE_OK')
