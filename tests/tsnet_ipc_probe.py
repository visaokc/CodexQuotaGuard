"""Windows regression: real HTTP IPC must consume bodies before connection close."""
from pathlib import Path
import sys
import tempfile
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from quota_guard.tsnet_node import EmbeddedNode
with tempfile.TemporaryDirectory(prefix='cqg-ipc-regression-') as folder:
    node = EmbeddedNode(folder, 'isolated-ipc-regression')
    try:
        node.start()
        pid = node.process.pid
        for size in (0, 32000):
            for i in range(600):
                node.request('status' if i % 4 == 0 else 'receive', {'probe': 'x'*size})
        assert node.process.pid == pid and node.process.poll() is None
        print('REAL_WINDOWS_IPC_1200_REQUESTS_ZERO_RESETS_OK')
    finally:
        node.close()
