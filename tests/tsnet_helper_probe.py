"""Real Windows embedded-node smoke; never enrolls a node or changes system networking."""
import json
from pathlib import Path
import sys
import time
import urllib.error
import urllib.request
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from quota_guard.tsnet_node import EmbeddedNode, auth_url

root = Path('work')/('tsnet-smoke-'+uuid.uuid4().hex[:8])
node = EmbeddedNode(root, 'isolated-tsnet-probe')
try:
    node.start()
    node.request('login')
    for _ in range(20):
        state = node.request('status')
        if state.get('auth_url'):
            break
        time.sleep(1)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        opener.open(urllib.request.Request(node.api+'/status', data=b'{}'), timeout=5)
        raise AssertionError('IPC allowed unauthenticated request')
    except urllib.error.HTTPError as e:
        assert e.code == 401
    result = dict(state=state.get('state'), ready=state.get('ready'),
                  auth_url_available=bool(state.get('auth_url')),
                  auth_url_valid=auth_url(state['auth_url']) if state.get('auth_url') else None,
                  ipc_rejects_unauthenticated=True, helper_bytes=Path('vendor/tsnet/cqg-tsnet.exe').stat().st_size)
finally:
    node.close()
result['clean_exit'] = node.process.poll() is not None
(root/'result.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
print(json.dumps(result))
print(root/'result.json')
