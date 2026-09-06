"""Run locally as the release owner. The DPAPI private key is never published."""
import base64
import hashlib
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from quota_guard.pairing import dpapi
from quota_guard import updater, __version__

root = Path(__file__).resolve().parents[1]
keyfile = root/'.private'/'update-signing-key.dpapi'
if sys.argv[1:] == ['--init']:
    keyfile.parent.mkdir(exist_ok=True)
    if not keyfile.exists():
        keyfile.write_bytes(dpapi(Ed25519PrivateKey.generate().private_bytes_raw()))
    key = Ed25519PrivateKey.from_private_bytes(dpapi(keyfile.read_bytes(), decrypt=True))
    print(base64.b64encode(key.public_key().public_bytes_raw()).decode())
else:
    key = Ed25519PrivateKey.from_private_bytes(dpapi(keyfile.read_bytes(), decrypt=True))
    if base64.b64encode(key.public_key().public_bytes_raw()).decode() != updater.PUBLIC_KEY:
        raise ValueError('Release key does not match the embedded public key')
    exe, dest = map(Path, sys.argv[1:3])
    payload = dict(schema=1, version=__version__,
        url=f'https://github.com/{updater.REPOSITORY}/releases/download/v{__version__}/CodexQuotaGuard-{__version__}.exe',
        size=exe.stat().st_size, sha256=hashlib.sha256(exe.read_bytes()).hexdigest())
    manifest = dict(payload=payload, signature=base64.b64encode(key.sign(updater.canonical(payload))).decode())
    updater.verify(manifest)
    dest.write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    print('SIGNED_UPDATE_MANIFEST_OK', payload['version'], payload['sha256'])
