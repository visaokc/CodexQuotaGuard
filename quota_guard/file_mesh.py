"""Encrypted anti-entropy mailboxes transported by the embedded Syncthing runtime."""
import hashlib
import json
import threading
import time

from .autolink import LinkNode
from .pairing import Cipher
from .storage import atomic_json


class FileMesh:
    def __init__(self, config, account, on_message, local_test=False):
        self.config, self.account = dict(config), account
        self.device = config['device_id']
        self.on_message, self.local_test = on_message, local_test
        self.cipher = Cipher(config['group_secret'], 'account-policy-v2:'+account)
        self.stop = threading.Event()
        self.lock = threading.RLock()
        self.node = None
        self.thread = None
        self.peers = {}
        self.received = {}
        self.status = '正在自动建立连接…'

    def start(self):
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _path(self, recipient, slot):
        sender = hashlib.sha256(self.device.encode()).hexdigest()[:24]
        target = hashlib.sha256(recipient.encode()).hexdigest()[:24]
        return self.node.shared/f'{self.cipher.room}-{sender}-{target}-{slot}.cqg'

    def _write(self, recipient, kind, value, slot):
        with self.lock:
            if not self.node or not self.node.process or self.node.process.poll() is not None:
                return
            envelope = self.cipher.seal(self.device, recipient, kind, value)
            if len(json.dumps(envelope)) > 240*1024:
                raise ValueError('同步消息过大')
            atomic_json(self._path(recipient, slot), envelope)

    def send(self, peer, value):
        # Facts and vectors must not overwrite each other before replication.
        slot = 'facts' if value.get('type') == 'facts' else 'sync'
        self._write(peer, 'app', value, slot)

    def _read(self):
        for path in self.node.shared.glob(self.cipher.room+'-*.cqg'):
            try:
                if path.stat().st_size > 256*1024 or path.is_symlink():
                    continue
                raw = path.read_bytes()
                digest = hashlib.sha256(raw).digest()
                if self.received.get(path.name) == digest:
                    continue
                self.received[path.name] = digest
                envelope = json.loads(raw)
                if envelope['sender'] == self.device:
                    continue
                recipient = '*' if envelope.get('kind') == 'hello' else self.device
                value = self.cipher.open(envelope, recipient)
                peer = envelope['sender']
                if not isinstance(peer, str) or not 1 <= len(peer) <= 100:
                    continue
                if envelope['kind'] == 'hello':
                    link = self.node.connections.get(value.get('link_device'), {})
                    if not link.get('connected'):
                        # Retry this hello after the authenticated transport reconnects.
                        self.received.pop(path.name, None)
                        continue
                    self.peers[peer] = dict(route='公共加密中转' if 'relay' in link.get('type', '').lower() else 'P2P 直连',
                        last_seen=time.time(), link_device=value['link_device'])
                elif envelope['kind'] == 'app':
                    self.on_message(peer, value)
            except (OSError, ValueError, KeyError, TypeError):
                continue
            except Exception:
                # Invalid AES-GCM authentication never reaches the ledger.
                continue

    def _run(self):
        while not self.stop.is_set():
            try:
                self.node = LinkNode(self.config['_data_dir'], self.config, self.local_test)
                self.node.start()
                last_hello = last_poll = 0
                while not self.stop.is_set():
                    now = time.time()
                    if self.node.process.poll() is not None:
                        raise RuntimeError('自动连接组件退出，正在重连')
                    if now-last_poll >= 5:
                        self.node.poll()
                        last_poll = now
                    if now-last_hello >= 15:
                        self._write('*', 'hello', dict(link_device=self.node.device), 'hello')
                        last_hello = now
                    with self.lock:
                        self._read()
                    peers = self.peer_states()
                    self.status = f'自动连接 · {len(peers)} 台同账号设备在线' if peers else '自动连接已启动 · 等待对方上线／输入匹配码'
                    self.stop.wait(1)
            except Exception as e:
                self.status = '自动连接重试中 · '+type(e).__name__
            finally:
                if self.node:
                    self.node.close()
            self.stop.wait(5)

    def peer_states(self):
        now = time.time()
        with self.lock:
            return {peer: dict(state) for peer, state in self.peers.items()
                    if now-state['last_seen'] < 90 and self.node
                    and self.node.connections.get(state.get('link_device'), {}).get('connected')}

    def close(self):
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=40)
            if self.thread.is_alive():
                raise RuntimeError('自动连接尚未退出，请稍后再试')
