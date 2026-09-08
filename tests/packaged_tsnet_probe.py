"""Check frozen client startup and extracted embedded helper, using disposable state."""
import importlib.util
from pathlib import Path
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PyInstaller.archive.readers import CArchiveReader
from quota_guard import __version__
from quota_guard.tsnet_node import EmbeddedNode, auth_url

spec = importlib.util.spec_from_file_location('old_packaged_probe', Path(__file__).with_name('packaged_probe.py'))
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)
probe.client('demo')
probe.client('untracked')

archive = CArchiveReader(str(Path('dist')/__version__/'Codex配额管家.exe'))
with tempfile.TemporaryDirectory(prefix='cqg-tsnet-frozen-') as temp:
    root = Path(temp)
    for name in ('cqg-tsnet.exe', 'manifest.json'):
        path = root/'vendor'/'tsnet'/name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(archive.extract('vendor\\tsnet\\'+name))
    sys._MEIPASS = temp
    node = EmbeddedNode(root/'state', 'frozen-isolated-probe')
    try:
        node.start()
        node.request('login')
        deadline = time.monotonic()+25
        while time.monotonic()<deadline:
            state = node.request('status')
            if state.get('auth_url'):
                break
            time.sleep(1)
        assert auth_url(state.get('auth_url', '')), 'Frozen helper did not obtain official login URL'
        print('FROZEN_TSNET_BROWSER_AUTH_URL_OK')
    finally:
        node.close()
        del sys._MEIPASS
    assert node.process.poll() is not None
print('PACKAGED_TSNET_PROBE_OK (no enrollment, no real account or network configuration changes)')
