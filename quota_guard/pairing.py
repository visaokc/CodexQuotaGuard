import base64
import ctypes
import hashlib
import json
import os
import secrets
import time
from urllib.parse import urlparse

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .storage import atomic_json

PAIR_FIELDS = ('rendezvous_url', 'fingerprint', 'relay_token', 'group_secret', 'stun_url')


def validate_url(url, local_test=False):
    p = urlparse(url)
    allowed = p.scheme == 'wss' or (local_test and p.scheme == 'ws' and p.hostname in ('127.0.0.1', 'localhost'))
    if not allowed or not p.hostname or p.username or p.password or p.query or p.fragment:
        raise ValueError('发现服务须使用 wss:// 地址（公开 TLS 服务），不含账号密码或查询参数')


def create_code(config):
    if config.get('link_enabled'):
        from .autolink import DEVICE
        peers = list(dict.fromkeys([config.get('link_device', ''), *config.get('link_peers', [])]))
        if not peers or len(peers) > 16 or any(not DEVICE.fullmatch(p) for p in peers) or len(config['group_secret']) < 32:
            raise ValueError('本机连接身份尚未准备完成，请点击重试')
        payload = dict(version=2, secret=config['group_secret'], peers=peers)
        if config.get('link_addresses'):
            payload['addresses'] = {p: config['link_addresses'][p] for p in peers if p in config['link_addresses']}
        data = json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()
        return 'CQG2.'+base64.urlsafe_b64encode(data).decode().rstrip('=')+'.'+hashlib.sha256(data).hexdigest()[:12]
    validate_url(config['rendezvous_url'])
    if len(config['group_secret']) < 32 or len(config['relay_token']) < 24:
        raise ValueError('请先配置服务地址与访问密钥，再生成匹配码')
    payload = dict(version=1, **{k: config[k] for k in PAIR_FIELDS})
    data = json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()
    return 'CQG1.'+base64.urlsafe_b64encode(data).decode().rstrip('=')+'.'+hashlib.sha256(data).hexdigest()[:12]


def read_code(text):
    if len(text) > 8192:
        raise ValueError('匹配码过长')
    try:
        prefix, encoded, checksum = ''.join(text.split()).split('.')
        data = base64.urlsafe_b64decode(encoded+'='*(-len(encoded) % 4))
        if prefix not in ('CQG1', 'CQG2') or checksum != hashlib.sha256(data).hexdigest()[:12]:
            raise ValueError()
        payload = json.loads(data)
        if prefix == 'CQG2':
            from .autolink import DEVICE, relay_address
            peers, secret = payload['peers'], payload['secret']
            if payload['version'] != 2 or not isinstance(secret, str) or not 32 <= len(secret) <= 128 \
                    or not isinstance(peers, list) or not 1 <= len(peers) <= 16 \
                    or any(not isinstance(p, str) or not DEVICE.fullmatch(p) for p in peers):
                raise ValueError()
            addresses = payload.get('addresses', {})
            if not isinstance(addresses, dict) or any(p not in peers or not isinstance(values, list)
                or len(values) > 4 or any(not isinstance(a, str) or len(a) > 1024 or not relay_address(a) for a in values)
                for p, values in addresses.items()):
                raise ValueError()
            return dict(link_enabled=True, link_peers=list(dict.fromkeys(peers)), group_secret=secret, link_addresses=addresses,
                        rendezvous_url='', relay_token='', fingerprint='')
        if payload['version'] != 1:
            raise ValueError()
        result = {k: payload[k] for k in PAIR_FIELDS}
        validate_url(result['rendezvous_url'])
        if any(not isinstance(v, str) for v in result.values()) or len(result['group_secret']) < 32 or len(result['relay_token']) < 24:
            raise ValueError()
        return dict(result, link_enabled=False)
    except (ValueError, KeyError, TypeError):
        raise ValueError('匹配码无效、复制不完整或版本不兼容') from None


class Cipher:
    def __init__(self, secret, account):
        # A high-entropy invitation secret, not a guessable PIN or account ID.
        key = hashlib.sha256(('CQG1\0'+secret+'\0'+account).encode()).digest()
        self.aes = AESGCM(key)
        self.room = hashlib.sha256(b'room\0'+key).hexdigest()
        self.seen = {}

    def seal(self, sender, recipient, kind, value):
        header = dict(sender=sender, recipient=recipient, kind=kind, id=secrets.token_hex(16), at=time.time())
        aad = json.dumps(header, sort_keys=True, separators=(',', ':')).encode()
        nonce = os.urandom(12)
        ciphertext = self.aes.encrypt(nonce, json.dumps(value, separators=(',', ':')).encode(), aad)
        return dict(header, data=base64.b64encode(nonce+ciphertext).decode())

    def open(self, envelope, recipient):
        if envelope['recipient'] != recipient or abs(time.time()-envelope['at']) > 180:
            raise ValueError('接收方或消息时间不匹配，请同步 Windows 时间')
        header = {k: envelope[k] for k in ('sender', 'recipient', 'kind', 'id', 'at')}
        aad = json.dumps(header, sort_keys=True, separators=(',', ':')).encode()
        raw = base64.b64decode(envelope['data'], validate=True)
        clear = self.aes.decrypt(raw[:12], raw[12:], aad)
        if header['id'] in self.seen:
            raise ValueError('重复消息')
        now = time.time()
        self.seen = {k: v for k, v in self.seen.items() if now-v < 180}
        self.seen[header['id']] = now
        return json.loads(clear)


class Blob(ctypes.Structure):
    _fields_ = [('size', ctypes.c_uint32), ('data', ctypes.POINTER(ctypes.c_ubyte))]


def dpapi(raw, decrypt=False):
    if os.name != 'nt':
        raise RuntimeError('客户端凭据存储仅支持 Windows')
    buffer = ctypes.create_string_buffer(raw)
    source = Blob(len(raw), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    target = Blob()
    api = ctypes.windll.crypt32.CryptUnprotectData if decrypt else ctypes.windll.crypt32.CryptProtectData
    if not api(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(target.data, target.size)
    finally:
        ctypes.windll.kernel32.LocalFree(ctypes.cast(target.data, ctypes.c_void_p))


def save_config(path, config):
    public = dict(config)
    private = {k: public.pop(k) for k in ('group_secret', 'relay_token')}
    public['protected_secrets'] = base64.b64encode(dpapi(json.dumps(private).encode())).decode()
    atomic_json(path, public)


def load_config(path):
    value = json.loads(path.read_text(encoding='utf-8'))
    protected = value.pop('protected_secrets', None)
    if protected:
        value.update(json.loads(dpapi(base64.b64decode(protected), decrypt=True)))
    return value
